"""Compara providers/modelos para enrichment v2 sobre un mismo spot.

Ejecuta UNA llamada síncrona contra cada combinación elegida y reporta:
  - claims emitidos, errores de parseo
  - presencia de summary_es/en, tags, best_for
  - tokens in/out, coste estimado

Útil para informar PR 6 (validación A/B) sin gastar mucho.

Uso:
  PYTHONPATH=. python tests/smoke_llm_compare.py 65482
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import asyncpg
from loguru import logger

from enrichment.gemini_response_parser import parse_enrichment_response
from enrichment.llm_provider import call_deepseek_sync, call_gemini_sync, estimate_cost
from enrichment.prompts import build_spot_user_prompt
from enrichment.spot_packager import (
    fetch_reviews_for_enrichment,
    fetch_spot_for_enrichment,
    select_reviews_for_prompt,
    should_enrich,
)


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _dsn() -> str:
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    pw = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


CANDIDATES = [
    ("gemini",   "gemini-2.5-flash"),
    ("gemini",   "gemini-2.5-flash-lite"),
    ("deepseek", "deepseek-v4-flash"),
]


async def main(spot_id: int) -> int:
    _load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY no definida")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY no definida")

    conn = await asyncpg.connect(dsn=_dsn())
    try:
        spot = await fetch_spot_for_enrichment(conn, spot_id)
        reviews_raw = await fetch_reviews_for_enrichment(conn, spot_id)
        decision, reason = should_enrich(spot, len(reviews_raw))
        if not decision:
            print(f"spot {spot_id} descartado: {reason}")
            return 1
        selected = select_reviews_for_prompt(reviews_raw)
        user_prompt = build_spot_user_prompt(spot, selected)
        print(f"spot {spot_id}: {spot['canonical_name']!r}")
        print(f"reviews raw={len(reviews_raw)} selected={len(selected)} user_prompt_chars={len(user_prompt)}\n")

        rows = []
        for provider, model in CANDIDATES:
            label = f"{provider}/{model}"
            print(f"─── {label} ───")
            try:
                t0 = time.time()
                if provider == "gemini":
                    if not os.environ.get("GEMINI_API_KEY"):
                        print("  saltado (sin API key)\n")
                        continue
                    resp = await asyncio.to_thread(call_gemini_sync, user_prompt, model=model)
                else:
                    if not os.environ.get("DEEPSEEK_API_KEY"):
                        print("  saltado (sin API key)\n")
                        continue
                    resp = await asyncio.to_thread(call_deepseek_sync, user_prompt, model=model)
                latency = time.time() - t0

                try:
                    parsed = parse_enrichment_response(resp.text)
                    parse_ok = True
                except Exception as e:
                    parsed = None
                    parse_ok = False
                    parse_err = str(e)[:200]

                cost = estimate_cost(model, resp.usage)
                row = {
                    "label": label,
                    "latency_s": round(latency, 1),
                    "in_tokens": resp.usage.get("prompt_token_count", 0),
                    "out_tokens": resp.usage.get("candidates_token_count", 0),
                    "cached_tokens": resp.usage.get("cached_content_token_count", 0),
                    "cost_usd": cost,
                    "claims": len(parsed.claims) if parse_ok else 0,
                    "parse_errors": len(parsed.errors) if parse_ok else -1,
                    "has_summary_es": bool(parsed.summary_es) if parse_ok else False,
                    "has_summary_en": bool(parsed.summary_en) if parse_ok else False,
                    "tags_n": len(parsed.tags) if parse_ok else 0,
                    "best_for_n": len(parsed.best_for) if parse_ok else 0,
                    "parse_ok": parse_ok,
                }
                rows.append(row)
                print(f"  latency: {latency:.1f}s")
                print(f"  tokens: in={resp.usage.get('prompt_token_count')} out={resp.usage.get('candidates_token_count')} cached={resp.usage.get('cached_content_token_count', 0)}")
                print(f"  cost (sync, sin cache hit real): ${cost:.5f}")
                if parse_ok:
                    print(f"  claims={len(parsed.claims)} errs={len(parsed.errors)} summary_es={'OK' if parsed.summary_es else 'NO'} summary_en={'OK' if parsed.summary_en else 'NO'} tags={len(parsed.tags)} best_for={len(parsed.best_for)}")
                    # Idiomas detectados en summary_es: heurística rápida
                    if parsed.summary_es and not any(c in parsed.summary_es.lower() for c in "áéíóúñ"):
                        print("  ⚠ summary_es no parece español (no acentos ni eñe)")
                else:
                    print(f"  ❌ parse_error: {parse_err}")
                print()
            except Exception as exc:
                print(f"  ❌ exception: {exc}\n")
                rows.append({"label": label, "error": str(exc)[:200]})

        # Resumen tabular
        print("═══ RESUMEN ═══")
        print(f"{'label':<35} {'lat':>5} {'in':>6} {'out':>6} {'cost':>9} {'claims':>7} {'sum_es':>7} {'sum_en':>7} {'tags':>5} {'bf':>4}")
        for r in rows:
            if "error" in r:
                print(f"{r['label']:<35} ERROR: {r['error']}")
                continue
            print(f"{r['label']:<35} {r['latency_s']:>5.1f} {r['in_tokens']:>6} {r['out_tokens']:>6} ${r['cost_usd']:>7.5f} {r['claims']:>7} {'OK' if r['has_summary_es'] else '-':>7} {'OK' if r['has_summary_en'] else '-':>7} {r['tags_n']:>5} {r['best_for_n']:>4}")

        # Proyección 80K spots (asumiendo este perfil de tokens)
        print("\nProyección 80K spots (lineal, sin batch/cache):")
        for r in rows:
            if "error" in r:
                continue
            proj = r["cost_usd"] * 80_000
            print(f"  {r['label']:<35} ${proj:,.0f}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 65482
    sys.exit(asyncio.run(main(sid)))
