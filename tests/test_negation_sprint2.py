"""Regresión Sprint 2 — negación separada multi-idioma.

Cubre BUG-04 (overnight prohibitivo), BUG-15 (no hay agua),
BUG-28 (nicht ruhig), BUG-29 (not clean / no me sentí seguro).
La negación por prefijo (unruhig/unsafe/unsicher) ya la cubre Sprint 1.

Ejecutar:  python -m tests.test_negation_sprint2
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

    # ── FP afirmativo suprimido por negación previa ──────────────────────────
    suppress = [
        ("aquí no hay agua potable", "water_working", "true", "BUG-15 no hay agua"),
        ("el sitio nicht ruhig por la noche", "quietness", "0.9", "BUG-28 nicht ruhig"),
        ("prohibido dormir en la zona", "overnight_safe", "true", "BUG-04 prohibido dormir"),
        ("no se puede dormir aquí", "overnight_safe", "true", "BUG-04 no se puede dormir"),
        ("the toilets were not clean at all", "cleanliness", "0.85", "BUG-29 not clean"),
        ("no me sentí seguro en este sitio", "safety", "0.85", "BUG-29 no...seguro"),
        ("on ne peut pas dormir ici", "overnight_safe", "true", "FR ne...pas dormir"),
    ]
    for text, signal, value, desc in suppress:
        claims = extract_claims_regex(text)
        check(not _has(claims, signal, value),
              f"negación no suprimió [{desc}]: '{text}' -> {signal}={value}")

    # ── La polaridad negativa SÍ se captura (no solo se pierde) ──────────────
    capture = [
        ("aquí no hay agua potable", "water_working", "false", "captura no hay agua"),
        ("el sitio nicht ruhig por la noche", "quietness", "0.2", "captura nicht ruhig"),
        ("prohibido dormir en la zona", "overnight_safe", "false", "captura prohibido dormir"),
        ("the toilets were not clean at all", "cleanliness", "0.15", "captura not clean"),
        ("no me sentí seguro en este sitio", "safety", "0.2", "captura inseguro"),
    ]
    for text, signal, value, desc in capture:
        claims = extract_claims_regex(text)
        check(_has(claims, signal, value),
              f"no capturó polaridad negativa [{desc}]: '{text}' -> falta {signal}={value}")

    # ── La negación NO debe cruzar puntuación ni sobre-suprimir ──────────────
    no_oversuppress = [
        # "no noise" es quietud positiva; la coma corta la negación a "very quiet"
        ("no noise at all, very quiet and peaceful", "quietness", "0.9", "coma corta negación"),
        # afirmación limpia sin negación previa
        ("we slept here, felt very safe all night", "safety", "0.85", "safe sin negación"),
        ("agua potable disponible y limpia", "water_working", "true", "agua sin negación"),
    ]
    for text, signal, value, desc in no_oversuppress:
        claims = extract_claims_regex(text)
        check(_has(claims, signal, value),
              f"sobre-supresión [{desc}]: '{text}' -> falta {signal}={value}")

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK — todos los casos de negación de Sprint 2 pasan")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
