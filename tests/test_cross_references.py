"""Test del parsing de cross_references (T2.6 — relaciones spot↔spot).

Cubre el parser (puro, sin DB). La resolución geo+trgm se valida en vivo aparte.

Ejecutar:  python -m tests.test_cross_references
"""

import json

from enrichment.gemini_response_parser import parse_enrichment_response


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # ── cross_reference válido se parsea ───────────────────────────────────────
    resp = json.dumps({
        "review_claims": [],
        "cross_references": [
            {"mentioned_name": "Marina del Este", "relation_type": "walking_distance",
             "review_id": 502, "excerpt": "walked to the marina"},
            {"mentioned_name": "Telesilla parking", "relation_type": "parking_for_visit"},
        ],
        "summary": None, "tags": [], "best_for": [],
    })
    p = parse_enrichment_response(resp)
    check(len(p.cross_references) == 2, f"esperaba 2 cross_refs, got {len(p.cross_references)}")
    if len(p.cross_references) == 2:
        a, b = p.cross_references
        check(a.mentioned_name == "Marina del Este", "nombre preservado")
        check(a.relation_type == "walking_distance", "relation_type ok")
        check(a.review_id == 502, "review_id ok")
        check(b.review_id is None, "review_id omitido → None")

    # ── relation_type fuera de vocabulario → descartado con error no fatal ─────
    resp2 = json.dumps({
        "review_claims": [],
        "cross_references": [
            {"mentioned_name": "Some place", "relation_type": "teleport_link"},
            {"mentioned_name": "Valid place", "relation_type": "same_complex"},
        ],
        "summary": None, "tags": [], "best_for": [],
    })
    p2 = parse_enrichment_response(resp2)
    check(len(p2.cross_references) == 1, "solo el válido sobrevive")
    check(any("relation_type fuera de vocabulario" in e for e in p2.errors),
          "error registrado para vocab inválido")

    # ── sin mentioned_name → descartado ────────────────────────────────────────
    resp3 = json.dumps({
        "review_claims": [],
        "cross_references": [{"relation_type": "same_complex"}],
        "summary": None, "tags": [], "best_for": [],
    })
    p3 = parse_enrichment_response(resp3)
    check(len(p3.cross_references) == 0, "sin mentioned_name → descartado")

    # ── ausencia de cross_references → lista vacía (no rompe) ──────────────────
    p4 = parse_enrichment_response(json.dumps(
        {"review_claims": [], "summary": None, "tags": [], "best_for": []}))
    check(p4.cross_references == [], "campo ausente → []")

    # ── 'name' como alias de 'mentioned_name' ──────────────────────────────────
    p5 = parse_enrichment_response(json.dumps({
        "review_claims": [],
        "cross_references": [{"name": "Aliased", "relation_type": "alternative_overnight"}],
        "summary": None, "tags": [], "best_for": [],
    }))
    check(len(p5.cross_references) == 1 and p5.cross_references[0].mentioned_name == "Aliased",
          "alias 'name' aceptado")

    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK - cross_references: parse, vocab guard, sin-nombre, ausencia, alias")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
