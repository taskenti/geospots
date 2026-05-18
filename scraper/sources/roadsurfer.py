"""Roadsurfer Spots — scraper con búsqueda radial global."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx
import re

from sources.base import AbstractSource

BASE_URL = "https://spots.roadsurfer.com/en_GB/search/spot"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://spots.roadsurfer.com",
    "referer": "https://spots.roadsurfer.com/en-gb/roadsurfer-spots",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

class RoadsurferSource(AbstractSource):
    name = "roadsurfer"
    rate_limit = 1.0
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Roadsurfer usa búsqueda radial global")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["location"]["lat"])
            lon = float(raw["location"]["lng"])
        except (KeyError, TypeError, ValueError):
            return None

        # Tipos (terrainFor)
        terrains = raw.get("terrainFor", [])
        tipo = "otro"
        if "camperVan" in terrains or "motorhome" in terrains or "caravan" in terrains:
            tipo = "area_ac"
        elif "tent" in terrains:
            tipo = "naturaleza"

        # Foto extraída del HTML del "previewImageHtml"
        fotos = []
        html = raw.get("previewImageHtml")
        if html:
            match = re.search(r'<img[^>]+src="([^"]+)"', html)
            if match:
                url = match.group(1)
                if url.startswith("/"):
                    url = f"https://spots.roadsurfer.com{url}"
                fotos.append(url)

        return {
            "source_id": str(raw.get("id")),
            "nombre": raw.get("name", "Roadsurfer Spot").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": raw.get("isFreeSpot", False),
            "web": raw.get("url", ""),
            "fotos_urls": fotos
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
            offset = 0
            size = 500
            seen_ids = set()
            
            while True:
                # Búsqueda circular gigante (20,000 km) desde el centro de Europa
                payload = {
                    "offset": offset,
                    "size": size,
                    "geoLocation": {
                        "lat": 50.0,
                        "lng": 10.0,
                        "type": None,
                        "name": None
                    },
                    "searchRadius": 20000,
                    "sort": "distance",
                    "allowWithoutLocation": False,
                    "terrainFor": [],
                    "activities": [],
                    "facilities": [],
                    "placeSituations": [],
                    "categories": None,
                    "country": None,
                    "startDate": None,
                    "endDate": None,
                    "maxPrice": None,
                    "onlyFreeSpots": False,
                    "searchType": "default"
                }

                try:
                    logger.info(f"[ROADSURFER] Buscando con offset {offset}...")
                    resp = await client.post(BASE_URL, json=payload, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[ROADSURFER] Error en offset {offset}: {e}")
                    stats["errores"] += 1
                    break

                spots = data.get("spots", [])
                if not spots:
                    break

                nuevos_en_pagina = 0
                for raw in spots:
                    sid = str(raw.get("id"))
                    if sid in seen_ids:
                        continue
                        
                    seen_ids.add(sid)
                    nuevos_en_pagina += 1
                    
                    norm = self.normalize(raw)
                    if not norm:
                        continue

                    try:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                existente = await find_spot_cercano(
                                    conn, norm["lat"], norm["lon"], self.dedup_radius_m
                                )
                                if existente:
                                    spot_id = existente["id"]
                                    await enriquecer_spot(conn, spot_id, norm, self.name)
                                    stats["actualizados"] += 1
                                else:
                                    norm["fuentes"] = [self.name]
                                    spot_id = await crear_spot(conn, norm)
                                    stats["nuevos"] += 1

                                await upsert_source_record(
                                    conn, spot_id, self.name, sid, raw, norm
                                )
                    except Exception as e:
                        logger.error(f"[ROADSURFER] Error spot '{norm.get('nombre')}': {e}")
                        stats["errores"] += 1

                logger.info(f"[ROADSURFER] Offset {offset}: procesados {len(spots)} spots.")
                
                # Rompemos si nos devuelven menos de 'size' o si todos ya los habíamos visto
                if nuevos_en_pagina == 0 or len(spots) < size:
                    break
                    
                offset += size
                await asyncio.sleep(self.rate_limit)

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[ROADSURFER] Completado en {dur:.0f}s | {stats}")
        return stats
