"""Materialización de columnas v2 desde observations.

Las columnas score_* las llena `state_aggregator.aggregate_observations` (las
calcula a partir del aggregate JSON). Las columnas v2 categóricas / array
las construimos aquí desde el detalle de observations (porque text-signals
no tienen un único "score" agregable).

Columnas v2 materializadas:
  - noise_sources TEXT[]: distinct value_text con peso decay > umbral
  - parking_capacity TEXT: value_text más reciente
  - cell_coverage REAL: ya lo cubre aggregate_observations (numeric)
  - wild_camping_legal BOOLEAN: ya lo cubre aggregate_observations (boolean)
  - last_observation_at TIMESTAMPTZ: MAX(observed_at) del spot
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable

NOISE_SOURCE_DECAY_HALF_LIFE = 180  # mismo que signal_registry
NOISE_SOURCE_MIN_WEIGHT = 0.2       # umbral para considerar la fuente "activa"

PARKING_CAPACITY_MAX_AGE_DAYS = 365 * 5  # 5 años — capacity rara vez cambia


def _decayed_weight(weight: float, observed_at: datetime, half_life_days: int,
                    now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
    if half_life_days <= 0:
        return weight
    return float(weight) * math.pow(0.5, age_days / half_life_days)


def aggregate_noise_sources(observations: Iterable[dict],
                            min_weight: float = NOISE_SOURCE_MIN_WEIGHT) -> list[str]:
    """De observations con signal_type='noise_source', devuelve sources únicas
    cuyo peso decayed acumulado supere `min_weight`.

    Cada observation aporta value_text (la fuente: highway/train/...).
    """
    bucket: dict[str, float] = {}
    for obs in observations:
        if obs.get("signal_type") != "noise_source":
            continue
        value = (obs.get("value_text") or "").strip().lower()
        if not value:
            continue
        weight = _decayed_weight(
            float(obs.get("observation_weight") or 0.0),
            obs["observed_at"],
            NOISE_SOURCE_DECAY_HALF_LIFE,
        )
        bucket[value] = bucket.get(value, 0.0) + weight

    sources = [src for src, w in bucket.items() if w >= min_weight]
    # Orden estable por peso descendente
    sources.sort(key=lambda s: bucket[s], reverse=True)
    return sources


def aggregate_parking_capacity(observations: Iterable[dict],
                               max_age_days: int = PARKING_CAPACITY_MAX_AGE_DAYS) -> str | None:
    """Toma la observación más reciente de parking_capacity dentro de la
    ventana de validez. `recent_wins` strategy.
    """
    now = datetime.now(timezone.utc)
    candidates = []
    for obs in observations:
        if obs.get("signal_type") != "parking_capacity":
            continue
        value = (obs.get("value_text") or "").strip().lower()
        if value not in ("small", "medium", "large"):
            continue
        observed_at = obs["observed_at"]
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age_days = (now - observed_at).total_seconds() / 86400.0
        if age_days > max_age_days:
            continue
        candidates.append((observed_at, float(obs.get("observation_weight") or 0.0), value))

    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return candidates[0][2]


async def compute_last_observation_at(conn, spot_id: int) -> datetime | None:
    """Fecha de la review más reciente del spot. Indica vitalidad del spot.

    Excluye claims/observations de "description" (observed_at = NOW por construcción),
    porque esos no son señal de actividad real. Si el spot no tiene reviews con fecha,
    cae al MAX(observed_at) de observations vinculadas a alguna review.
    """
    row = await conn.fetchrow(
        """
        SELECT MAX(r.fecha) AS last_review_at,
               MAX(no.observed_at) FILTER (WHERE ec.review_id IS NOT NULL) AS last_obs_at
        FROM normalized_observations no
        JOIN extracted_claims ec ON ec.id = no.claim_id
        LEFT JOIN reviews r ON r.id = ec.review_id
        WHERE no.spot_id = $1
        """,
        spot_id,
    )
    if not row:
        return None
    if row["last_review_at"]:
        d = row["last_review_at"]
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if not hasattr(d, "hour") else d
    return row["last_obs_at"]
