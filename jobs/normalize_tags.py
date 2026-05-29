"""Normalización one-off de tags legacy en spot_semantic_state (BUG-12).

Los ~200 spots enriquecidos por orchestrator_v2 antes de que T1.5
(canonicalize_batch) se integrara en ingest_v2 tienen tags almacenados
en formato bruto del LLM: espacios en vez de guiones ("dog friendly"),
aliases sin resolver ("crowded" en vez de "busy"), o tags fuera del
vocabulario ("large pitches").

Qué hace:
  1. Carga el índice canonical_tags desde la DB.
  2. Recorre todos los spots con tags no-nulos.
  3. Para cada array, ejecuta canonicalize_batch (normaliza + resuelve aliases).
  4. Si el array canónico difiere del original, actualiza spot_semantic_state.tags.
  5. Los tags no resueltos SE DESCARTAN (se quedan en unknown_tags para revisión).

Idempotente: correr una segunda vez no modifica nada (ya están canónicos).
Seguro: spot_semantic_state es regenerable según CLAUDE.md.

Uso:
    docker-compose exec enrichment python -m jobs.normalize_tags           # dry-run
    docker-compose exec enrichment python -m jobs.normalize_tags --apply   # ejecuta
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg
from loguru import logger

from enrichment.tag_canonicalizer import canonicalize_batch, load_canonical_index


async def _connect() -> asyncpg.Connection:
    from enrichment.worker import _dsn
    dsn = os.environ.get("DATABASE_URL") or _dsn()
    return await asyncpg.connect(dsn=dsn)


async def run(apply: bool = False) -> dict:
    conn = await _connect()
    try:
        # Pre-carga el índice una vez para todo el batch.
        index = await load_canonical_index(conn)
        logger.info(f"[normalize_tags] índice cargado: {len(index)} entradas")

        rows = await conn.fetch(
            """
            SELECT spot_id, tags
            FROM spot_semantic_state
            WHERE tags IS NOT NULL AND array_length(tags, 1) > 0
            """
        )
        logger.info(f"[normalize_tags] {len(rows)} spots con tags para revisar")

        stats = {
            "spots_checked": len(rows),
            "spots_updated": 0,
            "tags_dropped": 0,
            "applied": apply,
        }

        for row in rows:
            spot_id = row["spot_id"]
            raw_tags = list(row["tags"])

            # register_unknown=False: solo queremos normalizar, no inflar
            # unknown_tags con lo que ya fue registrado en su momento.
            canonical, unknown = await canonicalize_batch(
                conn, raw_tags, register_unknown=False
            )

            # Comparación de sets (orden puede diferir).
            if set(canonical) == set(raw_tags):
                continue  # ya canónico, nada que hacer

            stats["spots_updated"] += 1
            stats["tags_dropped"] += len(unknown)

            if apply:
                await conn.execute(
                    "UPDATE spot_semantic_state SET tags = $1 WHERE spot_id = $2",
                    canonical or None,
                    spot_id,
                )
            else:
                logger.debug(
                    f"[normalize_tags] spot={spot_id} "
                    f"raw={raw_tags} -> canonical={canonical} dropped={unknown}"
                )

        if not apply:
            logger.warning(
                f"[normalize_tags] DRY RUN -- {stats['spots_updated']} spots "
                f"necesitan actualización, {stats['tags_dropped']} tags serán descartados. "
                f"Usa --apply para ejecutar."
            )
        else:
            logger.info(
                f"[normalize_tags] actualizados {stats['spots_updated']} spots, "
                f"{stats['tags_dropped']} tags no canónicos descartados."
            )

        return stats
    finally:
        await conn.close()


def main():
    p = argparse.ArgumentParser(
        description="Normaliza tags legacy en spot_semantic_state (BUG-12)."
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Ejecuta las actualizaciones. Sin esta flag es dry-run.",
    )
    args = p.parse_args()
    stats = asyncio.run(run(apply=args.apply))
    logger.info(f"[normalize_tags] DONE {stats}")


if __name__ == "__main__":
    main()
