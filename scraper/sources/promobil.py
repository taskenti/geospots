"""Promobil source implementation for GeoSpots scraper."""

import asyncio
import re as _re
import unicodedata
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource


def _slugify(text: str) -> str:
    """Convert a spot name to a URL-safe slug (German-aware)."""
    replacements = {
        '\u00e4': 'ae', '\u00f6': 'oe', '\u00fc': 'ue',
        '\u00c4': 'ae', '\u00d6': 'oe', '\u00dc': 'ue', '\u00df': 'ss',
    }
    for char, repl in replacements.items():
        text = text.replace(char, repl)
    text = unicodedata.normalize('NFKD', text)
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = _re.sub(r'[^\w\s-]', '', text.lower())
    text = _re.sub(r'[-\s]+', '-', text).strip('-')
    return text

HEADERS = {
    "accept": "*/*",
    "accept-language": "es-ES,es;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36"
    ),
    "referer": "https://www.promobil.de/",
}


def safe_get_dict(d, key) -> dict:
    if not isinstance(d, dict):
        return {}
    val = d.get(key)
    return val if isinstance(val, dict) else {}


class PromobilSource(AbstractSource):
    name = "promobil"
    rate_limit = 0.5
    grid_step = 1.0
    dedup_radius_m = 60.0
    HEADERS = HEADERS

    async def generate_active_grid(self, pool, step=1.0, buffer=4):
        cells = await super().generate_active_grid(pool, step=step, buffer=buffer)
        eu = self.EU_BOUNDS
        filtered = [
            c for c in cells
            if c[2] <= eu["lat_max"] and c[0] >= eu["lat_min"] and c[1] <= eu["lon_max"] and c[3] >= eu["lon_min"]
        ]
        logger.info(f"[{self.name}] Grid reducido de {len(cells)} a {len(filtered)} celdas en Europa.")
        return filtered

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        url = "https://page-api.promobil.de/pro-pitch/pitch/getListData"
        # calculate center
        lat = (tl_lat + br_lat) / 2.0
        lon = (tl_lon + br_lon) / 2.0
        
        all_items = []
        p = 0
        max_pages = 30
        
        while p < max_pages:
            params = {
                "distance": "100",
                "lng": str(lon),
                "lat": str(lat),
                "pitchType[]": ["Campingplatz", "Stellplatz", "Privater Platz"],
                "p": str(p)
            }
            
            resp = None
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    resp = await client.get(url, params=params, timeout=20)
                    if resp.status_code == 200:
                        break
                    logger.warning(f"[{self.name}] HTTP {resp.status_code} at page {p} (attempt {attempt}/{max_retries})")
                    if resp.status_code == 429:
                        await asyncio.sleep(5 * attempt)
                    else:
                        await asyncio.sleep(2 * attempt)
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    logger.warning(f"[{self.name}] Error fetching page {p} (attempt {attempt}/{max_retries}): {repr(e)}")
                    if attempt == max_retries:
                        break
                    await asyncio.sleep(2 * attempt)
            
            if resp is None or resp.status_code != 200:
                logger.error(f"[{self.name}] Failed to fetch page {p} after {max_retries} attempts.")
                break
                
            try:
                data = resp.json()
            except Exception as e:
                logger.error(f"[{self.name}] Error parsing JSON at page {p}: {repr(e)}")
                break
            
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("list") or []
                
            if not items:
                break
                
            all_items.extend(items)
            p += 1
            await asyncio.sleep(self.rate_limit)
            
        return all_items

    def normalize(self, raw: dict) -> dict | None:
        if not isinstance(raw, dict):
            return None
        if "_de" in raw and not isinstance(raw["_de"], dict):
            raw = dict(raw)
            raw["_de"] = {}
        # Check source_id
        source_id = str(raw.get("id") or raw.get("_id") or "")
        if not source_id:
            return None
            
        # Parse coordinates
        try:
            gps = raw.get("gps")
            if gps and isinstance(gps, list) and len(gps) >= 2:
                lat = float(gps[0])
                lon = float(gps[1])
            else:
                lat = float(raw["latitude"])
                lon = float(raw["longitude"])
        except (KeyError, ValueError, TypeError):
            return None
            
        # Check valid bounds
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
            
        # Name
        nombre = None
        de_dict = safe_get_dict(raw, "_de")
        if de_dict.get("name"):
            nombre = de_dict["name"]
        elif raw.get("name"):
            nombre = raw["name"]
        else:
            nombre = "Stellplatz"
        nombre = nombre.strip()[:200]
        
        # Type
        pitch_type = raw.get("pitchType")
        if pitch_type == "Stellplatz":
            tipo = "area_ac"
        elif pitch_type == "Campingplatz":
            tipo = "camping"
        elif pitch_type == "Privater Platz":
            tipo = "naturaleza"
        else:
            tipo = "otro"
            
        # Price extraction
        precio_aprox = None
        costs = safe_get_dict(raw, "costs")
        
        # mainSeason price
        main_season = safe_get_dict(costs, "mainSeason")
        price = main_season.get("price")
        if price is not None:
            try:
                precio_aprox = float(price)
            except (ValueError, TypeError):
                pass
                
        # Fallback to adultsCombined
        if precio_aprox is None and costs.get("adultsCombined") is not None:
            try:
                precio_aprox = float(costs.get("adultsCombined"))
            except (ValueError, TypeError):
                pass
                
        # Fallback to sideSeason
        if precio_aprox is None:
            side_season = safe_get_dict(costs, "sideSeason")
            side_price = side_season.get("price")
            if side_price is not None:
                try:
                    precio_aprox = float(side_price)
                except (ValueError, TypeError):
                    pass
                    
        # Gratuito logic
        gratuito = None
        if precio_aprox is not None:
            gratuito = (precio_aprox == 0.0)
            
        # Price Info
        precio_info = None
        currency = raw.get("currency") or main_season.get("currency") or "EUR"
        if precio_aprox is not None:
            if gratuito:
                precio_info = "Gratuito"
            else:
                precio_info = f"{precio_aprox:.2f} {currency}"
                
        # Services
        perros = None
        pets = safe_get_dict(raw, "pets")
        if pets.get("allowed") is not None:
            perros = bool(pets.get("allowed"))
            
        wifi = None
        wifi_data = safe_get_dict(raw, "wifi")
        if wifi_data.get("exists") is not None:
            wifi = bool(wifi_data.get("exists"))
            
        ducha = None
        shower = safe_get_dict(raw, "shower")
        if shower.get("exists") is not None:
            ducha = bool(shower.get("exists"))
            
        wc_publico = None
        wc = safe_get_dict(raw, "wc")
        if wc.get("exists") is not None:
            wc_publico = bool(wc.get("exists"))
            
        electricidad = None
        power = safe_get_dict(raw, "powerSupply")
        if power.get("exists") is not None:
            electricidad = bool(power.get("exists"))
            
        agua_potable = None
        water = safe_get_dict(raw, "waterSupply")
        if water.get("exists") is not None:
            agua_potable = bool(water.get("exists"))
            
        vaciado_negras = None
        disp_chem = safe_get_dict(raw, "disposalChemical")
        if disp_chem.get("exists") is not None:
            vaciado_negras = bool(disp_chem.get("exists"))
            
        vaciado_grises = None
        disp = safe_get_dict(raw, "disposal")
        if disp.get("exists") is not None:
            vaciado_grises = bool(disp.get("exists"))
            
        # Additional fields
        num_plazas = None
        if raw.get("pitchCount") is not None:
            try:
                num_plazas = int(raw["pitchCount"])
            except (ValueError, TypeError):
                pass
                
        acceso_grandes = None
        if raw.get("caravan8Meters") is not None:
            acceso_grandes = bool(raw["caravan8Meters"])
            
        region = de_dict.get("region") or raw.get("federalState")
        if region:
            region = region.strip()[:100]
            
        country_iso = (raw.get("country") or "").strip().upper()[:2] or None
        
        # Web / website
        web = raw.get("website")
        if web:
            web = web.strip()
            if not web.startswith(("http://", "https://")):
                web = "https://" + web
            web = web[:500]
            
        # Contact info
        telefono = raw.get("telephone") or raw.get("mobile")
        if telefono:
            telefono = telefono.strip()[:50]
            
        email = raw.get("email")
        if email:
            email = email.strip()[:100]
            
        # Ratings
        rating_promedio = None
        num_reviews = 0
        user_rating = safe_get_dict(raw, "userRating")
        if user_rating.get("avg") is not None:
            try:
                rating_promedio = float(user_rating["avg"])
            except (ValueError, TypeError):
                pass
        if user_rating.get("count") is not None:
            try:
                num_reviews = int(user_rating["count"])
            except (ValueError, TypeError):
                pass
                
        # Photos extraction
        fotos_urls = []
        gps_image = safe_get_dict(raw, "gpsImage")
        fileurl = safe_get_dict(gps_image, "fileurl")
        for key in ["orig", "detail", "big", "medium", "websiteArticle", "websiteArticleNew"]:
            img_url = fileurl.get(key)
            if img_url:
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                if img_url not in fotos_urls:
                    fotos_urls.append(img_url)
                    
        for img_obj in raw.get("images") or []:
            if not isinstance(img_obj, dict):
                continue
            f_url = safe_get_dict(img_obj, "fileurl")
            for key in ["orig", "detail", "big", "medium", "websiteArticle", "websiteArticleNew"]:
                img_url = f_url.get(key)
                if img_url:
                    if img_url.startswith("//"):
                        img_url = "https:" + img_url
                    if img_url not in fotos_urls:
                        fotos_urls.append(img_url)
                        
        fotos_urls = fotos_urls[:8]
        
        # Description
        descripcion_de = None
        if de_dict.get("description"):
            descripcion_de = de_dict["description"]
        elif raw.get("generatedDescription"):
            descripcion_de = raw["generatedDescription"]
        if descripcion_de:
            descripcion_de = descripcion_de.strip()[:2000]
            
        # Build page URL for reviews fetching
        slug = _slugify(nombre)
        page_url = f"stellplatz/{slug}-{source_id}.html"

        return {
            "source_id":       source_id,
            "nombre":          nombre,
            "lat":             lat,
            "lon":             lon,
            "tipo":            tipo,
            "gratuito":        gratuito,
            "precio_info":     precio_info,
            "precio_aprox":    precio_aprox,
            "agua_potable":    agua_potable,
            "vaciado_negras":  vaciado_negras,
            "vaciado_grises":  vaciado_grises,
            "electricidad":    electricidad,
            "ducha":           ducha,
            "wifi":            wifi,
            "wc_publico":      wc_publico,
            "perros":          perros,
            "acceso_grandes":  acceso_grandes,
            "num_plazas":      num_plazas,
            "region":          region,
            "country_iso":     country_iso,
            "web":             web,
            "telefono":        telefono,
            "email":           email,
            "rating_promedio": rating_promedio,
            "num_reviews":     num_reviews,
            "fotos_urls":      fotos_urls,
            "descripcion_de":  descripcion_de,
            "page_url":        page_url,
        }

    async def download_reviews(self, pool, config) -> dict:
        import re
        import json

        NEXT_DATA_RE = re.compile(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            re.DOTALL
        )
        BASE_URL = "https://www.promobil.de"

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
                    sr.normalized_data->>'nombre'   AS nombre,
                    sr.normalized_data->>'page_url' AS page_url,
                    COALESCE(r.cnt, 0)              AS db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) AS cnt
                    FROM reviews
                    WHERE source = 'promobil'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'promobil'
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
        for row in review_jobs:
            job = dict(row)
            # Build page_url from normalized_data if not stored, or derive from name+id
            if not job.get("page_url"):
                nombre = job.get("nombre") or "stellplatz"
                slug = _slugify(nombre)
                job["page_url"] = f"stellplatz/{slug}-{job['source_id']}.html"
            await job_queue.put(job)

        async def worker(client):
            while not job_queue.empty():
                try:
                    job = job_queue.get_nowait()
                except (asyncio.QueueEmpty, asyncio.CancelledError):
                    break

                spot_id = job["spot_id"]
                sid = job["source_id"]
                page_url = job["page_url"]

                # The reviews page is: {BASE_URL}/{page_url}/bewertungen/
                bew_url = f"{BASE_URL}/{page_url}/bewertungen/"

                try:
                    await asyncio.sleep(self.rate_limit)
                    resp = await client.get(bew_url)

                    reviews_found = []
                    if resp.status_code == 200:
                        m = NEXT_DATA_RE.search(resp.text)
                        if m:
                            try:
                                nd = json.loads(m.group(1))
                                mobile = (
                                    nd.get("props", {})
                                      .get("pageProps", {})
                                      .get("pageData", {})
                                      .get("data", {})
                                      .get("mobile", [])
                                )
                                for elem in mobile:
                                    # Use pitch.ratings (full list page) not pitch.recentratings
                                    if elem.get("element") == "pitch.ratings":
                                        reviews_found = elem.get("data") or []
                                        break
                            except Exception as je:
                                logger.warning(f"[{self.name}] JSON parse error spot {sid}: {je}")
                    elif resp.status_code == 404:
                        # Slug mismatch - mark as fetched with 0 reviews to skip next time
                        logger.warning(f"[{self.name}] 404 for {bew_url} (slug mismatch?) - skipping")
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                UPDATE source_records
                                SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb
                                WHERE source = $1 AND source_id = $2
                            """, self.name, sid)
                        job_queue.task_done()
                        continue
                    else:
                        logger.warning(f"[{self.name}] HTTP {resp.status_code} for {bew_url}")
                        stats["errores"] += 1
                        job_queue.task_done()
                        continue

                    inserted = 0
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            for rev in reviews_found:
                                rev_id = rev.get("_id")
                                if not rev_id:
                                    continue

                                fecha = None
                                fecha_str = rev.get("date")
                                if fecha_str and len(fecha_str) >= 10:
                                    try:
                                        fecha = datetime.strptime(fecha_str[:10], "%Y-%m-%d").date()
                                    except Exception:
                                        pass

                                text_val = rev.get("displayText") or ""

                                rating_val = None
                                r_obj = rev.get("rating")
                                if isinstance(r_obj, dict) and r_obj.get("avg") is not None:
                                    try:
                                        rating_val = float(r_obj["avg"])
                                    except (ValueError, TypeError):
                                        pass
                                if rating_val is None and rev.get("rated") is not None:
                                    try:
                                        rating_val = float(rev["rated"])
                                    except (ValueError, TypeError):
                                        pass

                                author_val = None
                                cb = rev.get("_createdBy")
                                if isinstance(cb, dict):
                                    author_val = cb.get("username")

                                lang_val = rev.get("language")

                                from db import upsert_review

                                was_inserted = await upsert_review(conn, {
                                    "spot_id": spot_id,
                                    "source": self.name,
                                    "source_review_id": f"promobil_{rev_id}",
                                    "texto": text_val,
                                    "texto_original": text_val,
                                    "rating": rating_val,
                                    "autor": author_val,
                                    "fecha": fecha,
                                    "idioma": lang_val,
                                })
                                inserted += int(bool(was_inserted))

                            await conn.execute("""
                                UPDATE source_records
                                SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb,
                                    last_seen = NOW()
                                WHERE source = $1 AND source_id = $2
                            """, self.name, sid)

                    stats["reviews_nuevas"] += inserted
                    stats["actualizados"] += 1
                    logger.info(
                        f"[{self.name}] {sid}: {inserted} reviews de {job['review_count']} esperadas"
                    )

                except Exception as e:
                    logger.error(f"[{self.name}] Error spot {sid}: {e}")
                    stats["errores"] += 1
                finally:
                    job_queue.task_done()

        num_workers = min(config.max_workers or 3, 5)
        html_headers = {
            **self.HEADERS,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with httpx.AsyncClient(headers=html_headers, follow_redirects=True, timeout=30) as client:
            workers = [asyncio.create_task(worker(client)) for _ in range(num_workers)]
            await job_queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        return stats
