# Phase 3 Hardening — Notas de código real (T0.1)

**Propósito.** Antes de tocar nada del plan `fase-3-hardening-pre-batch.md`, mapear lo que ya existe en el repo contra lo que el plan dice construir. Sirve para no refactorizar algo que ya está, y para no asumir interfaces que el código no provee.

**Alcance leído.** `enrichment/prompts.py`, `spot_packager.py`, `state_aggregator.py`, `signal_registry.py`, `event_detector.py`, `worker.py`, `orchestrator_v2.py`, `llm_provider.py`, `claim_extractor.py` (parcial), `ingest_v2.py`, `embedding_generator.py` (parcial), `jobs/nightly_embeddings.py`, `db/schema.sql` (búsqueda dirigida).

---

## 1. Resumen por tarea del plan

| Tarea | Estado actual | Acción real necesaria |
|---|---|---|
| **T1.1** Prompt tail-loading + few-shot | ✅ **HECHO** (2026-05-28). `FEW_SHOT_EXAMPLES_V4` + `PROMPT_VERSION = "v4-fewshot-1"` añadidos a `enrichment/prompts.py`. Marcadores `=== SPOT DATA ===` / `=== END SPOT DATA ===` + directriz anti-recency-bias al final del user prompt. `max_tokens` 1500 → 2500 en `llm_provider.py`. `ENRICHMENT_VERSION` queda en 4 hasta T1.2 (que cambia el schema). sha256[:16] del system prompt = `6d5592d3a56fffa8`, ~15975 bytes, verificado byte-estable. | — |
| **T1.2** STATIC_CONTEXT vs REVIEW_EVIDENCE | ✅ **HECHO** (2026-05-28). `prompts.py`: SERVICES wrap en `<STATIC_CONTEXT readonly="true">`, REVIEWS wrap en `<REVIEW_EVIDENCE>`. Schema output: `claims[]` → `review_claims[]` + `contradicted_static_facts[]`. Reglas 13/15/16/17 reescritas (NO re-emisión de SERVICES). Few-shots reescritos. `ENRICHMENT_VERSION` 4→5, `PROMPT_VERSION` → "v5-static-review-split-1". `gemini_response_parser`: parsea v5 + rechaza review_claims/contradicted con `review_id NULL`. Legacy v4 schema sigue parseando como fallback. | — |
| **T1.3** CURRENT_DATE + age relativo | ✅ **HECHO** (2026-05-28). `build_spot_user_prompt` inyecta `CURRENT_DATE: YYYY-MM-DD` (UTC) en SPOT DATA. Cada review lleva `[age: Xd ago]` (o `[age: ?]` si sin fecha). Helper `_age_days` en `prompts.py`. Regla 3 del SYSTEM_PROMPT documenta el prefijo. `PROMPT_VERSION` → `v5-current-date-1`. | — |
| **T1.4** `spot_alerts` con lifecycle | ✅ **HECHO** (2026-05-28). Migración v6 aplicada. `state_resolver.py` nuevo con upsert idempotente + decay (0.85^meses + guarda 180d). Parser + prompt + ingest + cron job `jobs/nightly_alert_decay.py`. | — |
| **T1.4b** `spot_function`/`is_overnight_viable`/`authorization_status` | ✅ **HECHO** (2026-05-28). Migración v6 añade 3 columnas a `spots` + `source` a `spot_geo`. Parser valida vocabs. Prompt regla "OMIT si no hay evidencia". Ingest usa COALESCE; spot_geo respeta jerarquía source (DEM/OSM/manual > llm_v6). | — |
| **T1.4c** `signal_flux` + `active_alert_types` en `spot_semantic_state` | ✅ **HECHO** (2026-05-28). Migración v6 añade ambas columnas + GIN. Helper SQL `refresh_active_alert_types(spot_id)`. `signal_flux` reservado para T2.5 (empty dict). UPSERTs de `state_aggregator` no tocan estas columnas (preserva valor). | — |
| **T1.5** Canonicalizador de tags + unknown_tags | ✅ **HECHO** (2026-05-28). Migración v6b aplicada (separada de v6 para rollback independiente) con seed de 73 canonicals + 325 entradas multilingües. `tag_canonicalizer.py` con índice cacheado in-memory + UPSERT idempotente. `ingest_v2` filtra `parsed.tags` antes del UPDATE. CLI `jobs/review_unknown_tags.py` para promoción/dismiss mensual. | — |
| **T1.6** `semantic_fingerprint` + invalidación | ✅ **HECHO** (2026-05-28). Migración v6c añade columnas. `compute_fingerprint(state_row)` en `embedding_generator.py` (SHA1[:16] estable, no incluye scores continuos). `fetch_embedding_candidates` ahora compara fingerprints. `ingest_v2` calcula y persiste el fingerprint en cada UPDATE. | — |
| **T1.7** Idempotencia del worker | ✅ **HECHO** (2026-05-28). `force_spot_ids` añadido a `select_candidates` y `run_enrichment`. Flag CLI `--force-spot-ids` en `jobs/nightly_enrichment_v2.py`. | — |
| **T1.8** Cache hit rate logging | ✅ **HECHO** (2026-05-28). Migración v6c crea `llm_call_metrics`. Log per-call en `call_deepseek_sync`. `_record_llm_metric` invocado tras cada respuesta exitosa con latency_ms medido. | — |
| **T0.2** Regression suite v1 | ✅ **HECHO** (2026-05-28). `tests/regression/semantic_suite.py` (20 casos, 3 tiers). Baseline Grau Roig 85057: 1 hard fail `chronology_ok`, 1 band warn `quietness=0.900`, 3 skips (deps T1.4). Snapshot en `tests/regression/snapshots/grau_roig_obras.json`. | — |
| **T0.3** Inmutables vs regenerables en CLAUDE.md | ✅ **HECHO** (2026-05-28). Sección añadida. | — |
| **T2.7** flag `stale` | ✅ **HECHO** (2026-05-28). Migración v6c añade `mark_spot_stale_on_new_obs()` + `trg_stale_on_observation`. Smoke OK. | — |
| **Pre-Sprint 4** `--country` worker.py | ✅ **HECHO** (2026-05-28). `fetch_pending_reviews(countries=...)` JOIN a `spots.country_iso`. CLI `--country AD` o `--country ES,PT`. Bloqueante del smoke Andorra. | — |

