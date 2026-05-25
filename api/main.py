"""GeoSpots API - semantic geospatial engine."""

import os
from datetime import datetime, timezone
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
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scraper_jobs (
                id          SERIAL PRIMARY KEY,
                source      TEXT NOT NULL,
                job_type    TEXT NOT NULL DEFAULT 'spots',
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                started_at  TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                result      JSONB
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS scraper_jobs_pending_idx "
            "ON scraper_jobs(status, created_at) WHERE status IN ('pending','running')"
        )
        await conn.execute(
            "ALTER TABLE fuentes_config ADD COLUMN IF NOT EXISTS cron_schedule TEXT"
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
async def get_points(
    north: float = Query(..., ge=-90, le=90, description="Latitud superior del bbox"),
    south: float = Query(..., ge=-90, le=90, description="Latitud inferior del bbox"),
    east:  float = Query(..., ge=-180, le=180, description="Longitud derecha del bbox"),
    west:  float = Query(..., ge=-180, le=180, description="Longitud izquierda del bbox"),
    tipo: str | None = Query(None, description="Filtro opcional por tipo"),
    gratuito: bool | None = Query(None),
    limit: int = Query(5000, ge=1, le=20000, description="Máx puntos a devolver"),
):
    """Spots activos dentro de un bbox. Ordenados por master_rating DESC.

    bbox es obligatorio: a 742K spots, devolver todo de golpe satura el
    frontend (~70 MB JSON). El cliente debe pasar el viewport del mapa y
    refetchear en el evento `moveend`. Si el bbox supera el límite, se
    truncan los puntos con menor rating (los más prominentes ganan).
    """
    if north <= south:
        raise HTTPException(400, "north debe ser > south")
    if west > east:
        raise HTTPException(
            400,
            "bbox cruza el antimeridiano (±180). El cliente debe partirlo "
            "en dos llamadas separadas para usar el índice GIST eficientemente."
        )

    # Usa el índice GIST sobre spots.geog: ST_MakeEnvelope(xmin,ymin,xmax,ymax)
    # donde x = lon, y = lat
    conditions = [
        "activo = TRUE",
        "geog && ST_MakeEnvelope($1, $2, $3, $4, 4326)::geography",
    ]
    params: list = [west, south, east, north]
    idx = 5

    if tipo:
        conditions.append(f"tipo = ${idx}")
        params.append(tipo)
        idx += 1
    if gratuito is not None:
        conditions.append(f"gratuito = ${idx}")
        params.append(gratuito)
        idx += 1

    where_clause = " AND ".join(conditions)
    params_for_count = list(params)
    params.append(limit)

    query = f"""
        SELECT id, canonical_name as n, lat, lon, tipo as t,
               gratuito as g, agua_potable as w, master_rating as r,
               num_fuentes as nf, fuentes as f
        FROM spots
        WHERE {where_clause}
        ORDER BY master_rating DESC NULLS LAST
        LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
        # total real dentro del bbox (sin LIMIT), útil para que el cliente
        # decida si necesita acercar el zoom para ver todo.
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM spots WHERE {where_clause}",
            *params_for_count,
        )

    return {
        "bbox": {"north": north, "south": south, "east": east, "west": west},
        "returned": len(rows),
        "total_in_bbox": total,
        "truncated": total > len(rows),
        "spots": [dict(r) for r in rows],
    }


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


def _compute_health(ultimo_fin, ultimo_estado, ultimo_errores, ultimo_nuevos, ultimo_actualizados) -> tuple[str, str]:
    if ultimo_fin is None:
        return "red", "Sin historial"
    now = datetime.now(timezone.utc)
    fin = ultimo_fin if ultimo_fin.tzinfo else ultimo_fin.replace(tzinfo=timezone.utc)
    days = (now - fin).days
    if ultimo_estado == "error":
        return "red", f"Error en ejecución ({days}d)"
    total = (ultimo_nuevos or 0) + (ultimo_actualizados or 0)
    err_pct = (ultimo_errores or 0) / total if total > 0 else 0.0
    if err_pct > 0.20:
        return "red", f"Errores altos ({err_pct:.0%})"
    if days > 60:
        return "red", f"Caducado — {days} días sin ejecutar"
    if err_pct > 0.05 and days > 14:
        return "amber", f"Desfasado ({days}d) con errores ({err_pct:.0%})"
    if err_pct > 0.05:
        return "amber", f"Con errores ({err_pct:.0%})"
    if days > 14:
        return "amber", f"Desfasado — {days} días"
    return "green", f"Al día — hace {days} día{'s' if days != 1 else ''}"


@app.get("/admin/scrapers")
async def admin_scrapers_list():
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            WITH known_sources AS (
                SELECT source AS nombre FROM source_credibility
                UNION SELECT DISTINCT source FROM source_records
                UNION SELECT nombre FROM fuentes_config
            ),
            src_counts AS (
                SELECT source, COUNT(*) AS total_records
                FROM source_records GROUP BY source
            ),
            spot_counts AS (
                SELECT unnest(fuentes) AS source,
                       COUNT(*) AS spots_total,
                       COUNT(*) FILTER (WHERE cardinality(fuentes) = 1) AS spots_exclusive
                FROM spots WHERE activo = TRUE
                GROUP BY 1
            ),
            review_counts AS (
                SELECT source, COUNT(*) AS total_reviews
                FROM reviews GROUP BY source
            ),
            last_run AS (
                SELECT DISTINCT ON (fuente)
                    fuente, estado, iniciado_en, terminado_en,
                    spots_nuevos, spots_actualizados, reviews_nuevas, errores
                FROM scraper_log
                ORDER BY fuente, iniciado_en DESC
            ),
            reviews_support AS (
                SELECT DISTINCT regexp_replace(fuente, '_reviews$', '') AS source
                FROM scraper_log WHERE fuente LIKE '%_reviews'
            ),
            active_jobs AS (
                SELECT source, array_agg(DISTINCT job_type) AS job_types
                FROM scraper_jobs WHERE status IN ('pending','running')
                GROUP BY source
            )
            SELECT
                ks.nombre,
                COALESCE(sc.display_name, ks.nombre) AS display_name,
                sc.base_score,
                sc.coverage_region,
                COALESCE(sc.active, TRUE) AS activa,
                COALESCE(sr.total_records, 0) AS total_records,
                COALESCE(spc.spots_total, 0) AS spots_total,
                COALESCE(spc.spots_exclusive, 0) AS spots_exclusive,
                COALESCE(rc.total_reviews, 0) AS total_reviews,
                lr.estado AS ultimo_estado,
                lr.iniciado_en AS ultimo_inicio,
                lr.terminado_en AS ultimo_fin,
                lr.spots_nuevos AS ultimo_nuevos,
                lr.spots_actualizados AS ultimo_actualizados,
                lr.reviews_nuevas AS ultimo_reviews_nuevas,
                lr.errores AS ultimo_errores,
                fc.cron_schedule,
                (rs.source IS NOT NULL OR COALESCE(rc.total_reviews, 0) > 0) AS has_reviews_support,
                aj.job_types AS active_job_types
            FROM known_sources ks
            LEFT JOIN source_credibility sc ON sc.source = ks.nombre
            LEFT JOIN fuentes_config fc ON fc.nombre = ks.nombre
            LEFT JOIN src_counts sr ON sr.source = ks.nombre
            LEFT JOIN spot_counts spc ON spc.source = ks.nombre
            LEFT JOIN review_counts rc ON rc.source = ks.nombre
            LEFT JOIN last_run lr ON lr.fuente = ks.nombre
            LEFT JOIN reviews_support rs ON rs.source = ks.nombre
            LEFT JOIN active_jobs aj ON aj.source = ks.nombre
            ORDER BY ks.nombre
        """)

    result = []
    for row in rows:
        d = dict(row)
        color, texto = _compute_health(
            d.get("ultimo_fin"), d.get("ultimo_estado"),
            d.get("ultimo_errores"), d.get("ultimo_nuevos"),
            d.get("ultimo_actualizados"),
        )
        d["salud"] = color
        d["salud_texto"] = texto
        result.append(d)

    health_order = {"red": 0, "amber": 1, "green": 2}
    result.sort(key=lambda x: health_order.get(x["salud"], 3))
    return result


