"""Reset + re-extraccion de claims de reviews poisonados (Sprint 7).

Por que es necesario:
  Sprints 1-6 corrigieron ~25 bugs lexicos/logicos en claim_extractor y
  state_aggregator, pero los claims ya en DB fueron generados con el codigo
  antiguo. Este job los elimina de forma segura para que worker.py los
  regenere limpios.

Que borra (regenerable segun CLAUDE.md):
  - extracted_claims con extractor_name IN _REVIEW_EXTRACTORS
      ('regex_v1', 'llm_deepseek', 'llm_gemini', 'gemini_flash', ...)
  - normalized_observations asociadas (ON DELETE CASCADE)
  - reviews.llm_processed se resetea a FALSE para esas reviews

Que NO toca (inmutable segun CLAUDE.md):
  - scraped_facts_v1 (claims de source_records — evidencia canonica)
  - reviews.texto / texto_original
  - source_records.raw_data

Modo --orchestrator (adicional):
  Elimina ademas los claims de los ~203 spots ya procesados por
  orchestrator_v2 (extractor 'deepseek_spot_v2' / 'gemini_spot_v2') y
  borra los campos narrativos de spot_semantic_state (summary_en, tags,
  best_for, best_season, avoid_season). Permite re-enriquecer con el
  codigo nuevo antes de generar embeddings.

Runbook completo Sprint 7 (ejecutar en este orden):
  1.  Aplicar migracion v8 (date_estimated) si no esta aplicada:
        docker-compose exec db psql -U geospots -d geospots -f /dev/stdin < db/migration_phase3_v8.sql
      (v9 ya aplicada en Sprint 5)
  2.  Dry-run de este script para ver estadisticas:
        docker-compose exec enrichment python -m jobs.reextract_reviews
  3.  Ejecutar el reset:
        docker-compose exec enrichment python -m jobs.reextract_reviews --apply
  4.  Re-extraer reviews via worker (pais por pais):
        docker-compose exec enrichment python -m enrichment.worker --batch-size 500 --country AD
        docker-compose exec enrichment python -m enrichment.worker --batch-size 50000 --country ES
        # etc. segun plan operativo CLAUDE.md
  5.  Recomputar spot_semantic_state con observaciones limpias:
        docker-compose exec enrichment python -m jobs.full_recompute
  6.  Reset + re-enriquecimiento de los ~203 spots con orchestrator_v2:
        docker-compose exec enrichment python -m jobs.reextract_reviews --orchestrator --apply
        # luego re-ejecutar orchestrator_v2 / nightly_enrichment_v2
  7.  Limpieza de tags legacy:
        docker-compose exec enrichment python -m jobs.normalize_tags --apply
  8.  Sync de contadores de fuentes:
        docker-compose exec scraper python sync_db.py
  9.  Generar embeddings (Phase 4 — solo DESPUES de pasos 1-8):
        docker-compose exec enrichment python -m jobs.nightly_embeddings

Uso:
    python -m jobs.reextract_reviews                    # dry-run (seguro)
    python -m jobs.reextract_reviews --apply            # reset review-level claims
    python -m jobs.reextract_reviews --orchestrator     # dry-run orchestrator reset
    python -m jobs.reextract_reviews --orchestrator --apply   # ambos
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg
from loguru import logger

# Extractores de nivel-review que se regeneran con worker.py.
# scraped_facts_v1 se excluye explicitamente (inmutable).
_REVIEW_EXTRACTORS = (
    "regex_v1",
    "llm_deepseek",
    "llm_gemini",
    "gemini_flash",
)

# Extractores de nivel-spot (orchestrator_v2).
_ORCHESTRATOR_EXTRACTORS = (
    "deepseek_spot_v2",
    "gemini_spot_v2",
)


async def _connect() -> asyncpg.Connection:
    from enrichment.worker import _dsn
    dsn = os.environ.get("DATABASE_URL") or _dsn()
    return await asyncpg.connect(dsn=dsn)


async def _stats_review(conn) -> dict:
    """Cuenta lo que se borraria en el reset de review-level."""
    claims = await conn.fetchval(
        "SELECT COUNT(*) FROM extracted_claims WHERE extractor_name = ANY($1::text[])",
        list(_REVIEW_EXTRACTORS),
    )
    obs = await conn.fetchval(
        """
        SELECT COUNT(*) FROM normalized_observations no
        JOIN extracted_claims ec ON no.claim_id = ec.id
        WHERE ec.extractor_name = ANY($1::text[])
        """,
        list(_REVIEW_EXTRACTORS),
    )
    reviews_processed = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT ec.review_id)
        FROM extracted_claims ec
        WHERE ec.extractor_name = ANY($1::text[])
          AND ec.review_id IS NOT NULL
        """,
        list(_REVIEW_EXTRACTORS),
    )
    return {
        "claims_to_delete": claims,
        "observations_to_delete": obs,
        "reviews_to_reset": reviews_processed,
    }


