"""Portugal EasyCamp — scraper de agroturismos vía Sitemap + Regex HTML."""

import asyncio
import re
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

class PortugalEasyCampSource(AbstractSource):
    name = "portugaleasycamp"
    rate_limit = 2.0  # Servidor WordPress pequeño, hay que ser amables
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Utiliza sitemap y regex de DOM directo")

    def normalize(self, raw: dict) -> dict | None:
        # En este scraper, toda la normalización se hace directamente en run()
        return raw

    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {},
        }

        async with httpx.AsyncClient(follow_redirects=True) as client:
            # 1. Obtener el Sitemap de tours
            logger.info("[portugaleasycamp] Descargando sitemap de fincas...")
            try:
                resp = await client.get("https://portugaleasycamp.com/tour-sitemap.xml", timeout=30)
                resp.raise_for_status()
                sitemap_xml = resp.text
            except Exception as e:
                logger.error(f"[portugaleasycamp] Error cargando sitemap: {e}")
                stats["errores"] = 1
                async with pool.acquire() as conn:
                    await finish_scraper_log(conn, log_id, stats)
                return stats
            
            urls = re.findall(r'<!\[CDATA\[(https://portugaleasycamp\.com/tour/[^\]]+)\]\]>', sitemap_xml)
            logger.info(f"[portugaleasycamp] Encontradas {len(urls)} fincas en el sitemap.")

            for i, url in enumerate(urls):
                try:
                    resp = await client.get(url, timeout=20)
                    resp.raise_for_status()
                    html = resp.text
                    
                    # 2. Extracción Regex de Variables JS incrustadas
                    lat_match = re.search(r"location_latitude:\s*([\d.-]+)", html)
                    lon_match = re.search(r"location_longitude:\s*([\d.-]+)", html)
                    name_match = re.search(r"name:\s*'([^']+)'", html)
                    img_match = re.search(r"map_image_url:\s*'([^']+)'", html)
                    
                    if not (lat_match and lon_match and name_match):
                        logger.warning(f"[portugaleasycamp] No se encontraron coordenadas en {url}")
                        continue
                        
                    lat = float(lat_match.group(1))
                    lon = float(lon_match.group(1))
                    name = name_match.group(1).strip()
                    
                    # Extracción del precio aproximado del pack en la tabla
                    price = None
                    price_match = re.search(r'class="text-right total-cost">\s*(\d+)', html)
                    if price_match:
                        price = price_match.group(1)
                        
                    fotos = []
                    if img_match:
                        fotos.append(img_match.group(1))
                    
                    if price:
                        desc = f"Finca agroturística de Portugal EasyCamp. Pernocta legal (24h) garantizada mediante la compra de su pack de bienvenida (vino, miel, etc.) en la web oficial. Coste aprox. del pack: {price}€."
                    else:
                        desc = "Finca agroturística de Portugal EasyCamp. Pernocta legal (24h) garantizada mediante la compra de un pack de bienvenida de sus productos locales."

                    sid = url.strip('/').split('/')[-1]

                    # Precio del pack a euros si vino parseado del HTML
                    precio_aprox = None
                    precio_info = None
                    if price:
                        try:
                            precio_aprox = float(price)
                            precio_info = f"{precio_aprox:.2f} € (pack bienvenida)"
                        except (ValueError, TypeError):
                            pass

                    norm = {
                        "source_id": sid,
                        "nombre": f"EasyCamp - {name}",
                        "lat": lat,
                        "lon": lon,
                        "tipo": "naturaleza", # Agroturismo / Finca
                        "gratuito": False, # Requiere comprar el pack
                        "precio_aprox": precio_aprox,
                        "precio_info": precio_info,
                        "country_iso": "pt",
                        "web": url,
                        "fotos_urls": fotos,
                        "descripcion_es": desc,
                        # PortugalEasyCamp son agroturismos con pernocta legal regulada;
                        # marcamos online_booking=True (compra del pack en web)
                        "online_booking": True,
                    }
                    
                    # 3. Guardado en BD (PostGIS)
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            existente = await find_spot_cercano(conn, norm["lat"], norm["lon"], self.dedup_radius_m)
                            if existente:
                                spot_id = existente["id"]
                                await enriquecer_spot(conn, spot_id, norm, self.name)
                                stats["actualizados"] += 1
                            else:
                                norm["fuentes"] = [self.name]
                                spot_id = await crear_spot(conn, norm)
                                stats["nuevos"] += 1
                                
                            await upsert_source_record(conn, spot_id, self.name, sid, {"url": url}, norm)

                except Exception as e:
                    logger.error(f"[portugaleasycamp] Error procesando {url}: {e}")
                    stats["errores"] += 1

                # Rate limiting estricto
                await asyncio.sleep(self.rate_limit)
                
                if (i + 1) % 10 == 0:
                    logger.info(f"[portugaleasycamp] Progreso: {i+1}/{len(urls)}...")
                    await self.update_job_progress(pool, job_id, i + 1, len(urls), stats)

        # Finalizar ejecución
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[portugaleasycamp] Completado en {dur:.0f}s | {stats}")
        return stats
