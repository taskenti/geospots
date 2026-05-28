import asyncio, asyncpg, os
from dotenv import load_dotenv

async def main():
    load_dotenv(r"c:\geospots\.env")
    
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    db = os.environ.get("POSTGRES_DB", "postgres")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    
    dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
    print("Conectando con DSN:", dsn.replace(password, '***'))
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print("Error de conexion:", e)
        # fallback a postgres/postgres
        conn = await asyncpg.connect(f"postgresql://postgres:postgres@{host}:{port}/{db}")
        print("Conectado con usuario postgres")
    
    print('--- LOG DE SCRAPERS ---')
    logs = await conn.fetch("SELECT fuente, estado, spots_nuevos, reviews_nuevas FROM scraper_log WHERE estado = 'running'")
    for r in logs:
        print(dict(r))
        
    print('\n--- ESTADO REVIEWS ---')
    rows = await conn.fetch('''
        SELECT 
            source, 
            COUNT(r.id) as reviews_db, 
            SUM(sr.review_count) as expected
        FROM source_records sr
        LEFT JOIN reviews r ON r.source = sr.source AND r.spot_id = sr.spot_id
        WHERE sr.source IN ('campercontact', 'park4night', 'freecampsites', 'stayfree', 'alpacacamping')
        GROUP BY source
    ''')
    for r in rows:
        print(dict(r))
        
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
