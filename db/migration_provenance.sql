-- ════════════════════════════════════════════════════════════════════
-- Migración: spot_field_provenance — procedencia y confianza por campo
-- ════════════════════════════════════════════════════════════════════
-- Idempotente. Aplicar sobre DB existente:
--   psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_provenance.sql
--
-- Sprint 0 del plan docs/plan-verdad-geoespacial.md.
--
-- Problema: reconciliar.py ya calcula voto ponderado, margen de desempate,
-- fuentes de soporte y detección de conflicto — pero descarta esa metadata al
-- escribir un valor plano en spots. Esta tabla lateral la persiste sin tocar el
-- hot path (spots sigue con columnas planas indexadas para SQL/PostGIS/embeddings).
--
-- Diseño: una fila por (spot_id, field). Solo se pueblan campos de alto valor /
-- dinámicos (PROVENANCE_FIELDS en reconciliar.py) y solo para spots multifuente.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS spot_field_provenance (
    spot_id            INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    -- Columna canónica de spots que esta fila documenta (web, telefono, ...)
    field              TEXT NOT NULL,
    -- Valor ganador (texto, truncado a 500 chars para evitar bloating)
    winning_value      TEXT,
    -- Confianza del DATO (no del spot):
    --   voto ponderado → cuota de peso del ganador (winner_w / total) ∈ (0,1]
    --   rank-first      → base_score de la fuente ganadora
    confidence         REAL NOT NULL DEFAULT 1.0,
    -- Margen de consenso (winner_w - second_w)/total. NULL para campos rank-first.
    consensus_margin   REAL,
    -- Fuentes que respaldan el valor ganador (para atribución y explicación LLM)
    supporting_sources TEXT[] DEFAULT '{}',
    -- Hubo más de un valor distinto entre fuentes para este campo
    conflict_detected  BOOLEAN DEFAULT FALSE,
    updated_at         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (spot_id, field)
);

CREATE INDEX IF NOT EXISTS idx_sfp_spot
    ON spot_field_provenance(spot_id);

-- Acelera la cola de "conflictos activos de alto valor" que consume el
-- desempatador de Google (Sprint 1).
CREATE INDEX IF NOT EXISTS idx_sfp_conflict
    ON spot_field_provenance(field, conflict_detected)
    WHERE conflict_detected = TRUE;
