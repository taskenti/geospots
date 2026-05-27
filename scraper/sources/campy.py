"""Campy — scraper para la fuente Campy app (GraphQL API)."""

import asyncio
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
