"""Motor de contexto geoespacial OSM (Sprint 3) — piloto Overpass.

Para cada spot (piloto: país ES) consulta Overpass alrededor del punto y calcula
la distancia al amenity más cercano por categoría, poblando spot_geo. Esto es lo
que convierte un spot en un "spot contextualizado" sin depender de usuarios,
reseñas, LLM ni Google:

    "Área gratuita. Agua a 180 m. Súper a 700 m. Mirador a 1.4 km."

Diseño deliberado de PILOTO:
  - Overpass público es frágil y rate-limited → 1 query/spot, batch pequeño,
    delay generoso, backoff y abort tras N errores seguidos. NO escala a 250k;
    si valida, Sprint 4 migra a PBF→PostGIS local.
  - Idempotente: solo procesa spots sin fila spot_geo de OSM (o stale por TTL).

Config por env:
  GEO_OSM_COUNTRY    país piloto (default 'es')
  GEO_OSM_BATCH      spots por run (default 300)
  GEO_OSM_RADIUS_M   radio de búsqueda (default 3000)
  GEO_OSM_RATE       delay entre queries en s (default 1.5)
  GEO_OSM_REFRESH_DAYS  TTL antes de recomputar (default 180)
  OVERPASS_URL       endpoint (default api pública)
"""

import asyncio
import json
import math
import os
from datetime import datetime, timezone

import httpx
from loguru import logger

DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"

# Categoría → (clave OSM, valor OSM). Se guarda en nearby_osm JSONB {categoria: km}.
# Añadir una categoría nueva aquí = aparece en el import y en el contexto sin más
# cambios de schema (re-import del PBF + re-run geo).
CATEGORIES = [
    ("drinking_water", "amenity", "drinking_water"),
    ("dump_station",   "amenity", "sanitary_dump_station"),
    ("supermarket",    "shop",    "supermarket"),
    ("fuel",           "amenity", "fuel"),
    ("pharmacy",       "amenity", "pharmacy"),
    ("viewpoint",      "tourism", "viewpoint"),
    ("bakery",         "shop",    "bakery"),
    ("laundry",        "shop",    "laundry"),
    ("restaurant",     "amenity", "restaurant"),
    ("ev_charging",    "amenity", "charging_station"),
    ("beach",          "natural", "beach"),
]

# Etiquetas legibles (ES) para UI / prompt LLM.
CATEGORY_LABELS = {
    "drinking_water": "agua", "dump_station": "vaciado", "supermarket": "super",
    "fuel": "gasolinera", "pharmacy": "farmacia", "viewpoint": "mirador",
    "bakery": "panaderia", "laundry": "lavanderia", "restaurant": "restaurante",
    "ev_charging": "recarga EV", "beach": "playa",
}

# 1b — proximidad a NUESTROS spots por tipo/servicio. {etiqueta: condición SQL}.
# Condiciones fijas (sin inyección). Complementa a OSM: area_ac/camping no son
# amenities OSM, y "spot con vaciado" usa la verdad reconciliada de fuentes camper.
NEARBY_SPOT_CONDS = {
    "area_ac": "tipo = 'area_ac'",
    "camping": "tipo = 'camping'",
    "spot_vaciado": "vaciado_negras = TRUE",
}


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _build_query(lat: float, lon: float, radius_m: int) -> str:
    parts = "".join(
        f"  nwr(around:{radius_m},{lat},{lon})[{key}={val}];\n"
        for _, key, val in CATEGORIES
    )
    return f"[out:json][timeout:25];\n(\n{parts});\nout center tags;"


def _categorize(tags: dict) -> str | None:
    for name, key, val in CATEGORIES:
        if tags.get(key) == val:
            return name
    return None


def _nearest_by_category(elements: list, lat: float, lon: float) -> dict:
    """Distancia (km) al elemento más cercano por categoría."""
    best: dict[str, float] = {}
    for el in elements:
        tags = el.get("tags") or {}
        cat = _categorize(tags)
        if not cat:
            continue
        if "lat" in el and "lon" in el:
            e_lat, e_lon = el["lat"], el["lon"]
        elif "center" in el:
            e_lat, e_lon = el["center"].get("lat"), el["center"].get("lon")
        else:
            continue
        if e_lat is None or e_lon is None:
            continue
        d = _haversine_km(lat, lon, e_lat, e_lon)
        if cat not in best or d < best[cat]:
            best[cat] = d
    return best


