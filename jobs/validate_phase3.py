"""Phase 3 validation checks from the implementation plan."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import sys

from loguru import logger

from enrichment.event_detector import detect_semantic_events
from enrichment.observation_normalizer import normalize_claim
from enrichment.review_cleaner import clean_review
from enrichment.state_aggregator import aggregate_observations, recompute_spot_state, update_semantic_state
from enrichment.worker import create_pool


async def _insert_claim_and_observation(conn, spot_id: int, review_id: int, signal: str, value: str, days_ago: int = 0):
    observed_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    claim = {"signal": signal, "value": value, "confidence": 0.9}
    obs = normalize_claim(claim, source_confidence=1.0, observed_at=observed_at)
    claim_id = await conn.fetchval(
        """
        INSERT INTO extracted_claims (
            review_id, spot_id, signal_type, raw_value, extraction_confidence,
            extractor_name, extractor_version, excerpt
        ) VALUES ($1, $2, $3, $4, $5, 'validator', 'phase3', 'synthetic')
        RETURNING id
        """,
        review_id,
        spot_id,
        signal,
        value,
        0.9,
    )
    await conn.execute(
        """
        INSERT INTO normalized_observations (
            claim_id, spot_id, signal_type, value_num, value_bool, value_text,
            extraction_confidence, source_confidence, reviewer_confidence,
            observation_weight, observed_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
        claim_id,
        spot_id,
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
    return obs


async def run_validation(conn) -> dict:
    checks = {}
    cleaned, informative = clean_review("Merci ! Super endroit !!!")
    checks["cleaner_non_informative"] = cleaned and not informative

    spot_id = await conn.fetchval(
        """
        INSERT INTO spots (canonical_name, lat, lon, tipo, fuentes)
        VALUES ('phase3-validator', 40.0, -3.0, 'otro', ARRAY['validator'])
        RETURNING id
        """
    )
    review_id = await conn.fetchval(
        """
        INSERT INTO reviews (spot_id, source, source_review_id, texto, texto_original, fecha)
        VALUES ($1, 'validator', 'phase3-validator-base', 'quiet spot', 'quiet spot', CURRENT_DATE)
        RETURNING id
        """,
        spot_id,
    )

    observations = []
    for i in range(100):
        value = 0.2 + (i / 100.0) * 0.7
        observations.append(await _insert_claim_and_observation(conn, spot_id, review_id, "quietness", str(value), days_ago=i % 10))
        await update_semantic_state(conn, spot_id, observations[-1])
    incremental = await conn.fetchrow("SELECT quietness_score FROM spot_semantic_state WHERE spot_id = $1", spot_id)
    batch = await recompute_spot_state(conn, spot_id)
    checks["incremental_approx_batch"] = abs(float(incremental["quietness_score"]) - float(batch["quietness_score"])) < 1e-4

    shift_spot_id = await conn.fetchval(
        """
        INSERT INTO spots (canonical_name, lat, lon, tipo, fuentes)
        VALUES ('phase3-validator-shift', 40.1, -3.1, 'otro', ARRAY['validator'])
        RETURNING id
        """
    )
    shift_review_id = await conn.fetchval(
        """
        INSERT INTO reviews (spot_id, source, source_review_id, texto, texto_original, fecha)
        VALUES ($1, 'validator', 'phase3-validator-shift-base', 'very quiet', 'very quiet', CURRENT_DATE)
        RETURNING id
        """,
        shift_spot_id,
    )
    initial_obs = await _insert_claim_and_observation(conn, shift_spot_id, shift_review_id, "quietness", "0.9")
    await update_semantic_state(conn, shift_spot_id, initial_obs)
    previous_snapshot_count = await conn.fetchval("SELECT COUNT(*) FROM spot_semantic_snapshots WHERE spot_id = $1", shift_spot_id)
    for _ in range(5):
        obs = await _insert_claim_and_observation(conn, shift_spot_id, shift_review_id, "quietness", "0.2")
        await update_semantic_state(conn, shift_spot_id, obs)
    snapshot_count = await conn.fetchval("SELECT COUNT(*) FROM spot_semantic_snapshots WHERE spot_id = $1", shift_spot_id)
    checks["snapshot_trigger"] = snapshot_count > previous_snapshot_count

    trace = await conn.fetchrow(
        """
        SELECT no.id AS observation_id, ec.id AS claim_id, r.id AS review_id, ec.excerpt, r.texto_original
        FROM normalized_observations no
        JOIN extracted_claims ec ON ec.id = no.claim_id
        JOIN reviews r ON r.id = ec.review_id
        WHERE no.spot_id = $1
        LIMIT 1
        """,
        spot_id,
    )
    checks["traceability"] = bool(trace and trace["observation_id"] and trace["claim_id"] and trace["review_id"])

    for i in range(4):
        rid = await conn.fetchval(
            """
            INSERT INTO reviews (spot_id, source, source_review_id, texto, texto_original, fecha)
            VALUES ($1, 'validator', $2, 'police fine', 'police fine', CURRENT_DATE)
            RETURNING id
            """,
            spot_id,
            f"phase3-police-{i}",
        )
        await _insert_claim_and_observation(conn, spot_id, rid, "police_risk", "0.8")
    await detect_semantic_events(conn)
    event_count = await conn.fetchval(
        "SELECT COUNT(*) FROM semantic_events WHERE spot_id = $1 AND event_type = 'police_burst'",
        spot_id,
    )
    checks["event_burst"] = event_count >= 1

    return checks


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="Persist synthetic validation rows")
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
        logger.info(f"[validate_phase3] {checks}")
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            logger.error(f"[validate_phase3] failed={failed}")
            return 1
        return 0
    finally:
        await pool.close()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
