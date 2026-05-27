"""AmigosAC — importación offline desde fichero KMZ."""

import os
import json
import zipfile
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from loguru import logger

from sources.base import AbstractSource
from sources._normalize_helpers import extract_amigosac, merge_extra

KMZ_PATH_DEFAULT = "data/amigosac.kmz"

# Mapeo de estilos a tipos de spot válidos en GeoSpots
STYLE_MAP = {
    # Areas de Autocaravanas (area_ac)
    "#icon-ci-1": "area_ac",
    "#icon-ci-1-nodesc": "area_ac",
    "#icon-ci-4": "area_ac",
    "#icon-ci-4-nodesc": "area_ac",
    "#icon-ci-3": "area_ac",
    "#icon-ci-3-nodesc": "area_ac",
    "#icon-ci-6": "area_ac",
    "#icon-ci-6-nodesc": "area_ac",
    "#icon-ci-9": "area_ac",
    "#icon-ci-9-nodesc": "area_ac",
    "#icon-ci-25": "area_ac",
    "#icon-ci-25-nodesc": "area_ac",
    "#icon-ci-26": "area_ac",
    "#icon-ci-26-nodesc": "area_ac",
    "#icon-ci-27": "area_ac",
    "#icon-ci-27-nodesc": "area_ac",
    "#icon-ci-28": "area_ac",
    "#icon-ci-28-nodesc": "area_ac",
    "#icon-1644-3949AB": "area_ac",
    "#icon-1644-3949AB-nodesc": "area_ac",

    # Parkings (parking)
    "#icon-1644-1A237E": "parking",
    "#icon-1644-1A237E-nodesc": "parking",
    "#icon-1644-C2185B": "parking",
    "#icon-1644-C2185B-nodesc": "parking",
    "#icon-ci-23": "parking",
    "#icon-ci-23-nodesc": "parking",
    "#icon-ci-24": "parking",
    "#icon-ci-24-nodesc": "parking",
    "#icon-ci-13": "parking",
    "#icon-ci-13-nodesc": "parking",

    # Gasolineras (gasolinera)
    "#icon-ci-7": "gasolinera",
    "#icon-ci-7-nodesc": "gasolinera",
    "#icon-ci-14": "gasolinera",
    "#icon-ci-14-nodesc": "gasolinera",
    "#icon-ci-20": "gasolinera",

    # Campings (camping)
    "#icon-1765-097138": "camping",
    "#icon-1765-097138-nodesc": "camping",
    "#icon-1765-0288D1": "camping",

    # Otros (laundries, shops, workshops, etc.)
    "#icon-ci-16": "otro",
    "#icon-ci-16-nodesc": "otro",
    "#icon-ci-17": "otro",
    "#icon-ci-17-nodesc": "otro",
    "#icon-ci-18": "otro",
    "#icon-ci-18-nodesc": "otro",
    "#icon-ci-19": "otro",
    "#icon-ci-19-nodesc": "otro",

    # Warnings / Restricciones
    "#icon-ci-8": "parking",
    "#icon-ci-8-nodesc": "parking",
    "#icon-ci-10": "parking",
    "#icon-ci-10-nodesc": "parking",
    "#icon-ci-11": "parking",
    "#icon-ci-11-nodesc": "parking",
}

WARNING_STYLES = {
    "#icon-ci-8", "#icon-ci-8-nodesc",
    "#icon-ci-10", "#icon-ci-10-nodesc",
    "#icon-ci-11", "#icon-ci-11-nodesc",
}

# Palabras clave para inferencia en español/portugués/inglés
KW_AGUA = ["agua", "llenado", "carga", "potable", "water", "wasser"]
KW_ELECTR = ["luz", "electricidad", "corriente", "toma", "enchufe", "electricity", "strom"]
KW_WIFI = ["wifi", "wi-fi", "internet"]
KW_DUCHA = ["ducha", "duchas", "shower", "douche", "duschen"]
KW_WC = ["wc", "baño", "baños", "aseo", "aseos", "toilet", "toilets", "sanitär"]
KW_PERROS = ["perro", "perros", "mascota", "mascotas", "animal", "animales", "dog", "dogs"]
KW_GRATIS = ["gratis", "gratuito", "gratuita", "libre", "0 euro", "sin costo", "free"]
KW_NEGRAS = ["negras", "aguas negras", "poty", "poti", "wc químico", "quimico"]
KW_GRISES = ["grises", "aguas grises", "descarga grises", "vaciado grises"]


