"""Park4Night — scraper usando API interna guest."""

import asyncio
import json
import random
from datetime import datetime
from loguru import logger
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import httpx

from sources.base import AbstractSource

P4N_BASE = "https://guest.park4night.com/services/V4.1"
P4N_LUGARES = f"{P4N_BASE}/lieuxGetFilter.php"
P4N_REVIEWS = f"{P4N_BASE}/commGet.php"
P4N_DETALLE = f"{P4N_BASE}/lieuGetDetail.php"

CODIGO_MAP = {"A": "area_ac", "P": "parking", "C": "camping", "N": "naturaleza", "H": "otro", "S": "otro"}
TIPO_MAP = {1: "area_ac", 2: "parking", 3: "camping", 4: "naturaleza", 5: "picnic", 8: "parking"}


def _b(raw: dict, key: str) -> bool | None:
    v = raw.get(key)
    if v is None:
        return None
    return str(v).strip() == "1"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=4, min=4, max=16),
    retry=(retry_if_exception_type(httpx.TimeoutException) | retry_if_exception_type(httpx.HTTPError)),
    reraise=True
)
async def _fetch_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    resp = await client.get(url, params=params, timeout=15)
    if resp.status_code == 429:
        logger.warning("P4N Rate limit 429. Esperando 60s...")
        await asyncio.sleep(60)
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()


