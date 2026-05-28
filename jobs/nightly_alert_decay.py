"""Nightly cron — aplica decay sobre `spot_alerts` activas (T1.4).

Ejecutable como:
    docker-compose exec enrichment python -m jobs.nightly_alert_decay
    docker-compose exec enrichment python -m jobs.nightly_alert_decay --dry-run
    docker-compose exec enrichment python -m jobs.nightly_alert_decay --spot-id 85057

Reglas (D1+D2 del plan fase-3-hardening):
    - Decay 0.85^meses por mes desde `last_decay_at` (o `detected_at` si NULL).
    - Marca `resolved=TRUE` y fija `valid_until = today` si confidence < 0.30
      Y han pasado ≥ 180 días desde `valid_from`.
    - `permanently_closed` y `permanent_*` se saltan (solo resolución manual).
    - Tras resolver una alerta, se refresca `spot_semantic_state.active_alert_types`.

Idempotente: una segunda ejecución el mismo día no cambia nada material
(months_elapsed ≈ 0 → confidence sin cambios).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

import asyncpg
from loguru import logger

from enrichment.state_resolver import (
    apply_decay,
    decay_all_active,
    refresh_active_alert_types,
)


async def _connect() -> asyncpg.Connection:
    dsn = os.environ.get("DATABASE_URL") or (
        f"postgresql://{os.environ.get('POSTGRES_USER','geospots')}:"
        f"{os.environ.get('POSTGRES_PASSWORD','geospots')}@"
        f"{os.environ.get('POSTGRES_HOST','db')}:"
        f"{os.environ.get('POSTGRES_PORT','5432')}/"
        f"{os.environ.get('POSTGRES_DB','geospots')}"
    )
    return await asyncpg.connect(dsn=dsn)


async def run(spot_id: int | None = None, dry_run: bool = False) -> dict:
    conn = await _connect()
    try:
        if dry_run:
            logger.warning("[decay] DRY RUN — no se persistirán cambios")
            # Para dry-run abrimos transacción y la abortamos
            tr = conn.transaction()
            await tr.start()
            try:
                stats = await _do_run(conn, spot_id)
            finally:
                await tr.rollback()
        else:
            stats = await _do_run(conn, spot_id)
        return stats
    finally:
        await conn.close()


async def _do_run(conn, spot_id: int | None) -> dict:
    current_ts = datetime.now(timezone.utc)
    if spot_id is not None:
        rows = await conn.fetch(
            """
            SELECT id, spot_id, alert_type, severity, confidence,
                   detected_at, valid_from, valid_until, last_decay_at
            FROM spot_alerts
            WHERE resolved = FALSE AND spot_id = $1
            """,
            spot_id,
        )
        stats = {"scanned": 0, "decayed": 0, "resolved": 0,
                 "skipped_permanent": 0, "decaying": 0, "errors": 0}
        for r in rows:
            dec = await apply_decay(conn, dict(r), current_ts=current_ts)
            stats["scanned"] += 1
            if dec.skipped_permanent:
                stats["skipped_permanent"] += 1
            else:
                stats["decayed"] += 1
            if dec.resolved:
                stats["resolved"] += 1
            elif dec.lifecycle_state == "decaying":
                stats["decaying"] += 1
        if stats["resolved"] > 0:
            await refresh_active_alert_types(conn, spot_id)
        logger.info(f"[decay] spot={spot_id} → {stats}")
        return stats
    return await decay_all_active(conn, current_ts=current_ts)


def main():
    p = argparse.ArgumentParser(description="Nightly decay of spot_alerts (T1.4).")
    p.add_argument("--spot-id", type=int, default=None,
                   help="Aplicar decay solo a este spot (debug).")
    p.add_argument("--dry-run", action="store_true",
                   help="Calcula y loguea pero rollbackea la transacción.")
    args = p.parse_args()
    stats = asyncio.run(run(spot_id=args.spot_id, dry_run=args.dry_run))
    logger.info(f"[decay] DONE {stats}")


if __name__ == "__main__":
    main()
