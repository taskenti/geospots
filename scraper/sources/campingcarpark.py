"""CampingCar Park — Bulk API Gateway scraper + paginated reviews.

Phase 1: Fetch all location IDs from /api/v1/stay/locations/status (~906 spots),
          then download full details from /shop-api/locations/{locationId}.
Phase 2: Paginated review download from /shop-api/locations/{locationId}/reviews.

Coverage: Europe (France, Spain, Portugal, Belgium, Germany, etc.)
API Base: https://gateway.feature.campingcarpark.com
"""

import asyncio
import json
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_campingcarpark, merge_extra


GATEWAY_BASE = "https://gateway.feature.campingcarpark.com"
STATUS_URL = f"{GATEWAY_BASE}/api/v1/stay/locations/status"
DETAIL_URL = f"{GATEWAY_BASE}/shop-api/locations/{{location_id}}"
REVIEWS_URL = f"{GATEWAY_BASE}/shop-api/locations/{{location_id}}/reviews"

HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "accept": "application/json",
}

# CCP countryCode -> ISO2 lowercase
COUNTRY_MAP = {
    "FR": "fr", "ES": "es", "PT": "pt", "BE": "be", "DE": "de",
    "IT": "it", "NL": "nl", "CH": "ch", "AT": "at", "GB": "gb",
    "LU": "lu", "DK": "dk", "SE": "se", "NO": "no", "PL": "pl",
    "CZ": "cz", "HR": "hr", "SI": "si", "HU": "hu", "IE": "ie",
}


