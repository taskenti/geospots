import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

def _checksum(data: dict) -> str:
    import hashlib
    s = json.dumps(data, sort_keys=True, default=str)
    return hashlib.md5(s.encode()).hexdigest()

TIPO_MAP = {
    "camperplace": "area_ac", "camping": "camping", "parking": "parking",
    "motorhome": "area_ac", "service": "area_ac", "nature": "naturaleza",
    "wild": "naturaleza", "picnic": "picnic",
}

# subtitle viene como "Ciudad, País" donde País está en inglés (free text).
# Mapeamos los más comunes a ISO2 lowercase. Para los desconocidos dejamos
# None y dejamos que el trigger geográfico de PostGIS clasifique por lat/lon.
COUNTRY_NAME_TO_ISO = {
    "spain": "es", "france": "fr", "germany": "de", "italy": "it",
    "portugal": "pt", "netherlands": "nl", "belgium": "be", "austria": "at",
    "switzerland": "ch", "united kingdom": "gb", "ireland": "ie",
    "denmark": "dk", "norway": "no", "sweden": "se", "finland": "fi",
    "iceland": "is", "poland": "pl", "czechia": "cz", "czech republic": "cz",
    "slovakia": "sk", "hungary": "hu", "slovenia": "si", "croatia": "hr",
    "bosnia and herzegovina": "ba", "serbia": "rs", "montenegro": "me",
    "north macedonia": "mk", "albania": "al", "greece": "gr",
    "bulgaria": "bg", "romania": "ro", "moldova": "md", "ukraine": "ua",
    "turkey": "tr", "morocco": "ma", "tunisia": "tn",
    "andorra": "ad", "monaco": "mc", "liechtenstein": "li", "malta": "mt",
    "estonia": "ee", "latvia": "lv", "lithuania": "lt", "luxembourg": "lu",
    "cyprus": "cy", "san marino": "sm", "vatican city": "va",
    "russia": "ru", "belarus": "by",
}

