-- ═══════════════════════════════════════════════════════════════════
-- Migration Phase 3 v4d — Hallazgos Capa 1 audit (PR 8f → 8g)
-- ═══════════════════════════════════════════════════════════════════
-- Idempotente. ADD COLUMN IF NOT EXISTS para columnas que faltaban.
--
-- Origen: auditoría capa 1 detectó que raw_data ya contenía estos campos
-- (902 spots CCP con securiplace, 605 agricamper con est_chemin_*, 64k
-- caramaps con contactInformation.email tirado a la basura, etc.) pero
-- los extractores no los recogían. Esta migration añade SOLO las columnas
-- nuevas; las que ya existían (seguridad, email, temporada_apertura,
-- municipio, acceso_grandes, iluminacion) se reutilizan.

BEGIN;

-- ───────────────────────────────────────────────────────────────────
-- Acceso / accesibilidad
-- ───────────────────────────────────────────────────────────────────

-- Camino difícil / con pendiente / pedregoso (agricamper.est_chemin_*).
-- OR de los 3 flags: si CUALQUIERA es true, marcamos acceso_dificil=true.
-- Critical para usuarios con vehículos grandes o conductores nerviosos.
ALTER TABLE spots ADD COLUMN IF NOT EXISTS acceso_dificil BOOLEAN;

-- Accesibilidad para movilidad reducida (agricamper.accepte_handicap,
-- ccp/p4n no exponen este campo aún).
ALTER TABLE spots ADD COLUMN IF NOT EXISTS accesibilidad_reducida BOOLEAN;

-- Acepta caravanas — DIFERENTE de acceso_grandes (que es para autocaravanas
-- grandes). Una caravana remolcada tiene maniobrabilidad y dimensiones
-- distintas a un camper autopropulsado. agricamper.accepte_caravanes lo
-- expone explícitamente.
ALTER TABLE spots ADD COLUMN IF NOT EXISTS acepta_caravanas BOOLEAN;

-- ───────────────────────────────────────────────────────────────────
-- Índices parciales (solo TRUE — economiza espacio)
-- ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_spots_acceso_dificil       ON spots(acceso_dificil)         WHERE acceso_dificil = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_accesibilidad        ON spots(accesibilidad_reducida) WHERE accesibilidad_reducida = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_acepta_caravanas     ON spots(acepta_caravanas)       WHERE acepta_caravanas = TRUE;

COMMIT;
