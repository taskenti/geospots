# GeoSpots — Motor Geoespacial Semántico

Motor de agregación, deduplicación y búsqueda semántica de spots para autocaravanas/furgonetas camper en Europa. Absorbe 10+ fuentes de POIs, fusiona duplicados geográficos en entidades canónicas, enriquece con LLM pre-computado (Phase 3) y permite búsqueda por lenguaje natural vía embeddings vectoriales (Phase 4).

---

## Stack Técnico

| Capa | Tecnología |
|---|---|
| Lenguaje | Python 3.12 (entornos Docker) / 3.14 (dev local) |
| Async | asyncio + asyncpg |
| HTTP / Scraping | httpx (async) + BeautifulSoup4 + tenacity |
| API | FastAPI + uvicorn |
| Base de datos | PostgreSQL + PostGIS + pgvector + pg_trgm + earthdistance |
| LLM | Google Gemini 2.0 Flash (`gemini-2.0-flash`) |
| Embeddings | Google `text-embedding-004` (768 dims) |
| Contenerización | Docker + docker-compose (4 servicios: db, scraper, enrichment, api) |
| Infra actual | NAS Synology local |

---

## Estructura de Carpetas

```
geospots/
├── CLAUDE.md                    # Este archivo
├── docker-compose.yml           # 4 servicios: db / scraper / enrichment / api
├── .env.example                 # Plantilla de variables de entorno
│
├── db/
│   ├── schema.sql               # Schema completo + triggers + seeds (ejecutado en initdb)
│   ├── migration_phase3.sql     # Migración idempotente para Phase 3 (columnas reviews)
│   └── migration_phase4.sql     # Migración idempotente para Phase 4 (vector 768 dims)
│
├── scraper/                     # Orquestador + fuentes de datos
│   ├── config.py                # Config desde env (dataclass)
│   ├── db.py                    # CRUD canónico: crear_spot, enriquecer_spot, upsert_review…
│   ├── scheduler.py             # Punto de entrada: python -m scheduler [--source] [--all]
│   ├── reconciliar.py           # Motor de reconciliación multi-fuente por credibilidad
│   ├── diagnostico.py           # Script de diagnóstico de conteos en DB
│   ├── sync_db.py               # Sincroniza fuentes_config con source_records reales
│   └── sources/
│       ├── base.py              # AbstractSource: fetch_cell, normalize, run, generate_active_grid
│       ├── park4night.py        # API interna JSON + quadtree adaptativo
│       ├── campercontact.py     # API bbox con subdivisión recursiva + scraping HTML detalle
│       ├── ioverlander.py       # Import offline KMZ
│       ├── furgovw.py           # API JSON global + RSS reviews + papelera (lugares retirados)
│       ├── areasac.py           # HTML scraping (BeautifulSoup)
│       ├── osm.py               # Overpass API
│       ├── [20+ fuentes más]    # Ver docs/DATA_SOURCES.md para estado completo
│       └── ...
│
├── enrichment/                  # Pipeline LLM Phase 3 + embeddings Phase 4
│   ├── worker.py                # Batch worker: revisa reviews pendientes → extract → store
│   ├── claim_extractor.py       # Extracción de señales: regex primero, Gemini como fallback
│   ├── observation_normalizer.py # Convierte claims a NormalizedObservation con peso
│   ├── state_aggregator.py      # Agrega observaciones → spot_semantic_state con decay temporal
│   ├── dsl_generator.py         # Genera semantic_dsl compacto ("quiet:+0.8 police:-0.2")
│   ├── review_cleaner.py        # Limpieza de reviews + detección de idioma
│   ├── embedding_generator.py   # Genera embeddings 768-dim + búsqueda vectorial híbrida
│   ├── event_detector.py        # Detecta eventos semánticos (police_burst, etc.)
│   ├── signal_registry.py       # Definiciones estáticas de signal_types
│   └── prompts.py               # Prompts para extracción Gemini
│
├── api/
│   ├── main.py                  # FastAPI: /health /points /spot/:id /search /search/semantic /dashboard
│   └── requirements.txt
│
├── jobs/                        # Scripts de mantenimiento ejecutables
│   ├── nightly_embeddings.py    # Regenera embeddings stale + genera nuevos
│   ├── nightly_events.py        # Detecta eventos semánticos nocturnos
│   ├── full_recompute.py        # Recompute completo de spot_semantic_state
│   └── validate_phase3.py       # Validación de integridad del pipeline Phase 3
│
├── data/
│   ├── ioverlander.kmz          # Dump offline de iOverlander (actualizar manualmente)
│   └── ne_50m_admin_0_countries.json  # GeoJSON de países para clasificación geográfica
│
├── pwa/
│   └── index.html               # Frontend PWA (MapLibre GL + chat Gemini)
│
└── docs/                        # Documentación de fases y fuentes
    ├── ARCHITECTURE.md          # Diagrama detallado de capas y flujo de datos
    ├── DATA_SOURCES.md          # Tabla de fuentes, campos, estado y estrategia de merge
    ├── DATA_MODEL.md            # Entidades, campos, relaciones, índices
    ├── DEVELOPMENT.md           # Setup, env vars, cómo añadir una nueva fuente
    └── fase-*.md                # Documentos de diseño de cada fase
```

