"""iOverlander — importación offline desde fichero KMZ."""

import os
import json
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from loguru import logger

from sources.base import AbstractSource

import re

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