@app.get("/admin/scrapers/{nombre}")
async def admin_scraper_detail(nombre: str):
    async with pool.acquire() as conn:
        sc = await conn.fetchrow("SELECT * FROM source_credibility WHERE source = $1", nombre)
        fc = await conn.fetchrow("SELECT * FROM fuentes_config WHERE nombre = $1", nombre)
        total_records = await conn.fetchval(
            "SELECT COUNT(*) FROM source_records WHERE source = $1", nombre
        )
        spot_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE $1 = ANY(fuentes)) AS spots_total,
                COUNT(*) FILTER (WHERE $1 = ANY(fuentes) AND cardinality(fuentes) = 1) AS spots_exclusive
            FROM spots WHERE activo = TRUE
            """,
            nombre,
        )
        total_reviews = await conn.fetchval(
            "SELECT COUNT(*) FROM reviews WHERE source = $1", nombre
        )
        last_run = await conn.fetchrow(
            """
            SELECT fuente, estado, iniciado_en, terminado_en,
                   EXTRACT(EPOCH FROM (terminado_en - iniciado_en))::INT AS duration_s,
                   spots_nuevos, spots_actualizados, reviews_nuevas, errores, detalle
            FROM scraper_log
            WHERE fuente = $1
            ORDER BY iniciado_en DESC LIMIT 1
            """,
            nombre,
        )
        last_reviews_run = await conn.fetchrow(
            """
            SELECT fuente, estado, iniciado_en, terminado_en,
                   EXTRACT(EPOCH FROM (terminado_en - iniciado_en))::INT AS duration_s,
                   spots_nuevos, spots_actualizados, reviews_nuevas, errores, detalle
            FROM scraper_log
            WHERE fuente = $1
            ORDER BY iniciado_en DESC LIMIT 1
            """,
            f"{nombre}_reviews",
        )

    if sc is None and fc is None and total_records == 0:
        raise HTTPException(404, f"Fuente '{nombre}' no encontrada")

    last_run_d = dict(last_run) if last_run else None
    salud_color, salud_texto = _compute_health(
        last_run_d.get("terminado_en") if last_run_d else None,
        last_run_d.get("estado") if last_run_d else None,
        last_run_d.get("errores") if last_run_d else None,
        last_run_d.get("spots_nuevos") if last_run_d else None,
        last_run_d.get("spots_actualizados") if last_run_d else None,
    )
    return {
        "nombre": nombre,
        "display_name": dict(sc)["display_name"] if sc else nombre,
        "credibility": dict(sc) if sc else None,
        "config": dict(fc) if fc else None,
        "stats": {
            "total_records": total_records,
            "spots_total": spot_row["spots_total"] if spot_row else 0,
            "spots_exclusive": spot_row["spots_exclusive"] if spot_row else 0,
            "total_reviews": total_reviews,
        },
        "last_run": last_run_d,
        "last_reviews_run": dict(last_reviews_run) if last_reviews_run else None,
        "salud": salud_color,
        "salud_texto": salud_texto,
    }


@app.post("/admin/scrapers/{nombre}/run")
async def admin_scraper_run(nombre: str, job_type: str = Query("spots", pattern="^(spots|reviews)$")):
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, status FROM scraper_jobs WHERE source=$1 AND job_type=$2 AND status IN ('pending','running')",
            nombre, job_type,
        )
        if existing:
            raise HTTPException(409, f"Ya hay un job {existing['status']} para {nombre} ({job_type})")
        job_id = await conn.fetchval(
            "INSERT INTO scraper_jobs (source, job_type) VALUES ($1,$2) RETURNING id",
            nombre, job_type,
        )
    return {"job_id": job_id, "source": nombre, "job_type": job_type, "status": "pending"}


@app.get("/admin/scrapers/{nombre}/history")
async def admin_scraper_history(nombre: str, limit: int = Query(10, ge=1, le=100)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT fuente, estado, iniciado_en, terminado_en,
                   EXTRACT(EPOCH FROM (terminado_en - iniciado_en))::INT AS duration_s,
                   spots_nuevos, spots_actualizados, reviews_nuevas, errores
            FROM scraper_log
            WHERE fuente = ANY($1::text[])
            ORDER BY iniciado_en DESC
            LIMIT $2
            """,
            [nombre, f"{nombre}_reviews"],
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/admin/scrapers/{nombre}/samples")
async def admin_scraper_samples(nombre: str, limit: int = Query(5, ge=1, le=20)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source_id, name AS nombre_canonico, lat, lon, tipo_original AS tipo, last_seen
            FROM source_records
            WHERE source = $1
            ORDER BY last_seen DESC NULLS LAST
            LIMIT $2
            """,
            nombre,
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/")
async def index():
    return FileResponse("/pwa/index.html")


app.mount("/pwa", StaticFiles(directory="/pwa"), name="pwa")
