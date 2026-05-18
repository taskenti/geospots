"""Nomady — scraper de la API oculta de puntos comprimidos."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

# Esta es la URL mágica que se descarga TODA la base de datos de Nomady de un tirón.
BASE_URL = "https://api.nomady.camp/cabin/public-compressed-v2"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://nomady.camp",
    "referer": "https://nomady.camp/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

class NomadySource(AbstractSource):
    name = "nomady"
    rate_limit = 1.0
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Nomady usa un dump masivo, no grid")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["latitude"])
            lon = float(raw["longitude"])
        except (KeyError, TypeError, ValueError):
            return None

        slug = raw.get("slug", "")
        # En Nomady los tipos suelen ser 'tent', 'caravan', 'hut', etc.
        types = raw.get("types", [])
        
        tipo = "otro"
        if "hut" in types:
            tipo = "camping"
        elif any(t in types for t in ["caravan", "medium_vehicle", "large_vehicle"]):
            tipo = "area_ac" # Sitios privados aptos para ACs
        elif "tent" in types:
            tipo = "naturaleza" # Sitios en prados/bosques

        fotos = raw.get("imageUrls", [])
        
        # Precio: Nomady siempre es de pago
        gratuito = False

        # Generar enlace web (normalmente es /en/c/slug)
        web = f"https://nomady.camp/en/c/{slug}" if slug else "https://nomady.camp"

        return {
            "source_id": str(raw.get("id")),
            "nombre": raw.get("title", "Nomady Camp").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "country_iso": raw.get("country", "")[:2],
            "agua_potable": bool(raw.get("drinkingWater")),
            "wc_publico": bool(raw.get("regularToilet") or raw.get("outdoorToilet") or raw.get("toiletQuickAccessible")),
            "ducha": bool(raw.get("regularShower") or raw.get("outdoorShower")),
            "electricidad": bool(raw.get("power")),
            "vaciado_negras": bool(raw.get("blackWater")),
            "vaciado_grises": bool(raw.get("greyWater")),
            "fotos_urls": fotos[:5], # Guardamos hasta 5 fotos para no saturar
            "web": web
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
            try:
                logger.info(f"[NOMADY] Descargando dump masivo comprimido desde {BASE_URL}...")
                resp = await client.get(BASE_URL, timeout=60)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"[NOMADY] Error descargando el dump: {e}")
                stats["errores"] += 1
                data = []

            if data and isinstance(data, list):
                logger.info(f"[NOMADY] Descarga completada: {len(data)} spots obtenidos. Procesando...")
                
                for raw in data:
                    norm = self.normalize(raw)
                    if not norm:
                        continue

                    sid = norm["source_id"]
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
                        logger.error(f"[NOMADY] Error spot '{norm.get('nombre')}': {e}")
                        stats["errores"] += 1
                        
            else:
                logger.warning("[NOMADY] No se obtuvieron datos o el formato es incorrecto.")

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[NOMADY] Completado en {dur:.0f}s | {stats}")
        return stats
