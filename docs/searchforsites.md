# 🇬🇧 SearchForSites Scraper

## 📖 Información General
**SearchForSites** (SFS) es una de las bases de datos de campings y áreas de autocaravanas más consolidadas y extensas, especialmente fuerte en el Reino Unido (UK) y la costa atlántica europea. Funciona como una plataforma integral de planificación de viajes, muy popular entre autocaravanistas británicos y del norte de Europa. Destaca por su sistema de categorización exhaustivo (Certificated Locations, Certificated Sites, Pub Stops, etc.) que la hace indispensable para descubrir sitios fuera del radar de las aplicaciones continentales francesas o alemanas.

## 🛠️ Arquitectura y Funcionamiento
El scraper `searchforsites.py` interactúa con un endpoint de búsqueda avanzada basado en un backend PHP clásico (`getDataAdvanced.php`). Debido a que el servidor de SFS trunca las respuestas masivas para proteger su base de datos, el scraper utiliza una táctica de **Divide y Vencerás (Iteración Matricial)**.

1. **Endpoint Principal (POST)**:
   - Ataca a `https://www.searchforsites.co.uk/pdo/getDataAdvanced.php` mediante el envío de formularios codificados en `x-www-form-urlencoded`.
2. **La Matriz de Búsqueda (Iteración País/Tipo)**:
   - Para evitar que la base de datos de SFS corte los resultados (ej. si pedimos "Todos los de Europa", nos daría solo los primeros 1000), el scraper fragmenta la petición.
   - Cuenta con una lista fija de **38 países europeos** (`"GB", "FR", "ES"...`).
   - Cuenta con una lista de **15 tipos de localizaciones** (`1` al `15`).
   - El scraper realiza un doble bucle (`for country... for loc_type...`), cruzando País X con Tipo Y, garantizando que el volumen devuelto por cada cruce sea lo suficientemente pequeño como para que el servidor entregue el 100% de los datos.

## 🧠 Lógica de Mapeo y Normalización
- **Traducción de Nomenclatura Británica**: SFS usa acrónimos muy específicos. El scraper los mapea al esquema GeoSpots:
  - `AC`, `ACF` (Campsites) → `camping`.
  - `ASN`, `CPA`, `CS`, `CL` (Pubs, Certificated Sites) → `area_ac`.
  - `PN` (Parking) → `parking`.
- **Detección de Coste Precisa**: SFS devuelve un objeto `cost` con `min` y `max`. Si ambos son `0`, el scraper marca el sitio categóricamente como `gratuito = True`.
- **Fotografías**: Reconstruye la URL de la miniatura a partir del nombre del archivo almacenado en su directorio de uploads (`https://www.searchforsites.co.uk/uploads/thumbs/...`).

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Truncamiento Silencioso (Silent Truncation)**:
   - Aunque la técnica de dividir por País y Tipo reduce drásticamente el volumen por petición, si en el futuro el "Tipo 1" (Áreas AC) en "GB" (Reino Unido) crece exponencialmente superando el límite oculto del servidor PHP (ej. 2000 filas), los resultados extra excedentes se perderán sin emitir ningún error.
2. **Ceguera de Tipos Hardcodeados**:
   - El scraper asume que solo existen IDs del 1 al 15. Si los administradores de SFS deciden añadir la categoría 16 (ej. "Granjas"), el scraper ignorará completamente estos puntos hasta que actualicemos el código fuente.
3. **Mapeo de Servicios Pendiente (Raw Facilities)**:
   - La API devuelve los servicios como un string de números separados por comas (ej. `"facilities": "1,2,3,5,7,12,18"`). Al desconocer exactamente qué número es electricidad o agua, el scraper actual guarda el campo crudo (`raw_facilities`). Esto requiere una auditoría futura de la web para decodificar qué significa cada número y mapearlo a los campos nativos de GeoSpots.

---
**Estado Actual:** Integrado y operativo. Realiza cientos de iteraciones ligeras con un alto índice de éxito y fiabilidad.
