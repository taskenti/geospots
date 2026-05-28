import asyncio
import re
import urllib.parse
from bs4 import BeautifulSoup
import httpx
import argparse
from loguru import logger
import sys
import os

sys.path.append(os.path.dirname(__file__))
from config import Config
import asyncpg

# Dominios que nunca queremos recuperar como web oficial
PROHIBITED_DOMAINS = {
    "park4night.com", "campercontact.com", "searchforsites.co.uk",
    "tripadvisor", "facebook.com", "instagram.com", "booking.com",
    "pitchup.com", "google.com", "caramaps.com", "camping.info",
    "youtube.com", "twitter.com", "x.com", "tiktok.com"
}

def is_clean_url(url: str) -> bool:
    if not url: return False
    url_lower = url.lower()
    for d in PROHIBITED_DOMAINS:
        if d in url_lower: return False
    return True

def extract_from_text(row) -> str | None:
    url_pattern = re.compile(r'https?://[^\s\"\'\}\,]+|www\.[^\s\"\'\}\,]+')
    se = str(row['servicios_extras'] or '')
    desc = str(row['descripcion_en'] or '') + ' ' + str(row['descripcion_es'] or '') + ' ' + str(row['descripcion_de'] or '')
    
    match = url_pattern.search(se) or url_pattern.search(desc)
    if match:
        url = match.group(0).rstrip(').,"\'')
        if is_clean_url(url):
            # Normalizamos si le falta el http
            if url.startswith('www.'):
                url = 'http://' + url
            return url
    return None

async def extract_from_osm(client: httpx.AsyncClient, lat: float, lon: float) -> str | None:
    overpass_url = "http://overpass-api.de/api/interpreter"
    query = f"""
    [out:json][timeout:10];
    (
      node["tourism"~"caravan_site|camp_site|camp_pitch"](around:50,{lat},{lon});
      way["tourism"~"caravan_site|camp_site|camp_pitch"](around:50,{lat},{lon});
    );
    out tags;
    """
    try:
        resp = await client.post(overpass_url, data={'data': query}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for element in data.get("elements", []):
                tags = element.get("tags", {})
                web = tags.get("website") or tags.get("contact:website")
                if web and is_clean_url(web):
                    if web.startswith('www.'):
                        web = 'http://' + web
                    return web
    except Exception as e:
        logger.debug(f"OSM error: {e}")
    return None

async def extract_from_duckduckgo(client: httpx.AsyncClient, name: str, region: str, country: str) -> str | None:
    query = f"{name} {region or ''} {country or ''} camping sitio oficial"
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = await client.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            for a in soup.select("a.result__url"):
                href = a.get('href')
                if href and 'uddg=' in href:
                    try:
                        actual_url = urllib.parse.unquote(href.split('uddg=')[1].split('&')[0])
                        if is_clean_url(actual_url):
                            return actual_url
                    except IndexError:
                        pass
    except Exception as e:
        logger.debug(f"DDG error: {e}")
    return None

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    
    cfg = Config.from_env()
    pool = await asyncpg.create_pool(cfg.db_dsn)
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(f'''
            SELECT id, canonical_name, lat, lon, region, country_iso, servicios_extras, descripcion_en, descripcion_es, descripcion_de
            FROM spots 
            WHERE web IS NULL AND activo = TRUE 
            ORDER BY master_rating DESC NULLS LAST
            LIMIT {args.limit}
        ''')
        
    logger.info(f"Procesando {len(rows)} spots sin web")
    
    stats = {"regex": 0, "osm": 0, "ddg": 0, "failed": 0}
    
    async with httpx.AsyncClient() as client:
        for r in rows:
            spot_id = r['id']
            name = r['canonical_name']
            
            # Paso 0: Regex Local
            web = extract_from_text(r)
            source = "regex"
            
            # Paso 1: OSM
            if not web:
                web = await extract_from_osm(client, r['lat'], r['lon'])
                source = "osm"
                if web: await asyncio.sleep(0.5)
                
            # Paso 2: DuckDuckGo
            if not web:
                web = await extract_from_duckduckgo(client, name, r['region'], r['country_iso'])
                source = "ddg"
                await asyncio.sleep(2) # Respetar DDG rate limit
                
            if web:
                logger.success(f"[{spot_id}] {name} -> {web} ({source})")
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE spots SET web = $1, updated_at = NOW() WHERE id = $2", web, spot_id)
                stats[source] += 1
            else:
                logger.warning(f"[{spot_id}] {name} -> No web found")
                stats["failed"] += 1
                
    logger.info(f"Resultados finales: {stats}")
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
