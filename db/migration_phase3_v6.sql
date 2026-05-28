-- Migration Phase 3 v6 — Sprint 2 del plan de hardening pre-batch
-- =========================================================================
-- Cubre tres tasks del plan en una sola migración para que el rollback sea atómico:
--   T1.4  — Tabla `spot_alerts` con lifecycle (alertas tipadas con decay).
--   T1.4b — Clasificación funcional del spot:
--           `spot_function`, `is_overnight_viable`, `authorization_status` en `spots`.
--           Columna `source` en `spot_geo` (para distinguir DEM/OSM/LLM).
--   T1.4c — Materializadas en `spot_semantic_state`:
--           `active_alert_types TEXT[]` + GIN, `signal_flux JSONB`.
--
-- También deja preparado el terreno (NO entra ahora) para tasks del Sprint 3:
--   T1.5  — `canonical_tags`, `unknown_tags`           [Sprint 2.5]
--   T1.6  — `semantic_fingerprint`, `built_from_fingerprint`  [Sprint 3]
--   T1.8  — `llm_call_metrics`                          [Sprint 3]
--   T2.7  — trigger `stale` en normalized_observations  [Sprint 3]
--
-- Esta migración SOLO añade T1.4/T1.4b/T1.4c. Los demás van en migration_phase3_v6b o
-- en bloques separados — preferimos ENVÍOS PEQUEÑOS reproducibles. (Decisión: el "single
-- migration v6" del plan se relaja a "migraciones v6/v6b/v6c agrupadas en Sprint 2/3"
-- porque mezclar T1.5 con T1.4 acopla dos features que pueden fallar independientemente).
--
-- Idempotente: usa `IF NOT EXISTS` y guards en plpgsql.
-- =========================================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────────
-- T1.4 — spot_alerts
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS spot_alerts (
    id BIGSERIAL PRIMARY KEY,
    spot_id BIGINT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,

    -- 'construction' | 'closed_season' | 'access_restricted'
    -- | 'temporary_ban' | 'natural_hazard' | 'event_overflow'
    -- | 'permanently_closed' | 'permanent_*'  (permanent_* no decae)
    alert_type TEXT NOT NULL,

    severity NUMERIC(3,2) NOT NULL CHECK (severity >= 0 AND severity <= 1),

    detected_at TIMESTAMPTZ NOT NULL,            -- fecha de la review/source que lo originó
    valid_from DATE NOT NULL,
    valid_until DATE,                            -- NULL = indefinido / hasta resolución

    confidence NUMERIC(3,2) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),

    source_observations BIGINT[] DEFAULT '{}',   -- FKs a normalized_observations
    source_review_ids BIGINT[] DEFAULT '{}',     -- FKs a reviews (mismo array que el LLM emite)

    -- 'llm_v4' | 'llm_v5' | 'llm_v6' | 'scraped_facts' | 'manual'
    detected_by TEXT NOT NULL,

    summary TEXT,                                -- 1-2 frases en inglés (el LLM ya devuelve summary)

    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    last_decay_at TIMESTAMPTZ,                   -- última vez que apply_decay tocó la fila
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Una alerta del MISMO tipo puede tener múltiples filas (una por evento detectado),
    -- pero NO queremos duplicados activos del mismo evento — usamos source_review_ids
    -- como descriminador. El resolver dedupe en código antes de insertar.
    CHECK (valid_until IS NULL OR valid_until >= valid_from)
);

-- Activas por spot — para que la API filtre sin scan
CREATE INDEX IF NOT EXISTS idx_spot_alerts_active
    ON spot_alerts (spot_id) WHERE resolved = FALSE;

-- Por tipo + validez — para cron de decay y para "todos los spots con obras activas"
CREATE INDEX IF NOT EXISTS idx_spot_alerts_type_validity
    ON spot_alerts (alert_type, valid_until) WHERE resolved = FALSE;

-- GIN sobre alert_type — para queries "WHERE 'construction' = ANY(...)" cuando agreguemos
-- texto libre en futuras versiones. Por ahora alert_type es scalar, así que un BTREE
-- alcanza; mantenemos el índice del plan original como BTREE plain.
CREATE INDEX IF NOT EXISTS idx_spot_alerts_type_btree
    ON spot_alerts (alert_type) WHERE resolved = FALSE;

-- Por detected_by — para auditar cuántas alertas vienen del LLM vs. scraped_facts vs. manual
CREATE INDEX IF NOT EXISTS idx_spot_alerts_detected_by
    ON spot_alerts (detected_by) WHERE resolved = FALSE;

COMMENT ON TABLE spot_alerts IS
    'T1.4: alertas tipadas con lifecycle (decay 0.85^meses + guarda 180d). Sustituye la idea de spot_operational_state. Multi-fila por spot.';
COMMENT ON COLUMN spot_alerts.last_decay_at IS
    'Última vez que el cron de decay tocó la fila. NULL = aún sin decay aplicado (alerta nueva).';
COMMENT ON COLUMN spot_alerts.source_review_ids IS
    'Reviews que originaron la alerta. El resolver determinista las usa como discriminador para evitar duplicados activos.';

