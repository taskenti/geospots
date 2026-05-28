-- Migration Phase 3 v6d — T2.2: estados intermedios del lifecycle de alertas
-- =========================================================================
-- Añade un estado `decaying` entre `active` y `likely_resolved`, derivado de
-- forma determinista desde (confidence, resolved). NO añade columna almacenada:
-- el estado es regenerable (función pura), igual que en `enrichment/state_resolver.py`.
--
-- Espejo SQL de:
--   state_resolver.lifecycle_state(confidence, resolved)
--   state_resolver.lifecycle_rank_weight(state)
-- Mantener AMBOS sincronizados. Umbrales:
--   resolved=TRUE                         -> 'likely_resolved'  (peso 0.0)
--   resolved=FALSE AND confidence < 0.50  -> 'decaying'         (peso 0.5)
--   resolved=FALSE AND confidence >= 0.50 -> 'active'           (peso 1.0)
--
-- Idempotente: CREATE OR REPLACE.
-- =========================================================================

BEGIN;

CREATE OR REPLACE FUNCTION alert_lifecycle_state(p_confidence NUMERIC, p_resolved BOOLEAN)
RETURNS TEXT AS $$
BEGIN
    IF p_resolved THEN
        RETURN 'likely_resolved';
    ELSIF p_confidence < 0.50 THEN
        RETURN 'decaying';
    ELSE
        RETURN 'active';
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION alert_lifecycle_state IS
    'T2.2: deriva el estado del lifecycle (active|decaying|likely_resolved) desde confidence+resolved. Espejo de state_resolver.lifecycle_state.';

CREATE OR REPLACE FUNCTION alert_rank_weight(p_state TEXT)
RETURNS NUMERIC AS $$
BEGIN
    RETURN CASE p_state
        WHEN 'active' THEN 1.0
        WHEN 'decaying' THEN 0.5
        ELSE 0.0           -- likely_resolved o desconocido
    END;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION alert_rank_weight IS
    'T2.2: peso de ranking [0,1] por estado del lifecycle. Espejo de state_resolver.lifecycle_rank_weight.';

COMMIT;

-- ─────────────────────────────────────────────────────────────────────
-- Smoke test post-migration (manual)
-- ─────────────────────────────────────────────────────────────────────
-- SELECT alert_lifecycle_state(0.80, FALSE) AS s1,   -- active
--        alert_lifecycle_state(0.40, FALSE) AS s2,   -- decaying
--        alert_lifecycle_state(0.20, TRUE)  AS s3;   -- likely_resolved
-- SELECT alert_lifecycle_state(confidence, resolved) AS lifecycle_state,
--        COUNT(*)
-- FROM spot_alerts GROUP BY 1 ORDER BY 1;
