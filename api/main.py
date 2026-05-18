"""GeoSpots API — Motor geoespacial semántico."""

import os
import json
import asyncpg
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

app = FastAPI(title="GeoSpots API", version="1.0.0")

pool: asyncpg.Pool = None
API_KEY = os.environ.get("API_SECRET_KEY", "")


# ═══════════════════════════════════════════════════════════════
# STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(
        dsn=(
            f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT','5432')}"
            f"/{os.environ['DB_NAME']}"
        ),
        min_size=2,
        max_size=10
    )
    logger.info("GeoSpots API ready")


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


# ═══════════════════════════════════════════════════════════════
# AUTH MIDDLEWARE
# ═══════════════════════════════════════════════════════════════

@app.middleware("http")
async def check_api_key(request: Request, call_next):
    if request.url.path in ("/health", "/", "/favicon.ico") or \
       request.url.path.startswith("/pwa"):
        return await call_next(request)
    key = request.headers.get("X-API-Key", "")
    if API_KEY and key != API_KEY:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return await call_next(request)


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM spots WHERE activo = TRUE")
    return {"status": "ok", "spots": count, "version": "1.0.0"}


# ═══════════════════════════════════════════════════════════════
# SPOTS — endpoints principales
# ═══════════════════════════════════════════════════════════════

@app.get("/points")
async def get_points():
    """Devuelve todos los spots activos para el mapa (compacto)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, canonical_name as n, lat, lon, tipo as t,
                   gratuito as g, agua_potable as w, master_rating as r,
                   num_fuentes as nf, fuentes as f
            FROM spots WHERE activo = TRUE
        """)
    return [dict(r) for r in rows]


@app.get("/spot/{spot_id}")
async def get_spot(spot_id: int):
    """Devuelve detalle completo de un spot con enrichments y sources."""
    async with pool.acquire() as conn:
        spot = await conn.fetchrow("SELECT * FROM spots WHERE id = $1", spot_id)
        if not spot:
            raise HTTPException(404, "Spot no encontrado")

        sources = await conn.fetch(
            "SELECT source, name, rating, review_count, last_seen "
            "FROM source_records WHERE spot_id = $1", spot_id
        )

        enrichment = await conn.fetchrow(
            "SELECT * FROM spot_enrichments WHERE spot_id = $1", spot_id
        )

        reviews = await conn.fetch(
            "SELECT source, texto, rating, autor, fecha, idioma "
            "FROM reviews WHERE spot_id = $1 ORDER BY fecha DESC NULLS LAST LIMIT 20",
            spot_id
        )

    result = dict(spot)
    result["sources"] = [dict(s) for s in sources]
    result["enrichment"] = dict(enrichment) if enrichment else None
    result["reviews"] = [dict(r) for r in reviews]

    # Serializar tipos especiales
    for key in result:
        if isinstance(result[key], (bytes, memoryview)):
            del result[key]

    return result


@app.get("/search")
async def search_spots(
    q: str = Query(None, description="Búsqueda por nombre"),
    lat: float = Query(None), lon: float = Query(None),
    radio_km: float = Query(50),
    tipo: str = Query(None),
    gratuito: bool = Query(None),
    limit: int = Query(50, le=200),
):
    """Búsqueda geográfica + filtros."""
    conditions = ["activo = TRUE"]
    params = []
    idx = 1

    if lat is not None and lon is not None:
        conditions.append(
            f"ST_DWithin(geog, ST_SetSRID(ST_MakePoint(${idx+1}, ${idx}), 4326)::geography, ${idx+2})"
        )
        params.extend([lat, lon, radio_km * 1000])
        idx += 3

    if q:
        conditions.append(f"canonical_name ILIKE ${idx}")
        params.append(f"%{q}%")
        idx += 1

    if tipo:
        conditions.append(f"tipo = ${idx}")
        params.append(tipo)
        idx += 1

    if gratuito is not None:
        conditions.append(f"gratuito = ${idx}")
        params.append(gratuito)
        idx += 1

    params.append(limit)

    where = " AND ".join(conditions)
    order = "ORDER BY master_rating DESC NULLS LAST"
    if lat is not None:
        order = f"ORDER BY ST_Distance(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography)"

    query = f"""
        SELECT id, canonical_name, lat, lon, tipo, gratuito,
               agua_potable, master_rating, num_fuentes, fuentes
        FROM spots
        WHERE {where}
        {order}
        LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


@app.get("/debug_furgovw")
async def debug_furgovw():
    async with pool.acquire() as conn:
        spots = await conn.fetch("""
            SELECT id, source, lat, lon, name
            FROM source_records
            WHERE source = 'furgovw'
            LIMIT 10
        """)
        
        matches = await conn.fetch("""
            SELECT s.id, s.canonical_name, s.lat, s.lon, s.fuentes
            FROM spots s
            WHERE 'furgovw' = ANY(s.fuentes)
            LIMIT 10
        """)
        
        total_furgovw = await conn.fetchval("SELECT COUNT(*) FROM source_records WHERE source = 'furgovw'")
        total_spots_con_furgovw = await conn.fetchval("SELECT COUNT(*) FROM spots WHERE 'furgovw' = ANY(fuentes)")
        
    return {
        "total_source_records": total_furgovw,
        "total_spots_con_furgovw": total_spots_con_furgovw,
        "source_records": [dict(s) for s in spots],
        "spots": [dict(m) for m in matches]
    }

# ═══════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════

@app.get("/dashboard")
async def dashboard():
    async with pool.acquire() as conn:
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE activo) as total_spots,
                COUNT(*) FILTER (WHERE activo AND num_fuentes > 1) as multi_fuente,
                COUNT(*) FILTER (WHERE activo AND conflictos != '[]'::jsonb) as con_conflictos,
                COUNT(*) FILTER (WHERE activo AND gratuito = TRUE) as gratuitos,
                COUNT(*) FILTER (WHERE activo AND verificado = TRUE) as verificados
            FROM spots
        """)

        fuentes = await conn.fetch("""
            SELECT source, COUNT(*) as total, MAX(last_seen) as ultimo
            FROM source_records
            GROUP BY source ORDER BY total DESC
        """)

        enriched = await conn.fetchval(
            "SELECT COUNT(*) FROM spot_enrichments"
        )

        config = await conn.fetch(
            "SELECT nombre, activa, spots_totales, ultimo_run_estado, ultimo_run_fin "
            "FROM fuentes_config ORDER BY nombre"
        )

    return {
        "stats": dict(stats),
        "fuentes": [dict(f) for f in fuentes],
        "enriched": enriched,
        "config": [dict(c) for c in config],
    }


# ═══════════════════════════════════════════════════════════════
# PWA STATIC FILES
# ═══════════════════════════════════════════════════════════════

@app.get("/")
async def index():
    return FileResponse("/pwa/index.html")

app.mount("/pwa", StaticFiles(directory="/pwa"), name="pwa")
