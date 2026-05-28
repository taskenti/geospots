# Phase 3 — Hardening pre-batch LLM (239K spots)

**Estado:** Plan aprobado, pendiente de ejecución.
**Origen:** Auditoría multi-revisor del pipeline `orchestrator_v2` + `worker.py` con caso de estudio Spot 85057 (Grau Roig, Andorra).
**Objetivo:** Endurecer el pipeline de enrichment LLM antes del batch masivo (~$272 input + ~$130 output con DeepSeek tras optimizaciones).
**No es una fase nueva.** Es trabajo de hardening sobre Phase 3 existente, previo a habilitar `--all`.

---

## Por qué este plan existe

Auditamos el pipeline con Grau Roig (3 reviews multilingües, contradicción temporal obras 2025 vs tranquilo 2026) y aparecieron 4 clases de problemas:

1. **Fuga de datos:** atributos valiosos como `terrain[]` y estados temporales (obras) no tienen receptor estructurado.
2. **Coste:** invalidación sistemática de prefix caching por orden volátil-primero en el user prompt.
3. **Redundancia circular:** SERVICES de la DB → prompt → LLM → claim idéntico → DB.
4. **Inconsistencia cronológica:** el summary invierte fechas, scores ignoran obras documentadas.

El plan resuelve estos 4 antes del batch. Lo que sobra (evidence vectors, sistemas reactivos de invalidación, ontologías formales) queda en Tier 3 y solo se ejecuta si los datos reales lo justifican.

---

## Decisiones cerradas

| ID | Decisión | Valor |
|---|---|---|
| D1 | Decay de eventos volátiles | `0.85^n` (moderado, placeholder revalidable a 3 meses) |
| D2 | Umbral `likely_resolved` | `confidence < 0.3` **+ guarda temporal**: nunca antes de 180 días desde `valid_from` |
| D3 | Granularidad invalidación embeddings | Solo cambios en estado operativo volátil (no en scores continuos) |
| D4 | Cuándo invalidar | Cron nocturno en `nightly_embeddings.py` ampliado, no triggers ni outbox |
| D5 | Léxico multilingüe inicial | 5 señales (`construction`, `closure`, `noise_source`, `police_pressure`, `wild_camping`) × 6 idiomas (EN/ES/FR/NL/DE/IT) |
| D6 | Combinación léxico vs LLM | 30% prior léxico + 70% juicio LLM contextual |
| D7 | Regression suite v1 | 20-30 casos, **dividida en hard invariants / statistical bands / soft review** |
| D8 | Atributos geofísicos (`elevation_m`, `terrain_surface`, `slope_grade`) | Poblar tabla existente `spot_geo` (Phase 6), **no** ensuciar `spots`. El LLM emite estos campos; pipeline los escribe en `spot_geo`. |

**Mecanismo de invalidación:** `semantic_fingerprint` (hash SHA1 truncado a 16 chars de `canonical_tags + operational_state + embedding_input_text + embedding_schema_version`), no `state_version` simple.

---

## Principios operativos (no negociables durante la implementación)

1. **Inmutables vs regenerables.** Solo son inmutables: `source_records.raw_data`, `reviews.texto`, `extracted_claims`. Todo lo demás (`normalized_observations`, `spot_semantic_state`, embeddings, summaries, tags, scores) debe poder regenerarse desde los inmutables. Cualquier cambio que rompa esto se rechaza.
2. **El LLM no decide verdad sobre estados discretos.** Para `under_construction`, `closed`, etc.: el LLM extrae claims, un resolver determinista decide estado.
3. **Para scores continuos** (`quietness`, `beauty`, etc.) sí se agrega LLM con peso temporal. No es contradicción con (2): son problemas distintos.
4. **Ausencia de evidencia ≠ evidencia de ausencia.** Los estados volátiles negativos (obras, cierre) no se resuelven por silencio puro; requieren decay + guarda temporal + opcionalmente N reviews posteriores sin mención.
5. **Cualquier llamada LLM nueva pasa por `enrichment/llm_provider.call_llm_sync`.** Nunca importar SDK directamente.
6. **Cambios que afecten al prompt o al schema de output deben pasar regression suite antes de merge.**

---

## Tier 0 — Preparación (1-2 días, $0 API)

Estos pasos no tocan producción ni el LLM. Son para evitar implementar a ciegas.

### T0.1 — Lectura de código real

