"""Job nocturno de enrichment v2 spot-level.

SEGURIDAD (Sprint 0 / BUG-21):
  - --max-cost-usd es OBLIGATORIO en runs reales. El run PARA al alcanzarlo.
  - Scope obligatorio: uno de --country / --tier / --rest. Para ALL a propósito: --allow-global.
  - Spending cap / quota del proveedor → abort GLOBAL inmediato (exit 2).

Ejemplos:
  # España, hasta 500 spots, tope $2
  python -m jobs.nightly_enrichment_v2 --country ES --limit 500 --max-cost-usd 2.0

  # Portugal con DeepSeek explícito, tope $1
  python -m jobs.nightly_enrichment_v2 --country PT --provider deepseek --max-cost-usd 1.0

  # Tier completa (ES+PT+FR+...) con tope $10
  python -m jobs.nightly_enrichment_v2 --tier 1 --concurrency 50 --max-cost-usd 10.0

  # Dry-run para ver candidatos sin gastar (no requiere --max-cost-usd)
  python -m jobs.nightly_enrichment_v2 --country ES --dry-run

  # Resto del mundo a propósito (requiere --allow-global)
  python -m jobs.nightly_enrichment_v2 --rest --allow-global --limit 1000 --max-cost-usd 20.0
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from loguru import logger

from enrichment.db_pool import create_pool
from enrichment.orchestrator_v2 import COUNTRY_TIERS, run_enrichment


def _resolve_countries(args) -> list[str] | None:
    if args.rest:
        # NULL = sin filtro; los tiers ya procesados se distinguen por
        # enrichment_version. Aquí dejamos pasar todos y el filtro de candidatos
        # excluye los ya enriched.
        return None
    if args.tier is not None:
        codes = COUNTRY_TIERS.get(args.tier)
        if codes is None:
            return None
        return codes
    if args.country:
        return [c.strip().upper() for c in args.country.split(",")]
    return None


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly enrichment v2 (spot-level)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--country", help="ISO code(s), comma-separated. e.g. ES or ES,PT")
    group.add_argument("--tier", type=int, help=f"Tier de países: {list(COUNTRY_TIERS.keys())}")
    group.add_argument("--rest", action="store_true", help="Todos los países (sin filtro)")
    parser.add_argument("--limit", type=int, default=500, help="Max spots por run")
    parser.add_argument(
        "--max-cost-usd", type=float, default=None,
        help="OBLIGATORIO (BUG-21): tope de coste USD. El run PARA al alcanzarlo. "
             "Sin este flag el job se niega a arrancar. Usa --dry-run para estimar sin gastar.",
    )
    parser.add_argument(
        "--allow-global", action="store_true",
        help="Permite correr sin scope (--country/--tier/--rest). Sin esto, ALL silencioso está prohibido.",
    )
    parser.add_argument("--concurrency", type=int,
                        default=int(os.environ.get("ENRICHMENT_CONCURRENCY", "20")))
    parser.add_argument("--provider", choices=("gemini", "deepseek"), default=None)
    parser.add_argument("--model", default=None, help="Modelo específico (override env)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force-spot-ids", type=str, default=None,
        help="Coma-separados. Reprocesa esos spots ignorando filtros (versión, stale, reviews). "
             "Útil tras cambio de prompt sin bumpear ENRICHMENT_VERSION global (T1.7).",
    )
    args = parser.parse_args(argv)

    force_spot_ids = None
    if args.force_spot_ids:
        try:
            force_spot_ids = [int(x.strip()) for x in args.force_spot_ids.split(",") if x.strip()]
        except ValueError as e:
            logger.error(f"--force-spot-ids inválido: {e}")
            return 1

    # ── Sprint 0 (BUG-21): puertas de seguridad de presupuesto ──────────────
    # 1) Tope de coste obligatorio (salvo dry-run, que no gasta).
    if not args.dry_run and (args.max_cost_usd is None or args.max_cost_usd <= 0):
        logger.error(
            "ABORT: --max-cost-usd es OBLIGATORIO y debe ser > 0 para un run real. "
            "Un batch sin tope puede quemar el presupuesto. Usa --dry-run para estimar."
        )
        return 2
    # 2) Scope explícito obligatorio: nunca ALL silencioso.
    has_scope = bool(args.country) or (args.tier is not None) or args.rest
    if not has_scope and not args.allow_global:
        logger.error(
            "ABORT: scope obligatorio. Indica uno de --country / --tier / --rest. "
            "Para correr sobre todos los países a propósito, añade --allow-global explícitamente."
        )
        return 2

    countries = _resolve_countries(args)
    label = f"countries={countries}" if countries else "ALL"
    logger.info(f"[nightly_enrichment_v2] start {label} limit={args.limit} "
                f"max_cost_usd={args.max_cost_usd} "
                f"concurrency={args.concurrency} provider={args.provider or '(env)'} "
                f"model={args.model or '(env)'} dry_run={args.dry_run}")

    pool = await create_pool(max_size=max(8, args.concurrency + 4))
    try:
        stats = await run_enrichment(
            pool,
            countries=countries,
            limit=args.limit,
            concurrency=args.concurrency,
            provider=args.provider,
            model=args.model,
            dry_run=args.dry_run,
            force_spot_ids=force_spot_ids,
            max_cost_usd=args.max_cost_usd,
        )
    finally:
        await pool.close()

    # Exit code: 0 si succeeded o no candidatos; 2 si partial/abort; 1 si todo fallo
    if getattr(stats, "abort", False):
        logger.error("[nightly_enrichment_v2] run ABORTADO por spending cap/quota")
        return 2
    if stats.spots_requested == 0:
        return 0
    if stats.spots_succeeded == 0 and stats.spots_failed > 0:
        return 1
    if stats.spots_failed > 0:
        return 2
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
