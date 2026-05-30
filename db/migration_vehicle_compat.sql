-- Sprint 1 — Modelo de compatibilidad de vehículos (físico).
-- Migración idempotente. Tabla REGENERABLE (derivada de raw_data/claims/OSM):
-- se puede borrar y rehacer sin tocar inmutables. NUNCA asumir valores: NULL = desconocido.
--
-- Diseño paramétrico: la tabla guarda las RESTRICCIONES del spot. El veredicto
-- apto/no_apto/desconocido se calcula AL VUELO contra las medidas del vehículo del usuario
-- (ver enrichment/vehicle_compat.py). Aquí NO se guarda ningún veredicto por perfil fijo.
--
-- Aplicar:  psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_vehicle_compat.sql

CREATE TABLE IF NOT EXISTS spot_vehicle_access (
    spot_id            INTEGER PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,

    -- Restricciones físicas máximas que admite el spot (metros / toneladas).
    -- NULL = desconocido. Un valor presente significa "no caben vehículos por encima de esto".
    max_length_m       REAL,
    max_height_m       REAL,
    max_width_m        REAL,
    max_weight_t       REAL,

    -- Tracción / terreno (tri-estado: TRUE / FALSE / NULL=desconocido).
    requires_4wd       BOOLEAN,          -- TRUE = acceso solo 4x4; FALSE = no necesario
    steep_access       BOOLEAN,          -- TRUE = aproximación con pendiente fuerte
    surface            TEXT,             -- paved|gravel|dirt|sand|grass|mixed|unknown
    access_difficulty  TEXT,             -- easy|moderate|hard|4x4_only (resumen derivado)

    -- Confianza global [0..1] y por campo (cada señal trae su propia confianza).
    confidence         REAL DEFAULT 0.0,
    field_confidence   JSONB DEFAULT '{}'::jsonb,   -- {"max_height_m":0.9,"requires_4wd":0.4}

    -- Procedencia regenerable: qué evidencia produjo cada valor (Tier-1 raw, OSM, reseña...).
    evidence           JSONB DEFAULT '{}'::jsonb,   -- {"max_height_m":{"value":2.0,"src":"park4night.hauteur_limite"}}

    -- Control / versionado del recompute.
    version            INTEGER DEFAULT 1,
    computed_at        TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT surface_valido CHECK (
        surface IS NULL OR surface IN ('paved','gravel','dirt','sand','grass','mixed','unknown')
    ),
    CONSTRAINT difficulty_valido CHECK (
        access_difficulty IS NULL OR access_difficulty IN ('easy','moderate','hard','4x4_only')
    )
);

-- Índices para filtrado por restricción (parcial: solo filas con dato, el resto es desconocido).
CREATE INDEX IF NOT EXISTS idx_sva_max_height ON spot_vehicle_access(max_height_m) WHERE max_height_m IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sva_max_length ON spot_vehicle_access(max_length_m) WHERE max_length_m IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sva_requires_4wd ON spot_vehicle_access(requires_4wd) WHERE requires_4wd IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sva_difficulty ON spot_vehicle_access(access_difficulty) WHERE access_difficulty IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sva_stale ON spot_vehicle_access(computed_at);

COMMENT ON TABLE spot_vehicle_access IS
  'Sprint 1: restricciones físicas de acceso por spot (regenerable). NULL=desconocido. '
  'El veredicto por vehículo se calcula al vuelo en enrichment/vehicle_compat.py.';