Leer y mapear comportamiento actual de:
- `enrichment/prompts.py` — confirmar estructura system/user prompt actual.
- `enrichment/spot_packager.py` — confirmar qué incluye en SERVICES, formato de reviews.
- `enrichment/state_aggregator.py` — confirmar cómo agrega observaciones, pesos temporales actuales.
- `enrichment/signal_registry.py` — verificar si ya existen aliases, decay_profile por señal.
- `enrichment/event_detector.py` — confirmar qué eventos ya detecta y cómo (CLAUDE.md menciona `police_burst`).
- `jobs/nightly_embeddings.py` — confirmar lógica actual de "stale" y filtro WHERE.
- `enrichment/worker.py` y `enrichment/orchestrator_v2.py` — confirmar interacción entre review-level y spot-level.

**Output esperado:** una nota corta (`docs/fase-3-hardening-codebase-notes.md`) listando qué de este plan **ya existe** y qué hay que construir. Sin esto, el plan está hablando contra una descripción, no contra el código.

### T0.2 — Construir regression suite v1

Crear `tests/regression/semantic_suite.py` con 20-30 casos. Distribución sugerida:

- 2 casos obras temporales (incluido Grau Roig 85057).
- 3 contradicción servicios DB vs reviews (agua dice sí, reviews dicen rota).
- 3 multilingüe con palabras culturalmente cargadas (NL "bouwput", DE "Baustelle", FR "chantier").
- 2 spots con 1 sola review (edge case agregación).
- 1 cerrado permanentemente.
- 2 estacionales.
- 3 redundancia circular (verificar que SERVICES no genera claims duplicados).
- 2 edge cases temporales (reviews de la misma semana con scores opuestos).
- 2 agregación con varianza alta.

**Tres niveles de validación:**

```yaml
hard_invariants:        # rompen build si fallan
  - chronology_inverted == false
  - operational_state detected when reviews mention it
  - parse_failure == false
  - redundant_claims_without_review_id == 0

statistical_bands:      # warning, no break
  - quietness ∈ [0.4, 0.7] para Grau Roig
  - confidence > 0.6
  - summary_word_count ∈ [60, 120]

soft_review:            # muestreo humano mensual
  - 20 spots aleatorios revisados a mano post-batch
```

Snapshot del output actual del pipeline para cada caso como baseline pre-cambios. No para comparar igualdad exacta, para detectar regresiones cualitativas.

### T0.3 — Documentar inmutables vs regenerables

Añadir sección en `CLAUDE.md`:

```markdown
## Datos regenerables vs. inmutables

**Inmutables (nunca borrar/sobreescribir):**
- source_records.raw_data
- reviews.texto, reviews.texto_original
- extracted_claims (cuando review_id IS NOT NULL)

**Regenerables desde inmutables:**
- normalized_observations
- spot_semantic_state
- spot_embeddings
- spot_operational_state (cuando se resuelve por decay)
- Todos los summary_en, tags, best_for, scores
```

---

## Tier 1 — Bloqueantes pre-batch (3-4 días)

### T1.1 — Prompt tail-loading + few-shot estables

**Archivo:** `enrichment/prompts.py` + `enrichment/spot_packager.py`

Refactorizar `build_spot_user_prompt`:

```
[PREFIJO ESTÁTICO - cacheable, ~3.5K tokens]
=== SCHEMA INSTRUCTIONS ===
[instrucciones de formato, leyenda de campos,
 definiciones de signal types, reglas de extracción]
=== END SCHEMA ===

=== FEW-SHOT EXAMPLES (2-3 canónicos, idénticos byte-a-byte entre llamadas) ===
Example 1: spot con obras temporales → input/output JSON esperado
Example 2: spot con contradicción servicios DB vs reviews → output
Example 3: spot multilingüe con palabra culturalmente cargada → output
=== END EXAMPLES ===

[SUFIJO VOLÁTIL]
=== SPOT DATA ===
CURRENT_DATE: 2026-05-28
SPOT id=85057
Name: ...
Coords: ...
[STATIC_CONTEXT - read-only, ver T1.2]
[REVIEW_EVIDENCE - ver T1.2]
=== END SPOT ===

[MINI-DIRECTRIZ FINAL - anti recency-bias]
Return strictly the JSON matching the schema above.
```

**Reglas duras para no romper la caché:**
- Nada de timestamps absolutos en el prefijo. `CURRENT_DATE` va en el sufijo SPOT DATA.
- Serializar SPOT DATA con orden determinista de claves (`sort_keys=True`). Sin espacios variables.
- Few-shot examples versionados como `prompt_version` en código; un cambio incrementa la versión y se asume cache miss esa semana.
- Si se añade contexto de país (`COUNTRY_CONTEXT`), va **entre** el prefijo estático y los datos del spot, y se mantiene cacheado dentro del lote del país (de ahí el batching país-a-país, ya planificado en CLAUDE.md).

