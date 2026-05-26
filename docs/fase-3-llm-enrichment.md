# Fase 3 — LLM Enrichment

> **Estado:** ✅ v1 en producción (regex-first, review-level) · 🔧 v2 en migración (LLM-first, spot-level, Batch API)
> **Última actualización:** 2026-05-26

---

## Idea central

Un spot con 40 reviews de 4 fuentes tiene un tesoro de información implícita que ningún campo booleano captura:

- "muy tranquilo de noche" → `quietness: 0.9`
- "vino la policía a las 3am" → `police_risk: 0.85`
- "ideal para furgos pequeñas, difícil con autocaravana grande" → `large_vehicle: 0.2`
- "vistas increíbles al mar" → `beauty: 0.95`, `sea_view: true`
- "ruido de autopista constante" → `road_noise: 0.8`, `noise_sources: ["highway"]`

**El LLM NO responde al usuario en tiempo real.** El LLM **pre-procesa offline** y genera datos estructurados. El chat y la PWA consultan solo datos pre-computados → rápido, barato, predecible.

---

## Arquitectura del pipeline (v2 — objetivo)

```
spot con ≥3 reviews (o descripción rica)
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ 1. SELECCIÓN + EMPAQUETADO (1 prompt por spot)          │
│  - top N reviews por peso temporal (decay)              │
│  - ≤ 3.500 tokens útiles (reviews + descripciones)      │
│  - cada review etiquetada con review_id + fecha + source│
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 2. LLM CALL (Gemini Flash 2.0, Batch API, ctx cache)    │
│  - system prompt cacheado (~1k tokens, reglas+señales)  │
│  - user prompt = bloque del spot                        │
│  - response: JSON con claims[] + summary + tags + flags │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ 3. PERSISTENCIA GRANULAR                                │
│  - cada claim → extracted_claims (con review_id origen) │
│  - claim → normalized_observation (con peso + decay)    │
│  - re-cálculo spot_semantic_state (agregación)          │
│  - summary/tags/best_for → spot_semantic_state          │
│  - detección de shift → spot_semantic_snapshots         │
└─────────────────────────────────────────────────────────┘
```

### Diferencia clave vs. v1

| Aspecto | v1 (actual) | v2 (objetivo) |
|---|---|---|
| Unidad LLM | 1 review = 1 call | 1 spot = 1 call (con N reviews dentro) |
| Estrategia | Regex primero, Gemini solo si regex no encuentra nada | Gemini siempre (≥3 reviews), regex como boost opcional |
| Salida | claims sueltos por review | claims + `summary_es/en` + `tags` + `best_for` + `noise_sources` + flags |
| Procesamiento | Síncrono review-a-review | Batch API nocturna |
| Coste de system prompt | Pagado N veces | Cacheado (context caching) |
| Coste estimado 80K spots | ~$100-150 (estimado mal) | **~$15-20** (real con descuentos) |

---

## Capa de datos

### Tablas Phase 3 vigentes

| Tabla | Rol |
|---|---|
| `signal_types` | Catálogo de señales (id, value_type, decay, importance_weight, parent_id) |
| `extracted_claims` | Una fila por afirmación extraída de una review. Inmutable. Lleva `extractor_name` + `extractor_version` + `pipeline_run_id`. |
| `normalized_observations` | Claim convertido en valor numérico/bool/text con peso (`extraction_confidence × source_confidence × reviewer_confidence`). |
| `spot_semantic_state` | Estado agregado por spot. Una fila por spot. Decay aplicado en cada recompute. |
| `spot_semantic_snapshots` | Historial cuando el estado cambia significativamente (semantic_distance > 0.15). |
| `semantic_events` | Eventos puntuales detectados (police_burst, etc.) — Phase 3.5. |

### Campos de `spot_semantic_state`

Ver [db/schema.sql](../db/schema.sql) `:617`. Campos materializados existentes:

```
quietness_score, safety_score, police_risk_score, beauty_score,
crowd_level_score, overnight_safe, stealth_score,
signals_data (JSONB completo), semantic_dsl,
summary_es, summary_en, tags[], best_for[], best_season,
total_observations, consensus_confidence, weight_support,
last_aggregated_at, stale
```

### Campos a añadir en v2 (`db/migration_phase3_v2.sql`)

