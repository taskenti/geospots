"""Nightly semantic event detection job."""

from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from enrichment.event_detector import detect_semantic_events
from enrichment.worker import create_pool


async def main_async(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser().parse_args(argv)
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            stats = await detect_semantic_events(conn)
        logger.info(f"[events] {stats}")
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
