# GeoSpots — Arquitectura Detallada

## Visión General

GeoSpots es un motor de agregación geoespacial semántica construido en 5 capas activas (con 2 más planificadas). El principio central es: **los datos crudos son sagrados, todo se pre-computa, nunca se imputa en tiempo real**.

```
FUENTES EXTERNAS (20+ fuentes)
        │
        ▼
[1] INGESTA ──────────── scraper/ (asyncio + httpx)
        │
        ▼
[2] CANONICAL MODEL ─── PostgreSQL (spots + source_records + reviews)
        │
        ▼
[3] LLM ENRICHMENT ──── enrichment/ (Gemini Flash → spot_semantic_state)
        │
        ▼
[4] VECTOR SEARCH ────── pgvector (text-embedding-004 → spot_embeddings)
        │
[5,6] VISUAL + GEO ───── Planeado (Gemini Vision + DEM/OSM)
        │
        ▼
[API] FastAPI ─────────── /search, /search/semantic, /spot/:id, /points
```

---

## Capa 1 — Ingesta (scraper/)

### AbstractSource (`scraper/sources/base.py`)

Clase base que todo scraper debe heredar. Define el contrato:

```
AbstractSource
  .name: str                     → clave única ('park4night', 'furgovw', ...)
  .rate_limit: float             → segundos de delay entre requests
  .grid_step: float              → tamaño de celda en grados (1.0 por defecto)
  .dedup_radius_m: float         → radio de búsqueda para dedup (50-100m típico)

  fetch_cell(client, tl, tl, br, br) → list[dict]   # raw items de una celda
  normalize(raw) → dict | None                        # raw → schema GeoSpots
  run(pool, config, log_id) → dict                   # pipeline completo
```

### Estrategias de Grid

Cada fuente puede usar una estrategia diferente:

| Fuente | Estrategia | Grid |
|---|---|---|
| park4night | Quadtree adaptativo (subdivide si =100 results) | Puntos lat/lon, profundidad hasta 0.125° |
| campercontact | BBox con subdivisión recursiva (si >50) | 1°×1° → 0.5°×0.5° |
| ioverlander | Import offline KMZ (no grid) | — |
| furgovw | API global (un solo request) | — |
| areasac | HTML scraping por URL | — |
| osm | Radio circular en Overpass | Puntos con radio 60km |
| Resto (caramaps, nomady...) | BBox estándar | `generate_active_grid` de la base |

### `generate_active_grid` (base.py:48)

En vez de grid fijo de Europa, calcula las celdas "activas" en base a los spots existentes en DB + un buffer de 4 celdas. Esto permite que fuentes nuevas solo descarguen donde ya hay datos conocidos, evitando el grid global de 37,000 celdas.

### Pipeline por item

```
fetch_cell() → raw_item
  → normalize(raw_item) → norm
  → find_spot_cercano(lat, lon, radius, nombre, tipo) → existente?
      YES → enriquecer_spot(spot_id, norm, fuente)   # COALESCE — nunca sobreescribe
      NO  → crear_spot(norm)                          # INSERT nuevo spot canónico
  → upsert_source_record(spot_id, source, source_id, raw, norm)
```

### Deduplicación (`db.py:find_spot_cercano`)

Tres capas de decisión:
1. **Radio < 20m** → fusión directa (error GPS típico), salvo camping vs wild
2. **Radio 20-100m** → comprueba exclusión de tipos mutuamente excluyentes (camping != wild)
3. **Radio 20-100m + nombre similar ≥ 0.35** → fusión

Grupos de exclusión definidos: `camping` no fusiona con `wild/naturaleza/parking`, etc.

### Credibilidad de Fuentes

`source_credibility` define `base_score` (0-1) por fuente. Las fuentes con mayor score "ganan" en reconciliación:
- park4night: 0.92 | campercontact: 0.90 | ioverlander: 0.85 | areasac: 0.85

---

## Capa 2 — Canonical Model (PostgreSQL)

### Tres tablas principales

```
spots (1 fila = 1 lugar físico real)
  └─ source_records (1:N — lo que cada fuente sabe)
  └─ reviews (1:N — todas las reviews de todas las fuentes)
```

### `enriquecer_spot` vs `crear_spot`

