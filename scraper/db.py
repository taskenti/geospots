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
                             radio_metros: float = 100, nombre: str = None, tipo: str = None,
                             source: str = None, source_id: str = None,
                             osm_id: str = None, place_id: str = None) -> dict | None:
    # ── Dedup STICKY (estabilidad de dedup) ─────────────────────────────────
    # Si ya conocemos este (source, source_id), devolvemos su spot ACTUAL sin
    # re-evaluar geográficamente. Sin esto, cada re-scrape vuelve a correr el
    # match por distancia: si las coords se mueven unos metros o aparece un spot
    # más cercano, el source_record MIGRA a otro spot y deja el original como
    # duplicado con sus reviews huérfanas (medido: ~310K reviews huérfanas, p4n
    # 267K). La estabilidad prima sobre re-emparejar: el dedup geográfico solo
    # debe decidir para source_ids NUEVOS.
    if source and source_id:
        sticky = await conn.fetchrow("""
            SELECT s.id, s.canonical_name, s.tipo, s.fuentes
            FROM source_records sr
            JOIN spots s ON s.id = sr.spot_id
            WHERE sr.source = $1 AND sr.source_id = $2 AND s.activo = TRUE
        """, source, str(source_id))
        if sticky:
            return dict(sticky)

    # ── Ancla de identidad EXACTA (Sprint 2) ────────────────────────────────
    # osm_id / google_place_id son únicos por entidad física. Si un spot activo
    # ya los tiene, es la misma entidad → match seguro, sin depender de distancia.
    # (telefono/web NO se anclan aquí: un valor compartido por una cadena daría
    #  falsos merges; quedan como señal de auditoría manual.)
    for col, val in (("osm_id", osm_id), ("google_place_id", place_id)):
        if val:
            anchor = await conn.fetchrow(
                f"SELECT id, canonical_name, tipo, fuentes FROM spots "
                f"WHERE {col} = $1 AND activo = TRUE LIMIT 1",
                str(val),
            )
            if anchor:
                return dict(anchor)

    # Buscar candidatos ordenados por distancia
    rows = await conn.fetch("""
        SELECT id, canonical_name, tipo, fuentes,
               ST_Distance(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography) AS dist_m,
               CASE WHEN $4::text IS NOT NULL THEN similarity(canonical_name, $4) ELSE 1.0 END as name_sim
        FROM spots
        WHERE ST_DWithin(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography, $3)
          AND activo = TRUE
        ORDER BY dist_m ASC
        LIMIT 5
    """, lat, lon, radio_metros, nombre)

    if not rows:
        return None

    # Si no se proveen metadatos para matching refinado, devolvemos el más cercano
    if not nombre or not tipo:
        return dict(rows[0])

    nombre_norm = nombre.lower().strip()
    tipo_norm = tipo.lower().strip()

    # Grupos de tipos mutuamente excluyentes para evitar falsas fusiones
    EXCLUSION_GROUPS = {
        "camping": {"wild", "naturaleza", "parking_publico", "parking", "picnic", "area_descanso"},
        "wild": {"camping", "parking_privado", "area_ac", "gasolinera", "marina", "naturaleza"},
        "naturaleza": {"camping", "parking_privado", "area_ac", "gasolinera", "marina", "wild"},
        "parking_publico": {"camping", "wild", "naturaleza"},
        "parking": {"camping", "wild", "naturaleza"},
    }

    for r in rows:
        dist = r["dist_m"]
        c_tipo = (r["tipo"] or "otro").lower().strip()
        c_sim = r["name_sim"] if r["name_sim"] is not None else 0.0

        # Caso 1: Extrema cercanía (< 20 metros) - Match por error de GPS típico
        if dist < 20.0:
            # Salvaguarda: nunca fusionar camping con wild camping
            if (tipo_norm == "camping" and c_tipo in EXCLUSION_GROUPS["camping"]) or \
               (c_tipo == "camping" and tipo_norm in EXCLUSION_GROUPS["camping"]):
                continue
            return dict(r)

        # Caso 2: Distancia media-larga (20m - 100m)
        # Comprobar exclusión de tipos
        if tipo_norm in EXCLUSION_GROUPS and c_tipo in EXCLUSION_GROUPS[tipo_norm]:
            continue
        if c_tipo in EXCLUSION_GROUPS and tipo_norm in EXCLUSION_GROUPS[c_tipo]:
            continue

        # Exigir similitud lingüística de nombre para distancias medias
        if c_sim >= 0.35:
            return dict(r)

    return None