def clean_desc_text(desc_text: str) -> str | None:
    if not desc_text:
        return None
    # Eliminar imágenes y tags HTML
    clean_desc = re.sub(r'<img[^>]+>', '', desc_text)
    clean_desc = re.sub(r'<[^>]+>', '\n', clean_desc)
    # Limpiar espacios continuos y no breaking spaces
    clean_desc = clean_desc.replace('\xa0', ' ')
    lines = [line.strip() for line in clean_desc.split('\n') if line.strip()]
    clean_desc = ' '.join(lines).strip()
    return clean_desc if clean_desc else None


def _inferir_bool(desc: str, keywords: list[str]) -> bool | None:
    if not desc:
        return None
    desc_lower = desc.lower()
    for kw in keywords:
        if kw in desc_lower:
            idx = desc_lower.find(kw)
            context = desc_lower[max(0, idx - 15):idx]
            if any(neg in context for neg in ["no hay", "sin", "no tiene", "no dispon", "no exist", "no hay"]):
                return False
            return True
    return None


def _parsear_atributos(desc_text: str, style: str) -> dict:
    attrs = {
        "agua_potable": None,
        "electricidad": None,
        "wifi": None,
        "ducha": None,
        "wc_publico": None,
        "vaciado_negras": None,
        "vaciado_grises": None,
        "perros": None,
        "gratuito": None,
        "advertencia": None
    }

    clean_desc = clean_desc_text(desc_text)

    if style in WARNING_STYLES:
        if clean_desc and len(clean_desc) < 150:
            attrs["advertencia"] = f"⚠️ {clean_desc}"
        else:
            attrs["advertencia"] = "⚠️ Lugar con restricciones de pernocta, prohibición municipal o no recomendado por AmigosAC"

    if clean_desc:
        attrs["agua_potable"] = _inferir_bool(clean_desc, KW_AGUA)
        attrs["electricidad"] = _inferir_bool(clean_desc, KW_ELECTR)
        attrs["wifi"] = _inferir_bool(clean_desc, KW_WIFI)
        attrs["ducha"] = _inferir_bool(clean_desc, KW_DUCHA)
        attrs["wc_publico"] = _inferir_bool(clean_desc, KW_WC)
        attrs["vaciado_negras"] = _inferir_bool(clean_desc, KW_NEGRAS)
        attrs["vaciado_grises"] = _inferir_bool(clean_desc, KW_GRISES)
        attrs["perros"] = _inferir_bool(clean_desc, KW_PERROS)
        attrs["gratuito"] = _inferir_bool(clean_desc, KW_GRATIS)

    return attrs