**Criterio de aceptación:** cache hit rate del **user prompt** medido en logs de DeepSeek >65% sobre 200+ llamadas consecutivas en Andorra.

### T1.2 — STATIC_CONTEXT vs REVIEW_EVIDENCE separados

**Archivo:** `enrichment/spot_packager.py` + schema de output en `prompts.py`

Cambiar bloque actual `SERVICES (structured facts...)` a:

```
<STATIC_CONTEXT readonly="true">
[Solo campos con contradicción posible: agua, electricidad, wifi, seguridad]
agua_potable: false
electricidad: false
wifi: true
seguridad: false
</STATIC_CONTEXT>

<REVIEW_EVIDENCE>
[review_id=172992] [age: 31d ago] [2026-04] [campercontact] ★★★★★
  parfait. endroit trés calme...
[review_id=172993] [age: 313d ago] [2025-07] [campercontact] ★
  Dit deel is op dit moment één grote bouwput...
</REVIEW_EVIDENCE>
```

Schema de output del LLM debe obligar:

```json
{
  "review_claims": [...],          // claims extraídos SOLO de REVIEW_EVIDENCE
  "contradicted_static_facts": [...], // solo si review choca con STATIC_CONTEXT
  "operational_state": {...},      // ver T1.4
  "summary_en": "...",
  "tags": [...],
  "best_for": [...]
}
```

**Postprocesado debe rechazar:** cualquier item en `review_claims` con `review_id IS NULL` (significa que el LLM re-emitió STATIC_CONTEXT como claim de review).

**Criterio de aceptación:** en Andorra, 0 claims persistidos con `review_id IS NULL` salvo los de `scraped_facts_v1`.

### T1.3 — CURRENT_DATE + age relativo por review

**Archivo:** `enrichment/spot_packager.py`

Inyectar `CURRENT_DATE: YYYY-MM-DD` al inicio del bloque SPOT DATA y prefijar cada review con `[age: Xd ago]` calculado contra `CURRENT_DATE`.

**Criterio de aceptación:** en regression suite, ningún summary invierte cronología (caso Grau Roig: no debe describir 2025 como "recent" cuando hay reviews de 2026).

### T1.4 — `spot_alerts` con lifecycle (sustituye a `spot_operational_state`)

**Archivo:** `db/migration_phase3_v6.sql` (nueva migración) + `enrichment/state_aggregator.py`

Diseño rico que cubre más tipos que solo "obras": cierres estacionales, prohibiciones, riesgos naturales, eventos.

```sql
CREATE TABLE spot_alerts (
  id BIGSERIAL PRIMARY KEY,
  spot_id BIGINT REFERENCES spots(id) ON DELETE CASCADE,
  alert_type TEXT NOT NULL,
    -- 'construction' | 'closed_season' | 'access_restricted'
    -- | 'temporary_ban' | 'natural_hazard' | 'event_overflow'
    -- | 'permanently_closed'
  severity NUMERIC(3,2) NOT NULL,           -- 0..1
  detected_at TIMESTAMPTZ NOT NULL,         -- fecha de la review/source
  valid_from DATE NOT NULL,
  valid_until DATE,                         -- NULL = indefinido
  confidence NUMERIC(3,2) NOT NULL,
  source_observations BIGINT[],             -- FKs a normalized_observations
  source_review_ids BIGINT[],
  detected_by TEXT NOT NULL,                -- 'llm_v4' | 'scraped_facts' | 'manual'
  summary TEXT,
  resolved BOOLEAN DEFAULT FALSE,
  last_decay_at TIMESTAMP,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_active_alerts ON spot_alerts (spot_id) WHERE resolved = FALSE;
CREATE INDEX idx_alerts_type_validity ON spot_alerts (alert_type, valid_until)
  WHERE resolved = FALSE;
CREATE INDEX idx_alerts_gin ON spot_alerts USING GIN (alert_type)
  WHERE resolved = FALSE AND (valid_until IS NULL OR valid_until > NOW());
```

El schema del LLM debe devolver array (un spot puede tener varias alertas activas simultáneas):
```json
"alerts": [
  {
    "alert_type": "construction",
    "severity": 0.82,
    "valid_from_inferred": "2025-06",
    "confidence": 0.82,
    "source_review_ids": [172993, 172994],
    "summary": "Heavy construction reported by two visitors in summer 2025"
  }
]
```