# Dominios de agregadores: NO son la web oficial de un spot. Se usan tanto para
# limpiar `web` como para evitar que `web_domain` agrupe spots NO relacionados.
EXCLUDED_DOMAINS = [
    "campercontact.com", "park4night.com", "caramaps.com", "stayfree.app",
    "campspace.com", "vansite.eu", "searchforsites.co.uk", "alpacacamping.de",
    "welcometomygarden.org", "campingcar-infos.com", "freecampsites.net",
    "campernight.com", "bobilguiden.no", "camperstop.com", "campingcarpark.com",
    "agricamper.it", "roadsurfer.com", "campy.app", "campy.nl", "agricamper.com",
    "thedyrt.com",
]


def _limpiar_web(web: str | None) -> str | None:
    if not web:
        return None
    web_lower = web.lower()
    for domain in EXCLUDED_DOMAINS:
        if domain in web_lower:
            return None
    return web


def normalize_phone(raw: str | None) -> str | None:
    """Normaliza un teléfono a una clave comparable entre fuentes.

    Conserva solo dígitos y el prefijo internacional ('00' inicial → '+').
    Devuelve None si no quedan al menos 6 dígitos. NO valida país: es una clave
    de agrupación para auditoría de duplicados, no E.164 estricto.
    """
    if not raw:
        return None
    s = str(raw).strip()
    plus = s.startswith("+") or s.startswith("00")
    digits = "".join(ch for ch in s if ch.isdigit())
    if s.startswith("00"):
        digits = digits[2:]
    if len(digits) < 6:
        return None
    return ("+" if plus else "") + digits


