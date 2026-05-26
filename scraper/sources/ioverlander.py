"""iOverlander — importación offline desde fichero KMZ + descarga autenticada de check-ins.

Fase 1 (activa): import KMZ offline → spots + descriptions.
Fase 2 (requiere cuenta): download_reviews autenticado:
  - Configura IOV_EMAIL y IOV_PASSWORD en .env
  - El método hace login via CSRF token, luego:
    1. Para cada spot de iOverlander busca su UUID en /places.json?lat=&lng=
    2. Descarga /places/{uuid}.json con los check-ins (comentarios de usuarios)
    3. Inserta cada check-in como review en la tabla reviews

Nota: iOverlander no tiene API pública. El endpoint .json requiere sesión autenticada.
iOverlander 2.0 (2025+) puede requerir suscripción premium para acceso completo.
"""

import asyncio
import os
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

KMZ_PATH_DEFAULT = "data/ioverlander.kmz"

STYLE_MAP = {
    "#stylexhD0": "camping",          # Established Campground
    "#stylexhD1": "parking",          # Informal Campsite (typically parking/rest areas)
    "#stylexhD3": "wild",             # Wild Camping
    "#stylexhD5": "gasolinera",       # Fuel Station
    "#stylexhD6": "area_ac",          # Sanitation Dump Station
    "#stylexhD7": "otro",             # Showers
    "#stylexhD2": "wild",             # Warning / Closed -> default to wild
}

EXCLUDED_STYLES = {
    "#stylexhD11", # SIM Card
    "#stylexhD9",  # Ferry Terminal / Shipping
    "#stylexhD4",  # Bank / ATM
    "#stylexhD8",  # Insurance
    "#stylexhD10", # Long-term storage
}

KW_AGUA = ["water", "agua", "wasser", "eau", "acqua", "potable"]
KW_ELECTR = ["electricity", "electric", "electr", "corriente", "strom"]
KW_WIFI = ["wifi", "wi-fi", "internet"]
KW_DUCHA = ["shower", "showers", "ducha", "douche", "duschen"]
KW_WC = ["toilet", "wc", "baño", "toilette", "sanitär"]
KW_PERROS = ["dog", "dogs", "perro", "hund", "animal", "pet"]
KW_GRATIS = ["free", "gratis", "kostenlos", "gratuit"]


def clean_desc_text(desc_text: str) -> str | None:
    if not desc_text:
        return None
    match_desc = re.search(r'desc:[a-z]{2}=(.*)', desc_text, re.DOTALL | re.IGNORECASE)
    if match_desc:
        clean_desc = match_desc.group(1).strip()
    else:
        clean_desc = re.sub(r'^name:[a-z]{2}=.*?\n+', '', desc_text, flags=re.IGNORECASE | re.DOTALL).strip()
    
    clean_desc = re.split(r'<[Bb][Rr]/?>', clean_desc)[0].strip()
    clean_desc = re.sub(r'<[^>]+>', '', clean_desc).strip()
    if "download the ioverlander app" in clean_desc.lower():
        return None
    return clean_desc if clean_desc else None


def map_type(style: str, name: str) -> str | None:
    name_lower = (name or "").lower()
    if style in EXCLUDED_STYLES:
        return None
    base_type = STYLE_MAP.get(style, "otro")
    if base_type in ("parking", "wild", "otro"):
        if any(kw in name_lower for kw in ["camping", "campsite", "campground"]):
            return "camping"
        if any(kw in name_lower for kw in ["parking", "michi-no-eki", "truck stop", "stationnement"]):
            return "parking"
        if any(kw in name_lower for kw in ["aire", "motorhome", "rv park", "dump station"]):
            return "area_ac"
    return base_type


def _inferir_bool(desc: str, keywords: list[str]) -> bool | None:
    if not desc:
        return None
    desc_lower = desc.lower()
    for kw in keywords:
        if kw in desc_lower:
            return True
    return None


