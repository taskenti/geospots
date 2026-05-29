"""Auditoria exhaustiva de los 10 spots conflictivos (Sprint 7 pre-flight).

Compara el estado ACTUAL en DB (claims viejos, semantic_state) contra lo que
el codigo NUEVO produciria al re-extraer. NO escribe en DB.

Deteccion categorizada por claim:
  TP  — True Positive que sobrevive en nuevo codigo (correcto, bien)
  FP  — False Positive eliminado por nuevos fixes (bug corregido)
  FN  — False Negative: señal que el nuevo codigo pierde respecto al viejo
  NEW — claim que el nuevo codigo genera y el viejo no tenia (mejora o bug nuevo)

Uso:
    docker-compose exec enrichment python -m jobs.audit_conflictive_spots
    docker-compose exec enrichment python -m jobs.audit_conflictive_spots --spot-id 4533
    docker-compose exec enrichment python -m jobs.audit_conflictive_spots --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import defaultdict
from datetime import datetime, timezone

import asyncpg
from loguru import logger

from enrichment.claim_extractor import extract_claims_regex

SPOT_IDS = [4533, 5049, 30252, 85063, 179854, 182524, 221624, 272617, 329717, 439305]

# Senales clave a rastrear (las afectadas por los bugs conocidos)
KEY_SIGNALS = {
    "spot_closed", "lake_nearby", "overnight_safe",
    "police_risk", "cleanliness", "beauty", "quietness", "safety",
    "water_working", "wind_exposure",
}

# Needles que sabemos eran FP bajo el codigo antiguo (pre-Sprint 1)
# Usados para etiquetar FPs conocidos en claims existentes.
_KNOWN_FP_PATTERNS = {
    "lake_nearby": [
        "emplac", "place", "platz", "plaza",  # "lac" substring
    ],
}


async def _connect() -> asyncpg.Connection:
    from enrichment.worker import _dsn
    dsn = os.environ.get("DATABASE_URL") or _dsn()
    return await asyncpg.connect(dsn=dsn)


async def _fetch_spot_info(conn, spot_id: int) -> dict:
    row = await conn.fetchrow(
        """
        SELECT s.id, s.canonical_name, s.country_iso, s.lat, s.lon,
               sss.quietness_score, sss.safety_score, sss.police_risk_score,
               sss.beauty_score, sss.overnight_safe, sss.signals_data,
               sss.total_observations, sss.consensus_confidence,
               sss.active_alert_types, sss.last_aggregated_at,
               sss.summary_en IS NOT NULL AS has_summary
        FROM spots s
        LEFT JOIN spot_semantic_state sss ON sss.spot_id = s.id
        WHERE s.id = $1
        """,
        spot_id,
    )
    return dict(row) if row else {}


async def _fetch_alerts(conn, spot_id: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT alert_type, ROUND(confidence,2) AS conf, valid_from,
               detected_at::date AS detected, valid_until, resolved,
               LEFT(summary, 120) AS summary
        FROM spot_alerts WHERE spot_id = $1 ORDER BY valid_from
        """,
        spot_id,
    )
    return [dict(r) for r in rows]


async def _fetch_old_claims(conn, spot_id: int) -> list[dict]:
    """Claims actuales en DB (solo review-level, excluyendo scraped_facts)."""
    rows = await conn.fetch(
        """
        SELECT ec.id, ec.signal_type, ec.raw_value, ec.extractor_name,
               ec.excerpt, r.fecha AS rev_date,
               LEFT(COALESCE(r.texto_limpio, r.texto, r.texto_original, ''), 200) AS rev_text
        FROM extracted_claims ec
        LEFT JOIN reviews r ON r.id = ec.review_id
        WHERE ec.spot_id = $1
          AND ec.extractor_name NOT IN ('scraped_facts_v1')
          AND ec.signal_type = ANY($2::text[])
        ORDER BY ec.signal_type, r.fecha DESC NULLS LAST
        """,
        spot_id,
        list(KEY_SIGNALS),
    )
    return [dict(r) for r in rows]


async def _fetch_review_sample(conn, spot_id: int, limit: int = 60) -> list[dict]:
    """Reviews recientes para re-extraccion offline."""
    rows = await conn.fetch(
        """
        SELECT id, fecha,
               COALESCE(texto_limpio, texto, texto_original, '') AS text
        FROM reviews
        WHERE spot_id = $1
          AND COALESCE(texto_limpio, texto, texto_original, '') != ''
          AND LENGTH(COALESCE(texto_limpio, texto, texto_original, '')) >= 30
        ORDER BY fecha DESC NULLS LAST
        LIMIT $2
        """,
        spot_id, limit,
    )
    return [dict(r) for r in rows]


