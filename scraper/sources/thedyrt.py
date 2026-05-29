"""The Dyrt source implementation for GeoSpots scraper.

API endpoints:
- Search results (bbox): GET https://thedyrt.com/api/v9/locations/search-results
- Campground details: GET https://thedyrt.com/api/v9/campgrounds/{location_id}
- Reviews: GET https://thedyrt.com/api/v9/reviews?filter[subject_id]={location_id}
"""

import asyncio
import json
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_thedyrt, merge_extra


class TheDyrtSource(AbstractSource):
    name = "thedyrt"
    rate_limit = 1.0  # 1s between requests to be safe
    grid_step = 2.0  # grid cell step
    dedup_radius_m = 100.0

    SEARCH_URL = "https://thedyrt.com/api/v9/locations/search-results"
    DETAIL_URL = "https://thedyrt.com/api/v9/campgrounds/{location_id}"
    REVIEWS_URL = "https://thedyrt.com/api/v9/reviews"

    HEADERS = {
        "accept": "application/vnd.api+json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    async def fetch_cell(self, client: httpx.AsyncClient, tl_lat: float, tl_lon: float, br_lat: float, br_lon: float) -> list[dict]:
        # bbox format: sw_lon,sw_lat,ne_lon,ne_lat
        bbox = f"{tl_lon},{br_lat},{br_lon},{tl_lat}"
        results = []
        page = 1
        page_size = 500

        max_5xx_retries = 3
        attempt = 0
        while True:
            params = {
                "filter[search][drive_time]": "any",
                "filter[search][air_quality]": "any",
                "filter[search][electric_amperage]": "any",
                "filter[search][max_vehicle_length]": "any",
                "filter[search][price]": "any",
                "filter[search][rating]": "any",
                "filter[search][bbox]": bbox,
                "sort": "recommended",
                "page[number]": str(page),
                "page[size]": str(page_size),
            }
            try:
                await asyncio.sleep(self.rate_limit)
                resp = await client.get(self.SEARCH_URL, params=params, timeout=20)
                if resp.status_code == 429:
                    logger.warning("[thedyrt] Rate limit (429) hit. Esperando 60s...")
                    await asyncio.sleep(60)
                    continue
                # 502/503/504 son errores de gateway transitorios: reintentar la
                # MISMA página con backoff en vez de abortar la celda (antes un 502
                # rompía el bucle y perdía el resto de páginas de la celda).
                if resp.status_code in (502, 503, 504):
                    attempt += 1
                    if attempt > max_5xx_retries:
                        logger.error(
                            f"[thedyrt] HTTP {resp.status_code} en page {page} cell {bbox} "
                            f"tras {max_5xx_retries} reintentos. Abandonando celda."
                        )
                        break
                    wait = 5 * (2 ** (attempt - 1))
                    logger.warning(
                        f"[thedyrt] HTTP {resp.status_code} en page {page} cell {bbox}. "
                        f"Reintento {attempt}/{max_5xx_retries} en {wait}s..."
                    )
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                attempt = 0  # página servida con éxito: resetear contador de gateway
                data = resp.json()
                items = data.get("data", [])
                if not items:
                    break
                results.extend(items)

                meta = data.get("meta", {})
                page_count = meta.get("page-count") or 1
                if page >= page_count:
                    break
                page += 1
            except Exception as e:
                logger.error(f"[thedyrt] Error fetching page {page} for cell {bbox}: {e}")
                break

        return results

    def normalize(self, raw: dict) -> dict | None:
        try:
            attrs = raw.get("attributes", {})
            loc_id = attrs.get("location-id") or raw.get("id")
            if not loc_id:
                return None

            lat = attrs.get("latitude")
            lon = attrs.get("longitude")
            if lat is None or lon is None:
                return None

            try:
                lat = float(lat)
                lon = float(lon)
            except (ValueError, TypeError):
                return None

            # Category and type mapping
            category = attrs.get("category") or ""
            accommodation_dispersed = attrs.get("accommodation-dispersed")
            pin_type = attrs.get("pin-type") or ""

            if category == "dispersed" or accommodation_dispersed is True:
                tipo = "wild"
            elif pin_type == "rv_park":
                tipo = "area_ac"
            else:
                tipo = "camping"

            # Prices
            price_low = attrs.get("price-low")
            price_high = attrs.get("price-high")
            precio_aprox = None
            precio_info = None
            if price_low is not None:
                try:
                    precio_aprox = float(price_low)
                    if price_high is not None and float(price_high) > precio_aprox:
                        precio_info = f"{precio_aprox:.2f} - {float(price_high):.2f} USD"
                    else:
                        precio_info = f"{precio_aprox:.2f} USD"
                except (ValueError, TypeError):
                    pass
            gratuito = (precio_aprox == 0.0) if precio_aprox is not None else None

            # Rating
            rating = None
            rating_val = attrs.get("rating")
            if rating_val is not None:
                try:
                    rating = float(rating_val)
                    if rating <= 0:
                        rating = None
                except (ValueError, TypeError):
                    pass

            # Reviews count
            reviews_count = 0
            rev_cnt_val = attrs.get("reviews-count")
            if rev_cnt_val is not None:
                try:
                    reviews_count = int(rev_cnt_val)
                except (ValueError, TypeError):
                    pass

            # Photos
            photos = attrs.get("photo-urls") or []
            if not photos and attrs.get("photo-url"):
                photos = [attrs.get("photo-url")]

            # Web url
            slug = attrs.get("slug")
            region_slug = attrs.get("nearest-city-region-name") or attrs.get("region-name") or "usa"
            region_slug = region_slug.lower().replace(" ", "-")
            web = f"https://thedyrt.com/camping/{region_slug}/{slug}" if slug else None

            # Basic amenities (can be enriched in Phase 2)
            drinking_water = attrs.get("drinking-water") or attrs.get("water-hookups")
            toilets = attrs.get("toilets")
            showers = attrs.get("showers")
            wifi = attrs.get("wifi")
            pets_allowed = attrs.get("pets-allowed")
            big_rig_friendly = attrs.get("big-rig-friendly")
            reservable = attrs.get("reservable") or attrs.get("permit-required")
            sanitary_dump = attrs.get("sanitary-dump")
            campsites_count = attrs.get("campsites-count")

            # Capacity
            num_plazas = None
            if campsites_count is not None:
                try:
                    num_plazas = int(campsites_count)
                except (ValueError, TypeError):
                    pass

            norm = {
                "source_id": str(loc_id),
                "nombre": (attrs.get("name") or "Sin nombre").strip()[:200],
                "lat": lat,
                "lon": lon,
                "tipo": tipo,
                "gratuito": gratuito,
                "precio_info": precio_info,
                "precio_aprox": precio_aprox,
                "rating_promedio": rating,
                "num_reviews": reviews_count,
                "fotos_urls": photos[:8],
                "web": web,
                "descripcion_en": attrs.get("description") or attrs.get("ai-description"),
                "region": attrs.get("region-name") or attrs.get("region"),
                "country_iso": "us",
                "agua_potable": drinking_water,
                "wc_publico": toilets,
                "ducha": showers,
                "wifi": wifi,
                "perros": pets_allowed,
                "acceso_grandes": big_rig_friendly,
                "reserva_req": reservable,
                "vaciado_grises": sanitary_dump,
                "vaciado_negras": sanitary_dump,
                "num_plazas": num_plazas,
            }
            return merge_extra(norm, extract_thedyrt(raw))
        except Exception as e:
            logger.error(f"[thedyrt] Error normalizing raw data: {e}")
            return None

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        from db import upsert_review

        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}

        # 1. Get list of spots needing details/reviews enrichment
        async with pool.acquire() as conn:
            jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id, sr.review_count, COALESCE(r.cnt, 0) as db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) as cnt
                    FROM reviews
                    WHERE source = 'thedyrt'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'thedyrt'
                  AND (
                    (sr.normalized_data->>'details_fetched') IS NULL
                    OR (sr.review_count > 0 AND COALESCE(r.cnt, 0) < sr.review_count)
                  )
                ORDER BY sr.review_count DESC;
            """)

        logger.info(f"[thedyrt] Encontrados {len(jobs)} spots con enriquecimiento/reviews pendientes.")
        if not jobs:
            return stats

        job_queue = asyncio.Queue()
        for j in jobs:
            await job_queue.put(dict(j))

        async def enrich_worker(client):
            while not job_queue.empty():
                try:
                    job = await job_queue.get()
                except asyncio.CancelledError:
                    break

                spot_id = job["spot_id"]
                sid = job["source_id"]

                # 2. Fetch Campground details
                detail_url = self.DETAIL_URL.format(location_id=sid)
                detail_data = None
                try:
                    await asyncio.sleep(self.rate_limit)
                    resp = await client.get(detail_url, timeout=15)
                    if resp.status_code == 429:
                        logger.warning(f"[thedyrt] Detail 429 limit. Sleeping 60s for spot {sid}")
                        await asyncio.sleep(60)
                        resp = await client.get(detail_url, timeout=15)
                    
                    if resp.status_code == 200:
                        detail_data = resp.json().get("data", {})
                    else:
                        logger.warning(f"[thedyrt] Detail status {resp.status_code} for spot {sid}")
                except Exception as e:
                    logger.error(f"[thedyrt] Error fetching details for spot {sid}: {e}")
                    stats["errores"] += 1

                if detail_data:
                    # Normalize detailed data
                    detail_norm = self.normalize(detail_data)
                    if detail_norm:
                        # Update spot in database with details
                        try:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    from db import enriquecer_spot
                                    await enriquecer_spot(conn, spot_id, detail_norm, self.name)
                                    
                                    # Mark details_fetched in the source records only
                                    detail_norm["details_fetched"] = True
                                    
                                    # Update source record
                                    await conn.execute("""
                                        UPDATE source_records
                                        SET normalized_data = normalized_data || $1::jsonb,
                                            raw_data = raw_data || $2::jsonb,
                                            last_seen = NOW()
                                        WHERE source = $3 AND source_id = $4
                                    """, json.dumps(detail_norm), json.dumps(detail_data), self.name, sid)
                            stats["actualizados"] += 1
                        except Exception as e:
                            logger.error(f"[thedyrt] DB error updating details for {sid}: {e}")
                            stats["errores"] += 1

                # 3. Fetch Reviews
                page = 1
                page_size = 100
                has_more_reviews = True
                
                while has_more_reviews:
                    params = {
                        "filter[subject_id]": sid,
                        "page[number]": str(page),
                        "page[size]": str(page_size),
                    }
                    try:
                        await asyncio.sleep(self.rate_limit)
                        r_resp = await client.get(self.REVIEWS_URL, params=params, timeout=15)
                        if r_resp.status_code == 429:
                            logger.warning(f"[thedyrt] Reviews 429 limit. Sleeping 60s for spot {sid}")
                            await asyncio.sleep(60)
                            continue
                        
                        r_resp.raise_for_status()
                        r_data = r_resp.json()
                        rev_list = r_data.get("data", [])
                        if not rev_list:
                            break

                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                for rev_raw in rev_list:
                                    rev_attrs = rev_raw.get("attributes", {})
                                    body = (rev_attrs.get("body") or "").strip() or None
                                    rating = rev_attrs.get("rating")
                                    
                                    # Created-at parsing
                                    created_at = None
                                    created_str = rev_attrs.get("created-at")
                                    if created_str:
                                        try:
                                            created_at = datetime.strptime(created_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                                        except Exception:
                                            pass
                                            
                                    rev_dict = {
                                        "spot_id": spot_id,
                                        "source": self.name,
                                        "source_review_id": f"dyrt_{rev_raw['id']}",
                                        "texto": body,
                                        "rating": rating,
                                        "autor": rev_attrs.get("title") or "Usuario The Dyrt",
                                        "fecha": created_at,
                                        "idioma": "en",
                                    }
                                    inserted = await upsert_review(conn, rev_dict)
                                    stats["reviews_nuevas"] += int(bool(inserted))

                        meta = r_data.get("meta", {})
                        page_count = meta.get("page-count") or 1
                        if page >= page_count:
                            break
                        page += 1
                    except Exception as e:
                        logger.error(f"[thedyrt] Error fetching reviews page {page} for spot {sid}: {e}")
                        stats["errores"] += 1
                        break
                
                # Mark as reviews fetched in metadata
                try:
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE source_records
                            SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb
                            WHERE source = $1 AND source_id = $2
                        """, self.name, sid)
                except Exception as e:
                    logger.error(f"[thedyrt] Failed to mark reviews_fetched for {sid}: {e}")

                job_queue.task_done()

        num_workers = min(config.max_workers or 3, 5)
        async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=20) as client:
            workers = []
            for _ in range(num_workers):
                workers.append(asyncio.create_task(enrich_worker(client)))

            await job_queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        return stats
