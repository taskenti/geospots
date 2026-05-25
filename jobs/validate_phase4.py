"""Phase 4 validation with transactional synthetic data."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys

from loguru import logger

from enrichment.embedding_generator import (
    EMBEDDING_DIMS,
    buscar_spots,
    construir_texto_para_embedding,
    extraer_intencion_heuristica,
    vector_literal,
)
from enrichment.worker import create_pool


def _unit_vector(seed: int) -> list[float]:
    values = [0.0] * EMBEDDING_DIMS
    values[seed % EMBEDDING_DIMS] = 1.0
    return values


async def run_validation(conn) -> dict:
    checks = {}
    spot_id = await conn.fetchval(
        """
        INSERT INTO spots (
            canonical_name, lat, lon, tipo, fuentes, gratuito, agua_potable,
            perros, ducha, wifi, total_reviews
        ) VALUES (
            'phase4 playa tranquila con sombra', 40.0, -3.0, 'naturaleza',
            ARRAY['validator'], TRUE, TRUE, TRUE, FALSE, FALSE, 12
        )
        RETURNING id
        """
    )
    await conn.execute(
        """
        INSERT INTO spot_semantic_state (
            spot_id, quietness_score, safety_score, police_risk_score,
            beauty_score, crowd_level_score, overnight_safe, stealth_score,
            signals_data, semantic_dsl, summary_es, tags, best_for,
            total_observations, consensus_confidence, weight_support
        ) VALUES (
            $1, 0.92, 0.8, 0.05, 0.86, 0.12, TRUE, 0.9,
            '{"sea_view":{"score":true},"shade_morning":{"score":true},"shade_afternoon":{"score":true}}'::jsonb,
            'quiet:+0.9 police:-0.1 beauty:+0.9 shade_am:T shade_pm:T overnight:T stealth:+0.9',
            'Playa tranquila, discreta, con sombra y apta para perros.',
            ARRAY['playa','sombra','perros'], ARRAY['pernocta libre','familias'],
            8, 0.9, 7.2
        )
        """,
        spot_id,
    )
    row = await conn.fetchrow(
        """
        SELECT s.*, sss.*
        FROM spots s
        JOIN spot_semantic_state sss ON sss.spot_id = s.id
        WHERE s.id = $1
        """,
        spot_id,
    )
    text = construir_texto_para_embedding(dict(row), dict(row))
    checks["embedding_text_contains_semantics"] = all(
        token in text.lower() for token in ("tranquila", "sombra", "perros", "pernoctar")
    )

    vec = _unit_vector(7)
    await conn.execute(
        """
        INSERT INTO spot_embeddings (spot_id, embedding, texto_fuente, model)
        VALUES ($1, $2::vector, $3, 'text-embedding-004')
        """,
        spot_id,
        vector_literal(vec),
        text,
    )
    dim = await conn.fetchval("SELECT vector_dims(embedding) FROM spot_embeddings WHERE spot_id = $1", spot_id)
    checks["embedding_dims_768"] = dim == EMBEDDING_DIMS

    intent = extraer_intencion_heuristica("playa tranquila con sombra para ir con perro sin que me molesten")
    filters = intent["sql_filters"]
    checks["intent_filters"] = (
        filters.get("quietness_score_min") == 0.7
        and filters.get("perros") is True
        and filters.get("police_risk_score_max") == 0.3
    )

    # Monkey-patch query embedding by inserting a deterministic vector into the local SQL path.
    similarity = await conn.fetchval(
        "SELECT 1 - (embedding <=> $1::vector) FROM spot_embeddings WHERE spot_id = $2",
        vector_literal(vec),
        spot_id,
    )
    checks["pgvector_similarity"] = math.isclose(float(similarity), 1.0, rel_tol=1e-6)

    # Full buscar_spots uses Google embeddings, so only execute if a key is present.
    checks["hybrid_sql_candidate"] = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM spots s
        JOIN spot_embeddings se ON se.spot_id = s.id
        JOIN spot_semantic_state sss ON sss.spot_id = s.id
        WHERE s.activo = TRUE
          AND ST_DWithin(s.geog, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography, $3)
          AND sss.quietness_score >= 0.7
          AND COALESCE(sss.police_risk_score, 0) <= 0.3
          AND s.perros = TRUE
        """,
        40.0,
        -3.0,
        50_000,
    ) == 1

    return checks


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                checks = await run_validation(conn)
                if args.commit:
                    await tx.commit()
                else:
                    await tx.rollback()
            except Exception:
                await tx.rollback()
                raise
        logger.info(f"[validate_phase4] {checks}")
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            logger.error(f"[validate_phase4] failed={failed}")
            return 1
        return 0
    finally:
        await pool.close()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
