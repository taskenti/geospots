"""iOverlander — importación offline desde fichero KMZ."""

import os
import json
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from loguru import logger

from sources.base import AbstractSource

KMZ_PATH_DEFAULT = "/data/ioverlander.kmz"

EU_LAT_MIN, EU_LAT_MAX = 35.0, 72.0
EU_LON_MIN, EU_LON_MAX = -25.0, 45.0

CATEGORIA_MAP = {
    "camping": "camping", "camp site": "camping", "campsite": "camping",
    "wild camping": "naturaleza", "informal campsite": "naturaleza",
    "bivouac": "naturaleza", "bivac": "naturaleza",
    "free camping": "naturaleza", "wild": "naturaleza",
    "parking": "parking",
    "aire": "area_ac", "motorhome": "area_ac", "service": "area_ac",
    "sani": "area_ac", "dump station": "area_ac", "dump": "area_ac",
    "picnic": "picnic", "rest area": "picnic",
    "hostel": "camping", "guest house": "camping", "hotel": "camping",
}

KW_AGUA = ["water", "agua", "wasser", "eau", "acqua", "potable"]
KW_ELECTR = ["electricity", "electric", "electr", "corriente", "strom"]
KW_WIFI = ["wifi", "wi-fi", "internet"]
KW_DUCHA = ["shower", "showers", "ducha", "douche", "duschen"]
KW_WC = ["toilet", "wc", "baño", "toilette", "sanitär"]
KW_PERROS = ["dog", "dogs", "perro", "hund", "animal", "pet"]
KW_GRATIS = ["free", "gratis", "kostenlos", "gratuit"]


def _inferir_tipo(name: str) -> str:
    name_lower = (name or "").lower()
    for keyword, tipo in CATEGORIA_MAP.items():
        if keyword in name_lower:
            return tipo
    return "otro"


def _inferir_bool(desc: str, keywords: list[str]) -> bool | None:
    if not desc:
        return None
    desc_lower = desc.lower()
    for kw in keywords:
        if kw in desc_lower:
            return True
    return None


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

        desc = raw.get("descripcion", "")
        tipo = _inferir_tipo(nombre)
        iov_id = raw.get("iov_id", f"{lat:.6f}_{lon:.6f}")

        return {
            "source_id": iov_id,
            "nombre": nombre,
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "descripcion_en": desc if desc else None,
            "agua_potable": _inferir_bool(desc, KW_AGUA),
            "electricidad": _inferir_bool(desc, KW_ELECTR),
            "wifi": _inferir_bool(desc, KW_WIFI),
            "ducha": _inferir_bool(desc, KW_DUCHA),
            "wc_publico": _inferir_bool(desc, KW_WC),
            "perros": _inferir_bool(desc, KW_PERROS),
            "gratuito": _inferir_bool(desc, KW_GRATIS),
        }

    @staticmethod
    def parsear_kmz(kmz_path: str) -> list[dict]:
        """Parsea KMZ y devuelve lista de dicts raw por placemark (solo Europa)."""
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

            if not (EU_LAT_MIN <= lat <= EU_LAT_MAX and EU_LON_MIN <= lon <= EU_LON_MAX):
                fuera += 1
                continue

            name_node = pm.find('name')
            nombre = (name_node.text or "Sin nombre").strip()[:200] if name_node is not None else "Sin nombre"

            desc_node = pm.find('description')
            desc = (desc_node.text or "").strip() if desc_node is not None else ""

            items.append({
                "iov_id": f"{lat:.6f}_{lon:.6f}",
                "nombre": nombre,
                "lat": lat,
                "lon": lon,
                "descripcion": desc,
            })

        logger.info(f"KMZ: {total} puntos, {fuera} fuera EU, {len(items)} para importar")
        return items

    async def run(self, pool, config, log_id: int) -> dict:
        """Override completo: lee KMZ offline, no usa API."""
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

        BATCH_LOG = 200
        for i, raw in enumerate(raw_items):
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
                            conn, spot_id, self.name, sid,
                            raw, norm
                        )
            except Exception as e:
                logger.error(f"[ioverlander] Error '{norm.get('nombre')}': {e}")
                stats["errores"] += 1

            if (i + 1) % BATCH_LOG == 0:
                logger.info(
                    f"[ioverlander] {i+1}/{len(raw_items)} | "
                    f"new={stats['nuevos']} upd={stats['actualizados']} err={stats['errores']}"
                )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[ioverlander] Completado en {dur:.0f}s | {stats}")
        return stats
