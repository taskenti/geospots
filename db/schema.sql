-- ═══════════════════════════════════════════════════════════════
-- GeoSpots — Schema v2.0 (Unified Canonical & Semantic Schema)
-- Motor geoespacial semántico para camper/vanlife
-- ═══════════════════════════════════════════════════════════════

-- Extensiones
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS earthdistance CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- ═══════════════════════════════════════════════════════════════
-- CAPA RAW: Log e payloads originales inmutables
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS raw_fetches (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,           -- 'park4night', 'caramaps', etc.
    fetch_type      TEXT NOT NULL,           -- 'grid_cell', 'spot_detail', 'reviews'
    bbox_tl_lat     REAL,
    bbox_tl_lon     REAL,
    bbox_br_lat     REAL,
    bbox_br_lon     REAL,
    source_id       TEXT,
    http_status     INT,
    records_found   INT DEFAULT 0,
    error_msg       TEXT,
    duration_ms     INT,
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rf_source_date ON raw_fetches(source, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_rf_source_id ON raw_fetches(source, source_id) WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS raw_payloads (
    id              BIGSERIAL PRIMARY KEY,
    fetch_id        BIGINT REFERENCES raw_fetches(id),
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,           -- ID en la fuente original
    payload_type    TEXT NOT NULL,           -- 'spot', 'review', 'detail'
    raw_json        JSONB NOT NULL,          -- JSON tal cual vino
    checksum        TEXT NOT NULL,           -- MD5 del raw_json para detectar cambios
    first_seen      TIMESTAMPTZ DEFAULT NOW(),
    last_seen       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source, source_id, payload_type)
);

CREATE INDEX IF NOT EXISTS idx_rp_source ON raw_payloads(source, source_id);
CREATE INDEX IF NOT EXISTS idx_rp_checksum ON raw_payloads(checksum);

-- ═══════════════════════════════════════════════════════════════
-- SPOTS: entidad canónica (1 por lugar físico real)
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS spots (
    id                  SERIAL PRIMARY KEY,
    canonical_name      TEXT NOT NULL,
    slug                TEXT UNIQUE,
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    geog                GEOGRAPHY(Point, 4326) GENERATED ALWAYS AS
                        (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) STORED,
    geohash7            TEXT GENERATED ALWAYS AS
                        (substring(encode(sha256((floor(lat*1e5)::bigint::text || ',' || floor(lon*1e5)::bigint::text)::bytea), 'base64'), 1, 8)) STORED,
    
    country_iso         TEXT,
    continent           TEXT,
    subregion           TEXT,
    region              TEXT,
    municipio           TEXT,
    tipo                TEXT DEFAULT 'otro',
    subtipo             TEXT,

    -- Servicios reconciliados
    gratuito            BOOLEAN,
    precio_aprox        REAL,
    precio_info         TEXT,
    agua_potable        BOOLEAN,
    vaciado_negras      BOOLEAN,
    vaciado_grises      BOOLEAN,
    electricidad        BOOLEAN,
    ducha               BOOLEAN,
    wifi                BOOLEAN,
    wc_publico          BOOLEAN,
    perros              BOOLEAN,
    acceso_grandes      BOOLEAN,
    num_plazas          INT,
    altura_max_m        REAL,
    temporada_apertura  TEXT,
    reserva_req         BOOLEAN,
    iluminacion         BOOLEAN,
    seguridad           BOOLEAN,

    -- v4c: amenidades extra (rescatadas de raw_data)
    piscina             BOOLEAN,
    lavanderia          BOOLEAN,
    gas_recharge        BOOLEAN,
    restaurant          BOOLEAN,
    juegos_ninos        BOOLEAN,
    mirador             BOOLEAN,
    zona_protegida      BOOLEAN,
    online_booking      BOOLEAN,
    winter_friendly     BOOLEAN,
    apto_motos          BOOLEAN,
    -- Actividades cercanas
    mtb_friendly        BOOLEAN,
    surf_friendly       BOOLEAN,
    fishing             BOOLEAN,
    climbing            BOOLEAN,
    hiking_nearby       BOOLEAN,
    -- Capacidad eléctrica y estancia
    amperaje            INT,
    n_enchufes          INT,
    max_noches          INT,
    -- Arrays (idiomas, productos a la venta)
    idiomas_hablados    TEXT[],
    productos_venta     TEXT[],
    -- JSONB flexible (prohibitions, risks, descriptions, pricing_breakdown, hours, nearby)
    servicios_extras    JSONB DEFAULT '{}'::jsonb,

    -- Metadata agregada
    master_rating       REAL,
    total_reviews       INT DEFAULT 0,
    fuentes             TEXT[] DEFAULT '{}',
    num_fuentes         INT GENERATED ALWAYS AS (COALESCE(array_length(fuentes, 1), 0)) STORED,
    -- v4 paso 2: scores derivados (recompute periódico, no trigger)
    popularity_score    REAL,
    reliability_score   REAL,

    -- Descripciones reconciliadas (nombres antiguos + pt/nl para máxima compatibilidad)
    descripcion_es      TEXT,
    descripcion_en      TEXT,
    descripcion_fr      TEXT,
    descripcion_de      TEXT,
    descripcion_it      TEXT,
    descripcion_nl      TEXT,
    descripcion_pt      TEXT,
    
    -- Contacto
    web                 TEXT,
    telefono            TEXT,
    email               TEXT,
    fotos_urls          JSONB DEFAULT '[]',

    -- Control
    activo              BOOLEAN DEFAULT TRUE,
    verificado          BOOLEAN DEFAULT FALSE,
    advertencia         TEXT,
    conflictos          JSONB DEFAULT '[]',
    confidence          REAL DEFAULT 0.5,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT tipo_valido CHECK (tipo IN (
        'area_ac',          -- área específica autocaravanas
        'camping',          -- camping oficial
        'parking_publico',  -- parking urbano/rural
        'parking_privado',  -- parking privado con pernocta
        'wild',             -- camping salvaje / monte
        'gasolinera',       -- gasolinera con pernocta
        'area_descanso',    -- área de descanso autopista
        'marina',           -- puerto deportivo
        'otro',
        'naturaleza',       -- compatibilidad con scrapers
        'parking',          -- compatibilidad con scrapers
        'picnic'            -- compatibilidad con scrapers
    ))
);

CREATE INDEX IF NOT EXISTS idx_spots_geog ON spots USING GIST(geog);
CREATE INDEX IF NOT EXISTS idx_spots_tipo ON spots(tipo) WHERE activo = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_fuentes ON spots USING GIN(fuentes);
CREATE INDEX IF NOT EXISTS idx_spots_activo ON spots(activo) WHERE activo = TRUE;
CREATE INDEX IF NOT EXISTS idx_spots_name_trgm ON spots USING gin(canonical_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_spots_rating ON spots(master_rating DESC NULLS LAST) WHERE activo = TRUE;

-- ═══════════════════════════════════════════════════════════════
-- SOURCE_CREDIBILITY: credibilidad por fuente
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS source_credibility (
    source          TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    base_score      REAL NOT NULL DEFAULT 0.5,    -- 0-1, peso base en reconciliación
    review_quality  REAL DEFAULT 0.5,             -- calidad media de reviews de esta fuente
    coverage_region TEXT[],                        -- ['ES', 'FR', 'PT'] o ['EU']
    active          BOOLEAN DEFAULT TRUE,
    last_scrape     TIMESTAMPTZ,
    total_records   INT DEFAULT 0,
    notes           TEXT
);

-- Valores iniciales de credibilidad
INSERT INTO source_credibility (source, display_name, base_score, review_quality, coverage_region) VALUES
('park4night',      'Park4Night',          0.92, 0.85, ARRAY['EU']),
('campercontact',   'CamperContact',       0.90, 0.80, ARRAY['EU']),
('ioverlander',     'iOverlander',         0.85, 0.90, ARRAY['WW']),
('caramaps',        'CaraMaps',            0.82, 0.75, ARRAY['EU']),
('furgovw',         'Furgovw',             0.80, 0.88, ARRAY['ES']),
('searchforsites',  'SearchForSites',      0.80, 0.75, ARRAY['UK']),
('stayfree',        'StayFree',            0.75, 0.70, ARRAY['EU']),
('campy',           'Campy',               0.75, 0.72, ARRAY['DE','AT','CH']),
('areasac',         'AreasAC',             0.85, 0.60, ARRAY['ES']),
('osm',             'OpenStreetMap',       0.60, 0.00, ARRAY['WW']),
('stellplatz',      'Stellplatz Radar',    0.75, 0.70, ARRAY['DE','AT','CH']),
('campernight',     'Campernight',         0.70, 0.68, ARRAY['EU']),
('roadsurfer',      'Roadsurfer Spots',    0.72, 0.65, ARRAY['EU']),
('wikicamps',       'WikiCamps',           0.70, 0.72, ARRAY['AU','EU']),
('campininfo',      'Camping.info',        0.78, 0.65, ARRAY['EU']),
('wikidata',        'Wikidata',            0.55, 0.00, ARRAY['WW']),
('eu_opendata',     'EU Open Data',        0.88, 0.00, ARRAY['EU'])
,
('promobil',        'Promobil',            0.84, 0.78, ARRAY['DE','AT','CH']),
('camperstop',      'Camperstop',          0.80, 0.72, ARRAY['EU']),
('vansite',         'Vansite',             0.72, 0.70, ARRAY['EU']),
('nomady',          'Nomady',              0.76, 0.78, ARRAY['EU']),
('campspace',       'Campspace',           0.74, 0.76, ARRAY['EU']),
('wtmg',            'Welcome To My Garden',0.70, 0.72, ARRAY['EU'])
ON CONFLICT (source) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    base_score = EXCLUDED.base_score,
    review_quality = EXCLUDED.review_quality,
    coverage_region = EXCLUDED.coverage_region;

-- ═══════════════════════════════════════════════════════════════
-- SOURCE_RECORDS: lo que cada fuente sabe de un spot (combinación)
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS source_records (
    id                  SERIAL PRIMARY KEY,
    spot_id             INT REFERENCES spots(id) ON DELETE CASCADE,
    source              TEXT NOT NULL,
    source_id           TEXT NOT NULL,

    raw_data            JSONB NOT NULL,
    normalized_data     JSONB NOT NULL,
    normalized          JSONB, -- campo para nuevo esquema

    lat                 DOUBLE PRECISION,
    lon                 DOUBLE PRECISION,
    name                TEXT,
    rating              REAL,
    review_count        INT,
    tipo_original       TEXT,

    credibility         REAL DEFAULT 0.5,
    checksum            TEXT NOT NULL,
    first_seen          TIMESTAMPTZ DEFAULT NOW(),
    last_seen           TIMESTAMPTZ DEFAULT NOW(),
    stale               BOOLEAN DEFAULT FALSE,

    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_sr_spot ON source_records(spot_id);
CREATE INDEX IF NOT EXISTS idx_sr_source ON source_records(source, source_id);
CREATE INDEX IF NOT EXISTS idx_sr_stale ON source_records(stale) WHERE stale = TRUE;

-- ═══════════════════════════════════════════════════════════════
-- REVIEWS: reviews de todas las fuentes con campos de limpieza
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS reviews (
    id                  BIGSERIAL PRIMARY KEY,
    spot_id             INT REFERENCES spots(id) ON DELETE CASCADE,
    source              TEXT NOT NULL,
    source_review_id    TEXT,
    
    texto               TEXT,                                 -- compatible
    texto_original      TEXT,                                 -- nuevo
    texto_limpio        TEXT,                                 -- nuevo
    rating              REAL,
    autor               TEXT,
    fecha               DATE,
    idioma              TEXT,
    
    cleaned             BOOLEAN DEFAULT FALSE,
    informativo         BOOLEAN,
    llm_processed       BOOLEAN DEFAULT FALSE,
    llm_analysis        JSONB,

    checksum            TEXT,
    first_seen          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(source, source_review_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_spot ON reviews(spot_id);
CREATE INDEX IF NOT EXISTS idx_reviews_source ON reviews(source);
CREATE INDEX IF NOT EXISTS idx_rev_fecha ON reviews(fecha DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_rev_cleaned ON reviews(cleaned) WHERE NOT cleaned;
CREATE INDEX IF NOT EXISTS idx_rev_info ON reviews(informativo) WHERE informativo = TRUE;

-- ═══════════════════════════════════════════════════════════════
-- REVIEW_FACTS: hechos extraídos de reviews (post-limpieza)
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS review_facts (
    id              BIGSERIAL PRIMARY KEY,
    review_id       BIGINT REFERENCES reviews(id) ON DELETE CASCADE,
    spot_id         INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    frase           TEXT NOT NULL,
    categoria       TEXT,
    sentimiento     REAL,
    confianza       REAL,
    atributo        TEXT,
    valor           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rf_spot ON review_facts(spot_id);
CREATE INDEX IF NOT EXISTS idx_rf_categoria ON review_facts(categoria);
CREATE INDEX IF NOT EXISTS idx_rf_atributo ON review_facts(atributo) WHERE atributo IS NOT NULL;

-- ═══════════════════════════════════════════════════════════════
-- SPOT_ENRICHMENTS: scores semánticos pre-computados por LLM
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS spot_enrichments (
    spot_id             INT PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,

    quietness           REAL,
    safety              REAL,
    police_risk         REAL,
    beauty              REAL,
    stealth             REAL,
    road_noise          REAL,
    wind_exposure       REAL,
    crowd_level         REAL,
    cleanliness         REAL,

    shade_morning       BOOLEAN,
    shade_afternoon     BOOLEAN,
    sea_view            BOOLEAN,
    mountain_view       BOOLEAN,
    lake_nearby         BOOLEAN,
    beach_nearby        BOOLEAN,
    forest_nearby       BOOLEAN,
    urban_area          BOOLEAN,

    large_vehicle       REAL,
    road_quality        REAL,
    overnight_safe      BOOLEAN,

    best_season         TEXT,
    avoid_season        TEXT,

    tags                TEXT[],
    best_for            TEXT[],

    llm_summary_es      TEXT, -- compatible
    llm_summary_en      TEXT, -- compatible
    resumen_es          TEXT, -- nuevo
    resumen_en          TEXT, -- nuevo

    reviews_analyzed    INT DEFAULT 0,
    facts_usados        INT DEFAULT 0,
    confidence          REAL,
    model_used          TEXT,
    processed_at        TIMESTAMPTZ DEFAULT NOW(),
    stale               BOOLEAN DEFAULT FALSE
);

-- ═══════════════════════════════════════════════════════════════
-- SPOT_EMBEDDINGS: vectores para búsqueda semántica
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS spot_embeddings (
    spot_id             INT PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,
    embedding           vector(768),
    texto_fuente        TEXT,
    model               TEXT DEFAULT 'text-embedding-004',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw ON spot_embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_embeddings_model_created ON spot_embeddings(model, created_at DESC);

-- ═══════════════════════════════════════════════════════════════
-- SPOT_GEO: análisis geoespacial del entorno
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS spot_geo (
    spot_id                 INT PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,
    elevation_m             REAL,
    slope_degrees           REAL,
    aspect_degrees          REAL,
    terrain_type            TEXT,
    sun_morning_summer      REAL,
    sun_afternoon_summer    REAL,
    sun_morning_winter      REAL,
    sun_afternoon_winter    REAL,
    dist_nearest_building_m REAL,
    dist_nearest_road_m     REAL,
    road_type_nearest       TEXT,
    buildings_100m          INT,
    buildings_500m          INT,
    vegetation_cover        REAL,
    dist_motorway_km        REAL,
    dist_fuel_km            REAL,
    dist_supermarket_km     REAL,
    dist_hospital_km        REAL,
    dist_coast_km           REAL,
    dist_lake_km            REAL,
    dist_river_km           REAL,
    landuse_type            TEXT,
    protected_area          BOOLEAN DEFAULT FALSE,
    protected_area_name     TEXT,
    noise_road              REAL,
    noise_urban             REAL,
    noise_combined          REAL,
    stealth_geo_score       REAL,
    processed_at            TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════════
-- TABLAS DE COMPATIBILIDAD CON SCRAPER Y LOGS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS fuentes_config (
    nombre              TEXT PRIMARY KEY,
    activa              BOOLEAN DEFAULT TRUE,
    notas               TEXT,
    ultimo_run_inicio   TIMESTAMPTZ,
    ultimo_run_fin      TIMESTAMPTZ,
    ultimo_run_estado   TEXT,
    spots_totales       INT DEFAULT 0,
    reviews_totales     INT DEFAULT 0,
    errores_ultimo_run  INT DEFAULT 0
);

INSERT INTO fuentes_config (nombre, activa, notas) VALUES
    ('park4night', TRUE, 'Park4Night — API interna JSON'),
    ('campercontact', TRUE, 'CamperContact — API interna search/results'),
    ('ioverlander', TRUE, 'iOverlander — KMZ offline import'),
    ('furgovw', TRUE, 'Furgovw — API JSON + scraping'),
    ('areasac', TRUE, 'ÁreasAC España — HTML scraping'),
    ('osm', TRUE, 'OpenStreetMap — Overpass API'),
    ('caramaps', FALSE, 'CaraMaps — pendiente'),
    ('searchforsites', FALSE, 'SearchForSites — pendiente'),
    ('stayfree', FALSE, 'StayFree — pendiente'),
    ('campy', FALSE, 'Campy — pendiente')
ON CONFLICT (nombre) DO NOTHING;

CREATE TABLE IF NOT EXISTS scraper_log (
    id                  SERIAL PRIMARY KEY,
    fuente              TEXT NOT NULL,
    estado              TEXT DEFAULT 'running',
    iniciado_en         TIMESTAMPTZ DEFAULT NOW(),
    terminado_en        TIMESTAMPTZ,
    spots_nuevos        INT DEFAULT 0,
    spots_actualizados  INT DEFAULT 0,
    reviews_nuevas      INT DEFAULT 0,
    errores             INT DEFAULT 0,
    detalle             JSONB DEFAULT '{}'
);

-- ═══════════════════════════════════════════════════════════════
-- SOPORTE Y COLAS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS dedup_log (
    id              BIGSERIAL PRIMARY KEY,
    action          TEXT NOT NULL,
    spot_id         INT,
    source_a        TEXT, source_id_a TEXT,
    source_b        TEXT, source_id_b TEXT,
    dist_m          REAL,
    name_similarity REAL,
    decision_score  REAL,
    reason          TEXT,
    manual_review   BOOLEAN DEFAULT FALSE,
    resolved        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scrape_queue (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    payload         JSONB NOT NULL,
    priority        INT DEFAULT 5,
    status          TEXT DEFAULT 'pending',
    attempts        INT DEFAULT 0,
    error_msg       TEXT,
    scheduled_for   TIMESTAMPTZ DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sq_pending ON scrape_queue(priority, scheduled_for)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS enrichment_queue (
    spot_id         INT PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,
    priority        INT DEFAULT 5,
    reason          TEXT,
    queued_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════════
-- COUNTRIES & CLASSIFICATION TRIGGER
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS countries (
    id SERIAL PRIMARY KEY,
    iso_a2 TEXT,
    name TEXT,
    continent TEXT,
    region_un TEXT,
    subregion TEXT,
    geom GEOMETRY(Geometry, 4326)
);

CREATE INDEX IF NOT EXISTS idx_countries_geom ON countries USING GIST(geom);

CREATE OR REPLACE FUNCTION fn_classify_spot()
RETURNS TRIGGER AS $$
DECLARE
    v_iso_a2 TEXT;
    v_continent TEXT;
    v_subregion TEXT;
BEGIN
    -- 1. Exact contains search
    SELECT LOWER(iso_a2), continent, subregion INTO v_iso_a2, v_continent, v_subregion
    FROM countries
    WHERE ST_Contains(geom, ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326))
    LIMIT 1;

    -- 2. Nearest fallback (up to 50km) if not found inside any country
    IF v_iso_a2 IS NULL THEN
        SELECT LOWER(iso_a2), continent, subregion INTO v_iso_a2, v_continent, v_subregion
        FROM countries
        WHERE ST_DWithin(ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326)::geography, geom::geography, 50000)
        ORDER BY ST_Distance(ST_SetSRID(ST_MakePoint(NEW.lon, NEW.lat), 4326)::geography, geom::geography) ASC
        LIMIT 1;
    END IF;

    -- 3. Set values
    IF v_iso_a2 IS NOT NULL THEN
        NEW.country_iso := v_iso_a2;
        NEW.continent := v_continent;
        NEW.subregion := v_subregion;
    ELSE
        IF NEW.country_iso IS NOT NULL THEN
            NEW.country_iso := LOWER(NEW.country_iso);
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_classify_spot ON spots;

CREATE TRIGGER trg_classify_spot
BEFORE INSERT OR UPDATE OF lat, lon ON spots
FOR EACH ROW
EXECUTE FUNCTION fn_classify_spot();

-- Phase 3: geotemporal semantic state engine.
-- Keep this block in sync with db/migration_phase3.sql for fresh databases.

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS texto_original TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS texto_limpio TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS texto_dsl TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS cleaned BOOLEAN DEFAULT FALSE;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS informativo BOOLEAN;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS llm_processed BOOLEAN DEFAULT FALSE;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS llm_analysis JSONB;

CREATE TABLE IF NOT EXISTS signal_types (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES signal_types(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    display_name_en TEXT,
    value_type TEXT NOT NULL CHECK (value_type IN ('numeric', 'boolean', 'text')),
    decay_class TEXT NOT NULL CHECK (decay_class IN ('permanent', 'slow', 'volatile')),
    half_life_days INT NOT NULL,
    aggregation_strategy TEXT NOT NULL CHECK (aggregation_strategy IN ('weighted_mean', 'consensus_boolean', 'recent_wins')),
    contradiction_strategy TEXT NOT NULL CHECK (contradiction_strategy IN ('recent_wins', 'majority_consensus', 'permanent_override')),
    importance_weight REAL DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO signal_types (id, parent_id, display_name, value_type, decay_class, half_life_days, aggregation_strategy, contradiction_strategy, importance_weight) VALUES
('noise', NULL, 'Ruido General', 'numeric', 'volatile', 90, 'weighted_mean', 'recent_wins', 1.5),
('road_noise', 'noise', 'Ruido de Carretera', 'numeric', 'volatile', 90, 'weighted_mean', 'recent_wins', 1.0),
('party_noise', 'noise', 'Ruido de Fiesta/Gente', 'numeric', 'volatile', 30, 'weighted_mean', 'recent_wins', 1.0),
('train_noise', 'noise', 'Ruido de Tren', 'numeric', 'slow', 730, 'weighted_mean', 'permanent_override', 0.8),
('quietness', NULL, 'Tranquilidad General', 'numeric', 'slow', 365, 'weighted_mean', 'majority_consensus', 1.5),
('beauty', NULL, 'Belleza del Entorno', 'numeric', 'permanent', 36500, 'weighted_mean', 'majority_consensus', 1.2),
('cleanliness', NULL, 'Limpieza', 'numeric', 'volatile', 60, 'weighted_mean', 'recent_wins', 0.8),
('safety', NULL, 'Seguridad General', 'numeric', 'slow', 365, 'weighted_mean', 'recent_wins', 1.5),
('police_risk', 'safety', 'Riesgo de Policia', 'numeric', 'volatile', 60, 'weighted_mean', 'recent_wins', 2.0),
('theft_risk', 'safety', 'Riesgo de Robos', 'numeric', 'volatile', 90, 'weighted_mean', 'recent_wins', 2.0),
('sea_view', 'beauty', 'Vistas al Mar', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.5),
('mountain_view', 'beauty', 'Vistas a Montana', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.5),
('lake_nearby', 'beauty', 'Lago Cercano', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.3),
('shade_morning', NULL, 'Sombra por la Manana', 'boolean', 'slow', 1825, 'consensus_boolean', 'majority_consensus', 0.4),
('shade_afternoon', NULL, 'Sombra por la Tarde', 'boolean', 'slow', 1825, 'consensus_boolean', 'majority_consensus', 0.4),
('large_vehicle', NULL, 'Acceso Vehiculos >7m', 'numeric', 'permanent', 36500, 'weighted_mean', 'permanent_override', 0.6),
('road_quality', NULL, 'Calidad del Acceso', 'numeric', 'slow', 1825, 'weighted_mean', 'majority_consensus', 0.5),
('overnight_safe', NULL, 'Pernocta Posible', 'boolean', 'volatile', 120, 'consensus_boolean', 'recent_wins', 2.0),
('crowd_level', NULL, 'Nivel de Masificacion', 'numeric', 'volatile', 30, 'weighted_mean', 'recent_wins', 1.0),
('wind_exposure', NULL, 'Exposicion al Viento', 'numeric', 'slow', 730, 'weighted_mean', 'majority_consensus', 0.6),
('stealth', NULL, 'Discrecion del Spot', 'numeric', 'slow', 365, 'weighted_mean', 'majority_consensus', 0.8),
-- Phase 3 v2
('noise_source', 'noise', 'Fuente de Ruido', 'text', 'slow', 180, 'recent_wins', 'recent_wins', 1.2),
('parking_capacity', NULL, 'Capacidad de Parking', 'text', 'slow', 1825, 'recent_wins', 'recent_wins', 0.6),
('cell_coverage', NULL, 'Cobertura Movil', 'numeric', 'slow', 365, 'weighted_mean', 'majority_consensus', 0.7),
('wild_camping_legal', NULL, 'Acampada Libre Legal', 'boolean', 'slow', 730, 'consensus_boolean', 'recent_wins', 2.0),
('mosquitoes', NULL, 'Mosquitos', 'numeric', 'volatile', 180, 'weighted_mean', 'recent_wins', 0.5),
('dog_friendly', NULL, 'Apto Perros', 'boolean', 'slow', 1825, 'consensus_boolean', 'majority_consensus', 0.6),
('family_friendly', NULL, 'Apto Familias', 'boolean', 'slow', 1825, 'consensus_boolean', 'majority_consensus', 0.6),
('accessible_pmr', NULL, 'Accesible PMR', 'boolean', 'slow', 1825, 'consensus_boolean', 'majority_consensus', 0.6),
('water_working', NULL, 'Agua Operativa', 'boolean', 'volatile', 60, 'consensus_boolean', 'recent_wins', 1.5),
('electricity_working', NULL, 'Electricidad Operativa', 'boolean', 'volatile', 60, 'consensus_boolean', 'recent_wins', 1.5),
('dump_station_working', NULL, 'Vaciado Aguas Operativo', 'boolean', 'volatile', 60, 'consensus_boolean', 'recent_wins', 1.5),
-- Phase 3 v3 — nuevas señales identificadas de análisis de reviews reales (2026-05)
('dark_sky', 'beauty', 'Cielo Oscuro / Estrellas', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 1.5),
('beach_access', 'beauty', 'Acceso a Playa', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.8),
('river_nearby', 'beauty', 'Rio/Arroyo Cercano', 'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.5),
('hiking_nearby', NULL, 'Senderismo Cercano', 'boolean', 'permanent', 36500, 'consensus_boolean', 'majority_consensus', 0.7),
('cycling_nearby', NULL, 'Ciclismo Cercano', 'boolean', 'permanent', 36500, 'consensus_boolean', 'majority_consensus', 0.6),
('height_restriction', NULL, 'Restriccion de Altura (m)', 'numeric', 'permanent', 36500, 'weighted_mean', 'permanent_override', 1.2),
('shower_working', NULL, 'Duchas Operativas', 'boolean', 'volatile', 60, 'consensus_boolean', 'recent_wins', 1.2),
('spot_closed', NULL, 'Spot Cerrado', 'boolean', 'volatile', 30, 'consensus_boolean', 'recent_wins', 2.5),
('youth_trouble', 'safety', 'Problemas con Jovenes', 'numeric', 'volatile', 60, 'weighted_mean', 'recent_wins', 1.5),
-- Phase 3 v3b — señales para mapeo directo de datos scrapeados
('campfire_allowed', NULL, 'Hoguera Permitida',           'boolean', 'slow',    730,   'consensus_boolean', 'recent_wins',        0.8),
('ev_charging',      NULL, 'Carga Vehículo Eléctrico',    'boolean', 'slow',    730,   'consensus_boolean', 'majority_consensus', 0.7),
('swimming_access',  NULL, 'Acceso a Baño/Piscina',       'boolean', 'permanent', 36500, 'consensus_boolean', 'permanent_override', 0.7),
('caravan_accepted', NULL, 'Acepta Caravanas Remolcadas', 'boolean', 'slow',    3650,  'consensus_boolean', 'majority_consensus', 0.6)
ON CONFLICT (id) DO UPDATE SET
    parent_id = EXCLUDED.parent_id,
    display_name = EXCLUDED.display_name,
    value_type = EXCLUDED.value_type,
    decay_class = EXCLUDED.decay_class,
    half_life_days = EXCLUDED.half_life_days,
    aggregation_strategy = EXCLUDED.aggregation_strategy,
    contradiction_strategy = EXCLUDED.contradiction_strategy,
    importance_weight = EXCLUDED.importance_weight;

CREATE TABLE IF NOT EXISTS extracted_claims (
    id BIGSERIAL PRIMARY KEY,
    review_id BIGINT REFERENCES reviews(id) ON DELETE CASCADE,  -- nullable: v2 admite claims desde descripciones del spot
    spot_id INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    signal_type TEXT NOT NULL REFERENCES signal_types(id),
    raw_value TEXT NOT NULL,
    extraction_confidence REAL NOT NULL DEFAULT 1.0,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    pipeline_run_id TEXT,
    excerpt TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ec_spot_signal ON extracted_claims(spot_id, signal_type);
CREATE INDEX IF NOT EXISTS idx_ec_review ON extracted_claims(review_id);
CREATE INDEX IF NOT EXISTS idx_ec_extractor ON extracted_claims(extractor_name, extractor_version);

CREATE TABLE IF NOT EXISTS normalized_observations (
    id BIGSERIAL PRIMARY KEY,
    claim_id BIGINT NOT NULL REFERENCES extracted_claims(id) ON DELETE CASCADE,
    spot_id INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    signal_type TEXT NOT NULL REFERENCES signal_types(id),
    value_num REAL,
    value_bool BOOLEAN,
    value_text TEXT,
    extraction_confidence REAL NOT NULL,
    source_confidence REAL NOT NULL DEFAULT 1.0,
    reviewer_confidence REAL NOT NULL DEFAULT 1.0,
    observation_weight REAL NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_no_spot_signal_date ON normalized_observations(spot_id, signal_type, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_no_claim ON normalized_observations(claim_id);

CREATE TABLE IF NOT EXISTS spot_semantic_state (
    spot_id INT PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,
    quietness_score REAL,
    safety_score REAL,
    police_risk_score REAL,
    beauty_score REAL,
    crowd_level_score REAL,
    overnight_safe BOOLEAN,
    stealth_score REAL,
    signals_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    semantic_dsl TEXT,
    summary_es TEXT,
    summary_en TEXT,
    tags TEXT[],
    best_for TEXT[],
    best_season TEXT,
    total_observations INT DEFAULT 0,
    consensus_confidence REAL DEFAULT 0.0,
    weight_support REAL DEFAULT 0.0,
    last_aggregated_at TIMESTAMPTZ DEFAULT NOW(),
    last_snapshot_data JSONB,
    stale BOOLEAN DEFAULT FALSE,
    -- Phase 3 v2
    enrichment_version INT DEFAULT 1,
    llm_model TEXT,
    last_observation_at TIMESTAMPTZ,
    noise_sources TEXT[],
    parking_capacity TEXT,
    cell_coverage REAL,
    wild_camping_legal BOOLEAN,
    avoid_season TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sss_stale ON spot_semantic_state(stale) WHERE stale = TRUE;
CREATE INDEX IF NOT EXISTS idx_sss_filters ON spot_semantic_state(quietness_score, police_risk_score, crowd_level_score) WHERE stale = FALSE;
CREATE INDEX IF NOT EXISTS idx_sss_enrichment_version ON spot_semantic_state(enrichment_version);
CREATE INDEX IF NOT EXISTS idx_sss_last_observation ON spot_semantic_state(last_observation_at DESC NULLS LAST);

-- Vista con freshness_warning calculado (NOW() no es immutable → no se puede usar en GENERATED STORED)
CREATE OR REPLACE VIEW v_spot_semantic_state AS
SELECT
    sss.*,
    (sss.last_observation_at IS NOT NULL
     AND sss.last_observation_at < NOW() - INTERVAL '24 months') AS freshness_warning
FROM spot_semantic_state sss;

-- Phase 3 v2: batches enviados a Gemini Batch API
CREATE TABLE IF NOT EXISTS enrichment_batches (
    id                  BIGSERIAL PRIMARY KEY,
    batch_name          TEXT UNIQUE NOT NULL,
    enrichment_version  INT  NOT NULL,
    llm_model           TEXT NOT NULL,
    spot_ids            INT[] NOT NULL,
    state               TEXT NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending','running','succeeded','failed','partial','cancelled')),
    n_requested         INT  NOT NULL,
    n_succeeded         INT,
    n_failed            INT,
    tokens_input        BIGINT,
    tokens_output       BIGINT,
    cost_estimated_usd  REAL,
    error_msg           TEXT,
    submitted_at        TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_eb_state ON enrichment_batches(state) WHERE state IN ('pending','running');

-- Phase 3 v2: estado del system-prompt cache (Gemini context caching)
CREATE TABLE IF NOT EXISTS enrichment_cache_state (
    id                  BIGSERIAL PRIMARY KEY,
    enrichment_version  INT  NOT NULL,
    llm_model           TEXT NOT NULL,
    cache_name          TEXT NOT NULL UNIQUE,
    cache_token_count   INT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ecs_active ON enrichment_cache_state(enrichment_version, llm_model, expires_at DESC);

CREATE TABLE IF NOT EXISTS spot_semantic_snapshots (
    id BIGSERIAL PRIMARY KEY,
    spot_id INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    snapshot_date DATE NOT NULL,
    semantic_data JSONB NOT NULL,
    trigger_reason TEXT NOT NULL,
    semantic_distance REAL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(spot_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_sssn_lookup ON spot_semantic_snapshots(spot_id, snapshot_date DESC);

CREATE TABLE IF NOT EXISTS semantic_events (
    id BIGSERIAL PRIMARY KEY,
    spot_id INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    severity REAL NOT NULL,
    evidence_count INT NOT NULL DEFAULT 1,
    first_seen TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    evidence_claim_ids BIGINT[] NOT NULL,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(spot_id, event_type, first_seen)
);

CREATE INDEX IF NOT EXISTS idx_se_active ON semantic_events(spot_id, active) WHERE active = TRUE;

CREATE OR REPLACE VIEW spot_temperature AS
SELECT
    s.id,
    s.total_reviews,
    CASE
        WHEN s.total_reviews >= 10 THEN 'hot'
        WHEN s.total_reviews >= 3 THEN 'warm'
        ELSE 'cold'
    END AS temperature
FROM spots s
WHERE s.activo = TRUE;
