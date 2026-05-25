# GeoSpots — Modelo de Datos

## Visión General

```
spots (1 por lugar físico real)
  ├── source_records (N — lo que sabe cada fuente del spot)
  ├── reviews (N — todas las reseñas de todas las fuentes)
  │     └── extracted_claims (N — claims extraídos por Phase 3)
  │           └── normalized_observations (N — observaciones normalizadas)
  ├── spot_semantic_state (1 — estado semántico actual, Phase 3)
  │     └── spot_semantic_snapshots (N — historial de cambios)
  └── spot_embeddings (1 — vector para búsqueda semántica, Phase 4)

spot_geo (1 — datos geofísicos, Phase 5/6, actualmente vacío)
countries (lookup — para clasificación geográfica automática)
signal_types (lookup — 21 señales semánticas)
source_credibility (lookup — score por fuente)
fuentes_config (control — configuración de scrapers)
scraper_log (auditoría — historial de ejecuciones)
dedup_log (auditoría — historial de fusiones)
raw_fetches / raw_payloads (opcional — datos crudos de API)
```

---

## Tabla: `spots`

**Clave primaria**: `id SERIAL`

### Identificación

| Campo | Tipo | Constraints | Descripción |
|---|---|---|---|
| `id` | `SERIAL` | PK | ID interno autoincremental |
| `canonical_name` | `TEXT` | NOT NULL | Nombre canónico (puede venir de reconciliación) |
| `tipo` | `TEXT` | CHECK(tipo IN (...)) | Categoría semántica del lugar |
| `fuentes` | `TEXT[]` | DEFAULT `{}` | Array de fuentes que aportan datos |
| `source_count` | `INT` | DEFAULT 1 | Número de fuentes (derivado de `fuentes`) |
| `activo` | `BOOL` | DEFAULT TRUE | Si el spot está activo/visible |
| `advertencia` | `TEXT` | nullable | Texto de advertencia (e.g. spot retirado) |
| `conflictos` | `JSONB` | nullable | Campos con valores contradictorios entre fuentes |

### Geolocalización

| Campo | Tipo | Constraints | Descripción |
|---|---|---|---|
| `lat` | `FLOAT8` | NOT NULL | Latitud WGS84 |
| `lon` | `FLOAT8` | NOT NULL | Longitud WGS84 |
| `geog` | `GEOGRAPHY(Point,4326)` | GENERATED ALWAYS AS | Columna calculada para PostGIS |
| `geohash7` | `TEXT` | GENERATED ALWAYS AS | Geohash de precisión 7 para clustering |
| `country_iso` | `TEXT` | nullable | Código ISO 2 letras (rellenado por trigger) |
| `continent` | `TEXT` | nullable | Continente (rellenado por trigger) |
| `subregion` | `TEXT` | nullable | Subregión ONU (rellenado por trigger) |
| `region` | `TEXT` | nullable | Región/provincia |
| `ciudad` | `TEXT` | nullable | Ciudad más cercana |
| `direccion` | `TEXT` | nullable | Dirección postal |

### Servicios (booleanos)

| Campo | Tipo | Descripción |
|---|---|---|
| `agua_potable` | `BOOL` | Agua potable |
| `electricidad` | `BOOL` | Conexión eléctrica |
| `ducha` | `BOOL` | Duchas disponibles |
| `wifi` | `BOOL` | WiFi disponible |
| `wc_publico` | `BOOL` | Baños públicos |
| `vaciado_negras` | `BOOL` | Punto vaciado aguas negras |
| `vaciado_grises` | `BOOL` | Punto vaciado aguas grises |
| `perros` | `BOOL` | Admite perros |
| `acceso_grandes` | `BOOL` | Accesible vehículos grandes |
| `iluminacion` | `BOOL` | Iluminación nocturna |
| `seguridad` | `BOOL` | Considerado seguro |
| `reserva_req` | `BOOL` | Requiere reserva |

### Precio

| Campo | Tipo | Descripción |
|---|---|---|
| `gratuito` | `BOOL` | Si es gratuito |
| `precio_aprox` | `FLOAT4` | Precio aprox en EUR |
| `precio_info` | `TEXT` | Texto libre de precio |

### Métricas

| Campo | Tipo | Descripción |
|---|---|---|
| `master_rating` | `FLOAT4` | Rating consolidado 0-10 |
| `total_reviews` | `INT` | Total de reviews (todas las fuentes) |
| `num_plazas` | `INT` | Plazas para vehículos |
| `altura_max_m` | `FLOAT4` | Altura máxima en metros |

### Contenido

