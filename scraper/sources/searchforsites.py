"""SearchForSites — scraper desde API oculta getDataAdvanced."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

BASE_URL = "https://www.searchforsites.co.uk/pdo/getDataAdvanced.php"

HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": "https://www.searchforsites.co.uk",
    "referer": "https://www.searchforsites.co.uk/advanced.php",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-requested-with": "XMLHttpRequest"
}

# Países europeos para iterar y no pasarnos del límite de rows por petición
COUNTRIES = [
    "GB", "FR", "ES", "PT", "IT", "DE", "NL", "BE", "AT", "CH", 
    "NO", "SE", "FI", "DK", "IE", "GR", "PL", "CZ", "HR", "SI", 
    "RO", "HU", "SK", "LU", "AD", "MC", "LI", "AL", "BA", "RS", 
    "ME", "MK", "BG", "LT", "LV", "EE", "TR", "MA"
]

# Tipos de lugares en SFS (del 1 al 15 cubrimos todo: parkings, campings, aires...)
LOCATION_TYPES = list(range(1, 16))

class SearchForSitesSource(AbstractSource):
    name = "searchforsites"
    rate_limit = 1.0
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("SearchForSites usa iteración por país/tipo")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["latlng"]["lat"])
            lon = float(raw["latlng"]["lng"])
        except (KeyError, TypeError, ValueError):
            return None

        sfs_type = raw.get("Type", "")
        tipo = "otro"
        
        # Clasificación
        if sfs_type in ("AC", "ACF"):
            tipo = "camping"
        elif sfs_type in ("ASN", "CPA", "CS", "CL"):
            tipo = "area_ac"
        elif sfs_type == "PN":
            tipo = "parking"

        # Fotos
        fotos = []
        thumb = raw.get("thumb")
        if thumb:
            fotos.append(f"https://www.searchforsites.co.uk/uploads/thumbs/{thumb}")

        # Coste
        cost = raw.get("cost", {})
        min_c = cost.get("min", 0)
        max_c = cost.get("max", 0)
        gratuito = (min_c == 0 and max_c == 0) if "min" in cost else None

        return {
            "source_id": str(raw.get("ID")),
            "nombre": raw.get("Name", "Sin nombre").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "country_iso": raw.get("cID", "")[:2],
            "region": raw.get("address", "").split(",")[0],
            "master_rating": raw.get("rT"),
            "fotos_urls": fotos,
            "web": f"https://www.searchforsites.co.uk/marker.php?id={raw.get('ID')}",
            "raw_facilities": raw.get("facilities", ""),  # Guardamos raw para mapear luego
        }

    async def run(self, pool, config, log_id: int) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config,
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {},
        }

        async with httpx.AsyncClient(headers=HEADERS) as client:
            for country in COUNTRIES:
                for loc_type in LOCATION_TYPES:
                    payload = {
                        "browse": "true",
                        "country": country,
                        "locations": str(loc_type)
                    }

                    try:
                        resp = await client.post(BASE_URL, data=payload, timeout=30)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        logger.error(f"[SFS] Error obteniendo {country} tipo {loc_type}: {e}")
                        stats["errores"] += 1
                        await asyncio.sleep(2)
                        continue

                    items = data.get("results", {})
                    if not items:
                        await asyncio.sleep(self.rate_limit)
                        continue

                    logger.info(f"[SFS] {country} tipo {loc_type}: {len(items)} spots (Total ref: {data.get('total')})")

                    for key, raw in items.items():
                        norm = self.normalize(raw)
                        if not norm:
                            continue

                        sid = norm["source_id"]
                        norm_db = norm.copy()
                        norm_db.pop("raw_facilities", None)
                        try:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    existente = await find_spot_cercano(
                                        conn, norm["lat"], norm["lon"], self.dedup_radius_m
                                    )
                                    if existente:
                                        spot_id = existente["id"]
                                        await enriquecer_spot(conn, spot_id, norm_db, self.name)
                                        stats["actualizados"] += 1
                                    else:
                                        norm_db["fuentes"] = [self.name]
                                        spot_id = await crear_spot(conn, norm_db)
                                        stats["nuevos"] += 1

                                    await upsert_source_record(
                                        conn, spot_id, self.name, sid, raw, norm
                                    )
                        except Exception as e:
                            logger.error(f"[SFS] Error spot '{norm.get('nombre')}': {e}")
                            stats["errores"] += 1

                    await asyncio.sleep(self.rate_limit)

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[SFS] Completado en {dur:.0f}s | {stats}")
        return stats
