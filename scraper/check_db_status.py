import asyncio
import os
import sys
sys.path.append(os.path.dirname(__file__))
from config import Config
import asyncpg

async def check():
    cfg = Config.from_env()
    pool = await asyncpg.create_pool(cfg.db_dsn)
    
    async with pool.acquire() as conn:
        # Total spots with web
        total_with_web = await conn.fetchval("SELECT COUNT(*) FROM spots WHERE web IS NOT NULL")
        
        # Recent updates (updated in the last 15 minutes)
        recent_updates = await conn.fetch("""
            SELECT id, canonical_name, web, updated_at
            FROM spots
            WHERE web IS NOT NULL AND updated_at > NOW() - INTERVAL '15 minutes'
            ORDER BY updated_at DESC
            LIMIT 10
        """)
        
        # Total spots active with web IS NULL
        total_without_web = await conn.fetchval("SELECT COUNT(*) FROM spots WHERE web IS NULL AND activo = TRUE")

        # Total spots in Spain with web IS NULL
        spain_without_web = await conn.fetchval("SELECT COUNT(*) FROM spots WHERE country_iso = 'es' AND web IS NULL AND activo = TRUE")
        print(f"Total spots in Spain without web: {spain_without_web}")

        # Total spots in Spain with web IS NULL and tipo in ('camping', 'area_ac')
        spain_camping_ac = await conn.fetchval("SELECT COUNT(*) FROM spots WHERE country_iso = 'es' AND web IS NULL AND activo = TRUE AND tipo IN ('camping', 'area_ac')")
        print(f"Total spots in Spain without web (camping & area_ac): {spain_camping_ac}")

        # Current DB Time
        db_now = await conn.fetchval("SELECT NOW()")
        print(f"Current DB Time: {db_now}")

        # Spot 85042 details
        spot_debug = await conn.fetchrow("SELECT id, canonical_name, web, updated_at FROM spots WHERE id = 85042")
        print(f"Spot 85042: {spot_debug}")

        # Search for Las Claras
        las_claras = await conn.fetch("SELECT id, canonical_name, web, updated_at FROM spots WHERE canonical_name ILIKE '%Las Claras%'")
        print(f"Las Claras spots found: {las_claras}")

        # Top 5 spots that recover_web.py should select
        top_5 = await conn.fetch("""
            SELECT id, canonical_name, lat, lon, region, country_iso, web, master_rating
            FROM spots 
            WHERE web IS NULL AND activo = TRUE 
            ORDER BY master_rating DESC NULLS LAST
            LIMIT 5
        """)
        print("\nTop 5 spots selected by query:")
        for r in top_5:
            print(f"  - [{r['id']}] {r['canonical_name']} (Rating: {r['master_rating']}, Web: {r['web']})")

        print(f"Total spots with web: {total_with_web}")
        print(f"Total active spots remaining without web: {total_without_web}")
        print(f"\nRecent updates in the last 15 minutes: {len(recent_updates)}")
        for r in recent_updates:
            print(f"  - [{r['id']}] {r['canonical_name']} -> {r['web']} (Updated at: {r['updated_at']})")
            
    await pool.close()

if __name__ == "__main__":
    asyncio.run(check())
