"""Campspace — scraper para ubicaciones en la naturaleza."""

import asyncio
from datetime import datetime, timezone
from loguru import logger
import httpx

from sources.base import AbstractSource
from sources._normalize_helpers import extract_campspace, merge_extra

BASE_URL = "https://campspace.com/en/discover/campsites?_format=json"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-requested-with": "XMLHttpRequest"
}

import re

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

def extract_price(price_str: str) -> tuple[float | None, str | None]:
    if not price_str:
        return None, None
    match = re.search(r"(\d+(?:[.,]\d+)?)", price_str)
    if match:
        try:
            val_str = match.group(1).replace(",", ".")
            val = float(val_str)
            currency = "€"
            if "£" in price_str:
                currency = "£"
            elif "$" in price_str:
                currency = "$"
            return val, f"{val:.2f} {currency}"
        except Exception:
            pass
    return None, None

class CampspaceSource(AbstractSource):
    name = "campspace"
    rate_limit = 1.0
    dedup_radius_m = 60.0
    countries = [
        "netherlands", "belgium", "germany", "france", "spain", "portugal", "italy",
        "denmark", "sweden", "norway", "austria", "switzerland", "poland", "czech-republic",
        "united-kingdom", "ireland", "croatia", "slovenia", "hungary", "slovakia",
        "luxembourg", "estonia", "latvia", "lithuania", "finland", "greece", "romania"
    ]

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        raise NotImplementedError("Campspace se extrae por lista directa")

    def normalize(self, raw: dict) -> dict | None:
        try:
            lat = float(raw["lat"])
            lon = float(raw["lng"])
        except (KeyError, TypeError, ValueError):
            return None

        href = raw.get("href", "")
        web_url = href
        if href and not href.startswith("http"):
            web_url = f"https://campspace.com{href}"

        price_str = raw.get("price")
        precio_aprox, precio_info = extract_price(price_str)
        gratuito = False
        if precio_aprox == 0.0:
            gratuito = True

        norm = {
            "source_id": str(raw.get("id")),
            "nombre": raw.get("title", "Campspace Spot").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": "naturaleza",
            "gratuito": gratuito,
            "precio_aprox": precio_aprox,
            "precio_info": precio_info,
            "web": web_url,
            "fotos_urls": []
        }
        return merge_extra(norm, extract_campspace(raw))

    def _parse_detail_html(self, html: str) -> dict | None:
        from bs4 import BeautifulSoup
        import re
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return None

        titulo_el = soup.find("h1") or soup.find("title")
        nombre = titulo_el.get_text().strip() if titulo_el else "Campspace Spot"

        descripcion_el = soup.find(class_="space-about")
        descripcion = ""
        if descripcion_el:
            descripcion = descripcion_el.get_text(separator="\n", strip=True)

        amenities = []
        dialog_amenities = soup.find("dialog", attrs={"data-popup-name": "amenities"})
        if dialog_amenities:
            for p in dialog_amenities.find_all("p", class_="m-0"):
                txt = p.get_text().strip()
                if txt and txt not in amenities:
                    amenities.append(txt)
        else:
            for p in soup.select(".amenities-list p.space-p"):
                txt = p.get_text().strip()
                if txt and txt not in amenities:
                    amenities.append(txt)

        surroundings = []
        dialog_surr = soup.find("dialog", attrs={"data-popup-name": "surroundings"})
        if dialog_surr:
            for p in dialog_surr.find_all("p", class_="m-0"):
                txt = p.get_text().strip()
                if txt and txt not in surroundings:
                    surroundings.append(txt)

        fotos_urls = []
        for a in soup.select('a[data-fancybox^="pitch-slider-popup"]'):
            href = a.get("href")
            if href and href.startswith("http") and href not in fotos_urls:
                fotos_urls.append(href)
        
        if not fotos_urls:
            media_modal = soup.find(id="media_modal")
            if media_modal:
                for s in media_modal.find_all("source"):
                    srcset = s.get("srcset")
                    if srcset and srcset.startswith("http") and srcset not in fotos_urls:
                        fotos_urls.append(srcset)
            for img in soup.find_all("img", src=lambda x: x and "teaser" in x):
                src = img.get("src")
                if src and src.startswith("http") and src not in fotos_urls:
                    fotos_urls.append(src)

        max_people = None
        num_plazas = None
        for dialog in soup.find_all("dialog"):
            p_name = dialog.get("data-popup-name", "")
            if p_name and p_name.startswith("pitch"):
                txt = dialog.get_text()
                m_people = re.search(r"Max\.\s*people\s*(\d+)", txt, re.IGNORECASE)
                if m_people:
                    max_people = int(m_people.group(1))
                m_pitches = re.search(r"Pitches\s*(\d+)", txt, re.IGNORECASE)
                if m_pitches:
                    num_plazas = int(m_pitches.group(1))
        
        host_name = None
        host_el = soup.find(class_=lambda x: x and "host" in x)
        if host_el:
            txt = host_el.get_text(separator="\n", strip=True)
            lines = [l.strip() for l in txt.split("\n") if l.strip()]
            for idx, line in enumerate(lines):
                if "host" in line.lower() and idx + 1 < len(lines):
                    host_name = lines[idx + 1]
                    break

        space_id = None
        review_load_more = soup.find(attrs={"data-app--review--load-more-url-value": True})
        if review_load_more:
            url_val = review_load_more.get("data-app--review--load-more-url-value")
            parts = [p for p in url_val.split("/") if p]
            if parts:
                space_id = parts[-1]

        return {
            "nombre": nombre,
            "descripcion": descripcion,
            "amenities": amenities,
            "surroundings": surroundings,
            "fotos_urls": fotos_urls,
            "max_people": max_people,
            "num_plazas": num_plazas,
            "host_name": host_name,
            "space_id": space_id,
        }

    def _normalize_detail(self, parsed: dict, fallback_web: str = None) -> dict | None:
        if not parsed:
            return None

        amenities_lower = [a.lower() for a in parsed.get("amenities", [])]
        surroundings_lower = [s.lower() for s in parsed.get("surroundings", [])]

        agua_potable = any("water" in a or "tap" in a or "potable" in a for a in amenities_lower)
        vaciado_negras = any("black water" in a or "chemical toilet" in a or "disposal toilet" in a for a in amenities_lower)
        vaciado_grises = any("gray water" in a or "grey water" in a or "waste water" in a for a in amenities_lower)
        electricidad = any("electricity" in a or "power" in a or "charging" in a for a in amenities_lower)
        ducha = any("shower" in a for a in amenities_lower)
        wifi = any("wifi" in a or "internet" in a for a in amenities_lower)
        wc_publico = any("toilet" in a or "restroom" in a for a in amenities_lower)
        perros = any("pets allowed" in a or "dogs allowed" in a or "pet-friendly" in a for a in amenities_lower)

        iluminacion = any("light" in s or "illuminated" in s for s in surroundings_lower)
        seguridad = any("secure" in s or "fenced" in s or "gate" in s for s in surroundings_lower)

        acceso_grandes = infer_large_vehicles(parsed.get("descripcion", ""))

        return {
            "agua_potable": agua_potable,
            "vaciado_negras": vaciado_negras,
            "vaciado_grises": vaciado_grises,
            "electricidad": electricidad,
            "ducha": ducha,
            "wifi": wifi,
            "wc_publico": wc_publico,
            "perros": perros,
            "acceso_grandes": acceso_grandes,
            "iluminacion": iluminacion,
            "seguridad": seguridad,
            "num_plazas": parsed.get("num_plazas"),
            "web": fallback_web,
            "fotos_urls": parsed.get("fotos_urls", []),
            "host_name": parsed.get("host_name"),
            "space_id": parsed.get("space_id"),
            "descripcion_en": parsed.get("descripcion"),
        }

    def _parse_reviews_html(self, html: str) -> list[dict]:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []

        reviews_list = []
        reviews = soup.find_all("section", class_="space-review")
        
        for rev in reviews:
            author_el = rev.find("span", class_="review-headline--span")
            autor = author_el.get_text().strip() if author_el else "Camper"

            svgs = rev.find("h3", class_="review-headline").find_all("svg") if rev.find("h3", class_="review-headline") else []
            rating = 0
            for svg in svgs:
                path = svg.find("path")
                if path and path.get("fill") == "#FDB000":
                    rating += 1

            subline_el = rev.find("p", class_="review-subline")
            fecha_str = subline_el.get_text().strip() if subline_el else ""
            fecha_str = " ".join(fecha_str.split())
            if "·" in fecha_str:
                fecha_str = fecha_str.split("·")[0].strip()

            fecha = None
            if fecha_str:
                try:
                    parts = fecha_str.split()
                    if len(parts) == 2:
                        meses = {
                            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
                            "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
                        }
                        m = meses.get(parts[0].lower())
                        y = int(parts[1])
                        if m and y:
                            from datetime import date
                            fecha = date(y, m, 1)
                except Exception:
                    pass

            text_el = rev.find("div", class_="review-text")
            texto = ""
            if text_el:
                texto = text_el.get_text(separator=" ", strip=True)
                if not texto:
                    body_el = rev.find("div", class_="review-body")
                    if body_el:
                        subline_el = body_el.find("p", class_="review-subline")
                        subline_text = subline_el.get_text().strip() if subline_el else ""
                        full_body_text = body_el.get_text(separator=" ", strip=True)
                        texto = full_body_text.replace(subline_text, "").strip()

            reviews_list.append({
                "autor": autor,
                "rating": rating if rating > 0 else None,
                "fecha": fecha,
                "texto": texto,
            })

        return reviews_list

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

        async with httpx.AsyncClient(headers=HEADERS) as client:
            seen_ids = set()
            
            # Recorrer todos los países y luego el endpoint global
            targets = [(c, f"https://campspace.com/en/discover/campsites/{c}?_format=json") for c in self.countries]
            targets.append(("global", "https://campspace.com/en/discover/campsites?_format=json"))
            
            for label, url in targets:
                try:
                    logger.info(f"[CAMPSPACE] Obteniendo listado para '{label}'...")
                    resp = await client.get(url, timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[CAMPSPACE] Error obteniendo listado para '{label}': {e}")
                    stats["errores"] += 1
                    continue

                if not data or not isinstance(data, list):
                    continue

                logger.info(f"[CAMPSPACE] Procesando {len(data)} spots de '{label}'...")
                for raw in data:
                    sid = str(raw.get("id"))
                    if sid in seen_ids:
                        continue
                        
                    seen_ids.add(sid)
                    
                    norm = self.normalize(raw)
                    if not norm:
                        continue
                    if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                        continue

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
                    except Exception as e:
                        logger.error(f"[CAMPSPACE] Error spot '{norm.get('nombre')}': {e}")
                        stats["errores"] += 1

                await self.update_job_progress(pool, job_id, len(seen_ids), 0, stats)
                await asyncio.sleep(self.rate_limit)

        # 2. Fase 2: Enriquecimiento asíncrono y de reviews
        logger.info(f"[{self.name}] Fase 1 completada. Iniciando Fase 2: Enriquecimiento y Reviews...")
        async with pool.acquire() as conn:
            enrich_jobs = await conn.fetch("""
                SELECT spot_id, source_id, normalized_data->>'web' as web
                FROM source_records
                WHERE source = $1
                  AND (normalized_data->>'details_fetched') IS NULL
            """, self.name)

        logger.info(f"[{self.name}] Encontrados {len(enrich_jobs)} spots que requieren detalles.")

        if enrich_jobs:
            job_queue = asyncio.Queue()
            for r in enrich_jobs:
                await job_queue.put(dict(r))

            async def enrich_worker():
                import json
                headers = HEADERS
                async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
                    while not job_queue.empty():
                        try:
                            job = await job_queue.get()
                        except asyncio.CancelledError:
                            break
                        
                        spot_id = job["spot_id"]
                        sid = job["source_id"]
                        web_url = job["web"]

                        if not web_url:
                            job_queue.task_done()
                            continue

                        try:
                            await asyncio.sleep(self.rate_limit)
                            r_web = await client.get(web_url)
                            if r_web.status_code == 200:
                                html = r_web.text
                                detail_data = self._parse_detail_html(html)
                                if detail_data:
                                    # Normalizar + merge v4c/extras desde amenities/surroundings
                                    detail_norm = self._normalize_detail(detail_data, fallback_web=web_url)
                                    if detail_norm:
                                        detail_norm = merge_extra(detail_norm, extract_campspace(detail_data))
                                        async with pool.acquire() as conn:
                                            async with conn.transaction():
                                                # Enriquecer spot en tabla spots
                                                db_detail = {k: v for k, v in detail_norm.items() if k not in ("host_name", "space_id")}
                                                await enriquecer_spot(conn, spot_id, db_detail, self.name)

                                                # Actualizar el source record para marcar details_fetched
                                                detail_norm["details_fetched"] = True
                                                await conn.execute("""
                                                    UPDATE source_records
                                                    SET normalized_data = normalized_data || $1::jsonb,
                                                        raw_data = raw_data || $2::jsonb,
                                                        last_seen = NOW()
                                                    WHERE source = $3 AND source_id = $4
                                                """, json.dumps(detail_norm), json.dumps(detail_data), self.name, sid)

                                                # Cargar reviews si hay space_id
                                                space_id = detail_norm.get("space_id")
                                                if space_id:
                                                    from db import upsert_review
                                                    import hashlib
                                                    reviews_url = f"https://campspace.com/en/reviews/{space_id}"
                                                    await asyncio.sleep(self.rate_limit)
                                                    r_reviews = await client.get(reviews_url)
                                                    if r_reviews.status_code == 200 and r_reviews.text:
                                                        parsed_reviews = self._parse_reviews_html(r_reviews.text)
                                                        for rev in parsed_reviews:
                                                            texto = clean_surrogates(rev.get("texto", ""))
                                                            autor = clean_surrogates(rev.get("autor", "Camper"))
                                                            fecha = rev.get("fecha")
                                                            rating = rev.get("rating")
                                                            idioma = detect_language(texto)

                                                            # Identificador único determinista
                                                            rev_hash = hashlib.md5(
                                                                (autor + str(fecha) + texto[:50]).encode('utf-8')
                                                            ).hexdigest()
                                                            source_review_id = f"cs_{space_id}_{rev_hash}"

                                                            # upsert_review devuelve True solo si la fila se INSERTÓ
                                                            # (no actualizada), evitando inflar el contador en re-runs
                                                            inserted = await upsert_review(conn, {
                                                                "spot_id": spot_id,
                                                                "source": self.name,
                                                                "source_review_id": source_review_id,
                                                                "texto": texto,
                                                                "rating": rating,
                                                                "autor": autor,
                                                                "fecha": fecha,
                                                                "idioma": idioma,
                                                            })
                                                            if inserted:
                                                                stats["reviews_nuevas"] += 1
                                                        # Sync review_count tras procesar todas las reviews del spot
                                                        from db import refresh_review_count
                                                        await refresh_review_count(conn, self.name, spot_id)

                                                # Contar enriquecimientos en detalle aparte para no
                                                # mezclarlo con "actualizados" de Phase 1 (que mide
                                                # spots ya existentes en DB encontrados por dedup)
                                                stats["detalle"].setdefault("enriquecidos_fase2", 0)
                                                stats["detalle"]["enriquecidos_fase2"] += 1
                            else:
                                logger.warning(f"[{self.name}] Error cargando {web_url}: status={r_web.status_code}")
                                stats["errores"] += 1
                        except Exception as e:
                            logger.error(f"[{self.name}] Error enriqueciendo spot {sid}: {e}")
                            stats["errores"] += 1
                        finally:
                            job_queue.task_done()

            # Iniciar trabajadores concurrentes
            num_workers = min(config.max_workers or 3, 3)
            workers = []
            for _ in range(num_workers):
                workers.append(asyncio.create_task(enrich_worker()))

            await job_queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[CAMPSPACE] Completado en {dur:.0f}s | {stats}")
        return stats
