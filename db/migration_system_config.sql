-- Migración: tabla system_config para configuración dinámica del sistema.
-- Permite cambiar el provider LLM (y otros settings) sin reiniciar contenedores.
-- Idempotente.

BEGIN;

CREATE TABLE IF NOT EXISTS system_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT NOT NULL DEFAULT 'system'
);

-- Valores por defecto (solo si no existen ya)
INSERT INTO system_config (key, value, description) VALUES
    ('enrichment_provider',   'deepseek', 'Provider LLM activo: deepseek | gemini'),
    ('enrichment_model',      '',         'Modelo override (vacío = usar default del provider)'),
    ('enrichment_concurrency','8',        'Semáforo de llamadas LLM paralelas por run'),
    ('enrichment_max_cost_default', '2.0','Tope de coste USD por defecto para runs vía API')
ON CONFLICT (key) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_system_config_updated ON system_config(updated_at DESC);

COMMIT;