- **`crear_spot`**: INSERT con todos los campos disponibles
- **`enriquecer_spot`**: UPDATE con COALESCE para cada campo — solo rellena NULLs, nunca sobreescribe. La excepción es `tipo` que pasa de 'otro' a cualquier tipo más específico, y `fotos_urls` / arrays JSONB vacíos.

### Reconciliación (`reconciliar.py`)

Corre sobre spots con >1 fuente. Para cada campo, aplica la jerarquía de credibilidad definida en `CREDIBILITY` dict: la fuente con mayor posición en la lista para ese campo "gana".

Los campos con conflictos (`gratuito`, `precio_info`, `agua_potable`, `electricidad`, `num_plazas`, `tipo`) se loguean en `spots.conflictos` JSONB para revisión manual.

**Limitación actual**: reconciliar hace un scan completo sin modo incremental.

### Clasificación Geográfica

El trigger `trg_classify_spot` (BEFORE INSERT OR UPDATE OF lat,lon) hace:
1. ST_Contains contra la tabla `countries` (cargada desde `ne_50m_admin_0_countries.json`)
2. Fallback: ST_DWithin 50km para puntos costeros/islas
3. Rellena `country_iso`, `continent`, `subregion` automáticamente

---

## Capa 3 — LLM Enrichment (enrichment/)

### Flujo completo Phase 3

```
reviews (llm_processed = FALSE)
  ↓
review_cleaner.clean_review_full()
  → CleanedReview(texto_limpio, informativo, idioma)
  
Si informativo:
  ↓
claim_extractor.extract_claims()
  → regex PATTERNS primero (26 patrones con needles multiidioma)
  → si sin resultado: Gemini Flash fallback
  → list[dict] claims: [{signal, value, confidence, excerpt}]
  
  ↓
observation_normalizer.normalize_claims()
  → NormalizedObservation(signal_type, value_num/bool/text, weight, observed_at)
  → weight = extraction_confidence × source_confidence × reviewer_confidence
  
  ↓
dsl_generator.generate_review_dsl()
  → texto DSL compacto: "quiet:+0.8 police:-0.2 sea:T"
  
  ↓
DB: UPDATE reviews (texto_limpio, texto_dsl, cleaned, informativo, llm_analysis)
    INSERT extracted_claims
    INSERT normalized_observations
    UPDATE spot_semantic_state (incremental o full recompute)
```

### Señales (signal_types)

21 señales definidas con:
- `decay_class`: permanent | slow | volatile → controla `half_life_days`
- `aggregation_strategy`: weighted_mean | consensus_boolean | recent_wins
- `contradiction_strategy`: recent_wins | majority_consensus | permanent_override

Ejemplos:
- `police_risk`: volatile (60 días half-life), recent_wins — una multa reciente pesa mucho
- `beauty`: permanent (36500 días) — las vistas al mar no cambian
- `crowd_level`: volatile (30 días) — la masificación es estacional

### `update_semantic_state` (incremental vs recompute)

- **Incremental**: al insertar una observación, actualiza solo la señal afectada usando weighted mean online
- **Full recompute** (`recompute_spot_state`): relec todas las observaciones del spot y reagrega. Más lento pero exacto
- **Snapshot**: si la distancia semántica entre estado actual y anterior > 0.15, guarda snapshot histórico en `spot_semantic_snapshots`

### semantic_dsl

String compacto generado por `dsl_generator.generate_spot_dsl()`:
```
quiet:+0.8 police:-0.2 beauty:+0.9 sea:T overnight:T crowd:-0.3
```
Ahorra ~85% de tokens vs mandar datos completos al LLM de respuesta.

---

## Capa 4 — Vector Search (enrichment/embedding_generator.py)

### Texto que se embeddea

No el raw text, sino una representación compuesta:
```
{canonical_name} - {tipo}
en {region}, {COUNTRY}
{summary_es o summary_en}
Tags: {tags}
Ideal para: {best_for}
{señales semánticas convertidas a frases naturales}
Servicios: agua potable, electricidad, ...
DSL: {semantic_dsl}
```

### Modelo: text-embedding-004 (Google, 768 dims)

