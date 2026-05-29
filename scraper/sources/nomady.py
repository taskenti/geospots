"""Nomady — scraper de la API oculta de puntos comprimidos."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_nomady, merge_extra

# Esta es la URL mágica que se descarga TODA la base de datos de Nomady de un tirón.
BASE_URL = "https://api.nomady.camp/cabin/public-compressed-v2"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://nomady.camp",
    "referer": "https://nomady.camp/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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

        # Generar enlace web (normalmente es /en/cabin/slug)
        web = f"https://nomady.camp/en/cabin/{slug}" if slug else "https://nomady.camp"

        rating_promedio = raw.get("averageRating")
        num_reviews = raw.get("numberOfRatings")

        norm = {
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
            "web": web,
            "rating_promedio": float(rating_promedio) if rating_promedio is not None else None,
            "num_reviews": int(num_reviews) if num_reviews is not None else 0
        }
        return merge_extra(norm, extract_nomady(raw))

    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
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
                
                for idx, raw in enumerate(data, 1):
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

                    if idx % 200 == 0:
                        await self.update_job_progress(pool, job_id, idx, len(data), stats)

            else:
                logger.warning("[NOMADY] No se obtuvieron datos o el formato es incorrecto.")

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[NOMADY] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        import httpx
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
                    WHERE source = 'nomady'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'nomady'
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
                    url = f"https://api.nomady.camp/ratings/cabin/{sid}"
                    resp = await client.get(url, timeout=20)
                    resp.raise_for_status()
                    ratings = resp.json()
                    
                    saved = 0
                    if ratings and isinstance(ratings, list):
                        async with pool.acquire() as conn:
                            for item in ratings:
                                r_user = item.get("userId")
                                r_date = item.get("bookingEndDate")
                                r_text = (item.get("text") or "").strip() or (item.get("textOriginal") or "").strip()
                                r_rating = item.get("numericRating")
                                r_author = item.get("userFirstName")
                                
                                if not r_user or not r_date:
                                    continue
                                    
                                r_id = f"{sid}_{r_user}_{r_date}"
                                
                                fecha = None
                                if r_date:
                                    try:
                                        fecha = datetime.fromisoformat(r_date.replace("Z", "+00:00"))
                                    except Exception:
                                        pass
                                        
                                r_lang = item.get("textOriginalLanguage") or detect_language(r_text)
                                
                                review_dict = {
                                    "spot_id": spot_id,
                                    "source": "nomady",
                                    "source_review_id": r_id,
                                    "texto": r_text,
                                    "rating": float(r_rating) if r_rating is not None else None,
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
                            WHERE source = 'nomady' AND source_id = $1
                        """, sid)
                    stats["actualizados"] += 1
                except Exception as e:
                    logger.error(f"[{self.name}] Error descargando reviews para spot {sid}: {e}")
                    stats["errores"] += 1

                job_queue.task_done()

        async with httpx.AsyncClient(headers=HEADERS) as client:
            workers = [asyncio.create_task(worker(client)) for _ in range(3)]
            await asyncio.gather(*workers)

        return stats
