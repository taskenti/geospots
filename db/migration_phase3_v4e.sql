-- ═══════════════════════════════════════════════════════════════════
-- Migration Phase 3 v4e — PR11: overrides temporales de campos canónicos
-- ═══════════════════════════════════════════════════════════════════
-- Idempotente. Añade spot_field_overrides para que el reconciliador exponga
-- estados temporales tipo "el agua lleva 2 semanas reportada como rota"
-- SIN modificar el valor canónico de spots.agua_potable (que sigue siendo
-- lo que dicen las fuentes objetivas).
--
-- Origen: PR11 audit "votación por mayoría + decay temporal". Las columnas
-- canónicas viven en spots, los overrides volátiles aquí. Un override
-- expira (active=FALSE) cuando expires_at < NOW().

BEGIN;

CREATE TABLE IF NOT EXISTS spot_field_overrides (
    id                  BIGSERIAL PRIMARY KEY,
    spot_id             INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,

    -- Columna de spots que está siendo "matizada" (agua_potable, electricidad…)
    field               TEXT NOT NULL,
    -- Snapshot del valor canónico cuando se creó el override (auditoría)
    canonical_value     BOOLEAN,
    -- Valor que la señal temporal afirma (típicamente FALSE = "no funciona ahora mismo")
    overridden_value    BOOLEAN NOT NULL,

    -- Razón legible (ej. "semantic_signal:water_working")
    reason              TEXT NOT NULL,
    -- ID del signal_type que disparó el override (FK soft — no es ON DELETE CASCADE
    -- para que un cambio en signal_types no rompa históricos)
    source_signal_type  TEXT,

    -- Métricas que llevaron a disparar el override (auditoría + debug)
    confidence          REAL NOT NULL,
    weight_support      REAL NOT NULL,
    n_observations      INT  NOT NULL,

    created_at          TIMESTAMPTZ DEFAULT NOW(),
    last_seen           TIMESTAMPTZ DEFAULT NOW(),  -- último reconciliador que lo reafirmó
    expires_at          TIMESTAMPTZ NOT NULL,        -- TTL = 1 × half_life_days del signal
    -- active es GENERATED — no necesita mantenimiento manual
    active              BOOLEAN GENERATED ALWAYS AS (expires_at > created_at) STORED,

    -- Un único override activo por (spot, campo, signal). Si llega de nuevo,
    -- ON CONFLICT UPDATE refresca last_seen / expires_at / métricas.
    UNIQUE (spot_id, field, source_signal_type)
);

-- Índices: el caso de uso principal es "dame overrides activos de este spot"
CREATE INDEX IF NOT EXISTS idx_sfo_spot_field   ON spot_field_overrides(spot_id, field);
CREATE INDEX IF NOT EXISTS idx_sfo_expires      ON spot_field_overrides(expires_at);

COMMIT;