```sql
ALTER TABLE spot_semantic_state ADD COLUMN IF NOT EXISTS
    enrichment_version    INT       DEFAULT 1,        -- invalidación selectiva por prompt
    llm_model             TEXT,                       -- 'gemini-2.0-flash@batch'
    last_observation_at   TIMESTAMPTZ,                -- fecha de la review más reciente
    freshness_warning     BOOLEAN GENERATED ALWAYS AS
        (last_observation_at IS NOT NULL
         AND last_observation_at < NOW() - INTERVAL '24 months') STORED,
    noise_sources         TEXT[],                     -- ['highway','train','party','sea']
    parking_capacity      TEXT,                       -- 'small'|'medium'|'large'|null
    cell_coverage         REAL,                       -- 0-1
    wild_camping_legal    BOOLEAN,                    -- null si desconocido
    avoid_season          TEXT;

CREATE INDEX IF NOT EXISTS idx_sss_version
    ON spot_semantic_state(enrichment_version);
CREATE INDEX IF NOT EXISTS idx_sss_freshness
    ON spot_semantic_state(freshness_warning) WHERE freshness_warning = TRUE;
```

### Señales nuevas en `signal_types`

```
noise_sources       (text_array, half_life=180d, importance=1.2)
parking_capacity    (text,       half_life=1825d, importance=0.6)
cell_coverage       (numeric,    half_life=365d, importance=0.7)
wild_camping_legal  (boolean,    half_life=730d, importance=2.0)
mosquitoes          (numeric,    half_life=180d, importance=0.5)
dog_friendly        (boolean,    half_life=1825d, importance=0.6)
family_friendly     (boolean,    half_life=1825d, importance=0.6)
accessible_pmr      (boolean,    half_life=1825d, importance=0.6)
water_working       (boolean,    half_life=60d, importance=1.5)
electricity_working (boolean,    half_life=60d, importance=1.5)
dump_station_working(boolean,    half_life=60d, importance=1.5)
```

`signals_data` (JSONB) sigue siendo el contenedor canónico. Las columnas materializadas son cache de query, no fuente de verdad.

---

## Prompt v2

### System prompt (cacheado vía Gemini context caching)

```
Eres un analista experto en spots para autocaravanas y furgonetas camper.
Recibes el contexto de UN spot (datos + descripciones + reviews ordenadas
por relevancia temporal, más recientes primero) y devuelves un JSON
estructurado con afirmaciones explícitas, resumen y tags.

REGLAS:
1. No inventes. Solo afirma lo que el texto soporta.
2. Cada claim debe citar `review_id` de origen (o "description" si viene
   de la descripción de la fuente).
3. Da más peso a reviews recientes. Si reviews antiguas y recientes
   contradicen, prioriza recientes pero menciónalo en el summary.
4. Negación, sarcasmo e ironía cuentan: "no muy tranquilo" ≠ "tranquilo".
5. Los scores son 0.0-1.0; los booleanos solo si hay evidencia clara.
6. Si un campo no tiene soporte, omítelo (no inventes nulls).

SEÑALES PERMITIDAS:
[catálogo completo de signal_types con descripción y rango]

FORMATO DE SALIDA: JSON estricto, sin markdown, sin comentarios.
```

### User prompt (por spot, no cacheado)

```
SPOT id=12345
Nombre: "Aire de Belharra"
Tipo: aire_municipal
País: FR
Coordenadas: 43.39, -1.61
Fuentes: park4night, campercontact, areasac

DESCRIPCIONES:
[ES] Aparcamiento gratuito frente al mar...
[FR] Parking gratuit face à la mer...

REVIEWS (n=14, ordenadas por peso temporal):
[review_id=98012] [2025-09] [park4night] [★★★★] "Muy tranquilo, vistas increíbles..."
[review_id=98014] [2025-08] [campercontact] [★★★] "Lleno en agosto, ruido de surfistas..."
[review_id=97002] [2024-06] [park4night] [★★★★★] "Sombra por la tarde, ideal..."
...
```

### Salida esperada

