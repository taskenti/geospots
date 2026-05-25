"""Burst detection for short-lived semantic events."""

from __future__ import annotations


async def detect_semantic_events(conn) -> dict:
    inserted = await conn.execute(
        """
        INSERT INTO semantic_events (spot_id, event_type, severity, evidence_count, first_seen, expires_at, evidence_claim_ids)
        SELECT
            ec.spot_id,
            CASE WHEN ec.signal_type = 'theft_risk' THEN 'theft_spree' ELSE 'police_burst' END,
            GREATEST(0.7, AVG(ec.extraction_confidence)),
            COUNT(*),
            MIN(COALESCE(r.fecha::timestamptz, ec.created_at)),
            NOW() + INTERVAL '15 days',
            array_agg(ec.id)
        FROM extracted_claims ec
        JOIN reviews r ON r.id = ec.review_id
        WHERE ec.signal_type IN ('police_risk', 'theft_risk')
          AND ec.raw_value ~ '^[0-9]+([.][0-9]+)?$'
          AND ec.raw_value::float > 0.6
          AND ec.created_at > NOW() - INTERVAL '7 days'
        GROUP BY ec.spot_id, ec.signal_type
        HAVING COUNT(*) >= 3
        ON CONFLICT DO NOTHING
        """
    )
    expired = await conn.execute(
        "UPDATE semantic_events SET active = FALSE WHERE expires_at < NOW() AND active = TRUE"
    )
    return {"inserted": inserted, "expired": expired}
