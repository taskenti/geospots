"""Vansite (Sharetribe Flex API) — scraper."""

import asyncio
from datetime import datetime, timezone
import urllib.parse
from loguru import logger
import httpx

from sources.base import AbstractSource

def transit_to_dict(t):
    """
    Convierte el formato 'Transit JSON' (usado por Sharetribe/Clojure) a un diccionario de Python.
    Un mapa en Transit se representa como una lista donde el primer elemento es '^ ',
    y los siguientes son pares clave-valor alternados.
    """
    if isinstance(t, list) and len(t) > 0 and t[0] == "^ ":
        d = {}
        for i in range(1, len(t), 2):
            if i+1 < len(t):
                d[t[i]] = transit_to_dict(t[i+1])
        return d
    elif isinstance(t, list):
        return [transit_to_dict(x) for x in t]
    else:
        return t

BASE_URL = "https://flex-api.sharetribe.com/v1/api/listings/query"

# IMPORTANTE: Sharetribe suele requerir un Bearer Token en la cabecera, o un Client ID.
# Si falla con 401/403, necesitaremos inyectarlo aquí.
HEADERS = {
    "accept": "application/transit+json",
    "origin": "https://vansite.eu",
    "referer": "https://vansite.eu/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

class VansiteSource(AbstractSource):
    name = "vansite"
    rate_limit = 1.0
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Vansite usa paginación global en lugar de grid")

    def normalize(self, raw: dict) -> dict | None:
        try:
            attrs = raw.get("~:attributes", {})
            geo = attrs.get("~:geolocation", [])
            # geo es de la forma ["~#geo", [lat, lng]]
            coords = geo[1]
            lat = float(coords[0])
            lon = float(coords[1])
        except (KeyError, TypeError, ValueError, IndexError):
            return None

        # Identificador (le quitamos el prefijo ~u de los UUIDs de Transit)
        sid = str(raw.get("~:id", "")).replace("~u", "")
        
        # Atributos públicos
        pub_data = attrs.get("~:publicData", {})
        
        precio_raw = attrs.get("~:price", [])
        gratuito = False
        if len(precio_raw) == 2 and isinstance(precio_raw[1], list):
            # ["~#mn", [1000, "EUR"]] -> 10.00 EUR
            if precio_raw[1][0] == 0:
                gratuito = True

        tipo = "naturaleza"
        if pub_data.get("~:category") == "campsite":
            tipo = "camping"

        return {
            "source_id": sid,
            "nombre": attrs.get("~:title", "Vansite Spot").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "web": f"https://vansite.eu/l/{sid}",
            "fotos_urls": [] # Para fotos habría que mapear el 'relationships' y el objeto 'included', lo omitimos para mayor velocidad
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

        # Pedimos toda la zona de Europa/Mundo con bounds enormes
        params = {
            "bounds": "71.5,-25.0,34.0,45.0", # Europa completa
            "mapSearch": "true",
            "per_page": 100,
            "pub_hidden": "false",
            "sort": "pub_verified,meta_rating",
            "fields.listing": "title,state,geolocation,price,createdAt,publicData.category,publicData.locationPlace,publicData.verified,metadata.rating"
        }

        async with httpx.AsyncClient(headers=HEADERS) as client:
            page = 1
            seen_ids = set()
            
            while True:
                params["page"] = page
                url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
                
                try:
                    logger.info(f"[VANSITE] Obteniendo página {page}...")
                    resp = await client.get(url, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (401, 403):
                        logger.error(f"[VANSITE] Error de Autenticación (401/403). Falta Token. {e}")
                        stats["errores"] += 1
                        break
                    logger.error(f"[VANSITE] HTTP Error en página {page}: {e}")
                    stats["errores"] += 1
                    break
                except Exception as e:
                    logger.error(f"[VANSITE] Error en página {page}: {e}")
                    stats["errores"] += 1
                    break

                # data[2] es donde viene el array de listings en Transit
                try:
                    parsed_data = transit_to_dict(data)
                    listings = parsed_data.get("~:data", [])
                except Exception as e:
                    logger.error(f"[VANSITE] Error parseando formato Transit JSON: {e}")
                    break

                if not listings or len(listings) == 0:
                    break

                nuevos_en_pagina = 0
                for raw in listings:
                    sid = str(raw.get("~:id", ""))
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
                        logger.error(f"[VANSITE] Error spot '{norm.get('nombre')}': {e}")
                        stats["errores"] += 1

                logger.info(f"[VANSITE] Página {page}: procesados {len(listings)} spots.")
                
                if nuevos_en_pagina == 0 or len(listings) < 100:
                    break
                    
                page += 1
                await asyncio.sleep(self.rate_limit)

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[VANSITE] Completado en {dur:.0f}s | {stats}")
        return stats
