# -*- coding: utf-8 -*-
"""StayFree — scraper de spots para autocaravanas.

Estrategia (diagnosticada 2026-05-22):
  - API publica: https://www.stayfree.app/api/spots
    - NO requiere auth. Devuelve spots sin coordenadas.
    - Bug zstd: El servidor (Cloudflare) responde con zstd si el Origin no es
      same-origin. FIX: usar Origin=https://www.stayfree.app y sec-fetch-site=same-origin.
    - maxResults=100 es el limite (503 con 200+). Paginacion por page=N (0-indexed).

  - API privada: https://api.stayfree.app/v1/spots (con coordenadas)
    - Requiere: Authorization: Bearer <JWT> + x-api-token: <app_token_estatico>
    - El x-api-token es un token hardcodeado en el APK de la app nativa.
    - El JWT del usuario (Supabase) NO funciona como x-api-token.
    - Para obtener el x-api-token: capturar trafico MITM del emulador Android.
    - Cuando STAYFREE_AUTHORIZATION y STAYFREE_API_TOKEN esten en .env,
      el scraper usara la API privada automaticamente.

Estado actual: Solo API publica disponible. Los spots se descartan en normalize()
por falta de coordenadas. El scraper escribe los source_records pero no inserta
nuevos spots hasta tener el x-api-token de la app.
"""

import asyncio
import os
import json
from datetime import datetime, timezone
from loguru import logger

from sources.base import AbstractSource

BASE_URL    = "https://www.stayfree.app/api/spots"
DETAIL_URL  = "https://www.stayfree.app/api/spots/{sid}"

# Códigos HTTP que indican credenciales inválidas/expiradas
AUTH_FAIL_CODES = (401, 403, 419)


def _log_token_expired(source_name: str, status: int, where: str, body_snippet: str = "") -> None:
    """Mensaje muy visible cuando un token caduca o no es válido.

    Se llama desde varios puntos del scraper. Centralizado para que el
    operador siempre vea las mismas instrucciones."""
    snippet = f" Body: {body_snippet[:120]}" if body_snippet else ""
    logger.error(
        f"\n{'='*70}\n"
        f"[{source_name}] CREDENCIALES STAYFREE INVÁLIDAS O CADUCADAS (HTTP {status} en {where}).{snippet}\n"
        f"\n"
        f"Cómo regenerar:\n"
        f"  1. Abre la app StayFree en el navegador y autentícate\n"
        f"  2. DevTools → Network → cualquier request autenticado\n"
        f"  3. Copia el header 'Authorization' (Bearer ...) y actualiza\n"
        f"     STAYFREE_AUTHORIZATION en .env\n"
        f"  4. Si también ha caducado el token de la app móvil:\n"
        f"     captura tráfico MITM del APK y actualiza STAYFREE_API_TOKEN\n"
        f"  5. Reinicia el container scraper (no necesita rebuild)\n"
        f"{'='*70}"
    )

# Todos los tipos en un solo string CSV (confirmado en captura real)
SPOT_TYPES_CSV = (
    "AGROTOURISM,PARKING_CAMPER_ACS,CAMPING_ACS,CAMPING_PRIVATE,"
    "PARKING_CAMPER,PARKING_FREE,WILD_SPOT,CAMPING"
)

# Paises objetivo (Europa + Norte de Africa)
COUNTRIES = [
    "ES", "FR", "PT", "IT", "DE", "AT", "CH", "BE", "NL", "LU",
    "GB", "IE", "DK", "SE", "NO", "FI", "IS",
    "PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "RS", "BA",
    "GR", "TR", "CY", "MT",
    "EE", "LV", "LT",
    "AL", "MK", "ME",
    "MA", "TN",
]

MAX_RESULTS = 100   # Limite del servidor — NO subir, da 503 con 200+
MAX_PAGES   = 20    # Tope de seguridad: 20 x 100 = 2000 spots por pais

# spotType API → tipo GeoSpots (para normalizar el campo _spotType inyectado)
TIPO_MAP = {
    "WILD_SPOT":          "naturaleza",
    "PARKING_FREE":       "area_ac",
    "CAMPING":            "camping",
    "PARKING_CAMPER":     "area_ac",
    "PARKING_CAMPER_ACS": "area_ac",
    "CAMPING_ACS":        "camping",
    "CAMPING_PRIVATE":    "camping",
    "AGROTOURISM":        "otro",
}

