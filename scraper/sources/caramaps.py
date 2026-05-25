"""Caramaps — scraper desde API ElasticSearch paginada."""

import asyncio
import json
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

BASE_URL = "https://admin.caramaps.com/api/revisions/elastic"

# Bounding box global (sin filtros geográficos restrictivos)
WORLD_TOP    =  90.0
WORLD_BOTTOM = -90.0
WORLD_LEFT   = -180.0
WORLD_RIGHT  =  180.0

HEADERS = {
    "accept": "*/*",
    "accept-language": "es",
    "origin": "https://www.caramaps.com",
    "referer": "https://www.caramaps.com/",
    "user-agent": (
        "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Mobile Safari/537.36"
    ),
}

# Mapeo type.code → tipo canónico GeoSpots
TIPO_MAP = {
    "parking":          "parking",
    "camping":          "camping",
    "spot":             "naturaleza",
    "service_area":     "area_ac",
    "highway_area":     "parking",
    "caramaps_host":    "camping",
    "private_area":     "area_ac",
    "concession":       "area_ac",
    "nature":           "naturaleza",
    "wild":             "naturaleza",
    "bivouac":          "naturaleza",
    "aire":             "area_ac",
    "motorhome":        "area_ac",
    "service":          "area_ac",
    "sani":             "area_ac",
    "dump":             "area_ac",
    "picnic":           "picnic",
}

# Mapeo country name → ISO2 (los más frecuentes en EU)
COUNTRY_ISO = {
    "France": "FR", "Spain": "ES", "Germany": "DE", "Italy": "IT",
    "Portugal": "PT", "Netherlands": "NL", "Belgium": "BE", "Austria": "AT",
    "Switzerland": "CH", "United Kingdom": "GB", "Ireland": "IE",
    "Denmark": "DK", "Norway": "NO", "Sweden": "SE", "Finland": "FI",
    "Poland": "PL", "Czech Republic": "CZ", "Croatia": "HR", "Slovenia": "SI",
    "Greece": "GR", "Turkey": "TR", "Morocco": "MA", "Romania": "RO",
    "Hungary": "HU", "Slovakia": "SK", "Luxembourg": "LU", "Andorra": "AD",
    "Monaco": "MC", "Liechtenstein": "LI", "Albania": "AL", "Bosnia": "BA",
    "Serbia": "RS", "Montenegro": "ME", "Macedonia": "MK", "Bulgaria": "BG",
    "Lithuania": "LT", "Latvia": "LV", "Estonia": "EE",
}

# Mapeo atributos de fallback → campos GeoSpots
ATTR_MAP = {
    "water":            "agua_potable",
    "electricity":      "electricidad",
    "shower":           "ducha",
    "toilet":           "wc_publico",
    "wifi":             "wifi",
    "dog":              "perros",
    "dump":             "vaciado_negras",
    "grey_water":       "vaciado_grises",
    "agua":             "agua_potable",
    "eau":              "agua_potable",
    "electricité":      "electricidad",
    "electricidad":     "electricidad",
    "strom":            "electricidad",
    "douche":           "ducha",
    "ducha":            "ducha",
    "wc":               "wc_publico",
    "toilette":         "wc_publico",
    "sanitär":          "wc_publico",
    "wi-fi":            "wifi",
    "internet":         "wifi",
    "chien":            "perros",
    "perro":            "perros",
    "hund":             "perros",
    "vidange":          "vaciado_negras",
    "vaciado":          "vaciado_negras",
    "eaux noires":      "vaciado_negras",
    "eaux grises":      "vaciado_grises",
}


def _map_attr(attr_item: dict) -> str | None:
    """Devuelve el campo GeoSpots correspondiente al atributo basado en picto originalName,
    o en su defecto label/code en minúsculas."""
    attr = attr_item.get("attribute") or {}
    
    # 1. Intentar por picto.originalName (la fuente más fiable y robusta en CaraMaps)
    picto = attr.get("picto") or {}
    picto_name = (picto.get("originalName") or "").lower().strip()
    
    if picto_name:
        # Vaciados primero (son más específicos y a menudo contienen substrings como 'eau' o 'wc')
        if "cassette" in picto_name or "noire" in picto_name or "vidange_wc" in picto_name:
            return "vaciado_negras"
        if "usees" in picto_name or "grise" in picto_name:
            return "vaciado_grises"
            
        # Otros servicios generales
        if "eau" in picto_name or "water" in picto_name:
            return "agua_potable"
        if "electr" in picto_name or "strom" in picto_name or "plug" in picto_name:
            return "electricidad"
        if "douch" in picto_name or "shower" in picto_name:
            return "ducha"
        if "toilet" in picto_name or "wc" in picto_name or "sanit" in picto_name:
            return "wc_publico"
        if "wifi" in picto_name or "internet" in picto_name:
            return "wifi"
        if "chien" in picto_name or "dog" in picto_name or "animau" in picto_name or "pet" in picto_name:
            return "perros"

    # 2. Fallbacks basados en code y label
    code = (attr.get("code") or "").lower().strip()
    label = (attr.get("label") or "").lower().strip()
    
    for k, v in ATTR_MAP.items():
        if k == code or k == label:
            return v
            
    return None


