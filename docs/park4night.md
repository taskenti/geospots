# 🏕️ Park4Night Scraper

## 📖 Información General
**park4night** (lanzada en 2011) es, indiscutiblemente, la aplicación reina del movimiento "vanlife". Con más de 6 millones de descargas, es la plataforma colaborativa líder en Europa y el mundo para la búsqueda de lugares de pernocta, áreas de descanso y espacios en plena naturaleza. Su fortaleza radica en el inmenso volumen de reseñas actualizadas casi en tiempo real por una comunidad hiperactiva, lo que la convierte en una herramienta imprescindible para viajes improvisados y "slow travel". 

## 🛠️ Arquitectura y Funcionamiento
El scraper `park4night.py` es uno de los motores más agresivos de la infraestructura de GeoSpots y la mayor fuente de datos: **~305K spots + 1.1M reviews** en producción. Debido a las medidas anti-scraping de P4N, la arquitectura combina cuatro estrategias:

1. **La API de Invitado (Guest API)**:
   - Ataca directamente a la versión `V4.1` de la API de invitados (`guest.park4night.com/services/V4.1/lieuxGetFilter.php`). Devuelve hasta **100 spots por petición** (cap del servidor).
2. **Grid Activo Dilatado + Quadtree Adaptativo**:
   - En el primer arranque (DB vacía) usa grid global 2°×2°. En arranques posteriores **lee las celdas 1°×1° con spots existentes** y las **dilata con buffer de 4 celdas** para cubrir territorio adyacente. Esto evita escanear océano/desierto donde nunca habrá spots.
   - Cada punto de grid se procesa con una cola asíncrona (`asyncio.Queue`). Si una consulta devuelve exactamente 100 spots (cap), **subdivide el punto en 4 cuadrantes más pequeños** y los añade a la cola. Se subdivide hasta `max_depth=3` (resolución mínima 0.125° si arrancó en 1°, o 0.25° si arrancó en 2°).
   - El array inicial se **baraja aleatoriamente** (`random.shuffle`) y los 5 User-Agents rotan para diluir el patrón de escaneo lineal.
3. **Resiliencia (Tenacity + 429)**:
   - `_fetch_json` envuelto en `@retry` con backoff exponencial (4-16s, 3 intentos). Si el servidor responde 429, el worker duerme 60s extra antes del reraise.
4. **Phase 2 — Descarga desacoplada de reviews** (`download_reviews()`):
   - Lee `source_records` donde `review_count > 0 AND db_review_count < review_count` (incremental: solo spots que tienen reviews nuevas).
   - Cola asíncrona con `min(max_workers or 3, 5)` trabajadores. Por cada spot llama a `commGet.php?lieu_id=X`, parsea las reviews y las inserta vía `upsert_review` (cuenta solo INSERTs reales).
   - Ordena por `review_count DESC` para procesar primero los spots populares.

## 🧠 Lógica de Mapeo y Normalización
- **Códigos de Tipo**: Park4Night usa letras (`code`) o ids numéricos (`id_type`) para definir lugares. Nuestro script traduce `"A"`/`1` → `area_ac`, `"P"`/`2` → `parking`, `"C"`/`3` → `camping`, `"N"`/`4` → `naturaleza`, `5` → `picnic`.
- **Dimensiones**: Extrae `hauteur_limite` → `altura_max_m`, `nb_places` → `num_plazas`.
- **Servicios**: Traduce los flags `point_eau`, `eau_noire`, `eau_usee`, `electricite`, `douche`, `wifi`, `wc_public`, `animaux`, `camping_car` a las columnas booleanas de GeoSpots vía el helper `_b()`.
- **Multiidioma**: las 6 descripciones (`description_fr/en/de/es/it/nl`) mapean directamente.

### Parsing defensivo (hardening 2026-05-25)
La API de P4N devuelve **todos los campos numéricos como strings** (e.g. `note_moyenne: "2.50"`, `nb_commentaires: "15"`, `hauteur_limite: "2.00"`). Para resistir respuestas malformadas se introdujeron tres helpers:
- `_to_int_safe(v)` y `_to_float_safe(v)`: devuelven `None` ante `""`, `"N/A"`, `"inconnu"` u otros valores no convertibles. Aceptan también comas decimales (`"3,14"`).
- `_b(raw, key)`: extendido para aceptar `"1"`/`"0"`, `"true"`/`"false"`, `"oui"`/`"non"`, `True`/`False` boolean, o `None`. Antes solo entendía `"1"`.
- `coords_validas()` aplicado tras `normalize()` (convención centralizada).

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Bloqueo Definitivo de IP (IP Ban)**:
   - Al lanzar miles de peticiones `GET` contra los endpoints `lieuxGetFilter.php` y `commGet.php`, el riesgo de ser baneados a nivel de red por un WAF (Web Application Firewall) como Cloudflare es crítico. Aunque controlamos los errores 429, un escaneo intensivo desde una IP residencial/NAS puede acabar en lista negra permanente.