class CamperContactSource(AbstractSource):
    name = "campercontact"
    rate_limit = 0.3
    grid_step = 1.0
    dedup_radius_m = 80.0

    HEADERS = {
        "accept": "*/*",
        "accept-language": "en",
        "origin": "https://www.campercontact.com",
        "referer": "https://www.campercontact.com/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "x-feature-flags": "microcamping",
    }

    BASE_URL = "https://services.campercontact.com/search/results/list"

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        params = {
            "topleft_lat": tl_lat, "topleft_lon": tl_lon,
            "bottomright_lat": br_lat, "bottomright_lon": br_lon,
        }
        try:
            r = await client.get(self.BASE_URL, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []

        items = data.get("items", [])
        
        total_val = 0
        total_obj = data.get("total")
        if isinstance(total_obj, dict):
            total_val = total_obj.get("value") or 0
        elif isinstance(total_obj, int):
            total_val = total_obj
        else:
            total_val = len(items)

        # Subdivide si >50 y la celda es divisible
        if total_val > 50 and (tl_lat - br_lat) > 0.1:
            mid_lat = round((tl_lat + br_lat) / 2, 4)
            mid_lon = round((tl_lon + br_lon) / 2, 4)
            results = []
            for cell in [
                (tl_lat, tl_lon, mid_lat, mid_lon),
                (tl_lat, mid_lon, mid_lat, br_lon),
                (mid_lat, tl_lon, br_lat, mid_lon),
                (mid_lat, mid_lon, br_lat, br_lon),
            ]:
                results.extend(await self.fetch_cell(client, *cell))
            return results

        return items

    def normalize(self, raw: dict) -> dict | None:
        loc = raw.get("location", {})
        lat, lon = loc.get("lat"), loc.get("lon")
        if lat is None or lon is None:
            return None

        filters = raw.get("filters", {})
        price_range = raw.get("priceRange", {})
        poi_type = filters.get("poiType", raw.get("type", ""))

        # Tipo
        tipo = "otro"
        for k, v in TIPO_MAP.items():
            if k in (poi_type or "").lower():
                tipo = v
                break

        # Gratuito
        gratuito = None
        if price_range:
            mn = price_range.get("min")
            if mn is not None:
                try:
                    mn_val = float(mn)
                    if mn_val == 0:
                        gratuito = True
                    elif mn_val > 0:
                        gratuito = False
                except ValueError:
                    pass

        # Ciudad / país del subtitle (formato "Ciudad, País" en inglés)
        subtitle = raw.get("subtitle", "")
        ciudad, country_iso = None, None
        if subtitle:
            parts = [p.strip() for p in subtitle.split(",")]
            ciudad = parts[0] if parts else None
            if len(parts) >= 2:
                pais_raw = parts[-1].lower()
                # Mapear inglés -> ISO2. Si no aparece, None para que el trigger
                # geográfico de PostGIS lo clasifique por lat/lon (evita meter
                # "Spain" o "S" truncado en una columna que espera ISO2)
                country_iso = COUNTRY_NAME_TO_ISO.get(pais_raw)

        cc_id = str(raw.get("sitecode") or raw.get("id", ""))
        if not cc_id:
            return None

        return {
            "source_id": cc_id,
            "nombre": (raw.get("title") or "Sin nombre").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "precio_info": (
                f"min: {price_range.get('min')} / max: {price_range.get('max')}"
                if price_range else None
            ),
            "rating_promedio": filters.get("rating"),
            "num_reviews": filters.get("numberOfReviews"),
            "num_plazas": filters.get("maxCamperSpots"),
            "region": ciudad,
            "country_iso": country_iso,
            "web": (
                "https://www.campercontact.com/en" + raw.get("permalink", "")
                if raw.get("permalink") else None
            ),
        }

    def _parse_detail_html(self, html: str) -> dict | None:
        matches = re.findall(r'self\.__next_f\.push\(\[\d+,\s*"(.*?)"\]\)', html, re.DOTALL)
        matches_single = re.findall(r"self\.__next_f\.push\(\[\d+,\s*'(.*?)'\]\)", html, re.DOTALL)
        all_chunks = matches + matches_single
        
        combined_str = ""
        for chunk in all_chunks:
            chunk_clean = chunk.replace('\\"', '"').replace('\\\\', '\\').replace('\\/', '/')
            combined_str += chunk_clean

        start_idx = combined_str.find('"poiV2":')
        if start_idx == -1:
            start_idx = combined_str.find('"poiV1":')
            if start_idx == -1:
                return None

        brace_idx = combined_str.rfind('{', 0, start_idx)
        if brace_idx == -1:
            return None
        
        open_braces = 0
        in_string = False
        escape = False
        
        for i in range(brace_idx, len(combined_str)):
            char = combined_str[i]
            if escape:
                escape = False
                continue
            if char == '\\':
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string:
                if char == '{':
                    open_braces += 1
                elif char == '}':
                    open_braces -= 1
                    if open_braces == 0:
                        json_str = combined_str[brace_idx:i+1]
                        try:
                            return json.loads(json_str)
                        except json.JSONDecodeError:
                            pass
        return None

    def _normalize_detail(self, data: dict, fallback_web: str = None) -> dict | None:
        poi = data.get("poiV2") or data.get("poiV1")
        if not poi:
            return None

        amenities = poi.get("amenities", [])
        amenities_map = {a.get("type"): a.get("priceStatus") for a in amenities if a.get("type")}

        # Terrain features
        terrain = poi.get("terrain", [])
        iluminacion = "illuminated" in terrain
        seguridad = "security" in terrain

        # Contact
        contact = poi.get("contactDetails", {})
        email = contact.get("email")
        telefono = contact.get("phoneNumber")
        web = contact.get("website")

        # Photos
        photos = [p.get("url") for p in poi.get("photos", {}).get("items", []) if p.get("url")]

        # Descriptions
        desc_trans = poi.get("descriptionTranslations", {})

        # Map amenities
        agua_potable = "water" in amenities_map
        vaciado_negras = "dischargeToilet" in amenities_map
        vaciado_grises = "dischargeWasteWater" in amenities_map
        electricidad = "electricity" in amenities_map
        ducha = "shower" in amenities_map
        wifi = "internet" in amenities_map
        wc_publico = "toilet" in amenities_map
        perros = "dogsAllowed" in amenities_map

        return {
            "agua_potable": agua_potable,
            "vaciado_negras": vaciado_negras,
            "vaciado_grises": vaciado_grises,
            "electricidad": electricidad,
            "ducha": ducha,
            "wifi": wifi,
            "wc_publico": wc_publico,
            "perros": perros,
            "iluminacion": iluminacion,
            "seguridad": seguridad,
            "num_plazas": poi.get("limits", {}).get("maxCapacity"),
            "web": web or fallback_web,
            "telefono": telefono,
            "email": email,
            "fotos_urls": photos,
            "descripcion_nl": desc_trans.get("nl"),
            "descripcion_de": desc_trans.get("de"),
            "descripcion_fr": desc_trans.get("fr"),
            "descripcion_es": desc_trans.get("es"),
            "descripcion_en": desc_trans.get("en"),
            "descripcion_it": desc_trans.get("it"),
        }

    async def run(self, pool, config, log_id: int) -> dict:
        """Pipeline completo: grid → fetch → normalize → store → Phase 2 (enrichment & reviews)."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        # 1. Fase 1: Grid Scan
        cells = await self.generate_active_grid(pool, step=self.grid_step)
        logger.info(f"[{self.name}] Fase 1: {len(cells)} celdas a procesar")

        seen_ids: set[str] = set()
        sem = asyncio.Semaphore(3)
        headers = getattr(self, 'HEADERS', {})

        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            LOTE = 20
            for i in range(0, len(cells), LOTE):
                batch = cells[i:i+LOTE]

                async def handle(cell):
                    async with sem:
                        await asyncio.sleep(self.rate_limit)
                        return await self.fetch_cell(client, *cell)

                results = await asyncio.gather(*[handle(c) for c in batch],
                                                return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        logger.warning(f"[{self.name}] Error en celda: {result}")
                        stats["errores"] += 1
                        continue

                    for raw_item in result:
                        norm = self.normalize(raw_item)
                        if not norm:
                            continue
                        if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                            continue

                        sid = str(norm.get("source_id", ""))
                        if not sid or sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                        try:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    existente = await find_spot_cercano(
                                        conn, norm["lat"], norm["lon"],
                                        self.dedup_radius_m
                                    )

                                    if existente:
                                        spot_id = existente["id"]
                                        await enriquecer_spot(
                                            conn, spot_id, norm, self.name
                                        )
                                        stats["actualizados"] += 1
                                    else:
                                        norm["fuentes"] = [self.name]
                                        spot_id = await crear_spot(conn, norm)
                                        stats["nuevos"] += 1

                                    await upsert_source_record(
                                        conn, spot_id, self.name, sid,
                                        raw_item, norm
                                    )
                        except Exception as e:
                            logger.error(f"[{self.name}] Error '{norm.get('nombre')}': {e}")
                            stats["errores"] += 1

                logger.info(
                    f"[{self.name}] {min(i+LOTE, len(cells))}/{len(cells)} | "
                    f"uniq={len(seen_ids)} new={stats['nuevos']} "
                    f"upd={stats['actualizados']} err={stats['errores']}"
                )

        # Finalizar logs en BD
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[{self.name}] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config) -> dict:
        stats = {
            "nuevos": 0,
            "actualizados": 0,
            "reviews_nuevas": 0,
            "errores": 0
        }

        logger.info(f"[{self.name}] Buscando spots con reviews/detalles pendientes de descarga...")
        async with pool.acquire() as conn:
            # IMPORTANTE: la URL de campercontact se reconstruye desde raw_data->>'permalink',
            # NO desde normalized_data->>'web'. Phase 2 sobreescribe normalized_data.web con
            # el sitio externo del establecimiento (e.g. sorkwity.pttk.pl), por lo que usar
            # esa columna provoca scraping de sitios que no son campercontact.
            enrich_jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id,
                       raw_data->>'permalink' AS permalink,
                       sr.review_count, COALESCE(r.cnt, 0) as db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) as cnt
                    FROM reviews
                    WHERE source = 'campercontact'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'campercontact'
                  AND raw_data->>'permalink' IS NOT NULL
                  AND (
                    (sr.normalized_data->>'details_fetched') IS NULL
                    OR (sr.review_count > 0 AND COALESCE(r.cnt, 0) < sr.review_count)
                  )
                ORDER BY sr.review_count DESC;
            """)

        logger.info(f"[{self.name}] Encontrados {len(enrich_jobs)} spots con reviews pendientes.")

        if not enrich_jobs:
            return stats

        job_queue = asyncio.Queue()
        for r in enrich_jobs:
            await job_queue.put(dict(r))

        headers = getattr(self, 'HEADERS', {})

        async def enrich_worker(client):
            while not job_queue.empty():
                try:
                    job = await job_queue.get()
                except asyncio.CancelledError:
                    break

                spot_id = job["spot_id"]
                sid = job["source_id"]
                permalink = job["permalink"]
                if not permalink:
                    job_queue.task_done()
                    continue
                # Construir URL canónica /en/ desde el permalink (formato:
                # "/france/brittany/.../100011/la-ferme-de-tuchennou")
                web_url = f"https://www.campercontact.com/en{permalink}"

                try:
                    await asyncio.sleep(self.rate_limit)
                    r_web = await client.get(web_url)
                    if r_web.status_code == 200:
                        html = r_web.text
                        detail_data = self._parse_detail_html(html)
                        if detail_data:
                            poi = detail_data.get("poiV2") or detail_data.get("poiV1") or {}
                            reviews_list = detail_data.get("reviews", {}).get("reviews", [])

                            # Normalizar detalles
                            detail_norm = self._normalize_detail(detail_data, fallback_web=web_url)
                            if detail_norm:
                                async with pool.acquire() as conn:
                                    async with conn.transaction():
                                        # Enriquecer spot en tabla spots
                                        from db import enriquecer_spot
                                        await enriquecer_spot(conn, spot_id, detail_norm, self.name)

                                        # Actualizar el source record para marcar details_fetched
                                        detail_norm["details_fetched"] = True
                                        await conn.execute("""
                                            UPDATE source_records
                                            SET normalized_data = normalized_data || $1::jsonb,
                                                raw_data = raw_data || $2::jsonb,
                                                last_seen = NOW()
                                            WHERE source = $3 AND source_id = $4
                                        """, json.dumps(detail_norm), json.dumps({"poiV2": poi}), self.name, sid)

                                        # Cargar reviews
                                        for rev_raw in reviews_list:
                                            fecha = None
                                            fecha_str = rev_raw.get("created")
                                            if fecha_str and len(fecha_str) >= 10:
                                                try:
                                                    fecha = datetime.strptime(fecha_str[:10], "%Y-%m-%d").date()
                                                except Exception:
                                                    pass

                                            from db import upsert_review

                                            inserted = await upsert_review(conn, {
                                                "spot_id": spot_id,
                                                "source": self.name,
                                                "source_review_id": f"cc_{rev_raw['id']}",
                                                "texto": rev_raw.get("text"),
                                                "texto_original": rev_raw.get("originalText") or rev_raw.get("text"),
                                                "rating": rev_raw.get("rating"),
                                                "autor": rev_raw.get("user", {}).get("displayName"),
                                                "fecha": fecha,
                                                "idioma": rev_raw.get("originalLocale"),
                                            })
                                            stats["reviews_nuevas"] += int(bool(inserted))

                                        stats["actualizados"] += 1
                    else:
                        logger.warning(f"[{self.name}] Error cargando {web_url}: status={r_web.status_code}")
                        stats["errores"] += 1
                except Exception as e:
                    logger.error(f"[{self.name}] Error enriqueciendo spot {sid}: {e}")
                    stats["errores"] += 1
                finally:
                    job_queue.task_done()

        # Iniciar trabajadores concurrentes compartiendo un único cliente
        num_workers = min(config.max_workers or 3, 5)
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            workers = []
            for _ in range(num_workers):
                workers.append(asyncio.create_task(enrich_worker(client)))

            await job_queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        return stats
