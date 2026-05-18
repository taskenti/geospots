import asyncio
import asyncpg
from config import Config

async def main():
    config = Config.from_env()
    pool = await asyncpg.create_pool(dsn=config.db_dsn, min_size=1, max_size=2)

    async with pool.acquire() as conn:
        # 1. Total spots
        total = await conn.fetchval("SELECT COUNT(*) FROM spots")
        print(f"\n=== TOTAL SPOTS EN DB: {total} ===\n")

        # 2. Spots por fuente
        rows = await conn.fetch("""
            SELECT unnest(fuentes) as fuente, COUNT(*) as cnt
            FROM spots GROUP BY fuente ORDER BY cnt DESC
        """)
        print("--- SPOTS POR FUENTE (en spots.fuentes[]) ---")
        for r in rows:
            print(f"  {r['fuente']}: {r['cnt']}")

        # 3. Source records por fuente
        rows2 = await conn.fetch("""
            SELECT source, COUNT(*) as cnt
            FROM source_records GROUP BY source ORDER BY cnt DESC
        """)
        print("\n--- SOURCE_RECORDS POR FUENTE ---")
        for r in rows2:
            print(f"  {r['source']}: {r['cnt']}")

        # 4. Spots con 1 sola fuente vs multi-fuente
        single = await conn.fetchval(
            "SELECT COUNT(*) FROM spots WHERE array_length(fuentes,1) = 1"
        )
        multi = await conn.fetchval(
            "SELECT COUNT(*) FROM spots WHERE array_length(fuentes,1) > 1"
        )
        print(f"\n--- DEDUP ---")
        print(f"  Spots con 1 fuente: {single}")
        print(f"  Spots multi-fuente: {multi}")

        # 5. Muestreo: 5 furgovw source_records con su spot asociado
        samples = await conn.fetch("""
            SELECT sr.source_id, sr.name as sr_name, sr.lat as sr_lat, sr.lon as sr_lon,
                   s.id as spot_id, s.canonical_name, s.lat as spot_lat, s.lon as spot_lon,
                   s.fuentes,
                   ST_Distance(s.geog, ST_SetSRID(ST_MakePoint(sr.lon, sr.lat), 4326)::geography) as dist_m
            FROM source_records sr
            JOIN spots s ON sr.spot_id = s.id
            WHERE sr.source = 'furgovw'
            ORDER BY RANDOM()
            LIMIT 5
        """)
        print("\n--- 5 FURGOVW ALEATORIOS CON SU SPOT ---")
        for s in samples:
            print(f"\n  Furgovw: {s['sr_name']} ({s['sr_lat']:.6f}, {s['sr_lon']:.6f})")
            print(f"  Spot:    {s['canonical_name']} ({s['spot_lat']:.6f}, {s['spot_lon']:.6f})")
            print(f"  Dist: {s['dist_m']:.2f}m | Fuentes: {s['fuentes']}")

        # 6. Reviews
        reviews = await conn.fetchval("SELECT COUNT(*) FROM reviews")
        reviews_furgovw = await conn.fetchval(
            "SELECT COUNT(*) FROM reviews WHERE source = 'furgovw'"
        )
        print(f"\n--- REVIEWS ---")
        print(f"  Total: {reviews}")
        print(f"  Furgovw: {reviews_furgovw}")

        # 7. Scraper log
        logs = await conn.fetch("""
            SELECT fuente, estado, iniciado_en, terminado_en,
                   spots_nuevos, spots_actualizados, reviews_nuevas, errores
            FROM scraper_log ORDER BY iniciado_en DESC LIMIT 10
        """)
        print(f"\n--- ULTIMOS SCRAPER LOGS ---")
        for l in logs:
            dur = ""
            if l['terminado_en'] and l['iniciado_en']:
                dur = f" ({(l['terminado_en']-l['iniciado_en']).total_seconds():.0f}s)"
            print(f"  {l['fuente']:15} | {l['estado']:15} | "
                  f"new={l['spots_nuevos']} upd={l['spots_actualizados']} "
                  f"rev={l['reviews_nuevas']} err={l['errores']}{dur}")

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
