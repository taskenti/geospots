"""Campy — scraper para la fuente Campy app (GraphQL API)."""

import asyncio
import json
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_campy, merge_extra

def clean_surrogates(text: str) -> str:
    if not text:
        return ""
    return "".join(c for c in text if not (0xD800 <= ord(c) <= 0xDFFF))

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

def infer_dogs(text: str) -> bool | None:
    if not text:
        return None
    text = text.lower()
    forbidden = ["no dogs", "no pets", "dogs not allowed", "pets not allowed", "keine hunde", "hunde verboten", "no perros", "sin perros", "pas de chien"]
    allowed = ["dogs allowed", "pets allowed", "dogs welcome", "hunde erlaubt", "hunde willkommen", "se admiten perros", "perros bienvenidos", "chiens admis"]
    for kw in forbidden:
        if kw in text:
            return False
    for kw in allowed:
        if kw in text:
            return True
    return None

GRAPHQL_URL = "https://graphql-server-132719581042.europe-west1.run.app/"

# Subconjunto de LocationFull con lo que aprovechamos en download_reviews:
# contacto (website/email/phone), facilidades, resumen IA ("sam") y reviews
# (chupadas de Google). El endpoint NO requiere autenticación (verificado).
LOCATION_FULL_QUERY = """query LocationFull($uid: String!, $language: String) {
  location: locationFull(uid: $uid, language: $language) {
    uid
    website
    email
    phone
    reviewsCount
    facilities { title available }
    reviewSummary { pros cons summary }
    reviews {
      id
      externalSource
      rating
      comment
      updatedAt
      userDisplayName
      translation { comment sourceLanguage }
    }
  }
}"""


def _map_facilities(facilities) -> dict:
    """Mapea la lista facilities {title, available} de campy a columnas spots."""
    out = {}
    for fac in facilities or []:
        if not isinstance(fac, dict):
            continue
        available = fac.get("available")
        if available is False or available == 0:
            continue
        title = (fac.get("title") or "").lower()
        if "wifi" in title or "internet" in title:
            out["wifi"] = True
        if "toilet" in title or "wc" in title:
            out["wc_publico"] = True
        if "shower" in title:
            out["ducha"] = True
        if "water" in title:
            out["agua_potable"] = True
        if "electricity" in title or "power" in title:
            out["electricidad"] = True
        if "grey" in title or "gray" in title:
            out["vaciado_grises"] = True
        if "chemical" in title or "black" in title:
            out["vaciado_negras"] = True
        if "dog" in title or "pet" in title:
            out["perros"] = True
    return out


