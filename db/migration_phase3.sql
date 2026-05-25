-- Phase 3: geotemporal semantic state engine.
-- Idempotent migration: safe to run repeatedly on an existing GeoSpots DB.

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS texto_original TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS texto_limpio TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS texto_dsl TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS cleaned BOOLEAN DEFAULT FALSE;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS informativo BOOLEAN;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS llm_processed BOOLEAN DEFAULT FALSE;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS llm_analysis JSONB;

UPDATE reviews
SET texto_original = texto
WHERE texto_original IS NULL AND texto IS NOT NULL;

INSERT INTO source_credibility (source, display_name, base_score, review_quality, coverage_region) VALUES
('promobil',        'Promobil',            0.84, 0.78, ARRAY['DE','AT','CH']),
('camperstop',      'Camperstop',          0.80, 0.72, ARRAY['EU']),
('vansite',         'Vansite',             0.72, 0.70, ARRAY['EU']),
('nomady',          'Nomady',              0.76, 0.78, ARRAY['EU']),
('campspace',       'Campspace',           0.74, 0.76, ARRAY['EU']),
('wtmg',            'Welcome To My Garden',0.70, 0.72, ARRAY['EU']),
('areasac',         'AreasAC',             0.85, 0.60, ARRAY['ES'])
ON CONFLICT (source) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    base_score = EXCLUDED.base_score,
    review_quality = EXCLUDED.review_quality,
    coverage_region = EXCLUDED.coverage_region;