# Features API → campos normalizados GeoSpots
FEATURE_MAP = {
    "SANITARY_WATER":        "agua_potable",
    "SANITARY_TOILET":       "wc_publico",
    "SANITARY_SHOWER":       "ducha",
    "SANITARY_ELECTRICITY":  "electricidad",
    "SANITARY_DUMP_STATION": "vaciado_negras",
    "SANITARY_GREY_WATER":   "vaciado_grises",
    "SANITARY_BLACK_WATER":  "vaciado_negras",
}


def _build_public_headers() -> dict:
    """Headers para la API publica www.stayfree.app/api/spots.

    CRITICO — Origin debe ser same-origin (https://www.stayfree.app) para que
    Cloudflare responda con gzip. Con Origin=https://localhost (captura app)
    el servidor usa zstd que httpx no puede decodificar (byte 0xb5).
    """
    return {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "accept-language": "es-ES,es;q=0.9",
        "origin": "https://www.stayfree.app",
        "referer": "https://www.stayfree.app/es/campspots/",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Mobile Safari/537.36"
        ),
    }


def _build_private_headers() -> dict | None:
    """Headers para la API privada api.stayfree.app/v1/ con coordenadas.

    Requiere STAYFREE_AUTHORIZATION y STAYFREE_API_TOKEN en el .env.
    El x-api-token es el token estatico de la app nativa (no el JWT del usuario).
    Retorna None si no estan configurados.
    """
    auth = os.environ.get("STAYFREE_AUTHORIZATION", "")
    api_token = os.environ.get("STAYFREE_API_TOKEN", "")
    if not auth or auth.startswith("Bearer your_") or not api_token or api_token.startswith("your_"):
        return None
    return {
        "Authorization": auth,
        "x-api-token": api_token,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 9; SH-M24 Build/PQ3B.190801.03250903; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
            "Chrome/124.0.6367.82 Safari/537.36"
        ),
        "Origin": "https://localhost",
        "Referer": "https://localhost/",
        "X-Requested-With": "com.stayfree.app",
    }


# Compatibilidad con codigo antiguo que llama _build_headers()
def _build_headers() -> dict:
    return _build_public_headers()


def clean_surrogates(text: str) -> str:
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return "".join(c for c in text if not (0xD800 <= ord(c) <= 0xDFFF))


