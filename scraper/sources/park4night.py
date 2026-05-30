"""Park4Night — scraper usando API interna guest."""

import asyncio
import json
import random
from datetime import datetime
from loguru import logger
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_park4night, merge_extra

P4N_BASE = "https://guest.park4night.com/services/V4.1"
P4N_LUGARES = f"{P4N_BASE}/lieuxGetFilter.php"
P4N_REVIEWS = f"{P4N_BASE}/commGet.php"
# P4N_DETALLE = f"{P4N_BASE}/lieuGetDetail.php"  # reservado, no usado actualmente

CODIGO_MAP = {"A": "area_ac", "P": "parking", "C": "camping", "N": "naturaleza", "H": "otro", "S": "otro"}
TIPO_MAP = {1: "area_ac", 2: "parking", 3: "camping", 4: "naturaleza", 5: "picnic", 8: "parking"}


def _b(raw: dict, key: str) -> bool | None:
    """Lee un flag booleano. P4N puede devolver "1"/"0", "true"/"false", o boolean
    nativo dependiendo del campo y versión de API. Defensivo contra los tres."""
    v = raw.get(key)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "oui", "si"):
        return True
    if s in ("0", "false", "no", "non"):
        return False
    return None


def _to_int_safe(v) -> int | None:
    """P4N devuelve campos numéricos como strings ('2', '15', '50'). Falla
    silenciosamente si llega un string raro ('N/A', '', None)."""
    if v is None:
        return None
    try:
        return int(float(v))  # acepta '2', '2.0', 2
    except (TypeError, ValueError):
        return None


def _to_float_safe(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=4, min=4, max=16),
    retry=(retry_if_exception_type(httpx.TimeoutException) | retry_if_exception_type(httpx.HTTPError)),
    reraise=True
)
async def _fetch_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
    }
    resp = await client.get(url, params=params, headers=headers, timeout=15)
    if resp.status_code == 429:
        logger.warning("P4N Rate limit 429. Esperando 60s...")
        await asyncio.sleep(60)
        resp.raise_for_status()
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception as e:
        logger.error(f"Error parsing JSON response: {e}")
        return {}


