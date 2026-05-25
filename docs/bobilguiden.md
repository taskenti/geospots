# 🏔️ Bobilguiden Scraper

## 📖 Información General
**Bobilguiden** ("La Guía del Autocaravanista" en noruego) es la referencia comunitaria de Escandinavia para áreas de servicio, campings oficiales y zonas de acampada libre. La cubertura es masiva en Noruega (~1900 spots) con presencia menor en Suecia, Finlandia y Dinamarca. Es la fuente que mejor cubre los **bobilplass** (parking nórdico estructurado) y los **free camping spots** de la "allemannsretten" (derecho de acampada libre noruego).

## 🛠️ Arquitectura y Funcionamiento

### Bulk endpoint único

```
GET https://api.bobilguiden.no/places/mobile
```

Una sola llamada devuelve un payload de ~1.4 MB con la base de datos completa:
- `places[]`: ~1936 spots con coordenadas, tipo, precios, facilities, comentarios y contacto
- `countries[]`: lookup id → nombre (en NO/EN/DE) — usado para mapear `countryId` del spot a ISO2
- `facilities[]`: lookup id → nombre — los IDs en `facilityIds` del spot se cruzan con esto
- `areas[]`: ids de regiones (no usado)

No necesita grid ni paginación. Tiempo de descarga: ~3 segundos. Tiempo total con normalize + DB: ~30-60 segundos.

## 🗂️ Mapeo y Normalización

### Tipos
4 tipos observados en producción (validados en 1936 spots):

| Type API | Count | → GeoSpots |
|---|---|---|
| `CAMPING_SITE` | 687 | `camping` |
| `FREE_CAMPING` | 645 | `wild` |
| `MOTORHOME_PARKING` | 597 | `area_ac` |
| `DISPOSAL_STATION` | 7 | `area_ac` (solo servicios, sin pernocta) |

### Facilities (cruce de IDs)
`FACILITY_IDS` dict en la clase mapea los IDs estables del top-level `facilities[]`:

| facilityId | Significado | → GeoSpots |
|---|---|---|
| 2 | Shower | `ducha` |
| 3 | Toilet | `wc_publico` |
| 5 | Electric power | `electricidad` |
| 6 | Water | `agua_potable` |
| 10 | Waste water | `vaciado_grises` |
| 11 | Chemical toilet drain | `vaciado_negras` |
| 15 | WiFi | `wifi` |

### País
La API trae `location.address.countryId` numérico, no ISO. La clase tiene `COUNTRY_ID_MAP` con los IDs observados (1=NO, 2=SE, 3=FI, 4=DK). Si llega un ID desconocido, fallback a `no` (la fuente es ~95% noruega).

### Precios
La API devuelve **siempre en NOK**. Mapeo dual:
- `precio_aprox`: en EUR (NOK × 0.085 fijo). Para que comparta escala con el resto de fuentes
- `precio_info`: texto exacto en NOK (más legible para el usuario nórdico), e.g. `"250 NOK"`
- Si hay `pricingDetails` (texto libre como "Free in winter, 250 NOK in summer"), se concatena al precio_info

### Rating
Escala API: 0-5. GeoSpots usa 0-10. Se multiplica × 2.
Adicional: `numberOfRatings` → `num_reviews`.

### Reviews
La respuesta `/places/mobile` incluye `comments` inline para ~14 de 1936 spots. Se insertan vía `upsert_review` (contador refleja solo INSERTs reales).

## 🔧 Auditoría Mayo 2026

### Estado pre-auditoría
Scraper creado por Gemini, **solo 5 source_records** en DB (la API devuelve 1936, así que el run anterior quedó incompleto). 6 bugs detectados:

| # | Bug | Impacto |
|---|---|---|
| 1 | `DISPOSAL_STATION` no mapeado → caía en `naturaleza` | 7 spots de solo-servicios mal clasificados como naturaleza |
| 2 | `gratuito = False` cuando `minPrice` es null | 421 spots informativos como "de pago" siendo desconocido |
| 3 | `precio_aprox` guardado como NOK sin conversión | Filtros por precio rotos (un spot de 250 NOK aparecía como caro siendo ~21 €) |
| 4 | `country_iso = "no"` hardcoded sin leer del API | Los pocos spots de SE/FI/DK mal etiquetados |
| 5 | Rating sin escalar (0-5 → 0-10) | Ratings inconsistentes con resto de fuentes |
| 6 | `numberOfRatings` ignorado | 656 spots sin contador de reviews |
| 7 | `reviews_nuevas` contador inflado (SQL crudo con `ON CONFLICT DO NOTHING` pero `+= 1` siempre) | Métricas falsas en re-runs |

### Validación post-fix sobre 1936 spots vivos
```
DISPOSAL_STATION_area_ac: 7/7 (todos los DISPOSAL ahora son area_ac)
gratuito_None: 421/1936 (los que la API no expone precio)
gratuito_False: 1179 (precio > 0)
gratuito_True:  336 (precio == 0)
rating_in_0_10: 656/1936 (todos los que tienen rating, escala correcta)
num_reviews_set: 656/1936
precio_eur:     1515/1936 (todos los con precio, convertidos NOK→EUR)
DB roundtrip: 30/30 OK
```

### Cleanup DB aplicado
- 5 source_records previos eliminados (contenían los bugs)
- 4 spots solo-bobilguiden eliminados
- 1 spot multi-fuente: fuente removida del array

### Siguiente paso recomendado
Lanzar scrape completo para poblar los 1936 spots reales con los fixes:
```bash
docker-compose exec scraper python scheduler.py --bobilguiden
```

---
**Estado Actual:** Auditado, hardened y limpio. Sin records en DB tras la limpieza — el próximo run insertará los 1936 spots correctos.
