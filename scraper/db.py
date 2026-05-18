import asyncio
import asyncpg
import json
from datetime import datetime, timezone
from loguru import logger
from config import Config


async def create_pool(config: Config) -> asyncpg.Pool:
    for i in range(10):
        try:
            pool = await asyncpg.create_pool(
                dsn=config.db_dsn,
                min_size=1,
                max_size=config.max_workers + 2
            )
            logger.info("Conexión a la base de datos establecida.")
            return pool
        except Exception as e:
            logger.warning(f"Intento {i+1}/10: {e}. Reintentando en 5s...")
            await asyncio.sleep(5)
    raise Exception("No se pudo conectar a la base de datos tras 10 intentos")


# ═══════════════════════════════════════════════════════════════
# SPOTS — CRUD canónico
# ═══════════════════════════════════════════════════════════════

async def find_spot_cercano(conn: asyncpg.Connection, lat: float, lon: float,
                             radio_metros: float = 100) -> dict | None:
    row = await conn.fetchrow("""
        SELECT id, canonical_name, tipo, fuentes,
               ST_Distance(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography) AS dist_m
        FROM spots
        WHERE ST_DWithin(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography, $3)
          AND activo = TRUE
        ORDER BY dist_m ASC
        LIMIT 1
    """, lat, lon, radio_metros)
    return dict(row) if row else None


async def crear_spot(conn: asyncpg.Connection, data: dict) -> int:
    row = await conn.fetchrow("""
        INSERT INTO spots (
            canonical_name, lat, lon, country_iso, region, tipo,
            gratuito, precio_info, agua_potable, vaciado_negras, vaciado_grises,
            electricidad, ducha, wifi, wc_publico, perros, acceso_grandes,
            num_plazas, altura_max_m, temporada_apertura,
            master_rating, total_reviews, fuentes,
            descripcion_es, descripcion_en, descripcion_fr, descripcion_de,
            descripcion_it, descripcion_nl,
            web, telefono, email, fotos_urls
        ) VALUES (
            $1, $2, $3, $4, $5, $6,
            $7, $8, $9, $10, $11,
            $12, $13, $14, $15, $16, $17,
            $18, $19, $20,
            $21, $22, $23,
            $24, $25, $26, $27, $28, $29,
            $30, $31, $32, $33::jsonb
        )
        RETURNING id
    """,
        data.get("nombre", "Sin nombre"), data["lat"], data["lon"],
        data.get("country_iso"), data.get("region"), data.get("tipo", "otro"),
        data.get("gratuito"), data.get("precio_info"),
        data.get("agua_potable"), data.get("vaciado_negras"), data.get("vaciado_grises"),
        data.get("electricidad"), data.get("ducha"), data.get("wifi"),
        data.get("wc_publico"), data.get("perros"), data.get("acceso_grandes"),
        data.get("num_plazas"), data.get("altura_max_m"), data.get("temporada_apertura"),
        data.get("rating_promedio"), data.get("num_reviews", 0),
        data.get("fuentes", []),
        data.get("descripcion_es"), data.get("descripcion_en"),
        data.get("descripcion_fr"), data.get("descripcion_de"),
        data.get("descripcion_it"), data.get("descripcion_nl"),
        data.get("web"), data.get("telefono"), data.get("email"),
        json.dumps(data.get("fotos_urls", []))
    )
    return row["id"]


async def enriquecer_spot(conn: asyncpg.Connection, spot_id: int,
                           datos: dict, fuente: str) -> None:
    """Añade fuente al spot existente y rellena campos NULL."""
    SKIP = {"lat", "lon", "nombre", "fuentes", "source", "source_id",
            "_topic_id", "verificado"}
    JSONB_FIELDS = {"fotos_urls", "conflictos"}
    sets = []
    vals = []
    i = 1

    for k, v in datos.items():
        if k in SKIP or v is None:
            continue
        col = k
        if k == "rating_promedio":
            col = "master_rating"
        elif k == "num_reviews":
            col = "total_reviews"
        elif k == "nombre":
            col = "canonical_name"

        # Serializar listas/dicts para campos jsonb
        if k in JSONB_FIELDS or isinstance(v, (list, dict)):
            v = json.dumps(v) if not isinstance(v, str) else v
            sets.append(f"{col} = CASE WHEN {col} = '[]'::jsonb OR {col} IS NULL THEN ${i}::jsonb ELSE {col} END")
        else:
            sets.append(f"{col} = COALESCE({col}, ${i})")
        vals.append(v)
        i += 1

    vals.append(fuente)
    fuente_idx = i
    vals.append(spot_id)
    spot_idx = i + 1

    set_clause = ", ".join(sets) + "," if sets else ""

    query = f"""
        UPDATE spots SET
            {set_clause}
            fuentes = array_append(array_remove(fuentes, ${fuente_idx}), ${fuente_idx}),
            updated_at = NOW()
        WHERE id = ${spot_idx}
    """
    await conn.execute(query, *vals)


