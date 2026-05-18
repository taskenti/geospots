"""Clase base para todas las fuentes de datos de GeoSpots."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from loguru import logger
import asyncio


class AbstractSource(ABC):
    """Cada fuente hereda de aquí e implementa fetch_cell + normalize."""

    name: str = "unknown"
    rate_limit: float = 1.0
    grid_step: float = 1.0
    dedup_radius_m: float = 100.0

    # Grid Europa por defecto
    EU_BOUNDS = {
        "lat_min": 34.0, "lat_max": 71.5,
        "lon_min": -25.0, "lon_max": 45.0,
    }

    @abstractmethod
    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        """Descarga items raw de una celda bbox. Devuelve lista de dicts crudos."""

    @abstractmethod
    def normalize(self, raw: dict) -> dict | None:
        """Convierte un item raw al esquema normalizado GeoSpots.
        Debe devolver al menos: nombre, lat, lon, source_id, tipo.
        Devuelve None si el item no es válido."""

    def generate_grid(self):
        """Genera celdas (tl_lat, tl_lon, br_lat, br_lon) para toda Europa."""
        lat = self.EU_BOUNDS["lat_max"]
        while lat > self.EU_BOUNDS["lat_min"]:
            lon = self.EU_BOUNDS["lon_min"]
            while lon < self.EU_BOUNDS["lon_max"]:
                yield (
                    round(lat, 4),
                    round(lon, 4),
                    round(lat - self.grid_step, 4),
                    round(lon + self.grid_step, 4),
                )
                lon += self.grid_step
            lat -= self.grid_step

    async def run(self, pool, config, log_id: int) -> dict:
        """Pipeline completo: grid → fetch → normalize → dedup → store."""
        from db import (
            find_spot_cercano, crear_spot, enriquecer_spot,
            upsert_source_record, finish_scraper_log, update_fuente_config
        )
        import httpx

        inicio = datetime.now(timezone.utc)
        stats = {
            "nuevos": 0, "actualizados": 0, "reviews_nuevas": 0,
            "errores": 0, "iniciado_en": inicio, "detalle": {}
        }

        cells = list(self.generate_grid())
        logger.info(f"[{self.name}] {len(cells)} celdas a procesar")

        seen_ids: set[str] = set()
        sem = asyncio.Semaphore(3)
        headers = getattr(self, 'HEADERS', {})

        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            LOTE = 20
            for i in range(0, len(cells), LOTE):
                batch = cells[i:i+LOTE]

                async def handle(cell):
                    async with sem:
                        await asyncio.sleep(self.rate_limit)
                        return await self.fetch_cell(client, *cell)

                results = await asyncio.gather(*[handle(c) for c in batch],
                                                return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        logger.warning(f"[{self.name}] Error en celda: {result}")
                        stats["errores"] += 1
                        continue

                    for raw_item in result:
                        norm = self.normalize(raw_item)
                        if not norm:
                            continue

                        sid = str(norm.get("source_id", ""))
                        if not sid or sid in seen_ids:
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
                                        await enriquecer_spot(
                                            conn, spot_id, norm, self.name
                                        )
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
                            logger.error(f"[{self.name}] Error '{norm.get('nombre')}': {e}")
                            stats["errores"] += 1

                logger.info(
                    f"[{self.name}] {min(i+LOTE, len(cells))}/{len(cells)} | "
                    f"uniq={len(seen_ids)} new={stats['nuevos']} "
                    f"upd={stats['actualizados']} err={stats['errores']}"
                )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[{self.name}] Completado en {dur:.0f}s | {stats}")
        return stats
