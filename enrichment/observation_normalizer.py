"""Normalize extracted claims into typed observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .signal_registry import STATIC_SIGNALS, SignalType


@dataclass(frozen=True)
class NormalizedObservation:
    signal_type: str
    value_num: float | None
    value_bool: bool | None
    value_text: str | None
    extraction_confidence: float
    source_confidence: float
    reviewer_confidence: float
    observation_weight: float
    observed_at: datetime


TRUE_VALUES = {"true", "t", "yes", "y", "si", "sí", "1", "ok", "allowed", "possible"}
FALSE_VALUES = {"false", "f", "no", "n", "0", "not", "forbidden", "impossible"}


def _observed_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _as_bool(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return None


def _as_num(raw: Any) -> float | None:
    try:
        value = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        mapping = {
            "low": 0.2,
            "bajo": 0.2,
            "medio": 0.5,
            "medium": 0.5,
            "alto": 0.8,
            "high": 0.8,
            "yes": 1.0,
            "no": 0.0,
        }
        value = mapping.get(str(raw).strip().lower())
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def normalize_claim(
    claim: dict,
    source_confidence: float = 1.0,
    reviewer_confidence: float = 1.0,
    observed_at: Any = None,
    signal_types: dict[str, SignalType] | None = None,
) -> NormalizedObservation | None:
    signal_types = signal_types or STATIC_SIGNALS
    signal = claim.get("signal") or claim.get("signal_type")
    stype = signal_types.get(signal)
    if not signal or not stype:
        return None

    raw = claim.get("value", claim.get("raw_value"))
    value_num = value_bool = value_text = None
    if stype.value_type == "boolean":
        value_bool = _as_bool(raw)
        if value_bool is None:
            return None
    elif stype.value_type == "numeric":
        value_num = _as_num(raw)
        if value_num is None:
            return None
    else:
        value_text = str(raw).strip() if raw is not None else None
        if not value_text:
            return None

    extraction_confidence = max(0.0, min(1.0, float(claim.get("confidence", claim.get("extraction_confidence", 1.0)))))
    source_confidence = max(0.0, min(1.0, float(source_confidence or 1.0)))
    reviewer_confidence = max(0.0, min(1.0, float(reviewer_confidence or 1.0)))
    weight = extraction_confidence * source_confidence * reviewer_confidence
    return NormalizedObservation(
        signal_type=signal,
        value_num=value_num,
        value_bool=value_bool,
        value_text=value_text,
        extraction_confidence=extraction_confidence,
        source_confidence=source_confidence,
        reviewer_confidence=reviewer_confidence,
        observation_weight=weight,
        observed_at=_observed_at(observed_at),
    )


def normalize_claims(
    claims: list[dict],
    source_confidence: float = 1.0,
    reviewer_confidence: float = 1.0,
    observed_at: Any = None,
    signal_types: dict[str, SignalType] | None = None,
) -> list[NormalizedObservation]:
    observations = []
    for claim in claims:
        obs = normalize_claim(claim, source_confidence, reviewer_confidence, observed_at, signal_types)
        if obs:
            observations.append(obs)
    return observations
