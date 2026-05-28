"""Regresión Sprint 1 — fixes léxicos del extractor regex.

Cubre BUG-01/33 (lac), BUG-02 (fine), BUG-05 (safe/unsafe), BUG-13 (sale),
BUG-14 (polaridad wind), BUG-18 (lleno/relleno), BUG-19 (molestos),
BUG-20 (security), y seguro/inseguro.

Ejecutar:  python -m tests.test_lexical_sprint1
"""

from enrichment.claim_extractor import extract_claims_regex


def _has(claims, signal, value=None) -> bool:
    for c in claims:
        if c["signal"] == signal and (value is None or c["value"] == value):
            return True
    return False


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # ── FALSOS POSITIVOS que ya NO deben aparecer ────────────────────────────
    fp_cases = [
        # (texto, signal_prohibido, value_prohibido, descripción)
        ("a great place to park near the beach", "lake_nearby", "true", "BUG-01 place→lac"),
        ("nice spot but the area was a bit black", "lake_nearby", "true", "BUG-01 black→lac"),
        ("bel emplacement tranquille", "lake_nearby", "true", "BUG-01 emplacement→lac"),
        ("the area felt unsafe at night", "safety", "0.85", "BUG-05 unsafe→safe+"),
        ("zona muy insegura, no me gustó", "safety", "0.85", "seguro en inseguro"),
        ("rellenamos el deposito, relleno de agua disponible", "crowd_level", "0.85", "BUG-18 relleno→lleno"),
        ("el camino estaba lleno de baches", "crowd_level", "0.85", "BUG-18 lleno de baches"),
        ("everything was just fine, lovely spot", "police_risk", "0.85", "BUG-02 fine adjetivo"),
        ("charcos molestos tras la lluvia", "youth_trouble", "0.8", "BUG-19 molestos→charcos"),
        ("security was poor and lighting bad", "safety", "0.85", "BUG-20 security neutro"),
        ("prohibido bañarse en el rio", "police_risk", "0.85", "prohibido no policial"),
        ("the campsite is on sale this season", "cleanliness", "0.15", "BUG-13 on sale→sale"),
    ]
    for text, signal, value, desc in fp_cases:
        claims = extract_claims_regex(text)
        check(not _has(claims, signal, value),
              f"FP no eliminado [{desc}]: '{text}' → {signal}={value}")

    # BUG-14: "sheltered" debe dar BAJA exposición (0.1), nunca alta (0.85).
    claims = extract_claims_regex("lovely spot, well sheltered from the wind, very calm")
    check(_has(claims, "wind_exposure", "0.1"), "BUG-14: sheltered debe dar wind 0.1")
    check(not _has(claims, "wind_exposure", "0.85"), "BUG-14: sheltered NO debe dar wind 0.85")

    # ── VERDADEROS POSITIVOS que deben seguir funcionando ────────────────────
    tp_cases = [
        ("magnifique spot au bord du lac", "lake_nearby", "true", "lac real (FR)"),
        ("we felt very safe here all night", "safety", "0.85", "safe real"),
        ("estaba lleno de autocaravanas, muy concurrido", "crowd_level", "0.85", "lleno real"),
        ("nos multaron por aparcar de noche", "police_risk", "0.85", "multa real"),
        ("le sol était très sale partout", "cleanliness", "0.15", "sale FR real"),
        ("muy windy, viento intenso toda la noche", "wind_exposure", "0.85", "windy real"),
    ]
    for text, signal, value, desc in tp_cases:
        claims = extract_claims_regex(text)
        check(_has(claims, signal, value),
              f"TP perdido [{desc}]: '{text}' → falta {signal}={value}")

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK — todos los casos léxicos de Sprint 1 pasan")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
