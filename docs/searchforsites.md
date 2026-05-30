# 🇬🇧 SearchForSites Scraper

## 📖 Información General
**SearchForSites** (SFS) es una de las bases de datos de campings y áreas de autocaravanas más consolidadas y extensas, especialmente fuerte en el Reino Unido (UK) y la costa atlántica europea. Funciona como una plataforma integral de planificación de viajes, muy popular entre autocaravanistas británicos y del norte de Europa. Destaca por su sistema de categorización exhaustivo (Certificated Locations, Certificated Sites, Pub Stops, etc.) que la hace indispensable para descubrir sitios fuera del radar de las aplicaciones continentales francesas o alemanas.

## 🛠️ Arquitectura y Funcionamiento
El scraper `searchforsites.py` interactúa con un endpoint de búsqueda avanzada basado en un backend PHP clásico (`getDataAdvanced.php`). Debido a que el servidor de SFS trunca las respuestas masivas para proteger su base de datos, el scraper utiliza una táctica de **Divide y Vencerás (Iteración Matricial)**.

1. **Endpoint Principal (POST)**:
   - Ataca a `https://www.searchforsites.co.uk/pdo/getDataAdvanced.php` mediante el envío de formularios codificados en `x-www-form-urlencoded`.
2. **La Matriz de Búsqueda (Iteración País/Tipo)**:
   - Para evitar que la base de datos de SFS corte los resultados (ej. si pedimos "Todos los de Europa", nos daría solo los primeros 1000), el scraper fragmenta la petición.
   - Carga la lista de países dinámicamente desde la base de datos `countries` para asegurar cobertura mundial, con un fallback a una lista estática de 38 países europeos si la base de datos está vacía.
   - Cuenta con una lista de **15 tipos de localizaciones** (`1` al `15`).
   - El scraper realiza un doble bucle (`for country... for loc_type...`), cruzando País X con Tipo Y, garantizando que el volumen devuelto por cada cruce sea lo suficientemente pequeño como para que el servidor entregue el 100% de los datos.

## 🧠 Lógica de Mapeo y Normalización
- **Traducción de Nomenclatura Británica**: SFS usa acrónimos muy específicos. El scraper los mapea al esquema GeoSpots:
  - `AC`, `ACF` (Campsites) → `camping`.
  - `ASN`, `CPA`, `CS`, `CL` (Pubs, Certificated Sites) → `area_ac`.
  - `PN` (Parking) → `parking`.
- **Detección de Coste Precisa**: SFS devuelve un objeto `cost` con `min` y `max`. Si ambos son `0`, el scraper marca el sitio categóricamente como `gratuito = True`.
- **Fotografías**: Reconstruye la URL de la miniatura a partir del nombre del archivo almacenado en su directorio de uploads (`https://www.searchforsites.co.uk/uploads/thumbs/...`).

## 💬 Pipeline de Reseñas (Mayo 2026)
Se ha implementado el descargador desacoplado `download_reviews` en `searchforsites.py`:
1. **Endpoint**: `POST https://www.searchforsites.co.uk/pdo/getReviews.php` con payload `x-www-form-urlencoded` conteniendo el parámetro `markerID`.
2. **Normalización de Escala**: Las valoraciones originales vienen en una escala de 10 puntos (`score` de la API). El scraper las divide por `2.0` para normalizarlas al estándar de 5 estrellas de GeoSpots.
3. **Mapeo de Campos**:
   - ID Único: `review.id`.
   - Texto: Extraído de `review.text`.
   - Autor: Extraído de `user.name`.
   - Fecha: Parseada del string `review.updated` usando el formato `%Y-%m-%d %H:%M:%S` con zona horaria UTC.
   - Idioma: Identificado dinámicamente con `detect_language`.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Truncamiento Silencioso (Silent Truncation)**:
   - Aunque la técnica de dividir por País y Tipo reduce drásticamente el volumen por petición, si en el futuro el "Tipo 1" (Áreas AC) en "GB" (Reino Unido) crece exponencialmente superando el límite oculto del servidor PHP (ej. 2000 filas), los resultados extra excedentes se perderán sin emitir ningún error.
2. **Ceguera de Tipos Hardcodeados**:
   - El scraper asume que solo existen IDs del 1 al 15. Si los administradores de SFS deciden añadir la categoría 16 (ej. "Granjas"), el scraper ignorará completamente estos puntos hasta que actualicemos el código fuente.
3. **Mapeo de Servicios Pendiente (Raw Facilities)**:
   - La API devuelve los servicios como un string de números separados por comas (ej. `"facilities": "1,2,3,5,7,12,18"`). Al desconocer exactamente qué número es electricidad o agua, el scraper actual guarda el campo crudo (`raw_facilities`). Esto requiere una auditoría futura de la web para decodificar qué significa cada número y mapearlo a los campos nativos de GeoSpots.

---
**Estado Actual:** Integrado, operativo y con pipeline de reviews activo.

## 🔄 Cambios Recientes (Mayo 2026)
- **Carga Dinámica de Países**: Eliminada la lista estática fija. Ahora se realiza una consulta `SELECT DISTINCT UPPER(iso_a2)` sobre la tabla `countries` para barrer todo el planeta, manteniendo la lista estática como fallback.
- **Pipeline de Reviews**: Añadido soporte para descarga concurrente de valoraciones usando `fetch_and_save_reviews()`.

## 🐛 Fix de paginación de reviews (2026-05-30)

**Síntoma:** el panel mostraba reviews "al 306%" (44K reviews reales vs ~15K
esperadas según `SUM(review_count)`).

**Causa raíz:** `getReviews.php` devuelve solo **10 reviews** por defecto y
`fetch_and_save_reviews` **no paginaba**. Los spots con `rvwCnt` alto (hasta
303) perdían el ~97% de sus reviews. Las 44K en DB se acumularon a lo largo de
múltiples runs (cada vez los 10 más recientes) + agregación por dedup
multi-marker, de ahí el desajuste contra el total esperado. **`rvwCnt` SÍ es el
total real** — no subcontaba; era el fetch el que truncaba.

**Fix:** `getReviews.php` acepta `limit` y `offset` (semántica `LIMIT/OFFSET`
SQL; verificado que `offset=0` y `offset=100` no solapan). `fetch_and_save_reviews`
ahora pagina en lotes de 200 (`REVIEWS_PAGE_SIZE`) hasta agotar. Verificado en
vivo: un spot pasó de 11 a 140 reviews; un marker de `rvwCnt=303` devuelve 302
(1-2 borradas/ocultas).

**Completitud con tolerancia:** el criterio de re-fetch en `download_reviews`
es `db_cnt < review_count * 0.9`. Sin la tolerancia del 10%, los spots donde
`getReviews` devuelve 1-2 menos que `rvwCnt` (o multi-marker que no suma exacto)
se re-fetchearían en cada run para siempre. El truncamiento real (10 de 303)
cae muy por debajo del 90% y sí se reintenta correctamente.

**Pendiente (no crítico):** ~3.6K reviews quedaron en 443 spots que ya no tienen
un `source_record` de searchforsites (deriva referencial: markers que en runs
posteriores dedujeron a un spot vecino distinto, dejando reviews "huérfanas" en
el spot_id antiguo). No es pérdida de datos —son reviews reales en spots
reales— y afecta potencialmente a todas las fuentes; queda para una revisión
transversal de estabilidad de dedup.

