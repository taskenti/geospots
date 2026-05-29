"""Regresión Sprint 3 — integridad temporal.

Cubre BUG-10/17 (review sin fecha -> observed_at=now pero marcada estimada),
BUG-22 (scraped_facts last_seen no es fecha de publicación),
BUG-31 (fechas futuras saneadas, sin recency boost),
BUG-07-adyacente (recency boost solo para fechas reales pasadas).

No toca DB: ejercita normalizer.normalize_claim/_resolve_observed_at y
aggregator.observation_weight_at directamente.

Ejecutar:  python -m tests.test_temporal_sprint3
"""

from datetime import date, datetime, timedelta, timezone

from enrichment.state_resolver import compute_decayed_confidence
from enrichment.observation_normalizer import (
    DATE_ESTIMATED_WEIGHT_FACTOR,
    FUTURE_SKEW_DAYS,
    _resolve_observed_at,
    normalize_claim,
)
from enrichment.state_aggregator import observation_weight_at, recency_boost


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    now = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)

    # ── _resolve_observed_at ─────────────────────────────────────────────────
    # None -> (now, True): fecha desconocida, no es fresca de verdad (BUG-10/17)
    dt, est = _resolve_observed_at(None, now=now)
    check(dt == now and est is True, f"None debería dar (now, True), dio ({dt}, {est})")

    # tipo no-fecha -> estimada
    dt, est = _resolve_observed_at("no soy fecha", now=now)
    check(est is True, "string no-fecha debería marcarse estimada")

    # fecha futura más allá del skew -> clamp a now + estimada (BUG-31)
    future = now + timedelta(days=FUTURE_SKEW_DAYS + 30)
    dt, est = _resolve_observed_at(future, now=now)
    check(dt == now and est is True,
          f"fecha futura debería clamp a now + estimada, dio ({dt}, {est})")

    # fecha pasada válida -> (fecha, False)
    past = now - timedelta(days=100)
    dt, est = _resolve_observed_at(past, now=now)
    check(dt == past and est is False,
          f"fecha pasada válida debería ser real, dio ({dt}, {est})")

    # dentro del skew (reloj ligeramente adelantado) -> NO estimada
    near_future = now + timedelta(days=FUTURE_SKEW_DAYS - 1)
    dt, est = _resolve_observed_at(near_future, now=now)
    check(est is False, "fecha dentro del skew no debería marcarse estimada")

    # ── normalize_claim: penalización de peso para fechas estimadas ──────────
    claim = {"signal": "quietness", "value": "0.8", "confidence": 1.0}

    obs_real = normalize_claim(claim, source_confidence=1.0, reviewer_confidence=1.0,
                               observed_at=past)
    obs_dateless = normalize_claim(claim, source_confidence=1.0, reviewer_confidence=1.0,
                                   observed_at=None)
    check(obs_real is not None and obs_dateless is not None, "claims deberían normalizar")
    check(obs_real.date_estimated is False, "obs con fecha real no debería ser estimada")
    check(obs_dateless.date_estimated is True, "obs sin fecha debería ser estimada")
    # el peso de la dateless es el real * factor de penalización
    check(abs(obs_dateless.observation_weight
              - obs_real.observation_weight * DATE_ESTIMATED_WEIGHT_FACTOR) < 1e-9,
          "obs estimada debería llevar penalización de peso DATE_ESTIMATED_WEIGHT_FACTOR")

    # date_estimated explícito (scraped_facts, BUG-22) fuerza la marca aunque la fecha sea válida
    obs_scraped = normalize_claim(claim, source_confidence=1.0, reviewer_confidence=1.0,
                                  observed_at=past, date_estimated=True)
    check(obs_scraped.date_estimated is True,
          "date_estimated=True explícito (scraped) debería forzar la marca")

    # ── observation_weight_at: recency boost solo para fechas reales ─────────
    hl = 60  # señal fast-decay (agua/elec/ducha)
    recent = now - timedelta(days=5)

    # fecha real reciente -> recibe recency boost (>1x sobre el decaído puro)
    w_real = observation_weight_at(1.0, recent, hl, now=now, date_estimated=False)
    w_no_boost = observation_weight_at(1.0, recent, hl, now=now, date_estimated=True)
    check(w_real > w_no_boost,
          f"fecha real reciente debería pesar más que la estimada ({w_real} vs {w_no_boost})")
    # el boost neutro debe equivaler al decaído sin boost
    expected_boost_ratio = recency_boost(5.0)
    check(expected_boost_ratio > 1.0, "recency_boost de 5 días debería ser > 1")
    check(abs(w_real - w_no_boost * expected_boost_ratio) < 1e-9,
          "la diferencia real/estimada debería ser exactamente el factor recency_boost")

    # fecha futura (date_estimated=False pero observed_at>now) -> sin boost igualmente (BUG-31)
    # (defensa por si una fila antigua escapó al clamp del normalizer)
    fut = now + timedelta(days=10)
    w_future = observation_weight_at(1.0, fut, hl, now=now, date_estimated=False)
    w_future_est = observation_weight_at(1.0, fut, hl, now=now, date_estimated=True)
    check(abs(w_future - w_future_est) < 1e-9,
          "fecha futura no debería recibir recency boost aunque no esté marcada estimada")

    # ── BUG-07: decay de alertas anclado a valid_from, no a detected_at ──────
    detected = now - timedelta(days=9)        # ingestada hace poco
    # Evento real de 2015 ingestado en 2026: con el ancla buggy (detected_at)
    # apenas decae; con valid_from arranca con >100 meses de decay y se resuelve.
    conf_buggy, _ = compute_decayed_confidence(0.9, None, detected, now)
    conf_fixed, months_fixed = compute_decayed_confidence(
        0.9, None, detected, now, valid_from=date(2015, 6, 1))
    check(conf_buggy > 0.8,
          "sanity: con ancla detected_at un evento recién ingestado casi no decae")
    check(conf_fixed < 0.05 and months_fixed > 120,
          f"BUG-07: evento de 2015 debería estar casi resuelto, dio conf={conf_fixed}")
    # Idempotencia incremental: last_decay_at tiene precedencia sobre valid_from
    last_decay = now - timedelta(days=30)
    _, months_incr = compute_decayed_confidence(
        0.5, last_decay, detected, now, valid_from=date(2015, 6, 1))
    check(months_incr < 2.0,
          f"decay incremental debería contar solo desde last_decay_at, dio {months_incr} meses")

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK — todos los casos temporales de Sprint 3 pasan")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