async def _fetch_overpass(client, url: str, query: str) -> list:
    r = await client.post(url, data={"data": query}, timeout=40)
    r.raise_for_status()
    return (r.json() or {}).get("elements", [])


async def _upsert_geo(conn, spot_id, nearby_osm: dict, nearby_spots: dict, source: str):
    """Upsert de la proximidad (JSONB) en spot_geo. Preserva columnas DEM."""
    await conn.execute(
        """
        INSERT INTO spot_geo (spot_id, nearby_osm, nearby_spots, source, processed_at)
        VALUES ($1, $2::jsonb, $3::jsonb, $4, NOW())
        ON CONFLICT (spot_id) DO UPDATE SET
          nearby_osm   = EXCLUDED.nearby_osm,
          nearby_spots = EXCLUDED.nearby_spots,
          source       = EXCLUDED.source,
          processed_at = NOW()
        """,
        spot_id, json.dumps(nearby_osm), json.dumps(nearby_spots), source,
    )


_LOCAL_NEAREST_SQL = """
    SELECT category,
           MIN(ST_Distance(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography))
             / 1000.0 AS km
    FROM osm_pois
    WHERE country = $4
      AND ST_DWithin(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography, $3)
    GROUP BY category
"""


async def _nearest_spots(conn, spot_id, lat, lon, radius_m: int = 30000) -> dict:
    """1b — distancia (km) al NUESTRO spot más cercano por tipo/servicio."""
    out: dict[str, float] = {}
    for label, cond in NEARBY_SPOT_CONDS.items():
        km = await conn.fetchval(
            f"""
            SELECT ST_Distance(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography)
                     / 1000.0
            FROM spots
            WHERE activo = TRUE AND {cond} AND id <> $3
              AND ST_DWithin(geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography, $4)
            ORDER BY geog <-> ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
            LIMIT 1
            """,
            lat, lon, spot_id, radius_m,
        )
        if km is not None:
            out[label] = round(km, 3)
    return out


async def _run_local(pool, candidatos, radius_m, country, stats, job_id):
    """Modo LOCAL: KNN sobre osm_pois (1a) + spots cercanos (1b). Sin internet."""
    async with pool.acquire() as conn:
        for idx, spot in enumerate(candidatos):
            try:
                lat, lon = float(spot["lat"]), float(spot["lon"])
                rows = await conn.fetch(_LOCAL_NEAREST_SQL, lat, lon, radius_m, country)
                nearby_osm = {r["category"]: round(r["km"], 3) for r in rows}
                nearby_spots = await _nearest_spots(conn, spot["id"], lat, lon)
                await _upsert_geo(conn, spot["id"], nearby_osm, nearby_spots, "osm_pbf")
                stats["procesados"] += 1
                if nearby_osm or nearby_spots:
                    stats["con_contexto"] += 1
            except Exception as e:
                stats["errores"] += 1
                logger.warning(f"[geo_osm] local error spot {spot['id']}: {e!r}")
            if (idx + 1) % 2000 == 0:
                logger.info(
                    f"[geo_osm] local {idx+1}/{len(candidatos)} | "
                    f"con_contexto={stats['con_contexto']}"
                )