| Campo | Tipo | Descripción |
|---|---|---|
| `descripcion_es` | `TEXT` | Descripción en español |
| `descripcion_en` | `TEXT` | Descripción en inglés |
| `descripcion_fr` | `TEXT` | Descripción en francés |
| `descripcion_de` | `TEXT` | Descripción en alemán |
| `fotos_urls` | `TEXT[]` | URLs de fotos |
| `tags` | `TEXT[]` | Etiquetas libres |
| `temporada_apertura` | `TEXT` | Temporada de apertura |
| `horario` | `TEXT` | Horario de acceso |
| `contacto` | `TEXT` | Teléfono/email |
| `url_fuente` | `TEXT` | URL en la fuente principal |

### Phase 3 (Semántico — Materializados)

Estos campos se copian desde `spot_semantic_state` para facilitar filtrado SQL sin JOIN:

| Campo | Tipo | Descripción |
|---|---|---|
| `quietud_score` | `FLOAT4` | Tranquilidad (−1 a +1) |
| `seguridad_score` | `FLOAT4` | Seguridad percibida |
| `belleza_score` | `FLOAT4` | Belleza del entorno |
| `aislamiento_score` | `FLOAT4` | Grado de aislamiento |
| `riesgo_policia_score` | `FLOAT4` | Riesgo de multa/visita policial |
| `masificacion_score` | `FLOAT4` | Nivel de masificación |
| `cerca_mar` | `BOOL` | Cerca del mar (observación semántica) |
| `pernocta_ok` | `BOOL` | Si se puede pernoctar tranquilamente |
| `semantic_updated_at` | `TIMESTAMPTZ` | Última actualización semántica |

### Timestamps

| Campo | Tipo | Descripción |
|---|---|---|
| `created_at` | `TIMESTAMPTZ` | DEFAULT NOW() |
| `updated_at` | `TIMESTAMPTZ` | Se actualiza en cada cambio |

### Índices en `spots`

```sql
-- Búsqueda geoespacial (principal)
CREATE INDEX spots_geog_idx ON spots USING GIST(geog);

-- Filtros más frecuentes en /search
CREATE INDEX spots_tipo_idx ON spots(tipo) WHERE activo = TRUE;
CREATE INDEX spots_country_idx ON spots(country_iso) WHERE activo = TRUE;
CREATE INDEX spots_gratuito_idx ON spots(gratuito) WHERE activo = TRUE;

-- Geohash para clustering de mapa
CREATE INDEX spots_geohash_idx ON spots(geohash7);

-- Búsqueda de texto (dedup + /search)
CREATE INDEX spots_name_trgm_idx ON spots USING GIN(canonical_name gin_trgm_ops);
```

---

## Tabla: `source_records`

Almacena lo que sabe cada fuente de cada spot. La tripla `(source, source_id)` es única.

| Campo | Tipo | Constraints | Descripción |
|---|---|---|---|
| `id` | `SERIAL` | PK | |
| `spot_id` | `INT` | FK → spots.id, NOT NULL | Spot al que pertenece |
| `source` | `TEXT` | NOT NULL | Fuente (e.g. `'park4night'`) |
| `source_id` | `TEXT` | NOT NULL | ID en la fuente original |
| `raw_data` | `JSONB` | nullable | JSON crudo tal cual llegó de la API |
| `normalized_data` | `JSONB` | nullable | Dict normalizado al schema GeoSpots |
| `normalized` | `JSONB` | nullable | **DUPLICADO** — artifact de migración, mismo contenido que `normalized_data` |
| `created_at` | `TIMESTAMPTZ` | DEFAULT NOW() | |
| `updated_at` | `TIMESTAMPTZ` | | |

**UNIQUE**: `(source, source_id)`

**Nota**: el campo `normalized` es un duplicado de `normalized_data`. Existe por una migración antigua. Se puede eliminar en una migración de limpieza futura.

### Índices en `source_records`

```sql
CREATE INDEX sr_spot_id_idx ON source_records(spot_id);
CREATE INDEX sr_source_idx ON source_records(source);
CREATE UNIQUE INDEX sr_source_id_unique ON source_records(source, source_id);
```

---

## Tabla: `reviews`

Todas las reseñas de todas las fuentes, enlazadas a spots canónicos.