class AmigosACSource(AbstractSource):
    """AmigosAC: importación offline desde fichero KMZ (España/Portugal)."""

    name = "amigosac"
    rate_limit = 0
    dedup_radius_m = 100.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("AmigosAC usa import offline")

    def normalize(self, raw: dict) -> dict | None:
        """Normaliza un placemark parseado de AmigosAC."""
        nombre = raw.get("nombre", "Sin nombre")
        lat = raw.get("lat")
        lon = raw.get("lon")
        if lat is None or lon is None:
            return None

        style = raw.get("style_url", "")
        tipo = STYLE_MAP.get(style, "otro")

        desc = raw.get("descripcion", "")
        amigos_id = raw.get("amigos_id", f"{lat:.6f}_{lon:.6f}")
        attrs = _parsear_atributos(desc, style)

        res = {
            "source_id": amigos_id,
            "nombre": nombre,
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "descripcion_es": clean_desc_text(desc),
            "agua_potable": attrs["agua_potable"],
            "electricidad": attrs["electricidad"],
            "wifi": attrs["wifi"],
            "ducha": attrs["ducha"],
            "wc_publico": attrs["wc_publico"],
            "vaciado_negras": attrs["vaciado_negras"],
            "vaciado_grises": attrs["vaciado_grises"],
            "perros": attrs["perros"],
            "gratuito": attrs["gratuito"],
        }
        if attrs["advertencia"]:
            res["advertencia"] = attrs["advertencia"]
        return merge_extra(res, extract_amigosac(raw))

    @staticmethod
    def parsear_kmz(kmz_path: str) -> list[dict]:
        """Parsea el archivo KMZ de AmigosAC."""
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

        # Quitar namespaces para facilitar xpath simple
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]

        total, fuera = 0, 0
        items = []

        for pm in root.findall('.//Placemark'):
            # Ignorar rutas/direcciones que no sean puntos específicos
            point_node = pm.find('.//Point')
            if point_node is None:
                continue

            total += 1
            coords_node = point_node.find('.//coordinates')
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

            # Normalizar encoding si hay caracteres corruptos en KML de Google My Maps
            try:
                nombre = nombre.encode('latin1').decode('utf-8', errors='replace')
            except Exception:
                pass
            try:
                desc = desc.encode('latin1').decode('utf-8', errors='replace')
            except Exception:
                pass

            items.append({
                "amigos_id": f"{lat:.6f}_{lon:.6f}",
                "nombre": nombre,
                "lat": lat,
                "lon": lon,
                "descripcion": desc,
                "style_url": style_url,
            })

        logger.info(f"KMZ: {total} puntos procesados, {fuera} fuera de coordenadas, {len(items)} para importar")
        return items

    async def run(self, pool, config, log_id: int) -> dict:
        """Pipeline de importación offline optimizado."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        kmz_path = os.environ.get("AMIGOSAC_KMZ_PATH", KMZ_PATH_DEFAULT)
        if not os.path.exists(kmz_path):
            logger.error(f"KMZ de AmigosAC no encontrado: {kmz_path}")
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        logger.info(f"[amigosac] Leyendo KMZ: {kmz_path} ({os.path.getsize(kmz_path)/1e6:.1f} MB)")

        raw_items = self.parsear_kmz(kmz_path)
        if not raw_items:
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        logger.info(f"[amigosac] {len(raw_items)} placemarks listos para procesar")

        BATCH_SIZE = 1000
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
                        logger.warning(f"[amigosac] Error en lote, reintentando individualmente: {batch_err}")
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
                                logger.error(f"[amigosac] Error individual '{b_norm.get('nombre')}': {single_err}")
                                stats["errores"] += 1

                    batch = []

                if (i + 1) % 1000 == 0:
                    logger.info(
                        f"[amigosac] {i+1}/{len(raw_items)} | "
                        f"new={stats['nuevos']} upd={stats['actualizados']} err={stats['errores']}"
                    )

        # Cross-reference warnings: marcar spots a menos de 100m de un prohibido de AmigosAC
        logger.info("[amigosac] Aplicando advertencias cruzadas para spots adyacentes a prohibidos...")
        try:
            async with pool.acquire() as conn:
                res_cross = await conn.execute("""
                    WITH prohibidos AS (
                        SELECT id, geog FROM spots
                        WHERE advertencia LIKE '%%prohibido%%AmigosAC%%' OR advertencia LIKE '%%restricciones%%AmigosAC%%'
                    )
                    UPDATE spots AS vecino SET
                        advertencia = '⚠️ Spot cercano a lugar con restricciones o prohibición según AmigosAC'
                    FROM prohibidos p
                    WHERE vecino.id != p.id
                      AND vecino.advertencia IS NULL
                      AND NOT ('amigosac' = ANY(vecino.fuentes))
                      AND ST_DWithin(vecino.geog, p.geog, 100)
                """)
                logger.info(f"[amigosac] Proceso finalizado. {res_cross}")
        except Exception as e:
            logger.warning(f"[amigosac] Error al aplicar advertencias cruzadas: {e}")

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[amigosac] Completado en {dur:.0f}s | {stats}")
        return stats