```json
{
  "claims": [
    {"signal":"quietness","value":0.7,"confidence":0.85,"review_id":98012,"excerpt":"muy tranquilo"},
    {"signal":"crowd_level","value":0.9,"confidence":0.9,"review_id":98014,"excerpt":"lleno en agosto"},
    {"signal":"sea_view","value":true,"confidence":0.95,"review_id":"description","excerpt":"face à la mer"},
    {"signal":"noise_sources","value":["surf","crowd"],"confidence":0.7,"review_id":98014,"excerpt":"ruido de surfistas"}
  ],
  "summary_es": "Aire gratuita frente al mar con vistas y sombra por la tarde. Muy concurrida en verano.",
  "summary_en": "Free seafront aire with sea views and afternoon shade. Crowded in summer.",
  "tags": ["mar","gratuito","sombra","surf"],
  "best_for": ["parejas","surferos","estancias cortas"],
  "best_season": "primavera-otoño",
  "avoid_season": "agosto",
  "noise_sources": ["surf","crowd"],
  "parking_capacity": "medium",
  "wild_camping_legal": true
}
```

Una salida → muchas filas en `extracted_claims`, una update en `spot_semantic_state`.

---

## Selección de reviews para el prompt

```python
def seleccionar_reviews(reviews: list[dict], max_tokens: int = 3500) -> list[dict]:
    """Top reviews por peso temporal, hasta llenar el presupuesto de tokens."""
    reviews.sort(key=lambda r: (
        peso_temporal(r["fecha"]),     # decay
        r.get("rating") or 0,          # rating como desempate
        len(r.get("texto") or ""),     # textos más largos = más info
    ), reverse=True)

    seleccionadas = []
    tokens = 0
    for r in reviews:
        chunk_tokens = estimate_tokens(r["texto"])
        if tokens + chunk_tokens > max_tokens:
            break
        seleccionadas.append(r)
        tokens += chunk_tokens
    return seleccionadas

def peso_temporal(fecha) -> float:
    if not fecha: return 0.3
    dias = (datetime.now(timezone.utc) - fecha).days
    if dias < 365:    return 1.0
    if dias < 730:    return 0.8
    if dias < 1095:   return 0.5
    if dias < 1825:   return 0.3
    return 0.1
```

---

## Procesamiento Batch (Gemini Batch API)

```python
# 1. Generar batch de jobs
async def build_batch(pool, version: int, limit: int = 5000):
    spots = await fetch_spots_to_enrich(pool, version=version, limit=limit)
    jobs = []
    for spot in spots:
        reviews = await fetch_top_reviews_for_spot(pool, spot["id"], max_tokens=3500)
        if len(reviews) < 3 and not has_rich_description(spot):
            continue
        jobs.append({
            "key": f"spot_{spot['id']}_v{version}",
            "request": {
                "system_instruction": {"cached_content": SYSTEM_CACHE_ID},
                "contents": [{"role":"user", "parts":[{"text": build_user_prompt(spot, reviews)}]}],
                "generation_config": {"response_mime_type":"application/json", "temperature":0.2},
            }
        })
    return jobs

# 2. Enviar a Batch API (50% descuento, latencia 24h)
batch = client.batches.create(model="gemini-2.0-flash", requests=jobs)

# 3. Polling + ingesta
while batch.state not in ("SUCCEEDED","FAILED"):
    await asyncio.sleep(300)
    batch = client.batches.get(batch.name)

for result in batch.results():
    spot_id = parse_key(result.key)
    payload = json.loads(result.response.text)
    await ingest_enrichment(pool, spot_id, payload, version=version)
```

`ingest_enrichment` hace, en una transacción:
1. INSERT claims en `extracted_claims` (con `review_id` referenciado).
2. INSERT observaciones en `normalized_observations`.
3. `recompute_spot_state(spot_id)` → recalcula `spot_semantic_state` desde cero.
4. Actualiza `summary_es/en`, `tags`, `best_for`, `noise_sources`, `parking_capacity`, `cell_coverage`, `wild_camping_legal`, `best_season`, `avoid_season`, `enrichment_version`, `llm_model`, `last_observation_at`.

---

## Context caching

Gemini permite cachear contenido reutilizable con TTL. Lo usamos para el system prompt + catálogo de señales (~1.000 tokens) que es idéntico en todas las llamadas.

```python
system_cache = client.caches.create(
    model="gemini-2.0-flash",
    contents=[{"role":"user", "parts":[{"text": SYSTEM_PROMPT}]}],
    ttl="3600s",
)
SYSTEM_CACHE_ID = system_cache.name  # reutilizar en cada request del batch
```

