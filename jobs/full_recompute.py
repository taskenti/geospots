"""Full or targeted recompute of spot_semantic_state."""

from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from enrichment.state_aggregator import recompute_spot_state
from enrichment.worker import create_pool


def _parse_ids(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(part.strip()) for part in value.split(",") if part.strip()]


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spot-ids")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    spot_ids = _parse_ids(args.spot_ids)
    pool = await create_pool()
    recomputed = 0
    try:
        async with pool.acquire() as conn:
            if spot_ids is None:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT spot_id
                    FROM normalized_observations
                    ORDER BY spot_id
                    LIMIT NULLIF($1, 0)
                    """,
                    args.limit,
                )
                spot_ids = [r["spot_id"] for r in rows]
            for spot_id in spot_ids:
                await recompute_spot_state(conn, spot_id)
                recomputed += 1
        logger.info(f"[full_recompute] recomputed={recomputed}")
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