-- ─────────────────────────────────────────────────────────────────────
-- T1.4b — Clasificación funcional en `spots` + source en `spot_geo`
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE spots
    ADD COLUMN IF NOT EXISTS spot_function TEXT;
-- Valores admitidos (no se enforza con CHECK para no bloquear la migración con datos legacy
-- — la validación vive en el parser/ingest):
--   'overnight_primary' | 'overnight_tolerated' | 'service_only'
--   | 'shop_workshop' | 'transit' | 'daytime_only'
COMMENT ON COLUMN spots.spot_function IS
    'T1.4b: clasificación funcional emitida por LLM v6 (overnight_primary, shop_workshop, service_only…). NULL = no determinada.';

ALTER TABLE spots
    ADD COLUMN IF NOT EXISTS is_overnight_viable BOOLEAN;
COMMENT ON COLUMN spots.is_overnight_viable IS
    'T1.4b: ¿se puede dormir aquí razonablemente? NULL = no determinada. False = no (taller, parking diurno, etc).';

ALTER TABLE spots
    ADD COLUMN IF NOT EXISTS authorization_status TEXT;
-- Valores admitidos: 'official' | 'tolerated' | 'sign_authorized' | 'illegal' | 'unknown'
COMMENT ON COLUMN spots.authorization_status IS
    'T1.4b: estado legal de la pernocta. NULL = no determinada.';

CREATE INDEX IF NOT EXISTS idx_spots_function ON spots (spot_function) WHERE spot_function IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_spots_overnight_viable ON spots (is_overnight_viable) WHERE is_overnight_viable IS NOT NULL;

-- spot_geo: añadir columna source para saber de dónde viene cada fila
ALTER TABLE spot_geo
    ADD COLUMN IF NOT EXISTS source TEXT;
-- Valores: 'llm_v6' | 'dem' | 'osm' | 'manual'
COMMENT ON COLUMN spot_geo.source IS
    'T1.4b/D8: origen de los valores geofísicos. llm_v6 mientras Phase 6 no ejecute DEM/OSM analysis.';

-- ─────────────────────────────────────────────────────────────────────
-- T1.4c — Materializadas en spot_semantic_state
-- ─────────────────────────────────────────────────────────────────────

ALTER TABLE spot_semantic_state
    ADD COLUMN IF NOT EXISTS active_alert_types TEXT[] NOT NULL DEFAULT '{}';
COMMENT ON COLUMN spot_semantic_state.active_alert_types IS
    'T1.4c: array materializado de alert_type WHERE resolved=FALSE. Se recomputa en cada update del estado.';

ALTER TABLE spot_semantic_state
    ADD COLUMN IF NOT EXISTS signal_flux JSONB NOT NULL DEFAULT '{}'::jsonb;
COMMENT ON COLUMN spot_semantic_state.signal_flux IS
    'T1.4c (reserva): cambios de régimen detectados (T2.5). Schema: {"<signal>": {"changed": bool, "old": num, "new": num, "since": "YYYY-MM-DD", "n_recent": int}}. Vacío hasta T2.5.';

-- GIN para que /search/semantic pueda filtrar "WHERE NOT 'construction' = ANY(active_alert_types)"
CREATE INDEX IF NOT EXISTS idx_sss_active_alert_types_gin
    ON spot_semantic_state USING GIN (active_alert_types);

-- ─────────────────────────────────────────────────────────────────────
-- Helpers de sincronía (consumidos por enrichment/state_aggregator)
-- ─────────────────────────────────────────────────────────────────────

-- Función helper: recalcular active_alert_types de un spot a partir de spot_alerts.
-- La invoca el ingest_v2 tras upsert de alerts y el cron de decay tras marcar resolved.
CREATE OR REPLACE FUNCTION refresh_active_alert_types(p_spot_id BIGINT)
RETURNS VOID AS $$
BEGIN
    UPDATE spot_semantic_state sss
    SET active_alert_types = COALESCE(
        (SELECT array_agg(DISTINCT alert_type ORDER BY alert_type)
         FROM spot_alerts
         WHERE spot_id = p_spot_id AND resolved = FALSE),
        '{}'::TEXT[]
    )
    WHERE sss.spot_id = p_spot_id;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION refresh_active_alert_types IS
    'T1.4c: recalcula active_alert_types desde spot_alerts. Llamarlo desde ingest_v2 tras INSERT/UPDATE de alerts y desde el cron de decay tras marcar resolved.';

COMMIT;

-- ─────────────────────────────────────────────────────────────────────
-- Smoke test post-migration (manual)
-- ─────────────────────────────────────────────────────────────────────
-- SELECT 'spot_alerts'    AS tbl, COUNT(*) AS n FROM spot_alerts
-- UNION ALL SELECT 'spots.spot_function', COUNT(*) FROM spots WHERE spot_function IS NOT NULL
-- UNION ALL SELECT 'sss.active_alert_types non-empty',
--                  COUNT(*) FROM spot_semantic_state WHERE cardinality(active_alert_types) > 0;
