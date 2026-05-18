-- ═══════════════════════════════════════════════════════════════
-- GeoSpots — Schema v1.0
-- Motor geoespacial semántico para camper/vanlife
-- ═══════════════════════════════════════════════════════════════

-- Extensiones
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS earthdistance CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ═══════════════════════════════════════════════════════════════
-- SPOTS: entidad canónica (1 por lugar físico real)
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE spots (
    id                  SERIAL PRIMARY KEY,
    canonical_name      TEXT NOT NULL,
    lat                 DOUBLE PRECISION NOT NULL,
    lon                 DOUBLE PRECISION NOT NULL,
    geog                GEOGRAPHY(Point, 4326) GENERATED ALWAYS AS
                        (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) STORED,
    country_iso         TEXT,
    region              TEXT,
    tipo                TEXT DEFAULT 'otro',

    -- Servicios reconciliados
    gratuito            BOOLEAN,
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

    -- Metadata agregada
    master_rating       REAL,
    total_reviews       INT DEFAULT 0,
    fuentes             TEXT[] DEFAULT '{}',
    num_fuentes         INT GENERATED ALWAYS AS (COALESCE(array_length(fuentes, 1), 0)) STORED,

    -- Descripciones reconciliadas
    descripcion_es      TEXT,
    descripcion_en      TEXT,
    descripcion_fr      TEXT,
    descripcion_de      TEXT,
    descripcion_it      TEXT,
    descripcion_nl      TEXT,

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
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_spots_geog ON spots USING GIST(geog);
CREATE INDEX idx_spots_tipo ON spots(tipo) WHERE activo = TRUE;
CREATE INDEX idx_spots_fuentes ON spots USING GIN(fuentes);
CREATE INDEX idx_spots_activo ON spots(activo) WHERE activo = TRUE;
CREATE INDEX idx_spots_name_trgm ON spots USING gin(canonical_name gin_trgm_ops);

-- ═══════════════════════════════════════════════════════════════
-- SOURCE_RECORDS: datos crudos por fuente (inmutables)
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE source_records (
    id                  SERIAL PRIMARY KEY,
    spot_id             INT REFERENCES spots(id) ON DELETE CASCADE,
    source              TEXT NOT NULL,
    source_id           TEXT NOT NULL,

    raw_data            JSONB NOT NULL,
    normalized_data     JSONB NOT NULL,

    lat                 DOUBLE PRECISION,
    lon                 DOUBLE PRECISION,
    name                TEXT,
    rating              REAL,
    review_count        INT,

    checksum            TEXT,
    first_seen          TIMESTAMPTZ DEFAULT NOW(),
    last_seen           TIMESTAMPTZ DEFAULT NOW(),
    stale               BOOLEAN DEFAULT FALSE,

    UNIQUE(source, source_id)
);

CREATE INDEX idx_sr_spot ON source_records(spot_id);
CREATE INDEX idx_sr_source ON source_records(source);

-- ═══════════════════════════════════════════════════════════════
-- REVIEWS: reviews de todas las fuentes
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE reviews (
    id                  SERIAL PRIMARY KEY,
    spot_id             INT REFERENCES spots(id) ON DELETE CASCADE,
    source              TEXT NOT NULL,
    source_review_id    TEXT,
    texto               TEXT,
    rating              REAL,
    autor               TEXT,
    fecha               DATE,
    idioma              TEXT,
    llm_analysis        JSONB,

    UNIQUE(source, source_review_id)
);

CREATE INDEX idx_reviews_spot ON reviews(spot_id);
CREATE INDEX idx_reviews_source ON reviews(source);

-- ═══════════════════════════════════════════════════════════════
-- SPOT_ENRICHMENTS: scores semánticos pre-computados por LLM
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE spot_enrichments (
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

    llm_summary_es      TEXT,
    llm_summary_en      TEXT,

    reviews_analyzed    INT,
    confidence          REAL,
    model_used          TEXT,
    processed_at        TIMESTAMPTZ DEFAULT NOW(),
    stale               BOOLEAN DEFAULT FALSE
);

-- ═══════════════════════════════════════════════════════════════
-- SPOT_EMBEDDINGS: vectores para búsqueda semántica
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE spot_embeddings (
    spot_id             INT PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,
    embedding           vector(384),
    model               TEXT DEFAULT 'all-MiniLM-L6-v2',
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_embeddings_hnsw ON spot_embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ═══════════════════════════════════════════════════════════════
-- FUENTES_CONFIG: registro de fuentes y su estado
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE fuentes_config (
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

-- ═══════════════════════════════════════════════════════════════
-- SCRAPER_LOG: historial de ejecuciones
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE scraper_log (
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