---

## Comandos Esenciales

```bash
# Levantar stack completo (DB + scraper + enrichment + API)
docker-compose up -d

# Ejecutar un scraper específico
docker-compose exec scraper python scheduler.py --park4night
docker-compose exec scraper python scheduler.py --furgovw
docker-compose exec scraper python scheduler.py --all

# Descargar reviews (separado del scrape de spots)
docker-compose exec scraper python scheduler.py --reviews park4night
docker-compose exec scraper python scheduler.py --reviews campercontact

# Ejecutar reconciliación multi-fuente
docker-compose exec scraper python scheduler.py --reconciliar

# Pipeline de enriquecimiento LLM (Phase 3)
docker-compose exec enrichment python -m enrichment.worker --batch-size 1000

# Generar embeddings (Phase 4)
docker-compose exec enrichment python -m jobs.nightly_embeddings

# Diagnóstico de estado de la DB
docker-compose exec scraper python diagnostico.py

# Sincronizar fuentes_config con source_records reales
docker-compose exec scraper python sync_db.py

# Validar Phase 3
docker-compose exec enrichment python -m jobs.validate_phase3

# Acceso directo a PostgreSQL (puerto expuesto: 25433)
psql -h localhost -p 25433 -U geospots -d geospots
```

---

## Variables de Entorno

| Variable | Descripción | Obligatoria |
|---|---|---|
| `POSTGRES_DB` | Nombre de la base de datos | Sí |
| `POSTGRES_USER` | Usuario PostgreSQL | Sí |
| `POSTGRES_PASSWORD` | Contraseña PostgreSQL | Sí |
| `GEMINI_API_KEY` | API key de Google (embeddings + LLM enrichment + search) | Sí para Phase 3/4 |
| `API_SECRET_KEY` | Clave para middleware de autenticación de la API | Opcional (sin key = sin auth) |
| `STAYFREE_XSRF_TOKEN` | Token XSRF para StayFree (capturar de DevTools) | Solo para StayFree |
| `REQUEST_DELAY_SECONDS` | Delay entre requests (default 2s) | No |
| `MAX_WORKERS` | Semáforo de concurrencia para scrapers (default 3) | No |
| `LOG_LEVEL` | Nivel de log loguru (default INFO) | No |

---

## Arquitectura en Diagrama

```
FUENTES EXTERNAS (20+ scrapers)
        │
        ▼
┌─────────────────────────────────────────────────────────┐
│ CAPA 1: INGESTA (scraper/sources/*.py)                  │
│  AbstractSource → fetch_cell → normalize → dedup        │
│  Grid: Europe/World (1°×1° a 0.125°×0.125°)            │
└─────────────────┬───────────────────────────────────────┘
                  │ upsert_source_record + crear/enriquecer_spot
                  ▼
┌─────────────────────────────────────────────────────────┐
│ CAPA 2: CANONICAL MODEL (PostgreSQL/PostGIS)            │
│  spots ← source_records (1:N)                           │
│  spots ← reviews (1:N)                                  │
│  Dedup: ST_DWithin(100m) + nombre similarity             │
│  Reconciliación: jerarquía de credibilidad por campo    │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ CAPA 3: SEMANTIC ENRICHMENT (enrichment/)               │
│  reviews → clean → extract_claims (regex+Gemini)        │
│  → normalize_claims → update_semantic_state             │
│  → extracted_claims → normalized_observations           │
│  → spot_semantic_state (quietness, safety, beauty...)   │
│  → spot_semantic_snapshots (historial de cambios)       │
│  → semantic_events (police_burst, etc.)                 │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ CAPA 4: VECTOR EMBEDDINGS (enrichment/embedding_generator.py) │
│  spot + semantic_state → texto compuesto → embed 768d   │
│  Model: Google text-embedding-004                       │
│  Index: HNSW coseno en pgvector                         │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ CAPA 5 (Planeada): VISUAL + GEO INTELLIGENCE            │
│  Fotos → Gemini Vision → claims → semantic_state         │
│  Coordenadas → DEM/OSM → geo_claims → semantic_state    │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│ API (api/main.py FastAPI)                               │
│  /points          → todos los spots (mapa)              │
│  /spot/:id        → detalle + sources + reviews         │
│  /search          → SQL: geo + tipo + servicios         │
│  /search/semantic → Gemini intent + vector + geo        │
│  /dashboard       → stats agregadas                     │
└─────────────────────────────────────────────────────────┘
```

---

## Fuentes de Datos — Resumen