| Campo | Tipo | Constraints | Descripción |
|---|---|---|---|
| `id` | `SERIAL` | PK | |
| `spot_id` | `INT` | FK → spots.id, NOT NULL | Spot al que pertenece |
| `source` | `TEXT` | NOT NULL | Fuente de la review |
| `source_review_id` | `TEXT` | NOT NULL | ID de la review en la fuente |
| `texto_original` | `TEXT` | | Texto raw sin modificar |
| `texto_limpio` | `TEXT` | | Texto tras `review_cleaner.clean_review()` |
| `texto_dsl` | `TEXT` | | DSL compacto `"quiet:+0.8 police:-0.2"` |
| `rating` | `FLOAT4` | | Rating 0-10 normalizado |
| `fecha` | `DATE` | | Fecha de la review |
| `autor` | `TEXT` | | Nombre/nick del autor |
| `idioma` | `TEXT` | | Código ISO de idioma detectado |
| `informativo` | `BOOL` | DEFAULT NULL | TRUE si tiene contenido relevante |
| `cleaned` | `BOOL` | DEFAULT FALSE | Si pasó por review_cleaner |
| `llm_processed` | `BOOL` | DEFAULT FALSE | Si pasó por el pipeline Phase 3 |
| `llm_analysis` | `JSONB` | | Resultado completo del análisis LLM |
| `created_at` | `TIMESTAMPTZ` | DEFAULT NOW() | |
| `updated_at` | `TIMESTAMPTZ` | | |

**UNIQUE**: `(source, source_review_id)`

### Índices en `reviews`

```sql
CREATE INDEX reviews_spot_id_idx ON reviews(spot_id);
CREATE INDEX reviews_llm_pending ON reviews(id) WHERE llm_processed = FALSE AND informativo = TRUE;
CREATE INDEX reviews_spot_date_idx ON reviews(spot_id, fecha DESC);
```

---

## Tabla: `signal_types` (lookup)

Define las 21 señales semánticas del sistema Phase 3. Tabla de referencia, no modificar en runtime.

| Campo | Tipo | Descripción |
|---|---|---|
| `name` | `TEXT` | PK — clave de señal (e.g. `'quietud'`) |
| `decay_class` | `TEXT` | `'permanent'`, `'slow'`, `'volatile'` |
| `half_life_days` | `INT` | Vida media en días para decay exponencial |
| `aggregation_strategy` | `TEXT` | `'weighted_mean'`, `'consensus_boolean'`, `'recent_wins'` |
| `contradiction_strategy` | `TEXT` | `'recent_wins'`, `'majority_consensus'`, `'permanent_override'` |
| `value_type` | `TEXT` | `'numeric'`, `'boolean'`, `'text'` |
| `description` | `TEXT` | Descripción en texto |

### Señales activas y su configuración

| Señal | Decay | Half-life | Aggregation | Tipo |
|---|---|---|---|---|
| `quietud` | slow | 365d | weighted_mean | numeric |
| `seguridad` | slow | 180d | weighted_mean | numeric |
| `belleza` | permanent | 36500d | weighted_mean | numeric |
| `aislamiento` | permanent | 36500d | weighted_mean | numeric |
| `riesgo_policia` | volatile | 60d | recent_wins | numeric |
| `masificacion` | volatile | 30d | weighted_mean | numeric |
| `calidad_suelo` | slow | 730d | weighted_mean | numeric |
| `acceso_dificultad` | slow | 730d | weighted_mean | numeric |
| `ruido_trafico` | slow | 365d | weighted_mean | numeric |
| `mosquitos` | volatile | 30d | weighted_mean | numeric |
| `sombra` | permanent | 36500d | weighted_mean | numeric |
| `cerca_mar` | permanent | 36500d | consensus_boolean | boolean |
| `cerca_montana` | permanent | 36500d | consensus_boolean | boolean |
| `pernocta_ok` | slow | 180d | consensus_boolean | boolean |
| `restos_basura` | volatile | 14d | recent_wins | numeric |
| `vistas_panoramicas` | permanent | 36500d | consensus_boolean | boolean |
| `para_familias` | slow | 365d | consensus_boolean | boolean |
| `para_furgoneta` | slow | 365d | consensus_boolean | boolean |
| `para_autocaravana` | slow | 365d | consensus_boolean | boolean |
| `ambiente` | volatile | 90d | weighted_mean | text |
| `nivel_privacidad` | slow | 365d | weighted_mean | numeric |

---

## Tabla: `extracted_claims`

Claims individuales extraídos de reviews en Phase 3.

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | `SERIAL` | PK |
| `review_id` | `INT` | FK → reviews.id |
| `spot_id` | `INT` | FK → spots.id (desnormalizado para queries) |
| `signal` | `TEXT` | FK → signal_types.name |
| `value` | `TEXT` | Valor bruto extraído |
| `confidence` | `FLOAT4` | Confianza de extracción 0-1 |
| `excerpt` | `TEXT` | Fragmento de texto de origen |
| `extractor` | `TEXT` | `'regex'` o `'gemini'` |
| `created_at` | `TIMESTAMPTZ` | |

