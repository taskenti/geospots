"""Regresion -- toggle ENRICHMENT_MODE (Opcion A / B / regex).

Verifica el punto unico de decision de escalado (should_escalate_to_llm) y el
resolver de modo. NO toca DB ni LLM: ejercita la logica de gating pura.

  hybrid      (Opcion B): escala solo si texto sustancial (>=120) y regex no
              cubrio (n<3), o si hay mencion ambigua (force_llm).
  llm_only    (Opcion A): escala TODA review con texto >= ENRICHMENT_LLM_MIN_CHARS.
  regex_only  : nunca escala.

Ejecutar:  python -m tests.test_enrichment_mode
"""

import os

# Fijar el umbral ANTES de importar (se lee en import-time como default).
os.environ.setdefault("ENRICHMENT_LLM_MIN_CHARS", "30")

from enrichment.claim_extractor import (
    resolve_enrichment_mode,
    should_escalate_to_llm,
)

SHORT = "buen sitio"                 # < 30 chars
MID = "x" * 60                       # 30..119 chars
LONG = "x" * 200                     # >= 120 chars


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # ── resolve_enrichment_mode ──────────────────────────────────────────────
    check(resolve_enrichment_mode("hybrid") == "hybrid", "resolve hybrid")
    check(resolve_enrichment_mode("LLM_ONLY") == "llm_only", "resolve normaliza mayusculas")
    check(resolve_enrichment_mode("  regex_only ") == "regex_only", "resolve trim")
    check(resolve_enrichment_mode("basura") == "hybrid", "resolve invalido -> hybrid")
    # env fallback
    prev = os.environ.get("ENRICHMENT_MODE")
    os.environ["ENRICHMENT_MODE"] = "llm_only"
    check(resolve_enrichment_mode(None) == "llm_only", "resolve None -> env")
    if prev is None:
        del os.environ["ENRICHMENT_MODE"]
    else:
        os.environ["ENRICHMENT_MODE"] = prev

    # ── regex_only: nunca escala ─────────────────────────────────────────────
    for txt in (SHORT, MID, LONG):
        check(not should_escalate_to_llm(txt, 0, False, "regex_only"),
              f"regex_only nunca escala (len={len(txt)})")
    check(not should_escalate_to_llm(LONG, 0, True, "regex_only"),
          "regex_only no escala ni con force_llm")

    # ── llm_only: escala todo lo que supere el umbral de chars ───────────────
    check(not should_escalate_to_llm(SHORT, 0, False, "llm_only", llm_min_chars=30),
          "llm_only NO escala texto < umbral")
    check(should_escalate_to_llm(MID, 5, False, "llm_only", llm_min_chars=30),
          "llm_only escala texto medio aunque regex cubriera (n=5)")
    check(should_escalate_to_llm(LONG, 99, False, "llm_only", llm_min_chars=30),
          "llm_only escala texto largo siempre")

    # ── hybrid (Opcion B): la logica clasica ─────────────────────────────────
    check(not should_escalate_to_llm(SHORT, 0, False, "hybrid"),
          "hybrid NO escala texto corto (<120) sin force")
    check(not should_escalate_to_llm(MID, 0, False, "hybrid"),
          "hybrid NO escala texto medio (<120) sin force")
    check(should_escalate_to_llm(LONG, 0, False, "hybrid"),
          "hybrid escala texto largo con regex pobre (n=0)")
    check(should_escalate_to_llm(LONG, 2, False, "hybrid"),
          "hybrid escala texto largo con regex n=2")
    check(not should_escalate_to_llm(LONG, 3, False, "hybrid"),
          "hybrid NO escala texto largo con cobertura suficiente (n>=3)")
    # force_llm (mencion ambigua) gana a todo en hybrid, incluso texto corto
    check(should_escalate_to_llm(SHORT, 9, True, "hybrid"),
          "hybrid: force_llm escala incluso texto corto con regex rico")

    if failures:
        print(f"FALLOS ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK -- toggle ENRICHMENT_MODE (hybrid/llm_only/regex_only) pasa")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