- Multiidioma nativo (ES/FR/DE/EN/IT/NL/PT)
- ~$0.006/1M tokens → batch completo <$1
- Requiere solo `GEMINI_API_KEY` (ya en .env)
- HNSW index en pgvector (m=16, ef_construction=64)

### Búsqueda en 3 capas

```
1. Gemini extrae intención → sql_filters + semantic_query
2. PostGIS ST_DWithin(radio_km) → candidatos geo
3. SQL filtros materializados sobre spot_semantic_state → reducción 10x
4. pgvector <=> cosine → ranking final Top-N
5. Gemini + semantic_dsl → respuesta en lenguaje natural
```

Fallback heurístico si Gemini falla: `extraer_intencion_heuristica()` mapea palabras clave a filtros.

---

## Capas 5 y 6 — Planeadas

### Fase 5 — Visual Intelligence

- Gemini 2.0 Flash Vision analiza fotos → claims → mismo pipeline Phase 3
- 338K spots con URLs de fotos, 1.1M URLs totales
- 0 fotos procesadas actualmente
- `spot_geo` table existe con 30 columnas, 0 filas

### Fase 6 — Geo Intelligence

- DEM (elevación) + pendiente + aspecto → sombra, acceso grandes vehículos
- OSM buildings/roads → stealth score geo-computado
- Observaciones geo con `extractor='geo_computed_v1'` e `extraction_confidence=0.90-0.95`

---

## Convergencia: cómo scraping y enrichment forman la ficha final

Las dos pipelines son independientes pero comparten el mismo `spot_id` como clave. La "ficha" de un spot no existe como tabla propia — es el resultado del JOIN que hace la API en tiempo real sobre datos ya pre-computados.

### Qué aporta cada pipeline

```
SCRAPING (fuentes externas)                ENRICHMENT (LLM pipeline)
         │                                          │
         ▼                                          ▼
source_records (raw + norm por fuente)     normalized_observations (claims + pesos)
         │                                          │
         └─────────────────┬──────────────────────-─┘
                           ▼
               spots.id  ←  clave compartida
```

| Datos factuales (scraping → `spots`) | Datos semánticos (enrichment → `spot_semantic_state`) |
|---|---|
| `canonical_name`, `tipo`, `lat/lon` | `quietness_score`, `beauty_score` |
| `agua_potable`, `ducha`, `electricidad`, `wifi` | `police_risk_score`, `overnight_safe` |
| `precio_info`, `gratuito` | `safety_score`, `crowd_level_score` |
| `descripcion_es/fr/de/en` | `signals_data` (JSONB completo con n_obs, confidence) |
| `fotos_urls`, `web`, `telefono` | `semantic_dsl` ("quiet:+0.9 clean:+0.9 bigveh:-0.3") |
| `master_rating`, `total_reviews` | `consensus_confidence` (qué tan fiable es el estado) |
| `fuentes[]` (qué fuentes conocen el spot) | `total_observations`, `last_aggregated_at` |

### La ficha real — ejemplo de producción

El endpoint `GET /spot/{id}` hace tres queries y une el resultado:

```sql
SELECT * FROM spots WHERE id = $1;                         -- datos factuales
SELECT * FROM spot_semantic_state WHERE spot_id = $1;      -- estado semántico
SELECT * FROM reviews WHERE spot_id = $1 ORDER BY fecha DESC LIMIT 20;  -- reviews
```

Ejemplo real (`spot_id=148518`, Area Camper Bellavista, Granada):

```
FACTUAL (caramaps + park4night + campercontact):
  canonical_name: "Area Camper Bellavista"
  tipo:           area_ac  |  gratuito: false
  agua_potable: ✅  wc: ✅  ducha: ✅  electricidad: ✅  wifi: ✅
  master_rating:  4.05  |  total_reviews: 517
  fotos_urls:     [25 URLs]
  descripcion_es: "Lugar tranquilo y seguro, parada de tranvía a 500 metros
                   directo al centro de Granada..."
  fuentes:        [caramaps, park4night, campercontact]

SEMÁNTICO (calculado de 517 reviews via regex + LLM):
  quietness_score:   0.862  (37 observaciones, confianza 100%)
  cleanliness_score: 0.851  (66 observaciones, confianza 100%)
  beauty_score:      0.900  ( 6 observaciones, confianza  83%)
  overnight_safe:    true   ( 4 observaciones)
  large_vehicle:     0.300  ← reviews dicen "difícil para vehículos grandes"
  semantic_dsl:      "quiet:+0.9 clean:+0.9 beauty:+0.9 overnight:T bigveh:-0.3"
  consensus:         0.24   ← pocas obs relativas al total de reviews (517)

REVIEWS (últimas 20 de 517):
  [park4night 2025-03] "Lugar tranquilo, duchas bien..."
  [caramaps   2025-02] "Muy recomendable, vistas a Sierra Nevada..."
  ...
```

