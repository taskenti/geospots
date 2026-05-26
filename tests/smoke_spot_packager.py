"""Smoke test: empaquetar un spot real de la DB e imprimir el prompt v2.

Ejecutar desde host:
  python tests/smoke_spot_packager.py 65482

NO llama al LLM. Solo construye el prompt y lo muestra para inspección visual.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from enrichment.prompts import SYSTEM_PROMPT_V2, build_spot_user_prompt
from enrichment.spot_packager import (
    estimate_tokens,
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
    conn = await asyncpg.connect(dsn=_dsn())
    try:
        spot = await fetch_spot_for_enrichment(conn, spot_id)
        if not spot:
            print(f"❌ spot {spot_id} no existe o no está activo")
            return 1

        reviews_raw = await fetch_reviews_for_enrichment(conn, spot_id)
        decision, reason = should_enrich(spot, len(reviews_raw))
        print(f"─── spot {spot_id}: {spot['canonical_name']!r} ({spot.get('country_iso')}) ───")
        print(f"reviews crudas: {len(reviews_raw)} | should_enrich: {decision} ({reason})")

        selected = select_reviews_for_prompt(reviews_raw)
        print(f"reviews seleccionadas: {len(selected)}")

        user_prompt = build_spot_user_prompt(spot, selected)
        sys_tokens = estimate_tokens(SYSTEM_PROMPT_V2)
        user_tokens = estimate_tokens(user_prompt)
        print(f"\nTokens estimados: system={sys_tokens}, user={user_tokens}, total={sys_tokens + user_tokens}")
        print("\n" + "═" * 72)
        print("USER PROMPT")
        print("═" * 72)
        print(user_prompt)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    sys.exit(asyncio.run(main(sid)))
