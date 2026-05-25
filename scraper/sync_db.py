import asyncio
from config import Config
from db import create_pool

async def main():
    print("Iniciando sincronización de fuentes_config...")
    config = Config.from_env()
    pool = await create_pool(config)
    
    async with pool.acquire() as conn:
        print("Conectado a la base de datos interna.")
        
        # 1. Insertar fuentes faltantes
        print("Registrando nuevas fuentes en fuentes_config...")
        await conn.execute("""
            INSERT INTO fuentes_config (nombre, activa, spots_totales)
            VALUES 
              ('portugaleasycamp', true, 0),
              ('campspace', true, 0),
              ('wtmg', true, 0),
              ('roadsurfer', true, 0),
              ('vansite', true, 0),
              ('caramaps', true, 0),
              ('stayfree', true, 0),
              ('promobil', true, 0),
              ('womostell', true, 0),
              ('thedyrt', true, 0),
              ('campingcarinfos', true, 0)
            ON CONFLICT (nombre) DO NOTHING;
        """)
        
        # 2. Sincronizar spots_totales reales
        print("Sincronizando contadores de spots_totales...")
        await conn.execute("""
            UPDATE fuentes_config fc
            SET spots_totales = (SELECT COUNT(*) FROM source_records sr WHERE sr.source = fc.nombre)
            WHERE fc.nombre IN ('ioverlander', 'park4night', 'portugaleasycamp', 'campspace', 'caramaps', 'stayfree', 'promobil', 'alpacacamping', 'womostell', 'thedyrt', 'campingcarinfos');
        """)
        
        print("Sincronización completada con éxito!")
        
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
