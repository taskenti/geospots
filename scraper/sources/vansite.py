"""Vansite (Sharetribe Flex API) — scraper."""

import asyncio
from datetime import datetime, timezone
import urllib.parse
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_vansite, merge_extra

def decode_cache_code(code):
    """Decodifica un código de base-44 (ASCII 48-91) a entero."""
    idx = 0
    for char in code:
        idx = idx * 44 + (ord(char) - 48)
    return idx

def decode_transit_value(val, cache):
    """Decodifica recursivamente un valor de Transit JSON resolviendo los cache codes."""
    if isinstance(val, str):
        if val.startswith("^") and val != "^ " and len(val) > 1:
            idx = decode_cache_code(val[1:])
            if idx < len(cache):
                return cache[idx]
            else:
                return val
        if (val.startswith("~:") or val.startswith("~#")) and len(val) > 3:
            if val not in cache:
                cache.append(val)
        return val

    elif isinstance(val, list):
        if len(val) > 0 and val[0] == "^ ":
            decoded_dict = {}
            for i in range(1, len(val), 2):
                if i+1 < len(val):
                    k = val[i]
                    k_dec = decode_transit_value(k, cache)
                    if isinstance(k, str) and not k.startswith("^") and len(k) > 3:
                        if k not in cache:
                            cache.append(k)
                    v_dec = decode_transit_value(val[i+1], cache)
                    decoded_dict[k_dec] = v_dec
            return decoded_dict
        else:
            return [decode_transit_value(x, cache) for x in val]

    elif isinstance(val, dict):
        decoded_dict = {}
        for k, v in val.items():
            k_dec = decode_transit_value(k, cache)
            if isinstance(k, str) and not k.startswith("^") and len(k) > 3:
                if k not in cache:
                    cache.append(k)
            v_dec = decode_transit_value(v, cache)
            decoded_dict[k_dec] = v_dec
        return decoded_dict
    else:
        return val

def transit_to_dict(t):
    """Convierte Transit JSON decodificando las referencias de caché base-44."""
    cache = []
    return decode_transit_value(t, cache)

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

def infer_dogs(text: str) -> bool | None:
    if not text:
        return None
    text = text.lower()
    forbidden_keywords = [
        "no perros", "sin perros", "perros no", "perros prohibidos", "no mascotas", "mascotas prohibidas", "no se admiten perros", "no se aceptan perros",
        "no dogs", "no pets", "dogs not allowed", "pets not allowed", "no animals", "without dogs", "without pets",
        "pas de chien", "chien interdit", "sans chien", "pas d'animaux", "animaux interdits", "sans animaux",
        "geen honden", "geen huisdieren", "honden niet toegestaan", "huisdieren niet toegestaan",
        "keine hunde", "keine haustiere", "hunde nicht erlaubt", "hunde verboten"
    ]
    allowed_keywords = [
        "perros bienvenidos", "se aceptan perros", "se admiten perros", "mascotas bienvenidas", "se aceptan mascotas", "se admiten mascotas", "perros ok",
        "dogs welcome", "dogs allowed", "pets welcome", "pets allowed", "dogs ok", "dog friendly", "pet friendly",
        "chien bienvenu", "chiens bienvenus", "chien accepté", "chiens acceptés", "animaux acceptés", "animaux bienvenus",
        "honden welkom", "honden toegestaan", "huisdieren welkom", "huisdieren toegestaan",
        "hunde willkommen", "hunde erlaubt", "haustiere willkommen", "haustiere erlaubt"
    ]
    for kw in forbidden_keywords:
        if kw in text:
            return False
    for kw in allowed_keywords:
        if kw in text:
            return True
    return None

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

