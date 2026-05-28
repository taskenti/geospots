import asyncio, httpx, os, json

async def main():
    auth = open('/app/stayfree_token.txt').read().strip()
    api_token = os.environ.get('STAYFREE_API_TOKEN', '')
    headers = {
        'Authorization': auth,
        'x-api-token': api_token,
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip, deflate',
    }
    sid = '67fecba72d678f3a44d2c18f'  # spot with 128 reviews
    base = f'https://api.stayfree.app/v1/spots/{sid}/reviews'
    
    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        # Try pagination params
        for params in [
            {},
            {'page': 0, 'limit': 50},
            {'page': 1, 'limit': 50},
            {'page': 0, 'size': 50},
            {'offset': 0, 'limit': 50},
            {'skip': 0, 'take': 50},
            {'limit': 200},
        ]:
            r = await client.get(base, params=params)
            d = r.json()
            count = len(d) if isinstance(d, list) else '?'
            print(f'params={params} -> HTTP {r.status_code} count={count}')

asyncio.run(main())
