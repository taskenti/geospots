-- ════════════════════════════════════════════════════════════════════
-- Migración: contexto de proximidad extensible (JSONB) — 1a + 1b
-- ════════════════════════════════════════════════════════════════════
-- Idempotente. Aplicar:
--   docker exec -i geospots-db psql -U geospots -d geospots -f - < db/migration_osm_nearby.sql
--
-- En vez de una columna dist_X_km por categoría (no escala con "muchas más
-- cosas cercanas"), guardamos diccionarios JSONB {categoria: km}:
--   - nearby_osm   : POIs de OSM cercanos (1a) — agua, super, panadería, playa…
--   - nearby_spots : NUESTROS spots cercanos por servicio/tipo (1b) — area_ac,
--                    camping, spot con vaciado…
-- Añadir una categoría nueva = 1 línea en geo_context.CATEGORIES, sin migración.
-- Las columnas DEM existentes (elevation, noise, dist_coast_km…) se conservan;
-- las puebla otro pipeline. Las dist_*_km de amenities que escribió el import
-- previo quedan obsoletas (se ignoran; ahora todo va a nearby_osm).
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS nearby_osm   JSONB;
ALTER TABLE spot_geo ADD COLUMN IF NOT EXISTS nearby_spots JSONB;
