"""Smoke test del léxico multilingüe ponderado (T2.1 / D5+D6).

Ejecutar:  python -m tests.test_multilingual_lexicon
"""

from enrichment.multilingual_lexicon import (
    D6_BLEND_LLM_WEIGHT,
    LEXICON,
    LEXICON_BLEND_WEIGHT,
    apply_lexicon_blend,
    blend_confidence,
    covered_signals,
    lexical_prior,
)


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # D6: pesos suman 1.
    check(_approx(LEXICON_BLEND_WEIGHT + D6_BLEND_LLM_WEIGHT, 1.0),
          "los pesos del blend deben sumar 1.0")
    check(_approx(LEXICON_BLEND_WEIGHT, 0.3), "lexicon weight debe ser 0.3 (D6)")

    # D5: 5 conceptos -> 4 señales reales (construction y closure mapean ambas a spot_closed).
    sigs = covered_signals()
    for s in ("spot_closed", "noise", "police_risk", "wild_camping_legal"):
        check(s in sigs, f"señal {s} debe estar cubierta por el léxico")

    # ~150 entradas en total.
    total_terms = sum(len(t) for t in LEXICON.values())
    check(total_terms >= 130, f"esperaba >=130 entradas léxicas, hay {total_terms}")

    # Acento-insensible: 'fermé' / 'ferme' deben matchear igual.
    p1 = lexical_prior("le spot est fermé définitivement", "spot_closed", "true")
    p2 = lexical_prior("le spot est ferme definitivement", "spot_closed", "true")
    check(p1 is not None and p2 is not None and _approx(p1, p2),
          f"matching debe ser acento-insensible (got {p1} vs {p2})")

    # NL 'bouwput' es el término más fuerte (0.95).
    pnl = lexical_prior("er was een bouwput naast de parking", "spot_closed", "true")
    check(pnl is not None and _approx(pnl, 0.95), f"bouwput prior debe ser 0.95, got {pnl}")

    # DE 'Baustelle' detectado pese a mayúscula y sin diacrítico.
    pde = lexical_prior("Direkt eine Baustelle daneben", "spot_closed", "true")
    check(pde is not None and pde >= 0.9, f"Baustelle prior debe ser >=0.9, got {pde}")

    # Sin término cargado -> prior None -> blend devuelve llm_score intacto.
    check(lexical_prior("lovely quiet beach", "spot_closed", "true") is None,
          "texto sin término de cierre no debe dar prior")
    check(_approx(blend_confidence("lovely quiet beach", "spot_closed", "true", 0.42), 0.42),
          "sin prior, blend_confidence devuelve llm_score sin tocar")

    # Blend numérico: 0.7*0.5 + 0.3*0.95 = 0.35 + 0.285 = 0.635
    b = blend_confidence("hay un bouwput al lado", "spot_closed", "true", 0.5)
    check(_approx(b, 0.635), f"blend esperado 0.635, got {b}")

    # Polaridad de wild_camping: prohibido vs permitido son señales/valores distintos.
    pf = lexical_prior("camping sauvage interdit ici", "wild_camping_legal", "false")
    pt = lexical_prior("camping sauvage interdit ici", "wild_camping_legal", "true")
    check(pf is not None and pt is None,
          f"'interdit' debe disparar value=false, no true (false={pf}, true={pt})")

    # apply_lexicon_blend muta solo los claims que matchean y los anota.
    claims = [
        {"signal": "spot_closed", "value": "true", "confidence": 0.5, "excerpt": "x"},
        {"signal": "beauty", "value": "0.9", "confidence": 0.8, "excerpt": "y"},
    ]
    out = apply_lexicon_blend("zona en obras, todo cerrado", claims)
    closed = next(c for c in out if c["signal"] == "spot_closed")
    beauty = next(c for c in out if c["signal"] == "beauty")
    check(closed.get("lexicon_blended") is True, "claim de cierre debe marcarse blended")
    check(closed["confidence"] != 0.5, "confidence de cierre debe haber cambiado")
    check("lexicon_blended" not in beauty, "claim sin match no debe marcarse")
    check(_approx(beauty["confidence"], 0.8), "claim sin match no debe cambiar confidence")

    # Idempotencia del flag: el blend está pensado para aplicarse una sola vez,
    # pero verificamos que valores quedan en [0,1].
    for c in out:
        check(0.0 <= c["confidence"] <= 1.0, "confidence fuera de rango tras blend")

    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK - {total_terms} entradas, {len(sigs)} senales cubiertas, todos los asserts pasan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