**Resolver determinista** (`enrichment/state_resolver.py` nuevo):

```python
def apply_decay(alert_row, current_date):
    # D1: decay 0.85^n donde n = meses desde última observación
    months_since = months_between(alert_row.last_decay_at, current_date)
    new_confidence = alert_row.confidence * (0.85 ** months_since)

    # D2: marcar resolved si confidence < 0.3 Y >=180 días desde valid_from
    days_since_start = (current_date - alert_row.valid_from).days
    if new_confidence < 0.3 and days_since_start >= 180:
        return mark_resolved(alert_row, current_date)  # set resolved=TRUE, valid_until=current_date
    return update_confidence(alert_row, new_confidence)
```

Cron diario que ejecute decay sobre alertas con `resolved=FALSE`. **`permanently_closed` y `permanent_*` no decaen** — requieren resolución manual.

**Criterio de aceptación:** Grau Roig tras procesamiento muestra una fila en `spot_alerts` con `alert_type='construction'`, `valid_from ∈ 2025-06`, `confidence > 0.5`, `resolved=FALSE`.

### T1.4b — Clasificación funcional del spot

**Archivo:** misma migración + schema del LLM ampliado

Resuelve el bug "Andorra Campers clasificado como spot de pernocta cuando es un taller".

```sql
ALTER TABLE spots ADD COLUMN spot_function TEXT;
  -- 'overnight_primary' | 'overnight_tolerated' | 'service_only'
  -- | 'shop_workshop' | 'transit' | 'daytime_only'
ALTER TABLE spots ADD COLUMN is_overnight_viable BOOLEAN;
ALTER TABLE spots ADD COLUMN authorization_status TEXT;
  -- 'official' | 'tolerated' | 'sign_authorized' | 'illegal' | 'unknown'
```

El schema del LLM debe devolver estos 3 campos como top-level (no enterrados en summary). El LLM **solo los emite si tiene evidencia**; si la columna queda `NULL` significa "no determinado", no "false".

**D8 (atributos geofísicos):** mismo schema del LLM emite `elevation_m`, `terrain_surface`, `slope_grade`. Pero **escriben en `spot_geo`, no en `spots`.** Si `spot_geo` no tiene fila para el spot, crearla. Esto consolida Phase 6 sin esperar a su sprint dedicado.

```sql
-- spot_geo ya existe (Phase 6). Asegurar columnas:
ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS elevation_m INT;
ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS terrain_surface TEXT;
ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS slope_grade TEXT;
ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS source TEXT;  -- 'llm_v4' | 'dem' | 'osm'
```

**Criterio de aceptación:**
- Andorra Campers tras procesamiento tiene `spot_function='shop_workshop'`, `is_overnight_viable=false`.
- Grau Roig tiene `authorization_status='sign_authorized'` y `spot_geo.elevation_m=2110`.

### T1.4c — `signal_flux` + `active_alert_types` en `spot_semantic_state`

**Archivo:** misma migración + `enrichment/state_aggregator.py`

Materializar info de alertas activas y cambios de régimen para que la API filtre sin JOIN:

```sql
ALTER TABLE spot_semantic_state ADD COLUMN active_alert_types TEXT[] DEFAULT '{}';
ALTER TABLE spot_semantic_state ADD COLUMN signal_flux JSONB DEFAULT '{}';
-- signal_flux ejemplo:
-- {"quietness": {"changed": true, "old": 0.82, "new": 0.31,
--                "since": "2025-03-01", "n_recent": 3}}

CREATE INDEX idx_active_alert_types_gin ON spot_semantic_state USING GIN (active_alert_types);
```

`active_alert_types` se recomputa en cada update tras leer `spot_alerts WHERE spot_id=X AND resolved=FALSE`. Permite a `/search/semantic`:

```sql
WHERE NOT 'construction' = ANY(active_alert_types)
```

`signal_flux` se rellena por T2.5 (detección de cambio de régimen). Pre-batch va vacío; la columna existe pero solo se popula post-Tier 2.

**Criterio de aceptación:** Grau Roig tras procesamiento muestra `active_alert_types = ['construction']`.

### T1.5 — Canonicalizador de tags + unknown_tags con frequency tracking

**Archivos:** `enrichment/tag_canonicalizer.py` (nuevo) + `db/migration_phase3_v6.sql`

