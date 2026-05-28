"""Smoke test de suggest_canonical (T2.4 — pista difusa para revisión mensual).

Ejecutar:  python -m tests.test_unknown_tag_suggest
"""

from enrichment.tag_canonicalizer import suggest_canonical

# Índice simulado {alias|canonical normalizado -> canonical_id}
INDEX = {
    "mountain": "mountain",
    "mountain-view": "mountain",
    "overnighting": "overnighting",
    "quiet": "quiet",
    "sea-view": "sea-view",
    "construction": "construction",
}


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # Typo cercano -> sugiere el canonical.
    check(suggest_canonical("quieet", INDEX) == "quiet", "typo 'quieet' -> quiet")
    check(suggest_canonical("constructio", INDEX) == "construction",
          "typo 'constructio' -> construction")

    # Variante que ya es alias resuelve al canonical destino.
    check(suggest_canonical("mountain-view", INDEX) == "mountain",
          "alias exacto -> canonical destino")

    # Algo totalmente distinto -> None (no fuerza match).
    check(suggest_canonical("xyzptlk", INDEX) is None, "tag sin parecido -> None")
    check(suggest_canonical("", INDEX) is None, "tag vacío -> None")
    check(suggest_canonical("quiet", {}) is None, "índice vacío -> None")

    # Cutoff alto evita falsos matches débiles.
    check(suggest_canonical("bus", INDEX, cutoff=0.95) is None,
          "cutoff alto descarta coincidencias débiles")

    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK - suggest_canonical: typos, alias, sin-match, cutoff")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
