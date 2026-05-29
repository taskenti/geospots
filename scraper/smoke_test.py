#!/usr/bin/env python3
"""Smoke test: lanza todas las fuentes con grid de 2 celdas y un timeout duro.

Uso (dentro del contenedor scraper):
    python smoke_test.py            # todas las fuentes
    python smoke_test.py park4night osm  # solo esas fuentes

Escribe un reporte en stdout + smoke_test.log en el directorio actual.
"""

import asyncio
import sys
import time
from datetime import datetime, timezone

from loguru import logger
import asyncpg

from config import Config
from db import create_pool, init_scraper_log
from scheduler import SOURCES, _load_source

MAX_CELLS = 2       # celdas máx para fuentes grid-based
SPOTS_TIMEOUT = 75  # s por fuente (spots)
REVIEWS_TIMEOUT = 40  # s por fuente (reviews)

LOG_FILE = "smoke_test.log"

logger.remove()
logger.add(sys.stderr, level="INFO", format="<level>{level}</level> | {message}")
logger.add(LOG_FILE, level="DEBUG", rotation="10 MB",
           format="{time:HH:mm:ss} | {level:<7} | {message}")


async def smoke_spots(source, pool, config, key: str) -> dict:
    original = source.generate_active_grid

    async def limited(*args, **kwargs):
        cells = await original(*args, **kwargs)
        trimmed = cells[:MAX_CELLS]
        logger.info(f"[smoke] {key}: grid {len(trimmed)}/{len(cells)} celdas")
        return trimmed

    source.generate_active_grid = limited

    async with pool.acquire() as conn:
        log_id = await init_scraper_log(conn, f"smoke_{key}")

    t0 = time.time()
    try:
        stats = await asyncio.wait_for(
            source.run(pool, config, log_id=log_id, job_id=None),
            timeout=SPOTS_TIMEOUT,
        )
        return {"status": "ok", "stats": stats or {}, "elapsed": round(time.time() - t0, 1)}
    except asyncio.TimeoutError:
        return {"status": "timeout", "stats": {}, "elapsed": SPOTS_TIMEOUT}
    except Exception as e:
        logger.exception(f"[smoke] {key} spots error")
        return {"status": "error", "error": str(e)[:300], "stats": {}, "elapsed": round(time.time() - t0, 1)}


async def smoke_reviews(source, pool, config, key: str) -> dict:
    t0 = time.time()
    try:
        stats = await asyncio.wait_for(
            source.download_reviews(pool, config, job_id=None),
            timeout=REVIEWS_TIMEOUT,
        )
        return {"status": "ok", "stats": stats or {}, "elapsed": round(time.time() - t0, 1)}
    except asyncio.TimeoutError:
        return {"status": "timeout", "stats": {}, "elapsed": REVIEWS_TIMEOUT}
    except Exception as e:
        logger.exception(f"[smoke] {key} reviews error")
        return {"status": "error", "error": str(e)[:300], "stats": {}, "elapsed": round(time.time() - t0, 1)}


async def main(keys_to_run: list[str]):
    config = Config.from_env()
    pool = await create_pool(config)

    report = {}
    total = len(keys_to_run)

    for i, key in enumerate(keys_to_run, 1):
        logger.info(f"\n{'='*60}")
        logger.info(f"[{i}/{total}] SMOKE: {key}")
        logger.info(f"{'='*60}")

        try:
            source = _load_source(key)
        except Exception as e:
            report[key] = {
                "spots": {"status": "import_error", "error": str(e)[:300], "stats": {}},
                "reviews": None,
            }
            logger.error(f"[smoke] {key}: import error — {e}")
            continue

        spot_r = await smoke_spots(source, pool, config, key)
        logger.info(
            f"[smoke] {key} spots → {spot_r['status']}  "
            f"+{spot_r['stats'].get('nuevos', 0)} nuevos  "
            f"~{spot_r['stats'].get('actualizados', 0)} act  "
            f"err:{spot_r['stats'].get('errores', 0)}  "
            f"({spot_r['elapsed']}s)"
        )

        rev_r = None
        contacto = spot_r["stats"].get("nuevos", 0) + spot_r["stats"].get("actualizados", 0)
        if spot_r["status"] in ("ok", "timeout") and contacto > 0:
            logger.info(f"[smoke] {key}: lanzando reviews ({contacto} spots)...")
            rev_r = await smoke_reviews(source, pool, config, key)
            logger.info(
                f"[smoke] {key} reviews → {rev_r['status']}  "
                f"{rev_r['stats']}  ({rev_r['elapsed']}s)"
            )
        elif contacto == 0:
            logger.warning(f"[smoke] {key}: 0 spots insertados/actualizados — saltando reviews")

        report[key] = {"spots": spot_r, "reviews": rev_r}

    await pool.close()
    _print_report(report)


def _print_report(report: dict):
    ok = timeouts = errors = import_errors = 0
    lines = []

    for key, r in report.items():
        s = r["spots"]
        st = s.get("status", "?")
        stats = s.get("stats", {})
        nuevos = stats.get("nuevos", 0)
        actualizados = stats.get("actualizados", 0)
        errs = stats.get("errores", 0)
        elapsed = s.get("elapsed", 0)

        rev = r.get("reviews")
        if rev is None:
            rev_str = "—"
        elif rev["status"] == "ok":
            rn = (rev["stats"].get("reviews_nuevas")
                  or rev["stats"].get("nuevas")
                  or rev["stats"].get("total", 0))
            rev_str = f"{rn}r ✅"
        else:
            rev_str = rev["status"][:9]

        icon = {"ok": "✅", "timeout": "⏱ ", "error": "❌", "import_error": "💀"}.get(st, "? ")

        if st == "ok":        ok += 1
        elif st == "timeout": timeouts += 1
        elif st == "import_error": import_errors += 1
        else:                 errors += 1

        line = (
            f"{icon} {key:<22}  {st:<13}"
            f"  +{nuevos:<5} ~{actualizados:<5} err:{errs:<3}"
            f"  {elapsed:>5}s  rev:{rev_str}"
        )
        lines.append(line)

    sep = "=" * 78
    header = f"SMOKE TEST REPORT  —  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    output = ["\n", sep, header, sep, *lines, sep,
              f"TOTAL: {ok} ✅ OK | {timeouts} ⏱ timeout | {errors} ❌ error | "
              f"{import_errors} 💀 import_error  (de {len(report)} fuentes)"]

    # Detailed errors / import errors
    detail = [(k, r) for k, r in report.items()
              if r["spots"]["status"] in ("error", "import_error")]
    if detail:
        output.append("\n--- ERRORES DETALLADOS ---")
        for key, r in detail:
            output.append(f"\n  {key}:")
            output.append(f"    {r['spots'].get('error', '?')}")

    # Reviews errors
    rev_errors = [(k, r) for k, r in report.items()
                  if r.get("reviews") and r["reviews"]["status"] == "error"]
    if rev_errors:
        output.append("\n--- ERRORES EN REVIEWS ---")
        for key, r in rev_errors:
            output.append(f"\n  {key}:")
            output.append(f"    {r['reviews'].get('error', '?')}")

    full = "\n".join(output)
    print(full)
    # También al log
    logger.info(full)


if __name__ == "__main__":
    keys = sys.argv[1:] if len(sys.argv) > 1 else list(SOURCES.keys())
    invalid = [k for k in keys if k not in SOURCES]
    if invalid:
        print(f"Fuentes desconocidas: {invalid}")
        print(f"Disponibles: {list(SOURCES.keys())}")
        sys.exit(1)
    asyncio.run(main(keys))
