"""SearchForSites — scraper desde API oculta getDataAdvanced."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_searchforsites, merge_extra

MONTHS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"
}

def detect_language(text: str) -> str:
    if not text:
        return "en"
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
        return "en"
    return max_lang

async def fetch_and_save_reviews(client: httpx.AsyncClient, pool, spot_id: int, marker_id: str) -> int:
    url = "https://www.searchforsites.co.uk/pdo/getReviews.php"
    payload = {"markerID": marker_id}
    try:
        resp = await client.post(url, data=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        reviews = data.get("reviews", [])
        if not reviews:
            return 0
        
        saved = 0
        async with pool.acquire() as conn:
            for item in reviews:
                r_id = item.get("review", {}).get("id")
                r_text = item.get("review", {}).get("text")
                r_author = item.get("user", {}).get("name")
                r_updated = item.get("review", {}).get("updated")
                
                r_score = item.get("review", {}).get("rating", {}).get("score")
                rating_val = None
                if r_score is not None:
                    try:
                        rating_val = float(r_score) / 2.0
                    except (ValueError, TypeError):
                        pass

                if not r_id or not r_text:
                    continue

                fecha = None
                if r_updated:
                    try:
                        fecha = datetime.strptime(r_updated, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                idioma = detect_language(r_text)

                review_dict = {
                    "spot_id": spot_id,
                    "source": "searchforsites",
                    "source_review_id": str(r_id),
                    "texto": r_text,
                    "rating": rating_val,
                    "autor": r_author,
                    "fecha": fecha,
                    "idioma": idioma
                }
                
                from db import upsert_review
                await upsert_review(conn, review_dict)
                saved += 1
            if saved > 0:
                await conn.execute("""
                    UPDATE spots SET total_reviews = (
                        SELECT COUNT(*) FROM reviews WHERE spot_id = $1
                    ) WHERE id = $1
                """, spot_id)
        return saved
    except Exception as e:
        logger.error(f"[SFS] Error descargando reviews para marker {marker_id}: {e}")
        return 0

BASE_URL = "https://www.searchforsites.co.uk/pdo/getDataAdvanced.php"

HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": "https://www.searchforsites.co.uk",
    "referer": "https://www.searchforsites.co.uk/advanced.php",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-requested-with": "XMLHttpRequest"
}

# Países europeos para iterar y no pasarnos del límite de rows por petición
COUNTRIES = [
    "GB", "FR", "ES", "PT", "IT", "DE", "NL", "BE", "AT", "CH", 
    "NO", "SE", "FI", "DK", "IE", "GR", "PL", "CZ", "HR", "SI", 
    "RO", "HU", "SK", "LU", "AD", "MC", "LI", "AL", "BA", "RS", 
    "ME", "MK", "BG", "LT", "LV", "EE", "TR", "MA"
]

# Tipos de lugares en SFS (del 1 al 15 cubrimos todo: parkings, campings, aires...)
LOCATION_TYPES = list(range(1, 16))

class SearchForSitesSource(AbstractSource):
    name = "searchforsites"
    rate_limit = 1.0
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("SearchForSites usa iteración por país/tipo")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["latlng"]["lat"])
            lon = float(raw["latlng"]["lng"])
        except (KeyError, TypeError, ValueError):
            return None

        sfs_type = raw.get("Type", "")
        
        tipo_map = {
            # Camping
            "AC": "camping",
            "ACS": "camping",
            "CC": "camping",
            "CCC": "camping",
            "CP": "camping",
            
            # Area AC
            "ASN": "area_ac",
            "CL": "area_ac",
            "CS": "area_ac",
            "ACF": "area_ac",
            "CPA": "area_ac",
            "CCP": "area_ac",
            "BS": "area_ac",
            "FP": "area_ac",
            "PEC": "area_ac",
            "AS": "area_ac",
            "AA": "area_ac",
            "FC": "area_ac",
            
            # Parking
            "APN": "parking",
            "PS": "parking",
            "AP": "parking",
            
            # Naturaleza
            "APCC": "naturaleza",
            "WILD": "naturaleza"
        }

        if sfs_type not in tipo_map:
            return None
        
        tipo = tipo_map[sfs_type]

        # Fotos
        fotos = []
        thumb = raw.get("thumb")
        if thumb:
            fotos.append(f"https://www.searchforsites.co.uk/uploads/thumbs/{thumb}")

        # Coste
        cost = raw.get("cost", {})
        min_c = None
        max_c = None
        gratuito = None
        precio_aprox = None
        precio_info = None
        
        if isinstance(cost, dict):
            try:
                if "min" in cost:
                    min_c = float(cost["min"])
                if "max" in cost:
                    max_c = float(cost["max"])
            except (ValueError, TypeError):
                pass
                
            if min_c is not None and max_c is not None:
                gratuito = (min_c == 0 and max_c == 0)
                precio_aprox = (min_c + max_c) / 2.0
            elif min_c is not None:
                gratuito = (min_c == 0)
                precio_aprox = min_c
            elif max_c is not None:
                gratuito = (max_c == 0)
                precio_aprox = max_c

            if gratuito:
                precio_info = "Gratuito"
            elif precio_aprox is not None:
                precio_info = f"{precio_aprox:.2f} €"

        # Facilities Mapping
        facs_raw = raw.get("facilities", "")
        if isinstance(facs_raw, (int, float)):
            facs_list = [str(int(facs_raw))]
        elif isinstance(facs_raw, str):
            facs_list = [f.strip() for f in facs_raw.split(",") if f.strip()]
        else:
            facs_list = []

        if facs_list:
            agua_potable = "1" in facs_list
            vaciado_grises = "2" in facs_list
            vaciado_negras = "3" in facs_list
            wc_publico = "4" in facs_list
            electricidad = "5" in facs_list
            ducha = "6" in facs_list
            wifi = "7" in facs_list
        else:
            agua_potable = None
            vaciado_grises = None
            vaciado_negras = None
            wc_publico = None
            electricidad = None
            ducha = None
            wifi = None

        # Dogs allowed Mapping
        dog_val = raw.get("dog")
        if dog_val is not None:
            try:
                dog_int = int(dog_val)
                if dog_int in (1, 2):
                    perros = True
                elif dog_int == 0:
                    perros = False
                else:
                    perros = None
            except (ValueError, TypeError):
                perros = None
        else:
            perros = None

        address = raw.get("address", "")
        address_str = str(address) if address is not None else ""
        parts = [p.strip() for p in address_str.split(",") if p.strip()]
        region = parts[1] if len(parts) > 1 else (parts[0] if parts else "")

        # Normalizar rating a escala 0-5
        rt = raw.get("rT")
        rating_promedio = None
        if rt is not None:
            try:
                rating_promedio = float(rt) / 2.0
            except (ValueError, TypeError):
                pass

        # Conteo de reviews
        rvw_cnt = raw.get("rvwCnt")
        num_reviews = 0
        if rvw_cnt is not None:
            try:
                num_reviews = int(rvw_cnt)
            except (ValueError, TypeError):
                pass

        # Descripcion corta (sD)
        descripcion_en = raw.get("sD")
        if descripcion_en:
            descripcion_en = descripcion_en.strip()

        # Temporada de apertura
        temporada_apertura = None
        if "12" in facs_list:
            temporada_apertura = "All year"
        else:
            dates = raw.get("dates")
            if isinstance(dates, dict):
                op = dates.get("open")
                cl = dates.get("closed")
                try:
                    op_int = int(op) if op is not None else None
                    cl_int = int(cl) if cl is not None else None
                    if op_int == 1 and cl_int == 12:
                        temporada_apertura = "All year"
                    elif op_int in MONTHS and cl_int in MONTHS:
                        temporada_apertura = f"{MONTHS[op_int]} - {MONTHS[cl_int]}"
                except (ValueError, TypeError):
                    pass

        # pitchTypes mapping for acceso_grandes
        acceso_grandes = None
        pitch_raw = raw.get("pitchTypes", "")
        if isinstance(pitch_raw, (int, float)):
            pitch_list = [str(int(pitch_raw))]
        elif isinstance(pitch_raw, str):
            pitch_list = [p.strip() for p in pitch_raw.split(",") if p.strip()]
        else:
            pitch_list = []

        if pitch_list:
            large_ids = {"18", "20", "5", "12", "13"}
            medium_large_ids = {"17", "19"}
            if any(x in large_ids for x in pitch_list):
                acceso_grandes = True
            elif any(x in medium_large_ids for x in pitch_list):
                acceso_grandes = True
            elif all(x in {"14", "15", "16", "9"} for x in pitch_list):
                acceso_grandes = False

        norm = {
            "source_id": str(raw.get("ID")),
            "nombre": raw.get("Name", "Sin nombre").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "precio_aprox": precio_aprox,
            "precio_info": precio_info,
            "country_iso": raw.get("cID", "")[:2].upper(),
            "region": region,
            "rating_promedio": rating_promedio,
            "num_reviews": num_reviews,
            "descripcion_en": descripcion_en,
            "temporada_apertura": temporada_apertura,
            "acceso_grandes": acceso_grandes,
            "fotos_urls": fotos,
            "web": f"https://www.searchforsites.co.uk/marker.php?id={raw.get('ID')}",
            "agua_potable": agua_potable,
            "vaciado_grises": vaciado_grises,
            "vaciado_negras": vaciado_negras,
            "wc_publico": wc_publico,
            "electricidad": electricidad,
            "ducha": ducha,
            "wifi": wifi,
            "perros": perros,
            "raw_facilities": facs_raw,
        }
        return merge_extra(norm, extract_searchforsites(raw))

    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config,
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {},
        }

        # Obtener países dinámicamente de la base de datos
        db_countries = []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT DISTINCT UPPER(iso_a2) as iso FROM countries WHERE iso_a2 IS NOT NULL ORDER BY iso")
                db_countries = [r["iso"] for r in rows if r["iso"]]
        except Exception as e:
            logger.error(f"[SFS] Error cargando países desde la BD: {e}")

        sfs_supported = set(COUNTRIES)
        countries_to_use = [c for c in db_countries if c in sfs_supported] if db_countries else COUNTRIES

        async with httpx.AsyncClient(headers=HEADERS) as client:
            for ci, country in enumerate(countries_to_use, 1):
                for loc_type in LOCATION_TYPES:
                    payload = {
                        "browse": "true",
                        "country": country,
                        "locations": str(loc_type)
                    }

                    try:
                        resp = await client.post(BASE_URL, data=payload, timeout=30)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        logger.error(f"[SFS] Error obteniendo {country} tipo {loc_type}: {e}")
                        stats["errores"] += 1
                        await asyncio.sleep(2)
                        continue

                    items = data.get("results", {})
                    if not items:
                        await asyncio.sleep(self.rate_limit)
                        continue

                    logger.info(f"[SFS] {country} tipo {loc_type}: {len(items)} spots (Total ref: {data.get('total')})")

                    for key, raw in items.items():
                        norm = self.normalize(raw)
                        if not norm:
                            continue

                        sid = norm["source_id"]
                        norm_db = norm.copy()
                        norm_db.pop("raw_facilities", None)
                        spot_id = None
                        try:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    existente = await find_spot_cercano(
                                        conn, norm["lat"], norm["lon"], self.dedup_radius_m
                                    )
                                    if existente:
                                        spot_id = existente["id"]
                                        await enriquecer_spot(conn, spot_id, norm_db, self.name)
                                        stats["actualizados"] += 1
                                    else:
                                        norm_db["fuentes"] = [self.name]
                                        spot_id = await crear_spot(conn, norm_db)
                                        stats["nuevos"] += 1

                                    await upsert_source_record(
                                        conn, spot_id, self.name, sid, raw, norm
                                    )
                        except Exception as e:
                            logger.error(f"[SFS] Error spot '{norm.get('nombre')}': {e}")
                            stats["errores"] += 1

                        # Descarga de reviews fuera de la transacción
                        if spot_id and norm.get("num_reviews", 0) > 0:
                            saved_reviews = await fetch_and_save_reviews(client, pool, spot_id, sid)
                            stats["reviews_nuevas"] += saved_reviews
                            await asyncio.sleep(0.2)

                    await asyncio.sleep(self.rate_limit)

                await self.update_job_progress(pool, job_id, ci, len(countries_to_use), stats)

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[SFS] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        import httpx
        stats = {
            "nuevos": 0,
            "actualizados": 0,
            "reviews_nuevas": 0,
            "errores": 0
        }

        logger.info(f"[{self.name}] Buscando spots con reviews pendientes...")
        async with pool.acquire() as conn:
            review_jobs = await conn.fetch("""
                SELECT 
                    sr.spot_id, 
                    sr.source_id, 
                    sr.review_count,
                    COALESCE(r.cnt, 0) AS db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) AS cnt
                    FROM reviews
                    WHERE source = 'searchforsites'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'searchforsites'
                  AND sr.review_count > 0
                  AND (
                        (sr.normalized_data->>'reviews_fetched') IS NULL
                     OR COALESCE(r.cnt, 0) < sr.review_count
                  )
                ORDER BY sr.review_count DESC;
            """)

        logger.info(f"[{self.name}] {len(review_jobs)} spots con reviews pendientes.")
        if not review_jobs:
            return stats

        job_queue = asyncio.Queue()
        for r in review_jobs:
            await job_queue.put(dict(r))

        async def worker(client):
            while not job_queue.empty():
                try:
                    job = job_queue.get_nowait()
                except (asyncio.QueueEmpty, asyncio.CancelledError):
                    break

                spot_id = job["spot_id"]
                sid = job["source_id"]

                try:
                    await asyncio.sleep(self.rate_limit)
                    saved = await fetch_and_save_reviews(client, pool, spot_id, sid)
                    stats["reviews_nuevas"] += saved
                    
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE source_records
                            SET normalized_data = normalized_data || '{"reviews_fetched": true}'::jsonb
                            WHERE source = 'searchforsites' AND source_id = $1
                        """, sid)
                    stats["actualizados"] += 1
                except Exception as e:
                    logger.error(f"[{self.name}] Error descargando reviews para spot {sid}: {e}")
                    stats["errores"] += 1

                job_queue.task_done()

        async with httpx.AsyncClient(headers=HEADERS) as client:
            workers = [asyncio.create_task(worker(client)) for _ in range(3)]
            await asyncio.gather(*workers)

        return stats
