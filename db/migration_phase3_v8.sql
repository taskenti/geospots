-- Migration Phase 3 v8 — Sprint 3 (temporal integrity)
-- BUG-10/17/22/31: marca observaciones cuya fecha NO es una fecha de publicación
-- real (review sin fecha, hecho scrapeado anclado al timestamp de ingesta, fecha
-- futura saneada). El agregador no les aplica recency boost y su peso ya viene
-- penalizado desde el normalizer.
-- Idempotente.

ALTER TABLE normalized_observations
    ADD COLUMN IF NOT EXISTS date_estimated BOOLEAN NOT NULL DEFAULT FALSE;

-- Índice parcial: las consultas de "evidencia fresca real" filtran date_estimated=FALSE.
CREATE INDEX IF NOT EXISTS idx_no_real_dates
    ON normalized_observations (spot_id, signal_type, observed_at DESC)
    WHERE date_estimated = FALSE;