async def _stats_orchestrator(conn) -> dict:
    """Cuenta lo que se borraria en el reset de orchestrator."""
    claims = await conn.fetchval(
        "SELECT COUNT(*) FROM extracted_claims WHERE extractor_name = ANY($1::text[])",
        list(_ORCHESTRATOR_EXTRACTORS),
    )
    spots = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT spot_id) FROM extracted_claims
        WHERE extractor_name = ANY($1::text[])
        """,
        list(_ORCHESTRATOR_EXTRACTORS),
    )
    return {"orchestrator_claims_to_delete": claims, "orchestrator_spots": spots}


async def reset_review_claims(conn, apply: bool) -> dict:
    """Elimina claims de nivel-review y resetea llm_processed."""
    stats = await _stats_review(conn)

    if not apply:
        logger.warning(
            f"[reextract] DRY RUN review-level -- "
            f"{stats['claims_to_delete']} claims, "
            f"{stats['observations_to_delete']} observations, "
            f"{stats['reviews_to_reset']} reviews a resetear. "
            f"Usa --apply para ejecutar."
        )
        return stats

    # 1. Resetear llm_processed ANTES de borrar claims (para no perder el join).
    reset_count = await conn.execute(
        """
        UPDATE reviews r SET llm_processed = FALSE
        WHERE EXISTS (
            SELECT 1 FROM extracted_claims ec
            WHERE ec.review_id = r.id
              AND ec.extractor_name = ANY($1::text[])
        )
        """,
        list(_REVIEW_EXTRACTORS),
    )
    logger.info(f"[reextract] llm_processed reseteado: {reset_count}")

    # 2. Borrar claims (observations caen por CASCADE).
    deleted = await conn.execute(
        "DELETE FROM extracted_claims WHERE extractor_name = ANY($1::text[])",
        list(_REVIEW_EXTRACTORS),
    )
    logger.info(
        f"[reextract] borrados {stats['claims_to_delete']} claims "
        f"({stats['observations_to_delete']} observations por CASCADE)"
    )

    # 3. Marcar todos los spots afectados como stale para que full_recompute
    #    los recoja. El trigger solo marca stale en INSERT, no en DELETE.
    await conn.execute(
        """
        UPDATE spot_semantic_state SET stale = TRUE
        WHERE spot_id IN (
            SELECT DISTINCT spot_id FROM normalized_observations
        )
        """
    )
    logger.info("[reextract] spots marcados stale para full_recompute")

    stats["applied"] = True
    stats["deleted"] = deleted
    return stats


async def reset_orchestrator_claims(conn, apply: bool) -> dict:
    """Elimina claims del orchestrator y limpia campos narrativos."""
    stats = await _stats_orchestrator(conn)

    if not apply:
        logger.warning(
            f"[reextract] DRY RUN orchestrator -- "
            f"{stats['orchestrator_claims_to_delete']} claims en "
            f"{stats['orchestrator_spots']} spots. Usa --apply para ejecutar."
        )
        return stats

    # Recoge los spot_ids ANTES de borrar (el subquery post-delete devolveria 0).
    spot_ids = await conn.fetch(
        "SELECT DISTINCT spot_id FROM extracted_claims WHERE extractor_name = ANY($1::text[])",
        list(_ORCHESTRATOR_EXTRACTORS),
    )
    affected_ids = [r["spot_id"] for r in spot_ids]

    # Borrar claims (observations caen por CASCADE).
    await conn.execute(
        "DELETE FROM extracted_claims WHERE extractor_name = ANY($1::text[])",
        list(_ORCHESTRATOR_EXTRACTORS),
    )
    logger.info(
        f"[reextract] borrados {stats['orchestrator_claims_to_delete']} claims "
        f"orchestrator para {len(affected_ids)} spots"
    )

    # Limpiar campos narrativos de spot_semantic_state para esos spots.
    # tags/best_for/best_season/avoid_season/summary_en son regenerables.
    if affected_ids:
        cleared = await conn.execute(
            """
            UPDATE spot_semantic_state SET
                summary_en   = NULL,
                tags         = NULL,
                best_for     = NULL,
                best_season  = NULL,
                avoid_season = NULL,
                stale        = TRUE
            WHERE spot_id = ANY($1::int[])
            """,
            affected_ids,
        )
        logger.info(f"[reextract] narrativa limpiada: {cleared}")

    stats["applied"] = True
    return stats


async def run(apply: bool = False, orchestrator: bool = False) -> dict:
    conn = await _connect()
    try:
        stats: dict = {}

        # Siempre muestra/ejecuta el reset de review-level.
        stats.update(await reset_review_claims(conn, apply))

        # Opcional: reset del orchestrator.
        if orchestrator:
            stats.update(await reset_orchestrator_claims(conn, apply))

        return stats
    finally:
        await conn.close()


def main():
    p = argparse.ArgumentParser(
        description="Reset de claims poisonados de reviews para Sprint 7."
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Ejecuta las eliminaciones. Sin esta flag es dry-run.",
    )
    p.add_argument(
        "--orchestrator",
        action="store_true",
        help=(
            "Incluye ademas el reset de claims del orchestrator_v2 (~203 spots). "
            "Ejecutar DESPUES de --apply (paso 6 del runbook)."
        ),
    )
    args = p.parse_args()
    stats = asyncio.run(run(apply=args.apply, orchestrator=args.orchestrator))
    logger.info(f"[reextract] DONE {stats}")


if __name__ == "__main__":
    main()
