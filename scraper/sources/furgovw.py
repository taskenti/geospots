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

FURGOVW_API = "https://www.furgovw.org/api.php"
FURGOVW_FORUM = "https://www.furgovw.org/foro/index.php"
PAPELERA_BOARD = 88

FURGOVW_BOARDS = [
    35, 24, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 49, 50, 51, 52, 53, 54,
    47, 48, 80, 81, 82, 83, 84, 96, 97, 98, 99, 100, 101, 103, 105,
    115, 116, 117, 145, 146, 147, 148,
]


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
    result = {}
    lines = body.strip().split('\n')
    desc_lines = []
    in_desc = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith('agua:'):
            result['agua_potable'] = low.split(':', 1)[1].strip() in ('si', 'sí', 'yes')
        elif low.startswith(('wc:', 'baño:')):
            result['wc_publico'] = low.split(':', 1)[1].strip() in ('si', 'sí', 'yes')
        elif low.startswith(('electricidad:', 'luz:')):
            result['electricidad'] = low.split(':', 1)[1].strip() in ('si', 'sí', 'yes')
        elif low.startswith('ducha:'):
            result['ducha'] = low.split(':', 1)[1].strip() in ('si', 'sí', 'yes')
        elif low.startswith(('vaciado:', 'negras:')):
            result['vaciado_negras'] = low.split(':', 1)[1].strip() in ('si', 'sí', 'yes')
        elif low.startswith('grises:'):
            result['vaciado_grises'] = low.split(':', 1)[1].strip() in ('si', 'sí', 'yes')
        elif low.startswith(('gratuito:', 'gratis:')):
            result['gratuito'] = low.split(':', 1)[1].strip() in ('si', 'sí', 'yes', 'gratuito')
        elif 'descripci' in low and ':' in low:
            in_desc = True
            after = line.split(':', 1)[1].strip()
            if after:
                desc_lines.append(after)
        elif in_desc:
            desc_lines.append(line)
        elif len(line) > 20:
            desc_lines.append(line)

    if desc_lines:
        result['descripcion_es'] = '\n'.join(desc_lines).strip()[:2000]
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
                "tipo": "naturaleza",
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
            return result
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

                sid = norm["source_id"]
                topic_id = norm.pop("_topic_id", None)

                if not first_logged:
                    logger.info(f"[furgovw] Primer item: {norm.get('nombre')} ({sid})")
                    first_logged = True

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

            # ¿Tiene coords en la API?
            api = api_map.get(tid)
            if not api:
                pstats["sin_coords"] += 1
                continue

            fid_str = str(api["fid"])
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
                    "nombre": api["nombre"] or titulo,
                    "lat": api["lat"], "lon": api["lon"],
                    "tipo": "naturaleza", "gratuito": True,
                    "country_iso": "es", "fuentes": [self.name],
                }
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        cercano = await find_spot_cercano(
                            conn, norm["lat"], norm["lon"], self.dedup_radius_m
                        )
                        if cercano:
                            spot_id = cercano["id"]
                        else:
                            spot_id = await crear_spot(conn, norm)

                        await conn.execute(
                            "UPDATE spots SET advertencia = $1, updated_at = NOW() WHERE id = $2",
                            adv, spot_id
                        )
                        await upsert_source_record(conn, spot_id, self.name, fid_str, {"papelera": True}, norm)
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