### Qué genera el enrichment sobre los datos del scraping

El LLM no "reemplaza" nada del scraping — lo **complementa**:

- El scraping aporta **verdades declarativas** (la ficha oficial del sitio: "tiene ducha", "cuesta €18")
- El enrichment aporta **verdades observadas** (lo que la gente realmente experimenta: "la ducha estaba fría", "en agosto está lleno", "vino la policía")

Son capas complementarias. Un spot puede tener `electricidad=true` (declarado por la fuente) y `electricity_working=false` (detectado por reviews recientes que dicen "el enchufe no funcionaba"). Esta tensión es información valiosa, no un conflicto a resolver.

### Por qué no hay tabla "ficha"

Los datos pre-computados están en `spots` + `spot_semantic_state`. La API solo hace el JOIN. Esto tiene ventajas:
- El scraping actualiza `spots` sin tocar `spot_semantic_state`
- El enrichment actualiza `spot_semantic_state` sin tocar `spots`
- Ambos pueden correr en paralelo sin lock contention
- Si el enrichment falla, los datos factuales siguen disponibles
- Si hay que re-enriquecer (nuevo modelo, nueva versión del prompt), se resetea solo `spot_semantic_state` sin perder nada del scraping

---

## API (api/main.py)

### Endpoints

| Endpoint | Tipo | Descripción |
|---|---|---|
| `GET /health` | Público | Estado + count de spots activos |
| `GET /points` | Auth | Spots activos en bbox (`north/south/east/west` obligatorios, `limit` ≤ 20000, orden por `master_rating DESC`). Filtros opcionales `tipo` y `gratuito`. Respuesta `{bbox, returned, total_in_bbox, truncated, spots[]}`. Usa índice GIST. |
| `GET /spot/{id}` | Auth | Detalle completo: spot + sources + enrichment + reviews |
| `GET /search` | Auth | SQL clásico: geo + tipo + gratuito + filtros semánticos materializados |
| `GET /search/semantic` | Auth | Búsqueda en lenguaje natural (Gemini + vector) |
| `GET /dashboard` | Auth | Estadísticas agregadas del sistema |
| `GET /admin/scrapers` | Auth | Lista resumen de todas las fuentes con semáforo de salud (rojo↑) |
| `GET /admin/scrapers/{nombre}` | Auth | Detalle de una fuente: credibilidad, stats, último run (spots + reviews) |
| `GET /admin/scrapers/{nombre}/history` | Auth | Historial de ejecuciones (`?limit=10`) — incluye runs de `_reviews` |
| `GET /admin/scrapers/{nombre}/samples` | Auth | Últimos N source_records insertados (`?limit=5`) sin raw_data |

### Autenticación

Middleware `X-API-Key` header. Si `API_SECRET_KEY` está vacía en env, no hay autenticación. Las rutas `/health`, `/`, `/favicon.ico`, `/pwa/*` son siempre públicas.

---

## Decisiones de Diseño

1. **Raw data inmutable**: `source_records.raw_data` es el JSON tal cual llegó de la fuente. Nunca modificar.
2. **COALESCE en enriquecer_spot**: la fuente que llega primero "gana" para datos básicos. La reconciliación posterior puede corregir esto para datos estructurados.
3. **Gemini como fallback, regex como primario**: más económico y predecible. El LLM solo entra cuando regex no detecta nada.
4. **Embeddings solo para spots con semantic_state**: garantiza que el texto embeddeado es rico y relevante.
5. **Puerto 25433 para PostgreSQL**: no colisiona con instalaciones locales de Postgres (5432).
6. **Un servicio `enrichment` separado**: permite escalar el procesamiento LLM independientemente del scraping.
