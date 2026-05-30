"""Nightly Phase 4 embeddings job."""

from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from enrichment.embedding_generator import generar_embeddings_batch, regenerar_embeddings_stale
from enrichment.worker import create_pool


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stale-batch-size", type=int, default=500)
    parser.add_argument("--new-batch-size", type=int, default=1000)
    parser.add_argument("--country", type=str, default=None,
                        help="Filtrar por country_iso (ej: es). Piloto geo-aware.")
    parser.add_argument("--loop", action="store_true",
                        help="Repetir hasta agotar candidatos (cubre el país de una vez).")
    args = parser.parse_args(argv)

    pool = await create_pool()
    try:
        total_stale = total_new = 0
        while True:
            stale = await regenerar_embeddings_stale(
                pool, batch_size=args.stale_batch_size, country=args.country)
            new = await generar_embeddings_batch(
                pool, batch_size=args.new_batch_size, country=args.country)
            total_stale += stale.get("processed", 0)
            total_new += new.get("processed", 0)
            logger.info(f"[nightly_embeddings] stale={stale} new={new} "
                        f"(acum stale={total_stale} new={total_new})")
            if not args.loop:
                break
            if stale.get("processed", 0) == 0 and new.get("processed", 0) == 0:
                break  # nada más que procesar
        logger.info(f"[nightly_embeddings] FIN total_stale={total_stale} total_new={total_new}")
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
