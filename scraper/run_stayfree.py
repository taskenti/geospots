import asyncio
import db
from scheduler import run_source
from config import Config

async def main():
    import scheduler
    c = Config.from_env()
    scheduler.pool = await db.create_pool(c)
    scheduler.config = c
    await run_source('stayfree')
    await scheduler.pool.close()

if __name__ == "__main__":
    asyncio.run(main())