CREATE TABLE IF NOT EXISTS signal_types (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES signal_types(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    display_name_en TEXT,
    value_type TEXT NOT NULL CHECK (value_type IN ('numeric', 'boolean', 'text')),
    decay_class TEXT NOT NULL CHECK (decay_class IN ('permanent', 'slow', 'volatile')),
    half_life_days INT NOT NULL,
    aggregation_strategy TEXT NOT NULL CHECK (aggregation_strategy IN ('weighted_mean', 'consensus_boolean', 'recent_wins')),
    contradiction_strategy TEXT NOT NULL CHECK (contradiction_strategy IN ('recent_wins', 'majority_consensus', 'permanent_override')),
    importance_weight REAL DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO signal_types (id, parent_id, display_name, value_type, decay_class, half_life_days, aggregation_strategy, contradiction_strategy, importance_weight) VALUES
('noise', NULL, 'Ruido General', 'numeric', 'volatile', 90, 'weighted_mean', 'recent_wins', 1.5),
('road_noise', 'noise', 'Ruido de Carretera', 'numeric', 'volatile', 90, 'weighted_mean', 'recent_wins', 1.0),
('party_noise', 'noise', 'Ruido de Fiesta/Gente', 'numeric', 'volatile', 30, 'weighted_mean', 'recent_wins', 1.0),
('train_noise', 'noise', 'Ruido de Tren', 'numeric', 'slow', 730, 'weighted_mean', 'permanent_override', 0.8),
('quietness', NULL, 'Tranquilidad General', 'numeric', 'slow', 365, 'weighted_mean', 'majority_consensus', 1.5),
('beauty', NULL, 'Belleza del Entorno', 'numeric', 'permanent', 36500, 'weighted_mean', 'majority_consensus', 1.2),
('cleanliness', NULL, 'Limpieza', 'numeric', 'volatile', 60, 'weighted_mean', 'recent_wins', 0.8),
('safety', NULL, 'Seguridad General', 'numeric', 'slow', 365, 'weighted_mean', 'recent_wins', 1.5),
('police_risk', 'safety', 'Riesgo de Policia', 'numeric', 'volatile', 60, 'weighted_mean', 'recent_wins', 2.0),
('theft_risk', 'safety', 'Riesgo de Robos', 'numeric', 'volatile', 90, 'weighted_mean', 'recent_wins', 2.0),
('sea_view', 'beauty', 'Vistas al Mar', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.5),
('mountain_view', 'beauty', 'Vistas a Montana', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.5),
('lake_nearby', 'beauty', 'Lago Cercano', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.3),
('shade_morning', NULL, 'Sombra por la Manana', 'boolean', 'slow', 1825, 'consensus_boolean', 'majority_consensus', 0.4),
('shade_afternoon', NULL, 'Sombra por la Tarde', 'boolean', 'slow', 1825, 'consensus_boolean', 'majority_consensus', 0.4),
('large_vehicle', NULL, 'Acceso Vehiculos >7m', 'numeric', 'permanent', 36500, 'weighted_mean', 'permanent_override', 0.6),
('road_quality', NULL, 'Calidad del Acceso', 'numeric', 'slow', 1825, 'weighted_mean', 'majority_consensus', 0.5),
('overnight_safe', NULL, 'Pernocta Posible', 'boolean', 'volatile', 120, 'consensus_boolean', 'recent_wins', 2.0),
('crowd_level', NULL, 'Nivel de Masificacion', 'numeric', 'volatile', 30, 'weighted_mean', 'recent_wins', 1.0),
('wind_exposure', NULL, 'Exposicion al Viento', 'numeric', 'slow', 730, 'weighted_mean', 'majority_consensus', 0.6),
('stealth', NULL, 'Discrecion del Spot', 'numeric', 'slow', 365, 'weighted_mean', 'majority_consensus', 0.8)
ON CONFLICT (id) DO UPDATE SET
    parent_id = EXCLUDED.parent_id,
    display_name = EXCLUDED.display_name,
    value_type = EXCLUDED.value_type,
    decay_class = EXCLUDED.decay_class,
    half_life_days = EXCLUDED.half_life_days,
    aggregation_strategy = EXCLUDED.aggregation_strategy,
    contradiction_strategy = EXCLUDED.contradiction_strategy,
    importance_weight = EXCLUDED.importance_weight;

CREATE TABLE IF NOT EXISTS extracted_claims (
    id BIGSERIAL PRIMARY KEY,
    review_id BIGINT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    spot_id INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    signal_type TEXT NOT NULL REFERENCES signal_types(id),
    raw_value TEXT NOT NULL,
    extraction_confidence REAL NOT NULL DEFAULT 1.0,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    pipeline_run_id TEXT,
    excerpt TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ec_spot_signal ON extracted_claims(spot_id, signal_type);
CREATE INDEX IF NOT EXISTS idx_ec_review ON extracted_claims(review_id);
CREATE INDEX IF NOT EXISTS idx_ec_extractor ON extracted_claims(extractor_name, extractor_version);

CREATE TABLE IF NOT EXISTS normalized_observations (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES extracted_claims(id) ON DELETE CASCADE,
    spot_id INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    signal_type TEXT NOT NULL REFERENCES signal_types(id),
    value_num REAL,
    value_bool BOOLEAN,
    value_text TEXT,
    extraction_confidence REAL NOT NULL,
    source_confidence REAL NOT NULL DEFAULT 1.0,
    reviewer_confidence REAL NOT NULL DEFAULT 1.0,
    observation_weight REAL NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_no_spot_signal_date ON normalized_observations(spot_id, signal_type, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_no_claim ON normalized_observations(claim_id);

CREATE TABLE IF NOT EXISTS spot_semantic_state (
    spot_id INT PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,
    quietness_score REAL,
    safety_score REAL,
    police_risk_score REAL,
    beauty_score REAL,
    crowd_level_score REAL,
    overnight_safe BOOLEAN,
    stealth_score REAL,
    signals_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    semantic_dsl TEXT,
    summary_es TEXT,
    summary_en TEXT,
    tags TEXT[],
    best_for TEXT[],
    best_season TEXT,
    total_observations INT DEFAULT 0,
    consensus_confidence REAL DEFAULT 0.0,
    weight_support REAL DEFAULT 0.0,
    last_aggregated_at TIMESTAMPTZ DEFAULT NOW(),
    last_snapshot_data JSONB,
    stale BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sss_stale ON spot_semantic_state(stale) WHERE stale = TRUE;
CREATE INDEX IF NOT EXISTS idx_sss_filters ON spot_semantic_state(quietness_score, police_risk_score, crowd_level_score) WHERE stale = FALSE;

CREATE TABLE IF NOT EXISTS spot_semantic_snapshots (
    id BIGSERIAL PRIMARY KEY,
    spot_id INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    snapshot_date DATE NOT NULL,
    semantic_data JSONB NOT NULL,
    trigger_reason TEXT NOT NULL,
    semantic_distance REAL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(spot_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_sssn_lookup ON spot_semantic_snapshots(spot_id, snapshot_date DESC);

CREATE TABLE IF NOT EXISTS semantic_events (
    id BIGSERIAL PRIMARY KEY,
    spot_id INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    severity REAL NOT NULL,
    evidence_count INT NOT NULL DEFAULT 1,
    first_seen TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    evidence_claim_ids BIGINT[] NOT NULL,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(spot_id, event_type, first_seen)
);

CREATE INDEX IF NOT EXISTS idx_se_active ON semantic_events(spot_id, active) WHERE active = TRUE;

CREATE OR REPLACE VIEW spot_temperature AS
SELECT
    s.id,
    s.total_reviews,
    CASE
        WHEN s.total_reviews >= 10 THEN 'hot'
        WHEN s.total_reviews >= 3 THEN 'warm'
        ELSE 'cold'
    END AS temperature
FROM spots s
WHERE s.activo = TRUE;
