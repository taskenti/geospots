"""Alpaca Camping source implementation for GeoSpots scraper."""

import asyncio
import json
import re
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_alpacacamping, merge_extra

class AlpacaCampingSource(AbstractSource):
    name = "alpacacamping"
    rate_limit = 1.0
    dedup_radius_m = 80.0

    HEADERS = {
        "accept": "application/json",
        "user-agent": "okhttp/4.12.0",
        "x-api-key": "6qJIPHk0q1tCw38g2wvQlbIwsNik"
    }

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        # Not used since we override run to do a global pagination query
        raise NotImplementedError("AlpacaCamping uses global pagination, not bbox grid.")

    def normalize(self, raw: dict) -> dict | None:
        try:
            alpaca_id = raw.get("id")
            if not alpaca_id:
                return None

            name = (raw.get("name") or "Sin nombre").strip()[:200]
            addr = raw.get("property_address") or {}
            
            lat = addr.get("latitude")
            lon = addr.get("longitude")
            if lat is None or lon is None:
                return None

            # Determine type: default to area_ac, but if only tents are allowed (ID 27)
            # and motorhomes (25) or caravans (28) are not, it's naturaleza.
            amenities = raw.get("amenities_infos") or {}
            am_ids = set(amenities.get("id") or [])
            
            tipo = "area_ac"
            if 27 in am_ids and not (25 in am_ids or 28 in am_ids):
                tipo = "naturaleza"

            # Price
            price_obj = raw.get("property_price") or {}
            price_val = price_obj.get("price")
            precio_aprox = None
            precio_info = None
            if price_val is not None:
                try:
                    precio_aprox = float(price_val)
                    currency = price_obj.get("currency_code") or "EUR"
                    precio_info = f"{price_val} {currency}"
                except (ValueError, TypeError):
                    pass

            rating_val = raw.get("avg_rating")
            rating = float(rating_val) if rating_val and float(rating_val) > 0 else None
            num_reviews = int(raw.get("reviews_count") or 0)

            # Amenities mapping
            agua_potable = (14 in am_ids or 238 in am_ids)
            electricidad = (13 in am_ids or 223 in am_ids)
            vaciado_grises = (20 in am_ids)
            vaciado_negras = (21 in am_ids)
            wc_publico = (16 in am_ids or 284 in am_ids)
            ducha = (17 in am_ids or 476 in am_ids)
            wifi = (1 in am_ids)

            perros = None
            if 4 in am_ids:
                perros = False
            elif any(i in am_ids for i in [41, 315, 229, 231]):
                perros = True

            acceso_grandes = (26 in am_ids)

            # Photos
            photos = []
            for p in raw.get("photos") or []:
                url = p.get("largeUrl") or p.get("mediumUrl")
                if url and url not in photos:
                    photos.append(url)

            # Description summary
            desc_de = (raw.get("property_description") or {}).get("summary")

            norm = {
                "source_id": str(alpaca_id),
                "nombre": name,
                "lat": float(lat),
                "lon": float(lon),
                "tipo": tipo,
                "gratuito": False, # Alpaca Camping is a paid platform
                "precio_info": precio_info,
                "precio_aprox": precio_aprox,
                "rating_promedio": rating,
                "num_reviews": num_reviews,
                "country_iso": (addr.get("country") or "").lower() or None,
                "region": addr.get("state") or addr.get("city") or None,
                "web": raw.get("detail_page_base_link") or f"https://www.alpacacamping.de/properties/{alpaca_id}",
                "descripcion_de": desc_de,
                "agua_potable": agua_potable,
                "electricidad": electricidad,
                "vaciado_grises": vaciado_grises,
                "vaciado_negras": vaciado_negras,
                "wc_publico": wc_publico,
                "ducha": ducha,
                "wifi": wifi,
                "perros": perros,
                "acceso_grandes": acceso_grandes,
                "fotos_urls": photos[:10],
            }
            return merge_extra(norm, extract_alpacacamping(raw))
        except Exception as e:
            logger.error(f"[alpacacamping] Error normalizing item: {e}")
            return None

    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
        """Global scan utilizing pagination on a wide bounding box query."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        seen_ids = set()
        search_url = "https://search.alpacacamping.de/api/search"
        
        # Bounding box covering Europe/entire world
        params = {
            "min_lat": "-90.0", "max_lat": "90.0",
            "min_long": "-180.0", "max_long": "180.0",
            "property_type": "1", "size": "200", "page": "1"
        }

        async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=20) as client:
            page = 1
            while True:
                params["page"] = str(page)
                logger.info(f"[alpacacamping] Fetching page {page}...")
                try:
                    await asyncio.sleep(self.rate_limit)
                    resp = await client.get(search_url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[alpacacamping] Error fetching search page {page}: {e}")
                    stats["errores"] += 1
                    break

                hits = data.get("hits") or []
                if not hits:
                    break

                for raw_item in hits:
                    norm = self.normalize(raw_item)
                    if not norm:
                        continue

                    sid = str(norm["source_id"])
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)

                    try:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                existente = await find_spot_cercano(
                                    conn, norm["lat"], norm["lon"],
                                    self.dedup_radius_m,
                                    nombre=norm.get("nombre"),
                                    tipo=norm.get("tipo")
                                )

                                if existente:
                                    spot_id = existente["id"]
                                    await enriquecer_spot(
                                        conn, spot_id, norm, self.name
                                    )
                                    stats["actualizados"] += 1
                                else:
                                    norm["fuentes"] = [self.name]
                                    spot_id = await crear_spot(conn, norm)
                                    stats["nuevos"] += 1

                                await upsert_source_record(
                                    conn, spot_id, self.name, sid,
                                    raw_item, norm
                                )
                    except Exception as e:
                        logger.error(f"[alpacacamping] Error saving spot {sid}: {e}")
                        stats["errores"] += 1

                await self.update_job_progress(pool, job_id, len(seen_ids), 0, stats)

                if len(hits) < 200:
                    break
                page += 1

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[alpacacamping] Completado en {dur:.0f}s | {stats}")
        return stats

    def _parse_reviews_html(self, html: str, spot_id: int) -> list[dict]:
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(html, 'html.parser')
        reviews_list = []
        
        # 1. Parse structured pre dumps inside comments
        comments = re.findall(r'<!--\s*<pre>(.*?)</pre>\s*-->', html, re.DOTALL)
        parsed_dumps = []
        for c in comments:
            if "App\\Models\\Reviews" not in c:
                continue
            
            attr_match = re.search(r'\["attributes":protected\]=>\s*array\(\d+\)\s*\{(.*?)\n\s*\}', c, re.DOTALL)
            if not attr_match:
                continue
            content = attr_match.group(1)
            
            id_m = re.search(r'\["id"\]=>\s*int\((\d+)\)', content)
            r_id = int(id_m.group(1)) if id_m else None
            if not r_id:
                continue
                
            rating_m = re.search(r'\["rating"\]=>\s*int\((\d+)\)', content)
            rating = int(rating_m.group(1)) if rating_m else None
            
            created_m = re.search(r'\["created_at"\]=>\s*string\(19\)\s*"([^"]+)"', content)
            fecha = None
            if created_m:
                try:
                    fecha = datetime.strptime(created_m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except Exception:
                    pass
                    
            msg_m = re.search(r'\["message"\]=>\s*string\(\d+\)\s*"(.*?)"\s*\n\s*\["(?:secret_feedback|sender_id|receiver_id)', content, re.DOTALL)
            texto = msg_m.group(1) if msg_m else None
            
            parsed_dumps.append({
                "id": r_id,
                "texto": texto,
                "rating": rating,
                "fecha": fecha
            })
            
        # 2. Iterate over DOM visual reviews to extract author names
        blocks = soup.find_all("div", class_=lambda x: x and "rounded-xl" in x and "border-gray-200" in x)
        for b in blocks:
            p = b.find("p", class_=lambda x: x and "text-gray-800" in x)
            if not p:
                continue
            
            text_elements = [t.strip() for t in b.find_all(string=True) if t.strip()]
            author = "Usuario AlpacaCamping"
            if text_elements:
                author = text_elements[0]
                
            p_text = p.text.strip()
            
            matched_dump = None
            for d in parsed_dumps:
                # Compare cleaned text subsets to align author with pre dump
                d_text_clean = re.sub(r'[^a-zA-Z]', '', d["texto"] or "")
                p_text_clean = re.sub(r'[^a-zA-Z]', '', p_text)
                if d_text_clean and p_text_clean[:30] in d_text_clean:
                    matched_dump = d
                    break
                    
            if matched_dump:
                reviews_list.append({
                    "spot_id": spot_id,
                    "source": self.name,
                    "source_review_id": str(matched_dump["id"]),
                    "texto": p_text,
                    "rating": float(matched_dump["rating"]) if matched_dump["rating"] is not None else None,
                    "autor": author,
                    "fecha": matched_dump["fecha"],
                    "idioma": "de"
                })
                
        return reviews_list

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        from db import upsert_review
        
        stats = {
            "nuevos": 0,
            "actualizados": 0,
            "reviews_nuevas": 0,
            "errores": 0
        }

        logger.info(f"[{self.name}] Buscando spots con reviews pendientes...")
        async with pool.acquire() as conn:
            review_jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id, sr.review_count, COALESCE(r.cnt, 0) as db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) as cnt
                    FROM reviews
                    WHERE source = 'alpacacamping'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'alpacacamping'
                  AND sr.review_count > 0
                  AND (
                    (sr.normalized_data->>'reviews_fetched') IS NULL
                    OR COALESCE(r.cnt, 0) < sr.review_count
                  )
                ORDER BY sr.review_count DESC;
            """)

        logger.info(f"[{self.name}] {len(review_jobs)} spots con reviews pendientes.")
        if not review_jobs:
            return stats

        job_queue = asyncio.Queue()
        for r in review_jobs:
            await job_queue.put(dict(r))

        async def worker(client):
            while not job_queue.empty():
                try:
                    job = job_queue.get_nowait()
                except (asyncio.QueueEmpty, asyncio.CancelledError):
                    break

                spot_id = job["spot_id"]
                sid = job["source_id"]
                url = f"https://www.alpacacamping.de/properties/{sid}"

                try:
                    await asyncio.sleep(self.rate_limit)
                    resp = await client.get(url, timeout=20)
                    if resp.status_code == 404:
                        # Skip if not found
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                UPDATE source_records
                                SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb
                                WHERE source = 'alpacacamping' AND source_id = $1
                            """, sid)
                        job_queue.task_done()
                        continue
                        
                    resp.raise_for_status()
                    
                    parsed_reviews = self._parse_reviews_html(resp.text, spot_id)
                    inserted = 0
                    if parsed_reviews:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                for rev in parsed_reviews:
                                    await upsert_review(conn, rev)
                                    inserted += 1
                                    
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE source_records
                            SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb
                            WHERE source = 'alpacacamping' AND source_id = $1
                        """, sid)
                        if inserted > 0:
                            await conn.execute("""
                                UPDATE spots SET total_reviews = (
                                    SELECT COUNT(*) FROM reviews WHERE spot_id = $1
                                ) WHERE id = $1
                            """, spot_id)

                    stats["reviews_nuevas"] += inserted
                    stats["actualizados"] += 1
                except Exception as e:
                    logger.error(f"[{self.name}] Error descargando reviews para spot {sid}: {e}")
                    stats["errores"] += 1

                job_queue.task_done()

        # Public detail page doesn't require API key, just browser user-agent
        web_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with httpx.AsyncClient(headers=web_headers, follow_redirects=True, timeout=25) as client:
            workers = [asyncio.create_task(worker(client)) for _ in range(3)]
            await asyncio.gather(*workers)

        return stats