```sql
CREATE TABLE canonical_tags (
  canonical_id TEXT PRIMARY KEY,
  aliases TEXT[] NOT NULL DEFAULT '{}',
  category TEXT,
  added_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE unknown_tags (
  tag TEXT PRIMARY KEY,
  first_seen TIMESTAMP DEFAULT NOW(),
  last_seen TIMESTAMP DEFAULT NOW(),
  occurrence_count INT DEFAULT 1,
  reviewed BOOLEAN DEFAULT FALSE
);
```

Función `canonicalize_tag(raw_tag) -> canonical_id | None`. Si retorna None: incrementar `unknown_tags.occurrence_count`, NO persistir en `spot_semantic_state.tags`.

Job mensual `jobs/review_unknown_tags.py`: lista top 20 por `occurrence_count`, output para revisión humana.

**Criterio de aceptación:** en Andorra, todos los tags persistidos en `spot_semantic_state.tags` existen en `canonical_tags`.

### T1.6 — `semantic_fingerprint` + invalidación de embeddings

**Archivos:** migration + `enrichment/embedding_generator.py` + `jobs/nightly_embeddings.py`

```sql
ALTER TABLE spot_semantic_state
  ADD COLUMN semantic_fingerprint TEXT;
ALTER TABLE spot_embeddings
  ADD COLUMN built_from_fingerprint TEXT;
CREATE INDEX idx_emb_fingerprint
  ON spot_embeddings(built_from_fingerprint);
```

Función:
```python
def compute_fingerprint(state_row, embedding_schema_version):
    canonical_tags = sorted(state_row.tags or [])
    active_alerts = sorted(state_row.active_alert_types or [])
    text = build_embedding_input_text(state_row)
    payload = f"{canonical_tags}|{active_alerts}|{text}|{embedding_schema_version}"
    return hashlib.sha1(payload.encode()).hexdigest()[:16]
```

Recalcular fingerprint en cada update de `spot_semantic_state` y en cada cambio de `spot_alerts` que toque `active_alert_types`. **D3:** la actualización solo se dispara por cambios en tags canónicos o tipos de alerta activos, no por cambios menores en scores continuos.

`nightly_embeddings.py`:
```sql
SELECT spot_id FROM spot_semantic_state s
LEFT JOIN spot_embeddings e ON s.spot_id = e.spot_id
WHERE e.built_from_fingerprint IS NULL
   OR e.built_from_fingerprint != s.semantic_fingerprint
LIMIT batch_size;
```

**Criterio de aceptación:** simular cambio en `active_alert_types` de 5 spots de Andorra, verificar que el siguiente run de `nightly_embeddings` los regenera.

### T1.7 — Idempotencia / reanudación del worker

**Archivo:** `enrichment/worker.py` + `enrichment/orchestrator_v2.py`

Verificar que el filtro WHERE del worker incluye `enrichment_version < CURRENT_VERSION` para que un kill -9 a mitad de batch no reprocese spots ya completados.

Para orchestrator_v2:
```sql
WHERE NOT EXISTS (
  SELECT 1 FROM spot_semantic_state s
  WHERE s.spot_id = spots.id
    AND s.enrichment_version >= 4
)
```

Para worker.py: `WHERE reviews.llm_processed = FALSE`.

**Criterio de aceptación:** kill -9 al worker después de procesar 50 spots en Andorra, reiniciar, verificar que los 50 no se reprocesan (medido por `llm_calls_total` en logs).

### T1.8 — Observabilidad: cache hit rate por llamada

**Archivo:** `enrichment/llm_provider.py`

Sin observabilidad, T1.1 es ciego. DeepSeek devuelve `usage.prompt_cache_hit_tokens` y `usage.prompt_tokens` en cada respuesta. Loggear en cada llamada:

```python
cache_hit_ratio = (
    response.usage.prompt_cache_hit_tokens / response.usage.prompt_tokens
    if response.usage.prompt_tokens > 0 else 0
)
logger.info("llm.cache_hit",
    provider="deepseek",
    prompt_tokens=response.usage.prompt_tokens,
    cached_tokens=response.usage.prompt_cache_hit_tokens,
    cache_hit_ratio=cache_hit_ratio,
    spot_id=spot_id,
    country=country,
)
```

Adicionalmente: tabla agregada `llm_call_metrics` (o columna en logs estructurados) para query post-batch:

```sql
CREATE TABLE llm_call_metrics (
  ts TIMESTAMPTZ DEFAULT NOW(),
  provider TEXT,
  model TEXT,
  spot_id BIGINT,
  country TEXT,
  prompt_tokens INT,
  cached_tokens INT,
  completion_tokens INT,
  latency_ms INT
);
CREATE INDEX idx_llm_metrics_country_ts ON llm_call_metrics (country, ts);
```