---

## Tabla: `normalized_observations`

Observaciones normalizadas con peso temporal.

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | `SERIAL` | PK |
| `spot_id` | `INT` | FK → spots.id |
| `claim_id` | `INT` | FK → extracted_claims.id |
| `signal_type` | `TEXT` | FK → signal_types.name |
| `value_num` | `FLOAT4` | Valor numérico (−1 a +1) si aplica |
| `value_bool` | `BOOL` | Valor booleano si aplica |
| `value_text` | `TEXT` | Valor texto si aplica |
| `weight` | `FLOAT4` | Peso = extractor_conf × source_conf × reviewer_conf |
| `observed_at` | `DATE` | Fecha de la observación (de la review) |
| `extractor` | `TEXT` | `'regex'`, `'gemini'`, o `'geo_computed_v1'` (Phase 6) |
| `extraction_confidence` | `FLOAT4` | Confianza del extractor |

---

## Tabla: `spot_semantic_state`

Estado semántico actual de un spot. Un registro por spot, actualizado incrementalmente.

| Campo | Tipo | Descripción |
|---|---|---|
| `spot_id` | `INT` | PK + FK → spots.id |
| `signals` | `JSONB` | `{signal_name: {mean, weight_sum, sample_count, last_updated}}` |
| `semantic_dsl` | `TEXT` | DSL compacto `"quiet:+0.8 sea:T"` |
| `observation_count` | `INT` | Total observaciones procesadas |
| `last_signal_update` | `TIMESTAMPTZ` | Última actualización de cualquier señal |
| `created_at` | `TIMESTAMPTZ` | |
| `updated_at` | `TIMESTAMPTZ` | |

---

## Tabla: `spot_semantic_snapshots`

Historial de cambios semánticos significativos (distancia > 0.15).

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | `SERIAL` | PK |
| `spot_id` | `INT` | FK → spots.id |
| `signals_snapshot` | `JSONB` | Estado de señales en ese momento |
| `semantic_dsl` | `TEXT` | DSL en ese momento |
| `trigger_event` | `TEXT` | Qué desencadenó el snapshot |
| `created_at` | `TIMESTAMPTZ` | |

---

## Tabla: `spot_embeddings`

Vector de embedding para búsqueda semántica (Phase 4).

| Campo | Tipo | Descripción |
|---|---|---|
| `spot_id` | `INT` | PK + FK → spots.id |
| `embedding` | `vector(768)` | Google text-embedding-004 |
| `texto_embebido` | `TEXT` | Texto que se embebió (para debugging) |
| `modelo` | `TEXT` | Nombre del modelo usado |
| `generated_at` | `TIMESTAMPTZ` | Cuándo se generó |

### Índice HNSW

