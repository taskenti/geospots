-- ═══════════════════════════════════════════════════════════════
-- Migration: Phase 3 v2 (1 LLM call per spot, Batch API + caching)
-- Safe to run with scrapers and v1 enrichment workers active.
-- Idempotente: se puede re-ejecutar sin efectos secundarios.
-- ═══════════════════════════════════════════════════════════════
-- Ningún statement bloquea writes en spots/reviews/source_records.
-- ALTER ADD COLUMN nullable + DEFAULT = metadata-only (PG ≥11).
-- CREATE INDEX CONCURRENTLY = no bloquea writes; debe ir fuera de
-- transacción (psql lo ejecuta así por defecto).
-- ═══════════════════════════════════════════════════════════════

-- ───────────────────────────────────────────────────────────────
-- 0. extracted_claims.review_id nullable (v2 admite claims desde "description")
-- ───────────────────────────────────────────────────────────────
ALTER TABLE extracted_claims ALTER COLUMN review_id DROP NOT NULL;

-- ───────────────────────────────────────────────────────────────
-- 1. Nuevas columnas en spot_semantic_state
-- ───────────────────────────────────────────────────────────────
ALTER TABLE spot_semantic_state
    ADD COLUMN IF NOT EXISTS enrichment_version   INT       DEFAULT 1,
    ADD COLUMN IF NOT EXISTS llm_model            TEXT,
    ADD COLUMN IF NOT EXISTS last_observation_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS noise_sources        TEXT[],
    ADD COLUMN IF NOT EXISTS parking_capacity     TEXT,
    ADD COLUMN IF NOT EXISTS cell_coverage        REAL,
    ADD COLUMN IF NOT EXISTS wild_camping_legal   BOOLEAN,
    ADD COLUMN IF NOT EXISTS avoid_season         TEXT;

-- freshness_warning como VISTA (PG no permite NOW() en GENERATED STORED).
CREATE OR REPLACE VIEW v_spot_semantic_state AS
SELECT
    sss.*,
    (sss.last_observation_at IS NOT NULL
     AND sss.last_observation_at < NOW() - INTERVAL '24 months') AS freshness_warning
FROM spot_semantic_state sss;

-- ───────────────────────────────────────────────────────────────
-- 2. Nuevas signal_types (v2)
--    Modelo: señales text/numeric/boolean atómicas.
--    noise_sources, tags, best_for se materializan en spot_semantic_state
--    desde claims individuales (noise_source) o desde la respuesta narrativa
--    del LLM (tags, best_for) — no son signal_types.
-- ───────────────────────────────────────────────────────────────
INSERT INTO signal_types
    (id, parent_id, display_name, value_type, decay_class,
     half_life_days, aggregation_strategy, contradiction_strategy, importance_weight)
VALUES
    ('noise_source',         'noise', 'Fuente de Ruido',          'text',    'slow',     180,  'recent_wins',       'recent_wins',         1.2),
    ('parking_capacity',     NULL,    'Capacidad de Parking',     'text',    'slow',     1825, 'recent_wins',       'recent_wins',         0.6),
    ('cell_coverage',        NULL,    'Cobertura Movil',          'numeric', 'slow',     365,  'weighted_mean',     'majority_consensus',  0.7),
    ('wild_camping_legal',   NULL,    'Acampada Libre Legal',     'boolean', 'slow',     730,  'consensus_boolean', 'recent_wins',         2.0),
    ('mosquitoes',           NULL,    'Mosquitos',                'numeric', 'volatile', 180,  'weighted_mean',     'recent_wins',         0.5),
    ('dog_friendly',         NULL,    'Apto Perros',              'boolean', 'slow',     1825, 'consensus_boolean', 'majority_consensus',  0.6),
    ('family_friendly',      NULL,    'Apto Familias',            'boolean', 'slow',     1825, 'consensus_boolean', 'majority_consensus',  0.6),
    ('accessible_pmr',       NULL,    'Accesible PMR',            'boolean', 'slow',     1825, 'consensus_boolean', 'majority_consensus',  0.6),
    ('water_working',        NULL,    'Agua Operativa',           'boolean', 'volatile', 60,   'consensus_boolean', 'recent_wins',         1.5),
    ('electricity_working',  NULL,    'Electricidad Operativa',   'boolean', 'volatile', 60,   'consensus_boolean', 'recent_wins',         1.5),
    ('dump_station_working', NULL,    'Vaciado Aguas Operativo',  'boolean', 'volatile', 60,   'consensus_boolean', 'recent_wins',         1.5)
ON CONFLICT (id) DO NOTHING;

-- ───────────────────────────────────────────────────────────────
-- 3. Tabla auxiliar: batches enviados a Gemini Batch API
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enrichment_batches (
    id                  BIGSERIAL PRIMARY KEY,
    batch_name          TEXT UNIQUE NOT NULL,
    enrichment_version  INT  NOT NULL,
    llm_model           TEXT NOT NULL,
    spot_ids            INT[] NOT NULL,
    state               TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending','running','succeeded','failed','partial','cancelled')),
    n_requested         INT  NOT NULL,
    n_succeeded         INT,
    n_failed            INT,
    tokens_input        BIGINT,
    tokens_output       BIGINT,
    cost_estimated_usd  REAL,
    error_msg           TEXT,
    submitted_at        TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

-- ───────────────────────────────────────────────────────────────
-- 4. Tabla auxiliar: estado del system-prompt cache (Gemini context caching)
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enrichment_cache_state (
    id                  BIGSERIAL PRIMARY KEY,
    enrichment_version  INT  NOT NULL,
    llm_model           TEXT NOT NULL,
    cache_name          TEXT NOT NULL UNIQUE,
    cache_token_count   INT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    UNIQUE (enrichment_version, llm_model, cache_name)
);

-- ───────────────────────────────────────────────────────────────
-- 5. Índices (CONCURRENTLY, fuera de transacción)
-- ───────────────────────────────────────────────────────────────
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sss_enrichment_version
    ON spot_semantic_state(enrichment_version);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sss_last_observation
    ON spot_semantic_state(last_observation_at DESC NULLS LAST);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_eb_state
    ON enrichment_batches(state) WHERE state IN ('pending','running');

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ecs_active
    ON enrichment_cache_state(enrichment_version, llm_model, expires_at DESC);

-- ───────────────────────────────────────────────────────────────
-- 6. Verificación rápida
-- ───────────────────────────────────────────────────────────────
DO $$
DECLARE
    v_cols  INT;
    v_sigs  INT;
BEGIN
    SELECT COUNT(*) INTO v_cols
    FROM information_schema.columns
    WHERE table_name = 'spot_semantic_state'
      AND column_name IN ('enrichment_version','llm_model','last_observation_at',
                          'noise_sources','parking_capacity',
                          'cell_coverage','wild_camping_legal','avoid_season');

    SELECT COUNT(*) INTO v_sigs
    FROM signal_types
    WHERE id IN ('noise_source','parking_capacity','cell_coverage','wild_camping_legal',
                 'mosquitoes','dog_friendly','family_friendly','accessible_pmr',
                 'water_working','electricity_working','dump_station_working');

    RAISE NOTICE 'Phase 3 v2 migration check: % new columns, % new signals', v_cols, v_sigs;

    IF v_cols < 8 THEN
        RAISE WARNING 'Faltan columnas en spot_semantic_state (esperadas 8, presentes %)', v_cols;
    END IF;
    IF v_sigs < 11 THEN
        RAISE WARNING 'Faltan signal_types nuevos (esperados 11, presentes %)', v_sigs;
    END IF;
END $$;
