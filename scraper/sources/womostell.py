"""Womo Stellplatz / Camping-App.eu source implementation for GeoSpots scraper.

Data backend: Turso (distributed LibSQL/SQLite) with direct SQL access.
49,314 spots globally with full amenity data, pricing, and ratings.

Endpoint: POST https://ca-sites-letsgo.aws-eu-west-1.turso.io/v2/pipeline
Auth: Bearer JWT (static token captured from mobile app)
"""

import asyncio
import json
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource


# Mapping: place_type_id -> tipo canónico GeoSpots
PLACE_TYPE_MAP = {
    1: "parking",      # Parkplatz
    2: "area_ac",      # Stellplatz / Wohnmobilstellplatz
    3: "area_ac",      # Campingähnlicher Platz
    4: "camping",      # Camping
    5: "naturaleza",   # Wildnis / freie Natur
    6: "parking",      # Autobahnraststätte
    7: "parking",      # Supermarkt / Einkaufszentrum
    8: "area_ac",      # Weingut / Bauernhof
    9: "area_ac",      # Hotel / Gasthof
    10: "parking",     # Museum / Freizeit
    11: "area_ac",     # Hafen / Marina
    12: "wild",        # Tolerierter Platz
}


class WomoStellplatzSource(AbstractSource):
    name = "womostell"
    rate_limit = 0.2  # 200ms between requests (Turso is fast)
    dedup_radius_m = 60.0

    TURSO_URL = "https://ca-sites-letsgo.aws-eu-west-1.turso.io/v2/pipeline"
    TURSO_TOKEN = (
        "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9"
        ".eyJpYXQiOjE3Njk4OTYzMTAsImlkIjoiNDA0YTM2YWQtMDIyYS00YzI1LTk2ZjEt"
        "YjkwMTkyM2VkYzMyIiwicmlkIjoiYzI2ZWJkZTEtNjA0MS00MmJhLTk3OTgtMzI2"
        "NDMwYjdmZjhkIn0.Kh915tWuuTjpFwXAbba1RktoqNpy0pQz0W7PInO4quMvNlusg"
        "pU87hUkbx6iawPX9lov9sQbx_0Zijp1Kob8BQ"
    )
    IMAGE_BASE = "https://nbg1.your-objectstorage.com/caimg/{place_id}/webp_mid/{filename}"

    BATCH_SIZE = 500  # rows per SQL query

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        raise NotImplementedError("WomoStellplatz uses direct Turso SQL, not bbox grid.")

    def _headers(self):
        return {
            "authorization": f"Bearer {self.TURSO_TOKEN}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }

    async def _run_sql(self, client: httpx.AsyncClient, sql: str, args=None) -> dict:
        stmt = {"sql": sql}
        if args:
            stmt["args"] = args
        payload = {
            "requests": [
                {"type": "execute", "stmt": stmt},
                {"type": "close"}
            ]
        }
        await asyncio.sleep(self.rate_limit)
        resp = await client.post(self.TURSO_URL, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        return result["results"][0]["response"]["result"]

    def normalize(self, raw: dict) -> dict | None:
        try:
            place_id = raw.get("place_id")
            lat = raw.get("latitude")
            lon = raw.get("longitude")
            if not place_id or lat is None or lon is None:
                return None

            try:
                lat = float(lat)
                lon = float(lon)
            except (ValueError, TypeError):
                return None

            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                return None

            place_type_id = raw.get("place_type_id")
            tipo = PLACE_TYPE_MAP.get(place_type_id, "area_ac")

            # Amenities - booleanas (1=si, 0=no, None=desconocido)
            def _bool(val):
                if val is None:
                    return None
                return bool(int(val)) if str(val).isdigit() else None

            agua_potable = _bool(raw.get("b_water"))
            electricidad = _bool(raw.get("b_electricity"))
            vaciado_negras = _bool(raw.get("b_chemical_wc"))
            wc_publico = _bool(raw.get("b_wc"))
            ducha = _bool(raw.get("b_shower"))
            wifi = _bool(raw.get("b_wifi"))
            perros = _bool(raw.get("b_animals_allowed"))
            acceso_grandes = _bool(raw.get("b_long_campers"))
            vaciado_grises = _bool(raw.get("b_disposal"))
            reserva_req = _bool(raw.get("b_reservation"))

            # Precio
            precio_aprox = None
            precio_info = None
            price_val = raw.get("price")
            if price_val is not None:
                try:
                    precio_aprox = float(price_val)
                    if precio_aprox > 0:
                        precio_info = f"{precio_aprox:.2f} EUR"
                except (ValueError, TypeError):
                    pass
            gratuito = (precio_aprox == 0.0) if precio_aprox is not None else None

            # Capacidad
            num_plazas = None
            cap = raw.get("capacity")
            if cap is not None:
                try:
                    num_plazas = int(cap)
                except (ValueError, TypeError):
                    pass

            # Rating promedio (rm = rating medio, escala 1-5)
            rating = None
            rm = raw.get("rm")
            if rm is not None:
                try:
                    rating = float(rm)
                    if rating <= 0:
                        rating = None
                except (ValueError, TypeError):
                    pass

            # Temporada
            open_from = raw.get("open_from")
            open_to = raw.get("open_to")
            temporada = None
            month_names = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
                           "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
            if open_from and open_to:
                try:
                    of = int(open_from)
                    ot = int(open_to)
                    if of == 1 and ot == 12:
                        temporada = "Todo el año"
                    else:
                        temporada = f"{month_names[of]} - {month_names[ot]}"
                except (ValueError, TypeError, IndexError):
                    pass

            # Fotos
            images_raw = raw.get("images") or ""
            photos = []
            for img in images_raw.split(","):
                img = img.strip()
                if img:
                    photos.append(
                        self.IMAGE_BASE.format(place_id=place_id, filename=img)
                    )

            # Web
            int_id = raw.get("int_id") or ""
            web = f"https://www.womo-stellplatz.eu/place/{int_id}" if int_id else None

            return {
                "source_id": str(place_id),
                "nombre": (raw.get("name") or "Sin nombre").strip()[:200],
                "lat": lat,
                "lon": lon,
                "tipo": tipo,
                "gratuito": gratuito,
                "precio_info": precio_info,
                "precio_aprox": precio_aprox,
                "num_plazas": num_plazas,
                "rating_promedio": rating,
                "agua_potable": agua_potable,
                "electricidad": electricidad,
                "vaciado_negras": vaciado_negras,
                "vaciado_grises": vaciado_grises,
                "wc_publico": wc_publico,
                "ducha": ducha,
                "wifi": wifi,
                "perros": perros,
                "acceso_grandes": acceso_grandes,
                "reserva_req": reserva_req,
                "temporada_apertura": temporada,
                "descripcion_de": (raw.get("description") or "").strip() or None,
                "web": web,
                "fotos_urls": photos[:8],
                "country_iso": None,  # Se infiere por region_id en segunda fase
                "region": raw.get("city") or None,
            }
        except Exception as e:
            logger.error(f"[womostell] Error normalizing place {raw.get('place_id')}: {e}")
            return None

    async def run(self, pool, config, log_id: int) -> dict:
        """Full database pull via Turso SQL pipeline API."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        seen_ids = set()

        async with httpx.AsyncClient(headers=self._headers(), follow_redirects=True, timeout=30) as client:
            # Count total
            count_result = await self._run_sql(client, "SELECT COUNT(*) FROM places WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
            total = int(count_result["rows"][0][0]["value"])
            logger.info(f"[womostell] Total places con coordenadas: {total}")

            offset = 0
            while offset < total:
                logger.info(f"[womostell] Fetching places {offset}-{offset + self.BATCH_SIZE}...")
                sql = (
                    "SELECT place_id, name, latitude, longitude, place_type_id, "
                    "capacity, b_electricity, b_water, b_chemical_wc, b_wc, b_shower, "
                    "b_animals_allowed, b_wifi, b_long_campers, b_disposal, b_reservation, "
                    "price, description, images, homepage, city, open_from, open_to, "
                    "rm, int_id "
                    f"FROM places WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
                    f"ORDER BY place_id LIMIT {self.BATCH_SIZE} OFFSET {offset}"
                )
                try:
                    result = await self._run_sql(client, sql)
                except Exception as e:
                    logger.error(f"[womostell] Error fetching batch at offset {offset}: {e}")
                    stats["errores"] += 1
                    offset += self.BATCH_SIZE
                    continue

                cols = [c["name"] for c in result["cols"]]
                rows = result["rows"]
                if not rows:
                    break

                for row in rows:
                    raw = {}
                    for i, col in enumerate(cols):
                        raw[col] = row[i]["value"] if row[i]["type"] != "null" else None

                    norm = self.normalize(raw)
                    if not norm:
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
                                    nombre=norm.get("nombre"),
                                    tipo=norm.get("tipo")
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
                        logger.error(f"[womostell] Error saving place {sid}: {e}")
                        stats["errores"] += 1

                offset += self.BATCH_SIZE

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[womostell] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config) -> dict:
        from db import upsert_review

        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}

        # Get spots pending reviews
        async with pool.acquire() as conn:
            jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id
                FROM source_records sr
                WHERE sr.source = 'womostell'
                  AND (sr.normalized_data->>'reviews_fetched') IS NULL
                ORDER BY sr.source_id::integer
            """)

        logger.info(f"[womostell] {len(jobs)} spots con reviews pendientes.")
        if not jobs:
            return stats

        async with httpx.AsyncClient(headers=self._headers(), follow_redirects=True, timeout=30) as client:
            # Pull ratings in batches of 200 place_ids
            job_list = [dict(j) for j in jobs]
            batch_size = 200

            for batch_start in range(0, len(job_list), batch_size):
                batch = job_list[batch_start:batch_start + batch_size]
                place_ids = [j["source_id"] for j in batch]
                placeholders = ", ".join(place_ids)
                spot_map = {j["source_id"]: j["spot_id"] for j in batch}

                sql = (
                    f"SELECT id, place_id, name, ratingtext, r1, r2, r3, r4, r5, rm, "
                    f"comeBack, ratingtime FROM place_ratings "
                    f"WHERE place_id IN ({placeholders}) AND published = 1"
                )
                try:
                    result = await self._run_sql(client, sql)
                except Exception as e:
                    logger.error(f"[womostell] Error fetching ratings batch: {e}")
                    stats["errores"] += 1
                    continue

                cols = [c["name"] for c in result["cols"]]
                inserted = 0
                for row in result["rows"]:
                    raw = {cols[i]: (row[i]["value"] if row[i]["type"] != "null" else None)
                           for i in range(len(cols))}

                    pid = str(raw.get("place_id"))
                    spot_id = spot_map.get(pid)
                    if not spot_id:
                        continue

                    # Rating promedio de r1-r5 (escala 1-5 cada uno)
                    r_vals = [raw.get(f"r{i}") for i in range(1, 6)]
                    r_vals = [float(v) for v in r_vals if v is not None and str(v).replace('.','').isdigit()]
                    rating = sum(r_vals) / len(r_vals) if r_vals else None

                    # Fecha
                    fecha = None
                    rt = raw.get("ratingtime")
                    if rt:
                        try:
                            fecha = datetime.strptime(rt[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        except Exception:
                            pass

                    texto = (raw.get("ratingtext") or "").strip() or None
                    autor = (raw.get("name") or "Usuario WomoStellplatz").strip()

                    rev = {
                        "spot_id": spot_id,
                        "source": self.name,
                        "source_review_id": str(raw.get("id")),
                        "texto": texto,
                        "rating": rating,
                        "autor": autor,
                        "fecha": fecha,
                        "idioma": "de",
                    }
                    try:
                        async with pool.acquire() as conn:
                            await upsert_review(conn, rev)
                        inserted += 1
                    except Exception as e:
                        logger.error(f"[womostell] Error saving review {raw.get('id')}: {e}")
                        stats["errores"] += 1

                # Mark batch as fetched
                async with pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE source_records
                        SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb
                        WHERE source = 'womostell' AND source_id = ANY($1::text[])
                    """, place_ids)

                stats["reviews_nuevas"] += inserted
                stats["actualizados"] += len(batch)
                logger.info(f"[womostell] Batch {batch_start}-{batch_start+len(batch)}: {inserted} reviews")

        return stats
