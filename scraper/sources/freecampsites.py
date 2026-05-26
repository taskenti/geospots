"""FreeCampsites.net — Scraper based on Mobile theme API & WordPress JSON API."""

import asyncio
import json
import re
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

class FreeCampsitesSource(AbstractSource):
    """FreeCampsites.net: imports campsites from radius search API and downloads reviews from wp-json."""

    name = "freecampsites"
    rate_limit = 2.5
    grid_step = 1.0
    dedup_radius_m = 150.0

    # Mapeo nombre país (en raw_data.country) -> ISO2 lowercase. La fuente
    # cubre principalmente Norteamérica + algunos países LATAM.
    COUNTRY_NAME_TO_ISO = {
        "united states": "us", "usa": "us", "u.s.a.": "us", "u.s.": "us",
        "canada": "ca",
        "mexico": "mx", "méxico": "mx",
        "belize": "bz", "guatemala": "gt", "honduras": "hn", "nicaragua": "ni",
        "costa rica": "cr", "panama": "pa", "panamá": "pa",
        "puerto rico": "pr", "bahamas": "bs", "cuba": "cu",
    }

    HEADERS = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36",
        "referer": "https://freecampsites.net/"
    }

    async def generate_active_grid(self, pool, step=1.0, buffer=4):
        """Genera celdas filtradas por los límites de Norteamérica para optimizar el scraper."""
        cells = await super().generate_active_grid(pool, step=step, buffer=buffer)
        
        # Filtrar a celdas que caen dentro de Norteamérica:
        # latitud entre 24.0 y 72.0, longitud entre -170.0 y -50.0
        na_cells = []
        for c in cells:
            tl_lat, tl_lon, br_lat, br_lon = c
            lat = (tl_lat + br_lat) / 2.0
            lon = (tl_lon + br_lon) / 2.0
            if 24.0 <= lat <= 72.0 and -170.0 <= lon <= -50.0:
                na_cells.append(c)
                
        logger.info(f"[freecampsites] Filtro de bounds aplicado (Norteamérica): de {len(cells)} celdas iniciales a {len(na_cells)} celdas filtradas.")
        return na_cells

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        """Query freecampsites mobile endpoint using the cell center coordinates with retry on 429/errors."""
        lat = round((tl_lat + br_lat) / 2.0, 5)
        lon = round((tl_lon + br_lon) / 2.0, 5)
        
        url = f"https://freecampsites.net/wp-content/themes/freecampsites/androidApp.php?location=({lat},{lon})&coordinates=({lat},{lon})&advancedSearch={{}}"
        
        retries = 3
        backoff = 5.0
        for attempt in range(retries):
            try:
                r = await client.get(url, headers=self.HEADERS, timeout=20)
                if r.status_code == 429:
                    wait_time = backoff * (2 ** attempt)
                    logger.warning(f"[freecampsites] HTTP 429 (Rate Limit) at ({lat}, {lon}). Retrying in {wait_time}s... (attempt {attempt+1}/{retries})")
                    await asyncio.sleep(wait_time)
                    continue
                    
                if r.status_code != 200:
                    logger.warning(f"[freecampsites] HTTP error {r.status_code} at ({lat}, {lon})")
                    return []
                    
                # Extract JSON payload from text response
                match = re.search(r'\{.*\}', r.text, re.DOTALL)
                if not match:
                    return []
                    
                data = json.loads(match.group(0))
                return data.get("resultList", [])
            except Exception as e:
                if attempt < retries - 1:
                    wait_time = backoff * (2 ** attempt)
                    logger.warning(f"[freecampsites] Error querying ({lat}, {lon}): {e}. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.warning(f"[freecampsites] Error querying ({lat}, {lon}) after {retries} attempts: {e}")
                    return []
        return []

    def clean_html(self, text: str) -> str:
        if not text:
            return ""
        # Strip HTML tags
        clean = re.sub(r'<[^>]+>', ' ', text)
        clean = clean.replace('\xa0', ' ')
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean

    def normalize(self, raw: dict) -> dict | None:
        """Normalize freecampsites JSON result item."""
        campsite_id = raw.get("id")
        if not campsite_id:
            return None

        nombre = (raw.get("name") or "Campsite").strip()[:200]
        lat = raw.get("latitude")
        lon = raw.get("longitude")
        if lat is None or lon is None:
            return None
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None

        # Tipo: priorizar keywords del NOMBRE (señal más fuerte que el icon color),
        # luego icon (green = wild = free camping en la convención FC), luego default.
        # Tipos legacy "parking" sustituidos por "parking_publico".
        icon = (raw.get("type_specific") or {}).get("icon", "")
        name_lower = nombre.lower()
        excerpt_lower = (raw.get("excerpt") or "").lower()

        if any(kw in name_lower for kw in ["parking", "lot", "aparcamiento", "sosta"]):
            tipo = "parking_publico"
        elif any(kw in name_lower for kw in ["rv park", "resort", "campground", "camping"]):
            tipo = "camping"
        elif "green" in icon.lower():
            tipo = "wild"  # FC marca free camping con tent-green icon
        else:
            tipo = "camping"

        # Excerpt (plain text)
        desc = self.clean_html(raw.get("excerpt") or "")
        desc_lower = desc.lower()

        # Fee — direct from API field
        ts = raw.get("type_specific") or {}
        fee_type = (ts.get("fee") or "").lower()
        gratuito = None
        if fee_type == "free":
            gratuito = True
        elif fee_type in ("fee", "paid", "$"):
            gratuito = False

        # Rating: escala 0-5 en API, convertimos a 0-10 (convención GeoSpots multi-fuente)
        rating_promedio = None
        ra = raw.get("ratings_average")
        if ra is not None:
            try:
                v = float(ra)
                if v > 0:
                    rating_promedio = round(v * 2, 2)
            except (TypeError, ValueError):
                pass

        # Num reviews
        num_reviews = 0
        rc = raw.get("ratings_count")
        if rc is not None:
            try:
                num_reviews = int(rc)
            except (TypeError, ValueError):
                pass

        # País desde el campo raw "country" (texto libre - mapeo)
        country_raw = (raw.get("country") or "").lower().strip()
        country_iso = self.COUNTRY_NAME_TO_ISO.get(country_raw)

        # Region (state/province) y city
        region = raw.get("region") or raw.get("county") or raw.get("city") or None

        def _kw(keywords):
            return True if any(k in desc_lower for k in keywords) else None

        res = {
            "source_id": str(campsite_id),
            "nombre": nombre,
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "country_iso": country_iso,
            "region": region,
            "descripcion_en": desc if desc else None,
            "rating_promedio": rating_promedio,
            "num_reviews": num_reviews,
            "gratuito": gratuito,
            "agua_potable": _kw(["water", "potable", "drinking"]),
            "electricidad": _kw(["electric", "electricity", "hookup", "amp"]),
            "wifi": _kw(["wifi", "wi-fi", "internet"]),
            "ducha": _kw(["shower", "showers"]),
            "wc_publico": _kw(["toilet", "restroom", "latrine", "outhouse", "privy"]),
            "perros": _kw(["dog", "dogs", "pet", "pets", "leash"]),
            "acceso_grandes": _kw(["rv", "motorhome", "big rig", "slide-out"]),
            "web": raw.get("url"),
        }
        return res

    async def download_reviews(self, pool, config) -> dict:
        """Download reviews using the public WordPress JSON API comments endpoint."""
        inicio = datetime.now(timezone.utc)
        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}
        
        # Query target spots belonging to freecampsites
        async with pool.acquire() as conn:
            spots = await conn.fetch("""
                SELECT spot_id, source_id FROM source_records 
                WHERE source = 'freecampsites'
            """)
            
        if not spots:
            logger.info("[freecampsites] No spots found in DB to retrieve reviews for.")
            return stats
            
        logger.info(f"[freecampsites] Fetching reviews for {len(spots)} spots...")
        
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        from db import upsert_review
        
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            for idx, spot in enumerate(spots):
                spot_id = spot["spot_id"]
                source_id = spot["source_id"]
                
                url = f"https://freecampsites.net/wp-json/wp/v2/comments?post={source_id}&per_page=100"
                
                try:
                    await asyncio.sleep(0.5)  # respectful delay
                    r = await client.get(url, timeout=15)
                    if r.status_code != 200:
                        continue
                        
                    comments = r.json()
                    if not comments:
                        continue
                        
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            for comment in comments:
                                raw_text = comment.get("content", {}).get("rendered", "")
                                clean_text = self.clean_html(raw_text)
                                if not clean_text:
                                    continue
                                    
                                date_str = comment.get("date", "")
                                fecha = None
                                if date_str:
                                    try:
                                        fecha = datetime.fromisoformat(date_str).date()
                                    except Exception:
                                        pass
                                        
                                review_dict = {
                                    "spot_id": spot_id,
                                    "source": self.name,
                                    "source_review_id": str(comment["id"]),
                                    "texto": clean_text,
                                    "texto_original": clean_text,
                                    "rating": None,
                                    "autor": comment.get("author_name", "Anonymous"),
                                    "fecha": fecha,
                                    "idioma": "en"
                                }
                                
                                inserted = await upsert_review(conn, review_dict)
                                if inserted:
                                    stats["reviews_nuevas"] += 1
                except Exception as e:
                    logger.warning(f"[freecampsites] Error fetching reviews for spot {spot_id} (ID: {source_id}): {e}")
                    stats["errores"] += 1
                    
                if (idx + 1) % 50 == 0:
                    logger.info(f"[freecampsites] Ingestion: {idx+1}/{len(spots)} spots processed | new_reviews={stats['reviews_nuevas']}")
                    
        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[freecampsites] Reviews ingestion completed in {dur:.0f}s: {stats}")
        return stats