```sql
CREATE INDEX spot_embeddings_hnsw_idx
ON spot_embeddings
USING hnsw(embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

---

## Tabla: `spot_geo` (Phase 5/6 — vacía actualmente)

Datos geofísicos calculados por capas 5 y 6.

30 columnas incluyendo: `elevacion_m`, `pendiente_grados`, `aspecto_grados`, `sombra_matutina`, `sombra_vespertina`, `distancia_carretera_m`, `distancia_edificios_m`, `stealth_score`, `acceso_vehiculo_grande_geo`, etc.

**Estado actual**: tabla creada en schema, 0 filas. Ver [fase-6-geo-intelligence.md](fase-6-geo-intelligence.md).

---

## Tabla: `source_credibility` (lookup/seed)

| Campo | Tipo | Descripción |
|---|---|---|
| `source` | `TEXT` | PK — nombre de la fuente |
| `base_score` | `FLOAT4` | Score de confianza base 0-1 |
| `review_score` | `FLOAT4` | Confianza específica de reviews |
| `geo_accuracy` | `FLOAT4` | Confianza en coordenadas |
| `notes` | `TEXT` | Notas sobre la fuente |

---

## Tabla: `fuentes_config`

Control de qué scrapers están activos.

| Campo | Tipo | Descripción |
|---|---|---|
| `nombre` | `TEXT` | PK — clave del scraper |
| `activa` | `BOOL` | Si se ejecuta en el scheduler |
| `spots_totales` | `INT` | Contador de spots por fuente |
| `ultima_ejecucion` | `TIMESTAMPTZ` | Última vez que corrió |
| `config_json` | `JSONB` | Configuración extra (no usado actualmente) |

---

## Tabla: `scraper_log`

Auditoría de ejecuciones de scrapers.

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | `SERIAL` | PK |
| `source` | `TEXT` | Fuente ejecutada |
| `started_at` | `TIMESTAMPTZ` | Inicio |
| `finished_at` | `TIMESTAMPTZ` | Fin |
| `stats` | `JSONB` | `{nuevos, actualizados, reviews_nuevas, errores}` |
| `status` | `TEXT` | `'running'`, `'ok'`, `'error'` |

---

## Tabla: `dedup_log`

Historial de fusiones/deduplicaciones.

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | `SERIAL` | PK |
| `spot_id_kept` | `INT` | Spot que se conservó |
| `spot_id_merged` | `INT` | Spot que se fusionó (ya no activo) |
| `source` | `TEXT` | Fuente que desencadenó la fusión |
| `distance_m` | `FLOAT4` | Distancia entre los dos puntos |
| `reason` | `TEXT` | Razón de la fusión |
| `created_at` | `TIMESTAMPTZ` | |

---

## Tablas de Buffer (Ingesta)

### `raw_fetches`

Log de requests HTTP crudos (opcional, para debugging).

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | `SERIAL` | PK |
| `source` | `TEXT` | Fuente |
| `url` | `TEXT` | URL del request |
| `status_code` | `INT` | Código HTTP |
| `response_body` | `TEXT` | Body crudo |
| `fetched_at` | `TIMESTAMPTZ` | |

### `raw_payloads`

Payloads individuales antes de normalizar.

---

## Tabla: `countries` (lookup)

Cargada desde `ne_50m_admin_0_countries.json` (Natural Earth 50m).

| Campo | Tipo | Descripción |
|---|---|---|
| `iso_a2` | `TEXT` | Código ISO 2 letras |
| `name` | `TEXT` | Nombre en inglés |
| `continent` | `TEXT` | Continente |
| `subregion` | `TEXT` | Subregión ONU |
| `geom` | `GEOMETRY(MultiPolygon,4326)` | Polígono del país |

Usada por el trigger `trg_classify_spot`:
```sql
-- Trigger BEFORE INSERT OR UPDATE OF lat,lon en spots
-- ST_Contains(countries.geom, ST_MakePoint(NEW.lon, NEW.lat))
-- Fallback: ST_DWithin(countries.geom, punto, 0.5) para islas/costas
```

---

## Vista: `spot_temperature`

```sql
CREATE VIEW spot_temperature AS
SELECT
    id,
    canonical_name,
    total_reviews,
    CASE
        WHEN total_reviews >= 10 THEN 'hot'
        WHEN total_reviews >= 3  THEN 'warm'
        ELSE 'cold'
    END AS temperature
FROM spots WHERE activo = TRUE;
```

Usada por `enrichment/worker.py` para priorizar el procesamiento LLM (procesa "hot" primero).

---

## Relaciones Clave

```
spots (1) ──── (N) source_records    [spot_id FK]
spots (1) ──── (N) reviews           [spot_id FK]
spots (1) ──── (1) spot_semantic_state [spot_id PK=FK]
spots (1) ──── (N) spot_semantic_snapshots [spot_id FK]
spots (1) ──── (1) spot_embeddings   [spot_id PK=FK]
spots (1) ──── (1) spot_geo          [spot_id PK=FK, Phase 6]
reviews (1) ── (N) extracted_claims  [review_id FK]
extracted_claims (1) ─ (N) normalized_observations [claim_id FK]
signal_types (1) ── (N) normalized_observations [signal_type FK]
countries (lookup) ← trigger en spots.lat/lon
```

---

## Campos Calculados/Derivados

| Campo | Tabla | Cómo se calcula |
|---|---|---|
| `geog` | spots | GENERATED ALWAYS AS `ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography` |
| `geohash7` | spots | GENERATED ALWAYS AS `ST_GeoHash(ST_MakePoint(lon, lat), 7)` |
| `country_iso`, `continent`, `subregion` | spots | Trigger `trg_classify_spot` en INSERT/UPDATE lat,lon |
| `spots_totales` | fuentes_config | Actualizado por `sync_db.py` o tras cada scrape |
| `total_reviews` | spots | Actualizado por `upsert_review()` en db.py |
| `semantic_dsl` | spot_semantic_state | Generado por `dsl_generator.generate_spot_dsl()` |
| `temperatura` | (view) spot_temperature | Derivada de total_reviews |
| `weight` | normalized_observations | `extraction_confidence × source_confidence × reviewer_confidence` |
