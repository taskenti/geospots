-- ═══════════════════════════════════════════════════════════════════
-- Migration Phase 3 v4 — Spot popularity_score + reliability_score
-- ═══════════════════════════════════════════════════════════════════
-- Idempotente. Añade 2 columnas computadas + índices para sort/filter.
-- El cómputo lo hace jobs/recompute_spot_metrics.py (no trigger, ver razón abajo).
--
-- popularity_score (0.0-1.0):
--   - Mide cómo de transitado/conocido es un spot.
--   - useful para intents opuestos:
--      * "spot tranquilo aislado"   → buscar low  popularity
--      * "camping seguro con vibes" → buscar high popularity
--
-- reliability_score (0.0-1.0):
--   - Mide cuánto podemos fiarnos de que el spot existe y los datos son fiables.
--   - Spot con 1 fuente sin reviews → score bajo (puede ser fantasma).
--   - Spot con 4 fuentes + reviews recientes → score alto.
--
-- ¿Por qué no trigger? Recalcular avg(source_credibility) y EXISTS en cada
-- insert de review es costoso. Mejor recompute batch diario o por --spot-id
-- on demand. Los scores cambian lentamente; un día de delay es aceptable.

BEGIN;

ALTER TABLE spots ADD COLUMN IF NOT EXISTS popularity_score  REAL;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS reliability_score REAL;

COMMENT ON COLUMN spots.popularity_score IS
    '0-1 score: how trafficked/known a spot is. Combines total_reviews (log-scaled, 50% weight), num_fuentes (log-scaled, 30%), and recency of last review (20%). Recomputed periodically by jobs/recompute_spot_metrics.py';

COMMENT ON COLUMN spots.reliability_score IS
    '0-1 score: how trustworthy the spot data is. Combines avg source credibility (40%), source count (30%), has_any_review (20%), has_recent_review (10%). Recomputed periodically by jobs/recompute_spot_metrics.py';

-- Índices para sort y filter típicos del API
CREATE INDEX IF NOT EXISTS idx_spots_popularity
    ON spots(popularity_score DESC NULLS LAST)
    WHERE activo = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_reliability
    ON spots(reliability_score DESC NULLS LAST)
    WHERE activo = TRUE;

-- Útil para queries tipo "spots tranquilos con baja popularidad cerca de X"
CREATE INDEX IF NOT EXISTS idx_spots_low_popularity
    ON spots(popularity_score ASC NULLS LAST)
    WHERE activo = TRUE AND popularity_score IS NOT NULL;

COMMIT;
