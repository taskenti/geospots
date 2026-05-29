"""Dedup de claims duplicados del orchestrator spot-level (BUG-30).

Causa: re-correr orchestrator_v2 con otra ENRICHMENT_VERSION (p.ej. v4 y luego
v6) reinsertaba el mismo claim por (spot, señal, review/descripción), inflando la
confidence agregada (875 obs v6 + 2.733 obs v4 sobre 102 spots).

Qué hace: para cada grupo duplicado
    (spot_id, signal_type, extractor_name, COALESCE(review_id,-1))
restringido a extractores '%_spot_v2', CONSERVA el claim de MAYOR id (el más
reciente — la última versión de enriquecimiento) y BORRA los demás. Las
`normalized_observations` de los claims borrados caen por ON DELETE CASCADE.

Solo afecta a tablas REGENERABLES (normalized_observations) y a copias REDUNDANTES
de claims (la evidencia única se conserva: queda una fila por grupo). NO toca
'scraped_facts_v1' (inmutable, multi-fuente).

DEBE ejecutarse ANTES de aplicar la migración v9 (el índice único fallaría si
quedan duplicados).

Uso:
    docker-compose exec enrichment python -m jobs.dedup_claims            # dry-run
    docker-compose exec enrichment python -m jobs.dedup_claims --apply    # ejecuta
    docker-compose exec enrichment python -m jobs.dedup_claims --apply --spot-id 85057

Idempotente: una segunda ejecución no encuentra duplicados.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg
from loguru import logger

_ORCHESTRATOR_EXTRACTORS = ("gemini_spot_v2", "deepseek_spot_v2")


async def _connect() -> asyncpg.Connection:
    from enrichment.worker import _dsn
    dsn = os.environ.get("DATABASE_URL") or _dsn()
    return await asyncpg.connect(dsn=dsn)


# Selecciona los ids REDUNDANTES (todos menos el de mayor id por grupo).
_REDUNDANT_IDS_SQL = """
    SELECT id FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY spot_id, signal_type, extractor_name, COALESCE(review_id, -1)
                   ORDER BY id DESC
               ) AS rn
        FROM extracted_claims
        WHERE extractor_name = ANY($1::text[])
          AND ($2::int IS NULL OR spot_id = $2)
    ) ranked
    WHERE rn > 1
"""


async def run(spot_id: int | None = None, apply: bool = False) -> dict:
    conn = await _connect()
    try:
        redundant = await conn.fetch(_REDUNDANT_IDS_SQL, list(_ORCHESTRATOR_EXTRACTORS), spot_id)
        ids = [r["id"] for r in redundant]
        stats = {"duplicate_claims": len(ids), "deleted_claims": 0, "applied": apply}
        if not ids:
            logger.info("[dedup] sin duplicados orchestrator — nada que hacer")
            return stats
        if not apply:
            logger.warning(
                f"[dedup] DRY RUN — {len(ids)} claims duplicados detectados "
                f"(usa --apply para borrarlos). Observaciones asociadas caen por CASCADE."
            )
            return stats
        # Borrado real (las observations caen por ON DELETE CASCADE).
        deleted = await conn.execute(
            "DELETE FROM extracted_claims WHERE id = ANY($1::bigint[])", ids
        )
        stats["deleted_claims"] = len(ids)
        logger.info(f"[dedup] borrados {len(ids)} claims duplicados ({deleted})")
        return stats
    finally:
        await conn.close()


def main():
    p = argparse.ArgumentParser(description="Dedup de claims duplicados orchestrator (BUG-30).")
    p.add_argument("--spot-id", type=int, default=None, help="Limitar a un spot (debug).")
    p.add_argument("--apply", action="store_true",
                   help="Ejecuta el borrado. Sin esta flag es dry-run.")
    args = p.parse_args()
    stats = asyncio.run(run(spot_id=args.spot_id, apply=args.apply))
    logger.info(f"[dedup] DONE {stats}")


if __name__ == "__main__":
    main()
