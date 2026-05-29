"""Clase base para todas las fuentes de datos de GeoSpots."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from loguru import logger
import asyncio
import json


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

    @staticmethod
    def coords_validas(lat, lon) -> bool:
        """Filtro de seguridad: descarta None, NaN, fuera de rango y el
        clásico (0,0) que devuelven muchas APIs cuando la geolocalización falla.
        Llamar tras normalize() y antes de cualquier INSERT."""
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            return False
        if lat_f != lat_f or lon_f != lon_f:  # NaN check
            return False
        if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
            return False
        if abs(lat_f) < 1e-6 and abs(lon_f) < 1e-6:
            return False
        return True

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

    async def generate_active_grid(self, pool, step=1.0, buffer=4):
        """Genera celdas mundiales activas basadas en spots existentes en la base de datos,
        alineando las coordenadas a múltiplos de `step` para evitar huecos y solapamientos.
        """
        from math import floor

        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT lat, lon FROM spots WHERE lat IS NOT NULL AND lon IS NOT NULL")
        
        existing_cells = set()
        for r in rows:
            try:
                lat = float(r['lat'])
                lon = float(r['lon'])
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    lat_idx = int(floor(lat / step))
                    lon_idx = int(floor(lon / step))
                    existing_cells.add((lat_idx, lon_idx))
            except (ValueError, TypeError):
                continue
        
        if not existing_cells:
            # Bootstrap: DB vacía (entorno nuevo o primera ejecución).
            # Genera un grid COARSE sobre Europa (~110 celdas de 5°) en vez del
            # grid global completo (~48k celdas con step=1°) que tumbaría APIs.
            # Tras la primera ingesta, el grid activo normal toma el relevo.
            bootstrap_step = max(step * 5, 5.0)
            logger.warning(
                f"[{self.name}] DB sin spots. Bootstrap grid Europa "
                f"(step={bootstrap_step}°, EU_BOUNDS). "
                f"Tras la primera ingesta, el grid activo se ajustará automáticamente."
            )
            cells = []
            lat = self.EU_BOUNDS["lat_max"]
            while lat > self.EU_BOUNDS["lat_min"]:
                lon = self.EU_BOUNDS["lon_min"]
                while lon < self.EU_BOUNDS["lon_max"]:
                    cells.append((
                        round(lat, 4),
                        round(lon, 4),
                        round(lat - bootstrap_step, 4),
                        round(lon + bootstrap_step, 4),
                    ))
                    lon += bootstrap_step
                lat -= bootstrap_step
            logger.info(
                f"[{self.name}] Bootstrap grid: {len(cells)} celdas cubriendo Europa "
                f"(lat {self.EU_BOUNDS['lat_min']}-{self.EU_BOUNDS['lat_max']}, "
                f"lon {self.EU_BOUNDS['lon_min']}-{self.EU_BOUNDS['lon_max']})"
            )
            return cells
            
        min_lat_idx = int(floor(-90.0 / step))
        max_lat_idx = int(floor(90.0 / step)) - 1
        n_lon = int(360.0 / step)
        half_lon = int(180.0 / step)

        buffered = set()
        for lat_idx, lon_idx in existing_cells:
            for dlat in range(-buffer, buffer + 1):
                for dlon in range(-buffer, buffer + 1):
                    c_lat = max(min_lat_idx, min(max_lat_idx, lat_idx + dlat))
                    c_lon = (lon_idx + dlon + half_lon) % n_lon - half_lon
                    buffered.add((c_lat, c_lon))
                    
        logger.info(f"[{self.name}] Grid activo dilatado (step={step}): {len(existing_cells)} celdas iniciales a {len(buffered)} celdas mundiales.")
        
        cells = []
        for lat_idx, lon_idx in buffered:
            cells.append((
                round((lat_idx + 1) * step, 4),
                round(lon_idx * step, 4),
                round(lat_idx * step, 4),
                round((lon_idx + 1) * step, 4),
            ))
        return cells

    async def run(self, pool, config, log_id: int, job_id: int = None) -> dict:
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

        cells = await self.generate_active_grid(pool, step=self.grid_step)
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

                        if not self.coords_validas(norm.get("lat"), norm.get("lon")):
                            logger.debug(
                                f"[{self.name}] Coordenadas inválidas descartadas: "
                                f"lat={norm.get('lat')} lon={norm.get('lon')} "
                                f"src_id={norm.get('source_id')}"
                            )
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
                                        self.dedup_radius_m,
                                        nombre=norm.get("nombre"),
                                        tipo=norm.get("tipo")
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

                await self.update_job_progress(
                    pool, job_id,
                    processed=min(i + LOTE, len(cells)),
                    total=len(cells),
                    stats=stats,
                )

        async with pool.acquire() as conn:
            await finish_scraper_log(conn, log_id, stats)
            await update_fuente_config(conn, self.name, stats)

        dur = (datetime.now(timezone.utc) - inicio).total_seconds()
        logger.info(f"[{self.name}] Completado en {dur:.0f}s | {stats}")
        return stats

    async def update_job_progress(self, pool, job_id, processed, total, stats=None) -> None:
        """Persiste progreso en scraper_jobs.progress de forma segura.

        No-op si job_id es None (ejecución fuera de la cola). Usa default=str
        en json.dumps porque stats contiene datetimes (iniciado_en) que de otro
        modo romperían la serialización. Cualquier fuente que sobreescriba run()
        debería llamar a este helper en su bucle principal para alimentar la
        barra de progreso del panel admin.
        """
        if not job_id:
            return
        try:
            payload = {
                "processed": processed,
                "total": total,
                "stats": stats or {},
            }
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE scraper_jobs SET progress = $1::jsonb WHERE id = $2",
                    json.dumps(payload, default=str), job_id,
                )
        except Exception as e:
            logger.warning(f"[{self.name}] Error actualizando progreso: {e}")

    async def download_reviews(self, pool, config, job_id: int = None) -> dict:
        """Descarga de comentarios desacoplada para esta fuente."""
        logger.info(f"[{self.name}] Descarga de reviews no implementada de forma desacoplada para esta fuente.")
        return {"nuevos": 0, "actualizados": 0, "reviews_nuevas": 0, "errores": 0}