---

## 2. Hallazgos no anticipados en el plan

1. **`worker.py` no tiene flag `--country`.** El plan dice `python -m enrichment.worker --batch-size 500 --country AD` pero `fetch_pending_reviews` (líneas 130-153) **no acepta filtro por país** — ordena por `spot_temperature.temperature` y `r.fecha`. Para el smoke de Andorra hay que **añadir flag `--country` al CLI del worker** y JOIN a `spots` por `country_iso`. Trabajo extra, no en el plan.

2. **`spot_geo` ya existe con esquema rico.** El plan asume crear columnas `elevation_m`, `terrain_surface`, `slope_grade`. La tabla real (schema.sql 379-394) tiene `elevation_m` (✓), `slope_degrees` (numérico, no categorical "grade"), `terrain_type` (no `terrain_surface`). Decisión recomendada: el LLM emite `terrain_type` (texto libre tipo "grass/gravel/asphalt") y `slope_degrees` (número entero estimado). Actualizar T1.4b y D8 en el plan para reflejar nombres reales.

3. **`enrichment_version` lo controla `prompts.py` como constante** (`ENRICHMENT_VERSION = 4`). Cualquier cambio al system prompt o al schema debe bumpear esto. Documentar en T1.1.

4. **`event_detector.py` solo detecta `police_burst`/`theft_spree`** sobre `extracted_claims` de los últimos 7 días (líneas 12-26). El plan menciona "ya existe `semantic_events` + `event_detector.py`" — confirmado, pero el ámbito es estrecho. Mantener T2.5 (regime change) como módulo nuevo, no extender event_detector.

5. **`state_aggregator.update_semantic_state`** (línea 240) reagrega incrementalmente con cada nueva observación, pero **no** marca `stale=FALSE` — lo hace solo `recompute_spot_state`. Verificar que el path incremental también sea consistente, o forzar siempre `recompute_spot_state`. Quizá T1.7 deba simplificarse y eliminar la rama incremental.

6. **`embedding_generator.construir_texto_para_embedding`** usa `summary_es` primero y cae a `summary_en` (línea 138-141). Esto está **incoherente con v4** que escribe solo `summary_en` (`ingest_v2` línea 160 pone `summary_es=None`). El embedding actual probablemente está usando `summary_en` ya, pero la rama mezcla idiomas en el texto (`"en {region}, ES"`, `"Ideal para: ..."` en español). **No es bloqueante para el batch**, pero el embedding multilingüe se degrada. Anotar para Tier 2 — no entra en hardening pre-batch.

