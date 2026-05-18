# 🚐 Roadsurfer Spots Scraper

## 📖 Información General
**Roadsurfer Spots** (lanzada en 2020) es una plataforma creada por el gigante del alquiler de campers alemán, Roadsurfer GmbH. Se diferencia de bases de datos masivas colaborativas (como Park4Night) en que funciona como un **marketplace premium de anfitriones privados**. Ofrece a los viajeros la posibilidad de aparcar legalmente y pernoctar en terrenos privados exclusivos, como viñedos, granjas o jardines, aportando una experiencia más íntima, segura y regulada, apoyando al mismo tiempo la economía local.

## 🛠️ Arquitectura y Funcionamiento
El scraper `roadsurfer.py` utiliza una táctica de extracción única dentro del proyecto GeoSpots: la **Búsqueda Radial Global (Global Radial Search)**.

1. **Endpoint de Búsqueda (POST)**:
   - Ataca a la API de búsqueda interna `https://spots.roadsurfer.com/en_GB/search/spot` enviando payloads JSON a través de peticiones HTTP POST.
2. **Estrategia "Toda la Tierra"**:
   - En lugar de realizar peticiones basadas en un cuadro delimitador (Bounding Box) clásico o una cuadrícula GPS intensiva, el scraper explota la funcionalidad de radio de la plataforma. Configura un punto central en Europa (Latitud 50.0, Longitud 10.0) y establece un `searchRadius` de **20.000 kilómetros**.
   - Esto obliga al servidor de Roadsurfer a devolver *absolutamente todos los puntos del mundo* ordenados por distancia desde el centro de Europa.
3. **Paginación Lineal Directa**:
   - Una vez forzada la devolución global, el scraper simplemente utiliza los parámetros `offset` y `size=500` para devorar la base de datos página por página hasta vaciarla.

## 🧠 Lógica de Mapeo y Normalización
- **Extracción de Imágenes por Expresión Regular**: El JSON devuelto por Roadsurfer no entrega la URL de la imagen en texto limpio, sino un bloque gigante de código HTML (`previewImageHtml`) que contiene etiquetas `<picture>` y formatos adaptativos (AVIF, WebP). El scraper utiliza la librería `re` (Regex) para encontrar e inyectar el dominio base a la primera imagen de mayor calidad.
- **Tipología (`terrainFor`)**: Evalúa el array de vehículos permitidos. Si incluye `camperVan`, `motorhome` o `caravan`, lo etiqueta como `area_ac` (área de pago premium). Si solo acepta `tent` (tiendas de campaña), lo marca como `naturaleza`.
- **Detección Gratuita**: Chequea el atributo `isFreeSpot`. Aunque en Roadsurfer el 99% de los spots son de pago por reserva, existen excepciones promocionales.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Volatilidad del HTML inyectado (Regex Breakage)**:
   - Dado que la foto principal se extrae "rascando" el código HTML incrustado en el JSON, si Roadsurfer cambia el diseño de su frontend o la estructura de sus etiquetas `<picture>`, la extracción de imágenes fallará silenciosamente, dejando los spots sin fotografía.
2. **Límites Ocultos de Offset (Deep Pagination Dropoff)**:
   - Algunos motores de búsqueda backend imponen límites duros al parámetro `offset` (por ejemplo, impidiendo pedir más allá del offset 10.000). Al forzar un radio de 20.000km, si la plataforma crece a decenas de miles de spots, el scraper podría no llegar al final de la lista si el servidor rechaza offsets tan altos.
3. **Carencia de Servicios Detallados**:
   - El endpoint general de búsqueda devuelve coordenadas, foto, precio y tipos de vehículos permitidos, pero omite la lista completa de servicios del anfitrión (agua, electricidad, duchas). Para no saturar el servidor con una petición extra por cada parcela privada, estos campos se dejan vacíos.
4. **Protección Comercial Fuerte**:
   - Al ser una plataforma corporativa respaldada por una empresa potente (no una asociación de usuarios), la probabilidad de que inviertan en medidas Anti-Scraping modernas (WAFs como Cloudflare o Datadome) es alta.

---
**Estado Actual:** Integrado y operativo mediante técnica de Búsqueda Radial Global. Rápido y altamente eficiente.
