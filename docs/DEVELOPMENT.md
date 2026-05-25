# GeoSpots — Guía de Desarrollo

## Setup desde Cero

### Prerrequisitos

- Docker + Docker Compose
- Python 3.11+ (para desarrollo local sin Docker)
- Git

### 1. Clonar y configurar entorno

```bash
git clone <repo>
cd geospots
cp .env.example .env
# Editar .env con tus credenciales (ver sección Variables de Entorno)
```

### 2. Levantar la base de datos

```bash
docker-compose up -d db
```

Esto levanta PostgreSQL en el puerto **25433** (no 5432, para no colisionar con instalaciones locales).
Esperar ~5 segundos a que el container inicialice.

### 3. Inicializar el schema

```bash
# Aplicar schema base (extensiones, tablas, triggers, seeds)
docker-compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB -f /docker-entrypoint-initdb.d/schema.sql

# Alternativa desde el host:
psql -h localhost -p 25433 -U geospots -d geospots -f db/schema.sql
```

Si el proyecto ya tiene datos y solo hay nuevas migraciones:
```bash
psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_phase3.sql
psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_phase4.sql
```

Todas las migraciones son **idempotentes** (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`).

### 4. Levantar todos los servicios

```bash
docker-compose up -d
```

Servicios:
- `db`: PostgreSQL + PostGIS + pgvector (puerto 25433)
- `scraper`: Contenedor para ejecutar scrapers manualmente
- `enrichment`: Worker Phase 3 — procesa reviews automáticamente
- `api`: FastAPI (puerto 18889)

### 5. Verificar que todo funciona

```bash
# Health check de la API
curl http://localhost:18889/health

# Conectar a DB
psql -h localhost -p 25433 -U geospots -d geospots -c "SELECT COUNT(*) FROM spots;"

# Ver logs del worker de enrichment
docker-compose logs -f enrichment
```

---

## Variables de Entorno

Definidas en `.env` (copiar de `.env.example`):

| Variable | Requerida | Descripción | Ejemplo |
|---|---|---|---|
| `POSTGRES_DB` | Sí | Nombre de la base de datos | `geospots` |
| `POSTGRES_USER` | Sí | Usuario de PostgreSQL | `geospots` |
| `POSTGRES_PASSWORD` | Sí | Contraseña de PostgreSQL | `secretpassword` |
| `GEMINI_API_KEY` | Sí* | API key de Google Gemini | `AIza...` |
| `API_SECRET_KEY` | No | Key para autenticar la API REST | `mysecretkey` |
| `STAYFREE_XSRF_TOKEN` | Solo StayFree | Token CSRF para scraper StayFree | `eyJ...` |

*`GEMINI_API_KEY` es obligatoria para Phase 3 (enrichment), Phase 4 (embeddings), y búsqueda semántica. Sin ella, los scrapers básicos funcionan pero el enriquecimiento LLM falla.

### Obtener GEMINI_API_KEY

1. Ir a [Google AI Studio](https://aistudio.google.com/)
2. Crear proyecto o usar existente
3. Generar API key en "Get API key"
4. Copiar en `.env`

Modelos usados:
- `gemini-2.0-flash-exp`: claim extraction + intent extraction + respuestas
- `text-embedding-004`: embeddings de 768 dimensiones

### Obtener STAYFREE_XSRF_TOKEN

1. Abrir la web de StayFree en el navegador
2. Iniciar sesión
3. Abrir DevTools → Network
4. Hacer cualquier request
5. Copiar el header `X-XSRF-TOKEN` de cualquier request autenticado
6. Pegar en `.env`

El token caduca. Si el scraper de StayFree falla con 403/419, regenerar siguiendo estos pasos.

---

## Comandos Esenciales

### Scrapers

```bash
# Ejecutar un scraper específico
docker-compose run --rm scraper python scheduler.py --park4night
docker-compose run --rm scraper python scheduler.py --campercontact

# Descargar reviews de un scraper (desacoplado del scrape de spots)
docker-compose run --rm scraper python scheduler.py --reviews park4night

# Ejecutar todos los scrapers (secuencial, ~horas)
docker-compose run --rm scraper python scheduler.py --all

# Reconciliar datos multi-fuente
docker-compose run --rm scraper python scheduler.py --reconciliar

# Fuentes disponibles:
# campercontact, park4night, ioverlander, furgovw, areasac, osm,
# searchforsites, wtmg, nomady, campspace, roadsurfer, vansite,
# portugaleasycamp, caramaps, stayfree, promobil, camperstop,
# alpacacamping, womostell, thedyrt
```

### Enrichment (Phase 3)

```bash
# El worker corre automáticamente en docker-compose
# Para lanzarlo manualmente:
docker-compose run --rm enrichment python -m enrichment.worker --batch-size 100

# Generar embeddings (Phase 4)
docker-compose run --rm enrichment python -m enrichment.embedding_generator
```

### Base de Datos

```bash
# Conectar directamente
psql -h localhost -p 25433 -U geospots -d geospots

# Stats rápidas
psql -h localhost -p 25433 -U geospots -d geospots -c "
SELECT
    (SELECT COUNT(*) FROM spots WHERE activo=TRUE) AS spots_activos,
    (SELECT COUNT(*) FROM source_records) AS source_records,
    (SELECT COUNT(*) FROM reviews) AS reviews,
    (SELECT COUNT(*) FROM spot_semantic_state) AS spots_enriquecidos,
    (SELECT COUNT(*) FROM spot_embeddings) AS spots_con_embedding;
"

# Sincronizar contadores de fuentes
docker-compose run --rm scraper python sync_db.py
```

### API

```bash
# Arrancar en desarrollo (recarga automática)
cd api && uvicorn main:app --reload --port 18889

# Con Docker
docker-compose up -d api

# Buscar con API key
curl -H "X-API-Key: mysecretkey" "http://localhost:18889/search?lat=40.4&lon=-3.7&radio_km=50"

# Búsqueda semántica
curl -H "X-API-Key: mysecretkey" "http://localhost:18889/search/semantic?q=sitio+tranquilo+cerca+del+mar"
```

---

## Añadir una Nueva Fuente de Datos

### Paso 1: Crear el archivo del scraper

```python
# scraper/sources/nueva_fuente.py
from .base import AbstractSource

class NuevaFuenteSource(AbstractSource):
    name = "nueva_fuente"
    rate_limit = 1.0        # segundos entre requests
    grid_step = 1.0         # tamaño de celda en grados
    dedup_radius_m = 100.0  # radio de búsqueda para dedup
    
    async def fetch_cell(self, client, lat_min, lon_min, lat_max, lon_max):
        """Descarga items de la API para la celda dada."""
        resp = await client.get(
            "https://api.nueva-fuente.com/spots",
            params={
                "lat_min": lat_min, "lon_min": lon_min,
                "lat_max": lat_max, "lon_max": lon_max,
            }
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    
    def normalize(self, raw):
        """Convierte un item crudo al schema GeoSpots."""
        lat = raw.get("latitude")
        lon = raw.get("longitude")
        if not lat or not lon:
            return None  # rechazar si sin coordenadas
        
        return {
            "nombre": raw.get("name", "Sin nombre"),
            "lat": float(lat),
            "lon": float(lon),
            "tipo": self._map_tipo(raw.get("category")),
            "source": self.name,
            "source_id": str(raw["id"]),
            
            # Opcionales — solo si la fuente los proporciona
            "gratuito": raw.get("is_free"),
            "precio_aprox": raw.get("price_eur"),
            "agua_potable": raw.get("has_water"),
            "electricidad": raw.get("has_electricity"),
            "fotos_urls": raw.get("photos", []),
            "rating_promedio": self._normalize_rating(raw.get("rating"), scale=5),
            "num_reviews": raw.get("review_count"),
            "url_fuente": f"https://nueva-fuente.com/spot/{raw['id']}",
        }
    
    def _map_tipo(self, categoria):
        """Mapea categorías de la fuente a tipos GeoSpots."""
        TIPO_MAP = {
            "campsite": "camping",
            "wild": "wildcamp",
            "service": "area",
            "parking": "parking",
        }
        return TIPO_MAP.get(categoria, "otro")
    
    def _normalize_rating(self, rating, scale=5):
        """Normaliza rating a escala 0-10."""
        if rating is None:
            return None
        return round(float(rating) * (10 / scale), 2)
```

### Paso 2: Registrar en el scheduler

```python
# scraper/scheduler.py — añadir en SOURCES dict:
SOURCES = {
    # ... fuentes existentes ...
    "nueva_fuente": "sources.nueva_fuente:NuevaFuenteSource",
}
```

### Paso 3: Añadir a credibilidad

```python
# scraper/reconciliar.py — añadir en los campos relevantes de CREDIBILITY:
CREDIBILITY = {
    "tipo": [..., "nueva_fuente"],  # añadir en la posición apropiada
    "gratuito": [..., "nueva_fuente"],
    # etc.
}
```

### Paso 4: Registrar en la base de datos

```sql
-- Añadir a source_credibility
INSERT INTO source_credibility (source, base_score, review_score, geo_accuracy, notes)
VALUES ('nueva_fuente', 0.75, 0.70, 0.80, 'Descripción de la fuente')
ON CONFLICT (source) DO NOTHING;

-- Añadir a fuentes_config
INSERT INTO fuentes_config (nombre, activa, spots_totales)
VALUES ('nueva_fuente', true, 0)
ON CONFLICT (nombre) DO NOTHING;
```

O añadirlo en `sync_db.py` para que sea reproducible.

### Paso 5: Probar

```bash
# Ejecución de prueba
docker-compose run --rm scraper python scheduler.py --nueva_fuente

# Ver stats
docker-compose run --rm scraper python -c "
import asyncio, sys
sys.path.insert(0, '.')
from config import Config
from db import create_pool
import asyncpg

async def main():
    config = Config.from_env()
    pool = await create_pool(config)
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            'SELECT COUNT(*) FROM source_records WHERE source = \$1',
            'nueva_fuente'
        )
        print(f'Source records: {count}')
    await pool.close()

asyncio.run(main())
"
```

### Paso 6: Documentar

Añadir la fuente a:
- `docs/DATA_SOURCES.md` — tabla principal y mapeo de campos
- `CLAUDE.md` — tabla de fuentes en la sección de data sources

---

## Estrategias de Grid disponibles

La clase base `AbstractSource` proporciona `generate_active_grid()` que genera celdas basadas en spots existentes + buffer. Para fuentes que necesitan estrategias distintas:

### BBox estándar (por defecto)
```python
# La base ya lo implementa en run()
# Solo hay que implementar fetch_cell(client, lat_min, lon_min, lat_max, lon_max)
```

### API global (un solo request)
```python
async def run(self, pool, config, log_id):
    # Sobreescribir run() completamente
    async with httpx.AsyncClient() as client:
        items = await self._fetch_all(client)
        # procesar items...
```

### Import offline (KMZ, CSV, etc.)
```python
async def run(self, pool, config, log_id):
    # Leer archivo local, no hacer requests HTTP
    items = self._parse_file("data/source.kmz")
    # procesar items...
```

### Quadtree adaptativo (como park4night)
Sobreescribir `run()` con lógica recursiva: si `len(results) >= LIMIT`, subdividir la celda y re-fetch.

---

## Estructura del Proyecto

```
geospots/
├── .env                    # Credenciales (no commitear)
├── .env.example            # Template de variables
├── docker-compose.yml      # 4 servicios: db, scraper, enrichment, api
├── CLAUDE.md               # Contexto completo del proyecto para IA
│
├── db/
│   ├── schema.sql          # Schema completo (idempotente)
│   ├── migration_phase3.sql # Phase 3 tables y seeds
│   └── migration_phase4.sql # Phase 4 (vector 384→768 dims)
│
├── scraper/
│   ├── scheduler.py        # Orquestador CLI
│   ├── db.py               # Funciones DB (find_spot_cercano, crear_spot, ...)
│   ├── reconciliar.py      # Motor de reconciliación multi-fuente
│   ├── sync_db.py          # Sincroniza contadores en fuentes_config
│   ├── diagnostico.py      # Herramienta de diagnóstico de datos
│   ├── config.py           # Config.from_env()
│   ├── requirements.txt
│   └── sources/
│       ├── base.py         # AbstractSource (clase base)
│       ├── park4night.py   # Quadtree adaptativo
│       ├── campercontact.py # BBox recursiva + HTML scraping
│       ├── ioverlander.py  # Import KMZ offline
│       ├── furgovw.py      # API global + RSS (lat/lng invertidos)
│       ├── areasac.py      # HTML scraping URLs estáticas
│       ├── osm.py          # Overpass API con circuit breaker
│       └── [otras 14 fuentes]
│
├── enrichment/
│   ├── worker.py           # Worker Phase 3 (procesa reviews)
│   ├── review_cleaner.py   # Limpieza + detección idioma
│   ├── claim_extractor.py  # Regex + Gemini fallback
│   ├── observation_normalizer.py # Claims → observaciones normalizadas
│   ├── state_aggregator.py # Agregación con decay temporal
│   ├── dsl_generator.py    # Genera semantic_dsl compacto
│   └── embedding_generator.py # Embeddings + búsqueda semántica
│
├── api/
│   ├── main.py             # FastAPI app
│   └── requirements.txt
│
└── docs/
    ├── ARCHITECTURE.md     # Arquitectura detallada
    ├── DATA_SOURCES.md     # Fuentes y schema de normalización
    ├── DATA_MODEL.md       # Modelo de datos completo
    └── DEVELOPMENT.md      # Esta guía
```

---

## Debugging Común

### El scraper no encuentra spots nuevos

1. Verificar que `generate_active_grid()` genera celdas en la región esperada:
   ```python
   # En el scraper, temporalmente:
   grid = await source.generate_active_grid(pool)
   print(f"Celdas generadas: {len(grid)}")
   ```
2. Si la DB está vacía, la grilla activa también estará vacía. Usar un grid fijo para la primera carga.

### Dedup está fusionando spots que no debería

1. Revisar `EXCLUSION_GROUPS` en `db.py` — puede necesitar añadir el tipo del nuevo spot
2. Ajustar `dedup_radius_m` en la clase del scraper (reducir si hay mucha fusión incorrecta)
3. Revisar similitud de nombres con: `SELECT similarity('nombre_a', 'nombre_b');` en psql

### El worker de enrichment no procesa

1. Verificar que hay reviews con `llm_processed = FALSE AND informativo IS NULL OR informativo = TRUE`
2. Verificar `GEMINI_API_KEY` en `.env`
3. Ver logs: `docker-compose logs -f enrichment`

### La búsqueda semántica no retorna resultados

1. Verificar que hay spots con embeddings: `SELECT COUNT(*) FROM spot_embeddings;`
2. Generar embeddings si faltan: `python -m enrichment.embedding_generator`
3. Verificar que el índice HNSW existe: `\d spot_embeddings` en psql

### Furgovw devuelve coordenadas en el mar

Bug conocido — la API devuelve lat y lng invertidos. Ya está compensado en `furgovw.py:normalize()`. Si el bug aparece de nuevo, verificar que el swap sigue siendo necesario comparando con coordenadas reales.

---

## Convenciones de Código

### Scrapers

- Heredar siempre de `AbstractSource`
- `normalize()` debe retornar `None` si el item no tiene coordenadas válidas
- Nunca hacer requests síncronos — todo debe ser `async`/`await`
- Usar `self.rate_limit` entre requests (ya gestionado por la base en el semáforo)
- Los campos booleanos desconocidos deben ser `None`, no `False`

### DB

- Nunca modificar `raw_data` una vez insertado
- `enriquecer_spot()` usa COALESCE — no sobreescribe valores existentes
- Todas las operaciones DB son `async` con `asyncpg`

### Tests

No hay tests automatizados actualmente. Para verificar un scraper nuevo:
1. Ejecutar en modo seco con un subset pequeño de celdas
2. Verificar con `SELECT * FROM source_records WHERE source = 'nueva_fuente' LIMIT 5;`
3. Verificar que las coordenadas son correctas en un mapa

---

## Añadir Campos al Schema

Si una fuente nueva tiene campos que no existen en el schema:

1. Añadir columna a `spots` con valor DEFAULT NULL:
   ```sql
   ALTER TABLE spots ADD COLUMN nuevo_campo TEXT;
   ```
2. Añadir al dict que retorna `normalize()` en el scraper
3. Añadir al UPDATE en `enriquecer_spot()` en `db.py`
4. Si es relevante para búsqueda, añadir a `CREDIBILITY` en `reconciliar.py`
5. Documentar en `docs/DATA_MODEL.md`

Hacer migrations idempotentes:
```sql
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='spots' AND column_name='nuevo_campo'
    ) THEN
        ALTER TABLE spots ADD COLUMN nuevo_campo TEXT;
    END IF;
END $$;
```
