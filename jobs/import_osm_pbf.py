"""Importa POIs de un PBF de país a la tabla osm_pois (Sprint 4 — PBF local).

Extrae SOLO las categorías que usa el motor geoespacial (geo_context.CATEGORIES):
agua potable, vaciado, supermercado, gasolinera, farmacia, mirador. Resultado:
una tabla pequeña y local que geo_context consulta con KNN (sin Overpass).

Requisitos en el container (one-off; añadir a Dockerfile/requirements para
permanencia):
    apt-get install -y libexpat1
    pip install osmium

Uso:
    docker compose exec scraper python -m jobs.import_osm_pbf \
        --pbf /data/spain-260529.osm.pbf --country es

Idempotente por país: borra los POIs previos de ese country antes de insertar.
Tras importar, el .pbf se puede borrar (regenerable re-descargando de Geofabrik).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import asyncpg
from loguru import logger

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scraper"))

from geo_context import CATEGORIES  # única fuente de verdad de categorías


def _dsn() -> str:
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    pw = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


# {clave_osm: {valor_osm: nombre_categoria}} — solo 3 claves para minimizar
# lookups por objeto (amenity/shop/tourism).
def _build_keymap():
    km: dict[str, dict[str, str]] = {}
    for name, key, val in CATEGORIES:
        km.setdefault(key, {})[val] = name
    return km


def parse_pbf(pbf_path: str) -> list[tuple]:
    """Recorre el PBF con pyosmium y devuelve filas (category, lon, lat, type, id)."""
    import osmium

    keymap = _build_keymap()
    keys = tuple(keymap.keys())

    class POIHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.rows: list[tuple] = []

        def _cat(self, tags):
            for k in keys:
                v = tags.get(k)
                if v is not None:
                    name = keymap[k].get(v)
                    if name:
                        return name
            return None

        def node(self, n):
            cat = self._cat(n.tags)
            if cat and n.location.valid():
                self.rows.append((cat, n.location.lon, n.location.lat, "node", n.id))

        def way(self, w):
            cat = self._cat(w.tags)
            if not cat:
                return
            lons, lats = [], []
            for wn in w.nodes:
                loc = wn.location
                if loc.valid():
                    lons.append(loc.lon)
                    lats.append(loc.lat)
            if lons:
                self.rows.append(
                    (cat, sum(lons) / len(lons), sum(lats) / len(lats), "way", w.id)
                )

    h = POIHandler()
    t0 = time.monotonic()
    # locations=True + idx flex_mem: cachea coords de nodos para resolver ways.
    h.apply_file(pbf_path, locations=True, idx="flex_mem")
    logger.info(
        f"[import_pbf] parseado {pbf_path} en {time.monotonic()-t0:.0f}s → "
        f"{len(h.rows)} POIs"
    )
    return h.rows


async def load_rows(rows: list[tuple], country: str) -> dict:
    stats = {"insertados": 0, "por_categoria": {}}
    for r in rows:
        stats["por_categoria"][r[0]] = stats["por_categoria"].get(r[0], 0) + 1

    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                deleted = await conn.execute(
                    "DELETE FROM osm_pois WHERE country = $1", country
                )
                logger.info(f"[import_pbf] borrados previos de '{country}': {deleted}")
                await conn.execute(
                    "CREATE TEMP TABLE _stg_pois (category text, lon float8, "
                    "lat float8, osm_type text, osm_id bigint) ON COMMIT DROP"
                )
                await conn.copy_records_to_table(
                    "_stg_pois", records=rows,
                    columns=["category", "lon", "lat", "osm_type", "osm_id"],
                )
                res = await conn.execute(
                    """
                    INSERT INTO osm_pois (category, geog, osm_type, osm_id, country)
                    SELECT category,
                           ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography,
                           osm_type, osm_id, $1
                    FROM _stg_pois
                    """,
                    country,
                )
                stats["insertados"] = int(res.split()[-1]) if res else 0
    finally:
        await pool.close()
    logger.info(f"[import_pbf] insertados {stats['insertados']} | {stats['por_categoria']}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Importar POIs de PBF a osm_pois")
    parser.add_argument("--pbf", default="/data/spain-260529.osm.pbf")
    parser.add_argument("--country", default="es")
    args = parser.parse_args()

    if not os.path.exists(args.pbf):
        logger.error(f"[import_pbf] no existe el PBF: {args.pbf}")
        return 1

    rows = parse_pbf(args.pbf)
    if not rows:
        logger.warning("[import_pbf] 0 POIs extraídos — ¿categorías/tags?")
        return 0
    asyncio.run(load_rows(rows, args.country.lower()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
