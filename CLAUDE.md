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

# Ejecutar un scraper específico (desde terminal local)
docker-compose exec scraper python scheduler.py --park4night
docker-compose exec scraper python scheduler.py --furgovw
docker-compose exec scraper python scheduler.py --all

# Descargar reviews (separado del scrape de spots, desde terminal local)
docker-compose exec scraper python scheduler.py --reviews park4night
docker-compose exec scraper python scheduler.py --reviews campercontact

# Si usas la pestaña "Terminal" de un contenedor dentro de Docker Desktop (dentro de geospots-scraper):
python scheduler.py --park4night
python scheduler.py --reviews park4night
python scheduler.py --<nombre_de_la_fuente>
python scheduler.py --reviews <nombre_de_la_fuente>

# Ejecutar reconciliación multi-fuente
docker-compose exec scraper python scheduler.py --reconciliar

# Pipeline de enriquecimiento LLM (Phase 3) — reviews → claims → semantic state
docker-compose exec enrichment python -m enrichment.worker --batch-size 1000

# Pipeline de hechos scrapeados (Phase 3b) — source_records → claims → semantic state
# Correr ANTES del worker LLM para que las señales de alta confianza estén ya en la DB
docker-compose exec enrichment python -m jobs.ingest_spot_facts --batch-size 10000
docker-compose exec enrichment python -m jobs.ingest_spot_facts --country ES  # por país
docker-compose exec enrichment python -m jobs.ingest_spot_facts --dry-run      # validar sin escribir

# Aplicar migración Phase 3 v5 (nuevas señales) a DB existente
psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_phase3_v5.sql

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
| `DEEPSEEK_API_KEY` | API key de DeepSeek (provider alternativo para bulk enrichment) | Sólo si `ENRICHMENT_PROVIDER=deepseek` |
| `ENRICHMENT_PROVIDER` | `gemini` \| `deepseek`. Provider activo del pipeline LLM (default `gemini`) | No |
| `GEMINI_ENRICHMENT_MODEL` | Modelo Gemini activo (default `gemini-2.5-flash-lite`) | No |
| `DEEPSEEK_ENRICHMENT_MODEL` | Modelo DeepSeek activo (default `deepseek-v4-flash`) | No |
| `API_SECRET_KEY` | Clave para middleware de autenticación de la API | Opcional (sin key = sin auth) |
| `STAYFREE_AUTHORIZATION` | JWT Bearer del usuario StayFree (DevTools → Network) | Opcional |
| `STAYFREE_API_TOKEN` | Token estático de la app móvil (vía MITM del APK) | Opcional |
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
│  /points          → spots en bbox (paginado, máx 20K)   │
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
| agricamper | API JSON bulk (WP REST) | ✅ Integrada | IT (~605 agroturismos) |
| campendium | Tiles OSM zoom 8 + REST detail | ✅ Integrada | US/CA |
| campingcarpark | Bulk API gateway + detail por ID | ✅ Integrada | EU (~906 áreas oficiales CCP) |
| campy | GraphQL: LocationsWithinRadius (lista, 90km/grid 1°) + LocationFull (detalle: reviews Google + web/email/tel + resumen IA "sam") | ✅ Integrada con reviews | DACH + EU (microcamping) |
| bobilguiden | Bulk JSON `/places/mobile` (1 request) | ✅ Integrada | NO/SE/FI/DK (~1936 spots) |
| freecampsites | androidApp.php + wp-json comments | ✅ Integrada | US/CA/MX (~2248 spots, boondocking) |
| google_maps | Playwright headless (DOM scraping) | 🧪 Experimental — servicio docker separado `gmaps`, fallback manual | Mundial (enriquecimiento on-demand) |
| google_maps_api | Places API (New) vía httpx (searchText + Place Details) | ✅ Integrada — enriquecimiento dirigido de contacto (telefono/web/direccion/rating). Reviews en punto muerto (TOS). Respeta `GOOGLE_MAPS_DAILY_BUDGET` | Mundial (campings/áreas AC existentes) |

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

## Patrón de actuación (instrucción permanente del usuario)

- **Si por el camino encuentras fallos, los reparas. Esto siempre.** Mientras implementas una tarea, si detectas un bug, una inconsistencia o algo roto en el código que tocas o que está adyacente, arréglalo en el momento (o, si es ajeno al scope y arriesgado, déjalo anotado/escalado) — no lo ignores ni lo dejes "para luego". Esta es una regla de actuación por defecto, no una excepción.
- Corolario: al integrar algo nuevo, revisar que todos los caminos de entrada queden coherentes (ej. T2.1: el blend léxico se aplica tanto en `claim_extractor.extract_claims` como en el path regex-only de `worker._extract_claims_with_retry`).

---

## Convenciones de Código

