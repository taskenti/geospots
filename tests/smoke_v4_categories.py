"""Smoke v4 sobre 4 spots representativos de cada categoría.

Llama DeepSeek una vez por spot, escribe el output a un archivo para
inspección manual. Compara qué cambia respecto al v3.
"""
import asyncio
import os
import sys
import time

import asyncpg


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


# 4 spots representativos elegidos del análisis previo:
# - 139647 IT parking grande (caso conocido v3, multi-idioma)
# - 111566 ES camping con TODOS los servicios
# - 61376 SE area_ac (alguna agua/wc, sin electricidad)
# - 564419 DE naturaleza/wild (sin servicios, hospedaje en jardín)
SPOTS = [
    (139647, "IT parking — caso multi-idioma con prohibición parcial"),
    (111566, "ES camping — todos los servicios + sin reviews multi-lang"),
    (61376,  "SE area_ac — agua sí, electricidad no"),
    (564419, "DE wild/naturaleza — sin servicios, alquiler privado"),
]


async def main() -> int:
    _load_dotenv()
    sys.stdout = open(sys.argv[1] if len(sys.argv) > 1 else "docs/validation/smoke_v4_categories.txt",
                      "w", encoding="utf-8")

    # Imports tras la apertura del archivo (los logs van a stderr no afectan)
    from enrichment.gemini_response_parser import parse_enrichment_response
    from enrichment.llm_provider import call_deepseek_sync, estimate_cost
    from enrichment.prompts import ENRICHMENT_VERSION, build_spot_user_prompt
    from enrichment.spot_packager import (
        fetch_reviews_for_enrichment,
        fetch_spot_for_enrichment,
        select_reviews_for_prompt,
    )

    print(f"SMOKE v{ENRICHMENT_VERSION} — DeepSeek, 4 categorías\n")
    print("=" * 80)

    conn = await asyncpg.connect(dsn=_dsn())
    try:
        total_cost = 0.0
        for spot_id, descr in SPOTS:
            print(f"\n{'#' * 80}")
            print(f"# SPOT {spot_id} — {descr}")
            print(f"{'#' * 80}\n")

            spot = await fetch_spot_for_enrichment(conn, spot_id)
            if not spot:
                print(f"NO ENCONTRADO: {spot_id}")
                continue

            reviews_raw = await fetch_reviews_for_enrichment(conn, spot_id)
            selected = select_reviews_for_prompt(reviews_raw)
            user_prompt = build_spot_user_prompt(spot, selected)

            print(f"name: {spot['canonical_name']!r}  [{spot.get('country_iso','?').upper()}]  type={spot.get('tipo')}")
            print(f"reviews: total={len(reviews_raw)} selected_after_dedup={len(selected)}")
            print(f"prompt chars={len(user_prompt)}  tokens~{len(user_prompt)//4}")
            print()

            # Servicios resumidos
            services = []
            for k in ("gratuito", "agua_potable", "electricidad", "vaciado_grises",
                      "vaciado_negras", "ducha", "wifi", "wc_publico"):
                v = spot.get(k)
                if v is not None:
                    services.append(f"{k}={v}")
            print("SERVICES (raw): " + ", ".join(services))
            print()

            t0 = time.time()
            try:
                resp = await asyncio.to_thread(call_deepseek_sync, user_prompt,
                                               model="deepseek-v4-flash")
                lat = time.time() - t0
                cost = estimate_cost(resp.model, resp.usage)
                total_cost += cost

                print(f"--- response (latency={lat:.1f}s, in={resp.usage.get('prompt_token_count')}, "
                      f"out={resp.usage.get('candidates_token_count')}, cost=${cost:.5f}) ---\n")

                parsed = parse_enrichment_response(resp.text)
                print(f"CLAIMS ({len(parsed.claims)}):")
                # Categorizar: services-only / inferred from reviews
                from_services = [c for c in parsed.claims if c.review_id is None and
                                 c.signal in ("water_working", "electricity_working", "dump_station_working")]
                from_reviews = [c for c in parsed.claims if c.review_id is not None]
                from_other = [c for c in parsed.claims
                              if c not in from_services and c not in from_reviews]
                print(f"  - from services: {len(from_services)}")
                print(f"  - from reviews:  {len(from_reviews)}")
                print(f"  - from descriptions/other: {len(from_other)}")
                print()

                for i, c in enumerate(parsed.claims, 1):
                    src = f"rid={c.review_id}" if c.review_id else "(services/desc)"
                    print(f"  {i:2}. [{c.signal:<28}]  val={str(c.value):<18}  conf={c.confidence}  {src}")
                    if c.excerpt:
                        # Detect language of excerpt vs original review language (idioma)
                        ex = c.excerpt[:100].replace('\n', ' ')
                        print(f"       excerpt: {ex!r}")

                if parsed.errors:
                    print(f"\nPARSER WARNINGS ({len(parsed.errors)}):")
                    for e in parsed.errors:
                        print(f"  - {e}")

                print(f"\nSUMMARY (English):\n  {parsed.summary or '(empty)'}")
                print(f"\nTAGS:     {parsed.tags}")
                print(f"BEST_FOR: {parsed.best_for}")
                if parsed.best_season:
                    print(f"BEST_SEASON: {parsed.best_season}")
                if parsed.avoid_season:
                    print(f"AVOID_SEASON: {parsed.avoid_season}")

            except Exception as exc:
                print(f"FAILED: {exc}")

        print(f"\n{'=' * 80}")
        print(f"TOTAL COST (4 spots): ${total_cost:.5f}")
        print(f"PROJECTED 80K spots: ${total_cost / 4 * 80_000:.2f}")
    finally:
        await conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
