"""Google Maps Places API (New) — enriquecimiento dirigido de contacto.

A diferencia de `google_maps.py` (DOM scraping con Playwright, ahora reservado
como fallback manual), esta fuente usa la **Places API oficial** vía httpx para
rellenar datos de contacto de spots que YA existen en la base canónica:

    telefono, web, direccion_formateada, rating, num_reviews

Las RESEÑAS quedan en punto muerto: la API no devuelve el texto de todas las
reviews y su scraping viola los TOS de Google. Solo tomamos campos estructurados.

Modelo de coste (Google "pay-as-you-go", $200/mes de crédito gratis)
────────────────────────────────────────────────────────────────────
Para no quemar el crédito separamos el pipeline en dos llamadas por spot:

  Fase A — searchText  (field mask mínimo: id, location, displayName)
           → resuelve el place_id y permite validar el match.
  Fase B — Place Details (solo si el match geográfico+textual es válido)
           → pide los campos de contacto/rating.

Throttling y presupuesto se controlan por env:
  GOOGLE_MAPS_API_KEY         (obligatoria)
  GOOGLE_MAPS_DAILY_BUDGET    spots a enriquecer por run (default 135)
  GOOGLE_REFRESH_DAYS         TTL antes de reintentar un spot (default 30)
  GOOGLE_MATCH_DISTANCE_M     umbral de match espacial (default 150)
  GOOGLE_MATCH_NAME_SIM       umbral de similitud de nombre (default 0.6)
  GOOGLE_MAPS_RATE_LIMIT      delay entre requests en s (default 0.2)

Idempotencia
────────────
- Match encontrado → se fija spots.google_place_id (excluido de la cola).
- Sin match       → se fija spots.google_last_refreshed (excluido por TTL).
Así un spot nunca se reconsulta en cada run.
"""

import asyncio
import os
from datetime import datetime, timezone

import httpx
from loguru import logger

from sources.base import AbstractSource
# Reutilizamos helpers ya probados del scraper DOM (su import de Playwright
# está protegido por try/except, así que importar funciones puras es seguro).
from sources.google_maps import (
    haversine_distance,
    name_similarity,
    _clean_search_name,
)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Field masks — controlan el SKU facturado. Mantener al mínimo necesario.
SEARCH_FIELD_MASK = "places.id,places.location,places.displayName"
DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,internationalPhoneNumber,"
    "nationalPhoneNumber,websiteUri,rating,userRatingCount,"
    "businessStatus,location"
)

# Prioridad de país (mismo orden que el scraper DOM): ES > PT > FR > ...
_COUNTRY_PRIORITY_SQL = """
                  CASE country_iso
                    WHEN 'es' THEN 1  WHEN 'pt' THEN 2  WHEN 'fr' THEN 3
                    WHEN 'it' THEN 4  WHEN 'de' THEN 5  WHEN 'at' THEN 6
                    WHEN 'ch' THEN 7  WHEN 'be' THEN 8  WHEN 'nl' THEN 9
                    WHEN 'lu' THEN 10 WHEN 'gb' THEN 11 WHEN 'ie' THEN 12
                    WHEN 'dk' THEN 13 WHEN 'no' THEN 14 WHEN 'se' THEN 15
                    WHEN 'fi' THEN 16 WHEN 'is' THEN 17 WHEN 'pl' THEN 18
                    WHEN 'cz' THEN 19 WHEN 'sk' THEN 20 WHEN 'hu' THEN 21
                    WHEN 'si' THEN 22 WHEN 'hr' THEN 23 WHEN 'gr' THEN 24
                    WHEN 'ro' THEN 25 WHEN 'bg' THEN 26 ELSE 99
                  END,
"""


