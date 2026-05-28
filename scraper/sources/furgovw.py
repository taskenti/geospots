"""Furgovw — scraper desde API JSON + RSS reviews + Papelera."""

import asyncio
import re
import json
from datetime import datetime, timezone
from xml.etree import ElementTree
from html import unescape
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_furgovw, merge_extra

FURGOVW_API = "https://www.furgovw.org/api.php"
FURGOVW_FORUM = "https://www.furgovw.org/foro/index.php"
PAPELERA_BOARD = 88

FURGOVW_BOARDS = [
    35, 24, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 49, 50, 51, 52, 53, 54,
    47, 48, 80, 81, 82, 83, 84, 96, 97, 98, 99, 100, 101, 103, 105,
    115, 116, 117, 145, 146, 147, 148,
]


def parse_booleano_foro(val: str) -> bool:
    val_low = val.lower()
    if val_low.startswith(('no', 'nono', 'ninguno', 'ninguna')):
        return False
    if any(x in val_low for x in ('si', 'sí', 'yes', 'dispone', 'existe', 'hay', 'fuente', 'grifo', 'toma')):
        return True
    return False

def extract_price_es(price_str: str) -> tuple[float | None, str | None]:
    if not price_str:
        return None, None
    price_str_low = price_str.lower()
    if any(x in price_str_low for x in ('gratis', 'gratuito', 'libre', '0', 'no tiene', 'sin costo', 'no hay')):
        return 0.0, "Gratuito"
    match = re.search(r"(\d+(?:[.,]\d+)?)", price_str)
    if match:
        try:
            val_str = match.group(1).replace(",", ".")
            val = float(val_str)
            return val, f"{val:.2f} €"
        except Exception:
            pass
    return None, None

def detect_language(text: str) -> str:
    if not text:
        return "es"
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
        return "es"
    return max_lang

