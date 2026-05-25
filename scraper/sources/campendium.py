"""Campendium — Tile-based map scanner + detail/reviews enrichment.

Phase 1: Scan zoom-8 OSM tiles via GET /api/v2/tiles/8/{x}/{y}?limit=80
Phase 2: Fetch POI details + reviews via GET /api/v1/pois/{id}

Coverage: North America (US, CA).
"""

import asyncio
import json
import math
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource


# North America bounding box for grid filtering
NA_BOUNDS = {
    "lat_min": 10.0, "lat_max": 75.0,
    "lon_min": -170.0, "lon_max": -50.0,
}

TILE_ZOOM = 8
TILE_LIMIT = 80


def _latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to OSM tile x, y at given zoom level."""
    lat_rad = math.radians(max(-85.0511, min(85.0511, lat)))
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _bbox_to_tiles(tl_lat: float, tl_lon: float, br_lat: float, br_lon: float, zoom: int = TILE_ZOOM) -> list[tuple[int, int]]:
    """Convert a bbox to the set of tiles that cover it."""
    x1, y1 = _latlon_to_tile(tl_lat, tl_lon, zoom)
    x2, y2 = _latlon_to_tile(br_lat, br_lon, zoom)
    x_min, x_max = min(x1, x2), max(x1, x2)
    y_min, y_max = min(y1, y2), max(y1, y2)
    tiles = []
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            tiles.append((x, y))
    return tiles


# Category label → GeoSpots tipo mapping
CATEGORY_MAP = {
    "rv park": "area_ac",
    "dump station": "area_ac",
    "public land": "wild",
    "free camping": "wild",
    "blm": "wild",
    "national forest": "wild",
    "walmart": "parking",
    "overnight parking": "parking",
    "rest area": "parking",
    "casino": "parking",
    "cracker barrel": "parking",
    "cabela's": "parking",
}


class CampendiumSource(AbstractSource):
    name = "campendium"
    rate_limit = 1.0
    grid_step = 2.0
    dedup_radius_m = 100.0

    TILE_URL = "https://maps.campendium.com/api/v2/tiles/{z}/{x}/{y}?limit={limit}"
    DETAIL_URL = "https://maps.campendium.com/api/v1/pois/{poi_id}"

    HEADERS = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "referer": "https://maps.campendium.com/",
    }

    async def generate_active_grid(self, pool, step=2.0, buffer=4):
        """Restrict grid to North America and use parent logic for active cells."""
        cells = await super().generate_active_grid(pool, step=step, buffer=buffer)

        na_cells = [
            c for c in cells
            if (NA_BOUNDS["lat_min"] <= c[2] and c[0] <= NA_BOUNDS["lat_max"]
                and NA_BOUNDS["lon_min"] <= c[1] and c[3] <= NA_BOUNDS["lon_max"])
        ]

        if not na_cells:
            logger.warning(f"[{self.name}] No active cells in NA after filtering. Generating bootstrap grid.")
            bootstrap_step = max(step * 5, 5.0)
            na_cells = []
            lat = NA_BOUNDS["lat_max"]
            while lat > NA_BOUNDS["lat_min"]:
                lon = NA_BOUNDS["lon_min"]
                while lon < NA_BOUNDS["lon_max"]:
                    na_cells.append((
                        round(lat, 4),
                        round(lon, 4),
                        round(lat - bootstrap_step, 4),
                        round(lon + bootstrap_step, 4),
                    ))
                    lon += bootstrap_step
                lat -= bootstrap_step
            logger.info(f"[{self.name}] Bootstrap grid NA: {len(na_cells)} cells")

        logger.info(f"[{self.name}] Grid filtrado NA: {len(na_cells)} celdas (de {len(cells)} globales)")
        return na_cells

    async def fetch_cell(self, client: httpx.AsyncClient, tl_lat: float, tl_lon: float, br_lat: float, br_lon: float) -> list[dict]:
        """Convert bbox to zoom-8 tiles and fetch GeoJSON features."""
        tiles = _bbox_to_tiles(tl_lat, tl_lon, br_lat, br_lon, TILE_ZOOM)
        all_features = []
        seen_ids = set()

        for x, y in tiles:
            url = self.TILE_URL.format(z=TILE_ZOOM, x=x, y=y, limit=TILE_LIMIT)
            try:
                await asyncio.sleep(self.rate_limit)
                resp = await client.get(url, timeout=20)
                if resp.status_code == 429:
                    logger.warning(f"[{self.name}] Rate limit 429 on tile {x}/{y}. Waiting 60s...")
                    await asyncio.sleep(60)
                    resp = await client.get(url, timeout=20)
                if resp.status_code != 200:
                    logger.warning(f"[{self.name}] Tile {x}/{y} returned {resp.status_code}")
                    continue
                data = resp.json()
                features = data.get("features", [])
                for feat in features:
                    fid = feat.get("properties", {}).get("id")
                    if fid and fid not in seen_ids:
                        seen_ids.add(fid)
                        all_features.append(feat)
            except Exception as e:
                logger.error(f"[{self.name}] Error fetching tile {x}/{y}: {e}")

        return all_features

    def normalize(self, raw: dict) -> dict | None:
        """Normalize a GeoJSON tile feature to GeoSpots schema."""
        try:
            props = raw.get("properties", {})
            geom = raw.get("geometry", {})
            coords = geom.get("coordinates", [])

            poi_id = props.get("id")
            if not poi_id:
                return None

            if len(coords) < 2:
                return None
            lon = float(coords[0])
            lat = float(coords[1])

            name = (props.get("name") or "Unknown").strip()[:200]

            # Type mapping from filters_appearance label
            filters = props.get("filters_appearance", {}) or {}
            label = (filters.get("label") or "").lower().strip()

            tipo = CATEGORY_MAP.get(label, "camping")

            # Categories from primary_category
            primary_cat = props.get("primary_category", {}) or {}
            cat_name = (primary_cat.get("category_name") or "").lower()
            if cat_name in ("rv-parks", "rv_parks"):
                tipo = "area_ac"

            # Rating
            rating = None
            rating_val = props.get("combined_avg_rating")
            if rating_val is not None:
                try:
                    rating = float(rating_val)
                    if rating <= 0:
                        rating = None
                except (ValueError, TypeError):
                    pass

            # Reviews count
            reviews_count = 0
            rev_cnt = props.get("reviews_count")
            if rev_cnt is not None:
                try:
                    reviews_count = int(rev_cnt)
                except (ValueError, TypeError):
                    pass

            # Price from place_detail
            place_detail = props.get("place_detail", {}) or {}
            price_level = place_detail.get("price")
            gratuito = None
            precio_info = None
            if price_level is not None:
                try:
                    price_int = int(price_level)
                    gratuito = price_int == 0
                    price_map = {0: "Free", 1: "$", 2: "$$", 3: "$$$", 4: "$$$$"}
                    precio_info = price_map.get(price_int, f"Level {price_int}")
                except (ValueError, TypeError):
                    pass

            # Photo URL
            fotos = []
            primary_img_url = props.get("primary_image_url")
            if primary_img_url:
                fotos.append(primary_img_url)

            # Web URL
            path = props.get("path") or ""
            web = f"https://www.campendium.com{path}" if path else None

            # Basic amenities from tile place_detail (booleans as strings or real booleans)
            def _bool(val):
                if val is None:
                    return None
                if isinstance(val, bool):
                    return val
                if isinstance(val, str):
                    return val.lower() == "true"
                return bool(val)

            return {
                "source_id": str(poi_id),
                "nombre": name,
                "lat": lat,
                "lon": lon,
                "tipo": tipo,
                "gratuito": gratuito,
                "precio_info": precio_info,
                "rating_promedio": rating,
                "num_reviews": reviews_count,
                "fotos_urls": fotos[:8],
                "web": web,
                "region": props.get("state"),
                # Si la API trae country_code lo usamos (cubre US y CA); si no,
                # dejamos None y el trigger geográfico de PostGIS clasifica por lat/lon
                "country_iso": (props.get("country_code") or "").lower() or None,
                "agua_potable": None,
                "wc_publico": _bool(place_detail.get("restrooms")),
                "ducha": _bool(place_detail.get("showers")),
                "wifi": _bool(place_detail.get("wifi")),
                "perros": _bool(place_detail.get("pets")),
                "acceso_grandes": _bool(place_detail.get("big_rigs")),
                "electricidad": _bool(place_detail.get("fifty_amp")) or _bool(place_detail.get("full_hookup")),
                "vaciado_grises": _bool(place_detail.get("dump_station")) or _bool(place_detail.get("sewer_hookup")),
                "vaciado_negras": _bool(place_detail.get("dump_station")) or _bool(place_detail.get("sewer_hookup")),
            }
        except Exception as e:
            logger.error(f"[{self.name}] Error normalizing: {e}")
            return None

    async def download_reviews(self, pool, config) -> dict:
        """Phase 2: Fetch full detail + reviews for each POI via /api/v1/pois/{id}."""
        from db import upsert_review, enriquecer_spot

        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}

        async with pool.acquire() as conn:
            jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id, sr.review_count, COALESCE(r.cnt, 0) as db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) as cnt
                    FROM reviews
                    WHERE source = 'campendium'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'campendium'
                  AND (
                    (sr.normalized_data->>'details_fetched') IS NULL
                    OR (sr.review_count > 0 AND COALESCE(r.cnt, 0) < sr.review_count)
                  )
                ORDER BY sr.review_count DESC;
            """)

        logger.info(f"[{self.name}] {len(jobs)} spots pending detail/review enrichment.")
        if not jobs:
            return stats

        job_queue = asyncio.Queue()
        for j in jobs:
            await job_queue.put(dict(j))

        async def enrich_worker(client: httpx.AsyncClient):
            while not job_queue.empty():
                try:
                    job = await job_queue.get()
                except asyncio.CancelledError:
                    break

                spot_id = job["spot_id"]
                sid = job["source_id"]

                detail_url = self.DETAIL_URL.format(poi_id=sid)
                try:
                    await asyncio.sleep(self.rate_limit)
                    resp = await client.get(detail_url, timeout=15)
                    if resp.status_code == 429:
                        logger.warning(f"[{self.name}] Detail 429 for POI {sid}. Sleeping 60s...")
                        await asyncio.sleep(60)
                        resp = await client.get(detail_url, timeout=15)

                    if resp.status_code != 200:
                        logger.warning(f"[{self.name}] Detail {resp.status_code} for POI {sid}")
                        stats["errores"] += 1
                        job_queue.task_done()
                        continue

                    detail = resp.json()
                except Exception as e:
                    logger.error(f"[{self.name}] Error fetching detail for {sid}: {e}")
                    stats["errores"] += 1
                    job_queue.task_done()
                    continue

                # Parse detailed amenities from place_detail
                pd = detail.get("place_detail", {}) or {}
                def _bool(val):
                    if val is None:
                        return None
                    if isinstance(val, bool):
                        return val
                    if isinstance(val, str):
                        return val.lower() == "true"
                    return bool(val)

                enrichment = {
                    "descripcion_en": detail.get("description"),
                    "web": detail.get("website") or None,
                    "wc_publico": _bool(pd.get("restrooms")),
                    "ducha": _bool(pd.get("showers")),
                    "wifi": _bool(pd.get("wifi")),
                    "perros": _bool(pd.get("pets")),
                    "acceso_grandes": _bool(pd.get("big_rigs")),
                    "electricidad": _bool(pd.get("fifty_amp")) or _bool(pd.get("full_hookup")),
                    "vaciado_grises": _bool(pd.get("dump_station")) or _bool(pd.get("sewer_hookup")),
                    "vaciado_negras": _bool(pd.get("dump_station")) or _bool(pd.get("sewer_hookup")),
                    "details_fetched": True,
                }

                # Remove None values to avoid overwriting existing data with nulls
                enrichment_clean = {k: v for k, v in enrichment.items() if v is not None}

                # Separate tracking flag from spot enrichment data
                spot_enrichment = {k: v for k, v in enrichment_clean.items() if k != "details_fetched"}

                try:
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            if spot_enrichment:
                                await enriquecer_spot(conn, spot_id, spot_enrichment, self.name)
                            await conn.execute("""
                                UPDATE source_records
                                SET normalized_data = normalized_data || $1::jsonb,
                                    last_seen = NOW()
                                WHERE source = $2 AND source_id = $3
                            """, json.dumps(enrichment_clean), self.name, sid)
                    stats["actualizados"] += 1
                except Exception as e:
                    logger.error(f"[{self.name}] DB error updating details for {sid}: {e}")
                    stats["errores"] += 1

                # Parse reviews from comments list
                comments = detail.get("comments", []) or []
                if comments:
                    try:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                for comment in comments:
                                    text = (comment.get("text") or "").strip() or None
                                    rating = None
                                    r_val = comment.get("reviewer_rating")
                                    if r_val is not None:
                                        try:
                                            rating = float(r_val)
                                        except (ValueError, TypeError):
                                            pass

                                    created_at = None
                                    created_str = comment.get("created_at")
                                    if created_str:
                                        # ISO 8601 con Z al final ("2024-01-01T12:00:00Z") o con +00:00
                                        try:
                                            created_at = datetime.fromisoformat(
                                                created_str.replace("Z", "+00:00")
                                            )
                                            if created_at.tzinfo is None:
                                                created_at = created_at.replace(tzinfo=timezone.utc)
                                        except Exception:
                                            try:
                                                created_at = datetime.strptime(
                                                    created_str[:19], "%Y-%m-%dT%H:%M:%S"
                                                ).replace(tzinfo=timezone.utc)
                                            except Exception:
                                                pass

                                    rev_dict = {
                                        "spot_id": spot_id,
                                        "source": self.name,
                                        "source_review_id": f"camp_{comment['id']}",
                                        "texto": text,
                                        "rating": rating,
                                        "autor": comment.get("author_name") or comment.get("reviewer_name") or "Campendium User",
                                        "fecha": created_at,
                                        "idioma": "en",
                                    }
                                    inserted = await upsert_review(conn, rev_dict)
                                    stats["reviews_nuevas"] += int(bool(inserted))
                    except Exception as e:
                        logger.error(f"[{self.name}] Error inserting reviews for {sid}: {e}")
                        stats["errores"] += 1

                job_queue.task_done()

        num_workers = min(getattr(config, 'max_workers', 3) or 3, 5)
        async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=20) as client:
            workers = [asyncio.create_task(enrich_worker(client)) for _ in range(num_workers)]
            await job_queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        return stats
