"""Campingcar-Infos — descarga global de archivos ASCII POI (formato CCI).

Fuente francesa con cobertura europea de áreas de servicio para autocaravanas.
Los datos se descargan como un ZIP que contiene varios archivos .asc separados por categoría.
Cada línea del .asc tiene formato:
    LON,LAT,"CATEGORIA PAIS LOCALIDAD  (CP)  Aire CCI <ID>"

Aporta principalmente cobertura geográfica y categorización. No incluye servicios,
precios, fotos ni reviews — es complementaria a fuentes ricas como park4night.
"""

import io
import re
import zipfile
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource


CCI_DOWNLOAD_URL = "https://www.campingcar-infos.com/Francais/creepoigpstotal.php"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Categorías CCI -> tipo canónico GeoSpots
# AC   = Aire Communale (área municipal pública)
# ACF  = Aire Camping Ferme (camping en granja)
# ACS  = Aire Camping Site (camping)
# APCC = Aire Payante Camping Car (área pago)
# APN  = Aire Privée Nuit (privada nocturna - parking pago)
# AS   = Aire de Service (solo servicios)
# ASN  = Aire Stationnement Nuit (parking nocturno gratuito)
# AA   = Aire d'Accueil (área de acogida)
CATEGORY_MAP = {
    "AC":   ("area_ac", True),           # área municipal de servicios, típicamente gratis
    "ACF":  ("camping", None),           # camping en granja
    "ACS":  ("camping", None),           # camping
    "APCC": ("area_ac", False),          # área pago de servicios
    "APN":  ("parking_privado", False),  # parking privado nocturno
    "AS":   ("area_ac", None),           # solo servicios
    "ASN":  ("parking_publico", True),   # parking nocturno público gratis
    "AA":   ("area_ac", None),           # acogida
}

# País FR mayúsculas (como aparece en CCI) -> ISO2
COUNTRY_ISO = {
    "FRANCE": "FR", "ESPAGNE": "ES", "PORTUGAL": "PT", "ITALIE": "IT",
    "ALLEMAGNE": "DE", "AUTRICHE": "AT", "SUISSE": "CH", "BELGIQUE": "BE",
    "PAYS-BAS": "NL", "LUXEMBOURG": "LU", "ROYAUME-UNI": "GB",
    "IRLANDE": "IE", "DANEMARK": "DK", "NORVEGE": "NO", "SUEDE": "SE",
    "FINLANDE": "FI", "POLOGNE": "PL", "REPUBLIQUE-TCHEQUE": "CZ",
    "SLOVAQUIE": "SK", "HONGRIE": "HU", "SLOVENIE": "SI", "CROATIE": "HR",
    "SERBIE": "RS", "BOSNIE": "BA", "MONTENEGRO": "ME", "ALBANIE": "AL",
    "GRECE": "GR", "BULGARIE": "BG", "ROUMANIE": "RO", "MOLDAVIE": "MD",
    "UKRAINE": "UA", "TURQUIE": "TR", "MAROC": "MA", "TUNISIE": "TN",
    "ANDORRE": "AD", "MONACO": "MC", "LIECHTENSTEIN": "LI", "MALTE": "MT",
    "ISLANDE": "IS", "ESTONIE": "EE", "LETTONIE": "LV", "LITUANIE": "LT",
    "CHYPRE": "CY", "SAINT-MARIN": "SM", "MACEDOINE": "MK",
    "BIELORUSSIE": "BY", "GUERNSEY": "GG", "KOSOVO": "XK",
    "MAURITANIE": "MR", "RUSSIE": "RU", "TCHEQUIE": "CZ",
    "JERSEY": "JE", "GIBRALTAR": "GI", "ARMENIE": "AM", "GEORGIE": "GE",
    "AZERBAIDJAN": "AZ", "ALGERIE": "DZ", "EGYPTE": "EG", "SYRIE": "SY",
    "ISRAEL": "IL", "LIBAN": "LB", "JORDANIE": "JO",
}

# Regex de línea: lon,lat,"texto"
# El texto típico: "AC ANDORRE LA MASSANA  (AD400 ) Aire CCI 33603"
# o sin CP:        "AA SLOVENIE PETROL  Aire CCI 3439"
LINE_RE = re.compile(r'^\s*(-?\d+\.?\d*),(-?\d+\.?\d*),"(.+)"\s*$')

# Captura: <CAT> <PAIS> <LOCALIDAD (con espacios)>  [(<CP>)]  Aire CCI <ID>
DESC_RE = re.compile(
    r'^(?P<cat>[A-Z]+)\s+'
    r'(?P<pais>[A-Z\-]+)\s+'
    r'(?P<loc>.+?)'
    r'(?:\s*\(\s*(?P<cp>[^)]*?)\s*\))?'
    r'\s*Aire\s+CCI\s+(?P<id>\d+)\s*$'
)


def _parse_line(line: str) -> dict | None:
    """Parsea una línea del .asc al dict raw."""
    m = LINE_RE.match(line)
    if not m:
        return None
    try:
        lon = float(m.group(1))
        lat = float(m.group(2))
    except ValueError:
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    if lat == 0 and lon == 0:
        return None

    desc = m.group(3).strip()
    dm = DESC_RE.match(desc)
    if not dm:
        return None

    return {
        "cci_id": dm.group("id"),
        "categoria": dm.group("cat"),
        "pais": dm.group("pais"),
        "localidad": dm.group("loc").strip(),
        "cp": (dm.group("cp") or "").strip() or None,
        "lat": lat,
        "lon": lon,
        "descripcion_raw": desc,
    }