async def fetch_and_save_reviews(client: httpx.AsyncClient, pool, spot_id: int, listing_id: str) -> int:
    REVIEWS_URL = "https://flex-api.sharetribe.com/v1/api/reviews/query"
    params = {
        "listingId": listing_id,
        "include": "author",
        "per_page": 100
    }
    try:
        resp = await client.get(REVIEWS_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        decoded = transit_to_dict(data)
        reviews_data = decoded.get("~:data", [])
        if not reviews_data:
            return 0
        
        included = decoded.get("~:included", [])
        authors_map = {}
        for item in included:
            if item.get("~:type") == "~:user":
                uid = str(item.get("~:id", "")).replace("~u", "")
                profile = item.get("~:attributes", {}).get("~:profile", {})
                display_name = profile.get("~:displayName")
                if display_name:
                    authors_map[uid] = display_name

        saved = 0
        async with pool.acquire() as conn:
            for r in reviews_data:
                if r.get("~:type") != "~:review":
                    continue
                attrs = r.get("~:attributes", {})
                r_id = str(r.get("~:id", "")).replace("~u", "")
                r_text = attrs.get("~:content", "")
                r_rating = attrs.get("~:rating")
                
                author_id = ""
                rel_author = r.get("~:relationships", {}).get("~:author", {}).get("~:data", {})
                if rel_author:
                    author_id = str(rel_author.get("~:id", "")).replace("~u", "")
                r_author = authors_map.get(author_id, "Vansite User")
                
                rating_val = None
                if r_rating is not None:
                    try:
                        rating_val = float(r_rating)
                    except (ValueError, TypeError):
                        pass

                if not r_id or not r_text:
                    continue

                fecha = None
                created_at_raw = attrs.get("~:createdAt")
                if isinstance(created_at_raw, str) and created_at_raw.startswith("~m"):
                    try:
                        millis = int(created_at_raw[2:])
                        fecha = datetime.fromtimestamp(millis / 1000.0, timezone.utc)
                    except Exception:
                        pass

                idioma = detect_language(r_text)

                review_dict = {
                    "spot_id": spot_id,
                    "source": "vansite",
                    "source_review_id": r_id,
                    "texto": r_text,
                    "rating": rating_val,
                    "autor": r_author,
                    "fecha": fecha,
                    "idioma": idioma
                }
                
                from db import upsert_review
                inserted = await upsert_review(conn, review_dict)
                saved += int(bool(inserted))

            if saved > 0:
                # Recalcular total_reviews del spot en la base de datos
                await conn.execute("""
                    UPDATE spots SET total_reviews = (
                        SELECT COUNT(*) FROM reviews WHERE spot_id = $1
                    ) WHERE id = $1
                """, spot_id)

        return saved
    except Exception as e:
        logger.error(f"[VANSITE] Error descargando reviews para listing {listing_id}: {e}")
        return 0

BASE_URL = "https://flex-api.sharetribe.com/v1/api/listings/query"
TOKEN_URL = "https://flex-api.sharetribe.com/v1/auth/token"
CLIENT_ID = "c2d9908f-ca85-4a48-8b92-d462185ad472"

HEADERS = {
    "accept": "application/transit+json",
    "origin": "https://vansite.eu",
    "referer": "https://vansite.eu/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
            coords = geo[1]
            lat = float(coords[0])
            lon = float(coords[1])
        except (KeyError, TypeError, ValueError, IndexError):
            return None

        sid = str(raw.get("~:id", "")).replace("~u", "")
        pub_data = attrs.get("~:publicData", {})
        
        # Precio
        precio_raw = attrs.get("~:price")
        precio_info = None
        precio_aprox = None
        gratuito = False
        if isinstance(precio_raw, list) and len(precio_raw) == 2:
            val_list = precio_raw[1]
            if isinstance(val_list, list) and len(val_list) == 2:
                amount = val_list[0]
                currency = val_list[1]
                try:
                    amount_val = float(amount)
                    precio_aprox = amount_val / 100.0
                    if amount_val == 0:
                        gratuito = True
                        precio_info = "Gratuito"
                    else:
                        precio_info = f"{precio_aprox:.2f} {currency}"
                except (ValueError, TypeError):
                    pass

        # Tipo
        tipo = "naturaleza"
        if pub_data.get("~:category") == "campsite":
            tipo = "camping"

        # Capacidad
        num_plazas_raw = pub_data.get("~:amountOfSeats")
        num_plazas = None
        if num_plazas_raw is not None:
            try:
                if isinstance(num_plazas_raw, (int, float)):
                    num_plazas = int(num_plazas_raw)
                elif isinstance(num_plazas_raw, str) and not num_plazas_raw.startswith("~z"):
                    num_plazas = int(float(num_plazas_raw))
            except (ValueError, TypeError):
                num_plazas = None

        # Acceso Grandes
        kfz = pub_data.get("~:kfz", [])
        acceso_grandes = any(v in kfz for v in ("motorhome", "camper", "bus", "caravan"))

        # Amenities
        amenities = pub_data.get("~:amenities", [])
        agua_potable = "water" in amenities
        wc_publico = "toilet" in amenities or "wc" in amenities
        ducha = "shower" in amenities
        electricidad = "electricity" in amenities
        wifi = "wifi" in amenities
        perros = "dog" in amenities or "pets" in amenities
        vaciado_grises = "greyWater" in amenities or "greywater" in amenities
        vaciado_negras = "blackWater" in amenities or "blackwater" in amenities

        # Descripciones
        default_desc = attrs.get("~:description", "").strip()
        desc_es = None
        desc_en = None
        desc_fr = None
        desc_de = None
        desc_it = None
        desc_nl = None

        translations = pub_data.get("~:translations", {})
        desc_trans = translations.get("~:description", {})
        if desc_trans:
            desc_es = desc_trans.get("~:es")
            desc_en = desc_trans.get("~:en")
            desc_fr = desc_trans.get("~:fr")
            desc_de = desc_trans.get("~:de")
            desc_it = desc_trans.get("~:it")
            desc_nl = desc_trans.get("~:nl")

        # Inferencia
        if default_desc:
            lang = detect_language(default_desc)
            if lang == "es" and not desc_es: desc_es = default_desc
            elif lang == "en" and not desc_en: desc_en = default_desc
            elif lang == "fr" and not desc_fr: desc_fr = default_desc
            elif lang == "de" and not desc_de: desc_de = default_desc
            elif lang == "nl" and not desc_nl: desc_nl = default_desc
            elif lang == "it" and not desc_it: desc_it = default_desc

            if not perros:
                inf_dogs = infer_dogs(default_desc)
                if inf_dogs is not None:
                    perros = inf_dogs

            if not acceso_grandes:
                inf_large = infer_large_vehicles(default_desc)
                if inf_large is not None:
                    acceso_grandes = inf_large

        # Fotos
        fotos_urls = []
        rel_images = raw.get("~:relationships", {}).get("~:images", {}).get("~:data", [])
        included_images_map = raw.get("_included_images", {})
        if rel_images and included_images_map:
            for rimg in rel_images:
                r_id = rimg.get("~:id")
                if r_id and r_id in included_images_map:
                    img_url = included_images_map[r_id]
                    if img_url and img_url not in fotos_urls:
                        fotos_urls.append(img_url)

        meta_rating = attrs.get("~:metadata", {}).get("~:rating")
        rating_promedio = None
        if meta_rating is not None:
            try:
                rating_promedio = float(meta_rating) / 100.0
            except (ValueError, TypeError):
                pass

        norm = {
            "source_id": sid,
            "nombre": attrs.get("~:title", "Vansite Spot").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "precio_aprox": precio_aprox,
            "precio_info": precio_info,
            "agua_potable": agua_potable,
            "vaciado_negras": vaciado_negras,
            "vaciado_grises": vaciado_grises,
            "electricidad": electricidad,
            "ducha": ducha,
            "wifi": wifi,
            "wc_publico": wc_publico,
            "perros": perros,
            "acceso_grandes": acceso_grandes,
            "num_plazas": num_plazas,
            "rating_promedio": rating_promedio,
            "descripcion_es": desc_es,
            "descripcion_en": desc_en,
            "descripcion_fr": desc_fr,
            "descripcion_de": desc_de,
            "descripcion_it": desc_it,
            "descripcion_nl": desc_nl,
            "web": f"https://vansite.eu/l/{sid}",
            "fotos_urls": fotos_urls
        }
        return merge_extra(norm, extract_vansite(raw))

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

        # 1. Obtener Token OAuth dinámico
        access_token = None
        try:
            logger.info("[VANSITE] Obteniendo token de acceso...")
            async with httpx.AsyncClient() as token_client:
                token_data = {
                    "client_id": CLIENT_ID,
                    "grant_type": "client_credentials",
                    "scope": "public-read"
                }
                token_resp = await token_client.post(TOKEN_URL, data=token_data, timeout=15)
                token_resp.raise_for_status()
                token_json = token_resp.json()
                access_token = token_json.get("access_token")
                logger.info("[VANSITE] Token obtenido con éxito.")
        except Exception as e:
            logger.error(f"[VANSITE] Error al obtener el token: {e}")
            stats["errores"] += 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        headers = HEADERS.copy()
        if access_token:
            headers["authorization"] = f"Bearer {access_token}"

        params = {
            "bounds": "90.0,180.0,-90.0,-180.0", # Mundo completo
            "mapSearch": "true",
            "per_page": 100,
            "pub_hidden": "false",
            "sort": "pub_verified,meta_rating",
            "include": "images",
            "fields.listing": "title,description,state,geolocation,price,createdAt,publicData,metadata.rating",
            "fields.image": "variants.default"
        }

        async with httpx.AsyncClient(headers=headers) as client:
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
                    logger.error(f"[VANSITE] HTTP Error en página {page}: {e}")
                    stats["errores"] += 1
                    break
                except Exception as e:
                    logger.error(f"[VANSITE] Error en página {page}: {e}")
                    stats["errores"] += 1
                    break

                try:
                    parsed_data = transit_to_dict(data)
                    listings = parsed_data.get("~:data", [])
                except Exception as e:
                    logger.error(f"[VANSITE] Error parseando formato Transit JSON: {e}")
                    break

                if not listings or len(listings) == 0:
                    break

                # Mapear imágenes incluidas
                included = parsed_data.get("~:included", [])
                included_images_map = {}
                for item in included:
                    if item.get("~:type") == "~:image":
                        img_id = item.get("~:id")
                        variants = item.get("~:attributes", {}).get("~:variants", {})
                        default_img = variants.get("~:default", {})
                        img_url = default_img.get("~:url")
                        if img_url:
                            included_images_map[img_id] = img_url

                nuevos_en_pagina = 0
                for raw in listings:
                    sid = str(raw.get("~:id", ""))
                    if sid in seen_ids:
                        continue
                        
                    seen_ids.add(sid)
                    nuevos_en_pagina += 1
                    
                    # Pasar el mapa de imágenes
                    raw["_included_images"] = included_images_map
                    norm = self.normalize(raw)
                    if not norm:
                        continue

                    spot_id = None
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
                                    conn, spot_id, self.name, sid.replace("~u", ""), raw, norm
                                )
                    except Exception as e:
                        logger.error(f"[VANSITE] Error spot '{norm.get('nombre')}': {e}")
                        stats["errores"] += 1

                    if spot_id:
                        saved_reviews = await fetch_and_save_reviews(client, pool, spot_id, sid.replace("~u", ""))
                        stats["reviews_nuevas"] += saved_reviews
                        await asyncio.sleep(0.1)

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

    async def download_reviews(self, pool, config) -> dict:
        """Incremental review re-fetch for existing Vansite listings."""
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
                    logger.error(f"[VANSITE] Error incremental reviews {row['source_id']}: {e}")
                    stats["errores"] += 1
        return stats
