# Fase 3 — Plan de migración v1 → v2

> **Objetivo:** pasar de "1 LLM call por review, regex-first" a "1 LLM call por spot, Gemini-first via Batch API con context caching".
> **Riesgo:** medio. `extracted_claims` v1 son inmutables y compatibles; el cambio es de *cómo* se generan futuros claims y de *qué* se materializa en `spot_semantic_state`.
> **Reversibilidad:** alta. v1 y v2 coexisten distinguidos por `extractor_name` + `enrichment_version`.

---

## Principios rectores

1. **Coexistencia, no big-bang.** v1 sigue activo hasta que v2 demuestre paridad o mejora en N=100 spots de validación.
2. **Idempotencia.** Cada paso se puede repetir sin corromper datos. Migraciones SQL con `IF NOT EXISTS`.
3. **Trazabilidad.** Cada claim sabe quién lo generó (`extractor_name`, `extractor_version`, `pipeline_run_id`).
4. **Coste antes que velocidad.** Batch API (24h latencia, 50% off) es la opción por defecto. Síncrono solo para debug.

---

## Orden de PRs

### PR 1 — Schema + signal catalog (sin código nuevo) · ~150 LOC

**Archivos:**
- `db/migration_phase3_v2.sql` (nuevo)
- `db/schema.sql` (añadir cambios al schema canónico para deployments limpios)

**Cambios:**
1. `ALTER TABLE spot_semantic_state` añadir:
   `enrichment_version INT DEFAULT 1`, `llm_model TEXT`,
   `last_observation_at TIMESTAMPTZ`,
   `freshness_warning BOOLEAN GENERATED ALWAYS AS (...) STORED`,
   `noise_sources TEXT[]`, `parking_capacity TEXT`, `cell_coverage REAL`,
   `wild_camping_legal BOOLEAN`, `avoid_season TEXT`.
2. Índices `idx_sss_version`, `idx_sss_freshness`.
3. `INSERT INTO signal_types` (idempotente con `ON CONFLICT DO NOTHING`):
   `noise_sources`, `parking_capacity`, `cell_coverage`, `wild_camping_legal`,
   `mosquitoes`, `dog_friendly`, `family_friendly`, `accessible_pmr`,
   `water_working`, `electricity_working`, `dump_station_working`.

**Validación:**
- Migración corre dos veces sin error.
- `SELECT count(*) FROM signal_types` aumenta.
- v1 sigue funcionando: `worker.py --dry-run` no rompe.

**Rollback:** las columnas son nullables; no rompen v1. En el peor caso `ALTER TABLE ... DROP COLUMN`.

---

### PR 2 — Prompt v2 + selector de reviews · ~400 LOC

**Archivos:**
- `enrichment/prompts.py` (extender)
- `enrichment/spot_packager.py` (nuevo)
- `tests/test_spot_packager.py` (nuevo)

**Cambios:**
1. `prompts.py`:
   - `SPOT_SYSTEM_PROMPT_V2` con catálogo completo + reglas (negación, sarcasmo, citar `review_id`).
   - `build_spot_user_prompt(spot, reviews)` devuelve string.
   - Constante `ENRICHMENT_VERSION = 2`.
2. `spot_packager.py`:
   - `select_reviews_for_prompt(reviews, max_tokens=3500)` con decay + rating + longitud.
   - `estimate_tokens(text)` aprox (len/4 o tiktoken si está).
   - `has_rich_description(spot)` para spots sin reviews pero con texto útil.
3. Tests unitarios: selección respeta budget; orden estable; reviews sin fecha pesan 0.3.

**Validación:**
- `pytest tests/test_spot_packager.py`.
- Smoke: empaquetar 5 spots reales en notebook, inspeccionar prompts visualmente.

**Rollback:** módulo nuevo no usado por v1.

---

### PR 3 — Cliente Gemini Batch + context caching · ~500 LOC

**Archivos:**
- `enrichment/gemini_batch.py` (nuevo)
- `enrichment/gemini_cache.py` (nuevo)
- `tests/test_gemini_batch_parse.py` (nuevo, solo parseo, no llama API)

**Cambios:**
1. `gemini_cache.py`:
   - `ensure_system_cache(version, ttl=3600)` crea o recupera el cache del system prompt. Persiste `cache_name` en tabla `enrichment_cache_state(version, cache_name, created_at, expires_at)`.
   - Refresh automático si TTL < 10 min.
2. `gemini_batch.py`:
   - `submit_batch(jobs)` → devuelve `batch_name`.
   - `poll_batch(batch_name)` con backoff exponencial.
   - `parse_results(batch_name)` yielding `(spot_id, payload_dict, errors)`.
   - Validación schema de respuesta (pydantic o jsonschema): claims con `signal`, `value`, `review_id`; campos top-level opcionales.
3. Tests: parseo de respuesta válida, malformada, con markdown wrap, con campos extra.