| Fuente | Tipo acceso | Estado | Cobertura |
|---|---|---|---|
| park4night | API JSON interna (quadtree) | ✅ Integrada | EU/WW |
| ioverlander | KMZ offline | ✅ Integrada | WW |
| campercontact | API bbox JSON + HTML detalle | ✅ Integrada | EU |
| furgovw | API JSON + RSS + papelera | ✅ Integrada | ES |
| areasac | HTML scraping | ✅ Integrada | ES |
| osm | Overpass API | ✅ Integrada | EU |
| caramaps | API bbox JSON | 🔧 Scraper escrito | ES/FR/IT |
| searchforsites | HTML/API | 🔧 Scraper escrito | UK |
| stayfree | API app móvil (XSRF) | 🔧 Scraper escrito | EU |
| promobil | API/HTML | 🔧 Scraper escrito | DE/AT/CH |
| camperstop | API | 🔧 Scraper escrito | EU |
| vansite, nomady, campspace, roadsurfer, wtmg, alpacacamping, womostell, thedyrt, portugaleasycamp | Varios | 🔧 Scraper escrito | EU/WW |
| campingcarinfos | Descarga global ASCII (ZIP+POI) | ✅ Integrada | EU (43 países) |

---

## Modelo de Datos — Entidades Principales

| Tabla | Propósito | Clave |
|---|---|---|
| `spots` | Entidad canónica (1 por lugar físico) | `id`, `geog` (PostGIS), `fuentes[]` |
| `source_records` | Raw + normalizado por fuente | `(source, source_id)` único |
| `reviews` | Reviews de todas las fuentes | `(source, source_review_id)` único |
| `spot_semantic_state` | Estado semántico agregado (Phase 3) | `spot_id` FK |
| `spot_embeddings` | Vectores 768-dim (Phase 4) | `spot_id` FK |
| `extracted_claims` | Señales extraídas de reviews | `review_id`, `signal_type` |
| `normalized_observations` | Observaciones normalizadas con peso | `claim_id`, `spot_id`, `signal_type` |
| `signal_types` | Registro de señales con decay | `id` (text slug) |
| `source_credibility` | Pesos de credibilidad por fuente | `source` PK |
| `spot_geo` | Análisis geoespacial (Phase 6) | `spot_id` FK — **0 filas aún** |

---

## Convenciones de Código

- **Async everywhere**: todas las funciones de DB son `async def` con `asyncpg`
- **Cada fuente sobreescribe `run()`** si su estrategia difiere de la base (P4N, Furgovw, iOverlander lo hacen)
- **`normalize()`** devuelve siempre el mismo schema: `source_id, nombre, lat, lon, tipo, gratuito, ...`
- **`enriquecer_spot()`** usa `COALESCE(col, $val)` — nunca sobreescribe un valor ya existente
- **Checksums MD5** en `source_records` para detección de cambios incrementales
- **`fuentes[]`** en `spots` es el array de fuentes que conocen el spot — es la fuente de verdad para multi-fuente
- **Logging**: `loguru` en todo el proyecto. El scheduler loga cada lote de 20 celdas.
- **Retries**: `tenacity` con exponential backoff en P4N y OSM

---

## Lo que NO Hacer

1. **No sobreescribir raw_data en source_records** — es inmutable por diseño (principio "raw data es sagrada")
2. **No eliminar `texto` de reviews** — `texto_original` / `texto_limpio` / `texto_dsl` coexisten intencionalmente
3. **No asumir que spot.geohash7 se usa para dedup** — la columna existe pero el dedup real usa `ST_DWithin`. El geohash es solo un campo informativo
4. **No confundir `spot_enrichments` con `spot_semantic_state`** — la primera es legacy (Phase 3 viejo), la segunda es el sistema activo. La API consulta primero `spot_semantic_state` y cae en `spot_enrichments` como fallback
5. **No ejecutar `--all` sin control** — corre los 20+ scrapers secuencialmente. Puede tardar horas
6. **No usar `describe_it = "..." text` en coordenadas de Furgovw** — el API tiene `lat` y `lng` **intercambiados** (bug conocido del servidor, compensado en `normalize()`)
7. **No borrar `fuentes_config`** — es la tabla de estado de los scrapers. `source_credibility` es distinta (pesos de confianza)
8. **Recordar que `campingcarinfos` es bulk-download**: descarga un único ZIP de ~1.1MB con 24K POIs globales. No usa grid. Solo aporta tipo + coordenadas + país (no servicios, ni precios, ni reviews) — es complementaria a fuentes ricas como park4night
9. **Embeddings solo funcionan para spots que tienen `spot_semantic_state`** — la query de generación hace `JOIN spot_semantic_state`

---

## Contexto para Reanudar Trabajo con IA

**Estado actual de fases:**
- Phase 0 (infra) ✅ | Phase 1 (data) ~70% ✅ | Phase 2 (canonical) ✅ | Phase 3 (LLM enrichment) ✅ | Phase 4 (vector search) ✅ | Phase 5 (visual) 📋 | Phase 6 (geo) 📋 | Phase 7 (product) 📋

**Datos en producción (Mayo 2026):** ~125K spots activos, ~500K reviews, 5-6 fuentes integradas, ~338K spots con fotos (no descargadas)

**El pipeline de enriquecimiento** corre como: reviews (llm_processed=FALSE) → worker.py → extract_claims → normalize → update_semantic_state. El embedding batch corre desde jobs/nightly_embeddings.py.

**La búsqueda semántica** está en `/search/semantic`: Gemini extrae intención → embedding query → ST_DWithin + SQL filters + pgvector ranking → Gemini genera respuesta.
