# 🚐 Campercontact Scraper

## 📖 Información General
**Campercontact** es una aplicación móvil líder en Europa, desarrollada por la NKC (Nederlandse Kampeerauto Club). Cuenta con una base de datos de más de 60.000 ubicaciones en 58 países, incluyendo áreas de autocaravanas, campings y aparcamientos. Es conocida por la altísima fiabilidad de sus datos, precios actualizados y un volumen masivo de reseñas (más de 1 millón de usuarios activos). En su versión PRO+, ofrece navegación avanzada y filtros detallados.

## 🛠️ Arquitectura y Funcionamiento
El scraper `campercontact.py` se basa en realizar ingeniería inversa a la API interna utilizada por el mapa interactivo de su plataforma web. A diferencia de iOverlander, este scraper funciona "en vivo" escaneando el mapa de Europa mediante una **cuadrícula dinámica (Grid Subdivision)**.

1. **Endpoint Principal**: Ataca a `https://services.campercontact.com/search/results/list`.
2. **Sistema de Cuadrícula Recursiva (Grid)**:
   - El scraper de base (heredado de `AbstractSource`) le pasa coordenadas de un cuadro (Bounding Box).
   - Si la API responde indicando que hay **más de 50 resultados** (`total > 50`) en ese cuadro, el scraper automáticamente **subdivide la celda en 4 cuadrantes más pequeños** y vuelve a lanzar las peticiones recursivamente, hasta lograr cuadros que devuelvan 50 resultados o menos (o hasta llegar a una resolución mínima de 0.1 grados).
3. **Manejo de Cabeceras (Fingerprinting)**:
   - Utiliza una cabecera oculta específica (`x-feature-flags: microcamping`) que exige su backend moderno para devolver la lista completa.

## 🧠 Lógica de Mapeo y Normalización
- **Tipología (`poiType`)**: Traduce `camperplace`, `motorhome` y `service` a `area_ac`; `nature` o `wild` a `naturaleza`.
- **Detección de Gratuidad**: Evalúa el nodo `priceRange`. Si `min == 0`, se marca como gratuito (`gratuito = True`), permitiendo que nuestro mapa lo etiquete como área libre de pago.
- **Enriquecimiento Básico (Fase 1)**: Extrae de forma limpia el `rating_promedio` y `num_reviews` del nodo de filtros de la API, además de generar la URL canónica (`permalink`) para que el usuario pueda visitar la ficha original.
- **Enriquecimiento Profundo y Reviews (Fase 2)**: Para cada spot ingestado en el grid, se descarga asíncronamente su página web y se parsea el payload de datos JSON incrustado por Next.js (`self.__next_f.push`), extrayendo el listado detallado de servicios (amenities), fotos en alta resolución, descripciones multilingües, datos de contacto (teléfono, email, web) y opiniones de los usuarios, que se guardan en la tabla `reviews`.

## 🛠️ Mejoras y Solución de Carencias (Mayo 2026)

1. **Eliminación del Sesgo Estacional (Bypass de Disponibilidad)**:
   - Se removieron los parámetros de fecha (`fromDate`, `toDate`), plazas (`persons`, `babies`) y mascotas (`pets`) en las consultas al grid. Esto obliga a la API a devolver todos los spots registrados, independientemente de si están abiertos, cerrados por temporada o completos.
2. **Implementación de Fase 2 (Detalles y Opiniones asíncronos)**:
   - Se desarrolló un pipeline asíncrono concurrente con un pool de trabajadores (`enrich_worker`) que consume spots sin detalles marcados (`details_fetched IS NULL`), descarga la ficha web, y parsea los payloads Next.js.
   - Extrae amenities (agua potable, vaciado de negras/grises, electricidad, ducha, wifi, wc público y admisión de perros), seguridad, iluminación, capacidad de plazas y descripciones completas en español, inglés, francés, alemán, italiano y holandés.
   - Popula de forma masiva y eficiente la tabla `reviews` con valoraciones individuales, textos originales e idiomas del autor, previniendo duplicados (`ON CONFLICT DO NOTHING`).
3. **Optimización de Inserción y Prevención de Sobrescritura**:
   - `enriquecer_spot` en la capa de datos actualiza únicamente campos que sean `NULL`, asegurando que CamperContact complemente sin sobrescribir fuentes primarias de mayor credibilidad como Park4Night.

## 🔧 Auditoría y Fixes Mayo 2026

Auditoría completa del scraper tras detectar **2.253 errores** y **62% de URLs corruptas** en la última ejecución (24-05-2026, 4.9h, 30.995 actualizados, 0 nuevos).

### Bugs detectados

1. **URL externa sobreescribía la de campercontact (CRÍTICO)**
   - En Phase 2, `_normalize_detail` extrae `web = contact.get("website")` que es la URL del establecimiento (e.g. `sorkwity.pttk.pl`), NO la de campercontact.
   - Esto se guardaba en `source_records.normalized_data.web`.
   - La query de re-ejecución leía esa columna, intentando scrapear el sitio externo.
   - **Resultado**: 27.983 de 45.617 spots (62%) intentaban scraping fuera de campercontact y fallaban con `parse_html returned None`, 404, 403, timeouts, certificate errors.
   - **Fix**: la query de `download_reviews` ahora reconstruye la URL desde `raw_data->>'permalink'` (que siempre tiene `/france/brittany/.../100011/la-ferme-de-tuchennou`).

2. **`country_iso` con valores truncados (GRAVE)**
   - El `subtitle` viene como `"Madrid, Spain"`, con país en inglés y formato libre.
   - Se guardaba directamente en `country_iso` (columna varchar(2)), produciendo truncamiento: "Spain"→"S", "France"→"F", "Norway"→"N".
   - **Resultado**: 117.431 spots con `country_iso` de longitud 1 en toda la DB (9.511 "f", 8.028 "de" correctos, 2.802 "nl", **2.445 "n" corruptos**, etc.).
   - **Fix**: nuevo dict `COUNTRY_NAME_TO_ISO` con 48 países → ISO2 lowercase. Si no mapea, deja `NULL` y el trigger geográfico de PostGIS clasifica por lat/lon.

3. **Validación de coordenadas no aplicada**
   - El `run()` propio del scraper no llamaba a `coords_validas()` (convención nueva).
   - **Fix**: añadido en el loop principal después de `normalize()`.

### Limpieza de datos aplicada (2026-05-25)

```sql
-- 117.431 country_iso corruptos resetados a NULL (todas las fuentes, no solo cc)
UPDATE spots SET country_iso = NULL WHERE LENGTH(country_iso) <= 1;

-- 27.983 spots de campercontact marcados para re-fetch en próxima Phase 2
UPDATE source_records SET normalized_data = normalized_data - 'details_fetched'
WHERE source='campercontact'
  AND normalized_data->>'web' NOT LIKE '%campercontact%';
```

### Validación post-fix

Tests sobre 30 spots aleatorios con la URL reconstruida desde `permalink`: **30/30 OK**, 0 errores.

---
**Estado Actual:** Auditado y reparado. Próxima ejecución de Phase 2 (`scheduler.py --reviews campercontact`) re-procesará los 27.983 spots con URL corrupta y rellenará `country_iso` correcto donde falte.