class Park4NightSource(AbstractSource):
    """Park4Night usa puntos GPS (no bbox). Override completo de run()."""

    name = "park4night"
    rate_limit = 2.0
    grid_step = 0.25
    dedup_radius_m = 80.0

    HEADERS = {
        "User-Agent": "GeoSpots/1.0",
        "Accept": "application/json",
    }

    # P4N no usa bbox sino puntos lat/lon → no usamos fetch_cell
    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        raise NotImplementedError("P4N usa grid de puntos, no bbox")

    def normalize(self, raw: dict) -> dict | None:
        """Convierte raw P4N al esquema normalizado."""
        try:
            if "id" not in raw:
                return None

            p4n_id = int(raw["id"])
            nombre = (raw.get("name") or raw.get("titre") or "Sin nombre").strip()[:200]

            tipo = None
            code = raw.get("code")
            if code and code in CODIGO_MAP:
                tipo = CODIGO_MAP[code]
            else:
                id_type = int(raw.get("id_type", 0)) if raw.get("id_type") is not None else 0
                tipo = TIPO_MAP.get(id_type, "otro")

            lat = float(raw["latitude"])
            lon = float(raw["longitude"])

            gratuito = None
            prix = raw.get("prix")
            if prix is not None:
                if str(prix).strip() == "0":
                    gratuito = True
                elif str(prix).strip() == "1":
                    gratuito = False

            rating_str = raw.get("note_moyenne") or raw.get("note")
            rating = float(rating_str) if rating_str and float(rating_str) > 0 else None

            nb_comm = raw.get("nb_commentaires") or raw.get("nb_comm")
            num_reviews = int(nb_comm) if nb_comm is not None else 0

            hauteur = raw.get("hauteur_limite")
            altura = float(hauteur) if hauteur and float(hauteur) > 0 else None

            plazas = raw.get("nb_places")
            num_plazas = int(plazas) if plazas and int(plazas) > 0 else None

            fotos = [
                {"large": f.get("link_large"), "thumb": f.get("link_thumb")}
                for f in raw.get("photos", []) if f.get("link_large")
            ]

            return {
                "source_id": str(p4n_id),
                "nombre": nombre,
                "lat": lat,
                "lon": lon,
                "tipo": tipo,
                "gratuito": gratuito,
                "precio_info": raw.get("prix_stationnement") or raw.get("prix_services"),
                "rating_promedio": rating,
                "num_reviews": num_reviews,
                "num_plazas": num_plazas,
                "altura_max_m": altura,
                "country_iso": str(raw.get("pays_iso")).lower() if raw.get("pays_iso") else None,
                "region": raw.get("ville"),
                "web": raw.get("site_internet"),
                "telefono": raw.get("tel"),
                "email": raw.get("mail"),
                "descripcion_fr": raw.get("description_fr") or raw.get("description"),
                "descripcion_en": raw.get("description_en"),
                "descripcion_de": raw.get("description_de"),
                "descripcion_es": raw.get("description_es"),
                "descripcion_it": raw.get("description_it"),
                "descripcion_nl": raw.get("description_nl"),
                "agua_potable": _b(raw, "point_eau") or _b(raw, "water"),
                "vaciado_negras": _b(raw, "eau_noire") or _b(raw, "black_water"),
                "vaciado_grises": _b(raw, "eau_usee") or _b(raw, "grey_water"),
                "electricidad": _b(raw, "electricite") or _b(raw, "electricity"),
                "ducha": _b(raw, "douche") or _b(raw, "shower"),
                "wifi": _b(raw, "wifi"),
                "wc_publico": _b(raw, "wc_public") or _b(raw, "wc"),
                "perros": _b(raw, "animaux") or _b(raw, "animal"),
                "acceso_grandes": _b(raw, "camping_car"),
                "fotos_urls": fotos,
            }
        except Exception as e:
            logger.error(f"Error normalizando P4N id={raw.get('id')}: {e}")
            return None

    def _parse_review(self, raw: dict, spot_id: int) -> dict | None:
        try:
            texto = (raw.get("commentaire") or raw.get("comment") or "").strip() or None
            rating_val = raw.get("note")
            rating = int(rating_val) if rating_val and 1 <= int(rating_val) <= 5 else None

            fecha = None
            fecha_str = str(raw.get("date_creation") or raw.get("date_insert", ""))
            if len(fecha_str) >= 10:
                try:
                    fecha = datetime.strptime(fecha_str[:10], "%Y-%m-%d").date()
                except Exception:
                    pass

            return {
                "spot_id": spot_id,
                "source": "park4night",
                "source_review_id": f"p4n_{raw['id']}",
                "texto": texto,
                "rating": rating,
                "fecha": fecha,
                "autor": raw.get("uuid") or raw.get("pseudo") or raw.get("login"),
                "idioma": None,
            }
        except Exception:
            return None

    def _generate_points(self) -> list[tuple[float, float]]:
        """Genera grid de puntos lat/lon para toda Europa."""
        puntos = []
        lat = 35.0
        while lat <= 71.5:
            lon = -11.0
            while lon <= 30.0:
                puntos.append((round(lat, 4), round(lon, 4)))
                lon += self.grid_step
            lat += self.grid_step
        random.shuffle(puntos)
        return puntos

    async def run(self, pool, config, log_id: int) -> dict:
        """Override completo: P4N usa puntos, no bbox."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, upsert_review,
            finish_scraper_log, update_fuente_config
        )
        from datetime import timezone

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        puntos = self._generate_points()
        logger.info(f"[park4night] {len(puntos)} puntos GPS a procesar")

        seen_ids: set[str] = set()
        sem = asyncio.Semaphore(config.max_workers)

        async with httpx.AsyncClient(follow_redirects=True, headers=self.HEADERS) as client:

            async def procesar_punto(lat, lon):
                async with sem:
                    await asyncio.sleep(self.rate_limit)
                    try:
                        data = await _fetch_json(client, P4N_LUGARES, {
                            "latitude": lat, "longitude": lon
                        })
                    except Exception as e:
                        logger.warning(f"[P4N] Error punto {lat},{lon}: {e}")
                        stats["errores"] += 1
                        return

                    lugares_raw = data.get("lieux") or data.get("tab_lieux") or []

                    for raw in lugares_raw:
                        norm = self.normalize(raw)
                        if not norm:
                            continue

                        sid = norm["source_id"]
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                        try:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    existente = await find_spot_cercano(
                                        conn, norm["lat"], norm["lon"],
                                        self.dedup_radius_m
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

                            # Reviews (si el spot tiene y son nuevas)
                            n_reviews = norm.get("num_reviews", 0)
                            if n_reviews > 0:
                                await asyncio.sleep(self.rate_limit)
                                try:
                                    rev_data = await _fetch_json(client, P4N_REVIEWS, {
                                        "lieu_id": int(sid)
                                    })
                                    rev_list = rev_data.get("commentaires") or rev_data.get("historique") or []
                                    async with pool.acquire() as conn:
                                        for rev_raw in rev_list:
                                            rev = self._parse_review(rev_raw, spot_id)
                                            if rev:
                                                try:
                                                    await upsert_review(conn, rev)
                                                    stats["reviews_nuevas"] += 1
                                                except Exception:
                                                    pass
                                except Exception as e:
                                    logger.warning(f"[P4N] Reviews {sid}: {e}")

                        except Exception as e:
                            logger.error(f"[P4N] Error '{norm.get('nombre')}': {e}")
                            stats["errores"] += 1

            LOTE = 50
            for i in range(0, len(puntos), LOTE):
                batch = puntos[i:i+LOTE]
                await asyncio.gather(*[procesar_punto(lat, lon) for lat, lon in batch])

                logger.info(
                    f"[park4night] {min(i+LOTE, len(puntos))}/{len(puntos)} | "
                    f"uniq={len(seen_ids)} new={stats['nuevos']} "
                    f"upd={stats['actualizados']} rev={stats['reviews_nuevas']} "
                    f"err={stats['errores']}"
                )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[park4night] Completado en {dur:.0f}s | {stats}")
        return stats
