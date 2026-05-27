"""Valida summary adaptativo: 3 spots de riqueza distinta.

Espera ver:
  - minimal spot: summary 1-2 frases
  - medium spot: 3-5 frases
  - very_rich camping: 6-8 frases con cobertura amplia
"""
import asyncio, os, sys, time
import asyncpg


def _ld():
    if not os.path.exists('.env'): return
    for line in open('.env'):
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1); os.environ.setdefault(k,v.strip().strip(chr(34)).strip(chr(39)))


def _dsn():
    _ld()
    return f"postgresql://{os.environ.get('POSTGRES_USER','geospots')}:{os.environ.get('POSTGRES_PASSWORD','geospots')}@{os.environ.get('DB_HOST','localhost')}:{os.environ.get('DB_PORT','25433')}/{os.environ.get('POSTGRES_DB','geospots')}"


# Candidatos por nivel esperado de riqueza
SPOTS = [
    (111566, "ES camping — esperado SIMPLE (3 reviews + todos servicios)"),
    (61376,  "SE area_ac — esperado MEDIUM (28 reviews + servicios mixtos)"),
    (111084, "DK Tornby — esperado RICH/VERY_RICH (60 reviews + 16 servicios)"),
]


async def main():
    sys.stdout = open(sys.argv[1] if len(sys.argv) > 1 else
                      "docs/validation/smoke_v4d_richness.txt",
                      "w", encoding="utf-8")

    from enrichment.gemini_response_parser import parse_enrichment_response
    from enrichment.llm_provider import call_deepseek_sync, estimate_cost
    from enrichment.prompts import ENRICHMENT_VERSION, build_spot_user_prompt
    from enrichment.spot_packager import (
        compute_richness,
        fetch_reviews_for_enrichment,
        fetch_spot_for_enrichment,
        select_reviews_for_prompt,
    )

    print(f"SMOKE v4d — richness-aware summary\n{'='*80}\n")

    conn = await asyncpg.connect(dsn=_dsn())
    try:
        for spot_id, descr in SPOTS:
            print(f"\n{'#'*80}\n# SPOT {spot_id} — {descr}\n{'#'*80}\n")

            spot = await fetch_spot_for_enrichment(conn, spot_id)
            reviews_raw = await fetch_reviews_for_enrichment(conn, spot_id)
            selected = select_reviews_for_prompt(reviews_raw)
            user_prompt = build_spot_user_prompt(spot, selected)

            score, level = compute_richness(spot, selected)
            print(f"COMPUTED richness: score={score} level={level}")
            print(f"reviews total={len(reviews_raw)} selected={len(selected)}")
            n_services = sum(
                1 for f in ("gratuito","precio_aprox","agua_potable","vaciado_grises","vaciado_negras",
                            "electricidad","ducha","wifi","wc_publico","perros","iluminacion","seguridad",
                            "reserva_req","num_plazas","altura_max_m","temporada_apertura",
                            "acceso_grandes","web","telefono","email","precio_info")
                if spot.get(f) is not None and spot.get(f) != ""
            )
            print(f"n_services_filled={n_services}/21")
            print(f"prompt chars={len(user_prompt)}")
            print()

            t0 = time.time()
            resp = await asyncio.to_thread(call_deepseek_sync, user_prompt, model="deepseek-v4-flash")
            lat = time.time() - t0
            cost = estimate_cost(resp.model, resp.usage)
            parsed = parse_enrichment_response(resp.text)

            print(f"--- response (latency={lat:.1f}s, in={resp.usage.get('prompt_token_count')}, "
                  f"out={resp.usage.get('candidates_token_count')}, cost=${cost:.5f}) ---")
            print(f"claims emitted: {len(parsed.claims)}")
            print(f"tags: {parsed.tags}")
            print(f"best_for: {parsed.best_for}")
            print(f"best_season: {parsed.best_season}")
            print(f"avoid_season: {parsed.avoid_season}")
            print()

            summary = parsed.summary or "(empty)"
            # Contar frases (split simple por ./!/?)
            import re
            sents = [s.strip() for s in re.split(r'[.!?]+', summary) if s.strip()]
            print(f"SUMMARY ({len(sents)} sentences, {len(summary)} chars):")
            print(f"  {summary}")

            # Validación del bucket
            expected = {
                "minimal": (1, 2),
                "simple": (2, 3),
                "medium": (3, 5),
                "rich": (5, 7),
                "very_rich": (6, 8),
            }.get(level, (0, 100))
            lo, hi = expected
            status = "OK" if lo - 1 <= len(sents) <= hi + 1 else "DEVIATES"
            print(f"\n  expected range for '{level}': {lo}-{hi} sentences → actual={len(sents)} [{status}]")

    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