def clean_surrogates(text: str) -> str:
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return "".join(c for c in text if not (0xD800 <= ord(c) <= 0xDFFF))


class CaramapsSource(AbstractSource):
    name = "caramaps"
    rate_limit = 0.3
    grid_step = 2.0
    dedup_radius_m = 60.0
    HEADERS = HEADERS

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        """Descarga todos los items de una celda bbox paginando la API de CaraMaps."""
        all_items = []
        page = 1
        total_pages = 1
        
        while page <= total_pages:
            params = {
                "page": page,
                "itemsPerPage": 800,
                "order[createdAt]": "desc",
                "filters[bounds][top]": tl_lat,
                "filters[bounds][bottom]": br_lat,
                "filters[bounds][left]": tl_lon,
                "filters[bounds][right]": br_lon,
                "filters[attributesDetail][0]": 0,
                "filters[attributesDetail][1]": 200,
                "filters[attributesDetail][2]": 1,
                "filters[type.uuid][0]": "98eb91bf-3f57-490a-b4a3-632f31866bda",
                "filters[type.uuid][1]": "0f1596c3-bf8a-4508-b443-bae33d8a748a",
                "filters[type.uuid][2]": "f085f879-dba1-4744-94f9-616eb9ae9ef6",
                "filters[type.uuid][3]": "dc93f4dc-622b-47c1-8f0f-40f5a170c4a0",
                "filters[type.uuid][4]": "8e87c2dd-7720-4dad-98d5-99dd2fc1fedf",
                "filters[type.uuid][5]": "8606c8e1-8acc-44ce-a8ae-c2f7a4fb81f7",
                "filters[type.uuid][6]": "7a390087-587b-4045-a188-733423f2117c",
            }
            
            try:
                resp = await client.get(BASE_URL, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"[caramaps] Error en fetch_cell (page={page}, bbox=[{tl_lat}, {tl_lon}, {br_lat}, {br_lon}]): {e}")
                break
                
            items = data.get("items", [])
            all_items.extend(items)
            
            total_pages = data.get("lastPage", 1)
            if not items or page >= total_pages:
                break
                
            page += 1
            await asyncio.sleep(self.rate_limit)
            
        return all_items

    def normalize(self, raw: dict) -> dict | None:
        addr = raw.get("address") or {}
        lat = addr.get("lat")
        lon = addr.get("lng")
        if lat is None or lon is None:
            return None

        # Filtro de coordenadas globales válidas
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None

        poi = raw.get("pointOfInterest") or {}
        type_info = raw.get("type") or {}
        type_code = (type_info.get("code") or "").lower()
        tipo = "otro"
        for k, v in TIPO_MAP.items():
            if k in type_code:
                tipo = v
                break

        # Gratuito: parkingType o inferencia de atributos
        parking_type = raw.get("parkingType") or {}
        gratuito = None
        if parking_type.get("code") == "free_parking":
            gratuito = True
        elif parking_type.get("code") in ("paying_parking", "paid"):
            gratuito = False

        # Servicios desde attributes (lógica robusta por picto/label/code)
        servicios = {}
        for attr_item in raw.get("attributes") or []:
            campo = _map_attr(attr_item)
            if campo and campo not in servicios:
                servicios[campo] = True

        # Fotos
        fotos = []
        main_pic = poi.get("mainPicture") or {}
        if main_pic.get("contentUrl"):
            fotos.append(main_pic["contentUrl"])
        for p in poi.get("pictures") or []:
            url = (p.get("media") or {}).get("contentUrl")
            if url and url not in fotos:
                fotos.append(url)

        # Altura máxima
        max_height = raw.get("maxHeight") or 0
        altura = float(max_height) if max_height and max_height > 0 else None

        country_name = addr.get("country") or ""
        country_iso = COUNTRY_ISO.get(country_name) or country_name[:2].upper() or None

        return {
            "source_id":       str(raw.get("id") or raw.get("uuid", "")),
            "nombre":          (raw.get("name") or "Sin nombre").strip()[:200],
            "lat":             lat,
            "lon":             lon,
            "tipo":            tipo,
            "gratuito":        gratuito,
            "country_iso":     country_iso,
            "region":          addr.get("cityName"),
            "master_rating":   poi.get("averageNotation"),
            "altura_max_m":    altura,
            "fotos_urls":      fotos if fotos else [],
            "web":             f"https://www.caramaps.com/spot/{raw.get('uuid', '')}",
            **servicios,
        }

    async def download_reviews(self, pool, config) -> dict:
        from db import upsert_review
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
                    sr.raw_data->'pointOfInterest'->>'uuid' AS poi_uuid
                FROM source_records sr
                WHERE sr.source = 'caramaps'
                  AND sr.raw_data->'pointOfInterest'->>'uuid' IS NOT NULL
                  AND (sr.normalized_data->>'reviews_fetched') IS NULL
                ORDER BY sr.spot_id DESC;
            """)

        logger.info(f"[{self.name}] {len(review_jobs)} spots con reviews pendientes.")
        if not review_jobs:
            return stats

        job_queue = asyncio.Queue()
        for r in review_jobs:
            await job_queue.put(dict(r))

        COMMENTS_URL = "https://admin.caramaps.com/api/point_of_interest_comments"

        async def worker(client):
            while not job_queue.empty():
                try:
                    job = job_queue.get_nowait()
                except (asyncio.QueueEmpty, asyncio.CancelledError):
                    break

                spot_id = job["spot_id"]
                sid = job["source_id"]
                poi_uuid = job["poi_uuid"]

                page = 1
                items_per_page = 50
                has_error = False

                while True:
                    params = {
                        "pointOfInterest.uuid": poi_uuid,
                        "deletedAt": "false",
                        "itemsPerPage": str(items_per_page),
                        "page": str(page),
                        "order[createdAt]": "desc"
                    }
                    try:
                        await asyncio.sleep(self.rate_limit)
                        resp = await client.get(COMMENTS_URL, params=params, timeout=20)
                        if resp.status_code == 404:
                            break
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        logger.warning(f"[{self.name}] Error fetching reviews for spot {sid} page {page}: {e}")
                        stats["errores"] += 1
                        has_error = True
                        break

                    comments = data.get("items") or data.get("hydra:member") or []
                    if not comments:
                        break

                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            for c in comments:
                                comment_uuid = c.get("uuid")
                                if not comment_uuid:
                                    continue

                                text = clean_surrogates(
                                    c.get("value") or c.get("defaultValue") or ""
                                )

                                rating_val = c.get("notation")
                                try:
                                    rating = float(rating_val) if rating_val is not None else None
                                except (ValueError, TypeError):
                                    rating = None

                                author = c.get("author") or {}
                                given_name = author.get("givenName") or ""
                                family_name = author.get("familyName") or ""
                                autor_name = f"{given_name} {family_name}".strip() or "Usuario CaraMaps"

                                fecha_str = c.get("createdAt")
                                fecha_val = None
                                if fecha_str:
                                    try:
                                        fecha_val = datetime.fromisoformat(fecha_str).date()
                                    except Exception:
                                        pass

                                author_locale = c.get("authorLocale") or {}
                                lang = author_locale.get("alpha2") or "es"

                                review_dict = {
                                    "spot_id": spot_id,
                                    "source": "caramaps",
                                    "source_review_id": comment_uuid,
                                    "texto": text or None,
                                    "rating": rating,
                                    "autor": autor_name,
                                    "fecha": fecha_val,
                                    "idioma": lang,
                                }
                                await upsert_review(conn, review_dict)
                                stats["reviews_nuevas"] += 1

                    if len(comments) < items_per_page:
                        break
                    page += 1

                if not has_error:
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE source_records
                            SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb
                            WHERE source = 'caramaps' AND source_id = $1
                        """, sid)
                    stats["actualizados"] += 1
                job_queue.task_done()

        headers = getattr(self, 'HEADERS', {})
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            workers = [asyncio.create_task(worker(client)) for _ in range(3)]
            await asyncio.gather(*workers)

        return stats

