"""Test de detect_regime_change / compute_signal_flux (T2.5 — cambio de régimen).

Ejecutar:  python -m tests.test_regime_change
"""

from datetime import datetime, timedelta, timezone

from enrichment.state_aggregator import (
    REGIME_MIN_DELTA,
    REGIME_MIN_SEPARATION_DAYS,
    compute_signal_flux,
    detect_regime_change,
)

NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)


def _obs(signal, value, days_ago, weight=1.0, *, boolean=False):
    o = {
        "signal_type": signal,
        "observed_at": NOW - timedelta(days=days_ago),
        "observation_weight": weight,
        "value_num": None if boolean else float(value),
        "value_bool": bool(value) if boolean else None,
    }
    return o


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # ── Caso Grau Roig: obras (quietness baja) en 2025 → tranquilo en 2026 ──────
    # Histórico: >180d, quietness ~0.2. Reciente: ≤180d (con gap ≥90d), ~0.85.
    obs = [
        _obs("quietness", 0.2, 400), _obs("quietness", 0.25, 380), _obs("quietness", 0.15, 360),
        _obs("quietness", 0.85, 60), _obs("quietness", 0.9, 40), _obs("quietness", 0.8, 20),
    ]
    res = detect_regime_change(obs, "quietness", value_type="numeric", now=NOW)
    check(res is not None, "Grau Roig: debería detectar cambio")
    if res:
        check(res["changed"] is True, "changed=True")
        check(res["old"] < 0.3, f"old≈0.2, got {res['old']}")
        check(res["new"] > 0.8, f"new≈0.85, got {res['new']}")
        check(res["delta"] > REGIME_MIN_DELTA, "delta > umbral")
        check(res["n_recent"] == 3 and res["n_historical"] == 3, "conteos correctos")
        check(res["since"] == (NOW - timedelta(days=60)).date().isoformat(),
              "since = fecha de la obs reciente más antigua")

    # ── Guarda n bajo: <3 en un cluster → None ─────────────────────────────────
    few = [_obs("quietness", 0.2, 400), _obs("quietness", 0.25, 380),
           _obs("quietness", 0.9, 40), _obs("quietness", 0.8, 20)]
    check(detect_regime_change(few, "quietness", now=NOW) is None,
          "n histórico=2 → None")

    # ── Guarda separación: drift continuo cruzando 180d sin hueco → None ───────
    # Histórico justo >180d, reciente justo <180d → gap < 90d.
    cont = [
        _obs("quietness", 0.2, 200), _obs("quietness", 0.2, 195), _obs("quietness", 0.2, 190),
        _obs("quietness", 0.9, 175), _obs("quietness", 0.9, 170), _obs("quietness", 0.9, 165),
    ]
    check(detect_regime_change(cont, "quietness", now=NOW) is None,
          f"separación < {REGIME_MIN_SEPARATION_DAYS}d → None")

    # ── Guarda delta: cambio pequeño (<0.4) → None ─────────────────────────────
    small = [
        _obs("quietness", 0.5, 400), _obs("quietness", 0.5, 380), _obs("quietness", 0.5, 360),
        _obs("quietness", 0.6, 60), _obs("quietness", 0.6, 40), _obs("quietness", 0.6, 20),
    ]
    check(detect_regime_change(small, "quietness", now=NOW) is None,
          "Δ=0.1 < 0.4 → None")

    # ── Booleano: wild_camping_legal pasa de prohibido (0) a permitido (1) ──────
    bools = [
        _obs("wild_camping_legal", False, 400, boolean=True),
        _obs("wild_camping_legal", False, 380, boolean=True),
        _obs("wild_camping_legal", False, 360, boolean=True),
        _obs("wild_camping_legal", True, 60, boolean=True),
        _obs("wild_camping_legal", True, 40, boolean=True),
        _obs("wild_camping_legal", True, 20, boolean=True),
    ]
    rb = detect_regime_change(bools, "wild_camping_legal", value_type="boolean", now=NOW)
    check(rb is not None and rb["old"] == 0.0 and rb["new"] == 1.0,
          "boolean: 0→1 detectado")

    # ── compute_signal_flux: agrega por señal y salta TEXT (noise_source) ──────
    mixed = obs + [
        _obs("noise_source", 0.0, 400), _obs("noise_source", 0.0, 40),  # TEXT-ish, debe saltarse
    ]
    # noise_source es value_type="text" → se ignora; quietness sí entra.
    flux = compute_signal_flux(mixed, now=NOW)
    check("quietness" in flux, "flux incluye quietness")
    check("noise_source" not in flux, "flux NO incluye señal TEXT")

    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK - detect_regime_change: cambio, guardas (n/separación/delta), boolean, flux")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
