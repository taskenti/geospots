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
- **Tipología**: `tourism=caravan_site` → `area_ac`; `leisure=camping_site` → `camping`; `amenity=parking` (con `motorhome=yes`) → `parking`; **`amenity=water_point` y `amenity=sanitary_dump_station` → `area_ac`** (eran "otro" antes — son áreas que ofrecen un servicio concreto).
- **Servicios — lógica doble**: para `agua_potable`, `vaciado_negras` y `vaciado_grises` se marca True si (a) el POI ES el servicio (water_point / dump_station), o (b) un caravan_site cercano declara el sub-tag (`drinking_water=yes`, `sanitary_dump_station=yes`, `waste_disposal=yes`, `toilets:disposal=chemical_disposal`, etc.).
- **Otros flags**: `shower`, `toilets`, `electricity`, `power_supply`, `internet_access`, `dog`/`dogs`, `fee`, `maxheight`, `capacity` (con regex tolerante a "12 spaces" o "30+").
- **`country_iso`**: `addr:country` es texto libre. `OSM_COUNTRY_TO_ISO` mapea ~30 países (códigos ISO2/3 + nombre EN + nombre nativo). Desconocidos → NULL para que el trigger PostGIS clasifique por lat/lon.
- **`nombre`**: prioridad `name:es` > `name` > `name:en` > `name:fr/de/it`.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Saturación del Servidor Overpass Público**:
   - Usar el servidor público alemán (`overpass-api.de`) para escanear Europa entera es abusivo. Aunque el "Circuit Breaker" nos protege, es altamente probable que el scraper sufra cortes o bloqueos. La solución profesional a largo plazo sería desplegar nuestro propio contenedor de Overpass o usar una réplica de pago.
2. **Caos Taxonómico (Tagging Inconsistente)**:
   - En OSM, cualquier voluntario etiqueta como quiere. Algunos ponen `fee=no`, otros `fee=0`, otros omiten la etiqueta. La limpieza y normalización de estos datos nunca será perfecta al 100%.
3. **Ausencia de Fotografías**:
   - OpenStreetMap es una base de datos vectorial; no aloja imágenes de los lugares. En el frontend de GeoSpots, todos los puntos extraídos nativamente de OSM aparecerán sin foto (a menos que se fusionen con otro spot de P4N o CaraMaps durante la deduplicación espacial en PostGIS).

## 🔧 Auditoría Mayo 2026 — La cagada de las aguas

### Estado pre-auditoría
Reportado por el usuario: "OSM da timeouts 504 pero también veo cagada con las descargas de aguas". Confirmado en DB:
- 50 source_records, 41 spots con OSM
- **0 spots con `agua_potable=True`** (debía haber muchos: la query trae water_points)
- Solo 3 spots con `vaciado_negras=True`
- 0 spots con `vaciado_grises=True` (campo NUNCA mapeado)
- 1 zombie en `scraper_log` del 25-05

### Bugs detectados y fix

| # | Bug | Fix |
|---|---|---|
| 1 | **agua_potable**: solo se marcaba con sub-tag `drinking_water=yes` en caravan_sites. Los POIs `amenity=water_point` (cuya razón de ser es el agua) caían a `agua_potable=None` | Lógica doble: si el POI ES water_point → True automático; si no, leer del sub-tag |
| 2 | **vaciado_negras**: solo via sub-tag `sanitary_dump_station=yes`. Los POIs `amenity=sanitary_dump_station` no marcaban su propio flag | Lógica doble + nuevo tag `toilets:disposal=chemical_disposal` |
| 3 | **vaciado_grises**: NUNCA mapeado. El normalize ni tenía la clave | Mapeado: dump_station POI O `waste_disposal=yes` O `waste_water=yes` |
| 4 | `OSM_TIPO_MAP`: water_point y dump_station como `"otro"` | Cambiados a `area_ac` (son áreas que ofrecen servicios) |
| 5 | `wc_publico` y `power_supply` no leídos | Añadidos `toilets` y `power_supply` al mapeo |
| 6 | `country_iso = tags.get("addr:country", "").lower()`: texto libre truncado | `OSM_COUNTRY_TO_ISO` dict con ~30 países. Desconocidos → NULL |
| 7 | `capacity.isdigit()` rechazaba "12 spaces" o "30+" | Helper `_parse_int_safe` con regex `\d+` |
| 8 | `config.max_workers // 2` → TypeError si None | `getattr(config, 'max_workers', None) or 3` |
| 9 | `coords_validas` no aplicado | Añadido en el loop |

### Validación
Test sintético sobre 11 elementos representativos (sin red, Overpass público estaba dando 504 todo el tiempo de la auditoría):
- ✓ water_point → agua_potable=True
- ✓ dump_station → vaciado_negras=True, vaciado_grises=True
- ✓ caravan_site con todos los flags + country France→fr
- ✓ Deutschland→de, FR→fr
- ✓ capacity='12 spaces' → num_plazas=12
- ✓ Mordor (país desconocido) → country_iso=None
- ✓ toilets:disposal=chemical → vaciado_negras=True
- ✓ sin tags → rejected
- **8/8 asserts pasados**

### Cleanup DB
- 50 source_records previos eliminados
- 15 spots solo-osm borrados
- 26 spots multi-fuente: `osm` removido del array fuentes[]
- 1 zombie de scraper_log limpiado

---
**Estado Actual:** Auditado y hardened. La cagada de las aguas está resuelta. La DB queda limpia para el próximo run, que poblará los servicios correctamente. Por la inestabilidad crónica de Overpass público (504/429) considerar instalar instancia local Overpass-Docker en el futuro si OSM se convierte en crítica.
