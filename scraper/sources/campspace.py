"""Campspace — scraper para ubicaciones en la naturaleza."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

BASE_URL = "https://campspace.com/en/discover/campsites?_format=json"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-requested-with": "XMLHttpRequest"
}

class CampspaceSource(AbstractSource):
    name = "campspace"
    rate_limit = 1.0
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Campspace se extrae por lista directa")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["lat"])
            lon = float(raw["lng"])
        except (KeyError, TypeError, ValueError):
            return None

        # Campspace no provee facilities detalladas en el JSON del mapa,
        # pero sabemos que son de pago y están en la naturaleza.
        
        return {
            "source_id": str(raw.get("id")),
            "nombre": raw.get("title", "Campspace Spot").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": "naturaleza",
            "gratuito": False,
            "web": raw.get("href", ""),
            "fotos_urls": [] # No vienen en la lista
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
            page = 0
            seen_ids = set()
            
            while True:
                url = f"{BASE_URL}&page={page}" if page > 0 else BASE_URL
                try:
                    logger.info(f"[CAMPSPACE] Obteniendo página {page}...")
                    resp = await client.get(url, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[CAMPSPACE] Error obteniendo página {page}: {e}")
                    stats["errores"] += 1
                    break

                if not data or not isinstance(data, list) or len(data) == 0:
                    break

                nuevos_en_pagina = 0
                for raw in data:
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
                        logger.error(f"[CAMPSPACE] Error spot '{norm.get('nombre')}': {e}")
                        stats["errores"] += 1

                    if nuevos_en_pagina % 200 == 0:
                        logger.info(f"[CAMPSPACE] Progreso página {page}: {nuevos_en_pagina} procesados... (new={stats['nuevos']}, upd={stats['actualizados']})")

                logger.info(f"[CAMPSPACE] Página {page}: {len(data)} spots procesados en total.")
                
                # Si la página no trajo nuevos o fueron muy pocos, asumimos que no hay paginación o ya dimos la vuelta
                if nuevos_en_pagina == 0:
                    break
                    
                page += 1
                await asyncio.sleep(self.rate_limit)

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[CAMPSPACE] Completado en {dur:.0f}s | {stats}")
        return stats
