-- Migración idempotente: normaliza spots.country_iso a MINÚSCULAS y garantiza
-- que cualquier escritura futura quede en minúsculas, venga del path que venga.
--
-- CONTEXTO (BUG-COUNTRY-CASE, 2026-05-29)
-- ---------------------------------------------------------------------------
-- El trigger de geoclasificación `trg_classify_spot` solo dispara
-- `BEFORE INSERT OR UPDATE OF lat, lon` y normaliza country_iso a minúsculas.
-- Pero cualquier path que escriba country_iso SIN tocar lat/lon (reconciliación,
-- un scraper que UPDATE el valor crudo de la fuente, un INSERT con country_iso
-- pero lat/lon que caen fuera de los polígonos `countries`) deja el valor tal
-- cual. Resultado medido: ~32.5K filas activas en MAYÚSCULAS (FR=30.8K, NO=1.6K…).
-- Esto hacía que filtros como `country_iso = ANY(['fr'])` se saltaran esas filas.
--
-- Solución en dos partes:
--   1. UPDATE puntual que arregla las filas existentes.
--   2. Trigger dedicado `BEFORE INSERT OR UPDATE OF country_iso` que fuerza
--      minúsculas en TODA escritura, independiente de la geoclasificación.
-- ---------------------------------------------------------------------------

BEGIN;

-- 1. Arreglar datos existentes (solo las filas que lo necesitan).
UPDATE spots
   SET country_iso = lower(country_iso)
 WHERE country_iso IS NOT NULL
   AND country_iso <> lower(country_iso);

-- 2. Trigger dedicado: country_iso SIEMPRE en minúsculas.
CREATE OR REPLACE FUNCTION fn_lowercase_country_iso()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.country_iso IS NOT NULL THEN
        NEW.country_iso := lower(NEW.country_iso);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_lowercase_country_iso ON spots;

-- Nombre con prefijo 'a_' para que el orden alfabético de Postgres lo ejecute
-- ANTES de trg_classify_spot en un INSERT (la geoclasificación ya emite LOWER,
-- así que el orden no afecta al resultado, pero lo dejamos determinista).
CREATE TRIGGER a_trg_lowercase_country_iso
BEFORE INSERT OR UPDATE OF country_iso ON spots
FOR EACH ROW
EXECUTE FUNCTION fn_lowercase_country_iso();

COMMIT;

-- Verificación (no rompe la migración; informativo):
--   SELECT count(*) FROM spots WHERE country_iso <> lower(country_iso);  -- debe ser 0
