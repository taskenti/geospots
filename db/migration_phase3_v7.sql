-- ═══════════════════════════════════════════════════════════════════════
-- Migration Phase 3 — v7 (T2.6): spot_relations (relaciones spot↔spot)
-- ═══════════════════════════════════════════════════════════════════════
--
-- Idempotente. Aplicar con:
--   psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_phase3_v7.sql
--
-- El LLM (orchestrator_v2 / v4) emite `cross_references[]` cuando una review
-- menciona OTRO lugar ("parking del telesilla", "River shopping center nearby").
-- El postproceso (enrichment/relation_resolver.py) resuelve cada mención a un
-- spot_id real vía proximidad geográfica + similitud de nombre (pg_trgm) antes
-- de insertar aquí.
--
-- REGENERABLE: las relaciones con source='llm_review_inference' | 'geo_proximity'
-- se pueden borrar y rehacer desde reviews/spots. Las 'manual' son curadas a mano
-- (no las borra el reproceso).
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS spot_relations (
    spot_id         BIGINT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    related_spot_id BIGINT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    relation_type   TEXT   NOT NULL,
        -- 'alternative_overnight' | 'service_provider_for'
        -- | 'parking_for_visit' | 'same_complex' | 'walking_distance'
    distance_m      INT,
    bidirectional   BOOLEAN DEFAULT FALSE,
    confidence      NUMERIC(3,2),
    source          TEXT,  -- 'llm_review_inference' | 'manual' | 'geo_proximity'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (spot_id, related_spot_id, relation_type),
    -- Un spot no se relaciona consigo mismo.
    CONSTRAINT spot_relations_no_self CHECK (spot_id <> related_spot_id)
);

COMMENT ON TABLE spot_relations IS
    'T2.6: relaciones dirigidas spot→spot inferidas por el LLM (cross_references) '
    'o por proximidad geográfica. Resueltas en enrichment/relation_resolver.py.';
COMMENT ON COLUMN spot_relations.relation_type IS
    'alternative_overnight | service_provider_for | parking_for_visit | same_complex | walking_distance';
COMMENT ON COLUMN spot_relations.source IS
    'llm_review_inference | manual | geo_proximity. Solo manual sobrevive a un reproceso.';
COMMENT ON COLUMN spot_relations.bidirectional IS
    'TRUE si la relación es simétrica (same_complex, walking_distance). El resolver '
    'puede insertar la inversa cuando aplique.';

-- Lookup inverso: "¿qué spots apuntan a este?" (related_spot_id ya no es PK leftmost).
CREATE INDEX IF NOT EXISTS idx_spot_relations_related
    ON spot_relations (related_spot_id);

CREATE INDEX IF NOT EXISTS idx_spot_relations_type
    ON spot_relations (relation_type);