def _parsear_atributos(desc_text: str, style: str) -> dict:
    attrs = {
        "agua_potable": None,
        "electricidad": None,
        "wifi": None,
        "ducha": None,
        "wc_publico": None,
        "perros": None,
        "acceso_grandes": None,
        "gratuito": None,
        "advertencia": None
    }
    
    if style == "#stylexhD2":
        attrs["advertencia"] = "⚠️ Lugar cerrado o prohibido según iOverlander"

    if not desc_text:
        return attrs

    parts = re.split(r'<[Bb][Rr]/?>', desc_text)
    in_amenities = False
    amenities = {}
    tags = set()
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "Amenities" in part or "<b>Amenities</b>" in part.lower():
            in_amenities = True
            continue
        if in_amenities:
            if "<font" in part.lower() or "text_color" in part.lower():
                in_amenities = False
                break
            
            clean_part = re.sub(r'<[^>]+>', '', part).strip()
            if not clean_part:
                continue
            
            match = re.match(r'^([^:]+):\s*(.*)$', clean_part)
            if match:
                k, v = match.group(1).strip(), match.group(2).strip()
                amenities[k.lower()] = v.lower()
            else:
                tags.add(clean_part.lower())

    if "water" in amenities:
        attrs["agua_potable"] = "potable" in amenities["water"]
    elif "water" in tags:
        attrs["agua_potable"] = True
        
    if "electricity" in amenities:
        attrs["electricidad"] = "yes" in amenities["electricity"]
        
    if "wi-fi" in amenities:
        attrs["wifi"] = amenities["wi-fi"].startswith("yes")
        
    if "toilets" in amenities:
        attrs["wc_publico"] = any(x in amenities["toilets"] for x in ["running", "pit", "yes"])
    elif "toilets" in tags:
        attrs["wc_publico"] = True
        
    if "showers" in amenities:
        attrs["ducha"] = any(x in amenities["showers"] for x in ["hot", "warm", "cold", "bucket", "yes"])
    elif "showers" in tags:
        attrs["ducha"] = True

    if "pet friendly" in tags:
        attrs["perros"] = True
    if "big rig friendly" in tags:
        attrs["acceso_grandes"] = True

    clean_desc = clean_desc_text(desc_text)
    if clean_desc:
        clean_desc_lower = clean_desc.lower()
        if attrs["agua_potable"] is None:
            attrs["agua_potable"] = _inferir_bool(clean_desc_lower, KW_AGUA)
        if attrs["electricidad"] is None:
            attrs["electricidad"] = _inferir_bool(clean_desc_lower, KW_ELECTR)
        if attrs["wifi"] is None:
            attrs["wifi"] = _inferir_bool(clean_desc_lower, KW_WIFI)
        if attrs["ducha"] is None:
            attrs["ducha"] = _inferir_bool(clean_desc_lower, KW_DUCHA)
        if attrs["wc_publico"] is None:
            attrs["wc_publico"] = _inferir_bool(clean_desc_lower, KW_WC)
        if attrs["perros"] is None:
            attrs["perros"] = _inferir_bool(clean_desc_lower, KW_PERROS)
        if attrs["gratuito"] is None:
            attrs["gratuito"] = _inferir_bool(clean_desc_lower, KW_GRATIS)

    return attrs


