import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect('postgresql://geospots:camperbot_local_dev_2026@db:5432/geospots')
    try:
        res = await conn.execute("""
            UPDATE scraper_jobs
            SET status = 'pending', started_at = NULL, finished_at = NULL, progress = NULL, result = NULL
            WHERE id IN (43, 44)
        """)
        print(f"Requeuing result: {res}")
    finally:
        await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