def extract_domain(url: str | None) -> str | None:
    """Extrae el dominio raíz de una URL como clave de identidad.

    Quita esquema, 'www.', path y querystring. Devuelve None para dominios de
    agregador (park4night.com, etc.): agruparían spots NO relacionados.
    """
    if not url:
        return None
    s = str(url).strip().lower()
    s = s.split("://", 1)[-1]
    s = s.split("/", 1)[0]
    s = s.split("?", 1)[0].split("#", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    s = s.strip(".")
    if not s or "." not in s:
        return None
    for domain in EXCLUDED_DOMAINS:
        if domain in s:
            return None
    return s


async def crear_spot(conn: asyncpg.Connection, data: dict) -> int:
    row = await conn.fetchrow("""
        INSERT INTO spots (
            canonical_name, lat, lon, country_iso, region, tipo,
            gratuito, precio_info, precio_aprox, agua_potable, vaciado_negras, vaciado_grises,
            electricidad, ducha, wifi, wc_publico, perros, acceso_grandes,
            num_plazas, altura_max_m, temporada_apertura,
            master_rating, total_reviews, fuentes,
            descripcion_es, descripcion_en, descripcion_fr, descripcion_de,
            descripcion_it, descripcion_nl,
            web, telefono, email, fotos_urls,
            piscina, lavanderia, gas_recharge, restaurant, juegos_ninos,
            mirador, zona_protegida, online_booking, winter_friendly, apto_motos,
            mtb_friendly, surf_friendly, fishing, climbing, hiking_nearby,
            amperaje, n_enchufes, max_noches, idiomas_hablados, productos_venta,
            servicios_extras
        ) VALUES (
            $1, $2, $3, $4, $5, $6,
            $7, $8, $9, $10, $11, $12,
            $13, $14, $15, $16, $17, $18,
            $19, $20, $21,
            $22, $23, $24,
            $25, $26, $27, $28, $29, $30,
            $31, $32, $33, $34::jsonb,
            $35, $36, $37, $38, $39,
            $40, $41, $42, $43, $44,
            $45, $46, $47, $48, $49,
            $50, $51, $52, $53::text[], $54::text[],
            $55::jsonb
        )
        RETURNING id
    """,
        data.get("nombre", "Sin nombre"), data["lat"], data["lon"],
        data.get("country_iso"), data.get("region"), data.get("tipo", "otro"),
        data.get("gratuito"), data.get("precio_info"), data.get("precio_aprox"),
        data.get("agua_potable"), data.get("vaciado_negras"), data.get("vaciado_grises"),
        data.get("electricidad"), data.get("ducha"), data.get("wifi"),
        data.get("wc_publico"), data.get("perros"), data.get("acceso_grandes"),
        data.get("num_plazas"), data.get("altura_max_m"), data.get("temporada_apertura"),
        data.get("rating_promedio"), data.get("num_reviews", 0),
        data.get("fuentes", []),
        data.get("descripcion_es"), data.get("descripcion_en"),
        data.get("descripcion_fr"), data.get("descripcion_de"),
        data.get("descripcion_it"), data.get("descripcion_nl"),
        _limpiar_web(data.get("web")), data.get("telefono"), data.get("email"),
        json.dumps(data.get("fotos_urls", [])),
        data.get("piscina"), data.get("lavanderia"), data.get("gas_recharge"), data.get("restaurant"), data.get("juegos_ninos"),
        data.get("mirador"), data.get("zona_protegida"), data.get("online_booking"), data.get("winter_friendly"), data.get("apto_motos"),
        data.get("mtb_friendly"), data.get("surf_friendly"), data.get("fishing"), data.get("climbing"), data.get("hiking_nearby"),
        data.get("amperaje"), data.get("n_enchufes"), data.get("max_noches"),
        data.get("idiomas_hablados", []), data.get("productos_venta", []),
        json.dumps(data.get("servicios_extras", {}))
    )
    return row["id"]


SKIP_ENRIQUECER = {"lat", "lon", "nombre", "fuentes", "source", "source_id",
                   "_topic_id", "verificado", "page_url", "host_name", "space_id",
                   "details_fetched"}
JSONB_FIELDS = {"fotos_urls", "conflictos"}
TEXT_ARRAY_FIELDS = {"idiomas_hablados", "productos_venta"}
DEEP_MERGE_JSONB_FIELDS = {"servicios_extras"}


def _deep_merge_jsonb(existing: dict | None, new: dict) -> dict:
    """Merge recursivo por sub-keys con prioridad al valor existente.

    Reglas:
      - Dicts: deep merge. Si key existe en ambos como dict, recursa. Si existe
        en `existing`, se preserva (nunca sobreescribimos info ya almacenada).
        Si solo está en `new`, se añade.
      - Listas: unión dedup (preservando orden de existing primero).
      - Escalares: existing gana si está, si no se toma new.
    """
    if not existing:
        return dict(new) if new else {}
    if not isinstance(existing, dict):
        existing = {}
    result = dict(existing)
    for k, vn in new.items():
        if k not in result:
            result[k] = vn
            continue
        ve = result[k]
        if isinstance(ve, dict) and isinstance(vn, dict):
            result[k] = _deep_merge_jsonb(ve, vn)
        elif isinstance(ve, list) and isinstance(vn, list):
            seen = set()
            merged_list = []
            for item in ve + vn:
                key = json.dumps(item, sort_keys=True, default=str) if isinstance(item, (dict, list)) else str(item)
                if key not in seen:
                    seen.add(key)
                    merged_list.append(item)
            result[k] = merged_list
        # escalar: existing gana, no tocamos
    return result


async def enriquecer_spot(conn: asyncpg.Connection, spot_id: int,
                           datos: dict, fuente: str) -> None:
    """Añade fuente al spot existente y rellena campos NULL.

    SKIP: campos que NO son columnas de `spots` pero pueden venir en el dict
    normalizado (metadata para Phase 2 / source_records / debugging).

    Maneja 4 tipos de columnas:
      - Texto/numérico/bool: COALESCE (no pisa valor existente)
      - JSONB_FIELDS (fotos_urls, conflictos): solo si estaba vacío
      - TEXT_ARRAY_FIELDS (idiomas_hablados, productos_venta): unión dedup
      - DEEP_MERGE_JSONB_FIELDS (servicios_extras): merge recursivo en Python
        preservando keys ya pobladas (lee → merge → escribe en la misma TX)
    """
    sets = []
    vals = []
    i = 1
    deep_merge_pending: dict[str, dict] = {}

    for k, v in datos.items():
        if k in SKIP_ENRIQUECER or v is None:
            continue
        col = k
        if col == "web":
            v = _limpiar_web(v)
            if v is None:
                continue
        if k == "rating_promedio":
            col = "master_rating"
        elif k == "num_reviews":
            col = "total_reviews"
        elif k == "nombre":
            col = "canonical_name"

        if col in DEEP_MERGE_JSONB_FIELDS:
            if isinstance(v, dict) and v:
                deep_merge_pending[col] = v
            continue

        if col in TEXT_ARRAY_FIELDS:
            if not isinstance(v, list) or not v:
                continue
            arr = [str(x) for x in v if x is not None]
            if not arr:
                continue
            sets.append(
                f"{col} = ARRAY(SELECT DISTINCT unnest("
                f"COALESCE({col}, ARRAY[]::text[]) || ${i}::text[]"
                f"))"
            )
            vals.append(arr)
            i += 1
            continue

        if k in JSONB_FIELDS or isinstance(v, (list, dict)):
            v = json.dumps(v) if not isinstance(v, str) else v
            sets.append(f"{col} = CASE WHEN {col} = '[]'::jsonb OR {col} IS NULL THEN ${i}::jsonb ELSE {col} END")
        elif col == "tipo":
            sets.append(f"tipo = CASE WHEN tipo = 'otro' OR tipo IS NULL THEN ${i}::text ELSE tipo END")
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

    # Deep-merge JSONB: read-merge-write en la misma transacción.
    for col, new_val in deep_merge_pending.items():
        row = await conn.fetchrow(
            f"SELECT {col} AS cur FROM spots WHERE id = $1", spot_id
        )
        current = row["cur"] if row else None
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except (json.JSONDecodeError, TypeError):
                current = {}
        merged = _deep_merge_jsonb(current if isinstance(current, dict) else None, new_val)
        if merged != current:
            await conn.execute(
                f"UPDATE spots SET {col} = $1::jsonb, updated_at = NOW() WHERE id = $2",
                json.dumps(merged), spot_id,
            )


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
            normalized_data = COALESCE(source_records.normalized_data, '{}'::jsonb) || $5::jsonb,
            rating = $9,
            review_count = COALESCE($10, source_records.review_count),
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

async def refresh_review_count(conn: asyncpg.Connection, source: str, spot_id: int) -> int:
    """Sincroniza source_records.review_count con el COUNT real en reviews.

    Las fuentes con `num_reviews` en normalize() (park4night, campercontact, etc.)
    obtienen review_count del API y representa "esperado total". Las fuentes que
    no tienen ese campo (caramaps/womostell/vansite/campspace/wtmg/furgovw) tenían
    review_count NULL — bug histórico: el job incremental "no pendientes" siempre
    decía 0 y nunca se reintentaba.

    Esta función usa GREATEST para nunca decrementar — si la API ya dio un
    expected mayor, lo respetamos; si solo hay descargas reales, las usamos.

    Llamar tras upsertar reviews de un spot en download_reviews / run-with-reviews.
    """
    cnt = await conn.fetchval(
        "SELECT COUNT(*) FROM reviews WHERE source = $1 AND spot_id = $2",
        source, spot_id,
    )
    await conn.execute(
        """
        UPDATE source_records
        SET review_count = GREATEST(COALESCE(review_count, 0), $1::int)
        WHERE source = $2 AND spot_id = $3
        """,
        cnt, source, spot_id,
    )
    return int(cnt or 0)


async def upsert_review(conn: asyncpg.Connection, review: dict) -> None:
    status = await conn.execute("""
        INSERT INTO reviews (
            spot_id, source, source_review_id, texto, texto_original,
            rating, autor, fecha, idioma
        )
        VALUES ($1, $2, $3, $4, COALESCE($5, $4), $6, $7, $8, $9)
        ON CONFLICT (source, source_review_id) DO UPDATE SET
            texto = COALESCE(reviews.texto, EXCLUDED.texto),
            texto_original = COALESCE(reviews.texto_original, EXCLUDED.texto_original),
            rating = COALESCE(EXCLUDED.rating, reviews.rating),
            autor = COALESCE(reviews.autor, EXCLUDED.autor),
            fecha = COALESCE(reviews.fecha, EXCLUDED.fecha),
            idioma = COALESCE(reviews.idioma, EXCLUDED.idioma)
    """,
        review["spot_id"], review["source"], review.get("source_review_id"),
        review.get("texto"), review.get("texto_original"), review.get("rating"),
        review.get("autor"), review.get("fecha"), review.get("idioma")
    )
    return status == "INSERT 0 1"


async def insert_claim(conn: asyncpg.Connection, review: dict, claim: dict,
                       pipeline_run_id: str | None = None) -> int:
    return await conn.fetchval("""
        INSERT INTO extracted_claims (
            review_id, spot_id, signal_type, raw_value, extraction_confidence,
            extractor_name, extractor_version, pipeline_run_id, excerpt
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
    """,
        review["id"], review["spot_id"],
        claim.get("signal") or claim.get("signal_type"),
        str(claim.get("value", claim.get("raw_value"))),
        float(claim.get("confidence", claim.get("extraction_confidence", 1.0))),
        claim.get("extractor_name", "regex_v1"),
        claim.get("extractor_version", "phase3-2026-05-23"),
        pipeline_run_id,
        claim.get("excerpt")
    )


async def insert_observation(conn: asyncpg.Connection, claim_id: int,
                             review: dict, observation) -> int:
    return await conn.fetchval("""
        INSERT INTO normalized_observations (
            claim_id, spot_id, signal_type, value_num, value_bool, value_text,
            extraction_confidence, source_confidence, reviewer_confidence,
            observation_weight, observed_at, date_estimated
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id
    """,
        claim_id, review["spot_id"], observation.signal_type,
        observation.value_num, observation.value_bool, observation.value_text,
        observation.extraction_confidence, observation.source_confidence,
        observation.reviewer_confidence, observation.observation_weight,
        observation.observed_at, getattr(observation, "date_estimated", False)
    )


async def upsert_semantic_state(conn: asyncpg.Connection, spot_id: int,
                                state: dict) -> None:
    await conn.execute("""
        INSERT INTO spot_semantic_state (
            spot_id, quietness_score, safety_score, police_risk_score,
            beauty_score, crowd_level_score, overnight_safe, stealth_score,
            signals_data, semantic_dsl, total_observations,
            consensus_confidence, weight_support, last_snapshot_data,
            stale, updated_at, last_aggregated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11,
            $12, $13, $9::jsonb, FALSE, NOW(), NOW()
        )
        ON CONFLICT (spot_id) DO UPDATE SET
            quietness_score = EXCLUDED.quietness_score,
            safety_score = EXCLUDED.safety_score,
            police_risk_score = EXCLUDED.police_risk_score,
            beauty_score = EXCLUDED.beauty_score,
            crowd_level_score = EXCLUDED.crowd_level_score,
            overnight_safe = EXCLUDED.overnight_safe,
            stealth_score = EXCLUDED.stealth_score,
            signals_data = EXCLUDED.signals_data,
            semantic_dsl = EXCLUDED.semantic_dsl,
            total_observations = EXCLUDED.total_observations,
            consensus_confidence = EXCLUDED.consensus_confidence,
            weight_support = EXCLUDED.weight_support,
            last_snapshot_data = EXCLUDED.last_snapshot_data,
            stale = FALSE,
            updated_at = NOW(),
            last_aggregated_at = NOW()
    """,
        spot_id, state.get("quietness_score"), state.get("safety_score"),
        state.get("police_risk_score"), state.get("beauty_score"),
        state.get("crowd_level_score"), state.get("overnight_safe"),
        state.get("stealth_score"), json.dumps(state.get("signals_data", {})),
        state.get("semantic_dsl"), state.get("total_observations", 0),
        state.get("consensus_confidence", 0.0), state.get("weight_support", 0.0)
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