7. **`call_deepseek_sync` con `max_tokens=1500`** (línea 90) puede truncar outputs ricos del v4 (summaries largos `very_rich` + arrays). El parser fallará silenciosamente con JSON inválido. Subir a 2500-3000 y monitorear via `stats.tokens_output_total`. Pequeño riesgo de coste pero protege parse rate.

8. **`spot_packager.fetch_reviews_for_enrichment`** limita a `hard_limit=60` reviews por spot. El plan habla de "top-35 reviews" en el resumen original. Con dedup conservador + selección por peso, suele bajar a 10-15 en el prompt. OK como está. Sin cambio.

9. **`orchestrator_v2` ya hace circuit breaker** (línea 213-216: pausa 30s si 5 errores consecutivos). El plan no lo menciona como existente. Una preocupación menos.

10. **No hay flag para forzar reprocesado de spots ya enriched.** El filtro `enrichment_version < $1` los excluye. Si tras un bug en prompt v4 queremos re-enrichar Andorra, hay que bumpear a v5 o nullear `enrichment_version` manualmente. Útil tener un `--force-spot-ids` para el smoke.

---

## 3. Orden de implementación recomendado tras T0

Sigue el plan original pero con estos ajustes:

- **Sprint 1 (T1.1-T1.3)**: ✅ T1.1 completado. `max_tokens` ya subido a 2500. Siguiente: T1.2.
- **Sprint 2 (T1.4-T1.5)**: incorporar columnas a `spots` (function/viable/auth) en la **misma** migración v6 que `spot_alerts`, `canonical_tags`, `unknown_tags`, `llm_call_metrics`, `semantic_fingerprint` y los ALTERs de `spot_semantic_state`. **Una sola migración** para que el rollback sea limpio. Renombrar T1.4b `terrain_surface→terrain_type`, `slope_grade→slope_degrees`.
- **Sprint 3 (T1.6-T1.8)**: T1.7 se simplifica a "verificar y añadir test", no requiere código nuevo. T1.8 se beneficia de tener migración v6 ya aplicada (tabla `llm_call_metrics`).
- **Pre-Sprint 4**: añadir flag `--country` y `--force-spot-ids` al worker. No estaba en el plan pero es bloqueante para el smoke de Andorra.

---

## 4. Lo que el plan asume y el código confirma (sin acción)

- `call_llm_sync` como única puerta de salida LLM: confirmado, no hay imports directos del SDK fuera de `llm_provider.py` y `gemini_cache.py`.
- `enrichment_version`/`llm_model` persistidos en `spot_semantic_state`: confirmado (`ingest_v2` línea 153-154).
- Pipeline incremental review-level (worker.py) vs spot-level (orchestrator_v2.py) son ortogonales: confirmado. El plan ya documenta que B reemplaza/complementa a A.
- Throttling de DeepSeek configurable por env (`ENRICHMENT_CONCURRENCY`, etc.): confirmado en worker.py. orchestrator_v2 usa parámetro `concurrency` directo (no env) — divergencia menor pero no bloqueante.

---

## 5. Riesgos detectados

| Riesgo | Severidad | Mitigación |
|---|---|---|
| `max_tokens=1500` de DeepSeek trunca outputs ricos | Media | Subir a 2500-3000 en Sprint 1 |
| `worker.py` sin `--country` retrasa el smoke | Alta para smoke | Añadir flag antes de Sprint 4 |
| Schema divergencia `terrain_surface` (plan) vs `terrain_type` (DB) | Baja | Resolver en migración v6 — usar nombres DB |
| `update_semantic_state` incremental no marca `stale` consistentemente | Media | Forzar `recompute_spot_state` o auditar paths |
| `embedding_generator` mezcla idiomas en texto-fuente | Baja, no bloqueante | Tier 2 |

---

**Sprint 4 — Smoke Andorra ✅ HECHO (2026-05-28).**

