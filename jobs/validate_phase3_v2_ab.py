"""Validación A/B multi-modelo de enrichment v2.

Selecciona una muestra estratificada por país (ES/PT/NL/FR/IT/DE), ejecuta los
3 modelos candidatos contra cada spot, y emite métricas comparativas + report MD.

NO persiste en DB. Solo lee spots/reviews y llama LLMs.

Uso:
  PYTHONPATH=. python -m jobs.validate_phase3_v2_ab --n-per-country 4
  PYTHONPATH=. python -m jobs.validate_phase3_v2_ab --countries ES,PT --n-per-country 5 --models deepseek-v4-flash,gemini-2.5-flash-lite
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import asyncpg
from loguru import logger

from enrichment.db_pool import dsn
from enrichment.gemini_response_parser import ParseError, parse_enrichment_response
from enrichment.llm_provider import call_deepseek_sync, call_gemini_sync, estimate_cost
from enrichment.prompts import build_spot_user_prompt
from enrichment.spot_packager import (
    fetch_reviews_for_enrichment,
    fetch_spot_for_enrichment,
    select_reviews_for_prompt,
    should_enrich,
)

DEFAULT_COUNTRIES = ["es", "pt", "nl", "fr", "it", "de"]
DEFAULT_MODELS = [
    ("deepseek", "deepseek-v4-flash"),
    ("gemini",   "gemini-2.5-flash-lite"),
    ("gemini",   "gemini-2.5-flash"),
]


# Heurísticas de idioma para detectar si summary_es es realmente español, etc.
SPANISH_HINTS = re.compile(r"\b(el|la|los|las|un|una|de|del|y|con|en|para|sin|que|por)\b", re.IGNORECASE)
ENGLISH_HINTS = re.compile(r"\b(the|of|and|with|for|is|are|to|in|by|on|at)\b", re.IGNORECASE)


def looks_like_spanish(text: str | None) -> bool:
    if not text or len(text) < 20:
        return False
    matches = len(SPANISH_HINTS.findall(text))
    return matches >= 3


def looks_like_english(text: str | None) -> bool:
    if not text or len(text) < 20:
        return False
    matches = len(ENGLISH_HINTS.findall(text))
    return matches >= 3


@dataclass
class SpotResult:
    spot_id: int
    country: str
    provider: str
    model: str
    latency_s: float
    in_tokens: int
    out_tokens: int
    cost_usd: float
    parse_ok: bool
    parse_error: str | None = None
    claims_count: int = 0
    distinct_signals: int = 0
    summary_es_present: bool = False
    summary_en_present: bool = False
    summary_es_looks_spanish: bool = False
    summary_en_looks_english: bool = False
    tags_count: int = 0
    best_for_count: int = 0
    noise_sources_n: int = 0
    parser_warnings: int = 0
    error: str | None = None


async def select_sample(conn, countries: list[str], n_per_country: int) -> list[dict]:
    """Selección estratificada: N spots aleatorios por país, con ≥5 reviews."""
    sample: list[dict] = []
    for country in countries:
        rows = await conn.fetch(
            """
            SELECT id, country_iso
            FROM spots
            WHERE activo = TRUE
              AND country_iso = $1
              AND COALESCE(total_reviews, 0) >= 5
            ORDER BY random()
            LIMIT $2
            """,
            country.lower(),
            n_per_country,
        )
        for r in rows:
            sample.append({"spot_id": r["id"], "country": r["country_iso"]})
    return sample


async def evaluate_one(spot_id: int, country: str, provider: str, model: str,
                      conn) -> SpotResult:
    """Procesa un spot con un modelo y devuelve métricas (no persiste)."""
    base = SpotResult(spot_id=spot_id, country=country, provider=provider, model=model,
                      latency_s=0.0, in_tokens=0, out_tokens=0, cost_usd=0.0, parse_ok=False)
    try:
        spot = await fetch_spot_for_enrichment(conn, spot_id)
        if not spot:
            base.error = "spot not found"
            return base
        reviews = await fetch_reviews_for_enrichment(conn, spot_id)
        decision, reason = should_enrich(spot, len(reviews))
        if not decision:
            base.error = f"skipped: {reason}"
            return base
        selected = select_reviews_for_prompt(reviews)
        user_prompt = build_spot_user_prompt(spot, selected)
    except Exception as exc:
        base.error = f"prep: {exc}"
        return base

    try:
        t0 = time.time()
        if provider == "deepseek":
            resp = await asyncio.to_thread(call_deepseek_sync, user_prompt, model=model)
        else:
            resp = await asyncio.to_thread(call_gemini_sync, user_prompt, model=model)
        base.latency_s = round(time.time() - t0, 2)
        base.in_tokens = int(resp.usage.get("prompt_token_count", 0))
        base.out_tokens = int(resp.usage.get("candidates_token_count", 0))
        base.cost_usd = estimate_cost(model, resp.usage)
    except Exception as exc:
        base.error = f"llm: {exc}"
        return base

    try:
        parsed = parse_enrichment_response(resp.text)
        base.parse_ok = True
        base.claims_count = len(parsed.claims)
        base.distinct_signals = len({c.signal for c in parsed.claims})
        base.summary_es_present = bool(parsed.summary_es)
        base.summary_en_present = bool(parsed.summary_en)
        base.summary_es_looks_spanish = looks_like_spanish(parsed.summary_es)
        base.summary_en_looks_english = looks_like_english(parsed.summary_en)
        base.tags_count = len(parsed.tags)
        base.best_for_count = len(parsed.best_for)
        base.noise_sources_n = sum(1 for c in parsed.claims if c.signal == "noise_source")
        base.parser_warnings = len(parsed.errors)
    except ParseError as exc:
        base.parse_ok = False
        base.parse_error = str(exc)[:200]
    return base


def aggregate_by_model(results: list[SpotResult]) -> dict[str, dict]:
    """Stats por (provider, model)."""
    buckets: dict[str, list[SpotResult]] = {}
    for r in results:
        if r.error and "skipped" in (r.error or ""):
            continue
        key = f"{r.provider}/{r.model}"
        buckets.setdefault(key, []).append(r)

    out = {}
    for key, rs in buckets.items():
        ok = [r for r in rs if r.parse_ok]
        latencies = [r.latency_s for r in ok if r.latency_s > 0]
        costs = [r.cost_usd for r in ok]
        claims = [r.claims_count for r in ok]
        signals = [r.distinct_signals for r in ok]

        out[key] = {
            "n": len(rs),
            "errors": sum(1 for r in rs if r.error and "skipped" not in r.error),
            "parse_fail": sum(1 for r in rs if not r.parse_ok and not r.error),
            "parse_ok": len(ok),
            "latency_p50": round(statistics.median(latencies), 2) if latencies else None,
            "latency_p95": round(statistics.quantiles(latencies, n=20)[18], 2) if len(latencies) >= 20 else (round(max(latencies), 2) if latencies else None),
            "cost_avg": round(statistics.mean(costs), 5) if costs else 0,
            "cost_total": round(sum(costs), 5),
            "claims_avg": round(statistics.mean(claims), 1) if claims else 0,
            "distinct_signals_avg": round(statistics.mean(signals), 1) if signals else 0,
            "summary_es_pct": round(100 * sum(1 for r in ok if r.summary_es_present) / max(1, len(ok)), 1),
            "summary_es_spanish_pct": round(100 * sum(1 for r in ok if r.summary_es_looks_spanish) / max(1, len(ok)), 1),
            "summary_en_pct": round(100 * sum(1 for r in ok if r.summary_en_present) / max(1, len(ok)), 1),
            "summary_en_english_pct": round(100 * sum(1 for r in ok if r.summary_en_looks_english) / max(1, len(ok)), 1),
            "tags_avg": round(statistics.mean([r.tags_count for r in ok]), 1) if ok else 0,
            "best_for_avg": round(statistics.mean([r.best_for_count for r in ok]), 1) if ok else 0,
            "noise_sources_avg": round(statistics.mean([r.noise_sources_n for r in ok]), 2) if ok else 0,
        }
    return out


def aggregate_by_country(results: list[SpotResult]) -> dict[tuple[str, str], dict]:
    """Stats por (country, model) — útil para detectar fallos multilingüe."""
    buckets: dict[tuple[str, str], list[SpotResult]] = {}
    for r in results:
        if r.error and "skipped" in (r.error or ""):
            continue
        buckets.setdefault((r.country, f"{r.provider}/{r.model}"), []).append(r)

    out = {}
    for key, rs in buckets.items():
        ok = [r for r in rs if r.parse_ok]
        out[key] = {
            "n": len(rs),
            "parse_ok": len(ok),
            "summary_es_spanish_pct": round(100 * sum(1 for r in ok if r.summary_es_looks_spanish) / max(1, len(ok)), 1),
            "summary_en_english_pct": round(100 * sum(1 for r in ok if r.summary_en_looks_english) / max(1, len(ok)), 1),
            "claims_avg": round(statistics.mean([r.claims_count for r in ok]), 1) if ok else 0,
            "cost_avg": round(statistics.mean([r.cost_usd for r in ok]), 5) if ok else 0,
        }
    return out


def render_md_report(results: list[SpotResult], by_model: dict, by_country: dict,
                     n_per_country: int) -> str:
    lines = [
        "# Phase 3 v2 — A/B Multi-Modelo Report",
        "",
        f"Muestra: {len({r.spot_id for r in results})} spots, "
        f"{n_per_country} por país, "
        f"países: {sorted({r.country for r in results})}",
        "",
        "## Resumen por modelo",
        "",
        "| Modelo | N | OK | Parse fail | Lat p50 | Lat p95 | Coste/spot | Claims | Distinct signals | summary_es ES% | summary_en EN% | tags avg | best_for avg | noise_sources avg |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in sorted(by_model.keys()):
        s = by_model[key]
        lines.append(
            f"| {key} | {s['n']} | {s['parse_ok']} | {s['parse_fail']} | "
            f"{s['latency_p50']} | {s['latency_p95']} | "
            f"${s['cost_avg']:.5f} | {s['claims_avg']} | {s['distinct_signals_avg']} | "
            f"{s['summary_es_spanish_pct']}% | {s['summary_en_english_pct']}% | "
            f"{s['tags_avg']} | {s['best_for_avg']} | {s['noise_sources_avg']} |"
        )

    lines.append("")
    lines.append("## Proyección 80K spots (sync, lineal)")
    lines.append("")
    lines.append("| Modelo | $/spot | 80K spots | Latencia 80K (concurrency=50) |")
    lines.append("|---|---:|---:|---:|")
    for key in sorted(by_model.keys()):
        s = by_model[key]
        proj_cost = s["cost_avg"] * 80_000
        secs = (s["latency_p50"] or 5) * 80_000 / 50
        lines.append(f"| {key} | ${s['cost_avg']:.5f} | ${proj_cost:,.0f} | {secs/3600:.1f}h |")

    lines.append("")
    lines.append("## Calidad multilingüe (% summary_es en español por país × modelo)")
    lines.append("")
    countries = sorted({c for (c, _) in by_country.keys()})
    models = sorted({m for (_, m) in by_country.keys()})
    header = "| País | " + " | ".join(models) + " |"
    sep = "|---|" + "|".join(["---:"] * len(models)) + "|"
    lines.append(header)
    lines.append(sep)
    for country in countries:
        row = [country.upper()]
        for m in models:
            s = by_country.get((country, m))
            row.append(f"{s['summary_es_spanish_pct']}%" if s else "—")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    lines.append("## Detalle por spot (errores)")
    lines.append("")
    errs = [r for r in results if r.error and "skipped" not in (r.error or "")]
    if errs:
        for r in errs:
            lines.append(f"- spot={r.spot_id} ({r.country}) {r.provider}/{r.model}: {r.error}")
    else:
        lines.append("(sin errores)")

    return "\n".join(lines)


async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--countries", default=",".join(DEFAULT_COUNTRIES),
                        help="Comma-separated ISO codes")
    parser.add_argument("--n-per-country", type=int, default=4)
    parser.add_argument("--models",
                        help=("Comma-separated `provider:model` overrides. "
                              "Default: deepseek:deepseek-v4-flash,gemini:gemini-2.5-flash-lite,gemini:gemini-2.5-flash"))
    parser.add_argument("--output-dir", default="docs/validation")
    args = parser.parse_args(argv)

    countries = [c.strip().lower() for c in args.countries.split(",") if c.strip()]
    if args.models:
        models = []
        for item in args.models.split(","):
            if ":" not in item:
                continue
            provider, model = item.split(":", 1)
            models.append((provider.strip(), model.strip()))
    else:
        models = DEFAULT_MODELS

    logger.info(f"[validate_ab] countries={countries} n_per_country={args.n_per_country} "
                f"models={[m for _,m in models]}")

    conn = await asyncpg.connect(dsn=dsn())
    try:
        sample = await select_sample(conn, countries, args.n_per_country)
        logger.info(f"[validate_ab] sample={len(sample)} spots")

        results: list[SpotResult] = []
        for i, item in enumerate(sample):
            for provider, model in models:
                if provider == "deepseek" and not os.environ.get("DEEPSEEK_API_KEY"):
                    continue
                if provider == "gemini" and not os.environ.get("GEMINI_API_KEY"):
                    continue
                res = await evaluate_one(item["spot_id"], item["country"], provider, model, conn)
                results.append(res)
                logger.info(
                    f"[validate_ab] {i+1}/{len(sample)} spot={item['spot_id']} "
                    f"{provider}/{model}: claims={res.claims_count} "
                    f"cost=${res.cost_usd:.5f} ok={res.parse_ok} err={res.error}"
                )
    finally:
        await conn.close()

    by_model = aggregate_by_model(results)
    by_country = aggregate_by_country(results)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    # CSV con detalle por spot
    csv_path = output_dir / f"ab-{ts}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(results[0]).keys()) if results else [])
        if results:
            writer.writeheader()
            for r in results:
                writer.writerow(asdict(r))

    # JSON con stats agregadas
    json_path = output_dir / f"ab-{ts}.json"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump({
            "by_model": by_model,
            "by_country": {f"{c}|{m}": v for (c, m), v in by_country.items()},
            "n_results": len(results),
        }, fh, indent=2, ensure_ascii=False)

    # Markdown report
    md_path = output_dir / f"ab-{ts}.md"
    md_path.write_text(render_md_report(results, by_model, by_country, args.n_per_country),
                       encoding="utf-8")

    print(f"\n→ CSV: {csv_path}")
    print(f"→ JSON: {json_path}")
    print(f"→ MD:   {md_path}\n")

    print("─── RESUMEN POR MODELO ───")
    for key in sorted(by_model.keys()):
        s = by_model[key]
        print(f"\n{key}:")
        for k, v in s.items():
            print(f"  {k:<30} {v}")

    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
