# 🇪🇸 ÁreasAC Scraper

## 📖 Información General
**ÁreasAC** es, históricamente, el portal de referencia para el turismo en autocaravana en España. A diferencia de foros o apps colaborativas masivas, ÁreasAC actúa como un directorio cuasi-oficial, caracterizado por una alta fiabilidad en las ubicaciones y servicios de las Áreas de Autocaravanas (tanto públicas como privadas) en territorio nacional. Su formato de distribución de datos a menudo se apoya en listados en formato PDF o bases de datos estáticas, muy apreciadas por los viajeros para su uso offline.

## 🛠️ Arquitectura y Funcionamiento
El scraper `areasac.py` no realiza peticiones a internet. Es un proceso de **Ingesta Offline Estática** basado puramente en la Extracción de Texto de Archivos PDF (OCR Text Extraction).

1. **El Origen (PDF Mount)**:
   - El contenedor Docker de GeoSpots debe tener montado o alojado el archivo oficial en la ruta `/app/areasac.pdf`.
2. **Extracción con `pdfplumber`**:
   - El scraper utiliza la librería especializada `pdfplumber` para leer visualmente cada página del PDF y extraer todo su contenido como texto plano.
3. **Parseo por Expresiones Regulares (Regex)**:
   - Una vez tiene el texto masivo, lo divide línea por línea y aplica una estricta expresión regular (`_LINE_RE`) para trocear la estructura estándar de ÁreasAC: 
   - `Provincia - Municipio - Nombre - (Tipo) Símbolos - Longitud Latitud`

## 🧠 Lógica de Mapeo y Normalización
- **Traducción de Acrónimos**: El PDF utiliza acrónimos de 2 letras. El scraper los traduce: `PU` (Pública), `PR` (Privada), `RU` (Ruta), `AR` (Área en Ruta) pasan a ser `area_ac`. `CP` es `camping` y `PK` es `parking`.
- **Decodificación de Simbología Estenográfica**: Los servicios vienen como una cadena de texto ofuscada (ej. `#/AL/AG/AN/CE/`). El script lee estas claves:
  - `#` = Gratuito, `€` = De pago
  - `/AL/` = Agua limpia
  - `/AG/` = Aguas Grises
  - `/AN/` = Aguas Negras
  - `/CE/` = Electricidad
- **Sello de Calidad**: A todos los puntos extraídos se les inyecta la bandera `verificado = True` de forma predeterminada, dada la fiabilidad del equipo editorial de ÁreasAC.
- **Filtro Geográfico Planetario**: Se eliminó el filtro exclusivo de España y ahora acepta cualquier coordenada planetaria válida en los rangos (-90 a 90 de latitud y -180 a 180 de longitud).

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Fragilidad Extrema del Regex (Brittle Parsing)**:
   - Este es el scraper más frágil del sistema a nivel estructural. Si el maquetador del PDF de ÁreasAC decide cambiar un simple guion por una barra, añadir un salto de línea en un nombre muy largo, o cambiar la coma de los decimales por un punto de forma inconsistente, el Regex fallará y el punto será ignorado silenciosamente.
2. **Obsolescencia Estática (Data Stagnation)**:
   - Al no estar conectado a un endpoint vivo, los datos se irán desactualizando con el tiempo. Es responsabilidad del administrador descargar el nuevo PDF cada año y reiniciar el contenedor para que el scraper ingiera las novedades.
3. **Pérdida de Multimedia**:
   - Un archivo PDF de listado carece de fotografías. Los puntos provenientes de esta fuente siempre aparecerán sin imagen de portada en GeoSpots, requiriendo que otras fuentes (ej. Park4Night) se fusionen con ella por deduplicación para aportarle fotos al punto.

---
**Estado Actual:** Integrado y operativo. Extrae áreas de autocaravanas de alta calidad en España, aunque depende completamente de la exactitud tipográfica del PDF de origen.

## 🔄 Cambios Recientes (Mayo 2026)
- **Eliminación del Filtro Geográfico de España**: El parser ya no restringe las coordenadas a la Península e islas de España. Ahora acepta y normaliza puntos en cualquier parte del mundo.
