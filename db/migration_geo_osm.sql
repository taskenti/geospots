-- ════════════════════════════════════════════════════════════════════
-- Migración: contexto de proximidad OSM para spot_geo (Sprint 3)
-- ════════════════════════════════════════════════════════════════════
-- Idempotente. Aplicar:
--   psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_geo_osm.sql
--
-- Sprint 3 del plan docs/plan-verdad-geoespacial.md (motor geoespacial OSM).
--
-- spot_geo ya tenía columnas DEM/servicios en km (dist_supermarket_km,
-- dist_fuel_km, dist_coast_km, ...). Aquí añadimos las proximidades específicas
-- de camper que faltaban, en las MISMAS unidades (km) para consistencia con la
-- ficha viajero. Se pueblan vía Overpass desde scraper/geo_context.py (piloto ES).
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS dist_drinking_water_km REAL;
ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS dist_dump_station_km   REAL;
ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS dist_pharmacy_km       REAL;
ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS dist_viewpoint_km      REAL;

-- `source` ya existe (migration_phase3_v6) para distinguir DEM/OSM/LLM.
-- Índice para la query de cobertura del panel admin.
CREATE INDEX IF NOT EXISTS idx_spot_geo_processed
    ON spot_geo(processed_at) WHERE processed_at IS NOT NULL;
