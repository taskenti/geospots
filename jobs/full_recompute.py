"""Full or targeted recompute of spot_semantic_state."""

from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from enrichment.signal_registry import STATIC_SIGNALS
from enrichment.state_aggregator import needs_recompute, recompute_spot_state
from enrichment.worker import create_pool


def _parse_ids(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(part.strip()) for part in value.split(",") if part.strip()]


async def _should_recompute_conditional(conn, spot_id: int) -> bool:
    """Gate T2.3 para el modo --conditional.

    Recomputa SIEMPRE si el spot está `stale` (llegó observación nueva). Si no,
    aplica `needs_recompute`: solo re-agrega si alguna señal presente tiene
    half-life menor que los días desde el último agregado.
    """
    row = await conn.fetchrow(
        "SELECT stale, last_aggregated_at FROM spot_semantic_state WHERE spot_id = $1",
        spot_id,
    )
    # Sin estado previo → primer agregado, siempre recomputar.
    if row is None or row["last_aggregated_at"] is None:
        return True
    if row["stale"]:
        return True
    from datetime import datetime, timezone
    elapsed_days = max(0.0, (datetime.now(timezone.utc) - row["last_aggregated_at"]).total_seconds() / 86400.0)
    sig_rows = await conn.fetch(
        "SELECT DISTINCT signal_type FROM normalized_observations WHERE spot_id = $1",
        spot_id,
    )
    half_lives = [
        STATIC_SIGNALS[r["signal_type"]].half_life_days
        for r in sig_rows
        if r["signal_type"] in STATIC_SIGNALS
    ]
    return needs_recompute(half_lives, elapsed_days)


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spot-ids")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--conditional", action="store_true",
                        help="T2.3: salta spots persistentes recién agregados (gate needs_recompute).")
    args = parser.parse_args(argv)
    spot_ids = _parse_ids(args.spot_ids)
    pool = await create_pool()
    recomputed = 0
    skipped = 0
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
                if args.conditional and not await _should_recompute_conditional(conn, spot_id):
                    skipped += 1
                    continue
                await recompute_spot_state(conn, spot_id)
                recomputed += 1
        logger.info(f"[full_recompute] recomputed={recomputed} skipped={skipped}")
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