Ahorro: ~75% del coste de input sobre la porción cacheada.

---

## Economía (recalculada 2026-05)

Asunciones:
- 125K spots activos, ~80K con ≥3 reviews
- Tokens medios por spot: 2.500 input (700 system + 1.800 user) + 500 output
- Gemini Flash 2.0 público: $0.10/MT in, $0.40/MT out
- Batch API: 50% descuento
- Context cache: 50% descuento sobre los 700 tokens cacheados (vale 25% del input)

| Concepto | Cálculo | Coste |
|---|---|---|
| Input no cacheado (1.800 × 80K) | 144M × $0.10 × 0.5 | $7.2 |
| Input cacheado (700 × 80K) | 56M × $0.025 × 0.5 | $0.7 |
| Output (500 × 80K) | 40M × $0.40 × 0.5 | $8.0 |
| **TOTAL pasada inicial** | | **~$16** |
| Incremental mensual (~5%) | | **~$1/mes** |

---

## Triggers de re-enriquecimiento

Un spot se re-enriquece cuando:

1. **enrichment_version desactualizada** (cambio de prompt o catálogo de señales) → `spot_semantic_state.enrichment_version < CURRENT_VERSION`
2. **Reviews nuevas significativas** → `COUNT(reviews WHERE created_at > spot_semantic_state.last_aggregated_at) >= 5`
3. **Antigüedad** → `spot_semantic_state.last_aggregated_at < NOW() - INTERVAL '18 months'`
4. **Manual** → `UPDATE spot_semantic_state SET stale=TRUE WHERE spot_id=$1`

Job nocturno (`jobs/nightly_enrichment.py`) elige hasta 5.000 spots/noche por estos criterios, los manda a Batch API, ingesta resultados.

---

## Cómo cambia el chat

### Antes
```
Usuario: "sitios tranquilos cerca de León"
→ SQL: SELECT * FROM spots WHERE ST_DWithin(...) ORDER BY rating DESC
→ Gemini recibe campos crudos y adivina
```

### Después
```
Usuario: "sitios tranquilos cerca de León"
→ /search/semantic:
   1. Gemini extrae intención: {filter: quietness>0.7, near: 'León', radius: 50km}
   2. SQL: SELECT s.*, sss.* FROM spots s
           JOIN spot_semantic_state sss USING (spot_id)
           WHERE ST_DWithin(...) AND quietness_score > 0.7
             AND NOT freshness_warning
           ORDER BY quietness_score DESC
   3. pgvector ranking sobre embeddings
   4. Gemini compone respuesta con summary_es ya pre-computado
→ Respuesta < 2s, sin LLM-loop caro
```

---

## Métricas de éxito

| Métrica | Objetivo |
|---|---|
| Spots enriched (versión vigente) | ≥ 80% de los que cumplen criterios |
| Coste pasada inicial | < $30 |
| Coste mensual incremental | < $5 |
| Spots con summary_es/en | ≥ 95% de los enriched |
| Precisión scores (validación manual N=100) | ≥ 90% |
| Latencia chat con enrichment | < 2s p95 |
| % spots con freshness_warning | tracked; UI debe avisar |

---

## Lo que NO Hacer

1. **No mezclar v1 (review-level) y v2 (spot-level)**: durante la migración, marcar runs con `pipeline_run_id` + `extractor_name` para poder revertir.
2. **No invalidar `extracted_claims` antiguos** al subir version: son inmutables; basta con re-agregar `spot_semantic_state` con la nueva lógica.
3. **No usar Batch API para urgencias**: latencia hasta 24h. Para re-enriquecer un spot puntual (admin, debug) usar API síncrona.
4. **No cachear el user prompt**: cambia por spot y por revisión → no se beneficia y complica facturación.
5. **No borrar `last_snapshot_data`**: es la baseline para detectar `semantic_distance`.

---

## Referencias

- Implementación v1: [enrichment/worker.py](../enrichment/worker.py), [claim_extractor.py](../enrichment/claim_extractor.py), [state_aggregator.py](../enrichment/state_aggregator.py)
- Plan de migración v1 → v2: [docs/fase-3-migracion-v2.md](fase-3-migracion-v2.md)
- Schema: [db/schema.sql:534-674](../db/schema.sql)
- Embeddings (consumen este estado): [docs/fase-4-vector-search.md](fase-4-vector-search.md)