def _parse_zip(zip_bytes: bytes) -> list[dict]:
    """Extrae ATOTALES_CCI.asc del ZIP y devuelve la lista parseada."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            target = next(
                (n for n in z.namelist() if n.upper().endswith("ATOTALES_CCI.ASC")),
                None,
            )
            if not target:
                logger.error("[campingcarinfos] No se encontró ATOTALES_CCI.asc dentro del ZIP")
                return []
            with z.open(target) as f:
                raw_text = f.read().decode("latin-1", errors="replace")
    except zipfile.BadZipFile as e:
        logger.error(f"[campingcarinfos] ZIP corrupto: {e}")
        return []

    items, descartadas = [], 0
    for line in raw_text.splitlines():
        if not line.strip():
            continue
        parsed = _parse_line(line)
        if parsed:
            items.append(parsed)
        else:
            descartadas += 1

    logger.info(
        f"[campingcarinfos] Parseo: {len(items)} válidas, {descartadas} descartadas"
    )
    return items


class CampingcarInfosSource(AbstractSource):
    """Campingcar-Infos: descarga global única, no usa grid."""

    name = "campingcarinfos"
    rate_limit = 0
    dedup_radius_m = 100.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("campingcarinfos usa descarga global única")

    def normalize(self, raw: dict) -> dict | None:
        cat = raw.get("categoria", "")
        tipo, default_gratis = CATEGORY_MAP.get(cat, ("otro", None))

        pais_iso = COUNTRY_ISO.get(raw.get("pais", ""))
        localidad = raw.get("localidad") or ""
        cp = raw.get("cp")

        # Nombre legible: "Localidad (CP)" o solo localidad
        nombre = localidad.title()
        if cp:
            nombre = f"{nombre} ({cp})"
        if not nombre.strip():
            nombre = f"CCI {raw['cci_id']}"

        result = {
            "source_id": str(raw["cci_id"]),
            "nombre": nombre[:200],
            "lat": raw["lat"],
            "lon": raw["lon"],
            "tipo": tipo,
            "web": f"https://www.campingcar-infos.com/Francais/cherchgps.php?cci={raw['cci_id']}",
        }
        if pais_iso:
            result["country_iso"] = pais_iso
        if default_gratis is not None:
            result["gratuito"] = default_gratis
        return result

    async def _download(self) -> bytes | None:
        """Descarga el ZIP global de CCI."""
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=120, follow_redirects=True
        ) as client:
            try:
                resp = await client.get(CCI_DOWNLOAD_URL)
                resp.raise_for_status()
                logger.info(
                    f"[campingcarinfos] Descargado {len(resp.content)/1024:.0f} KB"
                )
                return resp.content
            except Exception as e:
                logger.error(f"[campingcarinfos] Fallo en descarga: {e}")
                return None

    async def run(self, pool, config, log_id: int) -> dict:
        """Override completo: descarga global, parseo y procesado en batch."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        zip_bytes = await self._download()
        if not zip_bytes:
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        raw_items = _parse_zip(zip_bytes)
        if not raw_items:
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        logger.info(f"[campingcarinfos] {len(raw_items)} POIs a procesar")

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
                                    conn, b_norm["lat"], b_norm["lon"],
                                    self.dedup_radius_m,
                                    nombre=b_norm.get("nombre"),
                                    tipo=b_norm.get("tipo"),
                                )
                                if existente:
                                    spot_id = existente["id"]
                                    await enriquecer_spot(conn, spot_id, b_norm, self.name)
                                    stats["actualizados"] += 1
                                else:
                                    b_norm["fuentes"] = [self.name]
                                    spot_id = await crear_spot(conn, b_norm)
                                    stats["nuevos"] += 1

                                await upsert_source_record(
                                    conn, spot_id, self.name, sid, b_raw, b_norm
                                )
                    except Exception as batch_err:
                        logger.warning(
                            f"[campingcarinfos] Batch falló, reintentando individual: {batch_err}"
                        )
                        for b_raw, b_norm in batch:
                            sid = b_norm["source_id"]
                            try:
                                async with conn.transaction():
                                    existente = await find_spot_cercano(
                                        conn, b_norm["lat"], b_norm["lon"],
                                        self.dedup_radius_m,
                                        nombre=b_norm.get("nombre"),
                                        tipo=b_norm.get("tipo"),
                                    )
                                    if existente:
                                        spot_id = existente["id"]
                                        await enriquecer_spot(conn, spot_id, b_norm, self.name)
                                        stats["actualizados"] += 1
                                    else:
                                        b_norm["fuentes"] = [self.name]
                                        spot_id = await crear_spot(conn, b_norm)
                                        stats["nuevos"] += 1
                                    await upsert_source_record(
                                        conn, spot_id, self.name, sid, b_raw, b_norm
                                    )
                            except Exception as single_err:
                                logger.error(
                                    f"[campingcarinfos] Error CCI {sid}: {single_err}"
                                )
                                stats["errores"] += 1
                    batch = []

                if (i + 1) % 5000 == 0:
                    logger.info(
                        f"[campingcarinfos] {i+1}/{len(raw_items)} | "
                        f"new={stats['nuevos']} upd={stats['actualizados']} err={stats['errores']}"
                    )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[campingcarinfos] Completado en {dur:.0f}s | {stats}")
        return stats
