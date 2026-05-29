-- Migración idempotente: añade fuentes_config.has_reviews_support
-- Marca qué fuentes implementan download_reviews() (lo puebla scraper/sync_db.py).
-- El PWA de scrapers usa esta columna para mostrar el botón "Reviews" aunque la
-- fuente todavía tenga 0 reviews en la DB (rompe el círculo vicioso del antiguo
-- heurístico total_reviews > 0).

ALTER TABLE fuentes_config
    ADD COLUMN IF NOT EXISTS has_reviews_support BOOLEAN DEFAULT FALSE;
