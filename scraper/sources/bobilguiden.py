"""Bobilguiden — Bulk JSON scraper para Noruega/Escandinavia."""

import asyncio
from datetime import datetime, timezone
import hashlib
from loguru import logger
import httpx

from sources.base import AbstractSource

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

class BobilguidenSource(AbstractSource):
    name = "bobilguiden"
    rate_limit = 1.0
    dedup_radius_m = 60.0

    # Mapeo countryId del top-level countries[] -> ISO2 lowercase.
    # IDs observados: 1=Norway, 2=Sweden. Resto se infieren defensivos.
    # Si llega un countryId desconocido, se usa "no" como fallback (la fuente
    # es noruega, ~95% spots).
    COUNTRY_ID_MAP = {1: "no", 2: "se", 3: "fi", 4: "dk"}

    # facilityIds del top-level facilities[] (verificados con la API).
    # Solo mapeamos los que corresponden a columnas de GeoSpots.
    FACILITY_IDS = {
        "wc_publico":      3,   # Toilet (verificado)
        "ducha":           2,   # Shower (verificado)
        "electricidad":    5,   # Electric power (verificado)
        "agua_potable":    6,   # Water (verificado)
        "vaciado_grises":  10,  # Waste water (verificado)
        "vaciado_negras":  11,  # Chemical toilet drain (asumido por convención BG)
        "wifi":            15,  # WiFi (asumido por convención BG)
    }

    # Tasa NOK -> EUR aproximada para precio_aprox. La API devuelve siempre NOK
    # pero el resto de la DB usa EUR. Tasa baja (~10 NOK = 1 EUR) razonable para
    # ranking/filtrado por precio. El texto exacto en NOK se conserva en precio_info.
    NOK_TO_EUR = 0.085

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Bobilguiden utiliza descarga bulk JSON directa")

    def normalize(self, raw: dict) -> dict | None:
        try:
            loc = raw.get("location", {}) or {}
            coords = loc.get("coordinates", {}) or {}
            lat = float(coords["latitude"])
            lon = float(coords["longitude"])
            source_id = str(raw["id"])
        except (KeyError, TypeError, ValueError):
            return None

        # Mapeo de tipos. 4 tipos observados en producción (1936 spots):
        # CAMPING_SITE (687), FREE_CAMPING (645), MOTORHOME_PARKING (597), DISPOSAL_STATION (7)
        t_raw = (raw.get("type") or "").upper().strip()
        tipo = "naturaleza"
        if t_raw == "CAMPING_SITE":
            tipo = "camping"
        elif t_raw == "MOTORHOME_PARKING":
            tipo = "area_ac"
        elif t_raw == "FREE_CAMPING":
            tipo = "wild"
        elif t_raw == "DISPOSAL_STATION":
            tipo = "area_ac"  # solo servicios sin pernocta

        # Plazas
        places = raw.get("vehicleCount")
        num_plazas = None
        if places is not None:
            try:
                num_plazas = int(places)
            except (TypeError, ValueError):
                pass

        # Atributos de facilidades (IDs mapeados en FACILITY_IDS constante de la clase)
        fac_ids = raw.get("facilityIds") or []
        F = self.FACILITY_IDS
        wc_publico = F["wc_publico"] in fac_ids
        ducha = F["ducha"] in fac_ids
        electricidad = F["electricidad"] in fac_ids
        agua_potable = F["agua_potable"] in fac_ids
        vaciado_grises = F["vaciado_grises"] in fac_ids
        vaciado_negras = F["vaciado_negras"] in fac_ids
        wifi = F["wifi"] in fac_ids
        
        # Fotos
        fotos_urls = raw.get("imageUrls") or []
        fotos_urls = [f.strip() for f in fotos_urls if isinstance(f, str) and f.strip().startswith("http")]

        # Contacto
        contact = raw.get("contact", {}) or {}
        web = (contact.get("website") or "").strip() or None
        if web and not web.startswith("http"):
            web = f"http://{web}"

        email = (contact.get("email") or "").strip() or None
        
        # Teléfono
        phones = contact.get("phones") or []
        telefono = None
        if phones:
            p = phones[0]
            dial = p.get("dialingCode") or ""
            num = p.get("number") or ""
            telefono = f"{dial} {num}".strip() or None

        # Descripción
        desc = clean_surrogates(raw.get("description") or "").strip()
        lang = "en"
        desc_fields = {}
        if desc:
            lang = detect_language(desc)
            desc_fields[f"descripcion_{lang}"] = desc

        # Precios. API devuelve siempre en NOK. precio_aprox se guarda en EUR
        # (tasa fija aprox) para que comparta escala con el resto de fuentes.
        # precio_info conserva el texto exacto en NOK (más útil al usuario nórdico).
        # gratuito = None cuando minPrice es null (no asumir False sin saber).
        min_price = raw.get("minPrice")
        precio_aprox = None
        precio_info = None
        gratuito = None
        if min_price is not None:
            try:
                nok = float(min_price)
                precio_aprox = round(nok * self.NOK_TO_EUR, 2)
                precio_info = f"{nok:.0f} NOK"
                gratuito = (nok == 0.0)
            except (TypeError, ValueError):
                pass

        # pricingDetails es texto libre (e.g. "Free in winter, 250 NOK in summer")
        # Se concatena al precio_info en lugar de sobrescribirlo
        pricing_details = raw.get("pricingDetails")
        if pricing_details:
            pd = clean_surrogates(pricing_details).strip()
            if pd:
                precio_info = f"{precio_info} ({pd})" if precio_info else pd

        # Región / Dirección
        addr = loc.get("address", {}) or {}
        region = addr.get("county") or addr.get("city") or None

        # country_iso: leer del countryId del propio spot (necesita el mapping
        # countries_id_to_iso inyectado por run()). Fallback a "no" porque la
        # fuente cubre ~95% Noruega.
        country_iso = "no"
        country_id = addr.get("countryId")
        if country_id is not None:
            country_iso = self.COUNTRY_ID_MAP.get(country_id, "no")

        # Rating: API en escala 0-5. GeoSpots usa 0-10 (convención multi-fuente),
        # multiplicamos por 2.
        rating = raw.get("rating")
        master_rating = None
        if rating is not None:
            try:
                v = float(rating)
                if v > 0:
                    master_rating = round(v * 2, 2)
            except (TypeError, ValueError):
                pass

        # Número de reviews
        num_reviews = None
        nr = raw.get("numberOfRatings")
        if nr is not None:
            try:
                num_reviews = int(nr)
            except (TypeError, ValueError):
                pass

        norm = {
            "source_id": source_id,
            "nombre": (raw.get("name") or "Bobilguiden Spot").strip()[:200],
            "lat": lat,
            "lon": lon,
            "country_iso": country_iso,
            "region": region,
            "rating_promedio": master_rating,
            "num_reviews": num_reviews or 0,
            "tipo": tipo,
            "gratuito": gratuito,
            "precio_aprox": precio_aprox,
            "precio_info": precio_info,
            "num_plazas": num_plazas,
            "wc_publico": wc_publico,
            "ducha": ducha,
            "electricidad": electricidad,
            "agua_potable": agua_potable,
            "vaciado_grises": vaciado_grises,
            "vaciado_negras": vaciado_negras,
            "wifi": wifi,
            "web": web,
            "email": email,
            "telefono": telefono,
            "fotos_urls": fotos_urls,
        }
        norm.update(desc_fields)
        return norm

    async def run(self, pool, config, log_id: int) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, upsert_review,
            finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {},
        }

        url = "https://api.bobilguiden.no/places/mobile"
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "accept": "application/json"
        }

        logger.info("[bobilguiden] Descargando base de datos completa...")
        try:
            async with httpx.AsyncClient(headers=headers, timeout=40, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
                payload = r.json()
                places = payload.get("places", []) or []
            logger.info(f"[bobilguiden] Descargados {len(places)} lugares.")
        except Exception as e:
            logger.error(f"[bobilguiden] Error descargando base de datos: {e}")
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        processed = 0
        for raw_item in places:
            norm = self.normalize(raw_item)
            if not norm:
                continue
            if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                continue

            sid = str(norm["source_id"])
            comments = raw_item.get("comments") or []

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
                            conn, spot_id, self.name, sid,
                            raw_item, norm
                        )

                        # Ingestión de comentarios como reviews (vía upsert_review
                        # para consistencia y para que el contador refleje SOLO
                        # los INSERTs reales — el SQL crudo + ON CONFLICT incrementaba
                        # el contador siempre, inflando reviews_nuevas en re-runs)
                        for c in comments:
                            c_id = c.get("id")
                            c_text = clean_surrogates(c.get("content") or "").strip()
                            if not c_id or not c_text:
                                continue

                            c_date = None
                            c_date_str = c.get("createdDate")
                            if c_date_str:
                                try:
                                    c_date = datetime.strptime(c_date_str[:10], "%Y-%m-%d").date()
                                except Exception:
                                    pass

                            author = clean_surrogates(c.get("username") or "Camper")
                            lang = detect_language(c_text)

                            inserted = await upsert_review(conn, {
                                "spot_id": spot_id,
                                "source": self.name,
                                "source_review_id": f"bg_{c_id}",
                                "texto": c_text,
                                "rating": None,
                                "autor": author,
                                "fecha": c_date,
                                "idioma": lang,
                            })
                            if inserted:
                                stats["reviews_nuevas"] += 1
            except Exception as e:
                logger.error(f"[bobilguiden] Error guardando spot {sid}: {e}")
                stats["errores"] += 1

            processed += 1
            if processed % 200 == 0:
                logger.info(f"[bobilguiden] Procesados {processed}/{len(places)} spots...")

        # Finalizar ejecución
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[bobilguiden] Completado en {dur:.0f}s | {stats}")
        return stats