**Criterio de aceptación:** tras smoke Andorra, query `SELECT AVG(cached_tokens::float / prompt_tokens) FROM llm_call_metrics WHERE country='AD'` retorna >0.65.

---

## Tier 1.5 — Smoke test Andorra (1 día, ~$0.10 API)

### Comando

```bash
docker-compose exec enrichment python -m enrichment.worker \
  --batch-size 500 --country AD

docker-compose exec enrichment python -m enrichment.orchestrator_v2 \
  --country AD --limit 200
```

### Acceptance criteria (todos deben pasar)

| Métrica | Umbral |
|---|---|
| Hard invariants de regression suite | 100% pasan |
| Statistical bands de regression suite | ≥85% en rango |
| Cache hit rate del user prompt (logs DeepSeek vía T1.8) | >65% |
| Parse failure rate | <2% |
| Claims persistidos con `review_id IS NULL` y no de scraped_facts | 0 |
| Tags persistidos fuera de `canonical_tags` | 0 |
| Caso Grau Roig: fila en `spot_alerts` con `alert_type='construction'`, `valid_from ∈ 2025-06`, `resolved=FALSE`; `active_alert_types=['construction']`; summary sin invertir cronología | Pasa |
| Caso Andorra Campers: `spot_function='shop_workshop'`, `is_overnight_viable=false` | Pasa |
| Caso Grau Roig: `spot_geo.elevation_m ≈ 2110`, `authorization_status='sign_authorized'` | Pasa |
| Reanudación tras kill -9: spots reprocesados | 0 |

Si **cualquiera** falla → iterar antes de seguir. No avanzar a Portugal sin smoke verde.

### Si parse failure >2%

Integrar `json-repair` o equivalente en `llm_provider.call_llm_sync` antes de continuar.

---

## Tier 2 — Post-batch (no bloqueante para `--all`)

Solo después de que Andorra → Portugal → España hayan corrido limpios.

### T2.1 — Léxico multilingüe ponderado

**Archivo:** `enrichment/multilingual_lexicon.py` (nuevo) + integración en `claim_extractor.py`.

5 señales × 6 idiomas ≈ 150 entradas. Combinación: `final_severity = 0.3 * lexical_prior + 0.7 * llm_score`.

### T2.2 — Estados intermedios del lifecycle

Añadir `decaying` entre `active` y `likely_resolved`. Ranking pondera distinto cada estado.

### T2.3 — Half-life por señal en signal_registry

Más granular que stable/volatile categórico. Cada señal tiene su propio half-life en días para el decay exponencial del agregador.

```python
# enrichment/signal_registry.py
SIGNAL_HALF_LIFE_DAYS = {
    # Volátiles (la realidad cambia en meses)
    'construction':     90,    # obras: 3 meses
    'police_risk':      180,
    'temporary_ban':    120,
    'crowd_level':      240,
    'noise':            365,
    'cleanliness':      365,
    'price':            365,

    # Semi-persistentes
    'agua_potable':     1095,  # 3 años
    'electricidad':     1095,
    'wifi':             730,
    'overnight_safe':   730,

    # Persistentes (geofísicas)
    'beauty':           3650,  # 10 años
    'elevation':        None,  # sin decay
    'terrain':          None,
    'sea_view':         None,
}
```

Fórmula de agregación:
```
w_final(obs) = source_confidence
             * extraction_confidence
             * 2^(-Δt_dias / half_life)
             * recency_boost(Δt)

recency_boost(Δt) = 1 + α · exp(-Δt / 60)   # α=0.5, ventana 60 días
```

Reprocesamiento condicionado: el cron solo recomputa `spot_semantic_state` si hay observaciones nuevas con `signal_type` cuyo half-life sea menor que el tiempo desde el último agregado. No tiene sentido reprocesar `beauty` cada semana.

### T2.4 — Job mensual de revisión de unknown_tags

`jobs/review_unknown_tags.py` → output markdown para revisión humana, promoción manual al registry.

### T2.5 — Detección de cambio de régimen con guardas

Para detectar contradicciones temporales reales (Grau Roig 2025 obras vs 2026 tranquilo) sin generar falsos positivos en spots con poca actividad.

