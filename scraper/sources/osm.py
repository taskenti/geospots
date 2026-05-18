"""OpenStreetMap — scraper vía Overpass API."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import httpx

from sources.base import AbstractSource

OVERPASS_URL = "https://overpass.kumi.systems/api/interpreter"

OVERPASS_QUERY = """
[out:json][timeout:60];
(
  node["tourism"="caravan_site"](around:{radio},{lat},{lon});
  way["tourism"="caravan_site"](around:{radio},{lat},{lon});
  node["amenity"="sanitary_dump_station"](around:{radio},{lat},{lon});
  node["amenity"="water_point"]["drinking_water"="yes"](around:{radio},{lat},{lon});
  node["amenity"="parking"]["motorhome"="yes"](around:{radio},{lat},{lon});
  way["amenity"="parking"]["motorhome"="yes"](around:{radio},{lat},{lon});
  node["leisure"="camping_site"](around:{radio},{lat},{lon});
  way["leisure"="camping_site"](around:{radio},{lat},{lon});
);
out center body qt;
"""

OSM_TIPO_MAP = {
    ("tourism", "caravan_site"): "area_ac",
    ("amenity", "sanitary_dump_station"): "vaciado",
    ("amenity", "water_point"): "otro",
    ("amenity", "parking"): "parking",
    ("leisure", "camping_site"): "camping",
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=4, min=60, max=120),
    retry=(retry_if_exception_type(httpx.TimeoutException) | retry_if_exception_type(httpx.HTTPError)),
    reraise=True
)
async def _fetch_overpass(client, query):
    resp = await client.post(OVERPASS_URL, data={"data": query}, timeout=60)
    if resp.status_code in (429, 504):
        logger.warning(f"Overpass {resp.status_code}. Esperando 60s...")
        await asyncio.sleep(60)
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()


class OSMSource(AbstractSource):
    """OpenStreetMap: Overpass API con grid de puntos."""

    name = "osm"
    rate_limit = 3.0
    grid_step = 0.35
    dedup_radius_m = 50.0

    EU_BOUNDS = {
        "lat_min": 35.0, "lat_max": 71.5,
        "lon_min": -11.0, "lon_max": 30.0,
    }

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("OSM usa puntos con radio")

    def normalize(self, raw: dict) -> dict | None:
        try:
            if "tags" not in raw:
                return None
            tags = raw["tags"]
            osm_id = int(raw["id"])

            tipo = None
            for (k, v), mapeo in OSM_TIPO_MAP.items():
                if tags.get(k) == v:
                    tipo = mapeo
                    break
            if not tipo:
                return None

            nombre = tags.get("name:es") or tags.get("name") or f"OSM {tipo} {osm_id}"

            lat, lon = None, None
            if raw["type"] == "node":
                lat, lon = float(raw["lat"]), float(raw["lon"])
            elif raw["type"] in ("way", "relation") and "center" in raw:
                lat = float(raw["center"]["lat"])
                lon = float(raw["center"]["lon"])
            if lat is None or lon is None:
                return None

            fee = tags.get("fee")
            gratuito = None
            if fee:
                gratuito = True if fee.lower() == "no" else (False if fee.lower() == "yes" else None)

            hauteur = tags.get("maxheight")
            altura = None
            if hauteur:
                try:
                    altura = float(hauteur.replace(",", "."))
                except Exception:
                    pass

            cap = tags.get("capacity")
            plazas = int(cap) if cap and cap.isdigit() else None

            dog = tags.get("dog")
            perros = True if dog and dog.lower() in ("yes", "leashed") else (
                False if dog and dog.lower() == "no" else None)

            wifi_tag = tags.get("internet_access")
            wifi = True if wifi_tag and wifi_tag.lower() in ("yes", "wlan") else (
                False if wifi_tag and wifi_tag.lower() == "no" else None)

            return {
                "source_id": str(osm_id),
                "nombre": nombre, "lat": lat, "lon": lon, "tipo": tipo,
                "descripcion_en": tags.get("description"),
                "temporada_apertura": tags.get("opening_hours"),
                "gratuito": gratuito, "altura_max_m": altura,
                "num_plazas": plazas,
                "telefono": tags.get("contact:phone") or tags.get("phone"),
                "email": tags.get("contact:email") or tags.get("email"),
                "web": tags.get("contact:website") or tags.get("website"),
                "region": tags.get("addr:city") or tags.get("addr:town"),
                "country_iso": tags.get("addr:country", "").lower() or None,
                "agua_potable": True if tags.get("drinking_water") == "yes" else None,
                "vaciado_negras": True if tags.get("sanitary_dump_station") == "yes" else None,
                "ducha": True if tags.get("shower") == "yes" else None,
                "electricidad": True if tags.get("electricity") == "yes" else None,
                "wifi": wifi, "perros": perros,
            }
        except Exception as e:
            logger.error(f"Error normalizando OSM {raw.get('id')}: {e}")
            return None

    def _generate_points(self):
        import random
        puntos = []
        lat = self.EU_BOUNDS["lat_min"]
        while lat <= self.EU_BOUNDS["lat_max"]:
            lon = self.EU_BOUNDS["lon_min"]
            while lon <= self.EU_BOUNDS["lon_max"]:
                puntos.append((round(lat, 4), round(lon, 4)))
                lon += self.grid_step
            lat += self.grid_step
        random.shuffle(puntos)
        return puntos

    async def run(self, pool, config, log_id: int) -> dict:
        from db import (find_spot_cercano, crear_spot, enriquecer_spot,
                        upsert_source_record, finish_scraper_log, update_fuente_config)
        inicio = datetime.now(timezone.utc)
        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
                 "errores": 0, "iniciado_en": inicio, "detalle": {}}

        puntos = self._generate_points()
        logger.info(f"[osm] {len(puntos)} puntos GPS")
        seen_ids: set[str] = set()
        sem = asyncio.Semaphore(max(1, config.max_workers // 2))
        circuit_breaker = 0

        headers = {
            "User-Agent": "GeoSpots Scraper/1.0 (admin@geospots.local)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"
        }
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            async def procesar(lat, lon):
                nonlocal circuit_breaker
                if circuit_breaker >= 5:
                    return
                async with sem:
                    await asyncio.sleep(self.rate_limit)
                    try:
                        q = OVERPASS_QUERY.format(radio=60000, lat=lat, lon=lon)
                        data = await _fetch_overpass(client, q)
                        circuit_breaker = 0
                    except Exception as e:
                        circuit_breaker += 1
                        logger.error(f"[osm] Error {lat},{lon}: {e}")
                        stats["errores"] += 1
                        return

                    for elem in data.get("elements", []):
                        norm = self.normalize(elem)
                        if not norm:
                            continue
                        sid = norm["source_id"]
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                        try:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    ex = await find_spot_cercano(conn, norm["lat"], norm["lon"], self.dedup_radius_m)
                                    if ex:
                                        await enriquecer_spot(conn, ex["id"], norm, self.name)
                                        stats["actualizados"] += 1
                                        spot_id = ex["id"]
                                    else:
                                        norm["fuentes"] = [self.name]
                                        spot_id = await crear_spot(conn, norm)
                                        stats["nuevos"] += 1
                                    await upsert_source_record(conn, spot_id, self.name, sid, elem, norm)
                        except Exception as e:
                            logger.error(f"[osm] DB {sid}: {e}")
                            stats["errores"] += 1

            LOTE = 20
            for i in range(0, len(puntos), LOTE):
                if circuit_breaker >= 5:
                    logger.error("[osm] Circuit breaker activado. Abortando.")
                    break
                batch = puntos[i:i+LOTE]
                await asyncio.gather(*[procesar(lat, lon) for lat, lon in batch])
                logger.info(f"[osm] {min(i+LOTE, len(puntos))}/{len(puntos)} | "
                            f"uniq={len(seen_ids)} new={stats['nuevos']} err={stats['errores']}")

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)
        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[osm] Completado en {dur:.0f}s | {stats}")
        return stats
