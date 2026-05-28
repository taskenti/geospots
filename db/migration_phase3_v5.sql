-- Migration Phase 3 v5: nuevas señales + regex expandido
-- Idempotente: ON CONFLICT DO UPDATE garantiza que es seguro correrlo varias veces.
-- Fecha: 2026-05-27
--
-- Cambios en esta versión:
--   - 9 nuevas señales: dark_sky, beach_access, river_nearby, hiking_nearby,
--     cycling_nearby, height_restriction, shower_working, spot_closed, youth_trouble
--   - PATTERNS de regex en claim_extractor.py expandidos de 26 a ~60 entradas
--   - Lógica de short-circuit corregida: LLM se activa si texto ≥120 chars y <3 claims regex
--   - text_trimmer.py: elimina filler antes del LLM
--   - EXTRACTOR_VERSION bumped a phase3-2026-05-27

INSERT INTO signal_types (id, parent_id, display_name, value_type, decay_class, half_life_days, aggregation_strategy, contradiction_strategy, importance_weight)
VALUES
  ('dark_sky',          'beauty',  'Cielo Oscuro / Estrellas',     'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 1.5),
  ('beach_access',      'beauty',  'Acceso a Playa',               'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.8),
  ('river_nearby',      'beauty',  'Rio/Arroyo Cercano',           'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.5),
  ('hiking_nearby',     NULL,      'Senderismo Cercano',           'boolean', 'permanent', 36500, 'consensus_boolean', 'majority_consensus', 0.7),
  ('cycling_nearby',    NULL,      'Ciclismo Cercano',             'boolean', 'permanent', 36500, 'consensus_boolean', 'majority_consensus', 0.6),
  ('height_restriction',NULL,      'Restriccion de Altura (m)',    'numeric', 'permanent', 36500, 'weighted_mean',     'permanent_override', 1.2),
  ('shower_working',    NULL,      'Duchas Operativas',            'boolean', 'volatile',    60,  'consensus_boolean', 'recent_wins',        1.2),
  ('spot_closed',       NULL,      'Spot Cerrado',                 'boolean', 'volatile',    30,  'consensus_boolean', 'recent_wins',        2.5),
  ('youth_trouble',     'safety',  'Problemas con Jovenes',        'numeric', 'volatile',    60,  'weighted_mean',     'recent_wins',        1.5),
  -- v3b — señales para mapeo directo de datos scrapeados
  ('campfire_allowed',  NULL,      'Hoguera Permitida',            'boolean', 'slow',       730,  'consensus_boolean', 'recent_wins',        0.8),
  ('ev_charging',       NULL,      'Carga Vehículo Eléctrico',     'boolean', 'slow',       730,  'consensus_boolean', 'majority_consensus', 0.7),
  ('swimming_access',   NULL,      'Acceso a Baño/Piscina',        'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.7),
  ('caravan_accepted',  NULL,      'Acepta Caravanas Remolcadas',  'boolean', 'slow',      3650,  'consensus_boolean', 'majority_consensus', 0.6)
ON CONFLICT (id) DO UPDATE SET
  parent_id              = EXCLUDED.parent_id,
  display_name           = EXCLUDED.display_name,
  value_type             = EXCLUDED.value_type,
  decay_class            = EXCLUDED.decay_class,
  half_life_days         = EXCLUDED.half_life_days,
  aggregation_strategy   = EXCLUDED.aggregation_strategy,
  contradiction_strategy = EXCLUDED.contradiction_strategy,
  importance_weight      = EXCLUDED.importance_weight;
