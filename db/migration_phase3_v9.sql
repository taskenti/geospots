-- Migration Phase 3 v9 — Sprint 5 (BUG-37 / BUG-30 guard)
-- Evita la DUPLICACIÓN de claims en re-enriquecimiento spot-level (orchestrator_v2).
--
-- Causa (BUG-30): re-correr orchestrator_v2 con otra ENRICHMENT_VERSION (v4 -> v6)
-- reinserta el mismo claim por (spot, señal, review) inflando confidence. No había
-- constraint que lo impidiera (BUG-37).
--
-- Diseño del índice (IMPORTANTE — respeta la regla de inmutabilidad de CLAUDE.md):
--   * Solo cubre los extractores spot-level del orchestrator ('%_spot_v2').
--   * NO toca 'scraped_facts_v1': esos claims (review_id NULL) vienen de
--     MÚLTIPLES source_records distintos y son inmutables — colapsarlos perdería
--     corroboración multi-fuente.
--   * COALESCE(review_id,-1) trata los NULL (claims desde descripciones) como
--     iguales, de modo que dos runs sobre la misma descripción colisionan.
--   * extractor_version queda FUERA de la clave: por eso v4 y v6 colisionan
--     (mismo extractor_name) y la segunda inserción se ignora (idempotente).
--
-- PRE-REQUISITO: si ya existen duplicados en la tabla, la creación del índice
-- FALLARÁ. Ejecutar ANTES el dedup:  python -m jobs.dedup_claims --apply
-- (por defecto es dry-run). Ver jobs/dedup_claims.py.
--
-- Idempotente (IF NOT EXISTS).

CREATE UNIQUE INDEX IF NOT EXISTS uq_ec_orchestrator_claim
    ON extracted_claims (spot_id, signal_type, extractor_name, COALESCE(review_id, -1))
    WHERE extractor_name IN ('gemini_spot_v2', 'deepseek_spot_v2');
