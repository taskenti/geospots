"""Regresión Sprint 5 — precisión y reversibilidad de cierres.

Cubre:
  BUG-03  obra/construcción CERCANA ya no marca spot_closed (1.368 FP)
  BUG-08  cierre PARCIAL de un servicio (WC/restaurante/ducha) ≠ spot cerrado
  BUG-11  cierre obsoleto: si hay actividad posterior, se descarta el cierre

No toca DB: ejercita extract_claims_regex y aggregate_observations directamente.

Ejecutar:  python -m tests.test_closure_sprint5
"""

from datetime import datetime, timedelta, timezone

from enrichment.claim_extractor import extract_claims_regex
from enrichment.state_aggregator import aggregate_observations
from enrichment.ingest_v2 import _excerpt_grounded


def _has_closed(text: str) -> bool:
    return any(c["signal"] == "spot_closed" and str(c["value"]) == "true"
               for c in extract_claims_regex(text))


def _obs(signal, value_bool=None, value_num=None, days_ago=10, estimated=False):
    now = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
    return {
        "signal_type": signal,
        "observed_at": now - timedelta(days=days_ago),
        "observation_weight": 1.0,
        "value_bool": value_bool,
        "value_num": value_num,
        "value_text": None,
        "date_estimated": estimated,
    }


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # ── BUG-03: obra cercana NO es cierre ────────────────────────────────────
    check(not _has_closed("Hay obras al lado pero se puede aparcar bien."),
          "obras cercanas no deberían marcar spot_closed")
    check(not _has_closed("Some construction nearby, still a great spot."),
          "construction nearby no debería marcar spot_closed")
    # cierre permanente real SÍ se mantiene
    check(_has_closed("This spot is permanently closed, no longer exists."),
          "cierre permanente real debería seguir detectándose")
    check(_has_closed("El parking está cerrado, barrera cerrada."),
          "cierre real en español debería detectarse")

    # ── BUG-08: cierre parcial de servicio ≠ spot cerrado ────────────────────
    check(not _has_closed("Nice parking, but the toilets were closed."),
          "WC cerrado no debería marcar spot_closed")
    check(not _has_closed("Buen sitio, el restaurante cerrado en invierno."),
          "restaurante cerrado no debería marcar spot_closed")
    check(not _has_closed("Schöner Platz, aber die Dusche geschlossen."),
          "ducha cerrada (DE) no debería marcar spot_closed")
    check(not _has_closed("Bon endroit mais les toilettes fermées."),
          "toilettes fermées no debería marcar spot_closed")
    # cierre genérico real SÍ dispara
    check(_has_closed("The whole area is closed now."),
          "cierre genérico real debería seguir detectándose")

    # ── BUG-11: reapertura por actividad posterior ───────────────────────────
    # cierre hace 200 días, pero hay reviews (beauty) hace 5 días -> descartado
    rows_reopened = [
        _obs("spot_closed", value_bool=True, days_ago=200),
        _obs("beauty", value_num=0.9, days_ago=5),
        _obs("quietness", value_num=0.8, days_ago=3),
    ]
    agg = aggregate_observations(rows_reopened)
    check("spot_closed" not in agg["signals_data"],
          "cierre con actividad posterior debería descartarse (BUG-11)")
    check("beauty" in agg["signals_data"], "el resto de señales debe sobrevivir")

    # cierre RECIENTE (sin actividad posterior) -> se mantiene
    rows_closed = [
        _obs("spot_closed", value_bool=True, days_ago=5),
        _obs("beauty", value_num=0.9, days_ago=120),
    ]
    agg2 = aggregate_observations(rows_closed)
    check(agg2["signals_data"].get("spot_closed", {}).get("score") is True,
          "cierre más reciente que la última actividad debería mantenerse")

    # actividad posterior pero ESTIMADA (review sin fecha) -> NO reabre
    rows_estimated = [
        _obs("spot_closed", value_bool=True, days_ago=200),
        _obs("beauty", value_num=0.9, days_ago=5, estimated=True),
    ]
    agg3 = aggregate_observations(rows_estimated)
    check(agg3["signals_data"].get("spot_closed", {}).get("score") is True,
          "una review sin fecha real no debería reabrir un cierre")

    # ── BUG-23: anclaje del excerpt a la review citada ───────────────────────
    review_txt = ("Beautiful quiet spot by the lake, we stayed two nights. "
                  "The water point was working and the area was very clean.")
    # excerpt genuino (subconjunto de la review) -> anclado, se conserva
    check(_excerpt_grounded("the water point was working", review_txt),
          "un excerpt textual de la review debería considerarse anclado")
    # eco de STATIC_CONTEXT (texto que no aparece en la review) -> no anclado
    check(not _excerpt_grounded(
            "Open all year, free service area with electricity and pool", review_txt),
          "un eco que no aparece en la review debería descartarse (BUG-23)")
    # cita una review inexistente (texto None) -> no anclado
    check(not _excerpt_grounded("anything at all here", None),
          "un claim que cita una review inexistente no está anclado")
    # excerpt vacío -> no evaluable por texto, conservador, se conserva
    check(_excerpt_grounded("", review_txt),
          "un excerpt vacío no es evaluable por texto y debe conservarse")
    check(_excerpt_grounded(None, review_txt),
          "un excerpt None no es evaluable por texto y debe conservarse")
    # paráfrasis leve (mayoría de palabras significativas presentes) -> anclado
    check(_excerpt_grounded("quiet spot lake clean water", review_txt),
          "una paráfrasis con solapamiento alto debería considerarse anclada")

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK -- todos los casos de cierre de Sprint 5 pasan")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
