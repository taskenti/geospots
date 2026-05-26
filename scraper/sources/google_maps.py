"""Google Maps Source — Scraper de enriquecimiento y reviews mediante Playwright.

NOTA OPERATIVA: este scraper requiere Playwright + Chromium (~700MB) y NO se
ejecuta en el container scraper estándar. Está aislado en un servicio docker
propio (`gmaps`) que se arranca on-demand:

    docker-compose --profile gmaps build gmaps
    docker-compose --profile gmaps run --rm gmaps python scheduler.py --google_maps

Limitaciones conocidas:
- Selectores CSS de Google Maps son obfuscated y cambian sin aviso (~3-6 meses).
  Cuando rompen, las reviews extraídas caen a 0 silenciosamente. Auditar
  periódicamente que `stats.reviews_nuevas > 0` en runs reales.
- Google detecta scraping intensivo. rate_limit=5s + LIMIT=50 spots por run
  son conservadores. Captchas pueden aparecer; el scraper no los resuelve.
- IP bans son riesgo real. Usar VPN/proxies rotatorios para volúmenes grandes.
"""

import asyncio
import json
import re
import math
from datetime import datetime, date, timedelta
from loguru import logger
from difflib import SequenceMatcher

# Playwright es opcional — solo necesario si se ejecuta el run() real.
# Permitimos importar el módulo sin Playwright para que el resto del scheduler
# no rompa si alguien intenta `--all` desde un container que no lo tiene.
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    async_playwright = None
    PLAYWRIGHT_AVAILABLE = False

