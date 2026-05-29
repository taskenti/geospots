"""Regresión Sprint 4 — agregación y confianza.

Cubre:
  BUG-06  confianza booleana = acuerdo × volumen (1 sola obs no da 1.0)
  BUG-25  consenso global monótono creciente con el peso total
  BUG-16  señales de texto (noise_source/parking_capacity) entran a signals_data y DSL
  BUG-27  wind_exposure se invierte en el DSL (alto=peor -> shelter, alto=mejor)
  BUG-35  raw_value booleano se normaliza a "true"/"false" (no "True"/"False")
  BUG-36  water_working=false se suprime con contexto de helada estacional

No toca DB: ejercita las funciones puras directamente.

Ejecutar:  python -m tests.test_aggregation_sprint4
"""

from datetime import datetime, timedelta, timezone

from enrichment.state_aggregator import (
    aggregate_observations,
    boolean_confidence,
    consensus_confidence,
)
from enrichment.dsl_generator import generate_spot_dsl
from enrichment.claim_extractor import extract_claims_regex


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    now = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
    recent = now - timedelta(days=10)

    # ── BUG-06: boolean_confidence ───────────────────────────────────────────
    # 1 sola observación (support≈1) NO debe dar confianza máxima
    c1 = boolean_confidence(1.0, 0.0)
    check(c1 < 1.0, f"1 sola obs no debería dar confianza 1.0, dio {c1}")
    # 3 observaciones unánimes -> confianza saturada a 1.0
    c3 = boolean_confidence(3.0, 0.0)
    check(abs(c3 - 1.0) < 1e-9, f"3 obs unánimes deberían dar 1.0, dio {c3}")
    # empate -> 0
    check(boolean_confidence(2.0, 2.0) == 0.0, "empate debería dar confianza 0")
    # más volumen con mismo acuerdo -> más confianza
    check(boolean_confidence(2.0, 0.0) > boolean_confidence(1.0, 0.0),
          "más observaciones concordantes deberían subir la confianza")
    check(boolean_confidence(0.0, 0.0) == 0.0, "sin soporte -> 0")

    # ── BUG-25: consensus_confidence monótono ────────────────────────────────
    check(consensus_confidence(0.0) == 0.0, "peso 0 -> consenso 0")
    seq = [consensus_confidence(w) for w in (1, 3, 6, 12, 30, 100)]
    check(all(b >= a for a, b in zip(seq, seq[1:])),
          f"consenso debería ser monótono creciente, dio {seq}")
    check(seq[-1] <= 1.0, "consenso debe saturar a 1.0")
    # más evidencia NUNCA debe bajar el consenso (el bug viejo dividía por n_obs)
    check(consensus_confidence(20.0) >= consensus_confidence(12.0),
          "añadir peso no debería reducir el consenso")

    # ── BUG-16: señales de texto en signals_data + DSL ───────────────────────
    text_rows = [
        {"signal_type": "noise_source", "observed_at": recent,
         "observation_weight": 1.0, "value_bool": None, "value_num": None,
         "value_text": "road", "date_estimated": False},
        {"signal_type": "noise_source", "observed_at": recent,
         "observation_weight": 1.0, "value_bool": None, "value_num": None,
         "value_text": "road", "date_estimated": False},
        {"signal_type": "noise_source", "observed_at": recent,
         "observation_weight": 0.5, "value_bool": None, "value_num": None,
         "value_text": "train", "date_estimated": False},
    ]
    agg = aggregate_observations(text_rows)
    check("noise_source" in agg["signals_data"],
          "señal de texto debería aparecer en signals_data (BUG-16)")
    check(agg["signals_data"]["noise_source"]["score"] == "road",
          f"moda ponderada debería ser 'road', dio {agg['signals_data'].get('noise_source')}")
    check("noisesrc:road" in agg["semantic_dsl"],
          f"texto debería entrar al DSL, dio {agg['semantic_dsl']}")

    # ── BUG-27 + BUG-POLARITY: polaridad unificada del DSL ────────────────────
    # Modelo: signo = bueno(+)/malo(-), magnitud = 2*|valor-0.5| (centrado en 0.5).
    # wind alto = MUY expuesto (malo) -> shelter negativo, magnitud alta
    dsl_windy = generate_spot_dsl({"wind_exposure": {"score": 0.9}})
    check("shelter:-0.8" in dsl_windy,
          f"wind alto debería dar shelter muy negativo, dio {dsl_windy}")
    dsl_calm = generate_spot_dsl({"wind_exposure": {"score": 0.1}})
    check("shelter:+0.8" in dsl_calm,
          f"wind bajo debería dar shelter muy positivo, dio {dsl_calm}")
    # consistencia de polaridad con quietness (alto=mejor)
    dsl_quiet = generate_spot_dsl({"quietness": {"score": 0.9}})
    check("quiet:+0.8" in dsl_quiet, f"quietness alto -> +, dio {dsl_quiet}")

    # Señales de riesgo/molestia "alto=peor": riesgo alto debe leerse NEGATIVO.
    dsl_theft = generate_spot_dsl({"theft_risk": {"score": 1.0}})
    check("theft:-1.0" in dsl_theft,
          f"theft_risk máximo debería ser muy negativo, dio {dsl_theft}")
    dsl_safe = generate_spot_dsl({"theft_risk": {"score": 0.0}})
    check("theft:+1.0" in dsl_safe,
          f"sin theft_risk debería ser muy positivo, dio {dsl_safe}")
    dsl_noisy = generate_spot_dsl({"noise": {"score": 0.7}})
    check("noise:-0.4" in dsl_noisy,
          f"noise alto debería ser negativo, dio {dsl_noisy}")
    dsl_police = generate_spot_dsl({"police_risk": {"score": 0.8}})
    check("police:-0.6" in dsl_police,
          f"police_risk alto debería ser negativo, dio {dsl_police}")

    # ── BUG-35: normalización de raw_value booleano ──────────────────────────
    # (réplica de la expresión inline de ingest_v2._insert_claim)
    def _raw(value):
        return "true" if value is True else "false" if value is False else str(value)
    check(_raw(True) == "true" and _raw(False) == "false",
          "bool de Python debería serializarse en minúsculas")
    check(_raw(0.8) == "0.8" and _raw("road") == "road",
          "valores no booleanos no deberían tocarse")

    # ── BUG-36: helada suprime water_working=false ───────────────────────────
    claims_freeze = extract_claims_regex(
        "El agua no funciona porque está cortada por la helada de estos días.")
    has_water_false = any(c["signal"] == "water_working" and str(c["value"]) == "false"
                          for c in claims_freeze)
    check(not has_water_false,
          f"helada estacional no debería emitir water_working=false, dio {claims_freeze}")
    # sin contexto de helada, sí se emite
    claims_real = extract_claims_regex("El agua no funciona, grifo seco todo el verano.")
    has_water_false_real = any(c["signal"] == "water_working" and str(c["value"]) == "false"
                               for c in claims_real)
    check(has_water_false_real,
          f"sin helada, agua no funciona SÍ debería emitirse, dio {claims_real}")

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK -- todos los casos de agregacion de Sprint 4 pasan")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