class CampingCarParkSource(AbstractSource):
    name = "campingcarpark"
    rate_limit = 0.1
    dedup_radius_m = 100.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("campingcarpark uses bulk API download, not grid cells")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw.get("latitude", 0))
            lon = float(raw.get("longitude", 0))
        except (ValueError, TypeError):
            return None

        source_id = str(raw.get("id", ""))
        if not source_id:
            return None

        nombre = (raw.get("publicName") or raw.get("name") or "").strip()
        if not nombre:
            nombre = f"CampingCar Park {source_id}"

        # Services
        services = raw.get("services", []) or []
        agua_potable = "water" in services
        electricidad = "electricity" in services or (raw.get("electricalOutletCount") or 0) > 0
        wifi = "wifi" in services
        vaciado = "drain" in services

        # Sanitary details
        sanitary = raw.get("sanitaryDetails") or {}
        sanitary_opening = raw.get("sanitaryOpening") or {}
        wc_count = (sanitary.get("WC") or 0) + (sanitary_opening.get("toiletCount") or 0)
        shower_count = (sanitary.get("shower") or 0) + (sanitary_opening.get("showerCount") or 0)
        wc_publico = wc_count > 0
        ducha = shower_count > 0

        # Prohibitions: solo asignar el flag si el campo está explícito en la respuesta.
        # Si el campo no viene, dejar None (desconocido) en lugar de defaultar a permitido.
        prohibitions = raw.get("prohibitions") or {}
        perros = (not prohibitions["dog"]) if "dog" in prohibitions else None
        acceso_grandes = (not prohibitions["vehicleMore9m"]) if "vehicleMore9m" in prohibitions else None

        # Price
        precio_aprox = None
        current_price = raw.get("currentPrice") or {}
        parking_price = current_price.get("allTaxesIncludedParkingPrice")
        if parking_price is not None:
            try:
                precio_aprox = float(parking_price)
            except (ValueError, TypeError):
                pass

        precio_info = None
        if precio_aprox is not None:
            precio_info = f"{precio_aprox:.2f} EUR"

        # Photos (max 8)
        images = raw.get("images") or []
        fotos_urls = []
        for img in images[:8]:
            url = img.get("mobileUrl") if isinstance(img, dict) else None
            if url and isinstance(url, str) and url.startswith("http"):
                fotos_urls.append(url)

        # Web URL
        link_url = raw.get("linkUrl") or ""
        web = f"https://www.campingcarpark.com{link_url}" if link_url else None

        # Country
        country_code = raw.get("countryCode") or ""
        country_iso = COUNTRY_MAP.get(country_code.upper(), country_code.lower() or None)

        # Description
        descripcion_fr = (raw.get("description") or "").strip() or None
        surroundings_fr = (raw.get("surroundingsDescription") or "").strip() or None
        if descripcion_fr and surroundings_fr:
            descripcion_fr = f"{descripcion_fr}\n\n{surroundings_fr}"
        elif surroundings_fr:
            descripcion_fr = surroundings_fr

        # Rating
        rating = None
        avg_rating = raw.get("averageRating")
        if avg_rating is not None:
            try:
                rating = float(avg_rating)
                if rating <= 0:
                    rating = None
            except (ValueError, TypeError):
                pass

        # Reviews count
        num_reviews = 0
        rev_num = raw.get("reviewsNumber")
        if rev_num is not None:
            try:
                num_reviews = int(rev_num)
            except (ValueError, TypeError):
                pass

        # Capacity
        num_plazas = None
        total_pitches = raw.get("totalPitchesNumber")
        if total_pitches is not None:
            try:
                num_plazas = int(total_pitches)
            except (ValueError, TypeError):
                pass

        norm = {
            "source_id": source_id,
            "nombre": nombre[:200],
            "lat": lat,
            "lon": lon,
            "tipo": "area_ac",
            "gratuito": False,
            "precio_aprox": precio_aprox,
            "precio_info": precio_info,
            "agua_potable": agua_potable,
            "electricidad": electricidad,
            "wifi": wifi,
            "vaciado_grises": vaciado,
            "vaciado_negras": vaciado,
            "wc_publico": wc_publico,
            "ducha": ducha,
            "perros": perros,
            "acceso_grandes": acceso_grandes,
            "num_plazas": num_plazas,
            "fotos_urls": fotos_urls,
            "web": web,
            "country_iso": country_iso,
            "region": raw.get("region"),
            "descripcion_fr": descripcion_fr,
            "rating_promedio": rating,
            "num_reviews": num_reviews,
        }
        return merge_extra(norm, extract_campingcarpark(raw))

    async def run(self, pool, config, log_id: int) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {},
        }

        # Step 1: Get all location IDs from status endpoint
        logger.info("[campingcarpark] Fetching location status list...")
        try:
            async with httpx.AsyncClient(
                headers=HEADERS, timeout=30, follow_redirects=True
            ) as client:
                resp = await client.get(STATUS_URL)
                resp.raise_for_status()
                status_list = resp.json()
        except Exception as e:
            logger.error(f"[campingcarpark] Failed to fetch status list: {e}")
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        location_ids = [item["locationId"] for item in status_list if isinstance(item, dict) and "locationId" in item]
        logger.info(f"[campingcarpark] {len(location_ids)} locations to process")

        # Step 2: Fetch details concurrently with semaphore
        sem = asyncio.Semaphore(5)
        processed = 0

        async def fetch_and_store(client: httpx.AsyncClient, loc_id: int):
            nonlocal processed
            async with sem:
                await asyncio.sleep(self.rate_limit)
                url = DETAIL_URL.format(location_id=loc_id)
                try:
                    resp = await client.get(url, timeout=15)
                    if resp.status_code == 429:
                        logger.warning(f"[campingcarpark] 429 on location {loc_id}. Sleeping 30s...")
                        await asyncio.sleep(30)
                        resp = await client.get(url, timeout=15)
                    if resp.status_code != 200:
                        logger.warning(f"[campingcarpark] Location {loc_id} returned {resp.status_code}")
                        stats["errores"] += 1
                        return
                    raw = resp.json()
                except Exception as e:
                    logger.error(f"[campingcarpark] Error fetching location {loc_id}: {e}")
                    stats["errores"] += 1
                    return

                norm = self.normalize(raw)
                if not norm:
                    return
                if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                    return

                sid = norm["source_id"]
                try:
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            existente = await find_spot_cercano(
                                conn, norm["lat"], norm["lon"],
                                self.dedup_radius_m,
                                nombre=norm.get("nombre"),
                                tipo=norm.get("tipo"),
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
                    logger.error(f"[campingcarpark] DB error for location {loc_id}: {e}")
                    stats["errores"] += 1

                processed += 1
                if processed % 50 == 0:
                    logger.info(
                        f"[campingcarpark] {processed}/{len(location_ids)} | "
                        f"new={stats['nuevos']} upd={stats['actualizados']} err={stats['errores']}"
                    )

        async with httpx.AsyncClient(
            headers=HEADERS, timeout=20, follow_redirects=True
        ) as client:
            BATCH = 50
            for i in range(0, len(location_ids), BATCH):
                batch_ids = location_ids[i:i + BATCH]
                await asyncio.gather(
                    *[fetch_and_store(client, lid) for lid in batch_ids],
                    return_exceptions=True,
                )
                logger.info(
                    f"[campingcarpark] Batch {i // BATCH + 1} done "
                    f"({min(i + BATCH, len(location_ids))}/{len(location_ids)})"
                )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[campingcarpark] Completed in {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config) -> dict:
        from db import upsert_review

        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}

        async with pool.acquire() as conn:
            jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id, sr.review_count, COALESCE(r.cnt, 0) as db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) as cnt
                    FROM reviews
                    WHERE source = 'campingcarpark'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'campingcarpark'
                  AND (sr.review_count > 0 AND COALESCE(r.cnt, 0) < sr.review_count)
                ORDER BY sr.review_count DESC;
            """)

        logger.info(f"[campingcarpark] {len(jobs)} spots pending review download.")
        if not jobs:
            return stats

        sem = asyncio.Semaphore(3)

        async def fetch_reviews_for_spot(client: httpx.AsyncClient, job: dict):
            spot_id = job["spot_id"]
            loc_id = job["source_id"]
            expected = job["review_count"] or 0
            already_have = job["db_review_count"] or 0

            async with sem:
                skip = 0
                page_size = 20
                total_inserted = 0

                while skip < expected:
                    await asyncio.sleep(self.rate_limit)
                    url = REVIEWS_URL.format(location_id=loc_id)
                    params = {"limit": page_size, "skip": skip}
                    try:
                        resp = await client.get(url, params=params, timeout=15)
                        if resp.status_code == 429:
                            logger.warning(f"[campingcarpark] Reviews 429 for loc {loc_id}. Sleeping 30s...")
                            await asyncio.sleep(30)
                            resp = await client.get(url, params=params, timeout=15)
                        if resp.status_code != 200:
                            logger.warning(f"[campingcarpark] Reviews {resp.status_code} for loc {loc_id}")
                            stats["errores"] += 1
                            break
                        reviews = resp.json()
                    except Exception as e:
                        logger.error(f"[campingcarpark] Error fetching reviews for loc {loc_id}: {e}")
                        stats["errores"] += 1
                        break

                    if not isinstance(reviews, list) or not reviews:
                        break

                    try:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                for rev in reviews:
                                    rev_id = rev.get("id")
                                    if not rev_id:
                                        continue

                                    # Parse date
                                    fecha = None
                                    created_str = rev.get("createdAt")
                                    if created_str:
                                        try:
                                            fecha = datetime.fromisoformat(created_str)
                                            if fecha.tzinfo is None:
                                                fecha = fecha.replace(tzinfo=timezone.utc)
                                        except Exception:
                                            try:
                                                fecha = datetime.strptime(
                                                    created_str[:19], "%Y-%m-%dT%H:%M:%S"
                                                ).replace(tzinfo=timezone.utc)
                                            except Exception:
                                                pass

                                    rating_val = None
                                    r_raw = rev.get("rating")
                                    if r_raw is not None:
                                        try:
                                            rating_val = float(r_raw)
                                        except (ValueError, TypeError):
                                            pass

                                    comment_text = (rev.get("comment") or "").strip() or None
                                    title = (rev.get("title") or "").strip()
                                    if title and comment_text:
                                        comment_text = f"{title}: {comment_text}"
                                    elif title:
                                        comment_text = title

                                    rev_dict = {
                                        "spot_id": spot_id,
                                        "source": self.name,
                                        "source_review_id": f"ccp_{rev_id}",
                                        "texto": comment_text,
                                        "rating": rating_val,
                                        "autor": rev.get("author") or "CampingCar Park User",
                                        "fecha": fecha,
                                        "idioma": rev.get("language") or "fr",
                                    }
                                    inserted = await upsert_review(conn, rev_dict)
                                    total_inserted += int(bool(inserted))
                    except Exception as e:
                        logger.error(f"[campingcarpark] DB error inserting reviews for loc {loc_id}: {e}")
                        stats["errores"] += 1

                    if len(reviews) < page_size:
                        break
                    skip += page_size

                stats["reviews_nuevas"] += total_inserted

        async with httpx.AsyncClient(
            headers=HEADERS, timeout=20, follow_redirects=True
        ) as client:
            await asyncio.gather(
                *[fetch_reviews_for_spot(client, dict(j)) for j in jobs],
                return_exceptions=True,
            )

        logger.info(f"[campingcarpark] Reviews download complete: {stats}")
        return stats
