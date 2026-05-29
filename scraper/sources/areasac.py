# -*- coding: utf-8 -*-
"""AreasAC — Scraper de marcadores e información de fichas de detalle."""

import asyncio
import re
import html
import urllib.parse
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from loguru import logger
import httpx

from sources.base import AbstractSource


def clean_surrogates(text: str) -> str:
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return "".join(c for c in text if not (0xD800 <= ord(c) <= 0xDFFF))


class AreasACSource(AbstractSource):
    name = "areasac"
    rate_limit = 0.5
    dedup_radius_m = 50.0

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon):
        # No se utiliza para AreasAC ya que sobreescribimos run() completamente.
        return []

    def normalize(self, raw: dict) -> dict | None:
        return raw

    def _extract_field(self, soup, label):
        pattern = re.compile(rf"^\s*{label}", re.IGNORECASE)
        for tag in soup.find_all(class_="informacion_basica_areas_titulos"):
            text = tag.get_text()
            if pattern.search(text):
                sibling = tag.find_next_sibling()
                if sibling:
                    return sibling.get_text(strip=True)
        for tag in soup.find_all(string=pattern):
            parent = tag.parent
            if parent:
                sibling = parent.find_next_sibling()
                if sibling:
                    return sibling.get_text(strip=True)
        return ""

    def _extract_section(self, soup, title):
        pattern = re.compile(rf"^\s*{title}", re.IGNORECASE)
        for tag in soup.find_all(class_="fondo_ficha_titulo"):
            if pattern.search(tag.get_text()):
                sibling = tag.find_next_sibling(class_="aloj_direccion_int_ficha")
                if sibling:
                    return sibling.get_text(strip=True)
        return ""

    def parse_detail_html(self, html_bytes: bytes, spot_info: dict) -> dict | None:
        html_content = html_bytes.decode("iso-8859-1", errors="replace")
        soup = BeautifulSoup(html_content, "html.parser")

        name_div = soup.find(class_="aloj_dir_int_nom")
        nombre = name_div.get_text(strip=True) if name_div else ""
        if not nombre:
            h1 = soup.find("h1")
            nombre = h1.get_text(strip=True) if h1 else spot_info["name"]

        nombre = clean_surrogates(nombre)
        if not nombre:
            return None

        direccion = self._extract_field(soup, "DIRECCIÓ|DIRECCI")
        provincia = self._extract_field(soup, "PROVINCIA")
        comunidad = self._extract_field(soup, "COMUNIDAD")
        tarifa = self._extract_field(soup, "TARIFA")

        plazas_div = soup.find(class_="aloj_direccion_int_completa")
        num_plazas_str = plazas_div.get_text(strip=True) if plazas_div else "0"
        digits = re.findall(r"\d+", num_plazas_str)
        num_plazas = int(digits[0]) if digits else 0

        entorno_div = soup.find(class_="aloj_direccion_int_pernocta")
        entorno = entorno_div.get_text(strip=True) if entorno_div else ""

        acceso = self._extract_section(soup, "ACCESO")
        observaciones = self._extract_section(soup, "OBSERVACIONES")
        interesante = self._extract_section(soup, "INTERESANTE")

        active_services = []
        for div in soup.find_all("div", class_="cont_icono_ficha"):
            classes = div.get("class", [])
            if "desactivado" not in classes:
                img = div.find("img")
                if img:
                    alt = img.get("alt", "")
                    src = img.get("src", "")
                    active_services.append((alt, src))

        agua_potable = any("agua" in alt.lower() or "agua" in src.lower() for alt, src in active_services)
        vaciado_grises = any("grises" in alt.lower() or "grises" in src.lower() for alt, src in active_services)
        vaciado_negras = any("negras" in alt.lower() or "negras" in src.lower() for alt, src in active_services)
        electricidad = any("elec" in alt.lower() or "elec" in src.lower() for alt, src in active_services)
        wifi = any("wi-fi" in alt.lower() or "wifi" in alt.lower() or "wi-fi" in src.lower() or "wifi" in src.lower() for alt, src in active_services)
        perros = any("mascota" in alt.lower() or "mascota" in src.lower() for alt, src in active_services)
        ducha = any("ducha" in alt.lower() or "ducha" in src.lower() for alt, src in active_services)
        wc_publico = any("wc" in alt.lower() or "wc" in src.lower() or "baño" in alt.lower() for alt, src in active_services)

        gratuito = None
        if any("gratuita" in src.lower() or "gratis" in alt.lower() for alt, src in active_services):
            gratuito = True
        elif tarifa:
            if "gratis" in tarifa.lower() or "gratuito" in tarifa.lower() or tarifa == "0":
                gratuito = True
            else:
                gratuito = False

        photos = []
        gallery_imgs = soup.select("div.galeria img, ul#lightSlider img, div#galleria_mov img")
        for img in gallery_imgs:
            src = img.get("src", "")
            if "imagen.asp?f=" in src:
                abs_url = urllib.parse.urljoin("https://www.areasac.es", src)
                abs_url = abs_url.replace("&amp;", "&")
                if abs_url not in photos:
                    photos.append(abs_url)

        href = spot_info.get("href", "")
        filename = href.split("/")[-1]
        if filename.endswith(".html"):
            filename = filename[:-5]
        source_id = filename if filename else spot_info["name"]

        icon_path = spot_info.get("icon_path", "")
        icon_name = icon_path.split("/")[-1] if icon_path else ""
        TIPO_ICON_MAP = {
            "Area_Publica.png": "area_ac",
            "Area_Privada.png": "area_ac",
            "Area_Ruta.png": "area_ac",
            "Estacionamiento.png": "parking",
            "Area_camping.png": "camping",
        }
        tipo = TIPO_ICON_MAP.get(icon_name, "area_ac")

        desc_parts = []
        if direccion:
            desc_parts.append(f"Dirección: {direccion}")
        if comunidad:
            desc_parts.append(f"Comunidad Autónoma: {comunidad}")
        if entorno:
            desc_parts.append(f"Entorno: {entorno}")
        if acceso:
            desc_parts.append(f"Acceso: {acceso}")
        if observaciones:
            desc_parts.append(f"Observaciones: {observaciones}")
        if interesante:
            desc_parts.append(f"Interesante: {interesante}")
        descripcion_es = "\n\n".join(desc_parts)

        res = {
            "source_id": source_id,
            "nombre": nombre[:200],
            "lat": spot_info["lat"],
            "lon": spot_info["lon"],
            "tipo": tipo,
            "gratuito": gratuito,
            "precio_info": tarifa[:200] if tarifa else None,
            "num_plazas": num_plazas,
            "descripcion_es": descripcion_es,
            "country_iso": "es",
            "region": provincia[:100] if provincia else None,
            "verificado": True,
            "agua_potable": agua_potable,
            "vaciado_grises": vaciado_grises,
            "vaciado_negras": vaciado_negras,
            "electricidad": electricidad,
            "wifi": wifi,
            "perros": perros,
            "ducha": ducha,
            "wc_publico": wc_publico,
            "fotos_urls": photos
        }
        return self.normalize(res)

    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )
        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        map_url = "https://www.areasac.es/areas-servicio-autocaravanas/areasaces/espana_4_1_ap.html?m=1#contenido"
        logger.info(f"[{self.name}] Descargando mapa principal...")

        headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "referer": "https://www.areasac.es/",
        }

        try:
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
                resp = await client.get(map_url)
                if resp.status_code != 200:
                    logger.error(f"[{self.name}] Error al descargar mapa: HTTP {resp.status_code}")
                    stats["errores"] = 1
                    async with pool.acquire() as conn:
                        await finish_scraper_log(conn, log_id, stats)
                    return stats

                content = resp.content.decode("iso-8859-1", errors="replace")
        except Exception as e:
            logger.error(f"[{self.name}] Exception al descargar mapa: {e}")
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        logger.info(f"[{self.name}] Parseando marcadores del mapa...")
        init_idx = content.find("function initializeMaps()")
        if init_idx == -1:
            logger.error(f"[{self.name}] No se encontró la función initializeMaps")
            stats["errores"] = 1
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        js_code = content[init_idx:]

        events = []
        for m in re.finditer(r"new google\.maps\.MarkerImage\('([^']+)'", js_code):
            events.append({"type": "image", "pos": m.start(), "val": m.group(1)})
        for m in re.finditer(r"new google\.maps\.LatLng\(([-0-9\.]+),([-0-9\.]+)\)", js_code):
            events.append({"type": "latlng", "pos": m.start(), "val": (float(m.group(1)), float(m.group(2)))})
        for m in re.finditer(r"new google\.maps\.InfoWindow\(\{\s*content:\s*\"([^\"]+)\"", js_code):
            events.append({"type": "infowindow", "pos": m.start(), "val": m.group(1)})

        events.sort(key=lambda e: e["pos"])

        current_image = None
        last_latlng = None
        spots_to_fetch = []
        seen_hrefs = set()

        for ev in events:
            if ev["type"] == "image":
                if "shadow" not in ev["val"]:
                    current_image = ev["val"]
            elif ev["type"] == "latlng":
                last_latlng = ev["val"]
            elif ev["type"] == "infowindow":
                info_html = ev["val"]
                clean_html = info_html.replace(r"\/", "/").replace(r'\"', '"').replace(r"\'", "'")
                clean_html = html.unescape(clean_html)

                href_match = re.search(r'href=["\']([^"\']+)["\']', clean_html)
                href = href_match.group(1) if href_match else ""

                name_match = re.search(r'class=["\']info_bloque_texto[^"\']*["\']>([^<]+)<', clean_html)
                name = name_match.group(1).strip() if name_match else ""

                if last_latlng and href:
                    if href not in seen_hrefs:
                        seen_hrefs.add(href)
                        spots_to_fetch.append({
                            "lat": last_latlng[0],
                            "lon": last_latlng[1],
                            "icon_path": current_image,
                            "href": href,
                            "name": name
                        })

        logger.info(f"[{self.name}] Se encontraron {len(spots_to_fetch)} spots únicos para descargar")

        sem = asyncio.Semaphore(5)

        async def process_spot(client, spot_info):
            async with sem:
                await asyncio.sleep(self.rate_limit)
                detail_url = urllib.parse.urljoin("https://www.areasac.es", spot_info["href"])
                try:
                    resp = await client.get(detail_url)
                    if resp.status_code != 200:
                        logger.warning(f"[{self.name}] Error HTTP {resp.status_code} en ficha {detail_url}")
                        return None
                    return resp.content, spot_info
                except Exception as ex:
                    logger.warning(f"[{self.name}] Error al descargar ficha {detail_url}: {ex}")
                    return None

        LOTE_SIZE = 50
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            for i in range(0, len(spots_to_fetch), LOTE_SIZE):
                lote = spots_to_fetch[i:i+LOTE_SIZE]
                tasks = [process_spot(client, s) for s in lote]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for res in results:
                    if not res or isinstance(res, Exception):
                        if isinstance(res, Exception):
                            stats["errores"] += 1
                        continue

                    html_bytes, spot_info = res
                    try:
                        norm = self.parse_detail_html(html_bytes, spot_info)
                        if not norm:
                            continue

                        sid = norm["source_id"]
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
                                    {"raw_html_len": len(html_bytes), "spot_info": spot_info},
                                    norm
                                )
                    except Exception as ex:
                        logger.error(f"[{self.name}] Error procesando {spot_info.get('name')}: {ex}")
                        stats["errores"] += 1

                logger.info(
                    f"[{self.name}] Progreso: {min(i+LOTE_SIZE, len(spots_to_fetch))}/{len(spots_to_fetch)} | "
                    f"Nuevos: {stats['nuevos']} | Actualizados: {stats['actualizados']} | Errores: {stats['errores']}"
                )
                await self.update_job_progress(
                    pool, job_id, min(i + LOTE_SIZE, len(spots_to_fetch)), len(spots_to_fetch), stats
                )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[{self.name}] Completado en {dur:.0f}s | {stats}")
        return stats

