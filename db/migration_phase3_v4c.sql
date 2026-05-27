-- ═══════════════════════════════════════════════════════════════════
-- Migration Phase 3 v4c — Extra services rescued from source_records.raw_data
-- ═══════════════════════════════════════════════════════════════════
-- Idempotente. ADD COLUMN IF NOT EXISTS para todas las columnas nuevas.
-- No borra ni renombra nada. Backfill se hace por separado (jobs/backfill_extra_services.py).
--
-- Origen: cada scraper recoge 30-50 campos en raw_data pero solo
-- ~14 llegaban a spots. Esta migration añade ~20 columnas + 1 JSONB
-- para rescatar lo que ya tenemos en raw_data sin re-scrapear.

BEGIN;

-- ───────────────────────────────────────────────────────────────────
-- Amenidades booleanas (filter-friendly en API)
-- ───────────────────────────────────────────────────────────────────
ALTER TABLE spots ADD COLUMN IF NOT EXISTS piscina             BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS lavanderia          BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS gas_recharge        BOOLEAN;  -- gaz/gpl combinados
ALTER TABLE spots ADD COLUMN IF NOT EXISTS restaurant          BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS juegos_ninos        BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS mirador             BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS zona_protegida      BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS online_booking      BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS winter_friendly     BOOLEAN;  -- caravaneige
ALTER TABLE spots ADD COLUMN IF NOT EXISTS apto_motos          BOOLEAN;

-- ───────────────────────────────────────────────────────────────────
-- Actividades cercanas (filtros típicos)
-- ───────────────────────────────────────────────────────────────────
ALTER TABLE spots ADD COLUMN IF NOT EXISTS mtb_friendly        BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS surf_friendly       BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS fishing             BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS climbing            BOOLEAN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS hiking_nearby       BOOLEAN;

-- ───────────────────────────────────────────────────────────────────
-- Capacidad eléctrica y estancia
-- ───────────────────────────────────────────────────────────────────
ALTER TABLE spots ADD COLUMN IF NOT EXISTS amperaje            INT;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS n_enchufes          INT;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS max_noches          INT;

-- ───────────────────────────────────────────────────────────────────
-- Arrays (idiomas hablados, productos en venta para agroturismos)
-- ───────────────────────────────────────────────────────────────────
ALTER TABLE spots ADD COLUMN IF NOT EXISTS idiomas_hablados    TEXT[];
ALTER TABLE spots ADD COLUMN IF NOT EXISTS productos_venta     TEXT[];

-- ───────────────────────────────────────────────────────────────────
-- JSONB flexible para contenido descriptivo / breakdowns
-- ───────────────────────────────────────────────────────────────────
-- Esquema documentado (ver docs/DATA_MODEL.md):
--   {
--     "prohibitions": ["no fires", "no music after 22h"],
--     "risks": ["flood zone in spring"],
--     "descriptions": {
--       "sanitary": "...", "surroundings": "...",
--       "events": "...",   "special_info": "..."
--     },
--     "pricing_breakdown": {
--       "pernocta_min": 15, "pernocta_max": 30,
--       "servicios": 5, "electricidad_extra": 3,
--       "shower_token": 1, "tourist_tax": 0.5
--     },
--     "hours": {
--       "check_in": "16:00", "check_out": "12:00",
--       "days_closed": ["sunday"]
--     },
--     "nearby": {
--       "shops": ["bakery 200m"], "restaurants": ["pizzeria 100m"]
--     }
--   }
ALTER TABLE spots ADD COLUMN IF NOT EXISTS servicios_extras JSONB DEFAULT '{}'::jsonb;

-- ───────────────────────────────────────────────────────────────────
-- Índices para filtros y búsqueda
-- ───────────────────────────────────────────────────────────────────
-- Booleanos parciales (solo indexa TRUE — economiza espacio)
CREATE INDEX IF NOT EXISTS idx_spots_piscina         ON spots(piscina)         WHERE piscina        = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_lavanderia      ON spots(lavanderia)      WHERE lavanderia     = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_gas_recharge    ON spots(gas_recharge)    WHERE gas_recharge   = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_restaurant      ON spots(restaurant)      WHERE restaurant     = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_juegos_ninos    ON spots(juegos_ninos)    WHERE juegos_ninos   = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_mirador         ON spots(mirador)         WHERE mirador        = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_zona_protegida  ON spots(zona_protegida)  WHERE zona_protegida = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_winter_friendly ON spots(winter_friendly) WHERE winter_friendly= TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_mtb             ON spots(mtb_friendly)    WHERE mtb_friendly   = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_surf            ON spots(surf_friendly)   WHERE surf_friendly  = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_fishing         ON spots(fishing)         WHERE fishing        = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_climbing        ON spots(climbing)        WHERE climbing       = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_hiking          ON spots(hiking_nearby)   WHERE hiking_nearby  = TRUE;

-- GIN para arrays e JSONB (consultas tipo `idiomas_hablados @> ARRAY['en']`)
CREATE INDEX IF NOT EXISTS idx_spots_idiomas         ON spots USING GIN (idiomas_hablados);
CREATE INDEX IF NOT EXISTS idx_spots_productos       ON spots USING GIN (productos_venta);
CREATE INDEX IF NOT EXISTS idx_spots_servicios_extras ON spots USING GIN (servicios_extras);

-- Numéricos para filtros range
CREATE INDEX IF NOT EXISTS idx_spots_max_noches      ON spots(max_noches)      WHERE max_noches IS NOT NULL;

COMMIT;