Resultados:
- `orchestrator_v2`: 102/102 spots OK, 0 failed, 870 claims, $0.06, 68s
- `worker.py`: 198 reviews procesadas (batch 500, exit 137 = SIGKILL contenedor; pipeline funcionó)
- Grau Roig 85057: hard 5/5 OK (chronology_ok ✅, alert_construction_active ✅, active_alert_has_construction ✅, elevation ✅, summary ✅) | 1 band warn (quietness=0.900, pre-existente)
- Regression suite: **hard fails=0, band warnings=1, TODOs=18 (casos pendientes de spot_id)**
- `avg_cache_hit_ratio` AD = 0.702 (>0.65 ✅)
- Claims con `review_id IS NULL AND extractor != scraped_facts_v1` en run v6 = **0** (los 200 legacy son de run v4 anterior)
- Tags fuera de `canonical_tags` en v6 = 0 ✅
- 41 alertas activas en Andorra, 6 tipos distintos

Fixes menores aplicados durante el smoke:
- `chronology_not_inverted()` heurística refinada: excluir caso `"2026" in s` (cronología correcta)
- `≠` en locator_hint reemplazado por "distinto" (UnicodeEncodeError Windows cp1252)

**Próximo paso: Batch real país a país.** Orden: PT → ES → FR → DE → IT → GB → US → resto.

```bash
# Batch Portugal (~3K spots activos con ≥3 reviews)
docker-compose exec enrichment python -m jobs.nightly_enrichment_v2 --country PT --limit 5000 --provider deepseek
docker-compose exec enrichment python -m enrichment.worker --batch-size 10000 --country PT

# Batch España
docker-compose exec enrichment python -m jobs.nightly_enrichment_v2 --country ES --limit 50000 --provider deepseek
docker-compose exec enrichment python -m enrichment.worker --batch-size 50000 --country ES
```

Antes de PT: rellenar `spot_id` en los 18 casos TODO de `tests/regression/semantic_suite.py`
usando los locator hints incluidos en cada caso.

---

## 7. Tier 2 — Progreso post-batch

### T2.1 — Léxico multilingüe ponderado ✅ (2026-05-28)

- **Nuevo:** `enrichment/multilingual_lexicon.py` — funciones puras, sin I/O.
  - 173 entradas, 5 conceptos D5 (`construction`, `closure`, `noise_source`,
    `police_pressure`, `wild_camping`) × 6 idiomas (EN/ES/FR/NL/DE/IT).
  - Mapeo a señales reales: construction+closure → `spot_closed=true`;
    noise_source → `noise=0.8`; police_pressure → `police_risk=0.85`;
    wild_camping → `wild_camping_legal=true/false` (polaridad explícita).
  - Matching acento-insensible (NFKD + strip diacríticos). `bouwput`=0.95,
    `Baustelle`=0.92, `chantier`=0.90.
  - Blend D6: `0.7*llm_score + 0.3*lexical_prior`, recortado a [0,1].
- **Integración** en `claim_extractor.extract_claims` (un solo punto, al final, para
  evitar doble aplicación no idempotente) **y** en el path regex-only de
  `worker._extract_claims_with_retry` (que no pasa por `extract_claims`).
- Solo re-pondera `confidence` de claims existentes; no inventa señales. Anota
  `lexicon_blended=True` para trazabilidad. El insert a `extracted_claims` y
  `normalize_claims` ignoran la clave extra (leen sólo campos concretos vía `.get`).
- **Test:** `tests/test_multilingual_lexicon.py` — 173 entradas, pesos suman 1,
  acento-insensible, polaridad wild_camping, mutación selectiva. Todos pasan.

### T2.2 — Estados intermedios del lifecycle ✅ (2026-05-28)

- El lifecycle pasó de binario (`active`/`likely_resolved`) a **tres estados**,
  con `decaying` entre medias:
  - `active` — no-resuelta, `confidence >= 0.50`. Peso ranking 1.0.
  - `decaying` — no-resuelta, `confidence < 0.50` (incluye conf<0.30 que aún no
    cumple la guarda temporal de 180d). Peso 0.5.
  - `likely_resolved` — `resolved=TRUE`. Peso 0.0.
- **Estado DERIVADO, no columna almacenada** (regenerable desde confidence+resolved,
  nunca desincronizado del decay). Umbral nuevo `DECAYING_CONFIDENCE_THRESHOLD=0.50`
  (> `RESOLVE_CONFIDENCE_THRESHOLD=0.30` para que exista banda intermedia real).
