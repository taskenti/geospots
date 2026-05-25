"""GeoSpots API - semantic geospatial engine."""

import os
import asyncpg
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from enrichment.embedding_generator import buscar_spots, generar_respuesta_busqueda

app = FastAPI(title="GeoSpots API", version="1.1.0")

pool: asyncpg.Pool | None = None
API_KEY = os.environ.get("API_SECRET_KEY", "")


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(
        dsn=(
            f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
            f"@{os.environ['DB_HOST']}:{os.environ.get('DB_PORT', '5432')}"
            f"/{os.environ['DB_NAME']}"
        ),
        min_size=2,
        max_size=10,
    )
    logger.info("GeoSpots API ready")


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


@app.middleware("http")
async def check_api_key(request: Request, call_next):
    if request.url.path in ("/health", "/", "/favicon.ico") or request.url.path.startswith("/pwa"):
        return await call_next(request)
    key = request.headers.get("X-API-Key", "")
    if API_KEY and key != API_KEY:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return await call_next(request)


@app.get("/health")
async def health():
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM spots WHERE activo = TRUE")
    return {"status": "ok", "spots": count, "version": "1.1.0"}


@app.get("/points")
async def get_points():
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, canonical_name as n, lat, lon, tipo as t,
                   gratuito as g, agua_potable as w, master_rating as r,
                   num_fuentes as nf, fuentes as f
            FROM spots WHERE activo = TRUE
            """
        )
    return [dict(r) for r in rows]


@app.get("/spot/{spot_id}")
async def get_spot(spot_id: int):
    async with pool.acquire() as conn:
        spot = await conn.fetchrow("SELECT * FROM spots WHERE id = $1", spot_id)
        if not spot:
            raise HTTPException(404, "Spot no encontrado")

        sources = await conn.fetch(
            "SELECT source, name, rating, review_count, last_seen "
            "FROM source_records WHERE spot_id = $1",
            spot_id,
        )

        enrichment = await conn.fetchrow(
            "SELECT * FROM spot_semantic_state WHERE spot_id = $1", spot_id
        )
        enrichment_source = "spot_semantic_state"
        if not enrichment:
            enrichment = await conn.fetchrow(
                "SELECT * FROM spot_enrichments WHERE spot_id = $1", spot_id
            )
            enrichment_source = "spot_enrichments" if enrichment else None

        reviews = await conn.fetch(
            "SELECT source, texto, texto_limpio, texto_dsl, rating, autor, fecha, idioma "
            "FROM reviews WHERE spot_id = $1 ORDER BY fecha DESC NULLS LAST LIMIT 20",
            spot_id,
        )

    result = dict(spot)
    result["sources"] = [dict(s) for s in sources]
    result["enrichment"] = dict(enrichment) if enrichment else None
    result["enrichment_source"] = enrichment_source
    result["reviews"] = [dict(r) for r in reviews]
    for key in list(result.keys()):
        if isinstance(result[key], (bytes, memoryview)):
            del result[key]
    return result


@app.get("/search")
async def search_spots(
    q: str = Query(None, description="Busqueda por nombre"),
    lat: float = Query(None),
    lon: float = Query(None),
    radio_km: float = Query(50),
    tipo: str = Query(None),
    gratuito: bool = Query(None),
    min_quietness: float = Query(None, ge=0, le=1),
    min_safety: float = Query(None, ge=0, le=1),
    min_beauty: float = Query(None, ge=0, le=1),
    max_police_risk: float = Query(None, ge=0, le=1),
    max_crowd_level: float = Query(None, ge=0, le=1),
    overnight_safe: bool = Query(None),
    limit: int = Query(50, le=200),
):
    semantic_filters = any(
        value is not None
        for value in (
            min_quietness,
            min_safety,
            min_beauty,
            max_police_risk,
            max_crowd_level,
            overnight_safe,
        )
    )
    conditions = ["s.activo = TRUE"]
    params = []
    idx = 1

    if lat is not None and lon is not None:
        conditions.append(
            f"ST_DWithin(s.geog, ST_SetSRID(ST_MakePoint(${idx + 1}, ${idx}), 4326)::geography, ${idx + 2})"
        )
        params.extend([lat, lon, radio_km * 1000])
        idx += 3

    if q:
        conditions.append(f"s.canonical_name ILIKE ${idx}")
        params.append(f"%{q}%")
        idx += 1

    if tipo:
        conditions.append(f"s.tipo = ${idx}")
        params.append(tipo)
        idx += 1

    if gratuito is not None:
        conditions.append(f"s.gratuito = ${idx}")
        params.append(gratuito)
        idx += 1

    if min_quietness is not None:
        conditions.append(f"sss.quietness_score >= ${idx}")
        params.append(min_quietness)
        idx += 1

    if min_safety is not None:
        conditions.append(f"sss.safety_score >= ${idx}")
        params.append(min_safety)
        idx += 1

    if min_beauty is not None:
        conditions.append(f"sss.beauty_score >= ${idx}")
        params.append(min_beauty)
        idx += 1

    if max_police_risk is not None:
        conditions.append(f"COALESCE(sss.police_risk_score, 0) <= ${idx}")
        params.append(max_police_risk)
        idx += 1

    if max_crowd_level is not None:
        conditions.append(f"COALESCE(sss.crowd_level_score, 0) <= ${idx}")
        params.append(max_crowd_level)
        idx += 1

    if overnight_safe is not None:
        conditions.append(f"sss.overnight_safe = ${idx}")
        params.append(overnight_safe)
        idx += 1

    params.append(limit)
    join = (
        "JOIN spot_semantic_state sss ON sss.spot_id = s.id AND sss.stale = FALSE"
        if semantic_filters
        else "LEFT JOIN spot_semantic_state sss ON sss.spot_id = s.id"
    )
    order = "ORDER BY s.master_rating DESC NULLS LAST"
    if lat is not None and lon is not None:
        order = "ORDER BY ST_Distance(s.geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography)"

    query = f"""
        SELECT s.id, s.canonical_name, s.lat, s.lon, s.tipo, s.gratuito,
               s.agua_potable, s.master_rating, s.num_fuentes, s.fuentes,
               sss.quietness_score, sss.safety_score, sss.police_risk_score,
               sss.beauty_score, sss.crowd_level_score, sss.overnight_safe,
               sss.semantic_dsl
        FROM spots s
        {join}
        WHERE {" AND ".join(conditions)}
        {order}
        LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