class CampySource(AbstractSource):
    name = "campy"
    rate_limit = 1.5
    grid_step = 1.0
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        lat = (tl_lat + br_lat) / 2
        lng = (tl_lon + br_lon) / 2
        # Cubrir la celda de grid_step=1.0 con un radio de 90km
        radius = 90.0

        q = {
            "operationName": "LocationsWithinRadius",
            "variables": {
                "lat": lat,
                "lng": lng,
                "radius": radius,
                "filters": {
                    "type": [],
                    "price": []
                }
            },
            "query": """query LocationsWithinRadius($lat: Float!, $lng: Float!, $radius: Float!, $filters: FiltersInput) {
              locations: locationsWithinRadius(
                lat: $lat
                lng: $lng
                radius: $radius
                filters: $filters
              ) {
                uid
                isTopQuality
                campsite_campy_rating
                title
                address
                country: country_short
                city
                description
                image
                places
                price
                rating
                latitude
                longitude
                type
                camperSize
                dateOpenFrom
                dateOpenTo
                facilities {
                  title
                  price
                  description
                  available
                }
              }
            }"""
        }

        try:
            resp = await client.post("https://graphql-server-132719581042.europe-west1.run.app/", json=q, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("locations", []) or []
        except Exception as e:
            logger.error(f"[CAMPY] Error fetching center ({lat}, {lng}): {e}")
            return []

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["latitude"])
            lon = float(raw["longitude"])
            uid = str(raw["uid"])
        except (KeyError, TypeError, ValueError):
            return None

        # Mapeo de tipos. Tipos reales observados en la API: camp/van/microcamping
        # (el endpoint público de Campy NO devuelve "parking" en producción, pero se
        # mantiene por defensa anti-cambios futuros)
        t_raw = (raw.get("type") or "").lower().strip()
        tipo = "naturaleza"
        if t_raw in ("camp", "camping", "microcamping"):
            tipo = "camping"
        elif t_raw == "van":
            tipo = "area_ac"
        elif t_raw == "parking":
            tipo = "parking_publico"

        # Fotos
        fotos = []
        img = raw.get("image")
        if img and isinstance(img, str) and img.strip().startswith("http"):
            fotos.append(img.strip())

        # Descripción
        desc = clean_surrogates(raw.get("description") or "").strip()
        lang = "en"
        desc_fields = {}
        if desc:
            lang = detect_language(desc)
            desc_fields[f"descripcion_{lang}"] = desc

        # Precios. La API pública NO devuelve price (siempre null en 150/150 spots
        # probados) - los precios solo se cargan en el checkout privado. Por eso
        # gratuito se deja en None (desconocido) cuando price falta, en lugar de
        # asumir False, que sería informativo INCORRECTO.
        price_raw = raw.get("price")
        precio_aprox = None
        precio_info = None
        gratuito = None
        if price_raw is not None:
            try:
                precio_aprox = float(price_raw)
                precio_info = f"{precio_aprox:.2f} EUR"
                gratuito = (precio_aprox == 0.0)
            except (TypeError, ValueError):
                precio_info = str(price_raw)

        # Plazas
        places = raw.get("places")
        num_plazas = None
        if places is not None:
            try:
                num_plazas = int(places)
            except (TypeError, ValueError):
                pass

        # Atributos iniciales
        wifi = None
        wc_publico = None
        ducha = None
        agua_potable = None
        electricidad = None
        vaciado_grises = None
        vaciado_negras = None
        perros = infer_dogs(desc)

        # Parsear facilidades si vinieran pobladas
        for fac in raw.get("facilities") or []:
            title = (fac.get("title") or "").lower()
            available = fac.get("available")
            if available is False or available == 0:
                continue
            
            if "wifi" in title or "internet" in title:
                wifi = True
            if "toilet" in title or "wc" in title:
                wc_publico = True
            if "shower" in title:
                ducha = True
            if "water" in title:
                agua_potable = True
            if "electricity" in title or "power" in title:
                electricidad = True
            if "grey" in title or "gray" in title:
                vaciado_grises = True
            if "chemical" in title or "black" in title:
                vaciado_negras = True
            if "dog" in title or "pet" in title:
                perros = True

        # Rating. La API tiene 2 campos: `rating` (siempre null en producción)
        # y `campsite_campy_rating` (poblado 150/150 spots). Usamos el segundo.
        rating_promedio = None
        ccr = raw.get("campsite_campy_rating")
        if ccr is not None:
            try:
                v = float(ccr)
                if v > 0:
                    rating_promedio = v
            except (TypeError, ValueError):
                pass
        # Fallback al rating user (por si en futuro se llena)
        if rating_promedio is None:
            r = raw.get("rating")
            if r is not None:
                try:
                    v = float(r)
                    if v > 0:
                        rating_promedio = v
                except (TypeError, ValueError):
                    pass

        # Defensivo: name puede ser explícito null
        nombre = (raw.get("title") or "Campy Spot").strip()[:200]

        # Region: preferimos city, fallback a address
        city = (raw.get("city") or "").strip() or None
        address = (raw.get("address") or "").strip() or None
        region = city or address

        res = {
            "source_id": uid,
            "nombre": nombre,
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "precio_aprox": precio_aprox,
            "precio_info": precio_info,
            "rating_promedio": rating_promedio,
            "num_plazas": num_plazas,
            "region": region,
            # web: no usamos URL específica por spot - Campy no expone permalink
            # público. El nombre + región basta para identificar la fuente.
            "fotos_urls": fotos,
            "wifi": wifi,
            "wc_publico": wc_publico,
            "ducha": ducha,
            "agua_potable": agua_potable,
            "electricidad": electricidad,
            "vaciado_grises": vaciado_grises,
            "vaciado_negras": vaciado_negras,
            "perros": perros,
        }
        res.update(desc_fields)
        return merge_extra(res, extract_campy(raw))

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        """Segunda fase: por cada spot campy llama LocationFull(uid) y aprovecha
        lo que LocationsWithinRadius NO devuelve:
          - reviews[] (chupadas de Google) → tabla reviews
          - website/email/phone → columnas de contacto
          - facilities ampliadas → columnas de servicios
          - reviewSummary (resumen IA "sam": pros/cons/summary) → servicios_extras
            (metadata del spot, NUNCA como review, para no contaminar el corpus)
        El endpoint GraphQL no requiere auth.
        """
        from db import enriquecer_spot, upsert_review, refresh_review_count

        stats = {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}

        logger.info(f"[{self.name}] Buscando spots con detalles/reviews pendientes...")
        async with pool.acquire() as conn:
            enrich_jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id,
                       sr.review_count, COALESCE(r.cnt, 0) AS db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) AS cnt
                    FROM reviews
                    WHERE source = 'campy'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'campy'
                  AND (
                    (sr.normalized_data->>'details_fetched') IS NULL
                    OR (sr.review_count > 0 AND COALESCE(r.cnt, 0) < sr.review_count)
                  )
                ORDER BY COALESCE(sr.review_count, 0) DESC;
            """)

        logger.info(f"[{self.name}] Encontrados {len(enrich_jobs)} spots pendientes.")
        if not enrich_jobs:
            return stats

        job_queue = asyncio.Queue()
        for r in enrich_jobs:
            await job_queue.put(dict(r))

        progress_state = [0, len(enrich_jobs)]

        async def enrich_worker(client):
            while not job_queue.empty():
                try:
                    job = await job_queue.get()
                except asyncio.CancelledError:
                    break

                spot_id = job["spot_id"]
                uid = job["source_id"]
                try:
                    await asyncio.sleep(self.rate_limit)
                    q = {
                        "operationName": "LocationFull",
                        "variables": {"uid": uid, "language": "en"},
                        "query": LOCATION_FULL_QUERY,
                    }
                    resp = await client.post(GRAPHQL_URL, json=q, timeout=30)
                    resp.raise_for_status()
                    loc = (resp.json().get("data") or {}).get("location") or {}
                    if not loc:
                        stats["errores"] += 1
                        job_queue.task_done()
                        progress_state[0] += 1
                        continue

                    # Detalle a enriquecer en spots
                    detail_norm: dict = {}
                    web = (loc.get("website") or "").strip()
                    if web:
                        detail_norm["web"] = web
                    email = (loc.get("email") or "").strip()
                    if email:
                        detail_norm["email"] = email
                    phone = (loc.get("phone") or "").strip()
                    if phone:
                        detail_norm["telefono"] = phone
                    detail_norm.update(_map_facilities(loc.get("facilities")))

                    # reviewSummary (bot "sam") → servicios_extras, no como review
                    rs = loc.get("reviewSummary")
                    if isinstance(rs, dict) and (rs.get("summary") or rs.get("pros") or rs.get("cons")):
                        detail_norm["servicios_extras"] = {
                            "campy_review_summary": {
                                "summary": clean_surrogates(rs.get("summary") or "") or None,
                                "pros": [clean_surrogates(str(p)) for p in (rs.get("pros") or [])],
                                "cons": [clean_surrogates(str(c)) for c in (rs.get("cons") or [])],
                            }
                        }

                    reviews_list = loc.get("reviews") or []
                    reviews_count = loc.get("reviewsCount")

                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            if detail_norm:
                                await enriquecer_spot(conn, spot_id, detail_norm, self.name)

                            for rev in reviews_list:
                                rid = rev.get("id")
                                if not rid:
                                    continue
                                comment = clean_surrogates(rev.get("comment") or "").strip()
                                if not comment:
                                    continue
                                fecha = None
                                ts = rev.get("updatedAt")
                                if ts:
                                    try:
                                        fecha = datetime.fromtimestamp(
                                            int(ts) / 1000, tz=timezone.utc
                                        ).date()
                                    except (TypeError, ValueError, OSError):
                                        pass
                                tr = rev.get("translation") or {}
                                idioma = (tr.get("sourceLanguage") or "").lower() or None
                                if not idioma:
                                    idioma = detect_language(comment)
                                inserted = await upsert_review(conn, {
                                    "spot_id": spot_id,
                                    "source": self.name,
                                    "source_review_id": str(rid),
                                    "texto": comment,
                                    "texto_original": comment,
                                    "rating": rev.get("rating"),
                                    "autor": clean_surrogates(rev.get("userDisplayName") or "") or None,
                                    "fecha": fecha,
                                    "idioma": idioma,
                                })
                                stats["reviews_nuevas"] += int(bool(inserted))

                            # Marcar details_fetched y guardar reviewsCount esperado
                            mark = {"details_fetched": True}
                            if reviews_count is not None:
                                try:
                                    mark["num_reviews"] = int(reviews_count)
                                except (TypeError, ValueError):
                                    pass
                            await conn.execute("""
                                UPDATE source_records
                                SET normalized_data = COALESCE(normalized_data, '{}'::jsonb) || $1::jsonb,
                                    review_count = GREATEST(COALESCE(review_count, 0), $2::int),
                                    last_seen = NOW()
                                WHERE source = $3 AND source_id = $4
                            """, json.dumps(mark), int(reviews_count or 0), self.name, uid)

                            await refresh_review_count(conn, self.name, spot_id)
                            stats["actualizados"] += 1
                except Exception as e:
                    logger.error(f"[{self.name}] Error enriqueciendo spot {uid}: {e}")
                    stats["errores"] += 1
                finally:
                    progress_state[0] += 1
                    if progress_state[0] % 20 == 0:
                        logger.info(
                            f"[{self.name}] Progreso: {progress_state[0]}/{progress_state[1]} "
                            f"spots | reviews={stats['reviews_nuevas']} errores={stats['errores']}"
                        )
                        if job_id:
                            try:
                                await self.update_job_progress(
                                    pool, job_id, progress_state[0], progress_state[1], stats
                                )
                            except Exception:
                                pass
                    job_queue.task_done()

        num_workers = min(config.max_workers or 3, 5)
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            workers = [asyncio.create_task(enrich_worker(client)) for _ in range(num_workers)]
            await job_queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        logger.info(
            f"[{self.name}] Reviews terminado: {stats['actualizados']} spots enriquecidos, "
            f"{stats['reviews_nuevas']} reviews nuevas, {stats['errores']} errores."
        )
        return stats