def infer_large_vehicles(text: str) -> bool | None:
    if not text:
        return None
    text = text.lower()
    tent_only_keywords = [
        "tent only", "tents only", "only tents", "only for tents", "no campers", "no motorhomes", "no caravans", "no rvs", "no vans", "no cars", "no vehicles",
        "solo tiendas", "sólo tiendas", "solo tienda", "sólo tienda", "no furgonetas", "no autocaravanas", "no caravanas", "no vehículos", "no ACs", "no ac", "no apto para autocaravanas", "no apto autocaravanas",
        "uniquement tentes", "tentes uniquement", "pas de camping-car", "pas de caravane", "pas de véhicule",
        "alleen tenten", "geen campers", "geen caravans", "geen voertuigen",
        "nur zelte", "nur für zelte", "keine wohnmobile", "keine wohnwagen", "keine fahrzeuge"
    ]
    vehicle_allowed_keywords = [
        "camper allowed", "campers allowed", "vans allowed", "van allowed", "motorhome allowed", "motorhomes allowed", "rv allowed", "rvs allowed", "vehicles allowed", "vehicle allowed", "camper van", "campervan", "motorhome ok", "camper ok",
        "se aceptan campers", "se admiten campers", "furgonetas bienvenidas", "se aceptan furgonetas", "autocaravanas bienvenidas", "se aceptan autocaravanas", "apto para autocaravanas", "apto autocaravanas",
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

def _dms_to_decimal(deg, mins, secs, direction):
    decimal = float(deg) + float(mins)/60.0 + float(secs)/3600.0
    if direction.upper() in ('S', 'W', 'O'):
        decimal = -decimal
    return decimal


def _extraer_coordenadas_foro(html: str) -> tuple[float, float] | None:
    html_clean = html.replace('&#39;', "'").replace('&quot;', '"').replace('&nbsp;', ' ')
    
    # 1. Buscar en enlaces de Google Maps comunes
    patterns = [
        r'q=(-?\d{1,2}\.\d{4,9})\s*,\s*(-?\d{1,2}\.\d{4,9})',
        r'll=(-?\d{1,2}\.\d{4,9})\s*,\s*(-?\d{1,2}\.\d{4,9})',
        r'dir//(-?\d{1,2}\.\d{4,9})\s*,\s*(-?\d{1,2}\.\d{4,9})',
        r'@(-?\d{1,2}\.\d{4,9})\s*,\s*(-?\d{1,2}\.\d{4,9})',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, html_clean)
        for lat_s, lon_s in matches:
            lat, lon = float(lat_s), float(lon_s)
            if 30 <= lat <= 65 and -25 <= lon <= 45:
                return lat, lon
                
    # 2. DMS formato tipo 1: 51°02'45.3"N 3°42'10.6"E
    dms_pat1 = r'(\d{1,2})[\s°º]*(\d{1,2})[\s\']+(\d{1,2}(?:\.\d+)?)[\s\"]*([NnSs])\s+(\d{1,2})[\s°º]*(\d{1,2})[\s\']+(\d{1,2}(?:\.\d+)?)[\s\"]*([EeWwOo])'
    dms_matches1 = re.findall(dms_pat1, html_clean)
    for m in dms_matches1:
        lat = _dms_to_decimal(m[0], m[1], m[2], m[3])
        lon = _dms_to_decimal(m[4], m[5], m[6], m[7])
        if 30 <= lat <= 65 and -25 <= lon <= 45:
            return lat, lon
            
    # 3. DMS formato tipo 2: N 36°44'38'' W 03° 35' 59''
    dms_pat2 = r'([NnSs])\s*(\d{1,2})[\s°º]*(\d{1,2})[\s\']+(\d{1,2}(?:\.\d+)?)[\s\']*[^NnSsEeWwOo]*([EeWwOo])\s*(\d{1,2})[\s°º]*(\d{1,2})[\s\']+(\d{1,2}(?:\.\d+)?)'
    dms_matches2 = re.findall(dms_pat2, html_clean)
    for m in dms_matches2:
        lat = _dms_to_decimal(m[1], m[2], m[3], m[0])
        lon = _dms_to_decimal(m[5], m[6], m[7], m[4])
        if 30 <= lat <= 65 and -25 <= lon <= 45:
            return lat, lon
            
    # 4. Buscar pares de decimales genéricos en el texto
    matches = re.findall(r'(-?\d{1,2}\.\d{4,9})\s*,\s*(-?\d{1,2}\.\d{4,9})', html_clean)
    for lat_s, lon_s in matches:
        lat, lon = float(lat_s), float(lon_s)
        if 30 <= lat <= 65 and -25 <= lon <= 45:
            return lat, lon
            
    return None


def _limpiar_html(texto: str) -> str:
    texto = unescape(texto)
    texto = re.sub(r'<br\s*/?>', '\n', texto)
    texto = re.sub(r'<[^>]+>', '', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()


def _sanitize_xml(text: str) -> str:
    """Elimina caracteres XML inválidos que rompen el parser."""
    # XML 1.0 permite solo ciertos rangos de caracteres
    return re.sub(
        r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]',
        '', text
    )


def _parsear_body(body: str) -> dict:
    if not body:
        return {}
    
    # 1. Desescapar entidades HTML
    body_clean = unescape(body)
    
    # 2. Reemplazar <br /> o <br> por saltos de línea
    body_clean = re.sub(r'<br\s*/?>', '\n', body_clean, flags=re.IGNORECASE)
    
    # 3. Eliminar otros tags HTML
    body_clean = re.sub(r'<[^>]+>', '', body_clean)
    
    # 4. Reemplazar espacios duros por espacios normales
    body_clean = body_clean.replace('\xa0', ' ')
    
    # 5. Eliminar tachaduras de BBCode [s]...[/s] COMPLETAMENTE con su contenido
    body_clean = re.sub(r'\[s\].*?\[/s\]', '', body_clean, flags=re.IGNORECASE | re.DOTALL)
    
    # 6. Eliminar el resto de etiquetas BBCode [b], [/b], [i], etc.
    body_clean = re.sub(r'\[/?[a-zA-Z*#]+(?:=[^\]]+)?\]', '', body_clean)
    
    result = {}
    lines = body_clean.strip().split('\n')
    desc_lines = []
    in_desc = False
    
    # Pre-calcular texto completo en minúsculas para deducción del tipo
    texto_completo_limpio = body_clean.lower()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Si tiene ':' intentamos parsear campos clave
        if ':' in line:
            parts = line.split(':', 1)
            key = parts[0].strip().lower()
            val = parts[1].strip()
            val_low = val.lower()
            
            # Evitar falsos positivos si la clave es demasiado larga
            if len(key) < 30:
                if 'agua' in key:
                    result['agua_potable'] = parse_booleano_foro(val)
                elif 'wc' in key or 'baño' in key or 'baños' in key:
                    result['wc_publico'] = parse_booleano_foro(val)
                elif 'electricidad' in key or 'luz' in key:
                    result['electricidad'] = parse_booleano_foro(val)
                elif 'ducha' in key:
                    result['ducha'] = parse_booleano_foro(val)
                elif 'negras' in key:
                    result['vaciado_negras'] = parse_booleano_foro(val)
                elif 'grises' in key:
                    result['vaciado_grises'] = parse_booleano_foro(val)
                elif 'vaciado' in key:
                    if 'vaciado_negras' not in result:
                        result['vaciado_negras'] = parse_booleano_foro(val)
                elif 'gratuito' in key or 'gratis' in key:
                    result['gratuito'] = parse_booleano_foro(val) or any(x in val_low for x in ('gratis', 'gratuito', 'libre'))
                elif 'precio' in key or 'tarifa' in key:
                    p_aprox, p_info = extract_price_es(val)
                    if p_aprox is not None:
                        result['precio_aprox'] = p_aprox
                        result['precio_info'] = p_info
                        if p_aprox == 0.0:
                            result['gratuito'] = True
                elif 'tipo' in key:
                    if 'camping' in val_low:
                        result['tipo'] = 'camping'
                    elif any(x in val_low for x in ('area de', 'area ac', 'área ac', 'autocaravanas')):
                        result['tipo'] = 'area_ac'
                    elif 'furgoperfecto' in val_low or 'fp' in val_low:
                        urban_words = ('pueblo', 'ciudad', 'urbano', 'parking', 'aparcamiento', 'calle', 'plaza', 'asfalto')
                        nature_words = ('playa', 'río', 'rio', 'embalse', 'pantano', 'monte', 'montaña', 'bosque', 'pinar', 'pradera', 'campo', 'vistas', 'mirador', 'senderismo', 'naturaleza', 'parque')
                        urban_count = sum(1 for w in urban_words if w in texto_completo_limpio)
                        nature_count = sum(1 for w in nature_words if w in texto_completo_limpio)
                        if urban_count > nature_count:
                            result['tipo'] = 'parking'
                        else:
                            result['tipo'] = 'naturaleza'
                
                # Manejar descripción
                if 'descripci' in key:
                    in_desc = True
                    if val.strip():
                        desc_lines.append(val.strip())
                    continue
        
        # Si estamos dentro de la descripción, o es una línea informativa
        if in_desc:
            desc_lines.append(line)
        elif len(line) > 20 and not any(k in line.lower() for k in ('nombre:', 'tipo:', 'gratuito:', 'agua:', 'wc:', 'baño:', 'electricidad:', 'ducha:', 'vaciado:', 'grises:', 'coordenadas:')):
            desc_lines.append(line)

    desc_str = '\n'.join(desc_lines).strip()
    if desc_str:
        result['descripcion_es'] = desc_str[:2000]
        result['acceso_grandes'] = infer_large_vehicles(desc_str)
    else:
        result['acceso_grandes'] = infer_large_vehicles(body_clean)
        
    return result


class FurgovwSource(AbstractSource):
    """Furgovw: API JSON (todos los puntos) + RSS reviews."""

    name = "furgovw"
    rate_limit = 1.0
    dedup_radius_m = 50.0

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Furgovw usa API global, no bbox")

    def normalize(self, raw: dict) -> dict | None:
        try:
            fid = int(raw.get("id", 0))
            if not fid:
                return None

            nombre = (raw.get("nombre") or raw.get("name") or "Sin nombre").strip()[:200]

            # COORDENADAS INVERTIDAS en la API de Furgovw
            try:
                lat = float(raw.get("lng", 0))
                lon = float(raw.get("lat", 0))
            except (ValueError, TypeError):
                return None

            if lat == 0 and lon == 0:
                return None
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                return None

            fotos = []
            imagen = raw.get("imagen") or raw.get("image")
            if imagen and isinstance(imagen, str) and imagen.startswith("http"):
                fotos = [{"large": imagen, "thumb": imagen}]

            topic_id = raw.get("topic_id") or raw.get("topicId")
            web = f"https://www.furgovw.org/foro/index.php?topic={topic_id}.0" if topic_id else None

            # Parsear body si existe
            body_data = _parsear_body(raw.get("body", ""))

            result = {
                "source_id": str(fid),
                "nombre": nombre,
                "lat": lat,
                "lon": lon,
                "tipo": body_data.get("tipo", "naturaleza"),
                "gratuito": body_data.get("gratuito", True),
                "fotos_urls": fotos,
                "web": web,
                "country_iso": "es",
                "descripcion_es": body_data.get("descripcion_es"),
                "agua_potable": body_data.get("agua_potable"),
                "wc_publico": body_data.get("wc_publico"),
                "electricidad": body_data.get("electricidad"),
                "ducha": body_data.get("ducha"),
                "vaciado_negras": body_data.get("vaciado_negras"),
                "vaciado_grises": body_data.get("vaciado_grises"),
            }
            result["_topic_id"] = int(topic_id) if topic_id else None
            return merge_extra(result, extract_furgovw(raw))
        except Exception as e:
            logger.warning(f"Error normalizando furgovw {raw.get('id')}: {e}")
            return None

    async def _fetch_rss_reviews(self, client, pool, topic_to_spot: dict) -> int:
        """Descarga reviews vía RSS de los boards de Furgoperfectos."""
        from db import upsert_review
        total = 0

        for board_id in FURGOVW_BOARDS:
            url = f"{FURGOVW_FORUM}?action=.xml;type=rss2;sa=recent;board={board_id};limit=255"
            try:
                resp = await client.get(url, timeout=20)
                resp.raise_for_status()
                
                # Intentar parsear con ElementTree primero
                try:
                    xml_clean = _sanitize_xml(resp.text)
                    root = ElementTree.fromstring(xml_clean)
                    
                    for item in root.iter('item'):
                        title = item.findtext('title', '').strip()
                        if not title.startswith('Re:'):
                            continue
    
                        link = item.findtext('link', '')
                        desc = item.findtext('description', '').strip()
                        await self._process_rss_item(pool, topic_to_spot, title, link, desc)
                        total += 1
                        
                except Exception as xml_err:
                    logger.debug(f"Fallback regex para board {board_id} por error XML: {xml_err}")
                    # Fallback robusto con Regex si el XML está roto
                    item_blocks = re.findall(r'<item>(.*?)</item>', resp.text, re.DOTALL | re.IGNORECASE)
                    for block in item_blocks:
                        title_m = re.search(r'<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', block, re.DOTALL | re.IGNORECASE)
                        link_m = re.search(r'<link>(.*?)</link>', block, re.DOTALL | re.IGNORECASE)
                        desc_m = re.search(r'<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>', block, re.DOTALL | re.IGNORECASE)
                        
                        title = title_m.group(1).strip() if title_m else ""
                        if not title.startswith('Re:'):
                            continue
                            
                        link = link_m.group(1).strip() if link_m else ""
                        desc = desc_m.group(1).strip() if desc_m else ""
                        
                        # Limpiar entidades HTML que el parser regex no limpia automáticamente
                        title = unescape(title)
                        link = unescape(link)
                        desc = unescape(desc)
                        
                        await self._process_rss_item(pool, topic_to_spot, title, link, desc)
                        total += 1

                await asyncio.sleep(1.0)
            except Exception as e:
                logger.warning(f"RSS board {board_id} falló completamente: {e}")

        return total

    async def _process_rss_item(self, pool, topic_to_spot, title, link, desc):
        """Procesa e inserta un item individual del RSS."""
        from db import upsert_review
        
        topic_match = re.search(r'topic=(\d+)', link)
        msg_match = re.search(r'msg(\d+)', link)
        if not topic_match:
            return

        tid = int(topic_match.group(1))
        spot_id = topic_to_spot.get(tid)
        if not spot_id:
            return

        texto = _limpiar_html(desc)[:2000]
        if not texto or len(texto) < 5:
            return

        msg_id = msg_match.group(1) if msg_match else str(tid)
        try:
            async with pool.acquire() as conn:
                await upsert_review(conn, {
                    "spot_id": spot_id,
                    "source": "furgovw",
                    "source_review_id": f"furgovw_{msg_id}",
                    "texto": texto,
                    "rating": None,
                    "fecha": None,
                    "autor": None,
                    "idioma": "es",
                })
                # Sync review_count tras cada insert (cada RSS msg es 1 review)
                from db import refresh_review_count
                await refresh_review_count(conn, "furgovw", spot_id)
        except Exception:
            pass

    async def run(self, pool, config, log_id: int) -> dict:
        """Override: API global + RSS reviews."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True) as client:
            # Fase 1: API con body
            logger.info("[furgovw] Descargando API con body...")
            try:
                resp = await client.get(FURGOVW_API,
                    params={"getEverything": "", "user": ""},
                    timeout=120
                )
                resp.raise_for_status()
                parsed = resp.json()
                raw_data = parsed if isinstance(parsed, list) else []
            except Exception as e:
                logger.error(f"[furgovw] API error: {e}")
                stats["errores"] = 1
                async with pool.acquire() as conn:
                    await finish_scraper_log(conn, log_id, stats)
                return stats

            logger.info(f"[furgovw] {len(raw_data)} items de la API, comenzando ingesta...")

            topic_to_spot: dict[int, int] = {}
            first_logged = False

            for idx, raw in enumerate(raw_data):
                norm = self.normalize(raw)
                if not norm:
                    continue
                if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                    continue

                sid = norm["source_id"]
                topic_id = norm.pop("_topic_id", None)

                if not first_logged:
                    logger.info(f"[furgovw] Primer item: {norm.get('nombre')} ({sid})")
                    first_logged = True

                try:
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            existente = await find_spot_cercano(
                                conn, norm["lat"], norm["lon"], self.dedup_radius_m, norm["nombre"], norm["tipo"]
                            )
                            if existente:
                                spot_id = existente["id"]
                                await enriquecer_spot(conn, spot_id, norm, self.name)
                                stats["actualizados"] += 1
                            else:
                                norm["fuentes"] = [self.name]
                                spot_id = await crear_spot(conn, norm)
                                stats["nuevos"] += 1

                            await upsert_source_record(conn, spot_id, self.name, sid, raw, norm)

                    if topic_id:
                        topic_to_spot[topic_id] = spot_id

                except Exception as e:
                    logger.error(f"[furgovw] Error '{norm.get('nombre')}': {e}")
                    stats["errores"] += 1

                if (idx + 1) % 200 == 0:
                    logger.info(
                        f"[furgovw] {idx+1}/{len(raw_data)} | "
                        f"new={stats['nuevos']} upd={stats['actualizados']} err={stats['errores']}"
                    )

            # Fase 2: RSS reviews
            logger.info(f"[furgovw] Descargando reviews RSS ({len(topic_to_spot)} topics mapeados)...")
            stats["reviews_nuevas"] = await self._fetch_rss_reviews(client, pool, topic_to_spot)

            # Fase 3: Papelera (lugares retirados)
            logger.info("[furgovw] Fase 3: Escaneando papelera (board 88)...")
            papelera_stats = await self._scrape_papelera(client, pool)
            stats["detalle"]["papelera"] = papelera_stats

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[furgovw] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        """Incremental RSS review re-fetch using mapped forum topic IDs."""
        stats = {"reviews_nuevas": 0, "errores": 0, "topics": 0}
        topic_to_spot: dict[int, int] = {}
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT spot_id, raw_data
                FROM source_records
                WHERE source = $1
                """,
                self.name,
            )
        for row in rows:
            raw = row["raw_data"] or {}
            topic_id = raw.get("topic_id") or raw.get("topicId")
            if topic_id:
                try:
                    topic_to_spot[int(topic_id)] = row["spot_id"]
                except (TypeError, ValueError):
                    continue

        stats["topics"] = len(topic_to_spot)
        async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True) as client:
            try:
                stats["reviews_nuevas"] = await self._fetch_rss_reviews(client, pool, topic_to_spot)
            except Exception as e:
                logger.error(f"[furgovw] Error incremental RSS reviews: {e}")
                stats["errores"] += 1
        return stats

    async def _fetch_papelera_topics(self, client) -> list[dict]:
        """Pagina TODAS las páginas del board 88 (papelera) y extrae topic IDs."""
        topics = []
        page = 0
        seen = set()

        while True:
            url = f"{FURGOVW_FORUM}?board={PAPELERA_BOARD}.{page}"
            try:
                resp = await client.get(url, timeout=15)
                resp.raise_for_status()
                html = resp.text

                found = re.findall(
                    r'topic=(\d+)\.0"\s*>\s*([^<]{3,200?})\s*</a>',
                    html
                )

                if not found and page == 0:
                    ids_only = list(set(re.findall(r'topic=(\d+)\.0"', html)))
                    found = [(tid, 'Lugar retirado') for tid in ids_only]
                    logger.info(f"[papelera] Fallback: {len(found)} IDs")

                if not found:
                    break

                new_count = 0
                for tid_str, title in found:
                    tid = int(tid_str)
                    if tid not in seen:
                        seen.add(tid)
                        topics.append({"topic_id": tid, "titulo": title.strip()[:200]})
                        new_count += 1

                logger.info(f"[papelera] Página {page}: {new_count} nuevos (total: {len(topics)})")

                # Detectar siguiente página
                if f'board={PAPELERA_BOARD}.{page + 20}' in html:
                    page += 20
                elif f'board={PAPELERA_BOARD}.{page + 25}' in html:
                    page += 25
                elif f'board={PAPELERA_BOARD}.{page + 50}' in html:
                    page += 50
                else:
                    break

                await asyncio.sleep(1.5)
            except Exception as e:
                logger.warning(f"[papelera] Error página {page}: {e}")
                break

        logger.info(f"[papelera] Total: {len(topics)} topics retirados")
        return topics

    async def _scrape_papelera(self, client, pool) -> dict:
        """Fase 3: Marcar spots retirados + cross-reference con otras fuentes."""
        from db import find_spot_cercano, crear_spot, upsert_source_record

        pstats = {"marcados": 0, "nuevos_retirados": 0, "cross_ref": 0, "sin_coords": 0}

        papelera_topics = await self._fetch_papelera_topics(client)
        if not papelera_topics:
            return pstats

        papelera_ids = {t["topic_id"] for t in papelera_topics}
        papelera_titulos = {t["topic_id"]: t["titulo"] for t in papelera_topics}

        # Construir mapeo topic_id → coords desde la API
        try:
            resp = await client.get(FURGOVW_API,
                params={"getEverything": "", "user": ""},
                timeout=120
            )
            resp.raise_for_status()
            parsed = resp.json()
            api_data = parsed if isinstance(parsed, list) else []
        except Exception as e:
            logger.error(f"[papelera] Error API: {e}")
            return pstats

        api_map = {}
        for raw in api_data:
            tid = raw.get("topic_id")
            if tid:
                try:
                    lat = float(raw.get("lng", 0))
                    lon = float(raw.get("lat", 0))
                    if lat != 0 and lon != 0 and -90 <= lat <= 90 and -180 <= lon <= 180:
                        api_map[int(tid)] = {
                            "fid": int(raw.get("id", 0)),
                            "lat": lat, "lon": lon,
                            "nombre": (raw.get("nombre") or raw.get("name") or "").strip()[:200],
                        }
                except (ValueError, TypeError):
                    pass

        logger.info(f"[papelera] API: {len(api_map)} topics con coords, {len(papelera_ids)} en papelera")

        # Buscar en DB qué spots ya existen con source furgovw
        async with pool.acquire() as conn:
            existing = await conn.fetch("""
                SELECT sr.source_id, sr.spot_id, s.advertencia
                FROM source_records sr
                JOIN spots s ON s.id = sr.spot_id
                WHERE sr.source = 'furgovw'
            """)

        existing_by_fid = {r["source_id"]: r for r in existing}

        for topic in papelera_topics:
            tid = topic["topic_id"]
            titulo = topic["titulo"]

            # ¿Tiene coords en la API o las extraemos del foro?
            api = api_map.get(tid)
            lat, lon = None, None
            fid_str = None
            nombre = titulo
            
            if api:
                lat = api["lat"]
                lon = api["lon"]
                fid_str = str(api["fid"])
                nombre = api["nombre"] or titulo
            else:
                # No está en la API: scrapear la página del foro para extraer coordenadas
                url = f"{FURGOVW_FORUM}?topic={tid}.0"
                try:
                    logger.info(f"[papelera] Scrapeando topic {tid} sin coords en API: {titulo}...")
                    resp = await client.get(url, timeout=15)
                    resp.raise_for_status()
                    coords = _extraer_coordenadas_foro(resp.text)
                    if coords:
                        lat, lon = coords
                        fid_str = f"papelera_{tid}"
                        logger.info(f"[papelera] Coordenadas extraídas del foro para topic {tid}: {lat}, {lon}")
                    else:
                        logger.warning(f"[papelera] No se pudieron extraer coordenadas del foro para topic {tid}")
                    await asyncio.sleep(1.0)
                except Exception as e:
                    logger.warning(f"[papelera] Error scrapeando topic {tid} del foro: {e}")

            if not lat or not lon or not fid_str:
                pstats["sin_coords"] += 1
                continue

            adv = "⚠️ Lugar retirado de Furgoperfectos (papelera del foro)"

            # ¿Ya existe en source_records?
            ex = existing_by_fid.get(fid_str)
            if ex:
                if not ex["advertencia"]:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE spots SET advertencia = $1, updated_at = NOW() WHERE id = $2",
                            adv, ex["spot_id"]
                        )
                    pstats["marcados"] += 1
                continue

            # No existe → crear con advertencia
            try:
                norm = {
                    "source_id": fid_str,
                    "nombre": nombre,
                    "lat": lat, "lon": lon,
                    "tipo": "naturaleza", "gratuito": True,
                    "country_iso": "es", "fuentes": [self.name],
                }
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        cercano = await find_spot_cercano(
                            conn, norm["lat"], norm["lon"], self.dedup_radius_m, norm["nombre"], norm["tipo"]
                        )
                        if cercano:
                            spot_id = cercano["id"]
                        else:
                            spot_id = await crear_spot(conn, norm)

                        await conn.execute(
                            "UPDATE spots SET advertencia = $1, updated_at = NOW() WHERE id = $2",
                            adv, spot_id
                        )
                        await upsert_source_record(conn, spot_id, self.name, fid_str, {"papelera": True, "topic_id": tid}, norm)
                pstats["nuevos_retirados"] += 1
            except Exception as e:
                logger.warning(f"[papelera] Error insertando {titulo}: {e}")

        # Cross-reference: marcar spots de OTRAS fuentes cercanos a retirados
        try:
            async with pool.acquire() as conn:
                cross = await conn.fetchval("""
                    WITH retirados AS (
                        SELECT id, geog FROM spots
                        WHERE advertencia LIKE '%%retirado%%Furgoperfectos%%'
                    )
                    UPDATE spots AS vecino SET
                        advertencia = '⚠️ Spot cercano a lugar retirado en Furgoperfectos'
                    FROM retirados r
                    WHERE vecino.id != r.id
                      AND vecino.advertencia IS NULL
                      AND NOT ('furgovw' = ANY(vecino.fuentes))
                      AND ST_DWithin(vecino.geog, r.geog, 100)
                    RETURNING vecino.id
                """)
                pstats["cross_ref"] = cross if isinstance(cross, int) else 0
        except Exception as e:
            logger.warning(f"[papelera] Cross-ref error: {e}")

        logger.info(f"[papelera] Resultado: {pstats}")
        return pstats