def detect_language(text: str) -> str:
    if not text or not isinstance(text, str):
        return "es"
    text = text.lower()
    scores = {
        "es": sum(1 for w in [" el ", " la ", " con ", " para ", " bueno ", " bien ", " muy ", " y "] if w in text),
        "fr": sum(1 for w in [" le ", " la ", " avec ", " pour ", " bon ", " bien ", " tres ", " et "] if w in text),
        "en": sum(1 for w in [" the ", " with ", " for ", " good ", " nice ", " very ", " and "] if w in text),
        "de": sum(1 for w in [" der ", " die ", " das ", " mit ", " gut ", " sehr ", " und "] if w in text),
        "it": sum(1 for w in [" il ", " la ", " con ", " per ", " buono ", " molto ", " e "] if w in text),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "es"


def parse_date(date_val):
    if not date_val:
        return None
    if isinstance(date_val, str) and date_val.isdigit():
        try:
            date_val = int(date_val)
        except ValueError:
            pass
    if isinstance(date_val, (int, float)):
        try:
            ts = date_val / 1000 if date_val > 1e11 else date_val
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except Exception:
            return None
    if isinstance(date_val, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(date_val, fmt).date()
            except ValueError:
                continue
    return None


class StayFreeSource(AbstractSource):
    name           = "stayfree"
    rate_limit     = 0.3   # 0.3s es mas rapido y seguro con buffer=1 y sem=6
    dedup_radius_m = 50.0
    grid_step      = 1.0   # No se usa (run() propio por pais)

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        if "x-api-token" in client.headers:
            url = "https://api.stayfree.app/v2/spots"
            min_lat = min(tl_lat, br_lat)
            max_lat = max(tl_lat, br_lat)
            min_lon = min(tl_lon, br_lon)
            max_lon = max(tl_lon, br_lon)
            box_data = {
                "bottomLeft": {
                    "type": "Point",
                    "coordinates": [min_lon, min_lat]
                },
                "upperRight": {
                    "type": "Point",
                    "coordinates": [max_lon, max_lat]
                }
            }
            params = {
                "locationBoxData": json.dumps(box_data),
                "currentZoom": "9",
            }
            try:
                resp = await client.get(url, params=params, timeout=20)
                if resp.status_code in AUTH_FAIL_CODES:
                    # Intento de recarga en caliente del token desde .env
                    new_auth = ""
                    if os.path.exists(".env"):
                        with open(".env", "r", encoding="utf-8") as f:
                            for line in f:
                                if line.startswith("STAYFREE_AUTHORIZATION="):
                                    new_auth = line.split("=", 1)[1].strip()
                                    if (new_auth.startswith('"') and new_auth.endswith('"')) or (new_auth.startswith("'") and new_auth.endswith("'")):
                                        new_auth = new_auth[1:-1]
                                    break
                    if new_auth and new_auth != client.headers.get("Authorization"):
                        logger.info(f"[{self.name}] Nuevo token detectado en .env. Actualizando y reintentando...")
                        client.headers["Authorization"] = new_auth
                        resp = await client.get(url, params=params, timeout=20)
                    if resp.status_code in AUTH_FAIL_CODES:
                        _log_token_expired(self.name, resp.status_code, "API privada v2", resp.text)
                        raise ValueError(f"TOKEN_EXPIRED: HTTP {resp.status_code} y no hay token nuevo válido en .env.")
                resp.raise_for_status()
                data = resp.json()
                items = data if isinstance(data, list) else data.get("spots", data.get("data", []))
                return items
            except Exception as e:
                if "TOKEN_EXPIRED" in str(e):
                    raise
                logger.error(f"[{self.name}] Error en API PRIVADA v2: {e}")
                return []
        else:
            return []

    def normalize(self, raw: dict) -> dict | None:
        # La API publica no devuelve coordenadas en ningun endpoint.
        # normalize() siempre devuelve None hasta resolver auth del v2 API.
        try:
            coords = raw.get("location", {}).get("coordinates", [])
            if len(coords) == 2:
                lon, lat = float(coords[0]), float(coords[1])
            elif "lat" in raw and "lon" in raw:
                lat, lon = float(raw["lat"]), float(raw["lon"])
            elif "latitude" in raw and "longitude" in raw:
                lat, lon = float(raw["latitude"]), float(raw["longitude"])
            else:
                return None  # Sin coordenadas — descartado
        except (TypeError, ValueError):
            return None

        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None

        features = raw.get("features") or {}
        ratings  = raw.get("ratings")  or {}

        kwargs = {}
        for api_key, campo in FEATURE_MAP.items():
            if features.get(api_key):
                kwargs[campo] = True

        precio = raw.get("priceValue")
        try:
            precio_aprox = float(precio) if precio is not None and precio != "" else None
        except (ValueError, TypeError):
            precio_aprox = None
        precio_info = f"{precio_aprox} €" if precio_aprox is not None else None

        spot_type_raw = raw.get("_spotType") or raw.get("spotType") or raw.get("_type") or ""
        tipo = TIPO_MAP.get(spot_type_raw, "otro")

        gratuito = (
            precio_aprox == 0.0
            or precio is None
            or precio == ""
            or (isinstance(precio, str) and "free" in precio.lower())
        )

        sid = raw.get("_id") or raw.get("id") or ""
        country_raw = raw.get("country") or ""

        return {
            "source_id":       str(sid),
            "nombre":          clean_surrogates(raw.get("name") or "StayFree Spot").strip()[:200],
            "lat":             lat,
            "lon":             lon,
            "tipo":            tipo,
            "gratuito":        gratuito,
            "precio_aprox":    precio_aprox,
            "precio_info":     precio_info,
            "country_iso":     country_raw[:2].upper() if country_raw else None,
            "descripcion_es":  clean_surrogates((raw.get("description") or "")[:1000]),
            "fotos_urls":      [raw["imageUrl"]] if raw.get("imageUrl") else [],
            "rating_promedio": float(ratings.get("overall_rating") or 0),
            "num_reviews":     int(ratings.get("total") or 0),
            "web":             f"https://www.stayfree.app/es/spot/{sid}",
            **kwargs,
        }

    async def _fetch_country(self, client, country: str) -> list[dict]:
        """Descarga todos los spots de un pais paginando la API."""
        all_items = []
        page = 0

        while page < MAX_PAGES:
            params = {
                "spotType":       SPOT_TYPES_CSV,
                "maxResults":     MAX_RESULTS,
                "locale":         "es",
                "sort":           "rating",
                "locationCountry": country,
                "page":           page,
            }
            try:
                resp = await client.get(BASE_URL, params=params, timeout=20)
                if resp.status_code == 503:
                    logger.warning(f"[{self.name}] 503 en {country} p{page}, esperando 10s...")
                    await asyncio.sleep(10)
                    resp = await client.get(BASE_URL, params=params, timeout=20)
                if resp.status_code == 429:
                    logger.warning(f"[{self.name}] 429 rate-limit en {country}, esperando 30s...")
                    await asyncio.sleep(30)
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(f"[{self.name}] Error {country} p{page}: {e}")
                break

            items = data if isinstance(data, list) else data.get("results", data.get("data", []))
            if not items:
                break

            all_items.extend(items)
            logger.debug(f"[{self.name}] {country} p{page}: +{len(items)} spots (total {len(all_items)})")

            if len(items) < MAX_RESULTS:
                break

            page += 1
            await asyncio.sleep(self.rate_limit)

        return all_items

    async def run(self, pool, config, log_id: int) -> dict:
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, upsert_review, finish_scraper_log, update_fuente_config,
        )
        import httpx

        inicio = datetime.now(timezone.utc)
        stats  = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {},
        }

        headers = _build_public_headers()
        private_headers = _build_private_headers()
        seen_ids: set[str] = set()

        use_private = False
        if private_headers:
            logger.info(
                f"[{self.name}] API PRIVADA activa (STAYFREE_AUTHORIZATION + STAYFREE_API_TOKEN configurados). "
                "Probando credenciales..."
            )
            try:
                # Hacer una peticion dummy de prueba
                box_dummy = {
                    "bottomLeft": {"type": "Point", "coordinates": [-4.0, 40.0]},
                    "upperRight": {"type": "Point", "coordinates": [-3.0, 41.0]}
                }
                async with httpx.AsyncClient(headers=private_headers, follow_redirects=True, timeout=10) as test_client:
                    resp = await test_client.get(
                        "https://api.stayfree.app/v2/spots",
                        params={"locationBoxData": json.dumps(box_dummy), "currentZoom": "9"}
                    )
                    if resp.status_code == 200:
                        logger.info(f"[{self.name}] Credenciales de la API PRIVADA validadas con exito.")
                        use_private = True
                    elif resp.status_code in AUTH_FAIL_CODES:
                        _log_token_expired(self.name, resp.status_code, "validación inicial", resp.text)
                        logger.warning(f"[{self.name}] Fallback a API pública (sin coordenadas).")
                    else:
                        logger.warning(
                            f"[{self.name}] Prueba de API privada fallo con estado {resp.status_code}. "
                            f"Mensaje: {resp.text[:200]}. Haciendo fallback a API publica (no tendra coordenadas)."
                        )
            except Exception as e:
                logger.warning(
                    f"[{self.name}] Error probando la API privada: {e}. "
                    "Haciendo fallback a API publica."
                )
        else:
            logger.warning(
                f"[{self.name}] LIMITACION: Credenciales privadas ausentes o incompletas en .env. "
                "Haciendo fallback a API publica (los spots se descargaran pero seran descartados en normalize() por falta de coordenadas)."
            )

        if use_private:
            # Flujo API Privada (Optimizada con buffer=1, sem=6 y LOTE=30 para completar antes de que expire el token de 1h)
            PROGRESS_FILE = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "scratch",
                "stayfree_progress.json"
            )
            completed_cells = set()
            if os.path.exists(PROGRESS_FILE):
                try:
                    with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                        completed_list = json.load(f)
                        if isinstance(completed_list, list):
                            completed_cells = set(completed_list)
                    logger.info(f"[{self.name}] Cargado progreso anterior. {len(completed_cells)} celdas ya procesadas.")
                except Exception as e:
                    logger.warning(f"[{self.name}] Error cargando archivo de progreso: {e}")

            cells = await self.generate_active_grid(pool, step=self.grid_step, buffer=1)
            all_cells_count = len(cells)
            cells = [c for c in cells if f"{c[0]},{c[1]},{c[2]},{c[3]}" not in completed_cells]
            logger.info(f"[{self.name}] Escaneo privado de celdas: {len(cells)} restantes de {all_cells_count} totales.")
            sem = asyncio.Semaphore(6)

            async with httpx.AsyncClient(headers=private_headers, follow_redirects=True, timeout=25) as client:
                LOTE = 30
                for i in range(0, len(cells), LOTE):
                    batch = cells[i:i+LOTE]

                    async def handle(cell):
                        async with sem:
                            await asyncio.sleep(self.rate_limit)
                            return await self.fetch_cell(client, *cell)

                    results = await asyncio.gather(*[handle(c) for c in batch], return_exceptions=True)

                    for result in results:
                        if isinstance(result, Exception):
                            if "TOKEN_EXPIRED" in str(result):
                                logger.critical(f"[{self.name}] Scraper detenido: El token ha expirado. Por favor, actualiza STAYFREE_AUTHORIZATION en .env")
                                raise result
                            logger.warning(f"[{self.name}] Error en celda: {result}")
                            stats["errores"] += 1
                            continue

                        for raw_item in result:
                            norm = self.normalize(raw_item)
                            if not norm:
                                continue

                            sid = norm["source_id"]
                            if not sid or sid == "None" or sid in seen_ids:
                                continue
                            seen_ids.add(sid)

                            try:
                                async with pool.acquire() as conn:
                                    async with conn.transaction():
                                        existente = await find_spot_cercano(
                                            conn, norm["lat"], norm["lon"],
                                            self.dedup_radius_m,
                                            nombre=norm.get("nombre"),
                                            tipo=norm.get("tipo"),
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
                                            conn, spot_id, self.name, sid, raw_item, norm
                                        )
                            except Exception as e:
                                logger.error(f"[{self.name}] Error '{norm.get('nombre')}': {e}")
                                stats["errores"] += 1

                    # Registrar progreso del lote completado con éxito
                    for cell in batch:
                        completed_cells.add(f"{cell[0]},{cell[1]},{cell[2]},{cell[3]}")
                    try:
                        os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
                        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
                            json.dump(list(completed_cells), f, indent=2)
                    except Exception as e:
                        logger.error(f"[{self.name}] Error guardando progreso: {e}")

                    logger.info(
                        f"[{self.name}] {min(i+LOTE, len(cells))}/{len(cells)} | "
                        f"uniq={len(seen_ids)} new={stats['nuevos']} "
                        f"upd={stats['actualizados']} err={stats['errores']}"
                    )

            if os.path.exists(PROGRESS_FILE):
                try:
                    os.remove(PROGRESS_FILE)
                    logger.info(f"[{self.name}] Archivo de progreso eliminado tras completar la ejecucion de celdas.")
                except Exception as e:
                    logger.error(f"[{self.name}] Error eliminando archivo de progreso: {e}")
        else:
            # Flujo API Publica (Fallback)
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
                for country in COUNTRIES:
                    logger.info(f"[{self.name}] Procesando pais {country}...")
                    items = await self._fetch_country(client, country)
                    logger.info(f"[{self.name}] {country}: {len(items)} spots descargados")

                    for raw_item in items:
                        norm = self.normalize(raw_item)
                        if not norm:
                            continue

                        sid = norm["source_id"]
                        if not sid or sid == "None" or sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                        try:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    existente = await find_spot_cercano(
                                        conn, norm["lat"], norm["lon"],
                                        self.dedup_radius_m,
                                        nombre=norm.get("nombre"),
                                        tipo=norm.get("tipo"),
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
                                        conn, spot_id, self.name, sid, raw_item, norm
                                    )
                        except Exception as e:
                            logger.error(f"[{self.name}] Error '{norm.get('nombre')}': {e}")
                            stats["errores"] += 1

                    await asyncio.sleep(self.rate_limit)

        # Finalizar logs en BD
        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)
 
        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[{self.name}] Completado en {dur:.0f}s | {stats}")
        return stats

    async def download_reviews(self, pool, config) -> dict:
        from db import upsert_review
        import httpx
 
        stats = {
            "nuevos": 0,
            "actualizados": 0,
            "reviews_nuevas": 0,
            "errores": 0
        }
 
        logger.info(f"[{self.name}] Buscando spots pendientes de reviews...")
        async with pool.acquire() as conn:
            review_jobs = await conn.fetch("""
                SELECT sr.spot_id, sr.source_id, sr.review_count, COALESCE(r.cnt, 0) as db_review_count
                FROM source_records sr
                LEFT JOIN (
                    SELECT spot_id, COUNT(*) as cnt
                    FROM reviews
                    WHERE source = 'stayfree'
                    GROUP BY spot_id
                ) r ON sr.spot_id = r.spot_id
                WHERE sr.source = 'stayfree'
                  AND sr.review_count > 0
                  AND COALESCE(r.cnt, 0) < sr.review_count
                ORDER BY sr.review_count DESC;
            """)
 
        logger.info(f"[{self.name}] Encontrados {len(review_jobs)} spots pendientes de reviews.")
 
        if not review_jobs:
            return stats
 
        job_queue = asyncio.Queue()
        for r in review_jobs:
            await job_queue.put(dict(r))
 
        async def fetch_detail_worker(client):
            while not job_queue.empty():
                try:
                    job = await job_queue.get()
                except asyncio.CancelledError:
                    break
 
                sid      = job["source_id"]
                spot_id  = job["spot_id"]
                
                try:
                    await asyncio.sleep(self.rate_limit)
                    resp = await client.get(DETAIL_URL.format(sid=sid))
                    if resp.status_code in (404, 410):
                        job_queue.task_done()
                        continue
                    if resp.status_code == 503:
                        await asyncio.sleep(10)
                        resp = await client.get(DETAIL_URL.format(sid=sid))
                    resp.raise_for_status()
                    detail = resp.json()
 
                    comments = detail.get("comments") or detail.get("reviews") or []
                    fotos_list = detail.get("photos") or detail.get("images") or []
 
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            for idx, r_raw in enumerate(comments):
                                text = clean_surrogates(
                                    r_raw.get("comment") or r_raw.get("text") or r_raw.get("texto") or ""
                                )
                                rating_val = r_raw.get("overall_rating") or r_raw.get("rating")
                                try:
                                    rating = int(rating_val) if rating_val is not None else None
                                except (ValueError, TypeError):
                                    rating = None
 
                                stable_id = (
                                    r_raw.get("id") or r_raw.get("_id") or r_raw.get("comment_id")
                                    or r_raw.get("review_id") or r_raw.get("timestamp")
                                    or f"{r_raw.get('owner_id') or r_raw.get('username') or 'anon'}:{text[:80]}"
                                )
                                review_dict = {
                                    "spot_id":          spot_id,
                                    "source":           "stayfree",
                                    "source_review_id": f"stayfree_{sid}_{stable_id}",
                                    "texto":            text or None,
                                    "rating":           rating,
                                    "autor":            r_raw.get("owner_id") or r_raw.get("username"),
                                    "fecha":            parse_date(r_raw.get("timestamp") or r_raw.get("date")),
                                    "idioma":           detect_language(text),
                                }
                                inserted = await upsert_review(conn, review_dict)
                                stats["reviews_nuevas"] += int(bool(inserted))
 
                            fotos_urls = []
                            for p in fotos_list:
                                url = p.get("url") if isinstance(p, dict) else p
                                if url and url not in fotos_urls:
                                    fotos_urls.append(url)
 
                            img_url = detail.get("imageUrl")
                            if img_url and img_url not in fotos_urls:
                                fotos_urls.insert(0, img_url)
 
                            if fotos_urls:
                                row = await conn.fetchrow(
                                    "SELECT fotos_urls FROM spots WHERE id = $1", spot_id
                                )
                                existing = []
                                if row and row["fotos_urls"]:
                                    try:
                                        existing = json.loads(row["fotos_urls"])
                                        if not isinstance(existing, list):
                                            existing = []
                                    except Exception:
                                        pass
                                merged = existing + [u for u in fotos_urls if u not in existing]
                                await conn.execute(
                                    "UPDATE spots SET fotos_urls = $1 WHERE id = $2",
                                    json.dumps(merged[:15]),
                                    spot_id,
                                )
                    stats["actualizados"] += 1
                except Exception as e:
                    logger.warning(f"[{self.name}] Error detalle {sid}: {e}")
                    stats["errores"] += 1
                finally:
                    job_queue.task_done()
 
        # Iniciar trabajadores concurrentes compartiendo un único cliente httpx
        num_workers = min(config.max_workers or 3, 5)
        async with httpx.AsyncClient(headers=_build_headers(), follow_redirects=True, timeout=20) as client:
            workers = []
            for _ in range(num_workers):
                workers.append(asyncio.create_task(fetch_detail_worker(client)))
 
            await job_queue.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
 
        return stats
