"""Roadsurfer Spots — scraper con búsqueda radial global."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx
import re

from sources.base import AbstractSource
from sources._normalize_helpers import extract_roadsurfer, merge_extra

BASE_URL = "https://spots.roadsurfer.com/en_GB/search/spot"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://spots.roadsurfer.com",
    "referer": "https://spots.roadsurfer.com/en-gb/roadsurfer-spots",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def clean_surrogates(text: str) -> str:
    if not text:
        return ""
    return "".join(c for c in text if not (0xD800 <= ord(c) <= 0xDFFF))

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

def infer_large_vehicles(text: str) -> bool | None:
    if not text:
        return None
    text = text.lower()
    tent_only_keywords = [
        "tent only", "tents only", "only tents", "only for tents", "no campers", "no motorhomes", "no caravans", "no rvs", "no vans", "no cars", "no vehicles",
        "solo tiendas", "sólo tiendas", "solo tienda", "sólo tienda", "no furgonetas", "no autocaravanas", "no caravanas", "no vehículos",
        "uniquement tentes", "tentes uniquement", "pas de camping-car", "pas de caravane", "pas de véhicule",
        "alleen tenten", "geen campers", "geen caravans", "geen voertuigen",
        "nur zelte", "nur für zelte", "keine wohnmobile", "keine wohnwagen", "keine fahrzeuge"
    ]
    vehicle_allowed_keywords = [
        "camper allowed", "campers allowed", "vans allowed", "van allowed", "motorhome allowed", "motorhomes allowed", "rv allowed", "rvs allowed", "vehicles allowed", "vehicle allowed", "camper van", "campervan", "motorhome ok", "camper ok",
        "se aceptan campers", "se admiten campers", "furgonetas bienvenidas", "se aceptan furgonetas", "autocaravanas bienvenidas", "se aceptan autocaravanas",
        "camping-cars bienvenus", "camping-car accepté", "vans acceptés", "fourgon accepté",
        "campers welkom", "campers toegestaan", "busjes welkom",
        "wohnmobile willkommen", "wohnmobile erlaubt", "camper willkommen", "camper erlaubt"
    ]
    for kw in tent_only_keywords:
        if kw in text:
            return False
    for kw in vehicle_allowed_keywords:
        if kw in text:
            return True
    return None

async def fetch_and_save_reviews(client: httpx.AsyncClient, pool, spot_id: int, source_id: str) -> int:
    from db import upsert_review
    reviews_url = f"https://spots.roadsurfer.com/api/spots/{source_id}/reviews"
    try:
        resp = await client.get(reviews_url, timeout=15)
        if resp.status_code == 404:
            return 0
        resp.raise_for_status()
        data = resp.json()
        elements = data.get("elements", [])
        if not elements:
            return 0
        
        nuevas = 0
        async with pool.acquire() as conn:
            for elem in elements:
                author = clean_surrogates(elem.get("user", "Anonymous"))
                msg = clean_surrogates(elem.get("message", ""))
                
                ratings = [elem.get("locationRating"), elem.get("hospitalityRating"), elem.get("facilitiesRating")]
                valid_ratings = [float(r) for r in ratings if r is not None]
                score = round(sum(valid_ratings) / len(valid_ratings), 1) if valid_ratings else 5.0
                
                dt_created = None
                created_ts = elem.get("creationDate")
                if created_ts:
                    try:
                        dt_created = datetime.fromtimestamp(created_ts, tz=timezone.utc)
                    except Exception:
                        pass
                
                lang = detect_language(msg) if msg else "en"
                
                import hashlib
                rev_hash = hashlib.md5(f"{author}:{created_ts}:{msg[:100]}".encode("utf-8", "ignore")).hexdigest()
                
                review_data = {
                    "spot_id": spot_id,
                    "source": "roadsurfer",
                    "source_review_id": f"rs_{rev_hash}",
                    "texto": msg,
                    "rating": score,
                    "autor": author[:100],
                    "fecha": dt_created,
                    "idioma": lang
                }
                
                inserted = await upsert_review(conn, review_data)
                nuevas += int(bool(inserted))
        return nuevas
    except Exception as e:
        logger.error(f"[ROADSURFER] Error reviews spot {source_id}: {e}")
        return 0

class RoadsurferSource(AbstractSource):
    name = "roadsurfer"
    rate_limit = 0.5
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Roadsurfer usa búsqueda radial global")

    def normalize(self, raw: dict, detail: dict | None = None) -> dict | None:
        try:
            if detail:
                lat = float(detail["latitude"])
                lon = float(detail["longitude"])
            else:
                lat = float(raw["location"]["lat"])
                lon = float(raw["location"]["lng"])
        except (KeyError, TypeError, ValueError):
            return None

        # Tipos
        camping_types = detail.get("campingTypes", []) if detail else raw.get("terrainFor", [])
        tipo = "otro"
        if "camperVan" in camping_types or "motorhome" in camping_types or "caravan" in camping_types:
            tipo = "area_ac"
        elif "tent" in camping_types:
            tipo = "naturaleza"

        # Fotos
        fotos = []
        if detail and detail.get("images"):
            for img in detail.get("images"):
                if isinstance(img, str):
                    if img.startswith("/"):
                        img = f"https://spots.roadsurfer.com{img}"
                    fotos.append(img)
        else:
            html = raw.get("previewImageHtml")
            if html:
                match = re.search(r'<img[^>]+src="([^"]+)"', html)
                if match:
                    url = match.group(1)
                    if url.startswith("/"):
                        url = f"https://spots.roadsurfer.com{url}"
                    fotos.append(url)

        # Precio
        gratuito = raw.get("isFreeSpot", False)
        precio_aprox = None
        precio_info = None
        if detail:
            gratuito = detail.get("isFreeSpot", False)
            if gratuito:
                precio_aprox = 0.0
                precio_info = "Gratuito"
            else:
                starting_cents = detail.get("startingPrice")
                if starting_cents is not None:
                    precio_aprox = float(starting_cents) / 100.0
                    precio_info = f"{precio_aprox:.2f} €"
        else:
            if gratuito:
                precio_aprox = 0.0
                precio_info = "Gratuito"
            else:
                raw_price = raw.get("price")
                if raw_price is not None:
                    precio_aprox = float(raw_price)
                    precio_info = f"{precio_aprox:.2f} €"

        # Num plazas (defensivo: capacity puede ser string "8-12" o "Variable" en algún spot)
        raw_capacity = detail.get("capacity") if detail else raw.get("capacity")
        num_plazas = None
        if raw_capacity is not None:
            try:
                num_plazas = int(raw_capacity)
            except (TypeError, ValueError):
                # Intenta extraer primer número de un string tipo "8-12"
                m = re.search(r"\d+", str(raw_capacity))
                if m:
                    try:
                        num_plazas = int(m.group())
                    except ValueError:
                        num_plazas = None

        # Acceso grandes
        acceso_grandes = None
        if camping_types:
            acceso_grandes = any(t in camping_types for t in ["motorhome", "caravan"])

        # Perros (defensivo: cat.get("name") puede venir None, no solo missing)
        categories = raw.get("categories", []) or []
        has_dog_cat = any(
            isinstance(cat, dict) and (
                cat.get("id") == 68
                or "dog" in (cat.get("name") or "").lower()
            )
            for cat in categories
        )
        
        perros = None
        if detail:
            facilities = detail.get("facilities", []) or []
            perros = "pets" in facilities or has_dog_cat
        else:
            if has_dog_cat:
                perros = True

        # Amenidades
        agua_potable = None
        electricidad = None
        ducha = None
        wifi = None
        wc_publico = None
        vaciado_grises = None
        vaciado_negras = None

        if detail:
            facilities = detail.get("facilities", []) or []
            agua_potable = "drinkingWater" in facilities
            electricidad = "electricity" in facilities
            ducha = "shower" in facilities
            wifi = "wlan" in facilities or "internet" in facilities
            wc_publico = any(t in facilities for t in ["toilet", "separateToilet", "separateDryToilet"])
            vaciado_grises = "veStation" in facilities
            vaciado_negras = "veStation" in facilities

        # Rating y Reviews Count
        rating_promedio = None
        num_reviews = None
        if detail and detail.get("rating"):
            rating_promedio = detail["rating"].get("average")
            num_reviews = detail["rating"].get("quantity")
        else:
            rating_promedio = raw.get("averageRating")
            num_reviews = raw.get("ratingsQuantity")

        # Descripción
        desc = None
        lang = "en"
        if detail:
            desc = clean_surrogates(detail.get("description", "")).strip()
            orig_lang = detail.get("originalLanguage", "en_GB").lower()
            if desc:
                lang = orig_lang[:2] if orig_lang and len(orig_lang) >= 2 else "en"
                if lang not in ["de", "en", "fr", "es", "it", "nl", "pt"]:
                    lang = detect_language(desc)

        # Inferencia extra de acceso grandes
        if desc:
            inf_large = infer_large_vehicles(desc)
            if inf_large is not None:
                acceso_grandes = inf_large

        # Inferencia extra de perros
        if desc and perros is None:
            from sources.vansite import infer_dogs
            inf_dogs = infer_dogs(desc)
            if inf_dogs is not None:
                perros = inf_dogs

        res = {
            "source_id": str(raw.get("id")),
            # Defensivo: name puede venir explícitamente null, no solo missing
            "nombre": (raw.get("name") or "Roadsurfer Spot").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "precio_aprox": precio_aprox,
            "precio_info": precio_info,
            "num_plazas": num_plazas,
            "acceso_grandes": acceso_grandes,
            "perros": perros,
            "agua_potable": agua_potable,
            "electricidad": electricidad,
            "ducha": ducha,
            "wifi": wifi,
            "wc_publico": wc_publico,
            "vaciado_grises": vaciado_grises,
            "vaciado_negras": vaciado_negras,
            "rating_promedio": rating_promedio,
            "num_reviews": num_reviews,
            "web": raw.get("url", "") or (detail.get("link", "") if detail else ""),
            "fotos_urls": fotos
        }

        if desc:
            res[f"descripcion_{lang}"] = desc

        # El extractor lee de detail si existe (estructura rica con facilities/activities/
        # placeSituations); si solo hay raw de Phase 1, devolverá poco.
        return merge_extra(res, extract_roadsurfer(detail or raw))

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
            offset = 0
            size = 100
            seen_ids = set()
            
            while True:
                payload = {
                    "offset": offset,
                    "size": size,
                    "geoLocation": {
                        "lat": 50.0,
                        "lng": 10.0,
                        "type": None,
                        "name": None
                    },
                    "searchRadius": 20000,
                    "sort": "distance",
                    "allowWithoutLocation": False,
                    "terrainFor": [],
                    "activities": [],
                    "facilities": [],
                    "placeSituations": [],
                    "categories": None,
                    "country": None,
                    "startDate": None,
                    "endDate": None,
                    "maxPrice": None,
                    "onlyFreeSpots": False,
                    "searchType": "default"
                }

                try:
                    logger.info(f"[ROADSURFER] Buscando con offset {offset}...")
                    resp = await client.post(BASE_URL, json=payload, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[ROADSURFER] Error en offset {offset}: {e}")
                    stats["errores"] += 1
                    break

                spots = data.get("spots", [])
                if not spots:
                    break

                nuevos_en_pagina = 0
                for raw in spots:
                    sid = str(raw.get("id"))
                    if sid in seen_ids:
                        continue
                        
                    seen_ids.add(sid)
                    nuevos_en_pagina += 1
                    
                    # Fetch details
                    detail = None
                    try:
                        detail_url = f"https://spots.roadsurfer.com/api/spots/{sid}"
                        resp_det = await client.get(detail_url, timeout=15)
                        if resp_det.status_code == 200:
                            detail = resp_det.json()
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.error(f"[ROADSURFER] Error details spot {sid}: {e}")
                    
                    norm = self.normalize(raw, detail)
                    if not norm:
                        continue
                    if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                        continue

                    try:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                existente = await find_spot_cercano(
                                    conn, norm["lat"], norm["lon"], self.dedup_radius_m,
                                    nombre=norm["nombre"], tipo=norm["tipo"]
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
                                    conn, spot_id, self.name, sid, detail or raw, norm
                                )
                                
                        # Fetch and save reviews
                        if (norm.get("num_reviews") or 0) > 0:
                            rev_count = await fetch_and_save_reviews(client, pool, spot_id, sid)
                            stats["reviews_nuevas"] += rev_count
                            await asyncio.sleep(0.1)
                            
                    except Exception as e:
                        logger.error(f"[ROADSURFER] Error spot '{norm.get('nombre')}': {e}")
                        stats["errores"] += 1

                logger.info(f"[ROADSURFER] Offset {offset}: procesados {len(spots)} spots.")
                
                if nuevos_en_pagina == 0 or len(spots) < size:
                    break
                    
                offset += size
                await asyncio.sleep(self.rate_limit)

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[ROADSURFER] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config) -> dict:
        """Incremental review re-fetch for existing Roadsurfer source records."""
        stats = {"reviews_nuevas": 0, "errores": 0, "procesados": 0}
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT spot_id, source_id
                FROM source_records
                WHERE source = $1
                  AND COALESCE(review_count, 0) > 0
                ORDER BY last_seen DESC NULLS LAST
                """,
                self.name,
            )

        async with httpx.AsyncClient(headers=HEADERS) as client:
            for row in rows:
                try:
                    stats["reviews_nuevas"] += await fetch_and_save_reviews(
                        client, pool, row["spot_id"], row["source_id"]
                    )
                    stats["procesados"] += 1
                    await asyncio.sleep(self.rate_limit)
                except Exception as e:
                    logger.error(f"[ROADSURFER] Error incremental reviews {row['source_id']}: {e}")
                    stats["errores"] += 1
        return stats
