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
    args = parser.parse_args(argv)

    pool = await create_pool()
    try:
        stale = await regenerar_embeddings_stale(pool, batch_size=args.stale_batch_size)
        logger.info(f"[nightly_embeddings] stale={stale}")
        new = await generar_embeddings_batch(pool, batch_size=args.new_batch_size)
        logger.info(f"[nightly_embeddings] new={new}")
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
