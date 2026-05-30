"""Prototipo Tier-3: lee tags de acceso de OSM (vía Overpass) para spots dados.

Objetivo: demostrar que podemos derivar señal de exclusión POLARIDAD-CORRECTA
(4wd_only, tracktype, surface, maxheight, maxwidth) enganchando el spot a la vía/
barrera de acceso cercana. NO es producción: sin caché, sin retry, radios fijos.

Uso:  docker exec geospots-scraper python overpass_access_probe.py
"""
import asyncio
import httpx

OVERPASS = "https://overpass-api.de/api/interpreter"

# (id, lat, lon) muestra de spots tipo=wild en Iberia
SPOTS = [
    (25834, 42.15418, 0.33709),
    (25451, 43.34633, 1.12676),
    (27260, 38.40107, -7.36415),
    (26794, 40.23005, -0.85545),
    (25822, 42.41629, 0.14348),
    (29653, 40.17955, -6.62760),
]

# Tags OSM relevantes para acceso de vehículos grandes / 4x4
WAY_TAGS = ["highway", "tracktype", "surface", "smoothness", "4wd_only",
            "motor_vehicle", "access", "width", "maxweight"]
BARRIER_TAGS = ["barrier", "maxheight", "maxwidth"]

# superficies/tracktypes que sugieren acceso difícil para AC grande
ROUGH_SURFACE = {"dirt", "ground", "earth", "mud", "sand", "grass", "unpaved",
                 "fine_gravel", "gravel", "pebblestone", "rock"}
ROUGH_TRACK = {"grade3", "grade4", "grade5"}


def query(lat, lon):
    # vías a ≤120 m (vía de acceso plausible) + barreras a ≤60 m
    return f"""
    [out:json][timeout:25];
    (
      way(around:120,{lat},{lon})[highway];
      node(around:60,{lat},{lon})[barrier];
      way(around:80,{lat},{lon})[barrier];
    );
    out tags;
    """


def verdict(elements):
    """Resumen heurístico de exclusión a partir de los tags OSM."""
    flags = []
    has_4wd = has_height = has_rough = has_narrow = False
    for el in elements:
        t = el.get("tags", {})
        if t.get("4wd_only") == "yes" or t.get("motor_vehicle") == "4wd_only":
            has_4wd = True
        if t.get("tracktype") in ROUGH_TRACK or t.get("surface") in ROUGH_SURFACE:
            has_rough = True
        if "maxheight" in t:
            has_height = True
            flags.append(f"maxheight={t['maxheight']}")
        if "maxwidth" in t:
            flags.append(f"maxwidth={t['maxwidth']}")
        w = t.get("width")
        if w:
            try:
                if float(str(w).replace("m", "").strip()) < 2.5:
                    has_narrow = True
            except ValueError:
                pass
    if has_4wd:
        flags.append("4WD_ONLY")
    if has_rough:
        flags.append("rough_surface/track")
    if has_narrow:
        flags.append("narrow<2.5m")
    return flags


async def main():
    headers = {"User-Agent": "GeoSpots/1.0 (access-probe; osozarpas@gmail.com)",
               "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=40, headers=headers) as client:
        for sid, lat, lon in SPOTS:
            try:
                r = await client.post(OVERPASS, data={"data": query(lat, lon)})
                els = r.json().get("elements", [])
            except Exception as e:
                print(f"spot {sid}: ERROR {e}")
                continue
            # resumen de tipos de vía encontrados
            hw = sorted({e["tags"].get("highway") for e in els
                         if e.get("tags", {}).get("highway")})
            v = verdict(els)
            verdict_str = " | ".join(v) if v else "(sin señal de exclusión)"
            print(f"spot {sid} @{lat},{lon}: {len(els)} elems | "
                  f"highways={hw or '∅'} | -> {verdict_str}")
            await asyncio.sleep(2)  # cortesía con Overpass público


if __name__ == "__main__":
    asyncio.run(main())
