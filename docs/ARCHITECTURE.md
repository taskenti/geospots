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

## API (api/main.py)

### Endpoints

| Endpoint | Tipo | Descripción |
|---|---|---|
| `GET /health` | Público | Estado + count de spots activos |
| `GET /points` | Auth | Todos los spots activos para mapa (sin paginación — cuidado con volumen) |
| `GET /spot/{id}` | Auth | Detalle completo: spot + sources + enrichment + reviews |
| `GET /search` | Auth | SQL clásico: geo + tipo + gratuito + filtros semánticos materializados |
| `GET /search/semantic` | Auth | Búsqueda en lenguaje natural (Gemini + vector) |
| `GET /dashboard` | Auth | Estadísticas agregadas del sistema |

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