- **Python:** `enrichment/state_resolver.py` — `lifecycle_state()`,
  `lifecycle_rank_weight()`, constantes `LIFECYCLE_*` + `LIFECYCLE_RANK_WEIGHT`.
  `DecayDecision.lifecycle_state` añadido; `decay_all_active`/`nightly_alert_decay`
  cuentan ahora `decaying` en stats y lo loguean.
- **SQL espejo:** `db/migration_phase3_v6d.sql` — `alert_lifecycle_state(conf, resolved)`
  + `alert_rank_weight(state)` (IMMUTABLE). Aplicada y verificada: 41 alertas de
  Andorra = todas `active` (sin decay aún). Mantener Python y SQL sincronizados.
- **Test:** `tests/test_state_lifecycle.py` — fronteras (0.50 inclusivo a active),
  conf<0.30 no-resuelta = decaying (no resolved), resolved manda, orden de pesos.
- **Nota de integración:** la API (`api/main.py`) aún no rankea por alertas; los
  helpers son el *enabler* para cuando se wire ranking en `/search`. `active_alert_types`
  sigue incluyendo active+decaying (ambos resolved=FALSE) — correcto.

### T2.3 — Half-life por señal + recency boost + reproceso condicionado ✅ (2026-05-28)

- **Half-life por señal YA EXISTÍA**: `SignalType.half_life_days` (más granular que
  el dict `SIGNAL_HALF_LIFE_DAYS` que proponía el plan) y `decayed_weight` ya aplicaba
  `0.5^(Δt/half_life)`. La parte estructural de T2.3 estaba hecha.
- **Net-new — recency boost** (`enrichment/state_aggregator.py`):
  - `recency_boost(Δt) = 1 + α·exp(-Δt/window)`, α=0.5, window=60d (constantes
    `RECENCY_BOOST_ALPHA`/`RECENCY_BOOST_WINDOW_DAYS`). t=0 → 1.5; t=60 → ~1.18; t≫ → 1.0.
  - `observation_weight_at(weight, observed_at, hl, now)` = `decayed_weight × recency_boost`,
    espejo exacto de la fórmula del plan `w_final = base · 2^(-Δt/hl) · recency_boost`.
  - `decayed_weight` se mantiene como decay puro (sin boost) para call-sites/tests
    que lo quieran aislado.
  - **Ambos caminos de agregación** ahora usan `observation_weight_at`: el batch
    (`aggregate_observations`) y el incremental (`update_semantic_state`) —
    coherencia de entry paths (corolario del patrón de actuación).
- **Net-new — reproceso condicionado** (`needs_recompute` + `jobs/full_recompute.py --conditional`):
  - `needs_recompute(half_lives, elapsed_days)` → True si `min(half_lives) < elapsed`.
    Las señales persistentes (beauty HL=36500) no cambian en una semana → skip.
  - `full_recompute --conditional` recomputa SIEMPRE spots `stale` o sin estado previo;
    si no, aplica el gate. Verificado: spot 85057 recién agregado y no-stale → skipped=1.
- **Decisión documentada:** `v2_materializer._decayed_weight` (noise_sources/parking_capacity,
  señales TEXT con estrategia presence-threshold / recent_wins) **NO** recibe recency boost.
  La fórmula del plan aplica a scores numéricos/booleanos (weighted_mean/consensus); meter
  boost en un umbral de presencia movería el corte sin beneficio. `recent_wins` ya prioriza
  lo reciente por diseño.
- **Verificación live:** recompute de spot 85057 → `weight_support` 10.35→13.69,
  `beauty_score` 0.601→0.630 (obs recientes pesan más, esperado), `quietness` estable.
- **Test:** `tests/test_signal_half_life.py` — boost en t=0/t=window/t≫, decay a 1 HL = 0.5,
  `observation_weight_at` = decay×boost, gate condicional (volátil/persistente/vacío/elapsed=0).

### T2.4 — Job mensual de revisión de unknown_tags ✅ (2026-05-28)

- **El job YA EXISTÍA** desde T1.5 (`jobs/review_unknown_tags.py`: listar top, promover,
  dismiss). T2.4 lo convierte en herramienta de revisión mensual real.
- **Net-new — stats de cabecera** (`tag_canonicalizer.unknown_tags_stats`):
  `COUNT total/reviewed/pending` + `SUM(occurrence_count) FILTER (WHERE NOT reviewed)`.
  Alimenta `_fmt_header` con pendientes/revisados/total + instrucciones de uso.
