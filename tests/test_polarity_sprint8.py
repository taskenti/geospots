"""Regresion Sprint 8 -- Opcion B: senales de polaridad ambigua.

Contexto (reportado por el usuario 2026-05-29):
  "Parfait, superbe et propre. La police fait quelques rondes."
  El regex emitia police_risk=0.85 (riesgo) cuando las rondas policiales son un
  indicador POSITIVO de seguridad. El regex es ciego a la polaridad contextual.

Opcion B implementada:
  - police_risk NO se emite por regex (esta en _AMBIGUOUS_POLARITY_SIGNALS). Su
    sola mencion fuerza el escalado al LLM via text_mentions_ambiguous_signal().
  - overnight_safe se resuelve por polaridad dentro del regex: una prohibicion
    explicita (false) anula la mencion positiva (true) de la misma review
    (cierra el residual de NEW-BUG-A a coste cero).

No toca DB: ejercita extract_claims_regex y text_mentions_ambiguous_signal
directamente.

Ejecutar:  python -m tests.test_polarity_sprint8
"""

from enrichment.claim_extractor import (
    extract_claims_regex,
    text_mentions_ambiguous_signal,
)


def _sigs(text: str) -> set[tuple[str, str]]:
    return {(c["signal"], c["value"]) for c in extract_claims_regex(text)}


def _has_signal(text: str, signal: str) -> bool:
    return any(c["signal"] == signal for c in extract_claims_regex(text))


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # ── police_risk: NUNCA se emite por regex (polaridad ambigua) ─────────────
    patrol = "Parfait, superbe et propre. La police fait quelques rondes."
    check(not _has_signal(patrol, "police_risk"),
          "rondas policiales NO deben emitir police_risk por regex (caso usuario)")
    check(text_mentions_ambiguous_signal(patrol),
          "rondas policiales SI deben marcar mencion ambigua -> escalar a LLM")
    # las demas senales del mismo texto siguen funcionando
    check(("beauty", "0.9") in _sigs(patrol),
          "beauty debe seguir extrayendose del texto de la policia")
    check(("cleanliness", "0.85") in _sigs(patrol),
          "cleanliness debe seguir extrayendose del texto de la policia")

    # multa REAL: tampoco se emite por regex, pero marca mencion -> LLM decide
    fined = "Nos multaron por aparcar aqui, vino la policia y nos echaron."
    check(not _has_signal(fined, "police_risk"),
          "multa real tampoco se emite por regex (la polaridad la decide el LLM)")
    check(text_mentions_ambiguous_signal(fined),
          "multa real debe marcar mencion ambigua -> escalar a LLM")

    # ingles: police fine
    fined_en = "We got a parking fine here, police came at night and fined us."
    check(not _has_signal(fined_en, "police_risk"),
          "police fine EN no se emite por regex")
    check(text_mentions_ambiguous_signal(fined_en),
          "police fine EN marca mencion ambigua")

    # texto sin policia: NO marca mencion ambigua
    neutral = "Beautiful quiet spot by the lake, very clean, water point working."
    check(not text_mentions_ambiguous_signal(neutral),
          "texto sin policia NO debe marcar mencion ambigua (no escala de mas)")
    check(not _has_signal(neutral, "police_risk"),
          "texto neutral no debe tener police_risk")

    # ── overnight_safe: resolucion de polaridad (false gana a true) ───────────
    prohibited = "Overnight parking is prohibited here, we got moved on."
    osigs = {v for (s, v) in _sigs(prohibited) if s == "overnight_safe"}
    check(osigs == {"false"},
          f"overnight prohibido debe dar SOLO false, no {{true,false}}; dio {osigs}")

    prohibited_es = "Aqui pernoctar esta prohibido, nos hicieron marchar de noche."
    osigs_es = {v for (s, v) in _sigs(prohibited_es) if s == "overnight_safe"}
    check("true" not in osigs_es,
          f"pernoctar prohibido (ES) no debe dar overnight_safe=true; dio {osigs_es}")

    # overnight positivo normal: SIGUE emitiendo true (no romper el caso comun)
    ok = "We slept here overnight, very quiet and clean, no problem at all."
    check(("overnight_safe", "true") in _sigs(ok),
          "overnight positivo normal debe seguir emitiendo true")
    check(("overnight_safe", "false") not in _sigs(ok),
          "overnight positivo normal NO debe emitir false")

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK -- todos los casos de polaridad de Sprint 8 (Opcion B) pasan")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
