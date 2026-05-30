"""Backfill de claves de entidad (Sprint 2): telefono_norm, web_domain, osm_id.

Columnas REGENERABLES derivadas de datos ya existentes:
  - telefono_norm : normalize_phone(spots.telefono)
  - web_domain    : extract_domain(spots.web)   (excluye dominios de agregador)
  - osm_id        : source_records.source_id donde source='osm'

Idempotente: solo rellena filas con la clave NULL. Para recomputar todo, poner
las columnas a NULL primero.

Uso:
  python -m jobs.backfill_entity_keys
  python -m jobs.backfill_entity_keys --batch-size 20000
  python -m jobs.backfill_entity_keys --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from loguru import logger

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scraper"))

from db import normalize_phone, extract_domain  # noqa: E402


def _dsn() -> str:
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    pw = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


async def run_backfill(batch_size: int = 20000, dry_run: bool = False) -> dict:
    stats = {"telefono_norm": 0, "web_domain": 0, "osm_id": 0}
    pool = await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=4)
    try:
        # ── telefono_norm + web_domain (cálculo en Python) ──
        # Paginación KEYSET por id (no OFFSET): el predicado no depende de las
        # columnas que escribimos, así que no se saltan filas ni hay bucle
        # infinito con teléfonos no normalizables. Recalcula siempre (regenerable).
        last_id = 0
        seen = 0
        while True:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, telefono, web
                    FROM spots
                    WHERE (telefono IS NOT NULL OR web IS NOT NULL) AND id > $1
                    ORDER BY id
                    LIMIT $2
                    """,
                    last_id, batch_size,
                )
            if not rows:
                break
            last_id = rows[-1]["id"]
            seen += len(rows)
            # Recalcula SIEMPRE todas las filas candidatas (incluido escribir NULL):
            # así un re-run limpia valores obsoletos (p.ej. dominios recién excluidos).
            updates = [
                (r["id"], normalize_phone(r["telefono"]), extract_domain(r["web"]))
                for r in rows
            ]
            if updates and not dry_run:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        "UPDATE spots SET telefono_norm = $2, web_domain = $3 WHERE id = $1",
                        updates,
                    )
            stats["telefono_norm"] += sum(1 for _, tn, _ in updates if tn)
            stats["web_domain"] += sum(1 for _, _, wd in updates if wd)
            logger.info(f"[backfill] vistos {seen} (last_id={last_id}) | {stats}")

        # ── osm_id (un UPDATE join, barato) ──
        if not dry_run:
            async with pool.acquire() as conn:
                res = await conn.execute(
                    """
                    UPDATE spots s SET osm_id = sr.source_id
                    FROM source_records sr
                    WHERE sr.spot_id = s.id AND sr.source = 'osm'
                      AND s.osm_id IS NULL AND sr.source_id IS NOT NULL
                    """
                )
                stats["osm_id"] = int(res.split()[-1]) if res else 0
    finally:
        await pool.close()

    logger.info(f"[backfill] completado | {stats} | dry_run={dry_run}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill claves de entidad (Sprint 2)")
    parser.add_argument("--batch-size", type=int, default=20000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_backfill(batch_size=args.batch_size, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
