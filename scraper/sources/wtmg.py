"""WelcomeToMyGarden — scraper desde Google Cloud Firestore."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

PROJECT_ID = "wtmg-production"
API_KEY = "AIzaSyDO-2F-GFTblgS6o1bbhhRledAJSoAfwzw"
BASE_URL = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents:runQuery?key={API_KEY}"

HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://welcometomygarden.org",
    "referer": "https://welcometomygarden.org/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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

def infer_dogs(text: str) -> bool | None:
    if not text:
        return None
    text = text.lower()
    forbidden_keywords = [
        "no perros", "sin perros", "perros no", "perros prohibidos", "no mascotas", "mascotas prohibidas", "no se admiten perros", "no se aceptan perros",
        "no dogs", "no pets", "dogs not allowed", "pets not allowed", "no animals", "without dogs", "without pets",
        "pas de chien", "chien interdit", "sans chien", "pas d'animaux", "animaux interdits", "sans animaux",
        "geen honden", "geen huisdieren", "honden niet toegestaan", "huisdieren niet toegestaan",
        "keine hunde", "keine haustiere", "hunde nicht erlaubt", "hunde verboten"
    ]
    allowed_keywords = [
        "perros bienvenidos", "se aceptan perros", "se admiten perros", "mascotas bienvenidas", "se aceptan mascotas", "se admiten mascotas", "perros ok",
        "dogs welcome", "dogs allowed", "pets welcome", "pets allowed", "dogs ok", "dog friendly", "pet friendly",
        "chien bienvenu", "chiens bienvenus", "chien accepté", "chiens acceptés", "animaux acceptés", "animaux bienvenus",
        "honden welkom", "honden toegestaan", "huisdieren welkom", "huisdieren toegestaan",
        "hunde willkommen", "hunde erlaubt", "haustiere willkommen", "haustiere erlaubt"
    ]
    for kw in forbidden_keywords:
        if kw in text:
            return False
    for kw in allowed_keywords:
        if kw in text:
            return True
    return None

def infer_large_vehicles(text: str) -> bool | None:
    if not text:
        return None
    text = text.lower()
    tent_only_keywords = [
        "tent only", "tents only", "only tents", "only for tents", "no campers", "no motorhomes", "no caravans", "no rvs", "no vans", "no cars", "no vehicles",
        "solo tiendas", "sólo tiendas", "solo tienda", "sólo tienda", "no furgonetas", "no autocaravanas", "no caravanas", "no vehículos",
        "uniquement tentes", "tentes uniquement", "pas de camping-car", "pas de caravane", "pas de véhicule",
        "alleen tenten", "geen campers", "geen caravans", "geen voertuigen",
        "nur zelte", "nur für zelte", "keine wohnmobile", "keine wohnwagen", "keine fahrzeuge"
    ]
    vehicle_allowed_keywords = [
        "camper allowed", "campers allowed", "vans allowed", "van allowed", "motorhome allowed", "motorhomes allowed", "rv allowed", "rvs allowed", "vehicles allowed", "vehicle allowed", "camper van", "campervan", "motorhome ok", "camper ok",
        "se aceptan campers", "se admiten campers", "furgonetas bienvenidas", "se aceptan furgonetas", "autocaravanas bienvenidas", "se aceptan autocaravanas",
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

class WelcomeToMyGardenSource(AbstractSource):
    name = "wtmg"
    rate_limit = 1.0
    dedup_radius_m = 60.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("WTMG usa paginación directa de Firestore, no grid")

    def _extract_firestore_value(self, field_data: dict):
        """Extrae el valor real de un campo de Firestore."""
        if not field_data:
            return None
        if "stringValue" in field_data:
            return field_data["stringValue"]
        if "doubleValue" in field_data:
            return field_data["doubleValue"]
        if "integerValue" in field_data:
            return int(field_data["integerValue"])
        if "booleanValue" in field_data:
            return field_data["booleanValue"]
        if "mapValue" in field_data:
            return {k: self._extract_firestore_value(v) for k, v in field_data["mapValue"].get("fields", {}).items()}
        return None

    def normalize(self, raw: dict) -> dict | None:
        doc = raw.get("document")
        if not doc:
            return None

        # El doc_name es "projects/.../campsites/ID_REAL"
        doc_name = doc.get("name", "")
        doc_id = doc_name.split("/")[-1] if "/" in doc_name else doc_name
        
        fields = doc.get("fields", {})
        
        # Extraer coordenadas
        location = self._extract_firestore_value(fields.get("location"))
        if not location or "latitude" not in location or "longitude" not in location:
            return None
            
        lat = location["latitude"]
        lon = location["longitude"]
        
        # Ignorar si no está listado (aunque ya filtramos en la query por si acaso)
        listed = self._extract_firestore_value(fields.get("listed"))
        if listed is False:
            return None
            
        # Extraer facilities
        facs = self._extract_firestore_value(fields.get("facilities")) or {}
        
        # Fotos (construimos URL asumiendo bucket por defecto de firebase)
        fotos = []
        photo_name = self._extract_firestore_value(fields.get("photo"))
        if photo_name and isinstance(photo_name, str) and photo_name.strip():
            # A veces viene la URL entera
            if photo_name.startswith("http"):
                fotos.append(photo_name)
            else:
                fotos.append(f"https://firebasestorage.googleapis.com/v0/b/{PROJECT_ID}.appspot.com/o/campsites%2F{doc_id}%2F{photo_name}?alt=media")

        description = self._extract_firestore_value(fields.get("description"))

        # Determinar idioma y asignar a la columna adecuada
        lang = "en"
        desc_fields = {}
        if description and isinstance(description, str) and description.strip():
            lang = detect_language(description)
            desc_fields[f"descripcion_{lang}"] = description

        # Inferir perros y vehículos grandes
        perros = infer_dogs(description)
        acceso_grandes = infer_large_vehicles(description)

        # Capacidad de plazas
        num_plazas = facs.get("capacity")
        if num_plazas is not None:
            try:
                num_plazas = int(num_plazas)
            except (ValueError, TypeError):
                num_plazas = None

        res = {
            "source_id": doc_id,
            "nombre": f"Jardín WTMG - {doc_id[:6]}", # No tienen nombre público, usamos ID
            "lat": lat,
            "lon": lon,
            "tipo": "naturaleza",
            "gratuito": True,  # WTMG es una plataforma de jardines gratuitos
            "agua_potable": facs.get("drinkableWater") or False,
            "wc_publico": facs.get("toilet") or False,
            "electricidad": facs.get("electricity") or False,
            "ducha": facs.get("shower") or False,
            "vaciado_negras": False, # En jardines no suele haber
            "vaciado_grises": False,
            "fotos_urls": fotos,
            "web": f"https://welcometomygarden.org/es/explore",
            "owner_description": description, # Para guardarlo como primera review
            "owner_description_lang": lang,   # Guardar idioma detectado
            "perros": perros,
            "acceso_grandes": acceso_grandes,
            "num_plazas": num_plazas,
        }
        res.update(desc_fields)
        return res

    async def run(self, pool, config, log_id: int) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot, upsert_review,
            upsert_source_record, finish_scraper_log, update_fuente_config,
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {},
        }

        async with httpx.AsyncClient(headers=HEADERS) as client:
            last_doc_name = None
            has_more = True
            page = 1

            while has_more:
                query = {
                    "structuredQuery": {
                        "from": [{"collectionId": "campsites", "allDescendants": False}],
                        "where": {
                            "fieldFilter": {
                                "field": {"fieldPath": "listed"},
                                "op": "EQUAL",
                                "value": {"booleanValue": True}
                            }
                        },
                        "orderBy": [
                            {"direction": "ASCENDING", "field": {"fieldPath": "__name__"}}
                        ],
                        "limit": 1000
                    }
                }

                if last_doc_name:
                    query["structuredQuery"]["startAt"] = {
                        "before": False,
                        "values": [{"referenceValue": last_doc_name}]
                    }

                try:
                    logger.info(f"[WTMG] Obteniendo página {page} de Firestore...")
                    resp = await client.post(BASE_URL, json=query, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[WTMG] Error llamando a Firestore: {e}")
                    stats["errores"] += 1
                    break

                if not data or len(data) == 0:
                    break
                    
                # Si el primer resultado no tiene "document", ya no hay más
                if "document" not in data[0]:
                    break

                logger.info(f"[WTMG] Página {page}: {len(data)} documentos obtenidos.")

                for raw in data:
                    norm = self.normalize(raw)
                    if not norm:
                        continue

                    doc_name = raw.get("document", {}).get("name")
                    if doc_name:
                        last_doc_name = doc_name

                    sid = norm["source_id"]
                    desc = norm.pop("owner_description", None)
                    desc_lang = norm.pop("owner_description_lang", "en")

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

                                await upsert_source_record(
                                    conn, spot_id, self.name, sid, raw, norm
                                )
                                
                                # Insertar la descripción como review del dueño
                                if desc and isinstance(desc, str) and len(desc.strip()) > 5:
                                    await upsert_review(conn, {
                                        "spot_id": spot_id,
                                        "source": self.name,
                                        "source_review_id": f"wtmg_desc_{sid}",
                                        "texto": desc[:2000],
                                        "rating": None,
                                        "fecha": None,
                                        "autor": "Anfitrión del jardín",
                                        "idioma": desc_lang,
                                    })
                                    stats["reviews_nuevas"] += 1

                    except Exception as e:
                        logger.error(f"[WTMG] Error procesando jardín '{sid}': {e}")
                        stats["errores"] += 1

                page += 1
                await asyncio.sleep(self.rate_limit)

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[WTMG] Completado en {dur:.0f}s | {stats}")
        return stats
