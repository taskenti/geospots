-- Migration Phase 3 v6c — Sprint 3 + T2.7
-- =========================================================================
-- Cubre:
--   T1.6 — `spot_semantic_state.semantic_fingerprint` + `spot_embeddings.built_from_fingerprint`.
--   T1.8 — Tabla `llm_call_metrics` (per-call observability del provider).
--   T2.7 — Trigger `trg_stale_on_observation` (marca `stale=TRUE` al llegar
--          observación nueva con observed_at > last_aggregated_at).
--
-- Idempotente: usa IF NOT EXISTS y CREATE OR REPLACE para función/trigger.
-- =========================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────
-- T1.6 — semantic_fingerprint
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE spot_semantic_state
    ADD COLUMN IF NOT EXISTS semantic_fingerprint TEXT;

COMMENT ON COLUMN spot_semantic_state.semantic_fingerprint IS
    'T1.6: SHA1[:16] de canonical_tags + active_alert_types + embedding_input_text + schema_version. Sustituye el comparador updated_at>created_at en jobs/nightly_embeddings.';

CREATE INDEX IF NOT EXISTS idx_sss_semantic_fingerprint
    ON spot_semantic_state (semantic_fingerprint) WHERE semantic_fingerprint IS NOT NULL;

ALTER TABLE spot_embeddings
    ADD COLUMN IF NOT EXISTS built_from_fingerprint TEXT;

COMMENT ON COLUMN spot_embeddings.built_from_fingerprint IS
    'T1.6: fingerprint del estado que generó este embedding. nightly_embeddings detecta drift comparando con spot_semantic_state.semantic_fingerprint.';

CREATE INDEX IF NOT EXISTS idx_spot_embeddings_fingerprint
    ON spot_embeddings (built_from_fingerprint) WHERE built_from_fingerprint IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────
-- T1.8 — llm_call_metrics
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS llm_call_metrics (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider        TEXT NOT NULL,                   -- 'gemini' | 'deepseek'
    model           TEXT NOT NULL,
    spot_id         BIGINT,                          -- NULL para llamadas no-spot (search intent, etc.)
    country         TEXT,                            -- ISO2 (Andorra → 'AD')
    prompt_tokens     INT NOT NULL DEFAULT 0,
    cached_tokens     INT NOT NULL DEFAULT 0,        -- prompt_cache_hit_tokens
    completion_tokens INT NOT NULL DEFAULT 0,
    latency_ms        INT,                           -- opcional (no siempre disponible)
    cache_hit_ratio   NUMERIC(5,4),                  -- derivado, materializado para queries rápidas
    enrichment_version INT,                          -- snapshot del ENRICHMENT_VERSION activo
    prompt_version    TEXT,                          -- snapshot de PROMPT_VERSION
    pipeline_run_id   TEXT,                          -- correlación con `extracted_claims.pipeline_run_id`
    extra             JSONB                          -- por si necesitamos volcar metadata adicional sin migración
);

CREATE INDEX IF NOT EXISTS idx_llm_metrics_country_ts
    ON llm_call_metrics (country, ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_metrics_ts
    ON llm_call_metrics (ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_metrics_spot
    ON llm_call_metrics (spot_id) WHERE spot_id IS NOT NULL;

COMMENT ON TABLE llm_call_metrics IS
    'T1.8: una fila por llamada LLM. Permite query post-batch: AVG(cache_hit_ratio) por país, latencia p95, tokens totales, etc.';

-- ─────────────────────────────────────────────────────────────────────
-- T2.7 — Trigger stale en normalized_observations
-- ─────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION mark_spot_stale_on_new_obs()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE spot_semantic_state
       SET stale = TRUE
     WHERE spot_id = NEW.spot_id
       AND stale = FALSE
       AND NEW.observed_at > COALESCE(last_aggregated_at, '1970-01-01'::TIMESTAMPTZ);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION mark_spot_stale_on_new_obs IS
    'T2.7: marca spot_semantic_state.stale=TRUE cuando llega normalized_observation con observed_at posterior a last_aggregated_at. Permite que orchestrator_v2 priorice spots con datos nuevos sin escanear 125K filas cada noche.';

-- Drop + create para idempotencia (CREATE TRIGGER no soporta IF NOT EXISTS en pg<14;
-- usamos DROP defensivo).
DROP TRIGGER IF EXISTS trg_stale_on_observation ON normalized_observations;
CREATE TRIGGER trg_stale_on_observation
    AFTER INSERT ON normalized_observations
    FOR EACH ROW
    EXECUTE FUNCTION mark_spot_stale_on_new_obs();

COMMIT;

-- ─────────────────────────────────────────────────────────────────────
-- Smoke test post-migration (manual)
-- ─────────────────────────────────────────────────────────────────────
-- \d+ spot_semantic_state | grep semantic_fingerprint
-- \d+ spot_embeddings     | grep built_from_fingerprint
-- \d  llm_call_metrics
-- SELECT tgname FROM pg_trigger WHERE tgrelid='normalized_observations'::regclass;
