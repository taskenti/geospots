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
        # Tabla de metadatos compartida API ↔ scraper daemon (heartbeat etc.)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scraper_jobs_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Limpieza one-shot al arrancar: si algún scraper crasheó en el último
        # ciclo, su scraper_log row quedó en 'running'. Marcarlo como zombie.
        cleanup = await _cleanup_stuck_runs(conn, max_hours=12)
        if cleanup["scraper_log_updated"] or cleanup["scraper_jobs_updated"]:
            logger.info(f"[startup] Limpieza zombies: {cleanup}")
    logger.info("GeoSpots API ready")


async def _cleanup_stuck_runs(conn, max_hours: int = 12) -> dict:
    """Marca como zombie los runs colgados >max_hours en estado 'running'.

    Aplica tanto a scraper_log (historial de ejecuciones) como a scraper_jobs
    (cola del panel admin). Idempotente — si no hay nada que limpiar, no
    altera filas.
    """
    log_result = await conn.execute(
        f"UPDATE scraper_log SET estado = 'zombie', terminado_en = NOW() "
        f"WHERE estado = 'running' AND iniciado_en < NOW() - INTERVAL '{max_hours} hours'"
    )
    job_result = await conn.execute(
        f"UPDATE scraper_jobs SET status = 'error', finished_at = NOW(), "
        f"result = COALESCE(result, '{{}}'::jsonb) || jsonb_build_object('error', 'timeout: zombie tras {max_hours}h en running') "
        f"WHERE status IN ('pending','running') AND created_at < NOW() - INTERVAL '{max_hours} hours'"
    )
    return {
        "scraper_log_updated": int(log_result.split()[-1]) if log_result else 0,
        "scraper_jobs_updated": int(job_result.split()[-1]) if job_result else 0,
    }


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


