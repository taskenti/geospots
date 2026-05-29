"""OpenStreetMap — scraper vía Overpass API."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_osm, merge_extra

OVERPASS_URL = "https://overpass.kumi.systems/api/interpreter"

OVERPASS_QUERY = """
[out:json][timeout:60];
(
  node["tourism"="caravan_site"](around:{radio},{lat},{lon});
  way["tourism"="caravan_site"](around:{radio},{lat},{lon});
  node["amenity"="sanitary_dump_station"](around:{radio},{lat},{lon});
  node["amenity"="water_point"]["drinking_water"="yes"](around:{radio},{lat},{lon});
  node["amenity"="parking"]["motorhome"="yes"](around:{radio},{lat},{lon});
  way["amenity"="parking"]["motorhome"="yes"](around:{radio},{lat},{lon});
  node["leisure"="camping_site"](around:{radio},{lat},{lon});
  way["leisure"="camping_site"](around:{radio},{lat},{lon});
);
out center body qt;
"""

OSM_TIPO_MAP = {
    ("tourism", "caravan_site"): "area_ac",
    # sanitary_dump_station y water_point son SERVICIOS, no spots de pernocta.
    # Los clasificamos como area_ac (área de servicios) y además marcamos el flag
    # de servicio correspondiente (vaciado_negras / agua_potable) en normalize().
    # Antes caían como "otro" sin flag → vacíos de información útil.
    ("amenity", "sanitary_dump_station"): "area_ac",
    ("amenity", "water_point"): "area_ac",
    ("amenity", "parking"): "parking",
    ("leisure", "camping_site"): "camping",
}

# Mapeo de nombres comunes de país (lo que escriben los editores OSM en addr:country)
# a ISO2 lowercase. addr:country es texto libre — puede venir como "France",
# "FR", "FRA", "francia", etc. Si no mapea, dejar None y que PostGIS clasifique.
OSM_COUNTRY_TO_ISO = {
    "fr": "fr", "fra": "fr", "france": "fr",
    "es": "es", "esp": "es", "spain": "es", "españa": "es", "espana": "es",
    "de": "de", "deu": "de", "germany": "de", "deutschland": "de", "alemania": "de",
    "it": "it", "ita": "it", "italy": "it", "italia": "it",
    "pt": "pt", "prt": "pt", "portugal": "pt",
    "nl": "nl", "nld": "nl", "netherlands": "nl", "holanda": "nl",
    "be": "be", "bel": "be", "belgium": "be", "belgique": "be",
    "ch": "ch", "che": "ch", "switzerland": "ch", "suisse": "ch", "schweiz": "ch",
    "at": "at", "aut": "at", "austria": "at", "österreich": "at", "osterreich": "at",
    "gb": "gb", "uk": "gb", "gbr": "gb", "united kingdom": "gb",
    "ie": "ie", "irl": "ie", "ireland": "ie",
    "dk": "dk", "dnk": "dk", "denmark": "dk", "danmark": "dk",
    "no": "no", "nor": "no", "norway": "no", "norge": "no",
    "se": "se", "swe": "se", "sweden": "se", "sverige": "se",
    "fi": "fi", "fin": "fi", "finland": "fi", "suomi": "fi",
    "pl": "pl", "pol": "pl", "poland": "pl", "polska": "pl",
    "cz": "cz", "cze": "cz", "czechia": "cz",
    "sk": "sk", "svk": "sk", "slovakia": "sk", "slovensko": "sk",
    "hu": "hu", "hun": "hu", "hungary": "hu",
    "si": "si", "svn": "si", "slovenia": "si", "slovenija": "si",
    "hr": "hr", "hrv": "hr", "croatia": "hr", "hrvatska": "hr",
    "gr": "gr", "grc": "gr", "greece": "gr",
    "ro": "ro", "rou": "ro", "romania": "ro",
    "bg": "bg", "bgr": "bg", "bulgaria": "bg",
}


def _parse_int_safe(v):
    """OSM tags vienen como string. capacity='12 spaces' o '12+' deben dar 12."""
    if v is None:
        return None
    import re
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else None


def _parse_float_safe(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        import re
        m = re.search(r"\d+(?:\.\d+)?", str(v).replace(",", "."))
        return float(m.group()) if m else None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=4, min=60, max=120),
    retry=(retry_if_exception_type(httpx.TimeoutException) | retry_if_exception_type(httpx.HTTPError)),
    reraise=True
)
async def _fetch_overpass(client, query):
    resp = await client.post(OVERPASS_URL, data={"data": query}, timeout=60)
    if resp.status_code in (429, 504):
        logger.warning(f"Overpass {resp.status_code}. Esperando 60s...")
        await asyncio.sleep(60)
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()


class OSMSource(AbstractSource):
    """OpenStreetMap: Overpass API con grid de puntos."""

    name = "osm"
    rate_limit = 3.0
    grid_step = 0.35
    dedup_radius_m = 50.0

    EU_BOUNDS = {
        "lat_min": 35.0, "lat_max": 71.5,
        "lon_min": -11.0, "lon_max": 30.0,
    }

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("OSM usa puntos con radio")

    def normalize(self, raw: dict) -> dict | None:
        try:
            if "tags" not in raw:
                return None
            tags = raw["tags"]
            try:
                osm_id = int(raw["id"])
            except (TypeError, ValueError, KeyError):
                return None

            # Identifica qué clave principal aplicó (amenity/tourism/leisure) para
            # poder marcar servicios cuando el spot ES el servicio en sí
            tipo = None
            kind = None
            for (k, v), mapeo in OSM_TIPO_MAP.items():
                if tags.get(k) == v:
                    tipo = mapeo
                    kind = (k, v)
                    break
            if not tipo:
                return None

            # Coordenadas
            lat, lon = None, None
            if raw.get("type") == "node":
                lat, lon = _parse_float_safe(raw.get("lat")), _parse_float_safe(raw.get("lon"))
            elif raw.get("type") in ("way", "relation") and "center" in raw:
                lat = _parse_float_safe(raw["center"].get("lat"))
                lon = _parse_float_safe(raw["center"].get("lon"))
            if lat is None or lon is None:
                return None

            # Nombre — priorizar el del usuario (es), después idiomas comunes EU, fallback genérico
            nombre = (
                tags.get("name:es") or tags.get("name")
                or tags.get("name:en") or tags.get("name:fr")
                or tags.get("name:de") or tags.get("name:it")
                or f"OSM {tipo} {osm_id}"
            )

            # Fee/gratuito
            fee = (tags.get("fee") or "").lower().strip()
            gratuito = None
            if fee == "no":
                gratuito = True
            elif fee == "yes":
                gratuito = False

            # Servicios — lógica DOBLE:
            # (a) Si el spot ES un water_point o dump_station, el flag correspondiente
            #     es True por definición (es lo que el POI es)
            # (b) Si no, leer del tag sub-correspondiente del caravan_site/camping
            #     (drinking_water=yes, waste_disposal=yes, etc.)
            is_water_point = kind == ("amenity", "water_point")
            is_dump_station = kind == ("amenity", "sanitary_dump_station")

            agua_potable = None
            if is_water_point:
                agua_potable = True
            elif tags.get("drinking_water") in ("yes", "1"):
                agua_potable = True
            elif tags.get("drinking_water") in ("no", "0"):
                agua_potable = False

            # Vaciado de aguas negras (chemical toilet drain / sanitary dump)
            vaciado_negras = None
            if is_dump_station:
                vaciado_negras = True
            elif tags.get("sanitary_dump_station") in ("yes", "1"):
                vaciado_negras = True
            elif tags.get("toilets:disposal") in ("chemical_disposal", "yes"):
                vaciado_negras = True

            # Vaciado de aguas grises (waste_disposal / waste_water tag)
            vaciado_grises = None
            if is_dump_station:
                # las dump stations típicas hacen ambos
                vaciado_grises = True
            elif tags.get("waste_disposal") in ("yes", "1"):
                vaciado_grises = True
            elif tags.get("waste_water") in ("yes", "1"):
                vaciado_grises = True

            # WC público
            wc_publico = None
            toilets = (tags.get("toilets") or "").lower()
            if toilets in ("yes", "1"):
                wc_publico = True
            elif toilets in ("no", "0"):
                wc_publico = False

            # Ducha
            ducha = None
            shower = (tags.get("shower") or "").lower()
            if shower in ("yes", "1", "hot"):
                ducha = True
            elif shower in ("no", "0"):
                ducha = False

            # Electricidad — varios tags posibles
            electricidad = None
            if tags.get("electricity") in ("yes", "1") or tags.get("power_supply") in ("yes", "1"):
                electricidad = True
            elif tags.get("electricity") in ("no", "0"):
                electricidad = False

            # WiFi
            wifi = None
            wifi_tag = (tags.get("internet_access") or "").lower()
            if wifi_tag in ("yes", "wlan", "wifi"):
                wifi = True
            elif wifi_tag == "no":
                wifi = False

            # Perros
            perros = None
            dog = (tags.get("dog") or tags.get("dogs") or "").lower()
            if dog in ("yes", "leashed", "1"):
                perros = True
            elif dog == "no":
                perros = False

            # Altura máxima vehículo
            altura = _parse_float_safe(tags.get("maxheight"))

            # Capacidad
            plazas = _parse_int_safe(tags.get("capacity"))

            # country_iso desde addr:country (texto libre - mapeo o None)
            country_raw = (tags.get("addr:country") or "").lower().strip()
            country_iso = OSM_COUNTRY_TO_ISO.get(country_raw)

            norm = {
                "source_id": str(osm_id),
                "nombre": nombre,
                "lat": lat, "lon": lon, "tipo": tipo,
                "descripcion_en": tags.get("description"),
                "temporada_apertura": tags.get("opening_hours"),
                "gratuito": gratuito,
                "altura_max_m": altura,
                "num_plazas": plazas,
                "telefono": tags.get("contact:phone") or tags.get("phone"),
                "email": tags.get("contact:email") or tags.get("email"),
                "web": tags.get("contact:website") or tags.get("website"),
                "region": tags.get("addr:city") or tags.get("addr:town"),
                "country_iso": country_iso,
                "agua_potable": agua_potable,
                "vaciado_negras": vaciado_negras,
                "vaciado_grises": vaciado_grises,
                "wc_publico": wc_publico,
                "ducha": ducha,
                "electricidad": electricidad,
                "wifi": wifi,
                "perros": perros,
            }
            return merge_extra(norm, extract_osm(raw))
        except Exception as e:
            logger.error(f"Error normalizando OSM {raw.get('id')}: {e}")
            return None

    async def _generate_active_points(self, pool):
        """Genera puntos de búsqueda basados en spots existentes en la base de datos."""
        import random
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT floor(lat) as lat_idx, floor(lon) as lon_idx FROM spots")
        
        existing_cells = {(int(r['lat_idx']), int(r['lon_idx'])) for r in rows}
        
        if not existing_cells:
            logger.info("[osm] No hay spots en la DB. Usando grid de fallback de Europa...")
            puntos = []
            lat = self.EU_BOUNDS["lat_min"]
            while lat <= self.EU_BOUNDS["lat_max"]:
                lon = self.EU_BOUNDS["lon_min"]
                while lon <= self.EU_BOUNDS["lon_max"]:
                    puntos.append((round(lat, 4), round(lon, 4)))
                    lon += self.grid_step
                lat += self.grid_step
            return puntos
            
        buffered = set()
        for lat_idx, lon_idx in existing_cells:
            for dlat in range(-1, 2):
                for dlon in range(-1, 2):
                    buffered.add((lat_idx + dlat, lon_idx + dlon))
                    
        puntos = [(lat_idx + 0.5, lon_idx + 0.5) for lat_idx, lon_idx in buffered]
        random.shuffle(puntos)
        logger.info(f"[osm] Generados {len(puntos)} puntos mundiales activos para Overpass.")
        return puntos

    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
        from db import (find_spot_cercano, crear_spot, enriquecer_spot,
                        upsert_source_record, finish_scraper_log, update_fuente_config)
        inicio = datetime.now(timezone.utc)
        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
                 "errores": 0, "iniciado_en": inicio, "detalle": {}}

        puntos = await self._generate_active_points(pool)
        logger.info(f"[osm] {len(puntos)} puntos GPS")
        seen_ids: set[str] = set()
        # Defensivo: config.max_workers puede ser None. Concurrencia baja para
        # Overpass (es un servicio compartido y agresivamente protegido contra abuso)
        mw = getattr(config, 'max_workers', None) or 3
        sem = asyncio.Semaphore(max(1, mw // 2))
        circuit_breaker = 0

        headers = {
            "User-Agent": "GeoSpots Scraper/1.0 (admin@geospots.local)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"
        }
        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            async def procesar(lat, lon):
                nonlocal circuit_breaker
                if circuit_breaker >= 5:
                    return
                async with sem:
                    await asyncio.sleep(self.rate_limit)
                    try:
                        q = OVERPASS_QUERY.format(radio=60000, lat=lat, lon=lon)
                        data = await _fetch_overpass(client, q)
                        circuit_breaker = 0
                    except Exception as e:
                        circuit_breaker += 1
                        logger.error(f"[osm] Error {lat},{lon}: {e}")
                        stats["errores"] += 1
                        return

                    for elem in data.get("elements", []):
                        norm = self.normalize(elem)
                        if not norm:
                            continue
                        if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                            continue
                        sid = norm["source_id"]
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                        try:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    ex = await find_spot_cercano(conn, norm["lat"], norm["lon"], self.dedup_radius_m)
                                    if ex:
                                        await enriquecer_spot(conn, ex["id"], norm, self.name)
                                        stats["actualizados"] += 1
                                        spot_id = ex["id"]
                                    else:
                                        norm["fuentes"] = [self.name]
                                        spot_id = await crear_spot(conn, norm)
                                        stats["nuevos"] += 1
                                    await upsert_source_record(conn, spot_id, self.name, sid, elem, norm)
                        except Exception as e:
                            logger.error(f"[osm] DB {sid}: {e}")
                            stats["errores"] += 1

            LOTE = 20
            for i in range(0, len(puntos), LOTE):
                if circuit_breaker >= 5:
                    logger.error("[osm] Circuit breaker activado. Abortando.")
                    break
                batch = puntos[i:i+LOTE]
                await asyncio.gather(*[procesar(lat, lon) for lat, lon in batch])
                logger.info(f"[osm] {min(i+LOTE, len(puntos))}/{len(puntos)} | "
                            f"uniq={len(seen_ids)} new={stats['nuevos']} err={stats['errores']}")
                await self.update_job_progress(
                    pool, job_id, min(i + LOTE, len(puntos)), len(puntos), stats
                )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)
        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[osm] Completado en {dur:.0f}s | {stats}")
        return stats