**Validación:**
- Test e2e manual: 10 spots → batch real → resultados parseables.
- Coste real comparado con estimación.

**Rollback:** módulo aislado, no toca v1.

---

### PR 4 — Ingesta v2 + recompute · ~400 LOC

**Archivos:**
- `enrichment/ingest_v2.py` (nuevo)
- `enrichment/state_aggregator.py` (extender `recompute_spot_state` para usar nuevas columnas)

**Cambios:**
1. `ingest_v2.py`:
   ```python
   async def ingest_spot_enrichment(conn, spot_id, payload, version, pipeline_run_id, model):
       async with conn.transaction():
           for claim in payload["claims"]:
               claim_id = await insert_claim_v2(conn, spot_id, claim, pipeline_run_id, model)
               obs = normalize_claim_v2(claim, spot_id)
               if obs: await insert_observation(conn, claim_id, obs)
           await recompute_spot_state(conn, spot_id)
           await update_narrative_fields(conn, spot_id, payload, version, model)
   ```
2. `recompute_spot_state` además materializa:
   - `last_observation_at = MAX(observed_at)` de observaciones del spot
   - `noise_sources` desde claims `signal_type='noise_sources'` (text_array)
   - resto de columnas nuevas desde claims correspondientes
   - `enrichment_version`, `llm_model`
3. `update_narrative_fields` mete `summary_es/en`, `tags`, `best_for`, `best_season`, `avoid_season`, `parking_capacity`, `wild_camping_legal`.

**Validación:**
- Test con payload fixture: claims se persisten, state se actualiza, `enrichment_version=2`.
- Idempotencia: ingerir el mismo payload dos veces no duplica `extracted_claims` si tienen `pipeline_run_id` distinto pero igual `review_id+signal+value` (PR 4.5 abajo).

**Decisión pendiente:** ¿`extracted_claims` admite duplicados entre runs o añadimos `UNIQUE(review_id, signal_type, extractor_version)`? Recomendación: **permitir duplicados** entre versiones; agregador hace `DISTINCT ON (review_id, signal_type)` por versión más reciente.

---

### PR 5 — Job nocturno + selección de candidatos · ~300 LOC

**Archivos:**
- `jobs/nightly_enrichment_v2.py` (nuevo)

**Cambios:**
```python
async def select_candidates(pool, limit=5000):
    # Prioridad:
    # 1. Nunca enriched + ≥3 reviews
    # 2. enrichment_version < CURRENT_VERSION
    # 3. stale = TRUE
    # 4. reviews_new_since_last_aggregated >= 5
    # 5. last_aggregated_at < NOW() - 18 months
    return await pool.fetch("""
        WITH candidates AS (
            SELECT s.id AS spot_id,
                   COALESCE(sss.enrichment_version, 0) AS v,
                   sss.stale,
                   sss.last_aggregated_at,
                   (SELECT COUNT(*) FROM reviews r WHERE r.spot_id=s.id) AS n_reviews,
                   (SELECT COUNT(*) FROM reviews r
                    WHERE r.spot_id=s.id
                      AND r.created_at > COALESCE(sss.last_aggregated_at, '1970-01-01')) AS n_new
            FROM spots s
            LEFT JOIN spot_semantic_state sss ON sss.spot_id=s.id
            WHERE s.activo = TRUE
        )
        SELECT spot_id FROM candidates
        WHERE n_reviews >= 3 AND (
            v < $1
            OR stale = TRUE
            OR n_new >= 5
            OR last_aggregated_at < NOW() - INTERVAL '18 months'
        )
        ORDER BY v ASC, n_new DESC NULLS LAST
        LIMIT $2
    """, CURRENT_ENRICHMENT_VERSION, limit)
```

Pipeline del job:
1. `ensure_system_cache(version)`.
2. `select_candidates(limit=5000)`.
3. Para cada uno: `select_reviews_for_prompt` + `build_spot_user_prompt`.
4. `submit_batch(jobs)` → guardar `batch_name` en `enrichment_batches`.
5. `poll_batch` hasta `SUCCEEDED`.
6. Para cada resultado: `ingest_spot_enrichment`.
7. Log de stats finales + coste estimado.

**Tabla auxiliar `enrichment_batches`:**
```sql
CREATE TABLE IF NOT EXISTS enrichment_batches (
    id              BIGSERIAL PRIMARY KEY,
    batch_name      TEXT UNIQUE NOT NULL,
    enrichment_version INT NOT NULL,
    spot_ids        INT[] NOT NULL,
    state           TEXT NOT NULL,            -- 'pending'|'succeeded'|'failed'
    n_requested     INT NOT NULL,
    n_succeeded     INT,
    n_failed        INT,
    cost_estimated  REAL,
    submitted_at    TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
```

**Validación:**
- Dry-run: 50 candidatos, generar jobs sin enviar a API. Inspeccionar tokens y prompts.
- Real: 200 candidatos, batch real, verificar persistencia.

