import asyncio
import asyncpg
import json
import os

async def check():
    host = os.environ.get("DB_HOST", "db")
    port = os.environ.get("DB_PORT", "5432")
    db = os.environ.get("DB_NAME", "geospots")
    user = os.environ.get("DB_USER", "geospots")
    password = os.environ.get("DB_PASSWORD", "camperbot_local_dev_2026")
    
    dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}"
    conn = await asyncpg.connect(dsn=dsn)
    try:
        # Check running jobs
        rows = await conn.fetch("SELECT id, source, job_type, status, progress, started_at FROM scraper_jobs WHERE status = 'running'")
        print("--- Running Jobs ---")
        if not rows:
            print("No running jobs.")
        for r in rows:
            print(f"Job {r['id']}: source={r['source']} type={r['job_type']} status={r['status']} progress={r['progress']} started_at={r['started_at']}")
            
        # Count reviews by source
        print("\n--- Review Counts by Source ---")
        counts = await conn.fetch("SELECT source, COUNT(*) as cnt FROM reviews GROUP BY source")
        for c in counts:
            print(f"{c['source']}: {c['cnt']}")
    finally:
        await conn.close()

if __name__ == '__main__':
    asyncio.run(check())
