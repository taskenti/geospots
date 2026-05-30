-- ════════════════════════════════════════════════════════════════════
-- Migración: claves de entidad para resolución de duplicados (Sprint 2)
-- ════════════════════════════════════════════════════════════════════
-- Idempotente. Aplicar:
--   psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_entity_keys.sql
--
-- Sprint 2 del plan docs/plan-verdad-geoespacial.md (entity resolution).
--
-- Añade claves normalizadas que sirven como SEÑALES de identidad entre fuentes:
--   - telefono_norm : teléfono normalizado (solo dígitos + prefijo), para agrupar.
--   - web_domain    : dominio raíz de la web (sin agregadores), para agrupar.
--   - osm_id        : id del elemento OSM (ancla de identidad exacta).
--
-- Regla del Sprint 2: estas claves se usan como SEÑAL FUERTE, nunca como verdad
-- absoluta. place_id/osm_id (únicos por entidad) sí pueden auto-anclar en
-- find_spot_cercano; telefono/web son solo señal de AUDITORÍA manual (un teléfono
-- o dominio compartido por una cadena provocaría falsos merges).
--
-- Columnas REGENERABLES (derivadas): poblar con jobs/backfill_entity_keys.py.
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE spots ADD COLUMN IF NOT EXISTS telefono_norm TEXT;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS web_domain    TEXT;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS osm_id        TEXT;

-- Índices parciales para el agrupado de candidatos de duplicado y el
-- anclaje por osm_id en find_spot_cercano.
CREATE INDEX IF NOT EXISTS idx_spots_telefono_norm
    ON spots(telefono_norm) WHERE telefono_norm IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_spots_web_domain
    ON spots(web_domain) WHERE web_domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_spots_osm_id
    ON spots(osm_id) WHERE osm_id IS NOT NULL;