async def _run_overpass(pool, candidatos, radius_m, rate, overpass_url, stats, job_id):
    """Modo OVERPASS: una query por spot (1a) + spots cercanos (1b). Fallback."""
    consecutive_errors = 0
    async with httpx.AsyncClient(headers={"User-Agent": "GeoSpots/1.0 (+geo_context)"}) as client:
        for idx, spot in enumerate(candidatos):
            spot_id = spot["id"]
            lat, lon = float(spot["lat"]), float(spot["lon"])
            try:
                await asyncio.sleep(rate)
                elements = await _fetch_overpass(client, overpass_url, _build_query(lat, lon, radius_m))
                consecutive_errors = 0
                nearby_osm = {k: round(v, 3) for k, v in
                              _nearest_by_category(elements, lat, lon).items()}
                async with pool.acquire() as conn:
                    nearby_spots = await _nearest_spots(conn, spot_id, lat, lon)
                    await _upsert_geo(conn, spot_id, nearby_osm, nearby_spots, "osm_overpass")
                stats["procesados"] += 1
                if nearby_osm or nearby_spots:
                    stats["con_contexto"] += 1
            except httpx.HTTPStatusError as e:
                stats["errores"] += 1
                consecutive_errors += 1
                logger.warning(f"[geo_osm] HTTP {e.response.status_code} spot {spot_id}")
                if e.response.status_code == 429:
                    await asyncio.sleep(min(60, 5 * consecutive_errors))
            except Exception as e:
                stats["errores"] += 1
                consecutive_errors += 1
                logger.warning(f"[geo_osm] error spot {spot_id}: {e!r}")
            if consecutive_errors >= 10:
                logger.error("[geo_osm] 10 errores seguidos — abortando (Overpass caído/ban).")
                break
            if job_id and (idx + 1) % 25 == 0:
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE scraper_jobs SET progress = $1::jsonb WHERE id = $2",
                            json.dumps({"processed": idx + 1, "total": len(candidatos),
                                        "stats": stats}, default=str), job_id,
                        )
                except Exception:
                    pass


async def run_geo_osm(pool, job_id: int = None) -> dict:
    """Pipeline de contexto OSM. Modo LOCAL (osm_pois, preferido) o OVERPASS
    (fallback/piloto). GEO_OSM_MODE = auto|local|overpass (default auto)."""
    country = os.environ.get("GEO_OSM_COUNTRY", "es").lower()
    batch = int(os.environ.get("GEO_OSM_BATCH", "300"))
    radius_m = int(os.environ.get("GEO_OSM_RADIUS_M", "3000"))
    rate = float(os.environ.get("GEO_OSM_RATE", "1.5"))
    refresh_days = int(os.environ.get("GEO_OSM_REFRESH_DAYS", "180"))
    overpass_url = os.environ.get("OVERPASS_URL", DEFAULT_OVERPASS)

    mode = os.environ.get("GEO_OSM_MODE", "auto").lower()
    inicio = datetime.now(timezone.utc)
    stats = {"procesados": 0, "con_contexto": 0, "errores": 0,
             "pais": country, "modo": None}

    # Determinar modo: LOCAL si osm_pois tiene datos del país (o forzado).
    has_local = False
    async with pool.acquire() as conn:
        if mode in ("auto", "local"):
            reg = await conn.fetchval("SELECT to_regclass('osm_pois')")
            if reg:
                has_local = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM osm_pois WHERE country = $1)", country
                )
    use_local = mode == "local" or (mode == "auto" and has_local)
    stats["modo"] = "local" if use_local else "overpass"

    # En local no hay rate limit → procesa todo el país de una pasada.
    limit = int(os.environ.get("GEO_OSM_LOCAL_BATCH", "100000")) if use_local else batch

    async with pool.acquire() as conn:
        candidatos = await conn.fetch(
            """
            SELECT s.id, s.lat, s.lon
            FROM spots s
            LEFT JOIN spot_geo g ON g.spot_id = s.id AND g.source LIKE 'osm%'
            WHERE s.activo = TRUE
              AND s.country_iso = $1
              AND s.lat IS NOT NULL AND s.lon IS NOT NULL
              AND (g.spot_id IS NULL
                   OR g.processed_at < NOW() - ($2 || ' days')::interval)
            ORDER BY COALESCE(s.total_reviews, 0) DESC, s.id
            LIMIT $3
            """,
            country, str(refresh_days), limit,
        )

    if not candidatos:
        logger.info(f"[geo_osm] No hay candidatos (pais={country}, modo={stats['modo']}).")
        return stats

    logger.info(
        f"[geo_osm] modo={stats['modo']} | {len(candidatos)} spots "
        f"(pais={country}, radio={radius_m}m)"
    )

    if use_local:
        await _run_local(pool, candidatos, radius_m, country, stats, job_id)
    else:
        await _run_overpass(pool, candidatos, radius_m, rate, overpass_url, stats, job_id)

    dur = (datetime.now(timezone.utc) - inicio).total_seconds()
    logger.info(f"[geo_osm] Completado en {dur:.0f}s | {stats}")
    return stats
