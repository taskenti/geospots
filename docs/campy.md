# 🏕️ Campy Scraper

## 📖 Información General
**Campy** es una app de "microcamping" europea con foco en la región DACH (Alemania, Austria, Suiza), aunque también cubre puntos de Francia, Italia y los Países Bajos. Su nicho son granjas pequeñas, viñedos y terrenos privados que admiten campistas para estancias cortas (1-3 noches). Se diferencia de plataformas como Park4Night en que **todos los spots están verificados por el equipo** y son de pago a través de la propia app.

Cobertura observada en producción: ~150 spots por celda 1°×1° en zona alpina densa. Los tipos predominantes son `camp` (74%) y `microcamping` (12%).

## 🛠️ Arquitectura y Funcionamiento

### Endpoint GraphQL

```
POST https://graphql-server-132719581042.europe-west1.run.app/
Content-Type: application/json
```

Una sola query GraphQL `LocationsWithinRadius` con parámetros lat/lng/radius/filters. El scraper convierte cada celda del grid (1°×1°) en una llamada centrada con radio de 90km (cubre los ~78km de la diagonal de la celda con margen).

### Pipeline (dos fases)
- **Fase 1 — descubrimiento (`run` heredado de `AbstractSource`):** el grid activo genera puntos centrales que se mapean a `fetch_cell` → POST GraphQL `LocationsWithinRadius`. Esta query de **lista** devuelve campos limitados (ver abajo) y NO trae reviews, contacto ni facilities pobladas.
- **Fase 2 — enriquecimiento (`download_reviews`):** por cada source_record campy se llama `LocationFull(uid, language)` (query de **detalle**), que SÍ devuelve la información rica. El endpoint GraphQL **no requiere autenticación** (verificado 2026-05-29). Aporta:
  - `reviews[]` — reseñas chupadas de **Google** (a veces también nativas de campy, con `externalSource: null`). Cada una: `id` (único, formato `google<digits><uid>`), `rating`, `comment`, `updatedAt` (epoch ms), `userDisplayName`, `translation{comment, sourceLanguage}`. → tabla `reviews`.
  - `website` / `email` / `phone` → columnas `web` / `email` / `telefono`. **Este es el mayor valor añadido** de campy: contacto directo verificado del establecimiento.
  - `facilities[]` pobladas (wifi/wc/ducha/agua/electricidad/vaciados/perros) → columnas de servicios.
  - `reviewSummary{pros, cons, summary}` — resumen IA generado por el bot **"sam"** de campy. Se guarda en `servicios_extras.campy_review_summary` como **metadata del spot, NUNCA como review** (no contamina el corpus de reseñas).
  - `reviewsCount` → `source_records.review_count` (esperado) + marca `details_fetched=true` para re-descargas incrementales.

## 🗂️ Mapeo y Normalización

### Tipos
La API devuelve tipos reales: `camp`, `van`, `microcamping`. Mapeo:
- `camp` / `camping` / `microcamping` → `camping`
- `van` → `area_ac`
- `parking` → `parking_publico` *(defensivo, no observado en producción)*
- otros → `naturaleza`

### Campos siempre poblados (validados sobre 150 spots reales)
| Campo Campy | GeoSpots | Notas |
|---|---|---|
| `uid` | `source_id` | string, formato `<timestamp><country>` |
| `title` | `nombre` | con fallback "Campy Spot" |
| `latitude`/`longitude` | `lat`/`lon` | con `coords_validas` (heredado de base) |
| `type` | `tipo` | mapeo arriba |
| `campsite_campy_rating` | `rating_promedio` | rating del equipo Campy (no users) |
| `image` | `fotos_urls[]` | 1 imagen principal |
| `description` | `descripcion_<lang>` | con detect_language |
| `address` / `city` | `region` | preferimos city, fallback address |

### Campos nulos en la query de LISTA (`LocationsWithinRadius`)
- `price`: el precio solo se carga en el checkout privado de la app → `gratuito = None` (desconocido), nunca `False`
- `facilities[]` viene vacío en la lista, pero **sí poblado en `LocationFull`** (fase 2)
- `places`, `rating`, `country`, `dateOpenFrom`, `dateOpenTo`, `camperSize`, `isTopQuality`
- `website`/`email`/`phone`/`reviews`/`reviewSummary`: NO existen en la lista; solo en `LocationFull` (fase 2)

## 🔧 Auditoría y Fixes Mayo 2026

### Estado pre-auditoría
Scraper recién creado por Gemini, 150 source_records descargados, `fuentes_config.activa = false` (desactivada por el usuario antes de validar). 4 bugs detectados:

| # | Bug | Impacto | Fix |
|---|---|---|---|
| 1 | `microcamping` no mapeado → cae en "naturaleza" | 13 spots mal clasificados como naturaleza siendo agroturismo | Mapeo extendido. UPDATE SQL aplicado sobre los 13 ya en DB. |
| 2 | `gratuito = False` cuando `price` viene null | 37 spots informativos como "gratuitos" siendo desconocido (precio en checkout privado) | `gratuito = None` por defecto. UPDATE SQL aplicado sobre los 37 ya en DB. |
| 3 | `web = "https://campy.app/"` fijo (no específico del spot) | URL inútil que confunde al usuario en el panel | Eliminado, queda `None` hasta que Campy exponga permalinks |
| 4 | `rating` y `country` siempre null en API → 0 ratings capturados | Sin métricas de calidad ni clasificación geográfica desde la fuente | Uso de `campsite_campy_rating` (150/150 poblado). country queda al trigger PostGIS por lat/lon |

### Validación post-fix
Sobre 150 spots vivos de la API:
- `microcamping_as_camping`: 18/18 ✓
- `gratuito_None`: 150/150 ✓ (0 falsos positivos de gratuito)
- `rating_set`: 150/150 ✓
- `region_set`: 150/150 ✓
- DB roundtrip 30 spots: 0 errores

### Cleanup DB
- 37 `gratuito=False` falsos → NULL
- 13 spots `tipo=naturaleza` mal puestos por microcamping → `tipo=camping`
- `fuentes_config.activa` reactivado a `true`

## 🚀 Mejora 2026-05-29 — Fase 2 (LocationFull) + subida de prioridad

Descubierto que el bug #3 ("Campy no expone permalink/web") era falso: la query de **detalle** `LocationFull` (no la de lista) devuelve contacto completo, reviews y resumen IA, **sin autenticación**. Se añadió `download_reviews()` (ver Pipeline arriba).

| Cambio | Detalle |
|---|---|
| `download_reviews()` nuevo | Reviews de Google + contacto (web/email/tel) + facilities + resumen "sam" |
| Bug latente en `extract_campy` | Trataba `facilities` (dicts `{title, available}`) como strings → corregido para leer `title` solo de las disponibles |
| Credibilidad subida | `base_score` 0.75→0.82, `review_quality` 0.72→0.82 — a la par de campercontact/caramaps, por debajo de park4night (0.92) |

Validación en vivo (uid `1530285951571NL`): website/email/phone poblados, 49 reviews, 7 facilities, resumen IA presente. Roundtrip a DB (enriquecer + upsert reviews) sin errores.

**Lanzar la fase 2:** `python scheduler.py --reviews campy` (dentro del contenedor scraper).

---
**Estado Actual:** Auditado y operativo, **ahora con reviews y contacto**. Fase 1 (lista) + Fase 2 (detalle/LocationFull) desacopladas como park4night/campercontact.
