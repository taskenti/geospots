"""Recompute popularity_score y reliability_score de spots.

Diseño:
  - SQL puro con CTEs para velocidad sobre 843K spots (~30s vs horas en Python).
  - Idempotente: re-correr no hace daño, sobreescribe con valores recalculados.
  - Sin trigger por overhead — los scores cambian lentamente (delay <1 día OK).

popularity_score (0-1):
  = log1p(total_reviews) / log1p(50)  * 0.5      ← 50+ reviews → max
  + log1p(num_fuentes)   / log1p(4)   * 0.3      ← 4+ fuentes → max
  + recency_factor                     * 0.2     ← review reciente

reliability_score (0-1):
  = avg(source_credibility) * 0.4
  + min(num_fuentes/3, 1)   * 0.3
  + has_any_review          * 0.2
  + has_recent_review (180d) * 0.1

Uso:
  python -m jobs.recompute_spot_metrics                # todos los spots activos
  python -m jobs.recompute_spot_metrics --country ES   # solo ES
  python -m jobs.recompute_spot_metrics --spot-id 123  # un spot
  python -m jobs.recompute_spot_metrics --dry-run      # solo mostrar distribución
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from loguru import logger


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _dsn() -> str:
    _load_dotenv()
    return (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'geospots')}:"
        f"{os.environ.get('POSTGRES_PASSWORD', 'geospots')}@"
        f"{os.environ.get('DB_HOST', 'localhost')}:"
        f"{os.environ.get('DB_PORT', '25433')}/"
        f"{os.environ.get('POSTGRES_DB', 'geospots')}"
    )


# ───────────────────────────────────────────────────────────────────
# SQL: compute via CTEs sobre TODA la tabla (rápido).
# ───────────────────────────────────────────────────────────────────
# Estrategia: pre-agregamos reviews y source_records ANTES del UPDATE
# para evitar correlaciones lentas. CTEs con LATERAL serían pretty pero
# las agregaciones planas escalan mejor a 843K spots.

RECOMPUTE_SQL = """
WITH
-- 1) Agregados de reviews por spot
review_aggs AS (
    SELECT
        spot_id,
        COUNT(*) AS n_reviews_actual,
        MAX(fecha) AS max_fecha,
        BOOL_OR(fecha > NOW() - INTERVAL '180 days') AS has_recent_review
    FROM reviews
    GROUP BY spot_id
),
-- 2) Agregados de source_records por spot + credibility media
source_aggs AS (
    SELECT
        sr.spot_id,
        AVG(COALESCE(sc.base_score, 0.5))::real AS avg_credibility,
        COUNT(DISTINCT sr.source)::int AS distinct_sources
    FROM source_records sr
    LEFT JOIN source_credibility sc ON sc.source = sr.source
    GROUP BY sr.spot_id
),
-- 3) Combinar todo
combined AS (
    SELECT
        s.id,
        -- total_reviews materializado en spots tiende a ir desfasado;
        -- preferimos n_reviews_actual del COUNT en vivo.
        COALESCE(ra.n_reviews_actual, s.total_reviews, 0) AS n_reviews,
        COALESCE(s.num_fuentes, sa.distinct_sources, 0) AS n_fuentes,
        ra.max_fecha,
        ra.has_recent_review,
        COALESCE(sa.avg_credibility, 0.5) AS avg_credibility,
        (ra.n_reviews_actual IS NOT NULL AND ra.n_reviews_actual > 0) AS has_any_review
    FROM spots s
    LEFT JOIN review_aggs ra ON ra.spot_id = s.id
    LEFT JOIN source_aggs sa ON sa.spot_id = s.id
    WHERE s.activo = TRUE
      {where_filter}
),
-- 4) Computar scores
scores AS (
    SELECT
        id,
        ROUND((
              LEAST(LN(1 + n_reviews) / LN(51.0), 1.0) * 0.5
            + LEAST(LN(1 + n_fuentes) / LN(5.0), 1.0) * 0.3
            + CASE
                WHEN max_fecha IS NULL THEN 0.0
                WHEN max_fecha > NOW() - INTERVAL '90 days'  THEN 1.0
                WHEN max_fecha > NOW() - INTERVAL '365 days' THEN 0.7
                WHEN max_fecha > NOW() - INTERVAL '730 days' THEN 0.3
                ELSE 0.1
              END * 0.2
        )::numeric, 3)::real AS popularity_score,
        ROUND((
              avg_credibility * 0.4
            + LEAST(n_fuentes / 3.0, 1.0) * 0.3
            + (CASE WHEN has_any_review THEN 0.2 ELSE 0.0 END)
            + (CASE WHEN has_recent_review THEN 0.1 ELSE 0.0 END)
        )::numeric, 3)::real AS reliability_score
    FROM combined
)
UPDATE spots s
SET popularity_score  = sc.popularity_score,
    reliability_score = sc.reliability_score