- **Async everywhere**: todas las funciones de DB son `async def` con `asyncpg`
- **Cada fuente sobreescribe `run()`** si su estrategia difiere de la base (P4N, Furgovw, iOverlander lo hacen)
- **`normalize()`** devuelve siempre el mismo schema: `source_id, nombre, lat, lon, tipo, gratuito, ...`
- **Validación de coordenadas centralizada**: `AbstractSource.coords_validas(lat, lon)` rechaza None, NaN, fuera de rango y (0,0). Aplicada automáticamente en `base.run()`; los scrapers que sobreescriben `run()` deben llamarla tras `normalize()`
- **`enriquecer_spot()`** usa `COALESCE(col, $val)` — nunca sobreescribe un valor ya existente
- **Checksums MD5** en `source_records` para detección de cambios incrementales
- **`fuentes[]`** en `spots` es el array de fuentes que conocen el spot — es la fuente de verdad para multi-fuente
- **Logging**: `loguru` en todo el proyecto. El scheduler loga cada lote de 20 celdas.
- **Retries**: `tenacity` con exponential backoff en P4N y OSM
- **Progreso de Tareas**: El scheduler invoca SIEMPRE `source.run(pool, config, log_id, job_id=job_id)` y `source.download_reviews(pool, config, job_id=job_id)`. **Toda fuente que sobreescriba `run()` o `download_reviews()` DEBE aceptar `job_id: int = None`** o reventará con `TypeError` y el job terminará con `errores:1` sin scrapear nada. Para reportar progreso al panel admin, llamar a `self.update_job_progress(pool, job_id, processed, total, stats)` (helper de `AbstractSource`) dentro del bucle principal; usa `total=0` si el total es desconocido (paginación). El test `tests/test_source_signatures.py` verifica esta convención para las 30 fuentes y falla si alguna nueva la rompe.

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

## Datos regenerables vs. inmutables

Regla operativa del pipeline (Phase 3 hardening). Mantener esta distinción para que cualquier refactor o reproceso pueda rehacerse sin perder información cruda.

**Inmutables — nunca borrar, sobreescribir ni mutar:**
- `source_records.raw_data` — JSON crudo tal cual lo devolvió cada fuente. Es la fuente de verdad para rehacer todo lo demás.
- `reviews.texto` y `reviews.texto_original` — textos originales (multi-idioma).
- `extracted_claims` cuando `review_id IS NOT NULL` — son evidencia atómica anclada a un fragmento concreto de texto.

**Regenerables desde inmutables (se pueden borrar y rehacer sin pérdida):**
- `normalized_observations` — proyección numérica/booleana de los claims con peso y decay.
- `spot_semantic_state` (incluyendo `summary_en`, `tags`, `best_for`, `signals_data`, `quietness_score`, etc.).
- `spot_embeddings` — derivables del estado semántico vía `text-embedding-004`.
- `spot_alerts` cuando `resolved=TRUE` por decay automático (no manual).
- `spot_semantic_snapshots` — historial de cambios, derivable rebobinando observations.
- `texto_limpio`, `texto_dsl`, `idioma` en reviews — derivables del texto original.

**Reglas concretas:**
1. Cualquier cambio que requiera borrar y rehacer regenerables (bumpear `ENRICHMENT_VERSION`, recomputar embeddings, etc.) debe poder ejecutarse sin tocar inmutables.
2. Si un cambio propuesto rompe esta distinción (ej. "necesitamos sobreescribir raw_data para X"), rechazarlo y buscar otra forma.
3. `extracted_claims` con `review_id IS NULL` que vengan de `scraped_facts_v1` son inmutables (anclados a `source_records`). Los que vengan de un LLM mal disciplinado se borran y rehacen.

