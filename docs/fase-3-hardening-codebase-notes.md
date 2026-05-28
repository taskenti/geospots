# Phase 3 Hardening — Notas de código real (T0.1)

**Propósito.** Antes de tocar nada del plan `fase-3-hardening-pre-batch.md`, mapear lo que ya existe en el repo contra lo que el plan dice construir. Sirve para no refactorizar algo que ya está, y para no asumir interfaces que el código no provee.

**Alcance leído.** `enrichment/prompts.py`, `spot_packager.py`, `state_aggregator.py`, `signal_registry.py`, `event_detector.py`, `worker.py`, `orchestrator_v2.py`, `llm_provider.py`, `claim_extractor.py` (parcial), `ingest_v2.py`, `embedding_generator.py` (parcial), `jobs/nightly_embeddings.py`, `db/schema.sql` (búsqueda dirigida).

---

## 1. Resumen por tarea del plan

| Tarea | Estado actual | Acción real necesaria |
|---|---|---|
| **T1.1** Prompt tail-loading + few-shot | El system prompt v4 ya es largo y mayormente estable (~3.5K tokens). El user prompt (`build_spot_user_prompt`) emite SPOT DATA primero (id, name, type, region, country, coords, sources) y luego SERVICES + DESCRIPTIONS + REVIEWS. **No hay few-shot examples**. No hay `CURRENT_DATE` en ningún sitio. `SUMMARY_RICHNESS`/`SUMMARY_INSTRUCTION` van al final del user prompt. | (a) Añadir bloque few-shot al final de `SYSTEM_PROMPT_V2` (mantener byte-estabilidad). (b) Mantener SPOT DATA al final del user prompt — ya está al final (bien). (c) Asegurar `json.dumps(..., sort_keys=True)` no aplica porque el prompt se construye con `"\n".join(lines)` — la estabilidad la garantiza el orden de las líneas (verificable). |
| **T1.2** STATIC_CONTEXT vs REVIEW_EVIDENCE | El SERVICES actual ya lleva la nota *"structured facts from sources — do not re-infer"* (línea 319 de `prompts.py`). Pero **el schema de output no separa `review_claims` de `contradicted_static_facts`** — solo hay `claims[]` con `review_id` opcional. `ingest_v2._resolve_observed_at` ya acepta `review_id IS NULL` (línea 65) y cae a NOW(). | (a) Marcar el SERVICES con tags XML literales `<STATIC_CONTEXT>...</STATIC_CONTEXT>` y reviews con `<REVIEW_EVIDENCE>...</REVIEW_EVIDENCE>`. (b) Modificar `SYSTEM_PROMPT_V2` para que el schema obligue `review_claims[]` + `contradicted_static_facts[]`. (c) `gemini_response_parser.parse_enrichment_response` debe rechazar items de `review_claims[]` con `review_id IS NULL`. Postprocesado: claims sin review_id de `'services'/'description'` se aceptan **solo** si vienen del array correcto. |
| **T1.3** CURRENT_DATE + age relativo | **Nada implementado.** `spot_packager._build_spot_user_prompt` formatea fecha como `"%Y-%m"` y no añade age. | Añadir `CURRENT_DATE: YYYY-MM-DD` al inicio del bloque SPOT DATA y `[age: Xd ago]` calculado contra `datetime.now(UTC) - r['fecha']` antes del `[source]` en la línea de cada review. |
| **T1.4** `spot_alerts` con lifecycle | **No existe.** No hay tabla `spot_alerts`, `spot_operational_state`, ni equivalente. No hay extracción/ingest de alertas en ingest_v2. | Crear migración `db/migration_phase3_v6.sql` con `spot_alerts`. Añadir `parsed.alerts[]` al parser (`gemini_response_parser.py`). Añadir paso 7 en `ingest_v2` para upsert de alertas. Resolver determinista en módulo nuevo `enrichment/state_resolver.py`. Cron diario: nuevo job en `jobs/` (o ampliación de `nightly_events.py`). |
| **T1.4b** `spot_function`/`is_overnight_viable`/`authorization_status` | **No existen** en `spots`. `spot_geo` ya tiene `elevation_m`, `slope_degrees`, `aspect_degrees`, `terrain_type` (línea 381 `schema.sql`). El plan menciona `terrain_surface` y `slope_grade` — **divergencia**: usar nombres existentes (`terrain_type`, `slope_degrees`) o añadir nuevos. Recomendación: usar los existentes y mapear desde el LLM. | (a) Migración añade columnas a `spots`. (b) LLM schema emite `spot_function/is_overnight_viable/authorization_status` top-level + `elevation_m/terrain_type/slope_degrees` (alineado a `spot_geo`). (c) `ingest_v2` crea fila en `spot_geo` si no existe y escribe ahí, no en `spots`. |
| **T1.4c** `signal_flux` + `active_alert_types` en `spot_semantic_state` | **No existen.** | Migración añade ambas columnas + GIN index. `state_aggregator.recompute_spot_state` debe escribir `active_alert_types` derivado de `spot_alerts WHERE resolved=FALSE`. `signal_flux` queda `'{}'::jsonb` hasta T2.5. |
| **T1.5** Canonicalizador de tags + unknown_tags | **No existe.** Los tags del LLM se persisten tal cual en `spot_semantic_state.tags` (`ingest_v2._update_narrative_and_materialized` línea 162: `parsed.tags or None`). | Crear `enrichment/tag_canonicalizer.py`. Tablas `canonical_tags`, `unknown_tags` en migración v6. Filtrar en `ingest_v2` antes del UPDATE. |
| **T1.6** `semantic_fingerprint` + invalidación | **No existe**. `embedding_generator.fetch_embedding_candidates` distingue stale vía `sss.updated_at > se.created_at` (línea 258) — esto es lo que el plan llama *"state_version simple"* y queremos sustituir por fingerprint. | Migración añade `spot_semantic_state.semantic_fingerprint` y `spot_embeddings.built_from_fingerprint`. Función `compute_fingerprint` en `embedding_generator.py`. Actualizar `fetch_embedding_candidates` para usar fingerprint en vez de `updated_at > created_at`. |
| **T1.7** Idempotencia del worker | **Parcialmente OK.** `orchestrator_v2.select_candidates` ya filtra por `COALESCE(sss.enrichment_version, 0) < $1` (línea 112). `worker.py.fetch_pending_reviews` filtra por `r.llm_processed = FALSE` (línea 139). Ambos sí soportan reanudación. **Bug menor**: kill -9 a mitad de una transacción `process_review` deja la review sin `llm_processed=TRUE` pero las inserts ya hechas hicieron rollback — al reiniciar se reprocesa, esto es correcto. | Solo añadir test en regression suite. Documentar que `enrichment_version` debe incrementarse cuando cambie el prompt v4 → v5. |
| **T1.8** Cache hit rate logging | **Parcial.** `llm_provider.call_deepseek_sync` ya extrae `prompt_cache_hit_tokens` y lo mete en `usage["cached_content_token_count"]` (línea 130). `orchestrator_v2._process_one_spot` lo acumula en `stats.tokens_input_total` pero **no loguea ratio per-call**, **no persiste por spot**. | (a) Añadir log `logger.info("llm.cache_hit", ...)` en `call_deepseek_sync` antes de devolver. (b) Crear tabla `llm_call_metrics` en migración v6. (c) Insertar fila en `_process_one_spot` tras recibir respuesta. |
| **T0.2** Regression suite v1 | **No existe.** No hay carpeta `tests/regression/`. | Crear estructura y 20-30 casos. Snapshot baseline ANTES de empezar T1.x. |
| **T0.3** Inmutables vs regenerables en CLAUDE.md | **No existe**. | Añadir sección. |
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

- **Sprint 1 (T1.1-T1.3)**: añadir también ajuste de `max_tokens` a 2500-3000 en `call_deepseek_sync` (riesgo bajo, cero coste de diseño).
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

**Próximo paso:** T0.2 — construir `tests/regression/semantic_suite.py` con baseline pre-cambios. Sin esto, los cambios T1.x no son verificables.
