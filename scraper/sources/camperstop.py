"""Camperstop — Scraper de la API oculta de Camperstop."""

import asyncio
from datetime import datetime, timezone
import json
from loguru import logger
import httpx

from sources.base import AbstractSource

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "culturecode": "en-GB",
    "content-type": "application/json",
    "user-agent": "Dart/3.11 (dart:io)",
}

def detect_language(text: str) -> str:
    if not text:
        return "en"
    text = text.lower()
    scores = {
        "es": sum(1 for w in [" el ", " la ", " con ", " para ", " jardín ", " tienda ", " bienvenidos "] if w in text),
        "fr": sum(1 for w in [" le ", " la ", " avec ", " pour ", " jardin ", " tente ", " bienvenue "] if w in text),
        "nl": sum(1 for w in [" het ", " een ", " met ", " voor ", " tuin ", " tent ", " welkom "] if w in text),
        "de": sum(1 for w in [" der ", " die ", " das ", " mit ", " garten ", " zelt ", " willkommen "] if w in text),
        "en": sum(1 for w in [" the ", " with ", " for ", " garden ", " tent ", " welcome ", " our "] if w in text)
    }
    max_lang = max(scores, key=scores.get)
    if scores[max_lang] == 0:
        return "en"
    return max_lang

class CamperstopSource(AbstractSource):
    name = "camperstop"
    rate_limit = 1.2  # Respeta el límite estricto de 60 req/min de la API
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Camperstop usa un dump masivo, no grid")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["latitude"])
            lon = float(raw["longitude"])
        except (KeyError, TypeError, ValueError):
            return None

        # Mapear tipos de Camperstop a canonical GeoSpots tipo
        # 1: Motorhome stopover -> area_ac
        # 2: Tolerated place -> wild
        # 3: Motorhome park -> area_ac
        # 4: Outside campsite -> area_ac
        # 5: Campsite -> camping
        # 6: At farm/vineyard -> area_ac
        # 7: At restaurant -> area_ac
        # 8: Overnight stay at company/enterprise -> area_ac
        # 9: At spa -> area_ac
        # 10: At zoo/museum/amusement parc -> parking
        # 11: Parking only -> parking
        # 12: Motorhome service -> area_ac
        # 13: At harbour/marina -> area_ac
        # 14: Camperstop on campsite -> camping
        # 15: Overnight stay in private area -> area_ac
        c_type_id = int(raw.get("camperStopTypeId") or 0)
        tipo = "otro"
        if c_type_id in [1, 3, 4, 6, 7, 8, 9, 12, 13, 15]:
            tipo = "area_ac"
        elif c_type_id in [5, 14]:
            tipo = "camping"
        elif c_type_id == 2:
            tipo = "wild"
        elif c_type_id in [10, 11]:
            tipo = "parking"

        # Amenities / Servicios
        agua_potable = bool(raw.get("waterAvailable"))
        vaciado_grises = bool(raw.get("drainageAvailable"))
        vaciado_negras = bool(raw.get("chemicalAvailable"))
        electricidad = bool(raw.get("powerAvailable"))
        wc_publico = bool(raw.get("toiletAvailable"))
        ducha = bool(raw.get("showerAvailable"))
        wifi = bool(raw.get("wifiAvailable"))

        # Extraer fotos
        images_raw = raw.get("images", [])
        fotos = [img.get("url") for img in images_raw if img.get("url")]

        # Enlace Web
        web = raw.get("contactWebsite", "").strip()
        if web and not web.startswith("http"):
            web = "http://" + web
        if not web:
            web = "https://www.camperstop.com"

        # Ratings
        average_score = raw.get("averageScore")
        rating_promedio = float(average_score) / 2.0 if average_score is not None else None
        num_reviews = int(raw.get("totalReviews") or 0)

        # Precio
        gratuito = False
        rate_str = str(raw.get("camperRate", "")).lower()
        if "free" in rate_str or "gratis" in rate_str or "0" in rate_str:
            gratuito = True

        return {
            "source_id": str(raw.get("id")),
            "nombre": raw.get("name", "Camperstop").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "country_iso": raw.get("countryCode", "")[:2],
            "agua_potable": agua_potable,
            "wc_publico": wc_publico,
            "ducha": ducha,
            "electricidad": electricidad,
            "vaciado_negras": vaciado_negras,
            "vaciado_grises": vaciado_grises,
            "wifi": wifi,
            "fotos_urls": fotos[:5],
            "web": web,
            "rating_promedio": rating_promedio,
            "num_reviews": num_reviews
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

        url = "https://camperstopapi.trone-live.nl/api/public/camperstops/getcamperstops"
        payload = {"latLng": "41.17129,-2.4313"}  # Medinaceli de referencia

        async with httpx.AsyncClient(headers=HEADERS) as client:
            try:
                logger.info(f"[CAMPERSTOP] Descargando dump completo desde {url}...")
                resp = await client.post(url, json=payload, timeout=60)
                # La API responde con código HTTP 256 o 200 en caso de éxito
                if resp.status_code not in (200, 256):
                    resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"[CAMPERSTOP] Error descargando el dump: {e}")
                stats["errores"] += 1
                data = []

            if data and isinstance(data, list):
                logger.info(f"[CAMPERSTOP] Descarga completada: {len(data)} spots obtenidos. Procesando...")
                
                for raw in data:
                    norm = self.normalize(raw)
                    if not norm:
                        continue

                    sid = norm["source_id"]
                    try:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                existente = await find_spot_cercano(
                                    conn, norm["lat"], norm["lon"], self.dedup_radius_m,
                                    nombre=norm.get("nombre"), tipo=norm.get("tipo")
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
                        logger.error(f"[CAMPERSTOP] Error spot '{norm.get('nombre')}': {e}")
                        stats["errores"] += 1
            else:
                logger.warning("[CAMPERSTOP] No se obtuvieron datos o el formato es incorrecto.")

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[CAMPERSTOP] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config) -> dict:
        stats = {
            "nuevos": 0,
            "actualizados": 0,
            "reviews_nuevas": 0,
            "errores": 0
        }

        logger.info(f"[{self.name}] Buscando spots con reviews pendientes...")
        async with pool.acquire() as conn:
            review_jobs = await conn.fetch("""
                SELECT 
                    sr.spot_id, 
                    sr.source_id, 
                    sr.review_count,
                    COALESCE(r.cnt, 0) AS db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) AS cnt
                    FROM reviews
                    WHERE source = 'camperstop'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'camperstop'
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
            from db import upsert_review
            while not job_queue.empty():
                try:
                    job = job_queue.get_nowait()
                except (asyncio.QueueEmpty, asyncio.CancelledError):
                    break

                spot_id = job["spot_id"]
                sid = job["source_id"]

                try:
                    await asyncio.sleep(self.rate_limit)
                    url = f"https://camperstopapi.trone-live.nl/api/public/camperstops/getreviews/{sid}/en-GB"
                    resp = await client.get(url, timeout=20)
                    if resp.status_code not in (200, 256):
                        resp.raise_for_status()
                    ratings = resp.json()
                    
                    saved = 0
                    if ratings and isinstance(ratings, list):
                        async with pool.acquire() as conn:
                            for item in ratings:
                                r_id = item.get("id")
                                r_text = (item.get("description") or "").strip()
                                r_rating = item.get("rating")
                                r_author = item.get("userName")
                                r_created = item.get("created")
                                
                                if not r_id or not r_text:
                                    continue
                                    
                                fecha = None
                                if r_created:
                                    try:
                                        fecha = datetime.strptime(r_created, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                                    except Exception:
                                        pass
                                        
                                r_lang = detect_language(r_text)
                                
                                rating_val = None
                                if r_rating is not None:
                                    try:
                                        rating_val = float(r_rating) / 2.0
                                    except (ValueError, TypeError):
                                        pass

                                review_dict = {
                                    "spot_id": spot_id,
                                    "source": "camperstop",
                                    "source_review_id": str(r_id),
                                    "texto": r_text,
                                    "rating": rating_val,
                                    "autor": r_author,
                                    "fecha": fecha,
                                    "idioma": r_lang
                                }
                                
                                await upsert_review(conn, review_dict)
                                saved += 1
                                
                            if saved > 0:
                                await conn.execute("""
                                    UPDATE spots SET total_reviews = (
                                        SELECT COUNT(*) FROM reviews WHERE spot_id = $1
                                    ) WHERE id = $1
                                """, spot_id)
                                
                    stats["reviews_nuevas"] += saved
                    
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE source_records
                            SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb
                            WHERE source = 'camperstop' AND source_id = $1
                        """, sid)
                    stats["actualizados"] += 1
                except Exception as e:
                    logger.error(f"[{self.name}] Error descargando reviews para spot {sid}: {e}")
                    stats["errores"] += 1

                job_queue.task_done()

        async with httpx.AsyncClient(headers=HEADERS) as client:
            workers = [asyncio.create_task(worker(client)) for _ in range(1)]
            await asyncio.gather(*workers)

        return stats
