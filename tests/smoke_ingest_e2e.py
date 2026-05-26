"""E2E real: spot DB → LLM call → parse → ingest → verificar state.

Usa una transacción que se revierte al final (no deja datos).
Útil para validar el pipeline completo antes de PR 5 (job nocturno).

  PYTHONPATH=. python tests/smoke_ingest_e2e.py 65482 [provider]
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from enrichment.gemini_response_parser import parse_enrichment_response
from enrichment.ingest_v2 import ingest_spot_enrichment
from enrichment.llm_provider import call_deepseek_sync, call_gemini_sync, estimate_cost, get_provider_name
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


async def main(spot_id: int, provider: str | None = None) -> int:
    _load_dotenv()
    provider = provider or get_provider_name()
    print(f"provider={provider}")

    conn = await asyncpg.connect(dsn=_dsn())
    tx = conn.transaction()
    await tx.start()
    try:
        spot = await fetch_spot_for_enrichment(conn, spot_id)
        if not spot:
            print(f"spot {spot_id} no existe")
            return 1
        reviews_raw = await fetch_reviews_for_enrichment(conn, spot_id)
        selected = select_reviews_for_prompt(reviews_raw)
        user_prompt = build_spot_user_prompt(spot, selected)
        print(f"spot={spot['canonical_name']!r} reviews={len(reviews_raw)}→{len(selected)}")

        if provider == "deepseek":
            resp = await asyncio.to_thread(call_deepseek_sync, user_prompt)
        else:
            resp = await asyncio.to_thread(call_gemini_sync, user_prompt)
        cost = estimate_cost(resp.model, resp.usage)
        print(f"LLM ok: model={resp.model} in={resp.usage.get('prompt_token_count')} out={resp.usage.get('candidates_token_count')} cost=${cost:.5f}")

        parsed = parse_enrichment_response(resp.text)
        print(f"parsed: claims={len(parsed.claims)} errs={len(parsed.errors)} summary_es={'OK' if parsed.summary_es else '-'}")

        stats = await ingest_spot_enrichment(
            conn, spot_id, parsed,
            provider=resp.provider, llm_model=resp.model,
        )
        print(f"ingest: claims_inserted={stats.claims_inserted} obs={stats.observations_inserted} run={stats.pipeline_run_id}")

        # Verificar state
        row = await conn.fetchrow(
            """
            SELECT quietness_score, beauty_score, safety_score,
                   summary_es, tags, best_for, noise_sources, parking_capacity,
                   last_observation_at, enrichment_version, llm_model,
                   total_observations, consensus_confidence
            FROM spot_semantic_state WHERE spot_id = $1
            """,
            spot_id,
        )
        print("\nspot_semantic_state:")
        for k, v in dict(row).items():
            print(f"  {k:<22} = {v}")

        # Verificar vista (freshness)
        fw = await conn.fetchval(
            "SELECT freshness_warning FROM v_spot_semantic_state WHERE spot_id = $1",
            spot_id,
        )
        print(f"\nvista v_spot_semantic_state.freshness_warning = {fw}")

        return 0
    finally:
        await tx.rollback()
        await conn.close()
        print("\n(transacción revertida — no se persiste nada)")


if __name__ == "__main__":
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 65482
    prov = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(asyncio.run(main(sid, prov)))