Esta sección es referencia del plan `docs/fase-3-hardening-pre-batch.md` (Principio operativo #1).

---

## Contexto para Reanudar Trabajo con IA

**Estado actual de fases:**
- Phase 0 (infra) ✅ | Phase 1 (data) ~70% ✅ | Phase 2 (canonical) ✅ | Phase 3 (LLM enrichment) ✅ | Phase 3b (scraped facts) ✅ | Phase 4 (vector search) ✅ | Phase 5 (visual) 📋 | Phase 6 (geo) 📋 | Phase 7 (product) 📋

**Datos en producción (Mayo 2026):** ~125K spots activos, ~5M reviews, 1.08M source records, 20+ fuentes integradas o escritas.

**Phase 3b — Scraped Facts Pipeline:** `jobs/ingest_spot_facts.py` convierte los campos estructurados de `source_records.normalized_data` (agua_potable, campfire, environment_labels, prohibitions, etc.) en `extracted_claims` + `normalized_observations` sin LLM. Cada source_record aporta 1 claim por señal con `source_confidence = source_credibility.base_score`. ~40 señales mapeadas, extractor_name=`scraped_facts_v1`. Correr una vez tras la primera ingesta de scrapers, antes del worker LLM. Idempotente.

**El pipeline de enriquecimiento** corre como: reviews (llm_processed=FALSE) → worker.py → clean → extract_claims (regex primero, LLM fallback) → normalized_observations → update_semantic_state. El embedding batch corre desde jobs/nightly_embeddings.py.

**La búsqueda semántica** está en `/search/semantic`: Gemini extrae intención → embedding query → ST_DWithin + SQL filters + pgvector ranking → Gemini genera respuesta.

**IMPORTANTE — enrichment container:** NO arrancar `docker-compose up enrichment` sin haber implementado throttling si hay billing activo en Gemini. El contenedor tiene `restart: unless-stopped` y procesa en bucle continuo — puede quemar cuota/presupuesto en minutos. Ver docs/fase-3-llm-enrichment.md § "Lecciones aprendidas". Para el batch inicial usar `ENRICHMENT_PROVIDER=deepseek` (~$38 total para 1.05M llamadas LLM).

---

## Pipeline LLM — Dos modos, una decisión

### Capa única `llm_provider`

**Todas las llamadas LLM** del proyecto pasan por `enrichment/llm_provider.py` (`call_llm_sync`). El provider activo lo define `ENRICHMENT_PROVIDER` (env). Llamantes:
- `enrichment/claim_extractor.extract_claims_llm` — extracción de claims desde reviews (worker)
- `enrichment/orchestrator_v2._call_llm` — enrichment a nivel spot (summary v2)
- `enrichment/embedding_generator.extraer_intencion` — search intent
- `enrichment/embedding_generator.generar_respuesta_busqueda` — recomendación en `/search/semantic`

**Regla:** cualquier nueva llamada LLM DEBE usar `call_llm_sync` — nunca importar el SDK de Gemini/DeepSeek directamente.

---

### Los dos pipelines de enrichment: diferencias clave

Hay dos pipelines implementados y funcionales. **No ejecutar los dos sobre el mismo spot** — solapan observaciones en `normalized_observations` y es coste doble.

#### Pipeline A — `worker.py` (review-level) ← DESCARTADO (Menor ROI)

```
review.texto → clean → regex → [si texto ≥120 chars y <3 claims] LLM v1
→ extracted_claims → normalized_observations → update_semantic_state (incremental)
```

| Aspecto | Valor |
|---|---|
| Granularidad | 1 llamada LLM por review |
| Contexto LLM | Solo el texto de una review (~200 chars) |
| Prompt | ~500 tokens input (v1 extraction, system prompt ligero) |
| Output | Claims atómicos (señales numéricas/booleanas) |
| Narrativa | ❌ No genera summary, tags, best_for |
| Reviews marcadas | `llm_processed = TRUE` por cada review procesada |
| Señales obtenidas | ~38 señales según cobertura regex + LLM |
| Reviews pendientes | ~4.5M (205K ya procesadas) |
| **Coste batch completo** | **~$113 con DeepSeek** (2.8M llamadas, tasa LLM 61.7%) |
| **Steady-state** | Gemini free tier (≤1500 reviews/día al LLM) |

**Cuándo usar:** siempre. Es el pipeline principal. Genera las señales que alimentan el filtrado de búsqueda (`/search`) y la búsqueda semántica vectorial.

**Enrutamiento (lógica corregida 2026-05-28; Opción B añadida 2026-05-29):**
- Texto < 120 chars → solo regex, nunca LLM (salvo mención ambigua)
- Texto ≥ 120 chars + regex ≥ 3 claims → solo regex (cobertura suficiente, salvo ambigua)
- Texto ≥ 120 chars + regex 0-2 claims → LLM para complementar
- **Mención de señal de polaridad ambigua (`police_risk`) → fuerza LLM** (Opción B,
  Sprint 8). El regex es ciego a la polaridad (*"police fait des rondes"* = seguridad,
  no riesgo), así que `police_risk` NO se emite por regex; su mención escala al LLM vía
  `claim_extractor.text_mentions_ambiguous_signal()`. `overnight_safe` se resuelve por
  polaridad dentro del regex (prohibición anula mención positiva). Tests:
  `tests/test_polarity_sprint8.py`.

**Opción A — LLM-only (objetivo de producción):** plan completo en
`docs/fase-3-llm-only-plan.md`. Generaliza la Opción B: si la polaridad importa,
decídela con el LLM. Coste ~$182 batch completo / ~$24 España con DeepSeek (vs ~$113 /
~$10.5 híbrido). Ejecutar SOLO con el hard-cap de presupuesto activo y probado. Es lo
que se hará si GeoSpots pasa a producción o genera ingresos.

#### Pipeline B — `orchestrator_v2` (spot-level) ← ACTIVO / APROBADO

```
spot_metadata + SERVICES + top-35 reviews seleccionadas → LLM v4
→ claims de alta calidad + summary_en + tags + best_for + recompute_spot_state (full)
```

| Aspecto | Valor |
|---|---|
| Granularidad | 1 llamada LLM por spot |
| Contexto LLM | Todas las reviews del spot (hasta 35) + metadatos + servicios estructurados |
| Prompt | ~8000 tokens input (v4 system prompt rico, build_spot_user_prompt) |
| Output | Claims de calidad superior + narrativa completa en inglés |
| Narrativa | ✅ Genera `summary_en`, `tags`, `best_for`, `best_season`, `avoid_season` |
| Reviews marcadas | ❌ No marca `llm_processed` — las reviews son contexto, no el objeto procesado |
| Detección de cambios | ✅ El LLM ve contradicciones entre reviews antiguas y recientes |
| Spots candidatos | ~239K (activos con ≥3 reviews) |
| **Coste batch completo** | **~$134 con DeepSeek V4 Flash** (bug de la auditoría corregido) |
| **Cuándo activar** | Ya activo. Genera el máximo ROI en relación calidad/precio. |

**Lo que pierdes sin orchestrator_v2:** narrativa (`summary_en`, `tags`, `best_for`), que afecta a la calidad de `/search/semantic`. Los scores de señales (quietness, safety...) los obtiene igual el worker.py.

---

### Números reales del batch (auditoría 2026-05-28)

Fuente: `jobs/audit_llm_volume.py` sobre 4.5M reviews pendientes (muestra n=5000).

| Métrica | Valor |
|---|---|
| Reviews pendientes con texto | 4.54M |
| De ellas con texto ≥ 120 chars | ~3.20M (70.4%) |
| Tasa de escalado al LLM | 61.7% de las ≥120 chars |
| **LLM calls estimadas (worker.py)** | **~2.8M** |
| Tokens medios por llamada (input) | ~517 (system≈400 + texto≈67 + frame≈50) |
| Distribución texto al LLM | 69% entre 121-300 chars, 26% entre 301-600 chars |
| Ahorro text_trimmer | ~1% (filler es raro en las reviews que llegan al LLM) |

Señales más detectadas por regex: `quietness` (28.6%), `cleanliness` (18.6%), `lake_nearby` (17.4%), `beauty` (10.1%), `overnight_safe` (4.3%).

Señales sin cobertura regex (solo vía LLM o scraped_facts): `campfire_allowed`, `swimming_access`, `train_noise`, `noise_source`, `accessible_pmr`, `caravan_accepted`, `ev_charging`.

### Coste del batch completo (worker.py)

| Provider | Coste | Notas |
|---|---|---|
| **DeepSeek V4 Flash** | **~$113** | Mejor opción — sin rate limits duros |
| Gemini 2.5 Flash Lite | ~$161 | Requiere billing activo en GCP |
| Gemini 2.5 Flash | ~$484 | No recomendado para bulk |

### Plan operativo por países (worker.py)

Procesar **país por país** en este orden:
**Andorra (smoke test) → Portugal → España → Francia → Alemania → Italia → UK → EEUU → resto**

```bash
# Smoke test (Andorra — ~200 reviews, gratis con free tier)
docker-compose exec enrichment python -m enrichment.worker --batch-size 500 --country AD

# Batch real país a país
docker-compose exec enrichment python -m enrichment.worker --batch-size 10000 --country ES
docker-compose exec enrichment python -m enrichment.worker --batch-size 10000 --country FR
# etc.

# O sin filtro (deja que el worker priorice por temperatura/fecha)
docker-compose exec enrichment python -m enrichment.worker --batch-size 50000
```

**Throttling implementado** en worker.py:
- `ENRICHMENT_CONCURRENCY` (default 8): semáforo de llamadas LLM simultáneas
- `ENRICHMENT_INTER_REQUEST_DELAY` (default 0): delay adicional por llamada (Gemini free tier: pon 4.3)
- Backoff exponencial en 429/5xx (2s → 4s → 8s, máx `ENRICHMENT_MAX_BACKOFF`)
- Abort automático si `ENRICHMENT_MAX_CONSECUTIVE_ERRORS` (default 20) fallos seguidos

```bash
# Configuración para Gemini free tier (15 RPM)
ENRICHMENT_PROVIDER=gemini
ENRICHMENT_CONCURRENCY=1
ENRICHMENT_INTER_REQUEST_DELAY=4.3

# Configuración para DeepSeek bulk
ENRICHMENT_PROVIDER=deepseek
ENRICHMENT_CONCURRENCY=8
ENRICHMENT_INTER_REQUEST_DELAY=0
```

**No lanzar enrichment hasta que termine la descarga de reviews** de la fuente principal del país. El worker procesa en continuo — si no hay pendientes, no consume API.