---

### PR 6 — Validación A/B vs. v1 · ~200 LOC

**Archivo:** `jobs/validate_phase3_v2.py`

**Cambios:**
- Tomar N=100 spots con `enrichment_version=2`.
- Para cada uno, comparar:
  - `quietness_score` v1 vs v2 → divergencia media, p95.
  - `summary_es` v2 vs reviews originales → ¿hay alucinaciones? (validación manual via UI admin).
  - `tags` y `best_for` → ¿cubren los principales temas?
- Generar reporte en markdown.

**Criterio de paso a producción:**
- |Δscore| medio < 0.15 entre v1 y v2 (donde v1 tenía señal).
- v2 cubre ≥80% de spots que v1 cubría + nuevos spots con summary.
- 0 alucinaciones detectadas en muestra de 30 revisada manualmente.

---

### PR 7 — Cleanup + apagar v1 · ~100 LOC

Una vez PR 6 pasa criterios:

1. `enrichment/worker.py` v1 entra en modo legacy: solo se invoca con `--legacy` flag.
2. Cron / scheduler apunta a `jobs/nightly_enrichment_v2.py`.
3. Admin panel muestra `enrichment_version` por spot.
4. `extracted_claims` v1 se conservan (inmutables); estado agregado pasa a depender solo de claims `extractor_name='gemini_spot_v2'` o version ≥ 2.

---

## Timeline sugerida

| Semana | PRs | Hito |
|---|---|---|
| 1 | PR 1, PR 2 | Schema listo, prompts diseñados, validados a ojo |
| 2 | PR 3, PR 4 | E2E manual de 10 spots reales funciona |
| 3 | PR 5 | Job nocturno corre con 200 spots de prueba |
| 4 | PR 6 | A/B validation report; decidir GO/NO-GO |
| 5 | PR 7 + backfill | Pasada inicial 80K spots (~$16, una sola noche) |

---

## Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Batch API tarda > 24h | Job tolera espera larga; alertar si > 36h. Para urgencias usar API síncrona puntual. |
| Gemini cambia formato de respuesta | Validación schema estricta; payload entero se guarda en `extracted_claims.excerpt` o tabla aux para debug. |
| Coste real > estimado | Tracking por `enrichment_batches.cost_estimated`; cortar batches si pasada nocturna > $5. |
| Alucinaciones en summary | PR 6 valida; si fallan, añadir "Si una afirmación no aparece literalmente en las reviews, omítela" al system prompt. |
| Reviews en idiomas no cubiertos | El system prompt menciona idiomas habituales (es/en/fr/de/it/nl); Gemini Flash maneja bien multilingüe. Tests con muestras. |
| Context cache expira mid-batch | `ensure_system_cache` se refresca; si el batch ya está submitted, los requests llevan `cache_name` que sigue vigente hasta TTL+gracia. |
| Pérdida de granularidad por review | Cada claim mantiene `review_id` de origen → trazabilidad intacta. |

---

## Decisiones que necesito confirmar antes de PR 1

1. **¿Mantenemos el regex extractor de v1 como "boost"** (corre además de Gemini para señales triviales) **o lo apagamos del todo en v2?**
   - Recomendación: **apagarlo**. Los falsos positivos pesan en `spot_semantic_state` y no compensan los céntimos ahorrados.

2. **¿`extracted_claims` admite múltiples versiones del mismo claim** (v1 dijo `quietness=0.9`, v2 dice `quietness=0.7` para la misma review) **o reemplazamos?**
   - Recomendación: **admitir múltiples**, con `extractor_version` distinguiendo. El agregador toma la versión más reciente vía `DISTINCT ON`. Conservamos historia barata.

3. **¿Pasada inicial (80K spots) la lanzamos toda de golpe** (~$16, una noche) **o por países en 3-4 tandas** para validar progresivamente?
   - Recomendación: **3 tandas**: ES (test calidad en idioma fuerte), luego FR+IT+DE, luego resto.

4. **¿`stale=TRUE` lo trata v2 igual que v1**, o queremos un campo `needs_v2_enrichment` separado para no romper la lógica antigua durante coexistencia?
   - Recomendación: **mismo campo `stale`**, distinguir por `enrichment_version`. Más simple.

---

## Quick-wins independientes (se pueden colar antes)

Cosas que mejoran v1 sin cambiar arquitectura, hacelas si querés mientras se decide el resto:

- **A.** Añadir negación al regex de v1: `\b(no|nada|sin|never|nicht|pas)\s+\w{0,15}\s+(tranquil|safe|clean)` → invierte el score.
- **B.** Añadir `last_observation_at` a `spot_semantic_state` y `freshness_warning` generated column. Beneficio inmediato en UI.
- **C.** Exponer `effective_confidence = consensus * freshness_factor * source_diversity` en el endpoint `/spot/:id`.