- **Net-new — sugerencia difusa** (`tag_canonicalizer.suggest_canonical`, `import difflib`):
  - `suggest_canonical(tag, index, cutoff=0.72)` → match exacto si está en el índice;
    si no, `difflib.get_close_matches(n=1, cutoff)` contra canonicals+aliases normalizados;
    devuelve el `canonical_id` destino o None. Acelera la revisión humana (typos/variantes).
  - Columna *suggested canonical* en `_fmt_markdown`; el reporte propone pero no promueve.
- **Net-new — archivado** (`--out`): escribe el markdown a fichero (os.makedirs + utf-8)
  para histórico mensual. Verificado: `docs/reports/unknown_tags_2026-05.md` (134 pendientes
  / 311 ocurrencias; sugerencias correctas: mountain-view→mountain, no-overnight→overnighting,
  bus→busy).
- **Bug pre-existente reparado (patrón de actuación):** `_connect()` de
  `review_unknown_tags.py` Y `nightly_alert_decay.py` tenían su propio fallback de DSN con
  password literal `'geospots'` / host `'db'` / port `5432`, que fallaba contra la DB real
  (`InvalidPasswordError`). Ambos reusan ahora `worker._dsn()` (carga .env), igual que el
  resto del pipeline. `DATABASE_URL` sigue teniendo prioridad si está definida.
- **Test:** `tests/test_unknown_tag_suggest.py` — typos (quieet→quiet, constructio→construction),
  alias exacto (mountain-view→mountain), sin parecido→None, vacío→None, índice vacío→None,
  cutoff alto descarta débiles.

### T2.5 — Detección de cambio de régimen con guardas ✅ (2026-05-28)

- **Objetivo:** detectar contradicciones temporales reales (Grau Roig: obras 2025 →
  tranquilo 2026) sin falsos positivos en spots con poca actividad.
- **`detect_regime_change(observations, signal_type, *, value_type, now)`**
  (`enrichment/state_aggregator.py`): parte las observaciones de UNA señal en
  reciente (≤180d) e histórico (>180d), compara medias ponderadas. Guardas:
  - cada cluster ≥3 observaciones (`REGIME_MIN_CLUSTER_SIZE`),
  - separación temporal entre clusters ≥90d (`REGIME_MIN_SEPARATION_DAYS`),
  - |Δmedia| > 0.4 (`REGIME_MIN_DELTA`).
  Devuelve `{changed, old, new, delta, since, n_recent, n_historical}` o None.
- **Pesos sin decay/recency:** usa `observation_weight` crudo — comparamos el valor
  intrínseco de cada periodo; decaer el histórico a ~0 distorsionaría su media.
- **Bug del pseudocódigo del plan reparado (patrón de actuación):** el plan escribía
  la separación como `min(historical) − max(recent)`, pero el histórico es MÁS
  antiguo → esa resta es siempre negativa → guard siempre activa → nunca detecta nada.
  Correcto: `min(recent_dates) − max(historical_dates)`. Implementado así y documentado
  en el docstring.
- **`compute_signal_flux(rows, ...)`**: aplica la detección a todas las señales
  numéricas/booleanas del spot; salta las TEXT (`recent_wins`: noise_source,
  parking_capacity — el test |Δ|>0.4 no aplica a categóricas libres).
- **Materialización:** se computa SOLO en `recompute_spot_state` (full recompute, donde
  están TODAS las observaciones) y se persiste en `spot_semantic_state.signal_flux`
  (columna ya reservada en T1.4c, schema `{"<signal>": {...}}`). El path incremental
  `update_semantic_state` NO la toca → el UPSERT preserva el valor existente.
- **Verificación live:** spot 78809 tenía Δmedia 0.67→0.20 en quietness con n≥3 por
  cluster, pero gap=29d <90d → correctamente rechazado (drift continuo, no salto). El
  test sintético con gap real ≥90d sí lo detecta. Las guardas funcionan como esperado.
- **Test:** `tests/test_regime_change.py` — caso Grau Roig (0.2→0.85), guarda n bajo,
  guarda separación (drift continuo cruzando 180d), guarda delta pequeño, señal boolean
  (0→1), `compute_signal_flux` salta señales TEXT.
