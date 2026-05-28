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
| **T1.6** `semantic_fingerprint` + invalidación | **No existe**. `embedding_generator.fetch_embedding_candidates` distingue stale vía `sss.updated_at > se.created_at` (línea 258) — esto es lo que el plan llama *"state_version simple"* y queremos sustituir por fingerprint. | Migración añade `spot_semantic_state.semantic_fingerprint` y `spot_embeddings.built_from_fingerprint`. Función `compute_fingerprint` en `embedding_generator.py`. Actualizar `fetch_embedding_candidates` para usar fingerprint en vez de `updated_at > created_at`. |
| **T1.7** Idempotencia del worker | **Parcialmente OK.** `orchestrator_v2.select_candidates` ya filtra por `COALESCE(sss.enrichment_version, 0) < $1` (línea 112). `worker.py.fetch_pending_reviews` filtra por `r.llm_processed = FALSE` (línea 139). Ambos sí soportan reanudación. **Bug menor**: kill -9 a mitad de una transacción `process_review` deja la review sin `llm_processed=TRUE` pero las inserts ya hechas hicieron rollback — al reiniciar se reprocesa, esto es correcto. | Solo añadir test en regression suite. Documentar que `enrichment_version` debe incrementarse cuando cambie el prompt v4 → v5. |
| **T1.8** Cache hit rate logging | **Parcial.** `llm_provider.call_deepseek_sync` ya extrae `prompt_cache_hit_tokens` y lo mete en `usage["cached_content_token_count"]` (línea 130). `orchestrator_v2._process_one_spot` lo acumula en `stats.tokens_input_total` pero **no loguea ratio per-call**, **no persiste por spot**. | (a) Añadir log `logger.info("llm.cache_hit", ...)` en `call_deepseek_sync` antes de devolver. (b) Crear tabla `llm_call_metrics` en migración v6. (c) Insertar fila en `_process_one_spot` tras recibir respuesta. |
| **T0.2** Regression suite v1 | ✅ **HECHO** (2026-05-28). `tests/regression/semantic_suite.py` (20 casos, 3 tiers). Baseline Grau Roig 85057: 1 hard fail `chronology_ok`, 1 band warn `quietness=0.900`, 3 skips (deps T1.4). Snapshot en `tests/regression/snapshots/grau_roig_obras.json`. | — |
| **T0.3** Inmutables vs regenerables en CLAUDE.md | ✅ **HECHO** (2026-05-28). Sección añadida. | — |
| **T2.7** flag `stale` | **YA EXISTE** (¡buenas!). `spot_semantic_state.stale BOOLEAN DEFAULT FALSE` + `last_aggregated_at TIMESTAMPTZ` (schema.sql líneas 697-699). `orchestrator_v2.select_candidates` ya lo respeta (línea 113). `state_aggregator` ya pone `stale = FALSE` tras recompute (líneas 192, 213, 377). | Sólo falta el **trigger** que marca `stale=TRUE` al llegar observación nueva con `observed_at > last_aggregated_at`. T2.7 se reduce a ese trigger. |

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

**Próximo paso:** Sprint 2 ✅ completo (T1.4 + T1.4b + T1.4c + T1.5). Siguiente bloque: **Sprint 3** — T1.6 (`semantic_fingerprint` para invalidación de embeddings), T1.7 (`--force-spot-ids` en `orchestrator_v2`, doc del bump convention), T1.8 (`llm_call_metrics` + log cache hit ratio per-call), y T2.7 (trigger SQL `stale=TRUE` al insertar `normalized_observations` con `observed_at > last_aggregated_at`). Necesitará una migración `db/migration_phase3_v6c.sql` con `semantic_fingerprint` en `spot_semantic_state`, `built_from_fingerprint` en `spot_embeddings`, tabla `llm_call_metrics`, y el trigger `trg_stale_on_observation`. También: añadir flag `--country` a `worker.py` (pre-Sprint 4, bloqueante para el smoke Andorra).
