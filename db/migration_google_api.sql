-- ════════════════════════════════════════════════════════════════════
-- Migración: Google Maps Places API (New) como enriquecedor de contacto
-- ════════════════════════════════════════════════════════════════════
-- Idempotente. Aplicar sobre DB existente:
--   psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_google_api.sql
--
-- Objetivo: integrar la Places API (New) como fuente de ENRIQUECIMIENTO
-- dirigido (no scraping masivo) para rellenar contacto (telefono, web,
-- direccion) y rating en campings / áreas AC ya existentes en la base
-- canónica. Las reseñas quedan en punto muerto (la API no devuelve texto
-- masivo y su scraping viola los TOS de Google).
-- ════════════════════════════════════════════════════════════════════

-- 1. Columnas nuevas en spots ----------------------------------------
ALTER TABLE spots ADD COLUMN IF NOT EXISTS direccion_formateada TEXT;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS google_place_id      TEXT;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS google_last_refreshed TIMESTAMPTZ;

-- place_id es estable y único por POI físico en Google. Índice parcial
-- (solo filas con valor) para idempotencia y lookups rápidos. No usamos
-- UNIQUE constraint duro porque un match impreciso podría, en teoría,
-- asignar el mismo place_id a dos spots distintos; preferimos detectarlo
-- por índice + lógica de match que romper una transacción de enriquecimiento.
CREATE INDEX IF NOT EXISTS idx_spots_google_place_id
    ON spots(google_place_id) WHERE google_place_id IS NOT NULL;

-- Cola de candidatos: spots tipo camping/area_ac sin contacto completo y
-- aún no intentados (o ya stale). Acelera la query de priorización del job.
CREATE INDEX IF NOT EXISTS idx_spots_gmaps_candidates
    ON spots(tipo, google_last_refreshed)
    WHERE activo = TRUE
      AND google_place_id IS NULL
      AND tipo IN ('camping', 'area_ac');

-- 2. Registro de credibilidad ----------------------------------------
-- base_score alto: Google es muy fiable para contacto/dirección/rating.
-- review_quality = 0 porque NO aportamos reviews desde esta fuente.
INSERT INTO source_credibility (source, display_name, base_score, review_quality, coverage_region, notes)
VALUES (
    'google_maps_api', 'Google Maps (Places API)', 0.90, 0.00, ARRAY['WW'],
    'Enriquecimiento dirigido de contacto (telefono/web/direccion) y rating. Sin reviews.'
)
ON CONFLICT (source) DO UPDATE SET
    display_name   = EXCLUDED.display_name,
    base_score     = EXCLUDED.base_score,
    review_quality = EXCLUDED.review_quality,
    coverage_region = EXCLUDED.coverage_region,
    notes          = EXCLUDED.notes;

-- 3. Registro de fuente (estado de scrapers) -------------------------
INSERT INTO fuentes_config (nombre, activa, notas)
VALUES ('google_maps_api', TRUE,
        'Google Places API (New) — enriquecimiento dirigido de contacto. Respeta GOOGLE_MAPS_DAILY_BUDGET.')
ON CONFLICT (nombre) DO NOTHING;