def _new_claims_from_reviews(reviews: list[dict]) -> list[dict]:
    """Re-extrae con el codigo nuevo offline (sin escritura en DB)."""
    results = []
    for rev in reviews:
        claims = extract_claims_regex(rev["text"])
        for c in claims:
            if c["signal"] in KEY_SIGNALS:
                results.append({
                    "review_id": rev["id"],
                    "rev_date": rev["fecha"],
                    "signal_type": c["signal"],
                    "value": str(c["value"]),
                    "excerpt": rev["text"][:80],
                })
    return results


def _days_since(dt) -> int | None:
    if dt is None:
        return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    elif not hasattr(dt, 'tzinfo'):
        return None
    return (datetime.now(timezone.utc) - dt).days


def _analyze_spot(
    info: dict,
    old_claims: list[dict],
    new_claims: list[dict],
    alerts: list[dict],
    verbose: bool = False,
) -> dict:
    """Genera el informe diff para un spot."""
    report: dict = {
        "spot_id": info["id"],
        "name": info.get("canonical_name", "?"),
        "country": info.get("country_iso", "?"),
        "signals_old": defaultdict(list),
        "signals_new": defaultdict(list),
        "issues": [],
        "fixed": [],
        "stale_alerts": [],
        "new_bugs": [],
    }

    # ── Señales actuales ───────────────────────────────────────────────────
    for c in old_claims:
        sig = c["signal_type"]
        report["signals_old"][sig].append(c)

    # ── Señales nuevas (re-extraccion) ─────────────────────────────────────
    for c in new_claims:
        sig = c["signal_type"]
        report["signals_new"][sig].append(c)

    # ── Analisis por señal ─────────────────────────────────────────────────
    all_sigs = KEY_SIGNALS & (
        set(report["signals_old"].keys()) | set(report["signals_new"].keys())
    )

    for sig in sorted(all_sigs):
        old = report["signals_old"].get(sig, [])
        new = report["signals_new"].get(sig, [])
        n_old, n_new = len(old), len(new)

        if n_old > 0 and n_new == 0:
            report["fixed"].append(
                f"{sig}: {n_old} claims eliminados por nuevo codigo (FP corregidos)"
            )
        elif n_old == 0 and n_new > 0:
            report["new_bugs"].append(
                f"{sig}: {n_new} claims NUEVOS que el viejo codigo no tenia — revisar"
            )
        elif n_old > 0 and n_new > 0:
            delta = n_new - n_old
            direction = f"+{delta}" if delta >= 0 else str(delta)
            if abs(delta) > 0:
                report["issues"].append(
                    f"{sig}: {n_old} old -> {n_new} new (delta {direction})"
                )

    # ── Señal spot_closed: verificar BUG-11 en estado ─────────────────────
    ssd = info.get("signals_data") or {}
    if isinstance(ssd, str):
        import json
        try:
            ssd = json.loads(ssd)
        except Exception:
            ssd = {}
    sc = ssd.get("spot_closed", {})
    if sc and sc.get("score") is True:
        old_sc = report["signals_old"].get("spot_closed", [])
        if old_sc:
            # Fecha del claim de cierre mas reciente
            max_closure_date = max(
                (c["rev_date"] for c in old_sc if c.get("rev_date")), default=None
            )
            # Fecha de la review de actividad mas reciente (cualquier señal)
            all_activity = [
                c["rev_date"]
                for sig, cs in report["signals_old"].items()
                if sig != "spot_closed"
                for c in cs if c.get("rev_date")
            ]
            max_activity = max(all_activity, default=None)
            if max_closure_date and max_activity and max_activity > max_closure_date:
                report["fixed"].append(
                    f"spot_closed: BUG-11 DEBERIA descartarlo "
                    f"(cierre {max_closure_date}, actividad posterior {max_activity})"
                )
            elif max_closure_date:
                report["issues"].append(
                    f"spot_closed: cierre {max_closure_date}, "
                    f"sin actividad posterior confirmada -> se mantiene"
                )

    # ── Alertas stale ──────────────────────────────────────────────────────
    today = datetime.now(timezone.utc).date()
    for alert in alerts:
        vf = alert["valid_from"]
        age_days = (today - vf).days if vf else None
        if age_days and age_days > 365 and not alert["resolved"]:
            report["stale_alerts"].append(
                f"{alert['alert_type']} desde {vf} "
                f"({age_days // 365}a {age_days % 365 // 30}m sin resolver) "
                f"conf={alert['conf']}: {alert['summary'][:80]}"
            )

    return report


