"""CamperContact — scraper usando API interna del mapa."""

from sources.base import AbstractSource

TIPO_MAP = {
    "camperplace": "area_ac", "camping": "camping", "parking": "parking",
    "motorhome": "area_ac", "service": "area_ac", "nature": "naturaleza",
    "wild": "naturaleza", "picnic": "picnic",
}

class CamperContactSource(AbstractSource):
    name = "campercontact"
    rate_limit = 0.3
    grid_step = 1.0
    dedup_radius_m = 80.0

    HEADERS = {
        "accept": "*/*",
        "accept-language": "en",
        "origin": "https://www.campercontact.com",
        "referer": "https://www.campercontact.com/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "x-feature-flags": "microcamping",
    }

    BASE_URL = "https://services.campercontact.com/search/results/list"

    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        from datetime import datetime, timedelta
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        params = {
            "topleft_lat": tl_lat, "topleft_lon": tl_lon,
            "bottomright_lat": br_lat, "bottomright_lon": br_lon,
            "fromDate": today, "toDate": tomorrow,
            "persons": "2", "babies": "0", "pets": "0",
        }
        try:
            r = await client.get(self.BASE_URL, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return []

        items = data.get("items", [])
        total = data.get("total", {}).get("value", 0)

        # Subdivide si >50 y la celda es divisible
        if total > 50 and (tl_lat - br_lat) > 0.1:
            mid_lat = round((tl_lat + br_lat) / 2, 4)
            mid_lon = round((tl_lon + br_lon) / 2, 4)
            results = []
            for cell in [
                (tl_lat, tl_lon, mid_lat, mid_lon),
                (tl_lat, mid_lon, mid_lat, br_lon),
                (mid_lat, tl_lon, br_lat, mid_lon),
                (mid_lat, mid_lon, br_lat, br_lon),
            ]:
                results.extend(await self.fetch_cell(client, *cell))
            return results

        return items

    def normalize(self, raw: dict) -> dict | None:
        loc = raw.get("location", {})
        lat, lon = loc.get("lat"), loc.get("lon")
        if lat is None or lon is None:
            return None

        filters = raw.get("filters", {})
        price_range = raw.get("priceRange", {})
        poi_type = filters.get("poiType", raw.get("type", ""))

        # Tipo
        tipo = "otro"
        for k, v in TIPO_MAP.items():
            if k in (poi_type or "").lower():
                tipo = v
                break

        # Gratuito
        gratuito = None
        if price_range:
            mn = price_range.get("min", -1)
            if mn == 0:
                gratuito = True
            elif mn > 0:
                gratuito = False

        # Ciudad / país del subtitle
        subtitle = raw.get("subtitle", "")
        ciudad, pais = None, None
        if subtitle:
            parts = [p.strip() for p in subtitle.split(",")]
            ciudad = parts[0] if parts else None
            pais = parts[-1] if len(parts) >= 2 else None

        cc_id = str(raw.get("sitecode") or raw.get("id", ""))
        if not cc_id:
            return None

        return {
            "source_id": cc_id,
            "nombre": (raw.get("title") or "Sin nombre").strip()[:200],
            "lat": lat,
            "lon": lon,
            "tipo": tipo,
            "gratuito": gratuito,
            "precio_info": (
                f"min: {price_range.get('min')} / max: {price_range.get('max')}"
                if price_range else None
            ),
            "rating_promedio": filters.get("rating"),
            "num_reviews": filters.get("numberOfReviews"),
            "num_plazas": filters.get("maxCamperSpots"),
            "region": ciudad,
            "country_iso": pais,
            "web": (
                "https://www.campercontact.com" + raw.get("permalink", "")
                if raw.get("permalink") else None
            ),
        }
