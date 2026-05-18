import asyncio
import asyncpg
import json
from config import Config

async def main():
    config = Config.from_env()
    pool = await asyncpg.create_pool(
        dsn=config.db_dsn,
        min_size=1, max_size=2
    )
    
    async with pool.acquire() as conn:
        print("\n--- ANALIZANDO MATCHES DE FURGOVW ---")
        
        # Buscar 5 spots de ioverlander en source_records
        records = await conn.fetch("""
            SELECT spot_id, name, lat, lon 
            FROM source_records 
            WHERE source = 'ioverlander' 
            LIMIT 5
        """)
        
        for r in records:
            # Buscar el spot canónico al que se ha unido
            spot = await conn.fetchrow("""
                SELECT id, canonical_name, lat, lon, fuentes,
                       ST_Distance(
                           geog, 
                           ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
                       ) as dist_m
                FROM spots
                WHERE id = $3
            """, r['lat'], r['lon'], r['spot_id'])
            
            if spot:
                print(f"\niOverlander: {r['name']} ({r['lat']}, {r['lon']})")
                print(f"Match DB: {spot['canonical_name']} ({spot['lat']}, {spot['lon']})")
                print(f"Distancia: {spot['dist_m']:.2f} metros | Fuentes: {spot['fuentes']}")

if __name__ == "__main__":
    asyncio.run(main())