# ═══════════════════════════════════════════════════════════════
# SOURCE_RECORDS
# ═══════════════════════════════════════════════════════════════

async def upsert_source_record(conn: asyncpg.Connection, spot_id: int,
                                source: str, source_id: str,
                                raw_data: dict, normalized_data: dict) -> int:
    row = await conn.fetchrow("""
        INSERT INTO source_records (
            spot_id, source, source_id, raw_data, normalized_data,
            lat, lon, name, rating, review_count, checksum, last_seen
        ) VALUES (
            $1, $2, $3, $4::jsonb, $5::jsonb,
            $6, $7, $8, $9, $10, $11, NOW()
        )
        ON CONFLICT (source, source_id) DO UPDATE SET
            spot_id = $1,
            raw_data = $4::jsonb,
            normalized_data = $5::jsonb,
            rating = $9,
            review_count = $10,
            checksum = $11,
            last_seen = NOW(),
            stale = FALSE
        RETURNING id
    """,
        spot_id, source, source_id,
        json.dumps(raw_data), json.dumps(normalized_data),
        normalized_data.get("lat"), normalized_data.get("lon"),
        normalized_data.get("nombre"), normalized_data.get("rating_promedio"),
        normalized_data.get("num_reviews"),
        _checksum(normalized_data)
    )
    return row["id"]


def _checksum(data: dict) -> str:
    import hashlib
    s = json.dumps(data, sort_keys=True, default=str)
    return hashlib.md5(s.encode()).hexdigest()


# ═══════════════════════════════════════════════════════════════
# REVIEWS
# ═══════════════════════════════════════════════════════════════

async def upsert_review(conn: asyncpg.Connection, review: dict) -> None:
    await conn.execute("""
        INSERT INTO reviews (spot_id, source, source_review_id, texto, rating, autor, fecha, idioma)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (source, source_review_id) DO NOTHING
    """,
        review["spot_id"], review["source"], review.get("source_review_id"),
        review.get("texto"), review.get("rating"), review.get("autor"),
        review.get("fecha"), review.get("idioma")
    )


# ═══════════════════════════════════════════════════════════════
# SCRAPER LOG
# ═══════════════════════════════════════════════════════════════

async def init_scraper_log(conn: asyncpg.Connection, fuente: str) -> int:
    return await conn.fetchval("""
        INSERT INTO scraper_log (fuente, estado, iniciado_en)
        VALUES ($1, 'running', NOW())
        RETURNING id
    """, fuente)


async def finish_scraper_log(conn: asyncpg.Connection, log_id: int, stats: dict):
    estado = 'ok_con_errores' if stats.get('errores', 0) > 0 else 'ok'
    await conn.execute("""
        UPDATE scraper_log SET
            terminado_en = NOW(),
            spots_nuevos = $1,
            spots_actualizados = $2,
            reviews_nuevas = $3,
            errores = $4,
            estado = $5,
            detalle = $6::jsonb
        WHERE id = $7
    """,
        stats.get("nuevos", 0), stats.get("actualizados", 0),
        stats.get("reviews_nuevas", 0), stats.get("errores", 0),
        estado, json.dumps(stats.get("detalle", {})), log_id
    )


async def update_fuente_config(conn: asyncpg.Connection, fuente: str, stats: dict):
    estado = 'ok_con_errores' if stats.get('errores', 0) > 0 else 'ok'
    spots_total = await conn.fetchval(
        "SELECT COUNT(*) FROM source_records WHERE source = $1", fuente
    )
    await conn.execute("""
        UPDATE fuentes_config SET
            ultimo_run_inicio = $1,
            ultimo_run_fin = NOW(),
            ultimo_run_estado = $2,
            spots_totales = $3,
            errores_ultimo_run = $4
        WHERE nombre = $5
    """, stats.get('iniciado_en'), estado, spots_total,
        stats.get("errores", 0), fuente)
