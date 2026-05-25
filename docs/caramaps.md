# 🗺️ CaraMaps Scraper

## 📖 Información General
**CaraMaps**, fundada en 2015 en Burdeos (Francia), es una plataforma líder europea para el turismo itinerante. Con una base de datos de más de 60.000 puntos y disponible en múltiples idiomas, destaca por su sólida comunidad (que permite añadir, corregir y valorar ubicaciones) y su mapa interactivo de alta calidad. Combina lugares gratuitos (como *bivouac* o *nature*) con lugares de pago (campings, parkings) y anfitriones profesionales.

## 🛠️ Arquitectura y Funcionamiento
El scraper `caramaps.py` tiene una particularidad técnica notable: no ataca una API REST tradicional, sino que **se conecta directamente a un endpoint proxy de ElasticSearch** de su panel de administración (`admin.caramaps.com/api/revisions/elastic`). Esto permite búsquedas extremadamente rápidas y filtrados complejos.

1. **Búsqueda Global (Sin Grid)**:
   - A diferencia de los scrapers que trocean el mapa en celdas, CaraMaps es capaz de gestionar un "Bounding Box" gigantesco a nivel mundial.
2. **Paginación Masiva (Deep Pagination)**:
   - Pide los resultados en lotes de **800 elementos por página** (`itemsPerPage=800`). 
   - Realiza el barrido en dos fases separadas: primero extrae los puntos de la comunidad (`isPro=False`) y luego los anfitriones/negocios profesionales (`isPro=True`).
3. **Filtros por UUID (Taxonomía Interna)**:
   - Para que la API devuelva los tipos de lugares correctos (áreas, campings, parkings), el scraper inyecta una matriz de **UUIDs hardcodeados** (ej. `98eb91bf-3f57-490a-b4a3-632f31866bda`) en la query string (`filters[type.uuid]`).

## 🧠 Lógica de Mapeo y Normalización
- **Extracción de Atributos Dinámica**: En lugar de campos booleanos simples, ElasticSearch devuelve un array de objetos "atributo" que contienen un `code` y un `label`. El scraper tiene un diccionario multilingüe para buscar palabras como `eau`, `strom`, `wifi` o `vaciado` e inferir qué servicios tiene el spot.
- **Detección de Coste**: Examina el nodo interno `parkingType.code` para determinar con precisión si el lugar es gratis (`free_parking`) o de pago (`paying_parking`, `paid`).
- **Mapeo Geopolítico**: Si la API no devuelve el código ISO del país, el scraper cuenta con un diccionario (`COUNTRY_ISO`) para inferir el código de dos letras a partir del nombre del país (ej. "France" -> "FR").

## 💬 Pipeline de Reseñas (Mayo 2026)
Se ha implementado el descargador desacoplado `download_reviews` en `caramaps.py`:
1. **Endpoint**: `GET https://admin.caramaps.com/api/point_of_interest_comments`.
2. **Parámetros**:
   - `pointOfInterest.uuid`: UUID del spot de CaraMaps.
   - `deletedAt`: `"false"`
   - `itemsPerPage`: `"50"`
   - `page`: Paginación numérica (inicia en 1).
   - `order[createdAt]`: `"desc"`
3. **Mapeo de Campos**:
   - ID Único: `uuid` (UUID nativo de la valoración).
   - Texto: Extraído de `value` o `defaultValue`.
   - Calificación: Extraída de `notation` (escala 1-5).
   - Autor: Reconstruido como `givenName` + `familyName` (con fallback a "Usuario CaraMaps").
   - Fecha: Parseada a partir del string ISO `createdAt`.
   - Idioma: Extraído de `authorLocale.alpha2` (con fallback a "es").

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Vulnerabilidad de los UUIDs Hardcodeados (Taxonomy Drift)**:
   - Es el riesgo más crítico. Los filtros de la petición dependen de IDs absolutos de la base de datos de CaraMaps. Si la empresa decide limpiar su base de datos, re-indexar ElasticSearch o cambiar la taxonomía de los "Tipos de Lugar", los UUIDs cambiarán y el scraper pasará a devolver **0 resultados**.
2. **Límites de ElasticSearch (Max Result Window) - INCIDENTE CONFIRMADO**:
   - ElasticSearch tiene un límite físico de profundidad de paginación de 10.000 resultados. Como CaraMaps no tiene implementado un sistema de cursores (`search_after`), **se ha confirmado en ejecución real que el servidor devuelve un Error 500 al llegar a la página 13** (12 páginas * 800 ítems = 9.600 ítems; la pag. 13 pide hasta el 10.400 y colapsa). Aunque se extraen casi 10.000 lugares por ejecución con éxito, si su base crece, habrá que subdividir el "Bounding Box" de Europa en 4 cuadrantes para que ninguna partición supere este límite de ElasticSearch. 
3. **Fotos como "Thumbnails"**:
   - Como ocurre con otros scrapers de mapas interactivos masivos, el endpoint de ElasticSearch devuelve las URL de las fotos pero optimizadas para miniaturas, careciendo del peso y calidad de la ficha de detalle.

---
**Estado Actual:** Integrado, operativo y con pipeline de reviews activo.

## 🔄 Cambios Recientes (Mayo 2026)
- **Eliminación de Filtros Geográficos**: Se amplió el Bounding Box de Europa a nivel mundial (`WORLD_TOP`, `WORLD_BOTTOM`, `WORLD_LEFT`, `WORLD_RIGHT` de `90.0, -90.0, -180.0, 180.0`).
- **Validación de Coordenadas**: Se modificó `normalize()` para aceptar cualquier coordenada planetaria válida.
- **Pipeline de Reviews**: Añadido soporte para descarga concurrente de valoraciones usando `download_reviews()`.

