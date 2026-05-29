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
    # Sprint 3 (BUG-10/17/22/31): True cuando la fecha NO es una fecha de
    # publicación real (review sin fecha, hecho scrapeado anclado al timestamp
    # de ingesta, o fecha futura saneada). El agregador NO aplica recency boost
    # a estas observaciones y su peso lleva penalización — así no "ganan" por
    # parecer frescas frente a evidencia datada real.
    date_estimated: bool = False


TRUE_VALUES = {"true", "t", "yes", "y", "si", "sí", "1", "ok", "allowed", "possible"}
FALSE_VALUES = {"false", "f", "no", "n", "0", "not", "forbidden", "impossible"}

# Tolerancia de reloj antes de considerar una fecha "futura" (BUG-31).
FUTURE_SKEW_DAYS = 2
# Factor de penalización de peso para observaciones con fecha estimada/saneada.
DATE_ESTIMATED_WEIGHT_FACTOR = 0.6


def _resolve_observed_at(value: Any, now: datetime | None = None) -> tuple[datetime, bool]:
    """Devuelve (observed_at, date_estimated).

    - None / tipo no fecha → (now, True): fecha desconocida, no es fresca de verdad.
    - fecha futura (> now + skew) → (now, True): error de parseo del scraper
      (BUG-31: reviews hasta 2033). Se clampa a now y se marca estimada para que
      no reciba recency boost ni domine.
    - fecha válida pasada/presente → (fecha, False).
    """
    now = now or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        return now, True
    if (dt - now).total_seconds() > FUTURE_SKEW_DAYS * 86400.0:
        return now, True
    return dt, False


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
    date_estimated: bool = False,
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

    resolved_at, resolved_estimated = _resolve_observed_at(observed_at)
    estimated = bool(date_estimated or resolved_estimated)
    if estimated:
        weight *= DATE_ESTIMATED_WEIGHT_FACTOR

    return NormalizedObservation(
        signal_type=signal,
        value_num=value_num,
        value_bool=value_bool,
        value_text=value_text,
        extraction_confidence=extraction_confidence,
        source_confidence=source_confidence,
        reviewer_confidence=reviewer_confidence,
        observation_weight=weight,
        observed_at=resolved_at,
        date_estimated=estimated,
    )


def normalize_claims(
    claims: list[dict],
    source_confidence: float = 1.0,
    reviewer_confidence: float = 1.0,
    observed_at: Any = None,
    signal_types: dict[str, SignalType] | None = None,
    date_estimated: bool = False,
) -> list[NormalizedObservation]:
    observations = []
    for claim in claims:
        obs = normalize_claim(claim, source_confidence, reviewer_confidence,
                              observed_at, signal_types, date_estimated)
        if obs:
            observations.append(obs)
    return observations