FROM scores sc
WHERE s.id = sc.id
  AND (
      s.popularity_score  IS DISTINCT FROM sc.popularity_score
   OR s.reliability_score IS DISTINCT FROM sc.reliability_score
  );
"""


DISTRIBUTION_SQL = """
SELECT
    ROUND(popularity_score::numeric, 1) AS pop_bucket,
    COUNT(*) AS n_spots
FROM spots
WHERE activo = TRUE AND popularity_score IS NOT NULL
GROUP BY pop_bucket
ORDER BY pop_bucket;
"""

RELIABILITY_DIST_SQL = """
SELECT
    ROUND(reliability_score::numeric, 1) AS rel_bucket,
    COUNT(*) AS n_spots
FROM spots
WHERE activo = TRUE AND reliability_score IS NOT NULL
GROUP BY rel_bucket
ORDER BY rel_bucket;
"""

SAMPLE_SQL = """
SELECT id, canonical_name, country_iso, tipo, total_reviews, num_fuentes,
       popularity_score, reliability_score
FROM spots
WHERE activo = TRUE
ORDER BY {order_by} DESC NULLS LAST
LIMIT 8;
"""


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recompute popularity_score + reliability_score")
    parser.add_argument("--country", help="ISO code (lowercase) — solo ese país")
    parser.add_argument("--spot-id", type=int, help="Solo este spot (debug)")
    parser.add_argument("--dry-run", action="store_true", help="No escribe, solo muestra distribución actual")
    args = parser.parse_args(argv)

    conn = await asyncpg.connect(dsn=_dsn())
    try:
        if args.dry_run:
            logger.info("[recompute] DRY RUN — solo muestro distribución actual\n")
        else:
            # Construir filtro WHERE adicional
            where_parts = []
            params: list = []
            pi = 1
            if args.spot_id:
                where_parts.append(f"AND s.id = ${pi}")
                params.append(args.spot_id)
                pi += 1
            if args.country:
                where_parts.append(f"AND s.country_iso = ${pi}")
                params.append(args.country.lower())
                pi += 1
            where_filter = " ".join(where_parts)

            sql = RECOMPUTE_SQL.format(where_filter=where_filter)
            logger.info(f"[recompute] running UPDATE (country={args.country} spot_id={args.spot_id})...")
            result = await conn.execute(sql, *params)
            logger.info(f"[recompute] {result}")

        # Mostrar distribución
        print("\n=== Distribución popularity_score (bucket=0.1) ===")
        rows = await conn.fetch(DISTRIBUTION_SQL)
        for r in rows:
            bar = "█" * int(r["n_spots"] / 1000)
            print(f"  {r['pop_bucket']:>5}  {r['n_spots']:>8,}  {bar[:80]}")

        print("\n=== Distribución reliability_score (bucket=0.1) ===")
        rows = await conn.fetch(RELIABILITY_DIST_SQL)
        for r in rows:
            bar = "█" * int(r["n_spots"] / 1000)
            print(f"  {r['rel_bucket']:>5}  {r['n_spots']:>8,}  {bar[:80]}")

        print("\n=== Top 8 spots por popularity_score ===")
        rows = await conn.fetch(SAMPLE_SQL.format(order_by="popularity_score"))
        for r in rows:
            print(f"  pop={r['popularity_score']:.3f}  rel={r['reliability_score']:.3f}  "
                  f"reviews={r['total_reviews']:>5}  fuentes={r['num_fuentes']}  "
                  f"[{r['country_iso']}] {r['tipo']:<10} {r['canonical_name'][:60]}")

        print("\n=== Top 8 spots por reliability_score ===")
        rows = await conn.fetch(SAMPLE_SQL.format(order_by="reliability_score"))
        for r in rows:
            print(f"  pop={r['popularity_score']:.3f}  rel={r['reliability_score']:.3f}  "
                  f"reviews={r['total_reviews']:>5}  fuentes={r['num_fuentes']}  "
                  f"[{r['country_iso']}] {r['tipo']:<10} {r['canonical_name'][:60]}")

        print("\n=== Spots con popularity más BAJA (potencial 'aislados') ===")
        rows = await conn.fetch("""
            SELECT id, canonical_name, country_iso, tipo, total_reviews, num_fuentes,
                   popularity_score, reliability_score
            FROM spots
            WHERE activo = TRUE AND popularity_score IS NOT NULL AND popularity_score > 0
            ORDER BY popularity_score ASC LIMIT 5;
        """)
        for r in rows:
            print(f"  pop={r['popularity_score']:.3f}  rel={r['reliability_score']:.3f}  "
                  f"reviews={r['total_reviews']:>3}  [{r['country_iso']}] {r['tipo']:<10} {r['canonical_name'][:60]}")

        return 0
    finally:
        await conn.close()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
