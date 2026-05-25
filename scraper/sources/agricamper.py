"""Agricamper Italia — Bulk API JSON scraper."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource

class AgricamperSource(AbstractSource):
    name = "agricamper"
    rate_limit = 1.0
    dedup_radius_m = 100.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Utiliza descarga bulk de fiches directa")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["adresse_latitude"])
            lon = float(raw["adresse_longitude"])
        except (ValueError, TypeError, KeyError):
            return None

        source_id = str(raw.get("id"))
        if not source_id:
            return None

        # Typology: Agricamping -> camping, rest -> parking_privado
        typologies = raw.get("fiche_typologie_label", []) or []
        tipo = "parking_privado"
        if "Agricamping" in typologies:
            tipo = "camping"

        # Services
        services = raw.get("fiche_service_label", []) or []
        agua_potable = "Water" in services
        electricidad = "Electrical connection" in services
        ducha = "Showers" in services
        wifi = "Wi-Fi" in services
        wc_publico = "WC" in services
        vaciado_negras = "WC drain" in services
        vaciado_grises = "Water drain" in services
        perros = any(d in services for d in ["Dogs accepted (kept on a leash)", "Dogs accepted"])
        iluminacion = "Illuminated" in services

        # Capacity
        num_plazas = None
        n_place = raw.get("nombre_place")
        if n_place is not None:
            try:
                num_plazas = int(n_place)
            except (ValueError, TypeError):
                pass

        # Big rigs
        acceso_grandes = None
        if raw.get("accepte_gros_camping_car") == 1:
            acceso_grandes = True

        # Contact
        web = (raw.get("url_www") or "").strip() or None
        if not web:
            web = (raw.get("url_facebook") or "").strip() or None

        tel_fixe = (raw.get("telephone_fixe") or "").strip()
        tel_mob = (raw.get("telephone_portable") or "").strip()
        telefono = None
        if tel_fixe and tel_mob:
            telefono = f"{tel_fixe} / {tel_mob}"
        elif tel_fixe:
            telefono = tel_fixe
        elif tel_mob:
            telefono = tel_mob

        # Photos
        photos_dict = raw.get("fiche_photo", {}) or {}
        fotos_urls = []
        for ptype in ["main_photo", "external_photo", "interior_photo", "product_photo", "parking_photo", "owner_photo"]:
            pinfo = photos_dict.get(ptype)
            if isinstance(pinfo, dict):
                opt_url = pinfo.get("optimisee")
                if opt_url and isinstance(opt_url, str) and opt_url.startswith("http"):
                    fotos_urls.append(opt_url)

        # Translations / Descriptions
        tr = raw.get("fiche_traduction", {}) or {}
        descripcion_it = None
        descripcion_en = None
        descripcion_fr = None
        descripcion_de = None
        descripcion_nl = None

        if "it_IT" in tr and isinstance(tr["it_IT"], dict):
            descripcion_it = (tr["it_IT"].get("description") or "").strip() or None
        if "en_EN" in tr and isinstance(tr["en_EN"], dict):
            descripcion_en = (tr["en_EN"].get("description") or "").strip() or None
        if "fr_FR" in tr and isinstance(tr["fr_FR"], dict):
            descripcion_fr = (tr["fr_FR"].get("description") or "").strip() or None
        if "de_DE" in tr and isinstance(tr["de_DE"], dict):
            descripcion_de = (tr["de_DE"].get("description") or "").strip() or None
        if "nl_NL" in tr and isinstance(tr["nl_NL"], dict):
            descripcion_nl = (tr["nl_NL"].get("description") or "").strip() or None

        # Build name: preferably business/farm name, fallback to contact person.
        # Sin prefijo "Agricamper -" porque la fuente ya queda registrada en
        # spots.fuentes[]; el prefijo solo añade ruido a la búsqueda y al mapa.
        nombre = (raw.get("nom_societe") or "").strip()
        if not nombre:
            prenom = (raw.get("prenom") or "").strip()
            nom = (raw.get("nom") or "").strip()
            full = f"{prenom} {nom}".strip()
            nombre = full or f"Agricamper {source_id}"

        # Limpiar región: "Benevento (BN)" → "Benevento"
        province_raw = (raw.get("adresse_province") or "").strip()
        region = province_raw.split("(")[0].strip() if province_raw else None

        norm = {
            "source_id": source_id,
            "nombre": nombre,
            "lat": lat,
            "lon": lon,
            "country_iso": "it",
            "region": region,
            "tipo": tipo,
            "gratuito": False,
            "precio_info": "Agricamper membership required (annual subscription)",
            # precio_aprox queda None: requiere membresía anual fija, no es un
            # precio por noche real. Poner 0.0 era contradictorio con "membership required".
            "agua_potable": agua_potable,
            "vaciado_negras": vaciado_negras,
            "vaciado_grises": vaciado_grises,
            "electricidad": electricidad,
            "ducha": ducha,
            "wifi": wifi,
            "wc_publico": wc_publico,
            "num_plazas": num_plazas,
            "acceso_grandes": acceso_grandes,
            "perros": perros,
            "iluminacion": iluminacion,
            "web": web,
            "telefono": telefono,
            "fotos_urls": fotos_urls,
            "descripcion_it": descripcion_it,
            "descripcion_en": descripcion_en,
            "descripcion_fr": descripcion_fr,
            "descripcion_de": descripcion_de,
            "descripcion_nl": descripcion_nl,
        }
        return norm

    async def run(self, pool, config, log_id: int) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {},
        }

        url = "https://www.agricamper.com/wp-json/interactive-map/v1/fiches"
        headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "accept": "*/*",
            "accept-language": "es-ES,es;q=0.9",
            "referer": "https://www.agricamper.com/map/"
        }

        logger.info("[agricamper] Descargando listado completo de fiches...")
        try:
            async with httpx.AsyncClient(headers=headers, timeout=40, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
                fiches = r.json()
            logger.info(f"[agricamper] Encontrados {len(fiches)} hosts en el listado.")
        except Exception as e:
            logger.error(f"[agricamper] Error descargando fiches: {e}")
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        processed = 0
        for raw_item in fiches:
            norm = self.normalize(raw_item)
            if not norm:
                continue
            if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                continue

            sid = str(norm["source_id"])

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
            except Exception as e:
                logger.error(f"[agricamper] Error guardando spot {sid}: {e}")
                stats["errores"] += 1

            processed += 1
            if processed % 100 == 0:
                logger.info(f"[agricamper] Procesados {processed}/{len(fiches)} spots...")

        # Finalizar ejecución
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[agricamper] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config) -> dict:
        # Agricamper no tiene comentarios públicos
        return {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}