def _print_report(report: dict, info: dict, verbose: bool = False) -> None:
    sc_sig = (info.get("signals_data") or {})
    if isinstance(sc_sig, str):
        import json
        try:
            sc_sig = json.loads(sc_sig)
        except Exception:
            sc_sig = {}

    sc = sc_sig.get("spot_closed", {})
    lake = sc_sig.get("lake_nearby", {})

    # Header
    print(f"\n{'='*72}")
    print(f"SPOT {report['spot_id']} — {report['name']} [{report['country'].upper()}]")
    def _fmt(v):
        return f"{v:.2f}" if v is not None else "NULL"

    print(f"  Signals: quiet={_fmt(info.get('quietness_score'))}"
          f"  safe={_fmt(info.get('safety_score'))}"
          f"  police={_fmt(info.get('police_risk_score'))}"
          f"  overnight={info.get('overnight_safe')}"
          f"  obs={info.get('total_observations')}")

    if sc and sc.get("score") is True:
        print(f"  [!] spot_closed=TRUE (conf={sc.get('confidence', '?'):.2f}, "
              f"n={sc.get('n_observations', '?')})")
    if lake and lake.get("score") is True:
        n_lake = lake.get("n_observations", 0)
        print(f"  [!] lake_nearby=TRUE ({n_lake} obs — probable BUG-01 FP si zona no lacustre)")

    # Alertas stale
    if report["stale_alerts"]:
        print(f"\n  ALERTAS STALE ({len(report['stale_alerts'])}):")
        for a in report["stale_alerts"]:
            print(f"    >> {a}")

    # Fixes que aplicara el nuevo codigo
    if report["fixed"]:
        print(f"\n  CORREGIDO por nuevo codigo ({len(report['fixed'])}):")
        for f in report["fixed"]:
            print(f"    -> {f}")

    # Issues residuales
    if report["issues"]:
        print(f"\n  ISSUES RESIDUALES ({len(report['issues'])}):")
        for i in report["issues"]:
            print(f"    ?? {i}")

    # Bugs nuevos encontrados
    if report["new_bugs"]:
        print(f"\n  POSIBLES NUEVOS BUGS ({len(report['new_bugs'])}):")
        for b in report["new_bugs"]:
            print(f"    !! {b}")

    if verbose:
        print("\n  --- CLAIMS ACTUALES (DB) ---")
        for sig, claims in sorted(report["signals_old"].items()):
            for c in claims[:3]:
                print(f"    [{c['extractor_name'][:14]}] {sig}={c['raw_value']} "
                      f"({c['rev_date']}) | {(c['excerpt'] or '')[:60]}")
            if len(claims) > 3:
                print(f"    ... y {len(claims)-3} mas")

        print("\n  --- CLAIMS NUEVOS (re-extraccion offline) ---")
        for sig, claims in sorted(report["signals_new"].items()):
            for c in claims[:3]:
                print(f"    {sig}={c['value']} ({c['rev_date']}) | {c['excerpt'][:60]}")
            if len(claims) > 3:
                print(f"    ... y {len(claims)-3} mas")


async def run(spot_ids: list[int] | None = None, verbose: bool = False) -> dict:
    conn = await _connect()
    target = spot_ids or SPOT_IDS
    summary = {
        "spots": len(target),
        "total_stale_alerts": 0,
        "spots_spot_closed_fixed": 0,
        "spots_lake_fixed": 0,
        "new_issues_found": 0,
    }
    try:
        for spot_id in target:
            info = await _fetch_spot_info(conn, spot_id)
            if not info:
                print(f"[WARN] spot_id={spot_id} no encontrado")
                continue

            old_claims = await _fetch_old_claims(conn, spot_id)
            reviews = await _fetch_review_sample(conn, spot_id)
            new_claims = _new_claims_from_reviews(reviews)
            alerts = await _fetch_alerts(conn, spot_id)

            report = _analyze_spot(info, old_claims, new_claims, alerts, verbose)
            _print_report(report, info, verbose)

            summary["total_stale_alerts"] += len(report["stale_alerts"])
            if any("spot_closed" in f for f in report["fixed"]):
                summary["spots_spot_closed_fixed"] += 1
            if any("lake_nearby" in f for f in report["fixed"]):
                summary["spots_lake_fixed"] += 1
            summary["new_issues_found"] += len(report["new_bugs"])

    finally:
        await conn.close()

    return summary


def main():
    p = argparse.ArgumentParser(
        description="Auditoria de 10 spots conflictivos: old DB vs nuevo codigo."
    )
    p.add_argument("--spot-id", type=int, nargs="+", help="Limitar a spots concretos.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Muestra claims individuales.")
    args = p.parse_args()
    stats = asyncio.run(run(spot_ids=args.spot_id, verbose=args.verbose))
    print(f"\n{'='*72}")
    print(f"RESUMEN: {stats}")


if __name__ == "__main__":
    main()
