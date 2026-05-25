# 🇩🇪 Promobil Scraper

## 📖 Información General
**Promobil** es una plataforma alemana (perteneciente al grupo editorial Motor Presse Stuttgart) de referencia absoluta para el caravaning en Centroeuropa. Su base de datos de áreas de pernocta (*Stellplatz*) y campings es muy valorada por su calidad de datos, descripciones detalladas en alemán y la gran cantidad de opiniones de usuarios expertos de la comunidad germana.

## 🛠️ Arquitectura y Funcionamiento
El scraper `promobil.py` descarga los spots y sus valoraciones mediante dos estrategias distintas:

1. **Descarga de Spots (Fase 1 - API)**:
   - Ataca al endpoint de la API de su frontend web: `https://page-api.promobil.de/pro-pitch/pitch/getListData`.
   - Requiere un payload JSON que especifique los límites geográficos o el país.
   - Es necesario enviar cabeceras realistas (como `referer: https://www.promobil.de/`) y un User-Agent de navegador para evitar bloqueos.

2. **Descarga de Reseñas (Fase 2 - Scraping de HTML/JSON Embebido)**:
   - Las opiniones no están expuestas en una API JSON pública directa. El scraper realiza peticiones HTTP `GET` a la página pública de valoraciones del spot.

## 💬 Pipeline de Reseñas (Mayo 2026)
Se ha implementado el descargador desacoplado `download_reviews` en `promobil.py`:
1. **Resolución de URL de Opiniones**:
   - URL: `https://www.promobil.de/{page_url}/bewertungen/`.
   - Si `page_url` no está guardado en los datos normalizados, se autogenera aplicando un algoritmo de *slugify* al nombre del spot con el formato: `stellplatz/{slug}-{source_id}.html`.
2. **Extracción de Datos Embebidos**:
   - En lugar de parsear el árbol HTML con BeautifulSoup, el scraper localiza la etiqueta script `<script id="__NEXT_DATA__" type="application/json">` utilizando expresiones regulares.
   - Carga el contenido de este bloque como un objeto JSON nativo.
3. **Navegación del Objeto JSON**:
   - Se accede a la ruta: `props.pageProps.pageData.data.mobile`.
   - Se busca el elemento cuya clave `element` sea igual a `"pitch.ratings"`. El nodo `data` de este elemento contiene el array completo de valoraciones.
4. **Mapeo de Campos**:
   - ID Único: `f"promobil_{_id}"` para evitar colisiones.
   - Texto: Extraído de `displayText`.
   - Calificación: Extraída de `rating.avg` o `rated` (escala 1-5).
   - Autor: Extraído de `_createdBy.username`.
   - Fecha: Parseada a partir del campo `date` (`YYYY-MM-DD`).
   - Idioma: Extraído directamente de `language`.
5. **Tratamiento del Error 404 (Slug Mismatch)**:
   - Si la web de Promobil ha cambiado el slug de un spot, la URL autogenerada devolverá un HTTP 404. El scraper detecta este estado y marca `reviews_fetched = true` en la base de datos para evitar reintentar indefinidamente sobre la URL errónea.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Fragilidad de la Estructura NextJS (`__NEXT_DATA__`)**:
   - Si Promobil actualiza la estructura interna del JSON de Next.js, cambia la ruta de navegación en `pageProps` o decide migrar su frontend a otra tecnología que no sea Next.js, la extracción de reseñas fallará por completo.
2. **Desajuste de Slugs (404)**:
   - Dado que el slug se autogenera de forma heurística, cualquier carácter especial o traducción no contemplada en la función local de slugify provocará una URL errónea y la pérdida de la descarga de reseñas para ese spot específico.
3. **Rate Limits y WAF**:
   - El servidor de Promobil bloquea peticiones de scraping masivas rápidamente. Es imprescindible mantener un `rate_limit` de al menos 1-2 segundos entre peticiones del worker de reseñas.

---
**Estado Actual:** Integrado, operativo y con pipeline de reviews activo.
