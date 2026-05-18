"""AreasAC — importación offline desde PDF areasac.pdf."""

import re
from pathlib import Path
from datetime import datetime, timezone
from loguru import logger

from sources.base import AbstractSource

PDF_PATH = Path("/app/areasac.pdf")

TIPO_MAP = {"PU": "area_ac", "PR": "area_ac", "RU": "area_ac",
            "AR": "area_ac", "CP": "camping", "PK": "parking"}

_LINE_RE = re.compile(
    r"^(.+?)\s*-\s*\(([A-Z]{2})\)([^-]*?)\s+(-?\d+[,.]\d+)\s+(\d+[,.]\d+)\s*$"
)
_LINE_RE2 = re.compile(
    r"^(.+?)\s+\(([A-Z]{2})\)([^(]*?)\s+(-?\d+[,.]\d+)\s+(\d+[,.]\d+)\s*$"
)
_SKIP_RE = re.compile(
    r"^(\d+)$|^www\.|^Está prohibida|^Las Áreas|^AreasAc|^LEYENDA"
    r"|^\([A-Z!€#@]{1,2}\)\s|^Áreas / Parkings|^\s*$",
    re.IGNORECASE
)


def _coord(s):
    return float(s.replace(",", "."))


def _sym(sim, code):
    u = sim.upper()
    return f"/{code}/" in u or u.startswith(code) or u.endswith(f"/{code}")


class AreasACSource(AbstractSource):
    name = "areasac"
    rate_limit = 0
    dedup_radius_m = 50.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError

    def normalize(self, raw):
        return raw

    @staticmethod
    def _parsear_linea(linea):
        linea = linea.strip()
        if _SKIP_RE.search(linea):
            return None
        m = _LINE_RE.match(linea) or _LINE_RE2.match(linea)
        if not m:
            return None
        texto_previo, tipo_raw, simbolos, lon_s, lat_s = m.groups()
        if tipo_raw not in TIPO_MAP:
            return None
        try:
            lon, lat = _coord(lon_s), _coord(lat_s)
        except ValueError:
            return None
        if not (27.0 <= lat <= 44.5) or not (-19.0 <= lon <= 5.0):
            return None

        h, e = "#" in simbolos, "€" in simbolos
        gratuito = False if (h and e) or e else (True if h else None)

        partes = [p.strip() for p in texto_previo.split(" - ")]
        if len(partes) < 3:
            return None
        provincia, mun_raw = partes[0], partes[1]
        nombre = " - ".join(partes[2:]).strip()[:200]
        mun = re.sub(r"\s*[\[\(].*?[\]\)]", "", mun_raw).strip()
        if not nombre:
            return None

        return {
            "source_id": f"{provincia}_{mun}_{nombre}"[:100],
            "nombre": nombre, "lat": lat, "lon": lon,
            "tipo": TIPO_MAP[tipo_raw], "gratuito": gratuito,
            "country_iso": "es", "region": mun, "verificado": True,
            "agua_potable": _sym(simbolos, "AL"),
            "vaciado_grises": _sym(simbolos, "AG"),
            "vaciado_negras": _sym(simbolos, "AN"),
            "electricidad": _sym(simbolos, "CE"),
            "wifi": _sym(simbolos, "WI"),
            "perros": _sym(simbolos, "MS"),
            "ducha": _sym(simbolos, "DU"),
        }

    @staticmethod
    def _parsear_pdf(pdf_path):
        try:
            import pdfplumber
        except ImportError:
            logger.error("pdfplumber no instalado")
            return []
        try:
            txt = []
            with pdfplumber.open(pdf_path) as pdf:
                for p in pdf.pages:
                    t = p.extract_text()
                    if t:
                        txt.append(t)
            texto = "\n".join(txt)
        except Exception as e:
            logger.error(f"Error PDF: {e}")
            return []
        items = [r for l in texto.split("\n") if (r := AreasACSource._parsear_linea(l))]
        logger.info(f"PDF parseado: {len(items)} áreas")
        return items

    async def run(self, pool, config, log_id):
        from db import (find_spot_cercano, crear_spot, enriquecer_spot,
                        upsert_source_record, finish_scraper_log, update_fuente_config)
        inicio = datetime.now(timezone.utc)
        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
                 "errores": 0, "iniciado_en": inicio, "detalle": {}}

        if not PDF_PATH.exists():
            logger.error(f"PDF no encontrado: {PDF_PATH}")
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        items = self._parsear_pdf(PDF_PATH)
        if not items:
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        for i, norm in enumerate(items):
            sid = norm["source_id"]
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
                        await upsert_source_record(conn, spot_id, self.name, sid, norm, norm)
            except Exception as e:
                logger.error(f"[areasac] Error '{norm.get('nombre')}': {e}")
                stats["errores"] += 1
            if (i + 1) % 100 == 0:
                logger.info(f"[areasac] {i+1}/{len(items)}")

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)
        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[areasac] Completado en {dur:.0f}s | {stats}")
        return stats