def _compute_health(iniciado_en, terminado_en, estado, errores, nuevos, actualizados) -> tuple[str, str]:
    """Devuelve (color, texto descriptivo) para el semáforo de salud.

    Maneja todos los estados conocidos del scraper_log: ok, ok_con_errores,
    error, running (incluido running viejo = crash probable), zombie, y la
    ausencia total de historial.
    """
    if iniciado_en is None:
        return "red", "Sin historial"

    now = datetime.now(timezone.utc)
    inicio = iniciado_en if iniciado_en.tzinfo else iniciado_en.replace(tzinfo=timezone.utc)
    hours_since_start = (now - inicio).total_seconds() / 3600

    # Job todavía en marcha (o quedó colgado)
    if estado == "running":
        if hours_since_start > 6:
            return "red", f"Crash probable — {int(hours_since_start)}h colgado en 'running'"
        if hours_since_start >= 1:
            return "amber", f"Ejecutándose ({int(hours_since_start)}h)"
        return "amber", f"Ejecutándose ({int(hours_since_start * 60)}min)"

    if estado == "zombie":
        return "red", "Job zombie — quedó sin terminar correctamente"

    if estado == "error":
        return "red", "Error en última ejecución"

    # Estados terminales (ok / ok_con_errores) — usa terminado_en o cae a iniciado_en
    fin = terminado_en if terminado_en else iniciado_en
    if fin.tzinfo is None:
        fin = fin.replace(tzinfo=timezone.utc)
    days = (now - fin).days

    total = (nuevos or 0) + (actualizados or 0)
    err_pct = (errores or 0) / total if total > 0 else 0.0
    n_errs = errores or 0

    # ok_con_errores: nunca verde, mínimo ámbar
    if estado == "ok_con_errores":
        if days > 60:
            return "red", f"Caducado con errores ({days}d, {n_errs} errs)"
        if n_errs > 1000 or err_pct > 0.30:
            return "red", f"Errores altos — {n_errs} errs ({err_pct:.0%})"
        return "amber", f"Con errores ({n_errs}) hace {days}d"

    # Estado ok normal
    if err_pct > 0.20:
        return "red", f"Errores altos ({err_pct:.0%})"
    if days > 60:
        return "red", f"Caducado — {days} días"
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
            WITH all_sources AS (
                SELECT nombre FROM fuentes_config
                UNION
                SELECT DISTINCT source FROM source_records
                UNION
                SELECT DISTINCT regexp_replace(fuente, '_reviews$', '') FROM scraper_log
            ),
            known_sources AS (
                -- Sólo fuentes con datos reales o config explícita.
                -- Excluimos:
                --   · seeds huérfanos de source_credibility (campernight, campininfo,
                --     stellplatz, wikidata, eu_opendata, wikicamps...) — ya filtrados
                --     al no estar en fuentes_config / source_records / scraper_log
                --   · fuentes scratch de desarrollo: *_test, *_dev, *_staging, *_tmp
                SELECT nombre FROM all_sources
                WHERE nombre !~* '_(test|dev|staging|tmp)$'
            ),
            src_counts AS (
                SELECT source,
                       COUNT(*) AS total_records,
                       COUNT(*) FILTER (WHERE COALESCE(stale, FALSE) = FALSE) AS active_records
                FROM source_records GROUP BY source
            ),
            -- Cuenta spots vía JOIN con source_records (NO via spots.fuentes[]):
            -- evita el problema de entradas huérfanas en fuentes[] cuando se
            -- eliminó el record pero no se sincronizó el array. Esto explicaba
            -- por qué CamperContact mostraba más spots (46K) que records (45K).
            spot_counts AS (
                SELECT sr.source,
                       COUNT(DISTINCT s.id) AS spots_total,
                       COUNT(DISTINCT s.id) FILTER (WHERE cardinality(s.fuentes) = 1) AS spots_exclusive
                FROM source_records sr
                JOIN spots s ON s.id = sr.spot_id
                WHERE s.activo = TRUE AND COALESCE(sr.stale, FALSE) = FALSE
                GROUP BY sr.source
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
                COALESCE(sr.active_records, 0) AS active_records,
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
            d.get("ultimo_inicio"), d.get("ultimo_fin"), d.get("ultimo_estado"),
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
        records_row = await conn.fetchrow(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE COALESCE(stale,FALSE)=FALSE) AS active "
            "FROM source_records WHERE source = $1",
            nombre,
        )
        total_records = records_row["total"] if records_row else 0
        active_records = records_row["active"] if records_row else 0
        # Spots con un source_record real (no via fuentes[] huérfano)
        spot_row = await conn.fetchrow(
            """
            SELECT
                COUNT(DISTINCT s.id) AS spots_total,
                COUNT(DISTINCT s.id) FILTER (WHERE cardinality(s.fuentes) = 1) AS spots_exclusive,
                COUNT(DISTINCT s.id) FILTER (WHERE NOT ($1 = ANY(s.fuentes))) AS spots_orphan
            FROM source_records sr
            JOIN spots s ON s.id = sr.spot_id
            WHERE s.activo = TRUE AND COALESCE(sr.stale, FALSE) = FALSE AND sr.source = $1
            """,
            nombre,
        )
        # Spots cuya fuentes[] menciona la fuente pero no hay record activo
        # (anomalía típica: dedup borró el record pero no actualizó el array)
        spots_fuentes_only = await conn.fetchval(
            """
            SELECT COUNT(*) FROM spots s
            WHERE s.activo = TRUE AND $1 = ANY(s.fuentes)
              AND NOT EXISTS (
                SELECT 1 FROM source_records sr
                WHERE sr.spot_id = s.id AND sr.source = $1 AND COALESCE(sr.stale,FALSE) = FALSE
              )
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
        last_run_d.get("iniciado_en") if last_run_d else None,
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
            "active_records": active_records,
            "stale_records": total_records - active_records,
            "spots_total": spot_row["spots_total"] if spot_row else 0,
            "spots_exclusive": spot_row["spots_exclusive"] if spot_row else 0,
            "spots_fuentes_huerfano": spots_fuentes_only or 0,
            "total_reviews": total_reviews,
        },
        "last_run": last_run_d,
        "last_reviews_run": dict(last_reviews_run) if last_reviews_run else None,
        "salud": salud_color,
        "salud_texto": salud_texto,
    }


@app.get("/admin/worker/status")
async def admin_worker_status():
    """Devuelve si el scraper daemon está vivo (heartbeat < 90s)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM scraper_jobs_meta WHERE key = 'worker_heartbeat'"
        )
        pending = await conn.fetchval(
            "SELECT COUNT(*) FROM scraper_jobs WHERE status IN ('pending','running')"
        )
    if not row or not row["value"]:
        return {"alive": False, "last_heartbeat": None, "seconds_ago": None, "pending_jobs": pending}
    try:
        last = datetime.fromisoformat(row["value"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        delta_s = int((datetime.now(timezone.utc) - last).total_seconds())
    except (ValueError, TypeError):
        return {"alive": False, "last_heartbeat": row["value"], "seconds_ago": None, "pending_jobs": pending}
    return {
        "alive": delta_s < 90,  # heartbeat cada 30s, margen 3x
        "last_heartbeat": row["value"],
        "seconds_ago": delta_s,
        "pending_jobs": pending,
    }


@app.get("/admin/scraper_log/recent")
async def admin_scraper_log_recent(limit: int = Query(25, ge=1, le=200)):
    """Feed cronológico de las últimas N entradas de scraper_log.

    Útil para verificar que un comando lanzado por CLI realmente está
    creando rows (si no aparece aquí, el comando falló antes de init).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, fuente, estado, iniciado_en, terminado_en,
                   EXTRACT(EPOCH FROM (COALESCE(terminado_en, NOW()) - iniciado_en))::INT AS elapsed_s,
                   spots_nuevos, spots_actualizados, reviews_nuevas, errores
            FROM scraper_log
            ORDER BY iniciado_en DESC
            LIMIT $1
        """, limit)
    return [dict(r) for r in rows]


@app.post("/admin/scrapers/{nombre}/force-zombie")
async def admin_force_zombie(nombre: str):
    """Marca como zombie el último 'running' de esta fuente (y su variante
    _reviews) sin esperar el umbral de 12h. Para desatascar manualmente."""
    async with pool.acquire() as conn:
        n = await conn.execute(
            "UPDATE scraper_log SET estado='zombie', terminado_en=NOW() "
            "WHERE fuente IN ($1, $1 || '_reviews') AND estado='running'",
            nombre,
        )
    return {"fuente": nombre, "filas_actualizadas": int(n.split()[-1]) if n else 0}


@app.post("/admin/cleanup/zombies")
async def admin_cleanup_zombies(max_hours: int = Query(12, ge=1, le=168)):
    """Marca como zombie scraper_log/scraper_jobs colgados > max_hours."""
    async with pool.acquire() as conn:
        result = await _cleanup_stuck_runs(conn, max_hours=max_hours)
    return result


@app.post("/admin/cleanup/fuentes-huerfanas")
async def admin_cleanup_fuentes_huerfanas(nombre: str = Query(..., description="Fuente a limpiar de spots.fuentes[]")):
    """Elimina `nombre` del array spots.fuentes[] cuando no existe un
    source_record activo. Útil cuando dedup eliminó records pero
    no sincronizó el array (causa de spots_total > total_records).
    """
    async with pool.acquire() as conn:
        n = await conn.execute(
            """
            UPDATE spots SET fuentes = array_remove(fuentes, $1)
            WHERE activo = TRUE AND $1 = ANY(fuentes)
              AND NOT EXISTS (
                SELECT 1 FROM source_records sr
                WHERE sr.spot_id = spots.id AND sr.source = $1 AND COALESCE(sr.stale,FALSE) = FALSE
              )
            """,
            nombre,
        )
    return {"fuente": nombre, "spots_actualizados": int(n.split()[-1]) if n else 0}


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
