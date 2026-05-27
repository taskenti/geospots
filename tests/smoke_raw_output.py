"""Imprime el JSON raw que devuelve cada modelo para un spot dado.

Uso:
  PYTHONPATH=. python tests/smoke_raw_output.py 139647
  PYTHONPATH=. python tests/smoke_raw_output.py 139647 --model deepseek-v4-flash
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import asyncpg

from enrichment.gemini_response_parser import parse_enrichment_response
from enrichment.llm_provider import call_deepseek_sync, call_gemini_sync, estimate_cost
from enrichment.prompts import build_spot_user_prompt
from enrichment.spot_packager import (
    fetch_reviews_for_enrichment,
    fetch_spot_for_enrichment,
    select_reviews_for_prompt,
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
    ("deepseek", "deepseek-v4-flash"),
    ("gemini",   "gemini-2.5-flash-lite"),
    ("gemini",   "gemini-2.5-flash"),
]

W = 80  # ancho de linea


def _sep(label: str = "") -> None:
    if label:
        pad = max(0, W - len(label) - 4)
        print(f"\n== {label} {'=' * pad}")
    else:
        print("-" * W)


async def main(spot_id: int, only_model: str | None = None) -> int:
    _load_dotenv()
    conn = await asyncpg.connect(dsn=_dsn())
    try:
        spot = await fetch_spot_for_enrichment(conn, spot_id)
        if not spot:
            print(f"Spot {spot_id} no encontrado")
            return 1
        reviews_raw = await fetch_reviews_for_enrichment(conn, spot_id)
        selected = select_reviews_for_prompt(reviews_raw)
        user_prompt = build_spot_user_prompt(spot, selected)

        print(f"SPOT {spot_id}: {spot['canonical_name']!r}  [{spot.get('country_iso','?').upper()}]")
        print(f"reviews total={len(reviews_raw)} seleccionadas={len(selected)}")
        print(f"prompt chars={len(user_prompt)}  tokens~{len(user_prompt)//4}")
        print()

        for provider, model in CANDIDATES:
            if only_model and model != only_model:
                continue
            label = f"{provider}/{model}"
            _sep(label)
            try:
                t0 = time.time()
                if provider == "gemini":
                    resp = await asyncio.to_thread(call_gemini_sync, user_prompt, model=model)
                else:
                    resp = await asyncio.to_thread(call_deepseek_sync, user_prompt, model=model)
                latency = time.time() - t0

                cost = estimate_cost(model, resp.usage)
                print(f"latency={latency:.1f}s  in={resp.usage.get('prompt_token_count')}  "
                      f"out={resp.usage.get('candidates_token_count')}  cost=${cost:.5f}")
                print()

                try:
                    parsed = parse_enrichment_response(resp.text)

                    print(f"CLAIMS ({len(parsed.claims)}):")
                    for i, c in enumerate(parsed.claims, 1):
                        print(f"  {i:2}. [{c.signal:<30}]  val={str(c.value):<20}  "
                              f"conf={c.confidence}  rev_idx={c.raw.get('review_index')}")
                        if c.excerpt:
                            excerpt = c.excerpt[:120].replace('\n', ' ')
                            print(f"      excerpt: {excerpt!r}")

                    if parsed.errors:
                        print(f"\nPARSER WARNINGS ({len(parsed.errors)}):")
                        for e in parsed.errors:
                            print(f"  WARN: {e}")

                    print(f"\nSUMMARY (English):\n  {parsed.summary or '(empty)'}")
                    print(f"\nTAGS:     {parsed.tags}")
                    print(f"BEST_FOR: {parsed.best_for}")
                    if parsed.best_season:
                        print(f"BEST_SEASON: {parsed.best_season}")
                    if parsed.avoid_season:
                        print(f"AVOID_SEASON: {parsed.avoid_season}")
                    if hasattr(parsed, 'noise_sources') and parsed.noise_sources:
                        print(f"NOISE_SOURCES: {parsed.noise_sources}")
                    if hasattr(parsed, 'parking_capacity') and parsed.parking_capacity:
                        print(f"PARKING_CAPACITY: {parsed.parking_capacity}")

                except Exception as parse_exc:
                    print(f"PARSE ERROR: {parse_exc}")
                    print("\nRAW TEXT (primeros 2000 chars):")
                    print(resp.text[:2000])

            except Exception as exc:
                print(f"LLM ERROR: {exc}")

        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    spot_id = int(args[0]) if args else 139647
    only_model = None
    if "--model" in args:
        idx = args.index("--model")
        only_model = args[idx + 1]
    out_path = None
    if "--out" in args:
        idx = args.index("--out")
        out_path = args[idx + 1]
        # Redirigir stdout a archivo utf-8 para evitar problemas de encoding Windows
        sys.stdout = open(out_path, "w", encoding="utf-8")
    sys.exit(asyncio.run(main(spot_id, only_model=only_model)))