class IOverlanderSource(AbstractSource):
    """iOverlander: import offline desde KMZ, no usa grid ni API."""

    name = "ioverlander"
    rate_limit = 0
    dedup_radius_m = 100.0

    # No usa fetch_cell
    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("iOverlander usa import offline")

    def normalize(self, raw: dict) -> dict | None:
        """Normaliza un placemark parseado del KMZ."""
        nombre = raw.get("nombre", "Sin nombre")
        lat = raw.get("lat")
        lon = raw.get("lon")
        if lat is None or lon is None:
            return None

        style = raw.get("style_url", "")
        tipo = map_type(style, nombre)
        if tipo is None:
            return None

        desc = raw.get("descripcion", "")
        iov_id = raw.get("iov_id", f"{lat:.6f}_{lon:.6f}")
        attrs = _parsear_atributos(desc, style)

        res = {
            "source_id": iov_id,
            "nombre": nombre,
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "descripcion_en": clean_desc_text(desc),
            "agua_potable": attrs["agua_potable"],
            "electricidad": attrs["electricidad"],
            "wifi": attrs["wifi"],
            "ducha": attrs["ducha"],
            "wc_publico": attrs["wc_publico"],
            "perros": attrs["perros"],
            "acceso_grandes": attrs["acceso_grandes"],
            "gratuito": attrs["gratuito"],
        }
        if attrs["advertencia"]:
            res["advertencia"] = attrs["advertencia"]
        return res

    @staticmethod
    def parsear_kmz(kmz_path: str) -> list[dict]:
        """Parsea KMZ y devuelve lista de dicts raw por placemark (a nivel mundial)."""
        try:
            with zipfile.ZipFile(kmz_path, 'r') as z:
                kml_filename = next(
                    (n for n in z.namelist() if n.endswith('.kml')), None
                )
                if not kml_filename:
                    logger.error("No se encontró .kml dentro del .kmz")
                    return []
                with z.open(kml_filename) as f:
                    tree = ET.parse(f)
                    root = tree.getroot()
        except Exception as e:
            logger.error(f"Error abriendo KMZ: {e}")
            return []

        # Eliminar namespaces
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]

        total, fuera = 0, 0
        items = []

        for pm in root.findall('.//Placemark'):
            total += 1
            coords_node = pm.find('.//coordinates')
            if coords_node is None or not coords_node.text:
                continue

            parts = coords_node.text.strip().split(',')
            if len(parts) < 2:
                continue
            try:
                lon = float(parts[0])
                lat = float(parts[1])
            except ValueError:
                continue

            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                fuera += 1
                continue

            name_node = pm.find('name')
            nombre = (name_node.text or "Sin nombre").strip()[:200] if name_node is not None else "Sin nombre"

            desc_node = pm.find('description')
            desc = (desc_node.text or "").strip() if desc_node is not None else ""

            style_node = pm.find('styleUrl')
            style_url = style_node.text.strip() if style_node is not None else ""

            items.append({
                "iov_id": f"{lat:.6f}_{lon:.6f}",
                "nombre": nombre,
                "lat": lat,
                "lon": lon,
                "descripcion": desc,
                "style_url": style_url,
            })

        logger.info(f"KMZ: {total} puntos procesados, {fuera} fuera de coordenadas, {len(items)} para importar")
        return items

    async def run(self, pool, config, log_id: int) -> dict:
        """Override completo: lee KMZ offline, no usa API. Optimizado en transacciones y conexiones."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        kmz_path = os.environ.get("IOV_KMZ_PATH", KMZ_PATH_DEFAULT)
        if not os.path.exists(kmz_path):
            logger.error(f"KMZ no encontrado: {kmz_path}")
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        logger.info(f"[ioverlander] KMZ: {kmz_path} ({os.path.getsize(kmz_path)/1e6:.1f} MB)")

        raw_items = self.parsear_kmz(kmz_path)
        if not raw_items:
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        logger.info(f"[ioverlander] {len(raw_items)} placemarks a procesar")

        BATCH_SIZE = 1000
        # Usamos una sola conexión persistente para evitar la latencia de adquirir del pool en cada bucle
        async with pool.acquire() as conn:
            batch = []
            for i, raw in enumerate(raw_items):
                norm = self.normalize(raw)
                if not norm:
                    continue
                if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                    continue
                batch.append((raw, norm))

                if len(batch) >= BATCH_SIZE or i == len(raw_items) - 1:
                    try:
                        async with conn.transaction():
                            for b_raw, b_norm in batch:
                                sid = b_norm["source_id"]
                                existente = await find_spot_cercano(
                                    conn, b_norm["lat"], b_norm["lon"], self.dedup_radius_m,
                                    nombre=b_norm.get("nombre"), tipo=b_norm.get("tipo")
                                )

                                if existente:
                                    spot_id = existente["id"]
                                    await enriquecer_spot(conn, spot_id, b_norm, self.name)
                                    if "advertencia" in b_norm:
                                        await conn.execute(
                                            "UPDATE spots SET advertencia = $1, updated_at = NOW() WHERE id = $2",
                                            b_norm["advertencia"], spot_id
                                        )
                                    stats["actualizados"] += 1
                                else:
                                    b_norm["fuentes"] = [self.name]
                                    spot_id = await crear_spot(conn, b_norm)
                                    if "advertencia" in b_norm:
                                        await conn.execute(
                                            "UPDATE spots SET advertencia = $1, updated_at = NOW() WHERE id = $2",
                                            b_norm["advertencia"], spot_id
                                        )
                                    stats["nuevos"] += 1

                                await upsert_source_record(
                                    conn, spot_id, self.name, sid,
                                    b_raw, b_norm
                                )
                    except Exception as batch_err:
                        logger.warning(f"[ioverlander] Error en batch, reintentando individualmente: {batch_err}")
                        for b_raw, b_norm in batch:
                            sid = b_norm["source_id"]
                            try:
                                async with conn.transaction():
                                    existente = await find_spot_cercano(
                                        conn, b_norm["lat"], b_norm["lon"], self.dedup_radius_m,
                                        nombre=b_norm.get("nombre"), tipo=b_norm.get("tipo")
                                    )

                                    if existente:
                                        spot_id = existente["id"]
                                        await enriquecer_spot(conn, spot_id, b_norm, self.name)
                                        if "advertencia" in b_norm:
                                            await conn.execute(
                                                "UPDATE spots SET advertencia = $1, updated_at = NOW() WHERE id = $2",
                                                b_norm["advertencia"], spot_id
                                            )
                                        stats["actualizados"] += 1
                                    else:
                                        b_norm["fuentes"] = [self.name]
                                        spot_id = await crear_spot(conn, b_norm)
                                        if "advertencia" in b_norm:
                                            await conn.execute(
                                                "UPDATE spots SET advertencia = $1, updated_at = NOW() WHERE id = $2",
                                                b_norm["advertencia"], spot_id
                                            )
                                        stats["nuevos"] += 1

                                    await upsert_source_record(
                                        conn, spot_id, self.name, sid,
                                        b_raw, b_norm
                                    )
                            except Exception as single_err:
                                logger.error(f"[ioverlander] Error individual '{b_norm.get('nombre')}': {single_err}")
                                stats["errores"] += 1

                    batch = []

                if (i + 1) % 10000 == 0:
                    logger.info(
                        f"[ioverlander] {i+1}/{len(raw_items)} | "
                        f"new={stats['nuevos']} upd={stats['actualizados']} err={stats['errores']}"
                    )


        # Cross-reference warnings: marcar spots de OTRAS fuentes que estén a menos de 100m de un prohibido/cerrado de iOverlander
        logger.info("[ioverlander] Aplicando advertencias cruzadas para spots adyacentes a prohibidos...")
        try:
            async with pool.acquire() as conn:
                res_cross = await conn.execute("""
                    WITH prohibidos AS (
                        SELECT id, geog FROM spots
                        WHERE advertencia LIKE '%%prohibido%%iOverlander%%' OR advertencia LIKE '%%cerrado%%iOverlander%%'
                    )
                    UPDATE spots AS vecino SET
                        advertencia = '⚠️ Spot cercano a lugar cerrado o prohibido según iOverlander'
                    FROM prohibidos p
                    WHERE vecino.id != p.id
                      AND vecino.advertencia IS NULL
                      AND NOT ('ioverlander' = ANY(vecino.fuentes))
                      AND ST_DWithin(vecino.geog, p.geog, 100)
                """)
                logger.info(f"[ioverlander] Proceso finalizado. {res_cross}")
        except Exception as e:
            logger.warning(f"[ioverlander] Error al aplicar advertencias cruzadas: {e}")

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[ioverlander] Completado en {dur:.0f}s | {stats}")
        return stats

    # ─────────────────────────────────────────────────────────────────
    # FASE 2: Descarga autenticada de check-ins como reviews
    # Requiere: IOV_EMAIL y IOV_PASSWORD en entorno/.env
    # ─────────────────────────────────────────────────────────────────

    async def download_reviews(self, pool, config) -> dict:
        """Descarga check-ins de iOverlander como reviews mediante sesión autenticada.

        Prerrequisitos:
            IOV_EMAIL    — email de la cuenta iOverlander
            IOV_PASSWORD — contraseña de la cuenta iOverlander

        Flujo:
            1. GET /users/sign_in  → extrae CSRF token del HTML
            2. POST /users/sign_in → autentica y obtiene cookie de sesión
            3. Para cada spot de iOverlander en DB:
               a. GET /places.json?lat=&lng=&radius_m=50  → encuentra UUID
               b. GET /places/{uuid}.json                  → descarga check-ins
               c. Inserta cada check-in como review
        """
        from db import upsert_review

        email = os.environ.get("IOV_EMAIL", "").strip()
        password = os.environ.get("IOV_PASSWORD", "").strip()

        if not email or not password:
            logger.warning(
                "[ioverlander] download_reviews requiere IOV_EMAIL e IOV_PASSWORD. "
                "Configúralos en .env para activar la descarga de check-ins."
            )
            return {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0,
                    "skipped": "no_credentials"}

        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}

        BASE_URL = "https://ioverlander.com"
        HEADERS = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.5",
        }
        JSON_HEADERS = {
            **HEADERS,
            "accept": "application/json",
            "x-requested-with": "XMLHttpRequest",
        }

        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=20,
            cookies=httpx.Cookies(),
        ) as client:

            # ── PASO 1: Obtener CSRF token ──────────────────────────────────
            logger.info("[ioverlander] Obteniendo CSRF token del formulario de login...")
            try:
                resp = await client.get(f"{BASE_URL}/users/sign_in")
                resp.raise_for_status()
                csrf_match = re.search(
                    r'<meta name="csrf-token" content="([^"]+)"', resp.text
                )
                if not csrf_match:
                    # Fallback: buscar en el formulario
                    csrf_match = re.search(
                        r'name="authenticity_token" value="([^"]+)"', resp.text
                    )
                if not csrf_match:
                    logger.error("[ioverlander] No se encontró CSRF token. "
                                 "Estructura HTML puede haber cambiado.")
                    stats["errores"] += 1
                    return stats
                csrf_token = csrf_match.group(1)
                logger.info(f"[ioverlander] CSRF token obtenido: {csrf_token[:20]}...")
            except Exception as e:
                logger.error(f"[ioverlander] Error obteniendo CSRF token: {e}")
                stats["errores"] += 1
                return stats

            # ── PASO 2: Autenticar ──────────────────────────────────────────
            logger.info(f"[ioverlander] Autenticando con {email}...")
            try:
                login_resp = await client.post(
                    f"{BASE_URL}/users/sign_in",
                    data={
                        "authenticity_token": csrf_token,
                        "user[email]": email,
                        "user[password]": password,
                        "user[remember_me]": "0",
                        "commit": "Log in",
                    },
                    headers={**HEADERS, "content-type": "application/x-www-form-urlencoded",
                             "referer": f"{BASE_URL}/users/sign_in"},
                )
                # Login exitoso → redirige a / o /explore (no a /sign_in)
                if "/sign_in" in str(login_resp.url):
                    logger.error(
                        "[ioverlander] Login fallido. Verifica IOV_EMAIL e IOV_PASSWORD. "
                        f"URL final: {login_resp.url}"
                    )
                    stats["errores"] += 1
                    return stats
                logger.info(f"[ioverlander] Login OK. URL final: {login_resp.url}")
            except Exception as e:
                logger.error(f"[ioverlander] Error en login: {e}")
                stats["errores"] += 1
                return stats

            # Actualizar headers para requests JSON autenticados
            client.headers.update({"accept": "application/json",
                                    "x-requested-with": "XMLHttpRequest"})

            # ── PASO 3: Cargar spots pendientes de reviews ──────────────────
            async with pool.acquire() as conn:
                jobs = await conn.fetch("""
                    SELECT sr.spot_id, sr.source_id, sr.lat, sr.lon,
                           COALESCE(r.cnt, 0) as db_review_count
                    FROM source_records sr
                    LEFT JOIN (
                        SELECT spot_id, COUNT(*) as cnt
                        FROM reviews
                        WHERE source = 'ioverlander'
                        GROUP BY spot_id
                    ) r ON sr.spot_id = r.spot_id
                    WHERE sr.source = 'ioverlander'
                      AND (sr.normalized_data->>'checkins_fetched') IS NULL
                    ORDER BY sr.spot_id
                """)

            total = len(jobs)
            logger.info(f"[ioverlander] {total} spots pendientes de check-ins.")
            if not jobs:
                return stats

            # ── PASO 4: Para cada spot, buscar UUID y descargar check-ins ───
            for i, job in enumerate(jobs):
                spot_id = job["spot_id"]
                lat = job["lat"]
                lon = job["lon"]

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"[ioverlander] Progreso: {i+1}/{total} | "
                        f"reviews={stats['reviews_nuevas']} errores={stats['errores']}"
                    )

                await asyncio.sleep(self.rate_limit or 1.0)

                # 4a. Buscar UUID del lugar por coordenadas
                place_uuid = None
                try:
                    search_resp = await client.get(
                        f"{BASE_URL}/places.json",
                        params={"lat": f"{lat:.6f}", "lng": f"{lon:.6f}"},
                        timeout=15,
                    )
                    if search_resp.status_code == 200:
                        places_data = search_resp.json()
                        # La API devuelve lista de lugares cercanos
                        places_list = places_data if isinstance(places_data, list) \
                            else places_data.get("places", [])
                        if places_list:
                            # Tomar el más cercano (primero)
                            place_uuid = places_list[0].get("id") or \
                                         places_list[0].get("uuid")
                    elif search_resp.status_code == 401:
                        logger.error("[ioverlander] Sesión expirada (401). Abortando.")
                        stats["errores"] += 1
                        break
                    else:
                        logger.debug(
                            f"[ioverlander] Search {search_resp.status_code} "
                            f"para ({lat:.4f},{lon:.4f})"
                        )
                except Exception as e:
                    logger.debug(f"[ioverlander] Error buscando UUID para spot {spot_id}: {e}")
                    stats["errores"] += 1
                    continue

                if not place_uuid:
                    # Marcar como procesado aunque no encontremos UUID
                    # para no reintentar infinitamente
                    try:
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                UPDATE source_records
                                SET normalized_data = normalized_data ||
                                    '{"checkins_fetched": true, "uuid_not_found": true}'::jsonb
                                WHERE source = 'ioverlander' AND spot_id = $1
                            """, spot_id)
                    except Exception:
                        pass
                    continue

                await asyncio.sleep(self.rate_limit or 1.0)

                # 4b. Descargar detalle del lugar (incluye check-ins)
                try:
                    place_resp = await client.get(
                        f"{BASE_URL}/places/{place_uuid}.json",
                        timeout=15,
                    )
                    if place_resp.status_code != 200:
                        logger.debug(
                            f"[ioverlander] Place detail {place_resp.status_code} "
                            f"para UUID {place_uuid}"
                        )
                        stats["errores"] += 1
                        continue
                    place_data = place_resp.json()
                except Exception as e:
                    logger.debug(f"[ioverlander] Error descargando place {place_uuid}: {e}")
                    stats["errores"] += 1
                    continue

                # 4c. Parsear check-ins como reviews
                check_ins = place_data.get("check_ins") or \
                            place_data.get("checkins") or \
                            place_data.get("verifications") or []

                if check_ins:
                    try:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                for ci in check_ins:
                                    # Texto del check-in
                                    texto = (
                                        ci.get("description") or
                                        ci.get("comment") or
                                        ci.get("body") or
                                        ""
                                    ).strip() or None

                                    # Rating (iOverlander usa 1-5 o thumb up/down)
                                    rating = None
                                    rating_raw = ci.get("rating") or ci.get("score")
                                    if rating_raw is not None:
                                        try:
                                            rating = float(rating_raw)
                                        except (ValueError, TypeError):
                                            pass

                                    # Fecha
                                    fecha = None
                                    fecha_str = (
                                        ci.get("created_at") or
                                        ci.get("date") or
                                        ci.get("visited_at") or
                                        ""
                                    )
                                    if fecha_str:
                                        try:
                                            fecha = datetime.fromisoformat(
                                                fecha_str.replace("Z", "+00:00")
                                            )
                                            if fecha.tzinfo is None:
                                                fecha = fecha.replace(tzinfo=timezone.utc)
                                        except Exception:
                                            try:
                                                fecha = datetime.strptime(
                                                    fecha_str[:10], "%Y-%m-%d"
                                                ).replace(tzinfo=timezone.utc)
                                            except Exception:
                                                pass

                                    # Autor
                                    user = ci.get("user") or {}
                                    autor = (
                                        user.get("username") or
                                        user.get("name") or
                                        ci.get("username") or
                                        "iOverlander User"
                                    )

                                    # ID único del check-in
                                    ci_id = ci.get("id") or ci.get("uuid") or ""
                                    source_review_id = f"iov_{ci_id}" if ci_id else None

                                    # Saltar si no tiene texto ni rating
                                    if texto is None and rating is None:
                                        continue

                                    if source_review_id is None:
                                        continue

                                    rev_dict = {
                                        "spot_id": spot_id,
                                        "source": self.name,
                                        "source_review_id": source_review_id,
                                        "texto": texto,
                                        "rating": rating,
                                        "autor": autor,
                                        "fecha": fecha,
                                        "idioma": None,  # multilingüe
                                    }
                                    inserted = await upsert_review(conn, rev_dict)
                                    stats["reviews_nuevas"] += int(bool(inserted))

                        stats["actualizados"] += 1
                    except Exception as e:
                        logger.error(
                            f"[ioverlander] Error insertando check-ins para spot {spot_id}: {e}"
                        )
                        stats["errores"] += 1

                # Marcar como procesado
                try:
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE source_records
                            SET normalized_data = normalized_data ||
                                jsonb_build_object(
                                    'checkins_fetched', true,
                                    'iov_uuid', $1::text,
                                    'checkins_count', $2::int
                                )
                            WHERE source = 'ioverlander' AND spot_id = $3
                        """, str(place_uuid), len(check_ins), spot_id)
                except Exception as e:
                    logger.debug(f"[ioverlander] Error marcando checkins_fetched: {e}")

        logger.info(
            f"[ioverlander] download_reviews completado: "
            f"reviews_nuevas={stats['reviews_nuevas']} "
            f"errores={stats['errores']}"
        )
        return stats
