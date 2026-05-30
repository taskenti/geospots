-- ════════════════════════════════════════════════════════════════════
-- Migración: osm_pois — POIs locales extraídos de PBF (Sprint 4)
-- ════════════════════════════════════════════════════════════════════
-- Idempotente. Aplicar:
--   docker exec -i geospots-db psql -U geospots -d geospots -f - < db/migration_osm_pois.sql
--
-- Sprint 4 del plan docs/plan-verdad-geoespacial.md (PBF local).
--
-- En vez de depender de Overpass (frágil, rate-limited), importamos UN PBF de
-- país (Geofabrik) y extraemos solo las categorías que usamos a esta tabla.
-- Después geo_context.py consulta aquí con KNN local (<-> / ST_DWithin):
-- sin internet, sin 429, milisegundos por spot. El .pbf se puede borrar tras
-- el import (regenerable). jobs/import_osm_pbf.py la puebla con pyosmium.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS osm_pois (
    id        BIGSERIAL PRIMARY KEY,
    category  TEXT NOT NULL,                 -- drinking_water, dump_station, supermarket…
    geog      GEOGRAPHY(Point, 4326) NOT NULL,
    osm_type  TEXT,                          -- 'node' | 'way'
    osm_id    BIGINT,
    country   TEXT                           -- 'es', 'fr'… (de qué PBF vino)
);

-- KNN/proximidad por categoría: GIST sobre geog + filtro por category.
CREATE INDEX IF NOT EXISTS idx_osm_pois_geog ON osm_pois USING GIST (geog);
CREATE INDEX IF NOT EXISTS idx_osm_pois_cat  ON osm_pois (category);
CREATE INDEX IF NOT EXISTS idx_osm_pois_country ON osm_pois (country);
