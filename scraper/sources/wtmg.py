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

        return {
            "source_id": doc_id,
            "nombre": f"Jardín WTMG - {doc_id[:6]}", # No tienen nombre público, usamos ID
            "lat": lat,
            "lon": lon,
            "tipo": "naturaleza",
            "gratuito": True,  # WTMG es una plataforma de jardines gratuitos
            "agua_potable": facs.get("drinkableWater") or facs.get("water") or False,
            "wc_publico": facs.get("toilet") or False,
            "electricidad": facs.get("electricity") or False,
            "ducha": facs.get("shower") or False,
            "vaciado_negras": False, # En jardines no suele haber
            "vaciado_grises": False,
            "fotos_urls": fotos,
            "web": f"https://welcometomygarden.org/es/explore",
            "owner_description": description # Para guardarlo como primera review
        }

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
                                        "idioma": "es",
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