class GoogleMapsAPISource(AbstractSource):
    """Enriquecimiento de contacto vía Places API (New). No crea spots nuevos."""

    name = "google_maps_api"
    rate_limit = 0.2
    grid_step = 1.0
    dedup_radius_m = 150.0

    # ── Config desde env ────────────────────────────────────────────
    def _load_env(self):
        self.api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
        self.daily_budget = int(os.environ.get("GOOGLE_MAPS_DAILY_BUDGET", "135"))
        self.refresh_days = int(os.environ.get("GOOGLE_REFRESH_DAYS", "30"))
        self.match_dist_m = float(os.environ.get("GOOGLE_MATCH_DISTANCE_M", "150"))
        self.match_name_sim = float(os.environ.get("GOOGLE_MATCH_NAME_SIM", "0.6"))
        self.rate_limit = float(os.environ.get("GOOGLE_MAPS_RATE_LIMIT", str(self.rate_limit)))

    # ── Interfaz abstracta (no aplica grid) ─────────────────────────
    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        raise NotImplementedError(
            "google_maps_api es un pipeline de enriquecimiento dirigido sobre "
            "spots existentes, no opera por celdas de grid."
        )

    def normalize(self, raw: dict) -> dict | None:
        """Convierte un objeto Place Details de la API al esquema GeoSpots."""
        try:
            loc = raw.get("location") or {}
            display = (raw.get("displayName") or {}).get("text")
            phone = raw.get("internationalPhoneNumber") or raw.get("nationalPhoneNumber")
            norm = {
                "source_id": raw.get("id"),
                "nombre": display,
                "lat": loc.get("latitude"),
                "lon": loc.get("longitude"),
                "telefono": phone,
                "web": raw.get("websiteUri"),
                "direccion_formateada": raw.get("formattedAddress"),
                "rating_promedio": raw.get("rating"),
                "num_reviews": raw.get("userRatingCount"),
                "fuentes": [self.name],
            }
            return norm
        except Exception as e:
            logger.error(f"[{self.name}] Error normalizando place: {e}")
            return None

    # ── Llamadas HTTP ───────────────────────────────────────────────
    async def _search_text(self, client, query, lat, lon):
        """Fase A: resuelve candidatos (id, location, displayName)."""
        body = {
            "textQuery": query,
            "maxResultCount": 5,
            "languageCode": "es",
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": 1000.0,
                }
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": SEARCH_FIELD_MASK,
        }
        r = await client.post(SEARCH_URL, json=body, headers=headers, timeout=20)
        r.raise_for_status()
        return (r.json() or {}).get("places", [])

    async def _place_details(self, client, place_id):
        """Fase B: detalles de contacto/rating del place_id ya validado."""
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": DETAILS_FIELD_MASK,
        }
        r = await client.get(
            DETAILS_URL.format(place_id=place_id), headers=headers, timeout=20
        )
        r.raise_for_status()
        return r.json()

    # ── Matching ────────────────────────────────────────────────────
    def _best_match(self, candidates, orig_name, orig_lat, orig_lon):
        """Elige el mejor candidato de la API que pase los umbrales geográfico
        y textual. Devuelve (place_id, dist_m, sim) o (None, None, None)."""
        clean = _clean_search_name(orig_name)
        best = None
        best_sim = 0.0
        best_dist = float("inf")
        for c in candidates:
            loc = c.get("location") or {}
            m_lat, m_lon = loc.get("latitude"), loc.get("longitude")
            pid = c.get("id")
            if m_lat is None or m_lon is None or not pid:
                continue
            name_text = (c.get("displayName") or {}).get("text", "")
            dist = haversine_distance(orig_lat, orig_lon, m_lat, m_lon)
            sim = max(
                name_similarity(orig_name, name_text),
                name_similarity(clean, name_text),
            )
            # Matching adaptativo (coherente con el scraper DOM):
            #   muy cerca → tolera nombre dispar; lejos → exige nombre fuerte.
            accepted = (
                (dist <= 30 and sim >= 0.30)
                or (dist <= 100 and sim >= 0.50)
                or (dist <= self.match_dist_m and sim >= self.match_name_sim)
            )
            if accepted and (sim > best_sim or (sim == best_sim and dist < best_dist)):
                best, best_sim, best_dist = pid, sim, dist
        if best:
            return best, best_dist, best_sim
        return None, None, None

    # ── Pipeline principal ──────────────────────────────────────────
    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
        from db import (
            enriquecer_spot, upsert_source_record,
            finish_scraper_log, update_fuente_config,
        )

        self._load_env()
        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio,
            "detalle": {"sin_match": 0, "cerrados": 0,
                        "search_calls": 0, "details_calls": 0, "budget": 0},
        }

        if not self.api_key:
            msg = "GOOGLE_MAPS_API_KEY no configurada — abortando."
            logger.error(f"[{self.name}] {msg}")
            stats["errores"] = 1
            stats["detalle"]["error"] = msg
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
            return stats

        # 1. Cola de candidatos priorizada (camping/area_ac sin contacto y
        #    no intentados o ya stale por TTL).
        async with pool.acquire() as conn:
            candidatos = await conn.fetch(f"""
                SELECT id, canonical_name, lat, lon, tipo, country_iso
                FROM spots
                WHERE activo = TRUE
                  AND lat IS NOT NULL AND lon IS NOT NULL
                  AND tipo IN ('camping', 'area_ac')
                  AND google_place_id IS NULL
                  AND (telefono IS NULL OR web IS NULL)
                  AND (google_last_refreshed IS NULL
                       OR google_last_refreshed < NOW() - ($1 || ' days')::interval)
                ORDER BY
                  {_COUNTRY_PRIORITY_SQL}
                  CASE tipo WHEN 'camping' THEN 1 WHEN 'area_ac' THEN 2 ELSE 99 END,
                  COALESCE(total_reviews, 0) DESC,
                  id
                LIMIT $2
            """, str(self.refresh_days), self.daily_budget)

        stats["detalle"]["budget"] = self.daily_budget
        if not candidatos:
            logger.info(f"[{self.name}] No hay candidatos para enriquecer.")
            async with pool.acquire() as conn:
                await finish_scraper_log(conn, log_id, stats)
                await update_fuente_config(conn, self.name, stats)
            return stats

        logger.info(
            f"[{self.name}] {len(candidatos)} candidatos (budget={self.daily_budget}, "
            f"refresh={self.refresh_days}d)"
        )

        async with httpx.AsyncClient() as client:
            for idx, spot in enumerate(candidatos):
                spot_id = spot["id"]
                orig_name = spot["canonical_name"]
                orig_lat = float(spot["lat"])
                orig_lon = float(spot["lon"])

                try:
                    await asyncio.sleep(self.rate_limit)

                    # Fase A — searchText
                    query = _clean_search_name(orig_name)
                    candidates = await self._search_text(client, query, orig_lat, orig_lon)
                    stats["detalle"]["search_calls"] += 1

                    pid, dist, sim = self._best_match(
                        candidates, orig_name, orig_lat, orig_lon
                    )

                    if not pid:
                        stats["detalle"]["sin_match"] += 1
                        async with pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE spots SET google_last_refreshed = NOW() WHERE id = $1",
                                spot_id,
                            )
                        continue

                    # Fase B — Place Details
                    await asyncio.sleep(self.rate_limit)
                    details = await self._place_details(client, pid)
                    stats["detalle"]["details_calls"] += 1

                    norm = self.normalize(details)
                    if not norm or not norm.get("source_id"):
                        stats["errores"] += 1
                        continue

                    biz = details.get("businessStatus")
                    cerrado = biz in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")

                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            # Enriquecer (COALESCE: nunca pisa lo que ya hay)
                            await enriquecer_spot(conn, spot_id, norm, self.name)
                            # Source record anclado al place_id (idempotente)
                            await upsert_source_record(
                                conn, spot_id, self.name, pid, details, norm
                            )
                            # Marcar place_id + refresh; desactivar si cerrado
                            if cerrado:
                                stats["detalle"]["cerrados"] += 1
                                await conn.execute(
                                    "UPDATE spots SET google_place_id = $1, "
                                    "google_last_refreshed = NOW(), activo = FALSE "
                                    "WHERE id = $2",
                                    pid, spot_id,
                                )
                            else:
                                await conn.execute(
                                    "UPDATE spots SET google_place_id = $1, "
                                    "google_last_refreshed = NOW() WHERE id = $2",
                                    pid, spot_id,
                                )
                    stats["actualizados"] += 1
                    logger.debug(
                        f"[{self.name}] '{orig_name}' → {pid} "
                        f"(dist={dist:.0f}m sim={sim:.2f}{' CERRADO' if cerrado else ''})"
                    )

                except httpx.HTTPStatusError as e:
                    stats["errores"] += 1
                    logger.warning(
                        f"[{self.name}] HTTP {e.response.status_code} en spot {spot_id}: "
                        f"{e.response.text[:200]}"
                    )
                    # 403/429 suelen ser cuota/clave → abortar para no quemar más.
                    if e.response.status_code in (403, 429):
                        logger.error(f"[{self.name}] Abortando run por {e.response.status_code}.")
                        break
                except Exception as e:
                    stats["errores"] += 1
                    logger.error(f"[{self.name}] Error en spot {spot_id}: {e}")

                if job_id and (idx + 1) % 20 == 0:
                    import json as _json
                    try:
                        async with pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE scraper_jobs SET progress = $1::jsonb WHERE id = $2",
                                _json.dumps({
                                    "processed": idx + 1,
                                    "total": len(candidatos),
                                    "stats": stats,
                                }, default=str), job_id,
                            )
                    except Exception as e:
                        logger.warning(f"[{self.name}] Error actualizando progreso: {e}")

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[{self.name}] Completado en {dur:.0f}s | {stats['detalle']}")
        return stats

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        """Reviews en punto muerto por diseño (TOS Google + sin texto masivo)."""
        logger.info(f"[{self.name}] Reviews deshabilitadas por diseño.")
        return {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}
