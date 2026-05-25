"""Batch worker for pending review enrichment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import asyncpg
from loguru import logger

from .claim_extractor import extract_claims
from .dsl_generator import generate_review_dsl
from .observation_normalizer import normalize_claims
from .review_cleaner import clean_review_full
from .state_aggregator import update_semantic_state


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _dsn() -> str:
    _load_dotenv()
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    password = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


async def create_pool(max_size: int = 4) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=max_size)


async def _insert_claim(conn, review: dict, claim: dict, pipeline_run_id: str) -> int:
    return await conn.fetchval(
        """
        INSERT INTO extracted_claims (
            review_id, spot_id, signal_type, raw_value, extraction_confidence,
            extractor_name, extractor_version, pipeline_run_id, excerpt
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        review["id"],
        review["spot_id"],
        claim["signal"],
        str(claim["value"]),
        float(claim.get("confidence", 1.0)),
        claim.get("extractor_name", "regex_v1"),
        claim.get("extractor_version", "phase3-2026-05-23"),
        pipeline_run_id,
        claim.get("excerpt"),
    )


async def _insert_observation(conn, claim_id: int, review: dict, obs) -> int:
    return await conn.fetchval(
        """
        INSERT INTO normalized_observations (
            claim_id, spot_id, signal_type, value_num, value_bool, value_text,
            extraction_confidence, source_confidence, reviewer_confidence,
            observation_weight, observed_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        claim_id,
        review["spot_id"],
        obs.signal_type,
        obs.value_num,
        obs.value_bool,
        obs.value_text,
        obs.extraction_confidence,
        obs.source_confidence,
        obs.reviewer_confidence,
        obs.observation_weight,
        obs.observed_at,
    )


async def fetch_pending_reviews(conn, batch_size: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT r.id, r.texto, r.texto_original, r.source, r.spot_id, r.fecha,
               COALESCE(sc.review_quality, 1.0) AS source_confidence,
               COALESCE(st.temperature, 'cold') AS temperature
        FROM reviews r
        LEFT JOIN source_credibility sc ON sc.source = r.source
        LEFT JOIN spot_temperature st ON st.id = r.spot_id
        WHERE COALESCE(r.llm_processed, FALSE) = FALSE
          AND COALESCE(r.texto, r.texto_original) IS NOT NULL
          AND length(COALESCE(r.texto, r.texto_original)) > 3
        ORDER BY
          CASE COALESCE(st.temperature, 'cold')
            WHEN 'hot' THEN 0
            WHEN 'warm' THEN 1
            ELSE 2
          END,
          r.fecha DESC NULLS LAST,
          r.id DESC
        LIMIT $1
        """,
        batch_size,
    )
    return [dict(r) for r in rows]


async def process_review(conn, review: dict, pipeline_run_id: str, dry_run: bool = False, use_gemini: bool = True) -> dict:
    raw_text = review.get("texto_original") or review.get("texto")
    cleaned = clean_review_full(raw_text)
    if not cleaned.informativo:
        if not dry_run:
            await conn.execute(
                """
                UPDATE reviews SET
                    texto_original = COALESCE(texto_original, texto),
                    texto_limpio = $1,
                    cleaned = TRUE,
                    informativo = FALSE,
                    llm_processed = TRUE,
                    llm_analysis = COALESCE(llm_analysis, '{}'::jsonb) || $2::jsonb
                WHERE id = $3
                """,
                cleaned.texto_limpio,
                json.dumps({"reason": "non_informative", "pipeline_run_id": pipeline_run_id}),
                review["id"],
            )
        return {"claims": 0, "observations": 0, "informativo": False}

    claims = await extract_claims(cleaned.texto_limpio, review, use_gemini=use_gemini)
    observations = normalize_claims(
        claims,
        source_confidence=review.get("source_confidence", 1.0),
        reviewer_confidence=1.0,
        observed_at=review.get("fecha") or datetime.now(timezone.utc),
    )
    review_dsl = generate_review_dsl(claims)
    if dry_run:
        return {"claims": len(claims), "observations": len(observations), "informativo": True}

    async with conn.transaction():
        await conn.execute(
            """
            UPDATE reviews SET
                texto_original = COALESCE(texto_original, texto),
                texto_limpio = $1,
                texto_dsl = $2,
                cleaned = TRUE,
                informativo = TRUE,
                idioma = COALESCE(idioma, $3),
                llm_analysis = $4::jsonb
            WHERE id = $5
            """,
            cleaned.texto_limpio,
            review_dsl,
            cleaned.idioma,
            json.dumps({"claims": claims, "pipeline_run_id": pipeline_run_id}),
            review["id"],
        )
        for claim, obs in zip(claims, observations):
            claim_id = await _insert_claim(conn, review, claim, pipeline_run_id)
            await _insert_observation(conn, claim_id, review, obs)
            await update_semantic_state(conn, review["spot_id"], obs)
        await conn.execute("UPDATE reviews SET llm_processed = TRUE WHERE id = $1", review["id"])
    return {"claims": len(claims), "observations": len(observations), "informativo": True}


async def process_pending_reviews(pool, batch_size: int = 100, dry_run: bool = False, use_gemini: bool = True) -> dict:
    pipeline_run_id = str(uuid.uuid4())
    stats = {"reviews": 0, "informative": 0, "claims": 0, "observations": 0, "errors": 0, "pipeline_run_id": pipeline_run_id}
    async with pool.acquire() as conn:
        pending = await fetch_pending_reviews(conn, batch_size)
        for review in pending:
            try:
                result = await process_review(conn, review, pipeline_run_id, dry_run=dry_run, use_gemini=use_gemini)
                stats["reviews"] += 1
                stats["informative"] += int(result["informativo"])
                stats["claims"] += result["claims"]
                stats["observations"] += result["observations"]
            except Exception as exc:
                stats["errors"] += 1
                logger.exception(f"[enrichment] Failed review {review.get('id')}: {exc}")
    return stats


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-gemini", action="store_true")
    args = parser.parse_args(argv)
    pool = await create_pool()
    try:
        stats = await process_pending_reviews(pool, args.batch_size, args.dry_run, use_gemini=not args.no_gemini)
        logger.info(f"[enrichment] stats={stats}")
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