@app.get("/search/semantic")
async def semantic_search(
    q: str = Query(..., description="Query en lenguaje natural"),
    lat: float = Query(...),
    lon: float = Query(...),
    radio_km: float = Query(50, le=500),
    limit: int = Query(20, le=50),
    with_response: bool = Query(True, description="Incluir respuesta LLM"),
):
    async with pool.acquire() as conn:
        spots, intent = await buscar_spots(conn, q, lat, lon, radio_km, limit)

    result = {"spots": spots, "total": len(spots), "intent": intent}
    if with_response and spots:
        try:
            result["response"] = await generar_respuesta_busqueda(q, spots)
        except Exception as exc:
            logger.warning(f"[semantic_search] response generation failed: {exc}")
            result["response_error"] = str(exc)
    return result


@app.get("/dashboard")
async def dashboard():
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE activo) as total_spots,
                COUNT(*) FILTER (WHERE activo AND num_fuentes > 1) as multi_fuente,
                COUNT(*) FILTER (WHERE activo AND conflictos != '[]'::jsonb) as con_conflictos,
                COUNT(*) FILTER (WHERE activo AND gratuito = TRUE) as gratuitos,
                COUNT(*) FILTER (WHERE activo AND verificado = TRUE) as verificados
            FROM spots
            """
        )
        fuentes = await conn.fetch(
            """
            SELECT source, COUNT(*) as total, MAX(last_seen) as ultimo
            FROM source_records
            GROUP BY source ORDER BY total DESC
            """
        )
        enriched = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM spot_enrichments) AS legacy,
                (SELECT COUNT(*) FROM spot_semantic_state) AS semantic
            """
        )
        config = await conn.fetch(
            "SELECT nombre, activa, spots_totales, ultimo_run_estado, ultimo_run_fin "
            "FROM fuentes_config ORDER BY nombre"
        )
    return {
        "stats": dict(stats),
        "fuentes": [dict(f) for f in fuentes],
        "enriched": dict(enriched),
        "config": [dict(c) for c in config],
    }


@app.get("/")
async def index():
    return FileResponse("/pwa/index.html")


app.mount("/pwa", StaticFiles(directory="/pwa"), name="pwa")