class Park4NightSource(AbstractSource):
    """Park4Night usa puntos GPS (no bbox). Override completo de run()."""

    name = "park4night"
    rate_limit = 2.0
    grid_step = 0.25
    dedup_radius_m = 80.0

    HEADERS = {
        "User-Agent": "GeoSpots/1.0",
        "Accept": "application/json",
    }

    # P4N no usa bbox sino puntos lat/lon → no usamos fetch_cell
    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        raise NotImplementedError("P4N usa grid de puntos, no bbox")

    def normalize(self, raw: dict) -> dict | None:
        """Convierte raw P4N al esquema normalizado."""
        try:
            if "id" not in raw:
                return None

            p4n_id = _to_int_safe(raw["id"])
            if p4n_id is None:
                return None
            nombre = (raw.get("name") or raw.get("titre") or "Sin nombre").strip()[:200]

            tipo = None
            code = raw.get("code")
            if code and code in CODIGO_MAP:
                tipo = CODIGO_MAP[code]
            else:
                id_type = _to_int_safe(raw.get("id_type")) or 0
                tipo = TIPO_MAP.get(id_type, "otro")

            lat = _to_float_safe(raw.get("latitude"))
            lon = _to_float_safe(raw.get("longitude"))
            if lat is None or lon is None:
                return None

            gratuito = None
            prix = raw.get("prix")
            if prix is not None:
                s = str(prix).strip()
                if s == "0":
                    gratuito = True
                elif s == "1":
                    gratuito = False

            rating = _to_float_safe(raw.get("note_moyenne") or raw.get("note"))
            if rating is not None and rating <= 0:
                rating = None

            num_reviews = _to_int_safe(raw.get("nb_commentaires") or raw.get("nb_comm")) or 0

            altura = _to_float_safe(raw.get("hauteur_limite"))
            if altura is not None and altura <= 0:
                altura = None

            num_plazas = _to_int_safe(raw.get("nb_places"))
            if num_plazas is not None and num_plazas <= 0:
                num_plazas = None

            # Defensive: "photos" puede venir como None explícito (no missing)
            fotos = [
                {"large": f.get("link_large"), "thumb": f.get("link_thumb")}
                for f in (raw.get("photos") or []) if isinstance(f, dict) and f.get("link_large")
            ]

            norm = {
                "source_id": str(p4n_id),
                "nombre": nombre,
                "lat": lat,
                "lon": lon,
                "tipo": tipo,
                "gratuito": gratuito,
                "precio_info": raw.get("prix_stationnement") or raw.get("prix_services"),
                "rating_promedio": rating,
                "num_reviews": num_reviews,
                "num_plazas": num_plazas,
                "altura_max_m": altura,
                "country_iso": str(raw.get("pays_iso")).lower() if raw.get("pays_iso") else None,
                "region": raw.get("ville"),
                "web": raw.get("site_internet"),
                "telefono": raw.get("tel"),
                "email": raw.get("mail"),
                "descripcion_fr": raw.get("description_fr") or raw.get("description"),
                "descripcion_en": raw.get("description_en"),
                "descripcion_de": raw.get("description_de"),
                "descripcion_es": raw.get("description_es"),
                "descripcion_it": raw.get("description_it"),
                "descripcion_nl": raw.get("description_nl"),
                "agua_potable": _b(raw, "point_eau") or _b(raw, "water"),
                "vaciado_negras": _b(raw, "eau_noire") or _b(raw, "black_water"),
                "vaciado_grises": _b(raw, "eau_usee") or _b(raw, "grey_water"),
                "electricidad": _b(raw, "electricite") or _b(raw, "electricity"),
                "ducha": _b(raw, "douche") or _b(raw, "shower"),
                "wifi": _b(raw, "wifi"),
                "wc_publico": _b(raw, "wc_public") or _b(raw, "wc"),
                "perros": _b(raw, "animaux") or _b(raw, "animal"),
                "acceso_grandes": _b(raw, "camping_car"),
                "fotos_urls": fotos,
            }
            return merge_extra(norm, extract_park4night(raw))
        except Exception as e:
            logger.error(f"Error normalizando P4N id={raw.get('id')}: {e}")
            return None

    def _parse_review(self, raw: dict, spot_id: int) -> dict | None:
        try:
            import sys
            import os
            # Ensure root folder is in sys.path so we can import from enrichment
            root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            if root_dir not in sys.path:
                sys.path.append(root_dir)
            from enrichment.review_cleaner import detect_language

            texto = (raw.get("commentaire") or raw.get("comment") or "").strip() or None
            rating_val = raw.get("note")
            rating = int(rating_val) if rating_val and 1 <= int(rating_val) <= 5 else None

            fecha = None
            fecha_str = str(raw.get("date_creation") or raw.get("date_insert", ""))
            if len(fecha_str) >= 10:
                try:
                    fecha = datetime.strptime(fecha_str[:10], "%Y-%m-%d").date()
                except Exception:
                    pass

            return {
                "spot_id": spot_id,
                "source": "park4night",
                "source_review_id": f"p4n_{raw['id']}",
                "texto": texto,
                "rating": rating,
                "fecha": fecha,
                "autor": raw.get("uuid") or raw.get("pseudo") or raw.get("login"),
                "idioma": detect_language(texto),
            }
        except Exception as e:
            logger.error(f"[{self.name}] Error parsing review for spot {spot_id}: {e}")
            return None

    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
        """Adaptive global crawler using queue-based quadtree subdivision."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, upsert_review,
            finish_scraper_log, update_fuente_config
        )
        from datetime import timezone

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        # 1. Generate starting grid points (Level 0: 1.0° cells)
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT floor(lat) as lat_idx, floor(lon) as lon_idx FROM spots")
        
        existing_cells = {(int(r['lat_idx']), int(r['lon_idx'])) for r in rows}
        
        if existing_cells:
            # Dilate cells with a 4-cell buffer to cover all adjacent lands
            buffered = set()
            for lat_idx, lon_idx in existing_cells:
                for dlat in range(-4, 5):
                    for dlon in range(-4, 5):
                        buffered.add((lat_idx + dlat, lon_idx + dlon))
            logger.info(f"[park4night] Dilated {len(existing_cells)} active cells into {len(buffered)} cells for global scanning.")
            
            # Start queue with centers of dilated 1.0° cells
            initial_points = []
            for lat_idx, lon_idx in buffered:
                initial_points.append((lat_idx + 0.5, lon_idx + 0.5, 1.0, 0))
        else:
            # Fallback to global 2.0° grid if no spots exist
            logger.info("[park4night] No existing spots in DB. Initializing with global 2.0° grid.")
            initial_points = []
            lat = -60.0
            while lat <= 75.0:
                lon = -180.0
                while lon <= 180.0:
                    initial_points.append((lat + 1.0, lon + 1.0, 2.0, 0))
                    lon += 2.0
                lat += 2.0

        random.shuffle(initial_points)
        
        # 2. Asynchronous Queue Processing
        queue = asyncio.Queue()
        for p in initial_points:
            await queue.put(p)
            
        seen_ids: set[str] = set()
        total_queries = 0
        max_depth = 3  # depth 0 (1.0°/2.0°), 1 (0.5°/1.0°), 2 (0.25°/0.5°), 3 (0.125°/0.25°)
        
        # We will log progress periodically
        processed_count = 0
        total_initial = len(initial_points)

        async with httpx.AsyncClient(follow_redirects=True, headers=self.HEADERS) as client:
            
            async def worker():
                nonlocal total_queries, processed_count
                while True:
                    try:
                        lat, lon, step, depth = await queue.get()
                    except asyncio.CancelledError:
                        break
                    
                    try:
                        # Rate limit delay per worker query
                        await asyncio.sleep(self.rate_limit)
                        
                        total_queries += 1
                        data = await _fetch_json(client, P4N_LUGARES, {
                            "latitude": lat, "longitude": lon
                        })
                        
                        lugares_raw = data.get("lieux") or data.get("tab_lieux") or []
                        spots_count = len(lugares_raw)
                        
                        # Process spots returned
                        for raw in lugares_raw:
                            norm = self.normalize(raw)
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
                                        existente = await find_spot_cercano(
                                            conn, norm["lat"], norm["lon"],
                                            self.dedup_radius_m,
                                            source=self.name, source_id=sid,
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
                                            conn, spot_id, self.name, sid, raw, norm
                                        )
                            except Exception as e:
                                logger.error(f"[park4night] Error saving spot '{norm.get('nombre')}': {e}")
                                stats["errores"] += 1
                                
                        # Subdivide if capped (exactly 100 spots returned) and depth < max_depth
                        if spots_count == 100 and depth < max_depth:
                            new_step = step / 2.0
                            new_depth = depth + 1
                            sub_points = [
                                (lat - new_step / 2, lon - new_step / 2),
                                (lat - new_step / 2, lon + new_step / 2),
                                (lat + new_step / 2, lon - new_step / 2),
                                (lat + new_step / 2, lon + new_step / 2),
                            ]
                            for sub_lat, sub_lon in sub_points:
                                await queue.put((sub_lat, sub_lon, new_step, new_depth))
                                
                    except Exception as e:
                        logger.warning(f"[park4night] Error scanning point {lat},{lon}: {e}")
                        stats["errores"] += 1
                    finally:
                        if depth == 0:
                            processed_count += 1
                        queue.task_done()

            # Launch workers — defensivo: config.max_workers puede ser None.
            # Mantener concurrencia baja (max 5) para no disparar el WAF anti-bot.
            num_workers = min(getattr(config, 'max_workers', None) or 3, 5)
            workers = []
            for _ in range(num_workers):
                workers.append(asyncio.create_task(worker()))
                
            # Log progress in background
            async def progress_reporter():
                while not queue.empty() or queue.unfinished_tasks > 0:
                    await asyncio.sleep(15)
                    logger.info(
                        f"[park4night] Grid Scan | level0_progress={processed_count}/{total_initial} | "
                        f"total_queries={total_queries} | queue_backlog={queue.qsize()} | "
                        f"uniq_spots={len(seen_ids)} nuevos={stats['nuevos']} upd={stats['actualizados']} err={stats['errores']}"
                    )
                    await self.update_job_progress(pool, job_id, processed_count, total_initial, stats)
            
            reporter_task = asyncio.create_task(progress_reporter())
            
            # Wait for queue to finish processing
            await queue.join()
            
            # Stop reporter and workers
            reporter_task.cancel()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        # 3. Finalize Logs
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)
 
        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[park4night] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        from db import upsert_review
        
        stats = {
            "nuevos": 0,
            "actualizados": 0,
            "reviews_nuevas": 0,
            "errores": 0
        }
        
        logger.info(f"[{self.name}] Buscando spots pendientes de reviews...")
        async with pool.acquire() as conn:
            review_jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id, sr.review_count, COALESCE(r.cnt, 0) as db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) as cnt
                    FROM reviews
                    WHERE source = 'park4night'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'park4night' 
                  AND sr.review_count > 0 
                  AND (sr.normalized_data->>'reviews_fetched') IS NULL
                  AND COALESCE(r.cnt, 0) < sr.review_count
                ORDER BY sr.review_count DESC;
            """)
            
        logger.info(f"[{self.name}] Encontrados {len(review_jobs)} spots pendientes de reviews.")
        
        if not review_jobs:
            return stats
            
        rev_queue = asyncio.Queue()
        for r in review_jobs:
            await rev_queue.put(dict(r))
            
        progress_state = [0, len(review_jobs)]
            
        async def review_worker(client):
            while not rev_queue.empty():
                try:
                    job = await rev_queue.get()
                except asyncio.CancelledError:
                    break
                
                spot_id = job["spot_id"]
                sid = job["source_id"]
                
                try:
                    await asyncio.sleep(self.rate_limit)
                    rev_data = await _fetch_json(client, P4N_REVIEWS, {
                        "lieu_id": int(sid)
                    })
                    rev_list = rev_data.get("commentaires") or rev_data.get("historique") or []
                    
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            for rev_raw in rev_list:
                                rev = self._parse_review(rev_raw, spot_id)
                                if rev:
                                    inserted = await upsert_review(conn, rev)
                                    stats["reviews_nuevas"] += int(bool(inserted))
                            
                            # Mark as reviews_fetched in source_records
                            await conn.execute("""
                                UPDATE source_records
                                SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb,
                                    review_count = GREATEST(COALESCE(review_count, 0), $1::int)
                                WHERE source = 'park4night' AND spot_id = $2
                            """, len(rev_list), spot_id)
                            
                    progress_state[0] += 1
                    if progress_state[0] % 20 == 0:
                        logger.info(f"[{self.name}] Progreso reviews: {progress_state[0]}/{progress_state[1]} spots | nuevas={stats['reviews_nuevas']} errores={stats['errores']}")
                        if job_id:
                            try:
                                async with pool.acquire() as conn2:
                                    await conn2.execute(
                                        "UPDATE scraper_jobs SET progress = $1::jsonb WHERE id = $2",
                                        json.dumps({"processed_spots": progress_state[0], "total_spots": progress_state[1], "stats": stats}), job_id
                                    )
                            except Exception:
                                pass
                            
                except Exception as e:
                    logger.warning(f"[{self.name}] Error fetching reviews for spot {sid}: {e}")
                    stats["errores"] += 1
                finally:
                    rev_queue.task_done()
                    
        # Iniciar trabajadores concurrentes compartiendo un único cliente httpx
        num_workers = min(config.max_workers or 3, 5)
        async with httpx.AsyncClient(follow_redirects=True, headers=self.HEADERS) as client:
            workers = []
            for _ in range(num_workers):
                workers.append(asyncio.create_task(review_worker(client)))
                
            await rev_queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            
        return stats
