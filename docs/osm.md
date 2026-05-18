# 🌍 OpenStreetMap (OSM) Scraper

## 📖 Información General
**OpenStreetMap (OSM)** es el proyecto colaborativo más grande del mundo para crear mapas libres y editables. Conocido como la "Wikipedia de los Mapas", contiene una cantidad colosal de datos cartográficos etiquetados por voluntarios en todo el planeta. Para el ecosistema GeoSpots, OSM funciona como el "mapa base definitivo": una fuente inagotable de campings, áreas de vaciado (sanitary dump stations) y aparcamientos que a veces no figuran en ninguna app comercial de caravaning.

## 🛠️ Arquitectura y Funcionamiento
El scraper `osm.py` no descarga la inmensa base de datos del planeta de golpe (que pesa terabytes), sino que utiliza la **Overpass API**, un motor de búsqueda especializado para consultar datos vivos de OSM mediante su propio lenguaje de programación (Overpass QL).

1. **Overpass Query Language**:
   - El scraper envía peticiones `POST` al intérprete público `overpass-api.de`.
   - Utiliza una query construida dinámicamente que busca nodos (`node`) y polígonos (`way`) que coincidan con etiquetas clave en un radio de 60km de un punto dado: `tourism=caravan_site`, `amenity=sanitary_dump_station`, `leisure=camping_site`, o parkings que admitan autocaravanas (`amenity=parking` + `motorhome=yes`).
2. **Escaneo de Malla (Grid con Shuffling)**:
   - Al igual que el scraper de Park4Night, no envía Bounding Boxes gigantes para no colapsar el servidor. Genera una cuadrícula de coordenadas para toda Europa y las **baraja al azar**.
3. **Mecanismos de Defensa (Circuit Breaker)**:
   - La API pública de Overpass es estricta con el abuso. El scraper implementa un sistema de reintentos (`tenacity`) que hibernera 60 segundos si recibe un error HTTP 429 o 504.
   - Además, incluye un **Circuit Breaker** (Cortocircuito): si se encadenan 5 errores graves consecutivos, el scraper aborta su ejecución por completo para proteger la IP del servidor NAS de un baneo permanente.

## 🧠 Lógica de Mapeo y Normalización
Las etiquetas (Tags) de OSM son texto libre, por lo que el mapeo requiere interpretar su taxonomía oficial:
- **Traducción de Geometrías**: OSM devuelve puntos exactos (`node`), pero los campings suelen estar dibujados como polígonos (`way` o `relation`). Para evitar guardar un trazado poligonal entero en nuestra BD, la query de Overpass exige `out center`, lo que obliga al servidor a calcular el centro exacto (centroide) del polígono y devolver una latitud/longitud única.
- **Tipología**: `tourism=caravan_site` se mapea a `area_ac`; `leisure=camping_site` a `camping`.
- **Servicios Detallados**: Se escanean etiquetas estandarizadas de OSM como `fee` (precio), `maxheight` (altura límite), `capacity` (plazas), `dog` y `internet_access`.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Saturación del Servidor Overpass Público**:
   - Usar el servidor público alemán (`overpass-api.de`) para escanear Europa entera es abusivo. Aunque el "Circuit Breaker" nos protege, es altamente probable que el scraper sufra cortes o bloqueos. La solución profesional a largo plazo sería desplegar nuestro propio contenedor de Overpass o usar una réplica de pago.
2. **Caos Taxonómico (Tagging Inconsistente)**:
   - En OSM, cualquier voluntario etiqueta como quiere. Algunos ponen `fee=no`, otros `fee=0`, otros omiten la etiqueta. La limpieza y normalización de estos datos nunca será perfecta al 100%.
3. **Ausencia de Fotografías**:
   - OpenStreetMap es una base de datos vectorial; no aloja imágenes de los lugares. En el frontend de GeoSpots, todos los puntos extraídos nativamente de OSM aparecerán sin foto (a menos que se fusionen con otro spot de P4N o CaraMaps durante la deduplicación espacial en PostGIS).

---
**Estado Actual:** Integrado y operativo. Actúa como la red de seguridad definitiva de GeoSpots para encontrar puntos ocultos, protegido por un agresivo sistema de tolerancia a fallos.