2. **Dependencia de una API Abierta (Guest API Volatility)**:
   - Estamos explotando un endpoint diseñado para usuarios no registrados. Si la empresa desarrolladora (AppMob) decide forzar la autenticación obligatoria para ver el mapa o cambia a una API cifrada, el scraper quedará inutilizado inmediatamente.
3. **Carga en la Base de Datos**:
   - Dado que P4N tiene cientos de miles de reseñas de alta calidad, la doble pasada del scraper (primero el lugar, luego descargar todos sus comentarios) genera un flujo masivo de transacciones `INSERT` en PostgreSQL.
4. **Fotos en Calidad "Thumb"**:
   - Aunque logramos capturar las fotos (`link_large` y `link_thumb`), el nivel de compresión que aplica P4N hace que, visualmente, no tengan el mismo estándar premium que las imágenes extraídas de Nomady o Campspace.

## 🔧 Auditoría Mayo 2026

**Estado pre-auditoría**: 305.040 source_records, 300.373 spots, 1.116.586 reviews. Última ejecución completa con éxito: hace tiempo (los últimos 4 runs del 22-24 mayo quedaron en "running" zombie y se limpiaron en esta auditoría).

### Validación de la API actual
- `lieuxGetFilter.php`: HTTP 200, devuelve hasta 100 spots por punto. Todos los campos numéricos vienen como strings.
- `commGet.php`: 3/3 spots probados devolvieron `parsed_ok == expected` (7/7, 13/13, 26/26 reviews). Phase 2 funciona perfecta.
- Normalize sobre 100 spots reales: 100/100 OK.

### Bugs latentes corregidos
1. **Crashes potenciales por int/float raw**: `int(raw["id"])`, `float(rating_str)`, `int(nb_comm)`, etc. romperían el normalize si P4N empezara a devolver `"N/A"`, `""` o valores inesperados. Reemplazados por `_to_int_safe`/`_to_float_safe`.
2. **`config.max_workers` puede ser None**: en `download_reviews` ya tenía fallback (`min(config.max_workers or 3, 5)`), pero en Phase 1 estaba como `range(config.max_workers)` directamente → `TypeError` si `max_workers` no se configuraba. Añadido fallback simétrico.
3. **`raw.get("photos", [])` rompía si `photos` venía como None explícito**: cambiado a `(raw.get("photos") or [])` + filtro `isinstance(f, dict)`.
4. **`_b()` solo entendía `"1"`**: extendido a `True`/`False` boolean, `"true"`, `"oui"`, `"yes"`.
5. **`coords_validas` no aplicado**: añadido en el worker de Phase 1 (convención obligatoria desde recomendación #9).
6. **`P4N_DETALLE` definido pero nunca usado**: comentado para evitar dead code.
7. **Doc desactualizado**: decía "barriendo Europa con grid fijo", el código real hace dilatación dinámica desde spots existentes + quadtree adaptivo + fallback global cuando la DB está vacía.

### Tests de defensa
- 11/11 casos `_b()` (booleans, strings raros, missing)
- 9/9 casos `_to_int_safe` (decimales, vacíos, "inconnu")
- 7/7 casos `_to_float_safe` (coma decimal, "N/A", None)
- 8/8 edge cases en normalize (id missing, photos None, photos non-dict, rating garbage, hauteur garbage, nb_places negativo/decimal, lat inválida)

### Cleanup DB
- 4 zombies de `scraper_log` (estado="running" desde 22-24 mayo) marcados como `zombie` con `terminado_en=NOW()`.

---
**Estado Actual:** Auditado y reforzado. La API actual está estable; los fixes son defensivos contra futuros cambios en la API. Considerado un "Scraper Pesado" que debe ejecutarse de forma esporádica o mediante proxies rotatorios para asegurar la supervivencia de la IP del servidor.
