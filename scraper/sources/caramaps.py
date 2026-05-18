"""Caramaps — scraper desde API ElasticSearch paginada."""

import asyncio
import json
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

BASE_URL = "https://admin.caramaps.com/api/revisions/elastic"

# Bounding box Europa completa
EU_TOP    =  71.5   # Noruega norte
EU_BOTTOM =  34.0   # Gibraltar / Malta
EU_LEFT   = -25.0   # Azores / Islandia
EU_RIGHT  =  45.0   # Turquía / Rusia occidental

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
    "aire":             "area_ac",
    "motorhome":        "area_ac",
    "service":          "area_ac",
    "sani":             "area_ac",
    "dump":             "area_ac",
    "picnic":           "picnic",
    "nature":           "naturaleza",
    "wild":             "naturaleza",
    "bivouac":          "naturaleza",
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

# Mapeo atributos → campos GeoSpots
# Basado en los labels visibles; se amplía con los codes cuando los veamos
ATTR_MAP = {
    # Por code
    "water":            "agua_potable",
    "electricity":      "electricidad",
    "shower":           "ducha",
    "toilet":           "wc_publico",
    "wifi":             "wifi",
    "dog":              "perros",
    "dump":             "vaciado_negras",
    "grey_water":       "vaciado_grises",
    # Por label (fallback cuando code está vacío)
    "agua":             "agua_potable",
    "eau":              "agua_potable",
    "water":            "agua_potable",
    "electricité":      "electricidad",
    "electricidad":     "electricidad",
    "strom":            "electricidad",
    "douche":           "ducha",
    "ducha":            "ducha",
    "wc":               "wc_publico",
    "toilette":         "wc_publico",
    "sanitär":          "wc_publico",
    "wifi":             "wifi",
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


def _map_attr(attribute: dict) -> str | None:
    """Devuelve el campo GeoSpots correspondiente al atributo, o None."""
    code = (attribute.get("code") or "").lower().strip()
    label = (attribute.get("label") or "").lower().strip()
    return ATTR_MAP.get(code) or ATTR_MAP.get(label)


class CaramapsSource(AbstractSource):
    name = "caramaps"
    rate_limit = 0.5
    dedup_radius_m = 60.0

    # No usa grid — pagina directamente la API elastic
    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Caramaps usa paginación propia")

    def normalize(self, raw: dict) -> dict | None:
        addr = raw.get("address") or {}
        lat = addr.get("lat")
        lon = addr.get("lng")
        if lat is None or lon is None:
            return None

        # Filtro Europa estricto
        if not (EU_BOTTOM <= lat <= EU_TOP and EU_LEFT <= lon <= EU_RIGHT):
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

        # Servicios desde attributes
        servicios = {}
        for attr_item in raw.get("attributes") or []:
            attr = attr_item.get("attribute") or {}
            campo = _map_attr(attr)
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

        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
            for is_pro in [False, True]:
                page = 1
                total_pages = 1  # se actualiza en la primera respuesta

                while page <= total_pages:
                    params = {
                        "page":                         page,
                        "itemsPerPage":                 800,
                        "order[createdAt]":             "desc",
                        "filters[bounds][top]":         EU_TOP,
                        "filters[bounds][bottom]":      EU_BOTTOM,
                        "filters[bounds][left]":        EU_LEFT,
                        "filters[bounds][right]":       EU_RIGHT,
                        "filters[pointOfInterest.isPro]": str(is_pro).lower(),
                        "filters[attributesDetail][0]": 0,
                        "filters[attributesDetail][1]": 200,
                        "filters[attributesDetail][2]": 1,
                        "filters[type.uuid][0]":        "98eb91bf-3f57-490a-b4a3-632f31866bda",
                        "filters[type.uuid][1]":        "0f1596c3-bf8a-4508-b443-bae33d8a748a",
                        "filters[type.uuid][2]":        "f085f879-dba1-4744-94f9-616eb9ae9ef6",
                        "filters[type.uuid][3]":        "dc93f4dc-622b-47c1-8f0f-40f5a170c4a0",
                        "filters[type.uuid][4]":        "8e87c2dd-7720-4dad-98d5-99dd2fc1fedf",
                        "filters[type.uuid][5]":        "8606c8e1-8acc-44ce-a8ae-c2f7a4fb81f7",
                        "filters[type.uuid][6]":        "7a390087-587b-4045-a188-733423f2117c",
                    }

                    try:
                        resp = await client.get(BASE_URL, params=params, timeout=60)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        logger.error(f"[caramaps] Error página {page} (isPro={is_pro}): {e}")
                        stats["errores"] += 1
                        break

                    total_pages = data.get("lastPage", 1)
                    items = data.get("items", [])

                    logger.info(
                        f"[caramaps] isPro={is_pro} | página {page}/{total_pages} "
                        f"| {len(items)} items"
                    )
                    
                    if len(items) == 0:
                        logger.warning(f"URL: {resp.url}")
                        logger.warning(f"Response: {data}")

                    for raw in items:
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
                            logger.error(f"[caramaps] Error '{norm.get('nombre')}': {e}")
                            stats["errores"] += 1

                    page += 1
                    await asyncio.sleep(self.rate_limit)

            logger.info(
                f"[caramaps] new={stats['nuevos']} upd={stats['actualizados']} "
                f"err={stats['errores']}"
            )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[caramaps] Completado en {dur:.0f}s | {stats}")
        return stats
