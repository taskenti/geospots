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
import math
import os
from datetime import datetime, timezone

import httpx
from loguru import logger

DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"

# Categoría → (clave OSM, valor OSM, columna spot_geo en km)
CATEGORIES = [
    ("drinking_water", "amenity", "drinking_water",        "dist_drinking_water_km"),
    ("dump_station",   "amenity", "sanitary_dump_station", "dist_dump_station_km"),
    ("supermarket",    "shop",    "supermarket",           "dist_supermarket_km"),
    ("fuel",           "amenity", "fuel",                  "dist_fuel_km"),
    ("pharmacy",       "amenity", "pharmacy",              "dist_pharmacy_km"),
    ("viewpoint",      "tourism", "viewpoint",             "dist_viewpoint_km"),
]
_COLS = [c[3] for c in CATEGORIES]


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
        for _, key, val, _ in CATEGORIES
    )
    return f"[out:json][timeout:25];\n(\n{parts});\nout center tags;"


def _categorize(tags: dict) -> str | None:
    for name, key, val, _ in CATEGORIES:
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


async def run_geo_osm(pool, job_id: int = None) -> dict:
    """Pipeline piloto: spots del país → Overpass → spot_geo."""
    country = os.environ.get("GEO_OSM_COUNTRY", "es").lower()
    batch = int(os.environ.get("GEO_OSM_BATCH", "300"))
    radius_m = int(os.environ.get("GEO_OSM_RADIUS_M", "3000"))
    rate = float(os.environ.get("GEO_OSM_RATE", "1.5"))
    refresh_days = int(os.environ.get("GEO_OSM_REFRESH_DAYS", "180"))
    overpass_url = os.environ.get("OVERPASS_URL", DEFAULT_OVERPASS)

    inicio = datetime.now(timezone.utc)
    stats = {"procesados": 0, "con_contexto": 0, "errores": 0,
             "pais": country, "batch": batch}

    async with pool.acquire() as conn:
        candidatos = await conn.fetch(
            """
            SELECT s.id, s.lat, s.lon
            FROM spots s
            LEFT JOIN spot_geo g ON g.spot_id = s.id AND g.source = 'osm_overpass'
            WHERE s.activo = TRUE
              AND s.country_iso = $1
              AND s.lat IS NOT NULL AND s.lon IS NOT NULL
              AND (g.spot_id IS NULL
                   OR g.processed_at < NOW() - ($2 || ' days')::interval)
            ORDER BY COALESCE(s.total_reviews, 0) DESC, s.id
            LIMIT $3
            """,
            country, str(refresh_days), batch,
        )

    if not candidatos:
        logger.info(f"[geo_osm] No hay candidatos (pais={country}).")
        return stats

    logger.info(
        f"[geo_osm] {len(candidatos)} spots (pais={country}, radio={radius_m}m, "
        f"rate={rate}s)"
    )

    consecutive_errors = 0
    async with httpx.AsyncClient(headers={"User-Agent": "GeoSpots/1.0 (+geo_context)"}) as client:
        for idx, spot in enumerate(candidatos):
            spot_id = spot["id"]
            lat, lon = float(spot["lat"]), float(spot["lon"])
            try:
                await asyncio.sleep(rate)
                elements = await _fetch_overpass(client, overpass_url, _build_query(lat, lon, radius_m))
                consecutive_errors = 0

                nearest = _nearest_by_category(elements, lat, lon)
                vals = {col: None for col in _COLS}
                for name, _k, _v, col in CATEGORIES:
                    if name in nearest:
                        vals[col] = round(nearest[name], 3)

                async with pool.acquire() as conn:
                    await conn.execute(
                        f"""
                        INSERT INTO spot_geo
                          (spot_id, {", ".join(_COLS)}, source, processed_at)
                        VALUES ($1, {", ".join(f"${i+2}" for i in range(len(_COLS)))},
                                'osm_overpass', NOW())
                        ON CONFLICT (spot_id) DO UPDATE SET
                          {", ".join(f"{c} = EXCLUDED.{c}" for c in _COLS)},
                          source = 'osm_overpass',
                          processed_at = NOW()
                        """,
                        spot_id, *[vals[c] for c in _COLS],
                    )
                stats["procesados"] += 1
                if nearest:
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
                # repr() para ver el tipo real (ReadTimeout/ConnectError suelen
                # tener str vacío). Overpass público es la causa habitual.
                logger.warning(f"[geo_osm] error spot {spot_id}: {e!r}")

            if consecutive_errors >= 10:
                logger.error("[geo_osm] 10 errores seguidos — abortando (Overpass caído/ban).")
                break

            if job_id and (idx + 1) % 25 == 0:
                import json as _json
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE scraper_jobs SET progress = $1::jsonb WHERE id = $2",
                            _json.dumps({"processed": idx + 1, "total": len(candidatos),
                                         "stats": stats}, default=str), job_id,
                        )
                except Exception:
                    pass

    dur = (datetime.now(timezone.utc) - inicio).total_seconds()
    logger.info(f"[geo_osm] Completado en {dur:.0f}s | {stats}")
    return stats
