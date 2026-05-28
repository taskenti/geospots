"""Smoke test del decay por half-life + recency boost + gate condicional (T2.3).

Ejecutar:  python -m tests.test_signal_half_life
"""

import math
from datetime import datetime, timedelta, timezone

from enrichment.signal_registry import STATIC_SIGNALS
from enrichment.state_aggregator import (
    RECENCY_BOOST_ALPHA,
    RECENCY_BOOST_WINDOW_DAYS,
    decayed_weight,
    needs_recompute,
    observation_weight_at,
    recency_boost,
)


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def main() -> int:
    failures: list[str] = []
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # recency_boost: 1+α en t=0, decae a ~1.0.
    check(_approx(recency_boost(0.0), 1.0 + RECENCY_BOOST_ALPHA), "boost en t=0 debe ser 1+α")
    check(_approx(recency_boost(RECENCY_BOOST_WINDOW_DAYS),
                  1.0 + RECENCY_BOOST_ALPHA * math.exp(-1.0)),
          "boost en t=window debe ser 1+α/e")
    check(recency_boost(3650.0) < 1.001, "boost decae a ~1.0 para obs antiguas")
    check(recency_boost(-5.0) == 1.0 + RECENCY_BOOST_ALPHA, "edad negativa se trata como 0")

    # Half-life puro: a 1 half-life el peso es la mitad.
    hl = 90
    obs = now - timedelta(days=hl)
    check(_approx(decayed_weight(1.0, obs, hl, now), 0.5),
          "decayed_weight a 1 half-life debe ser 0.5")

    # observation_weight_at = decay × recency_boost.
    age = 30.0
    obs30 = now - timedelta(days=age)
    expected = decayed_weight(1.0, obs30, hl, now) * recency_boost(age)
    check(_approx(observation_weight_at(1.0, obs30, hl, now), expected),
          "observation_weight_at debe ser decay*boost")
    # Una obs reciente pesa MÁS que el decay puro (boost > 1).
    check(observation_weight_at(1.0, obs30, hl, now) > decayed_weight(1.0, obs30, hl, now),
          "recency boost debe elevar el peso de obs recientes")

    # Half-life real de algunas señales (granularidad ya existente en el registry).
    check(STATIC_SIGNALS["spot_closed"].half_life_days == 30, "spot_closed HL=30")
    check(STATIC_SIGNALS["beauty"].half_life_days >= 36500, "beauty HL persistente")
    check(STATIC_SIGNALS["police_risk"].half_life_days == 60, "police_risk HL=60")

    # needs_recompute: señal volátil + bastante tiempo → recompute.
    check(needs_recompute([30, 36500], 100.0) is True,
          "con una señal volátil (HL<elapsed) debe recomputar")
    # Solo señales persistentes recién agregadas → skip.
    check(needs_recompute([36500, 1825], 7.0) is False,
          "solo persistentes y poco tiempo → no recomputar")
    check(needs_recompute([], 1000.0) is False, "sin señales → no recomputar")
    check(needs_recompute([30], 0.0) is False, "elapsed 0 → no recomputar (lo maneja stale)")

    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK - half-life decay + recency boost + gate condicional correctos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