from sources.base import AbstractSource

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula la distancia geodésica entre dos puntos en metros."""
    R = 6371000.0  # Radio de la Tierra en metros
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) *
         math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c

def name_similarity(name1: str, name2: str) -> float:
    """Calcula la similitud de cadenas entre dos nombres en rango [0, 1]."""
    if not name1 or not name2:
        return 0.0
    return SequenceMatcher(None, name1.lower().strip(), name2.lower().strip()).ratio()

class GoogleMapsSource(AbstractSource):
    """Google Maps Source — Diseñado exclusivamente para enriquecer spots existentes."""

    name = "google_maps"
    rate_limit = 5.0  # Retraso prudente para evitar bloqueos
    grid_step = 1.0
    dedup_radius_m = 150.0  # Umbral máximo de reconciliación espacial

    # Métodos requeridos por la interfaz abstracta base
    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        raise NotImplementedError("Google Maps se ejecuta como pipeline de enriquecimiento sobre spots existentes, no por celdas.")

    def normalize(self, raw: dict) -> dict | None:
        """Convierte los metadatos crudos extraídos al esquema GeoSpots."""
        try:
            return {
                "source_id": raw.get("cid"),
                "nombre": raw.get("nombre"),
                "lat": raw.get("lat"),
                "lon": raw.get("lon"),
                "tipo": raw.get("tipo"),
                "rating_promedio": raw.get("rating"),
                "num_reviews": raw.get("num_reviews"),
                "web": raw.get("web"),
                "telefono": raw.get("telefono"),
                "descripcion_es": raw.get("descripcion_es"),
                "fuentes": ["google_maps"]
            }
        except Exception as e:
            logger.error(f"[google_maps] Error normalizando item: {e}")
            return None

    async def run(self, pool, config, log_id: int) -> dict:
        """Pipeline principal: Busca, reconcilia, extrae opiniones y enriquece spots existentes."""
        from db import (
            enriquecer_spot, upsert_source_record, upsert_review,
            finish_scraper_log, update_fuente_config
        )
        from datetime import timezone

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        # Guardia: si Playwright no está disponible, abortar limpiamente.
        # Evita crash silencioso si alguien ejecuta este scraper desde el
        # container scraper normal (que no incluye playwright).
        if not PLAYWRIGHT_AVAILABLE:
            msg = ("Playwright no instalado en este container. Usa el servicio "
                   "gmaps: docker-compose --profile gmaps run --rm gmaps "
                   "python scheduler.py --google_maps")
            logger.error(f"[google_maps] {msg}")
            stats["errores"] = 1
            stats["detalle"]["error"] = msg
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        # 1. Obtener spots candidatos de la base de datos (con coordenadas válidas y sin google_maps registrado)
        async with pool.acquire() as conn:
            candidatos = await conn.fetch("""
                SELECT id, canonical_name, lat, lon, tipo
                FROM spots
                WHERE activo = TRUE 
                  AND lat IS NOT NULL 
                  AND lon IS NOT NULL
                  AND NOT 'google_maps' = ANY(fuentes)
                ORDER BY total_reviews DESC, id
                LIMIT 50;
            """)

        if not candidatos:
            logger.info("[google_maps] No hay spots candidatos para enriquecer.")
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        logger.info(f"[google_maps] Iniciando enriquecimiento para {len(candidatos)} spots...")

        # 2. Inicializar Playwright. async_playwright() gestiona shutdown del proceso
        # node helper automáticamente; el try/finally interno garantiza que browser
        # y context se cierran aunque haya excepciones en medio del loop de spots.
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars",
                    "--window-size=1280,800",
                    # Reducir signals adicionales de automation
                    "--disable-dev-shm-usage",
                ]
            )
            
            # Crear contexto con User-Agent realista
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="es-ES",
                timezone_id="Europe/Madrid",
                viewport={"width": 1280, "height": 800}
            )

            # Inyectar cookie SOCS para evitar que aparezca el diálogo de consentimiento de cookies de Google
            await context.add_cookies([{
                "name": "SOCS",
                "value": "CAESEwgDEgk0ODE3NzkzNDQaAmVzIAEaBgiA_K6dBg",
                "domain": ".google.com",
                "path": "/"
            }, {
                "name": "SOCS",
                "value": "CAESEwgDEgk0ODE3NzkzNDQaAmVzIAEaBgiA_K6dBg",
                "domain": ".google.es",
                "path": "/"
            }])

            page = await context.new_page()

            # Evitar detección básica de webdriver
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            for spot_db in candidatos:
                spot_id = spot_db["id"]
                orig_name = spot_db["canonical_name"]
                orig_lat = float(spot_db["lat"])
                orig_lon = float(spot_db["lon"])
                orig_tipo = spot_db["tipo"]

                logger.info(f"[google_maps] Procesando '{orig_name}' (ID: {spot_id}) en ({orig_lat}, {orig_lon})...")

                try:
                    # Espera prudente entre búsquedas
                    await asyncio.sleep(self.rate_limit)

                    # Búsqueda específica en Google Maps
                    query = f"{orig_name}"
                    search_url = f"https://www.google.com/maps/search/{query}/@{orig_lat},{orig_lon},15z?hl=es"
                    
                    try:
                        await page.goto(search_url, timeout=20000, wait_until="domcontentloaded")
                    except Exception as goto_err:
                        logger.warning(f"[google_maps] Navigation warning (will try to continue): {goto_err}")
                    
                    # Esperar a que la página cargue selectores clave (lista de resultados o ficha directa)
                    try:
                        await page.wait_for_selector('a[href*="/maps/place/"], h1', timeout=10000)
                    except Exception:
                        pass

                    # Si Google Maps muestra una lista de resultados en lugar del detalle directo
                    # Detectar si hay múltiples resultados buscando enlaces que contienen "/maps/place/"
                    list_items = await page.query_selector_all('a[href*="/maps/place/"]')
                    
                    target_matched = False
                    if len(list_items) > 0:
                        logger.info(f"[google_maps] Múltiples resultados ({len(list_items)}) encontrados para '{orig_name}'. Reconciliando...")
                        
                        best_match = None
                        best_sim = 0.0
                        best_dist = float("inf")

                        for item in list_items:
                            href = await item.get_attribute("href")
                            # Extraer nombre y coordenadas del href o del elemento
                            aria_label = await item.get_attribute("aria-label")
                            name_text = aria_label or await item.text_content()
                            
                            # Regex para coordenadas en href de Google Maps
                            coord_match = re.search(r"/@(-?\d+\.\d+),(-?\d+\.\d+)", href)
                            if coord_match:
                                m_lat = float(coord_match.group(1))
                                m_lon = float(coord_match.group(2))
                                
                                dist = haversine_distance(orig_lat, orig_lon, m_lat, m_lon)
                                sim = name_similarity(orig_name, name_text)
                                
                                if dist <= self.dedup_radius_m and sim >= 0.75:
                                    if sim > best_sim or (sim == best_sim and dist < best_dist):
                                        best_sim = sim
                                        best_dist = dist
                                        best_match = item

                        if best_match:
                            logger.info(f"[google_maps] Emparejado con similitud {best_sim:.2f} y distancia {best_dist:.1f}m. Accediendo...")
                            await best_match.click()
                            try:
                                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                                await page.wait_for_selector("h1", timeout=10000)
                            except Exception:
                                pass
                            target_matched = True
                        else:
                            logger.warning(f"[google_maps] Ninguno de los resultados coincidió con los límites geodésicos/textuales.")
                    else:
                        # Entrada directa o ficha individual abierta directamente
                        # Validar coordenadas actuales de la barra de direcciones o DOM
                        curr_url = page.url
                        coord_match = re.search(r"/@(-?\d+\.\d+),(-?\d+\.\d+)", curr_url)
                        if coord_match:
                            m_lat = float(coord_match.group(1))
                            m_lon = float(coord_match.group(2))
                            dist = haversine_distance(orig_lat, orig_lon, m_lat, m_lon)
                            
                            # Extraer nombre del h1
                            h1_el = await page.query_selector("h1")
                            h1_text = await h1_el.text_content() if h1_el else ""
                            sim = name_similarity(orig_name, h1_text)

                            if dist <= self.dedup_radius_m and sim >= 0.75:
                                target_matched = True
                                logger.info(f"[google_maps] Coincidencia directa exitosa (dist: {dist:.1f}m, sim: {sim:.2f}).")
                            else:
                                logger.warning(f"[google_maps] Descartado: Fuera de rango de reconciliación (dist: {dist:.1f}m, sim: {sim:.2f}).")

                    if not target_matched:
                        stats["errores"] += 1
                        continue

                    # 3. Extraer detalles de la ficha
                    # CID hex de la URL `0x...:0x...` (formato canónico de Google Maps).
                    # Fallback al place_id si Google muestra solo /place/{slug}/data=!4m...
                    # NUNCA usar f"gmaps_{spot_id}" como fallback porque colisionaría con
                    # otros spots si el match fuese impreciso (mismo source_id = misma URL).
                    cid = None
                    cid_match = re.search(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+", page.url)
                    if cid_match:
                        cid = cid_match.group(0)
                    else:
                        # Intentar extraer place_id del data= param o del FTID
                        ftid_match = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", page.url)
                        if ftid_match:
                            cid = ftid_match.group(1)
                    if not cid:
                        # Sin CID identificable, usar URL completa SHA1 — único pero opaco
                        import hashlib
                        cid = f"gmaps_url_{hashlib.sha1(page.url.encode()).hexdigest()[:16]}"

                    # Nombre canonical
                    h1_el = await page.query_selector("h1")
                    canonical_name = (await h1_el.text_content() if h1_el else orig_name).strip()

                    # Valoración media (ej: "4,4")
                    rating_el = await page.query_selector("div.F7nice > span > span")
                    rating_val = None
                    if rating_el:
                        try:
                            rating_txt = (await rating_el.text_content()).replace(",", ".").strip()
                            rating_val = float(rating_txt)
                        except Exception:
                            pass

                    # Cantidad total de reviews
                    reviews_count_el = await page.query_selector("div.F7nice > span:nth-child(2) > span > span")
                    reviews_count = 0
                    if reviews_count_el:
                        try:
                            rev_txt = await reviews_count_el.text_content()
                            rev_txt = re.sub(r"\D", "", rev_txt)
                            reviews_count = int(rev_txt)
                        except Exception:
                            pass

                    # Sitio web y teléfono
                    web_el = await page.query_selector('a[data-item-id="authority"]')
                    web_url = await web_el.get_attribute("href") if web_el else None

                    phone_el = await page.query_selector('button[data-tooltip="Copiar el número de teléfono"]')
                    phone_val = None
                    if phone_el:
                        aria_label = await phone_el.get_attribute("aria-label")
                        if aria_label:
                            phone_val = aria_label.replace("Teléfono:", "").strip()

                    # 4. Extraer opiniones (Reviews)
                    # Google Maps no siempre tiene pestaña 'Opiniones'. Intentar hacer click en ella,
                    # si no existe, intentar scroll del panel principal para cargar las reviews inline.
                    reviews_tab = await page.query_selector('button[role="tab"]:has-text("Opiniones")')
                    if not reviews_tab:
                        reviews_tab = await page.query_selector('button[role="tab"]:has-text("Reviews")')
                    if not reviews_tab:
                        reviews_tab = await page.query_selector('button[role="tab"]:has-text("Reseñas")')

                    reviews_list = []
                    if reviews_tab:
                        await reviews_tab.click()
                        await page.wait_for_timeout(2000)
                    else:
                        # Sin pestaña dedicada: intentar click en el enlace de conteo de reseñas
                        # (div.fontBodyMedium que contiene el rating y enlace de reseñas)
                        try:
                            rating_link = await page.query_selector('.fontBodyMedium span[aria-label*="reseñas"], .fontBodyMedium a[aria-label*="reseñas"]')
                            if rating_link:
                                await rating_link.click()
                                await page.wait_for_timeout(2000)
                        except Exception:
                            pass

                    # Selector contenedor de reviews (feed o panel principal scrollable)
                    feed_selector = 'div[role="feed"]'
                    feed_el = await page.query_selector(feed_selector)

                    if not feed_el:
                        # Google Maps lodging layout: reviews están en el panel lateral scrollable
                        # Intentar scroll del panel .m6QErb para forzar carga de reviews
                        try:
                            panel = await page.query_selector('div.m6QErb[tabindex="-1"]')
                            if not panel:
                                panel = await page.query_selector('div.m6QErb')
                            if panel:
                                for _ in range(5):
                                    await page.evaluate("el => el.scrollBy(0, 800)", panel)
                                    await page.wait_for_timeout(800)
                                feed_el = await page.query_selector(feed_selector)
                        except Exception:
                            pass

                    if feed_el:
                        # Scroll progresivo para cargar más opiniones
                        logger.info(f"[google_maps] Cargando opiniones de '{canonical_name}'...")
                        try:
                            for scroll_idx in range(4):
                                await page.evaluate("el => el.scrollBy(0, 800)", feed_el)
                                await page.wait_for_timeout(1200)
                        except Exception:
                            pass

                        # Parsear elementos de reviews en el DOM
                        review_cards = await page.query_selector_all('div[data-review-id]')
                        logger.info(f"[google_maps] Encontradas {len(review_cards)} opiniones visibles en el DOM.")

                        for card in review_cards:
                            try:
                                author_el = await card.query_selector("div.d4r55")
                                author = await author_el.text_content() if author_el else "Usuario Google"

                                # Obtener puntuación de estrellas
                                stars_el = await card.query_selector("span.kvwXae")
                                rating_stars = None
                                if stars_el:
                                    stars_label = await stars_el.get_attribute("aria-label")
                                    if stars_label:
                                        # Extrae el dígito ej: "5 estrellas" -> 5
                                        stars_match = re.search(r"\d", stars_label)
                                        if stars_match:
                                            rating_stars = float(stars_match.group(0))

                                # Obtener texto
                                text_el = await card.query_selector("span.wiu59c")
                                text_val = await text_el.text_content() if text_el else None

                                # ID opinión
                                rev_id = await card.get_attribute("data-review-id")
                                if not rev_id:
                                    continue

                                # Fecha aproximada a día de hoy
                                date_el = await card.query_selector("span.rsqawe")
                                date_str = await date_el.text_content() if date_el else ""
                                
                                # Mapear fecha relativa a DATE de manera defensiva.
                                # Si el regex no encuentra cantidad, dejar fecha=None
                                # en lugar de date.today() (que daría falsos "recientes").
                                fecha_db = None
                                date_str_low = date_str.lower()
                                num_match = re.search(r"\d+", date_str_low)
                                if num_match:
                                    n = int(num_match.group(0))
                                    if "día" in date_str_low or "dia" in date_str_low or "day" in date_str_low:
                                        fecha_db = date.today() - timedelta(days=n)
                                    elif "semana" in date_str_low or "week" in date_str_low:
                                        fecha_db = date.today() - timedelta(days=n * 7)
                                    elif "mes" in date_str_low or "month" in date_str_low:
                                        fecha_db = date.today() - timedelta(days=n * 30)
                                    elif "año" in date_str_low or "ano" in date_str_low or "year" in date_str_low:
                                        fecha_db = date.today() - timedelta(days=n * 365)

                                if text_val:
                                    from enrichment.review_cleaner import detect_language
                                    lang = detect_language(text_val)
                                else:
                                    lang = "en"

                                reviews_list.append({
                                    "spot_id": spot_id,
                                    "source": "google_maps",
                                    "source_review_id": f"gmaps_{rev_id}",
                                    "texto": text_val,
                                    "rating": rating_stars,
                                    "autor": author.strip(),
                                    "fecha": fecha_db,
                                    "idioma": lang
                                })
                            except Exception as card_err:
                                logger.debug(f"[google_maps] Error procesando tarjeta de opinión: {card_err}")
                                continue


                    # 5. Persistir datos normalizados
                    raw_data = {
                        "cid": cid,
                        "nombre": canonical_name,
                        "lat": orig_lat,
                        "lon": orig_lon,
                        "tipo": orig_tipo,
                        "rating": rating_val,
                        "num_reviews": reviews_count,
                        "web": web_url,
                        "telefono": phone_val,
                        "reviews_raw": len(reviews_list)
                    }

                    norm = self.normalize(raw_data)
                    if not norm:
                        continue

                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            # Enriquecer spot en tabla global
                            await enriquecer_spot(conn, spot_id, norm, self.name)
                            
                            # Guardar source record
                            await upsert_source_record(conn, spot_id, self.name, cid, raw_data, norm)
                            
                            # Guardar opiniones extraídas
                            for rev in reviews_list:
                                inserted = await upsert_review(conn, rev)
                                if inserted:
                                    stats["reviews_nuevas"] += 1
                                    
                            stats["actualizados"] += 1

                    logger.info(f"[google_maps] Spot '{orig_name}' enriquecido exitosamente. Opiniones guardadas: {len(reviews_list)}")

                except Exception as spot_err:
                    logger.error(f"[google_maps] Error en spot ID {spot_id}: {spot_err}")
                    stats["errores"] += 1

            # Cleanup garantizado aunque haya excepciones no atrapadas dentro del loop.
            # async_playwright()'s context manager también hace su parte al salir.
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

        # Finalizar el registro en base de datos
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[google_maps] Completado en {dur:.0f}s | {stats}")
        return stats