```python
def detect_regime_change(observations, signal_type):
    recent = [o for o in observations if days_ago(o.observed_at) <= 180]
    historical = [o for o in observations if days_ago(o.observed_at) > 180]

    # Guardas: necesarias para evitar ruido en n bajo
    if len(recent) < 3 or len(historical) < 3:
        return None
    # Guardas: separación temporal mínima entre clusters
    if (min(historical_dates) - max(recent_dates)).days < 90:
        return None

    recent_mean = weighted_mean(recent)
    hist_mean = weighted_mean(historical)

    if abs(recent_mean - hist_mean) > 0.4:
        return {
            'signal_type': signal_type,
            'old': hist_mean,
            'new': recent_mean,
            'since': min(o.observed_at for o in recent),
            'n_recent': len(recent),
        }
    return None
```

Resultado se materializa en `spot_semantic_state.signal_flux` (columna ya creada en T1.4c).

### T2.6 — `spot_relations` (relaciones spot↔spot)

```sql
CREATE TABLE spot_relations (
  spot_id BIGINT REFERENCES spots(id) ON DELETE CASCADE,
  related_spot_id BIGINT REFERENCES spots(id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,
    -- 'alternative_overnight' | 'service_provider_for'
    -- | 'parking_for_visit' | 'same_complex' | 'walking_distance'
  distance_m INT,
  bidirectional BOOLEAN DEFAULT FALSE,
  confidence NUMERIC(3,2),
  source TEXT,  -- 'llm_review_inference' | 'manual' | 'geo_proximity'
  PRIMARY KEY (spot_id, related_spot_id, relation_type)
);
```

LLM emite `cross_references[]` en su output cuando una review menciona otro lugar ("River shopping center nearby", "parking del telesilla"). Postprocesado resuelve a `spot_id` vía búsqueda geográfica + similitud de nombre antes de insertar.

### T2.7 — Flag `stale` para re-agregación selectiva

```sql
ALTER TABLE spot_semantic_state ADD COLUMN stale BOOLEAN DEFAULT FALSE;
ALTER TABLE spot_semantic_state ADD COLUMN last_aggregated_at TIMESTAMPTZ;
CREATE INDEX idx_state_stale ON spot_semantic_state (stale) WHERE stale = TRUE;
```

Cuando una observación nueva con `signal_type` volátil entra con `observed_at > last_aggregated_at` del spot → marcar `stale=TRUE`. Cron nocturno recomputa solo `WHERE stale=TRUE`. A 125K spots con ~2% diario tocados, esto reduce el coste del aggregator nightly de 125K a ~2.5K spots por ejecución.

---

## Tier 3 — Solo si los datos lo justifican (3-6 meses)

- **Evidence vectors** (T3.1): solo si soft review detecta colapso semántico entre señales correlacionadas.
- **Recalibración empírica de parámetros de decay** (T3.2): con 6 meses de datos reales, ajustar 0.85 y umbral 0.3.
- **Sistema reactivo de invalidación** (T3.3): solo si la escala supera ~1M spots o SLA <1min.

**No empezar T3 sin métricas que lo justifiquen.** Es exactamente el tipo de trabajo que genera deuda si se hace por anticipación.

---

## Lo que se descartó explícitamente (y por qué)

| Propuesta | Origen | Por qué se descarta |
|---|---|---|
| Aplanar `terrain[]` a columnas booleanas en `spots` | Claude B4 inicial | Acoplamiento rígido a taxonomía de fuente. Mantener JSONB + GIN + ingesta vía `scraped_facts_v1`. Atributos geofísicos van a `spot_geo` (D8) |
| `elevation_m`/`terrain_surface`/`slope_grade` directamente en `spots` | Auditoría 4 | Ensucia tabla canónica. Va a `spot_geo` (D8, T1.4b) |
| stddev sin guardas para `state_flux` | Claude B5 inicial | n<5 en muchos spots → falsos positivos. **Reformulado con guardas en T2.5** |
| Summary estructurado en 4 sub-campos | Arquitecto | Empeora coste, fuerza relleno alucinado, peor para embeddings |
| TTL automático puro en operational_state | Claude inicial | Sesgo de reporting asimétrico: silencio ≠ resolución. Sustituido por decay 0.85^n + guarda 180d (D1+D2) |
| Outbox pattern / Event Sourcing para invalidación | Gemini | Overkill a vuestra escala. Cron nocturno con fingerprint basta (D4) |
| Bayesian state machine formal | Gemini | Es decay con threshold, mismo cálculo con nombre más caro |
| Refactor a "Semantic Event Engine" como capa nueva | Arquitecto | Ya existe `semantic_events` + `event_detector.py` en el repo |
| Evidence vectors ahora | Arquitecto | Tier 3, no antes. Resolver problema real antes de generalizar |
| `spot_operational_state` simple | Plan v1 | Sustituido por `spot_alerts` (más rico, multi-tipo, multi-fila por spot) |
| `state_version` entero | Plan v1 | Sustituido por `semantic_fingerprint` (hash sobre input real del embedder) |

