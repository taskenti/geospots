import asyncio
import asyncpg
import json
import sys
sys.path.insert(0, "/app")
from config import Config

async def main():
    config = Config.from_env()
    pool = await asyncpg.create_pool(dsn=config.db_dsn, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM source_records WHERE source='promobil' AND review_count > 0"
        )
        print(f"Spots with reviews > 0: {total}")

        sample = await conn.fetchrow(
            "SELECT source_id, review_count, raw_data FROM source_records "
            "WHERE source='promobil' AND review_count > 0 ORDER BY review_count DESC LIMIT 1"
        )
        if sample:
            rd = json.loads(sample["raw_data"])
            print(f"source_id: {sample['source_id']}")
            print(f"review_count: {sample['review_count']}")
            url_keys = {k: rd.get(k) for k in ["slug","path","url","link","detailUrl","pitchUrl","alias"] if rd.get(k)}
            print(f"URL keys: {url_keys}")
            de = rd.get("_de") or {}
            if isinstance(de, dict):
                de_url = {k: de.get(k) for k in ["slug","path","url","alias"] if de.get(k)}
                print(f"_de url keys: {de_url}")
            # Print all raw_data keys
            print(f"All raw_data keys: {list(rd.keys())}")

        cnt = await conn.fetchval("SELECT COUNT(*) FROM reviews WHERE source='promobil'")
        print(f"Promobil reviews in DB: {cnt}")

        fetched = await conn.fetchval(
            "SELECT COUNT(*) FROM source_records "
            "WHERE source='promobil' AND (normalized_data->>'reviews_fetched')='true'"
        )
        print(f"Marked reviews_fetched=true: {fetched}")

    await pool.close()

asyncio.run(main())
