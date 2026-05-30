"""Tier-3 — Medición de cobertura OSM para señal de acceso de vehículos.

Muestrea spots reales (estratificado por tipo), consulta Overpass por la vía/barrera de
acceso cercana y clasifica cada spot en: EXCLUSION / ACCESSIBLE / UNKNOWN. Sirve para
decidir GO/NO-GO de Phase 6 antes de construir el pipeline completo.

Uso:  docker exec geospots-enrichment python overpass_coverage.py --per-type 30
"""
import argparse
import asyncio
import os
import sys

import asyncpg
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from jobs.ingest_spot_facts import _dsn
except Exception:
    def _dsn():
        h = os.environ.get("DB_HOST", "db"); p = os.environ.get("DB_PORT", "5432")
        n = os.environ.get("POSTGRES_DB", "geospots"); u = os.environ.get("POSTGRES_USER", "geospots")
        pw = os.environ.get("POSTGRES_PASSWORD", "geospots")
        return f"postgresql://{u}:{pw}@{h}:{p}/{n}"

OVERPASS = "https://overpass-api.de/api/interpreter"
HEADERS = {"User-Agent": "GeoSpots/1.0 (coverage-probe; osozarpas@gmail.com)",
           "Accept": "application/json"}

ROUGH_SURFACE = {"dirt", "ground", "earth", "mud", "sand", "grass", "unpaved",
                 "fine_gravel", "gravel", "pebblestone", "rock", "compacted"}
ROUGH_TRACK = {"grade3", "grade4", "grade5"}
PAVED_CLASSES = {"primary", "secondary", "tertiary", "residential", "unclassified",
                 "living_street", "service", "trunk"}


def query(lat, lon):
    return f"""[out:json][timeout:25];
    (way(around:150,{lat},{lon})[highway];
     node(around:80,{lat},{lon})[barrier];
     way(around:80,{lat},{lon})[barrier];);
    out tags;"""


def classify(elements):
    """Devuelve (verdict, detalle). EXCLUSION gana sobre ACCESSIBLE; si no hay nada, UNKNOWN."""
    excl, access = [], False
    for el in elements:
        t = el.get("tags", {})
        hw = t.get("highway")
        if t.get("4wd_only") == "yes" or t.get("motor_vehicle") == "4wd_only":
            excl.append("4wd_only")
        if t.get("tracktype") in ROUGH_TRACK:
            excl.append(f"track_{t['tracktype']}")
        if t.get("surface") in ROUGH_SURFACE:
            excl.append(f"surface_{t['surface']}")
        if "maxheight" in t:
            try:
                if float(str(t["maxheight"]).replace("m", "").strip()) < 3.0:
                    excl.append(f"maxheight_{t['maxheight']}")
            except ValueError:
                excl.append("maxheight_?")
        if "maxwidth" in t:
            excl.append(f"maxwidth_{t['maxwidth']}")
        # señal de accesibilidad: vía pavimentada/normal cerca
        if hw in PAVED_CLASSES and t.get("surface") not in ROUGH_SURFACE:
            access = True
    if excl:
        return "EXCLUSION", ",".join(sorted(set(excl)))
    if access:
        return "ACCESSIBLE", ""
    return "UNKNOWN", ""


async def fetch(client, lat, lon, retries=2):
    for i in range(retries + 1):
        try:
            r = await client.post(OVERPASS, data={"data": query(lat, lon)})
            if r.status_code == 200:
                return r.json().get("elements", [])
            if r.status_code in (429, 504):
                await asyncio.sleep(5 * (i + 1))
                continue
            return None
        except Exception:
            await asyncio.sleep(3)
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-type", type=int, default=30)
    ap.add_argument("--types", default="wild,naturaleza,parking,area_ac")
    ap.add_argument("--delay", type=float, default=1.1)
    args = ap.parse_args()

    conn = await asyncpg.connect(_dsn())
    sample = []
    for tipo in args.types.split(","):
        rows = await conn.fetch(
            "SELECT id, lat, lon, $1::text tipo FROM spots WHERE tipo=$1 AND lat IS NOT NULL "
            "AND lat BETWEEN 35 AND 60 AND lon BETWEEN -10 AND 20 ORDER BY random() LIMIT $2",
            tipo, args.per_type)
        sample += [dict(r) for r in rows]
    await conn.close()

    stats = {}  # tipo -> {EXCLUSION, ACCESSIBLE, UNKNOWN}
    excl_kinds = {}
    async with httpx.AsyncClient(timeout=40, headers=HEADERS) as client:
        for s in sample:
            els = await fetch(client, s["lat"], s["lon"])
            tipo = s["tipo"]
            st = stats.setdefault(tipo, {"EXCLUSION": 0, "ACCESSIBLE": 0, "UNKNOWN": 0, "ERROR": 0})
            if els is None:
                st["ERROR"] += 1
            else:
                verdict, detail = classify(els)
                st[verdict] += 1
                if verdict == "EXCLUSION":
                    for k in detail.split(","):
                        kind = k.split("_")[0]
                        excl_kinds[kind] = excl_kinds.get(kind, 0) + 1
            await asyncio.sleep(args.delay)

    print("\n=== COBERTURA OSM por tipo de spot ===")
    print(f"{'tipo':<12}{'n':>4}{'EXCL':>7}{'ACCES':>7}{'UNK':>6}{'ERR':>5}")
    tot = {"EXCLUSION": 0, "ACCESSIBLE": 0, "UNKNOWN": 0, "ERROR": 0, "n": 0}
    for tipo, st in stats.items():
        n = sum(st.values())
        tot["n"] += n
        for k in st:
            tot[k] += st[k]
        print(f"{tipo:<12}{n:>4}{st['EXCLUSION']:>7}{st['ACCESSIBLE']:>7}{st['UNKNOWN']:>6}{st['ERROR']:>5}")
    n = tot["n"] or 1
    print(f"\nTOTAL n={tot['n']}: EXCLUSION={tot['EXCLUSION']} ({100*tot['EXCLUSION']//n}%) "
          f"ACCESSIBLE={tot['ACCESSIBLE']} ({100*tot['ACCESSIBLE']//n}%) "
          f"UNKNOWN={tot['UNKNOWN']} ({100*tot['UNKNOWN']//n}%) ERROR={tot['ERROR']}")
    print(f"Tipos de exclusión detectados: {excl_kinds}")


if __name__ == "__main__":
    asyncio.run(main())
