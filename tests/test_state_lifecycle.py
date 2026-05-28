"""Smoke test del lifecycle de alertas con estado intermedio (T2.2).

Ejecutar:  python -m tests.test_state_lifecycle
"""

from enrichment.state_resolver import (
    DECAYING_CONFIDENCE_THRESHOLD,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DECAYING,
    LIFECYCLE_RESOLVED,
    RESOLVE_CONFIDENCE_THRESHOLD,
    lifecycle_rank_weight,
    lifecycle_state,
)


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # Banda intermedia real: el umbral de decaying debe estar por encima del de resolución.
    check(DECAYING_CONFIDENCE_THRESHOLD > RESOLVE_CONFIDENCE_THRESHOLD,
          "DECAYING_CONFIDENCE_THRESHOLD debe ser > RESOLVE_CONFIDENCE_THRESHOLD")

    # Tres estados según confidence/resolved.
    check(lifecycle_state(0.80, resolved=False) == LIFECYCLE_ACTIVE,
          "conf alta no-resuelta -> active")
    check(lifecycle_state(0.50, resolved=False) == LIFECYCLE_ACTIVE,
          "conf == umbral no-resuelta -> active (frontera inclusiva)")
    check(lifecycle_state(0.49, resolved=False) == LIFECYCLE_DECAYING,
          "conf bajo umbral no-resuelta -> decaying")
    check(lifecycle_state(0.20, resolved=False) == LIFECYCLE_DECAYING,
          "conf muy baja pero no-resuelta (sin guarda 180d) -> decaying, NO resolved")
    check(lifecycle_state(0.20, resolved=True) == LIFECYCLE_RESOLVED,
          "resolved=True -> likely_resolved")
    check(lifecycle_state(0.90, resolved=True) == LIFECYCLE_RESOLVED,
          "resolved manda sobre confidence")

    # Pesos de ranking: active > decaying > resolved.
    wa = lifecycle_rank_weight(LIFECYCLE_ACTIVE)
    wd = lifecycle_rank_weight(LIFECYCLE_DECAYING)
    wr = lifecycle_rank_weight(LIFECYCLE_RESOLVED)
    check(wa == 1.0, f"peso active debe ser 1.0, got {wa}")
    check(wd == 0.5, f"peso decaying debe ser 0.5, got {wd}")
    check(wr == 0.0, f"peso likely_resolved debe ser 0.0, got {wr}")
    check(wa > wd > wr, "orden de pesos active > decaying > likely_resolved")
    check(lifecycle_rank_weight("estado_inventado") == 0.0,
          "estado desconocido -> peso 0.0")

    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK - lifecycle 3 estados + pesos de ranking correctos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