---

## Sprints

### Sprint 0 — Preparación (2 días)
- T0.1 Lectura código → `docs/fase-3-hardening-codebase-notes.md`
- T0.2 Regression suite v1 (20-30 casos, 3 niveles)
- T0.3 Sección "Inmutables vs regenerables" en CLAUDE.md

### Sprint 1 — Prompt + redundancia (1.5 días)
- T1.1 Prompt tail-loading + few-shot estables
- T1.2 STATIC_CONTEXT vs REVIEW_EVIDENCE
- T1.3 CURRENT_DATE + age

### Sprint 2 — Estado operacional + clasificación funcional (2 días)
- T1.4 `spot_alerts` + resolver determinista
- T1.4b `spot_function` + `is_overnight_viable` + `authorization_status` + `spot_geo` (D8)
- T1.4c `signal_flux` + `active_alert_types` en `spot_semantic_state`
- T1.5 Canonicalizador + unknown_tags

### Sprint 3 — Invalidación + idempotencia + observabilidad (1 día)
- T1.6 semantic_fingerprint + nightly_embeddings ampliado
- T1.7 Verificar idempotencia worker
- T1.8 Logging de cache hit ratio + tabla `llm_call_metrics`

### Sprint 4 — Smoke Andorra (0.5 día)
- Ejecutar smoke con todos los acceptance criteria

### Sprint 5+ — Roll-out país a país
- Portugal → España → Francia → Alemania → Italia → UK → US → resto.

### Backlog Tier 2 (sin fecha hasta que el batch corra limpio)
- T2.1 Léxico multilingüe ponderado (D5/D6)
- T2.2 Estados intermedios del lifecycle (`decaying`)
- T2.3 Half-life por señal en signal_registry
- T2.4 Job mensual unknown_tags
- T2.5 Detección de cambio de régimen con guardas
- T2.6 `spot_relations`
- T2.7 Flag `stale` para re-agregación selectiva

---

## Disciplina para no descarrilar

Al implementar este plan:

1. **No empezar Tier 1 sin Tier 0 completo.** Implementar contra una descripción del código en vez de contra el código es exactamente cómo se hacen refactors equivocados.
2. **No saltar acceptance criteria.** Si un umbral del smoke no pasa, iterar antes de seguir. La tentación de "es solo un 1.5% por encima del umbral" es cómo se contamina producción.
3. **No añadir Tier 2/3 al alcance pre-batch.** Cada idea nueva que entre antes del batch retrasa el momento de tener datos reales.
4. **Si alguien (incluido yo) propone algo durante la implementación que no encaja en 10 minutos de código, dudar.** Anotar en backlog, no integrar.
5. **Regression suite es ley.** Cambios al prompt o schema sin pasar la suite no se mergean.

---

## Fuera de scope (otro documento, otro momento)

Estas cosas aparecieron en auditorías paralelas pero **no son parte de este plan** y no deben mezclarse:

- **Enriquecimiento de metadatos web** (teléfono, URL, contacto vía Google Places, OSM, SearXNG, Brave API, etc.). Endurecimiento de reglas de matching, cadena de fallbacks, circuit breaker. Pertenece a `docs/fase-X-web-enrichment.md` cuando se aborde.
- **Reorganización de las fuentes de scraping** (Phase 1). Fuera de scope.
- **Visual intelligence** (Phase 5) y **geo intelligence completa** (Phase 6). Solo se toca `spot_geo` parcialmente en T1.4b (elevation/terrain/slope) por oportunismo del LLM; el resto de Phase 6 (DEM, OSM analysis) sigue siendo fase futura.

---

## Referencias

- Auditoría completa: conversación de revisión 2026-05-28 con tres revisores (Claude Opus, Gemini, Arquitecto externo) + auditoría 4 con casos Andorra Campers y Grau Roig.
- Caso de estudio: Spot 85057 (Grau Roig, Andorra), 3 reviews multilingües con contradicción temporal obras vs tranquilidad.
- Pipeline objetivo: `enrichment/orchestrator_v2.py` (spot-level v4) sobre ~239K spots con ≥3 reviews.
- Provider: DeepSeek V4 Flash, coste estimado tras optimizaciones ~$400 total (input + output).
