import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect('postgresql://geospots:camperbot_local_dev_2026@db:5432/geospots')
    try:
        rows = await conn.fetch("""
            SELECT id, source, job_type, status, created_at, started_at, finished_at, progress
            FROM scraper_jobs
            WHERE status != 'done'
            ORDER BY id DESC
        """)
        for r in rows:
            print(dict(r))
    finally:
        await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
