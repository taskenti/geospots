"""Aggregate normalized observations into current spot semantic state."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Iterable

from .dsl_generator import generate_spot_dsl
from .signal_registry import STATIC_SIGNALS, SignalType

MATERIALIZED_SCORE_COLUMNS = {
    "quietness": "quietness_score",
    "safety": "safety_score",
    "police_risk": "police_risk_score",
    "beauty": "beauty_score",
    "crowd_level": "crowd_level_score",
    "stealth": "stealth_score",
}


def _json_object(value) -> dict:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(value)


def decayed_weight(weight: float, observed_at: datetime, half_life_days: int, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return weight
    return float(weight) * math.pow(0.5, age_days / half_life_days)


def semantic_distance(a: dict | None, b: dict | None, signal_types: dict[str, SignalType] | None = None) -> float:
    if not a or not b:
        return 0.0
    signal_types = signal_types or STATIC_SIGNALS
    keys = set(a) | set(b)
    total = 0.0
    for key in keys:
        av = a.get(key, {}).get("score") if isinstance(a.get(key), dict) else a.get(key)
        bv = b.get(key, {}).get("score") if isinstance(b.get(key), dict) else b.get(key)
        if isinstance(av, bool):
            av = 1.0 if av else 0.0
        if isinstance(bv, bool):
            bv = 1.0 if bv else 0.0
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            total += abs(float(av) - float(bv)) * signal_types.get(key, SignalType(key, "numeric", 365, "weighted_mean")).importance_weight
    return total


def aggregate_observations(rows: Iterable[dict], signal_types: dict[str, SignalType] | None = None) -> dict:
    signal_types = signal_types or STATIC_SIGNALS
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row["signal_type"], []).append(row)

    signals_data: dict[str, dict] = {}
    total_weight = 0.0
    total_observations = 0
    for signal, obs_rows in buckets.items():
        stype = signal_types.get(signal)
        if not stype:
            continue
        weighted_values = []
        bool_support = {True: 0.0, False: 0.0}
        support = 0.0
        for row in obs_rows:
            observed_at = row["observed_at"]
            weight = decayed_weight(float(row["observation_weight"]), observed_at, stype.half_life_days)
            if weight <= 0:
                continue
            support += weight
            total_weight += weight
            total_observations += 1
            if stype.value_type == "boolean":
                bool_support[bool(row["value_bool"])] += weight
            elif stype.value_type == "numeric" and row["value_num"] is not None:
                weighted_values.append((float(row["value_num"]), weight))

        if support <= 0:
            continue
        if stype.value_type == "boolean":
            score_value = bool_support[True] >= bool_support[False]
            confidence = abs(bool_support[True] - bool_support[False]) / support
            signals_data[signal] = {
                "score": score_value,
                "weight_support": round(support, 6),
                "n_observations": len(obs_rows),
                "confidence": round(confidence, 6),
            }
        elif weighted_values:
            score = sum(value * weight for value, weight in weighted_values) / support
            signals_data[signal] = {
                "score": round(score, 6),
                "weight_support": round(support, 6),
                "n_observations": len(obs_rows),
                "confidence": round(min(1.0, support / 5.0), 6),
            }

    materialized = {
        column: signals_data.get(signal, {}).get("score")
        for signal, column in MATERIALIZED_SCORE_COLUMNS.items()
    }
    materialized["overnight_safe"] = signals_data.get("overnight_safe", {}).get("score")
    consensus = min(1.0, total_weight / max(1.0, total_observations * 2.0))
    return {
        "signals_data": signals_data,
        "semantic_dsl": generate_spot_dsl(signals_data),
        "total_observations": total_observations,
        "weight_support": total_weight,
        "consensus_confidence": consensus,
        **materialized,
    }


async def recompute_spot_state(conn, spot_id: int, snapshot_threshold: float = 0.15) -> dict:
    rows = await conn.fetch(
        """
        SELECT signal_type, value_num, value_bool, value_text, observation_weight, observed_at
        FROM normalized_observations
        WHERE spot_id = $1
        """,
        spot_id,
    )
    state = aggregate_observations([dict(r) for r in rows])
    current = await conn.fetchrow("SELECT signals_data FROM spot_semantic_state WHERE spot_id = $1", spot_id)
    previous = _json_object(current["signals_data"]) if current else None
    distance = semantic_distance(previous, state["signals_data"])
    if previous and distance > snapshot_threshold:
        await conn.execute(
            """
            INSERT INTO spot_semantic_snapshots (spot_id, snapshot_date, semantic_data, trigger_reason, semantic_distance)
            VALUES ($1, CURRENT_DATE, $2::jsonb, 'semantic_shift', $3)
            ON CONFLICT (spot_id, snapshot_date) DO UPDATE SET
                semantic_data = EXCLUDED.semantic_data,
                semantic_distance = GREATEST(spot_semantic_snapshots.semantic_distance, EXCLUDED.semantic_distance)
            """,
            spot_id,
            json.dumps(previous),
            distance,
        )
    await conn.execute(
        """
        INSERT INTO spot_semantic_state (
            spot_id, quietness_score, safety_score, police_risk_score, beauty_score,
            crowd_level_score, overnight_safe, stealth_score, signals_data, semantic_dsl,
            total_observations, consensus_confidence, weight_support, last_snapshot_data,
            stale, updated_at, last_aggregated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $9::jsonb,
            FALSE, NOW(), NOW()
        )
        ON CONFLICT (spot_id) DO UPDATE SET
            quietness_score = EXCLUDED.quietness_score,
            safety_score = EXCLUDED.safety_score,
            police_risk_score = EXCLUDED.police_risk_score,
            beauty_score = EXCLUDED.beauty_score,
            crowd_level_score = EXCLUDED.crowd_level_score,
            overnight_safe = EXCLUDED.overnight_safe,
            stealth_score = EXCLUDED.stealth_score,
            signals_data = EXCLUDED.signals_data,
            semantic_dsl = EXCLUDED.semantic_dsl,
            total_observations = EXCLUDED.total_observations,
            consensus_confidence = EXCLUDED.consensus_confidence,
            weight_support = EXCLUDED.weight_support,
            last_snapshot_data = EXCLUDED.last_snapshot_data,
            stale = FALSE,
            updated_at = NOW(),
            last_aggregated_at = NOW()
        """,
        spot_id,
        state.get("quietness_score"),
        state.get("safety_score"),
        state.get("police_risk_score"),
        state.get("beauty_score"),
        state.get("crowd_level_score"),
        state.get("overnight_safe"),
        state.get("stealth_score"),
        json.dumps(state["signals_data"]),
        state["semantic_dsl"],
        state["total_observations"],
        state["consensus_confidence"],
        state["weight_support"],
    )
    return state


async def update_semantic_state(conn, spot_id: int, observation: object | None = None) -> dict:
    if observation is None:
        return await recompute_spot_state(conn, spot_id)

    signal = observation.signal_type
    stype = STATIC_SIGNALS.get(signal)
    if not stype:
        return await recompute_spot_state(conn, spot_id)

    row = await conn.fetchrow(
        """
        SELECT signals_data, total_observations, weight_support, last_snapshot_data
        FROM spot_semantic_state
        WHERE spot_id = $1
        """,
        spot_id,
    )
    previous_signals = _json_object(row["signals_data"]) if row else {}
    signals_data = dict(previous_signals)
    old_entry = dict(signals_data.get(signal, {}))
    old_support = float(old_entry.get("weight_support", 0.0) or 0.0)
    obs_weight = decayed_weight(
        observation.observation_weight,
        observation.observed_at,
        stype.half_life_days,
    )

    if stype.value_type == "boolean":
        old_true = old_support if old_entry.get("score") is True else 0.0
        old_false = old_support if old_entry.get("score") is False else 0.0
        if observation.value_bool is True:
            old_true += obs_weight
        else:
            old_false += obs_weight
        support = old_true + old_false
        score = old_true >= old_false
        confidence = abs(old_true - old_false) / support if support else 0.0
    else:
        old_score = float(old_entry.get("score", 0.0) or 0.0)
        support = old_support + obs_weight
        value = observation.value_num if observation.value_num is not None else old_score
        score = ((old_score * old_support) + (float(value) * obs_weight)) / support if support else value
        confidence = min(1.0, support / 5.0)

    signals_data[signal] = {
        "score": score,
        "weight_support": round(support, 6),
        "n_observations": int(old_entry.get("n_observations", 0) or 0) + 1,
        "confidence": round(confidence, 6),
    }

    materialized = {
        column: signals_data.get(sig, {}).get("score")
        for sig, column in MATERIALIZED_SCORE_COLUMNS.items()
    }
    materialized["overnight_safe"] = signals_data.get("overnight_safe", {}).get("score")
    semantic_dsl = generate_spot_dsl(signals_data)
    total_observations = (row["total_observations"] if row else 0) + 1
    weight_support = float(row["weight_support"] if row else 0.0) + obs_weight
    consensus = min(1.0, weight_support / max(1.0, total_observations * 2.0))
    snapshot_baseline = _json_object(row["last_snapshot_data"]) if row else {}
    if not snapshot_baseline:
        snapshot_baseline = previous_signals
    distance = semantic_distance(snapshot_baseline if row else None, signals_data)
    next_snapshot_data = snapshot_baseline or signals_data
    if row and distance > 0.15:
        await conn.execute(
            """
            INSERT INTO spot_semantic_snapshots (spot_id, snapshot_date, semantic_data, trigger_reason, semantic_distance)
            VALUES ($1, CURRENT_DATE, $2::jsonb, 'semantic_shift', $3)
            ON CONFLICT (spot_id, snapshot_date) DO UPDATE SET
                semantic_data = EXCLUDED.semantic_data,
                semantic_distance = GREATEST(spot_semantic_snapshots.semantic_distance, EXCLUDED.semantic_distance)
            """,
            spot_id,
            json.dumps(previous_signals),
            distance,
        )
        next_snapshot_data = signals_data

    state = {
        "signals_data": signals_data,
        "semantic_dsl": semantic_dsl,
        "total_observations": total_observations,
        "weight_support": weight_support,
        "consensus_confidence": consensus,
        **materialized,
    }
    await conn.execute(
        """
        INSERT INTO spot_semantic_state (
            spot_id, quietness_score, safety_score, police_risk_score, beauty_score,
            crowd_level_score, overnight_safe, stealth_score, signals_data, semantic_dsl,
            total_observations, consensus_confidence, weight_support, last_snapshot_data,
            stale, updated_at, last_aggregated_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14::jsonb,
            FALSE, NOW(), NOW()
        )
        ON CONFLICT (spot_id) DO UPDATE SET
            quietness_score = EXCLUDED.quietness_score,
            safety_score = EXCLUDED.safety_score,
            police_risk_score = EXCLUDED.police_risk_score,
            beauty_score = EXCLUDED.beauty_score,
            crowd_level_score = EXCLUDED.crowd_level_score,
            overnight_safe = EXCLUDED.overnight_safe,
            stealth_score = EXCLUDED.stealth_score,
            signals_data = EXCLUDED.signals_data,
            semantic_dsl = EXCLUDED.semantic_dsl,
            total_observations = EXCLUDED.total_observations,
            consensus_confidence = EXCLUDED.consensus_confidence,
            weight_support = EXCLUDED.weight_support,
            last_snapshot_data = EXCLUDED.last_snapshot_data,
            stale = FALSE,
            updated_at = NOW(),
            last_aggregated_at = NOW()
        """,
        spot_id,
        state.get("quietness_score"),
        state.get("safety_score"),
        state.get("police_risk_score"),
        state.get("beauty_score"),
        state.get("crowd_level_score"),
        state.get("overnight_safe"),
        state.get("stealth_score"),
        json.dumps(signals_data),
        semantic_dsl,
        total_observations,
        consensus,
        weight_support,
        json.dumps(next_snapshot_data),
    )
    return state
