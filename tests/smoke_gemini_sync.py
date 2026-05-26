"""Sanity check síncrono: 1 spot real → 1 llamada Gemini → parsea respuesta.

NO usa Batch API (latencia inmediata) y NO usa context caching (no merece
la pena para 1 llamada). Verifica que:
  - SYSTEM_PROMPT_V2 funciona end-to-end
  - El JSON que devuelve Gemini cumple nuestro schema
  - Los costes son los estimados

Ejecutar:
  PYTHONPATH=. python tests/smoke_gemini_sync.py 65482
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from loguru import logger

from enrichment.gemini_cache import call_gemini_once_sync
from enrichment.gemini_response_parser import parse_enrichment_response
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
    _load_dotenv()
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    pw = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


async def main(spot_id: int) -> int:
    _load_dotenv()
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY no definida — abortando")
        return 1

    conn = await asyncpg.connect(dsn=_dsn())
    try:
        spot = await fetch_spot_for_enrichment(conn, spot_id)
        if not spot:
            print(f"spot {spot_id} no existe o no activo")
            return 1

        reviews_raw = await fetch_reviews_for_enrichment(conn, spot_id)
        decision, reason = should_enrich(spot, len(reviews_raw))
        print(f"--- spot {spot_id}: {spot['canonical_name']!r} ---")
        print(f"reviews crudas={len(reviews_raw)} decision={decision} ({reason})")
        if not decision:
            print("descartado")
            return 0

        selected = select_reviews_for_prompt(reviews_raw)
        user_prompt = build_spot_user_prompt(spot, selected)
        print(f"reviews seleccionadas={len(selected)} user_prompt_chars={len(user_prompt)}")

        print("\n>>> llamando a Gemini (sync, sin caching)...")
        text, usage = await asyncio.to_thread(call_gemini_once_sync, user_prompt)
        print(f"<<< usage={usage}")
        print("\nRESPONSE RAW:")
        print(text[:2000])

        print("\nPARSING...")
        result = parse_enrichment_response(text)
        print(f"claims={len(result.claims)} errors={len(result.errors)}")
        print(f"summary_es={result.summary_es!r}")
        print(f"summary_en={result.summary_en!r}")
        print(f"tags={result.tags}")
        print(f"best_for={result.best_for}")
        print(f"best_season={result.best_season} avoid_season={result.avoid_season}")

        print("\nclaims:")
        for c in result.claims:
            print(f"  {c.signal:<22} = {c.value!r:<10} conf={c.confidence:.2f} review_id={c.review_id} | {c.excerpt[:80]!r}")

        if result.errors:
            print("\nwarnings:")
            for e in result.errors:
                print(f"  - {e}")

        # Estimación coste (sin caching, llamada síncrona)
        if usage:
            ti = usage.get("prompt_token_count", 0)
            to = usage.get("candidates_token_count", 0)
            # gemini-2.0-flash: $0.10/M in, $0.40/M out (síncrono, sin batch)
            cost = ti * 0.10 / 1e6 + to * 0.40 / 1e6
            print(f"\nestimación coste esta llamada (sync, sin batch): ${cost:.5f}")
            # Proyección con batch + caching ahorra ~50% global
            print(f"  → en Batch+caching: ~${cost * 0.5:.5f}")

        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 65482
    sys.exit(asyncio.run(main(sid)))
