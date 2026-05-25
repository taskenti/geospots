import asyncio
import asyncpg
from math import floor
import os

async def main():
    dsn = "postgresql://geospots:camperbot_local_dev_2026@localhost:25433/geospots"
    pool = await asyncpg.create_pool(dsn=dsn)
    
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT lat, lon FROM spots WHERE lat IS NOT NULL AND lon IS NOT NULL")
        
    step = 1.0
    existing_cells = set()
    for r in rows:
        lat = float(r['lat'])
        lon = float(r['lon'])
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            lat_idx = int(floor(lat / step))
            lon_idx = int(floor(lon / step))
            existing_cells.add((lat_idx, lon_idx))
            
    print(f"Celdas existentes únicas: {len(existing_cells)}")
    
    for buf in [0, 1, 2, 3, 4]:
        buffered = set()
        for lat_idx, lon_idx in existing_cells:
            for dlat in range(-buf, buf + 1):
                for dlon in range(-buf, buf + 1):
                    buffered.add((lat_idx + dlat, lon_idx + dlon))
        print(f"Buffer {buf} -> Celdas totales: {len(buffered)}")
        
    await pool.close()

if __name__ == '__main__':
    asyncio.run(main())
