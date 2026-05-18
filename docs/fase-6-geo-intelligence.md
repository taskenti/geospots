# Fase 6 — Geo Intelligence
## Análisis geoespacial avanzado: terreno, sol, stealth, acceso

---

## La Idea

Sin mirar UNA sola review ni foto, podemos inferir MUCHO de un spot solo con sus coordenadas y datos abiertos:

| Pregunta | Fuente de datos |
|---|---|
| ¿Tiene sombra por la mañana? | DEM (elevación) + cálculo solar + OSM árboles |
| ¿Es visible desde la carretera? | OSM roads + análisis de visibilidad |
| ¿Hay casas cerca? | OSM buildings |
| ¿Es empinado? | DEM pendiente |
| ¿Qué orientación tiene? | DEM aspect |
| ¿Hay cobertura móvil? | OpenCellID / estimación por densidad urbana |
| ¿A qué distancia está la autopista? | OSM highways |
| ¿Hay supermercados/gasolineras cerca? | OSM amenities |
| ¿Cuánta vegetación hay? | NDVI satelital / OSM landuse |
| ¿Es zona protegida? | OSM boundaries + datos EU |

**Todo esto es GRATUITO y computable offline.**

---

## Fuentes de Datos Abiertos

| Fuente | Datos | Resolución | Formato | Tamaño EU |
|---|---|---|---|---|
| **Copernicus DEM** | Elevación | 30m | GeoTIFF | ~50 GB |
| **OpenStreetMap** | Edificios, carreteras, landuse | Variable | PBF/GeoJSON | ~25 GB (planet) |
| **EU-DEM v1.1** | Elevación | 25m | GeoTIFF | ~40 GB |
| **Corine Land Cover** | Uso del suelo | 100m | GeoTIFF | ~2 GB |
| **OpenCellID** | Torres de telefonía | Puntos | CSV | ~1 GB |
| **SRTM** | Elevación (alternativa) | 90m | GeoTIFF | ~15 GB |
| **Sentinel-2 NDVI** | Vegetación | 10m | GeoTIFF | Bajo demanda |

> [!TIP]
> NO descargar todo Europa de golpe. Procesar por tiles/regiones bajo demanda. Cachear resultados por geohash.

---

## Tabla `spot_geo_analysis`

```sql
CREATE TABLE spot_geo_analysis (
    spot_id             INT PRIMARY KEY REFERENCES spots(id) ON DELETE CASCADE,

    -- ═══ TERRENO ═══
    elevation_m         REAL,       -- Altitud sobre el nivel del mar
    slope_degrees       REAL,       -- Pendiente del terreno
    aspect_degrees      REAL,       -- Orientación (0=N, 90=E, 180=S, 270=W)
    terrain_type        TEXT,       -- "flat", "hillside", "valley", "ridge", "coastal"
    terrain_roughness   REAL,       -- 0=llano, 1=muy irregular

    -- ═══ SOL ═══
    sun_morning_summer  REAL,       -- Horas de sol directo mañana (jun-ago)
    sun_afternoon_summer REAL,
    sun_morning_winter  REAL,       -- Horas de sol directo mañana (dic-feb)
    sun_afternoon_winter REAL,
    sunrise_visible     BOOLEAN,    -- ¿Se ve el amanecer? (no hay montaña al este)
    sunset_visible      BOOLEAN,    -- ¿Se ve el atardecer?

    -- ═══ STEALTH / VISIBILIDAD ═══
    dist_nearest_building_m  REAL,  -- Distancia al edificio más cercano
    dist_nearest_road_m      REAL,  -- Distancia a carretera más cercana
    road_type_nearest        TEXT,  -- "motorway", "primary", "secondary", "track"
    buildings_100m           INT,   -- Nº de edificios en radio 100m
    buildings_500m           INT,   -- Nº de edificios en radio 500m
    vegetation_cover         REAL,  -- 0-1, cobertura vegetal (NDVI o Corine)
    tree_canopy              REAL,  -- 0-1, estimación de dosel arbóreo
    visibility_score         REAL,  -- 0=muy oculto, 1=muy expuesto
    stealth_geo_score        REAL,  -- Score compuesto de discreción geográfica

    -- ═══ ACCESO ═══
    dist_motorway_km         REAL,  -- Distancia a autopista más cercana
    dist_fuel_station_km     REAL,  -- Distancia a gasolinera
    dist_supermarket_km      REAL,  -- Distancia a supermercado
    dist_hospital_km         REAL,  -- Distancia a hospital
    road_surface_access      TEXT,  -- "paved", "unpaved", "track", "unknown"

    -- ═══ ENTORNO ═══
    dist_coast_km            REAL,  -- Distancia a la costa
    dist_lake_km             REAL,  -- Distancia a lago/embalse
    dist_river_km            REAL,  -- Distancia a río
    landuse_type             TEXT,  -- "forest", "farmland", "urban", "scrub", "water"
    protected_area           BOOLEAN, -- Parque natural, reserva, etc.
    protected_area_name      TEXT,

    -- ═══ RUIDO ESTIMADO ═══
    noise_road               REAL,  -- 0-1 basado en distancia + tipo de carretera
    noise_urban              REAL,  -- 0-1 basado en densidad de edificios
    noise_combined           REAL,  -- Score combinado

    -- ═══ CONECTIVIDAD ═══
    cell_towers_1km          INT,   -- Torres de telefonía en 1km
    estimated_coverage       REAL,  -- 0-1 estimación de cobertura móvil

    -- ═══ METADATA ═══
    dem_source               TEXT,  -- "copernicus30m", "srtm90m"
    processed_at             TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Algoritmos Clave

### 1. Cálculo Solar (con pvlib o suncalc)

```python
from pvlib import solarposition
import pandas as pd

def calcular_horas_sol(lat: float, lon: float, elevation: float,
                       aspect: float, slope: float) -> dict:
    """Calcula horas de sol directo por temporada."""

    resultados = {}

    for season, months in [("summer", [6, 7, 8]), ("winter", [12, 1, 2])]:
        # Día representativo a mitad de temporada
        if season == "summer":
            times = pd.date_range("2026-07-15", periods=24*4, freq="15min", tz="UTC")
        else:
            times = pd.date_range("2026-01-15", periods=24*4, freq="15min", tz="UTC")

        solar = solarposition.get_solarposition(times, lat, lon, elevation)

        # Filtrar horas con sol sobre el horizonte
        sun_up = solar[solar["elevation"] > 0]

        # Mañana (antes de las 13:00 solar) y tarde
        morning = sun_up[sun_up.index.hour < 13]
        afternoon = sun_up[sun_up.index.hour >= 13]

        # Ajustar por pendiente/orientación del terreno
        # Si el spot mira al este (aspect~90), más sol por la mañana
        # Si mira al oeste (aspect~270), más sol por la tarde
        morning_factor = max(0, 1 - abs(aspect - 90) / 180) if slope > 5 else 1.0
        afternoon_factor = max(0, 1 - abs(aspect - 270) / 180) if slope > 5 else 1.0

        resultados[f"sun_morning_{season}"] = round(len(morning) / 4 * morning_factor, 1)
        resultados[f"sun_afternoon_{season}"] = round(len(afternoon) / 4 * afternoon_factor, 1)

    # Amanecer/atardecer visible (sin montaña bloqueando)
    resultados["sunrise_visible"] = aspect > 0 and aspect < 180  # Orientación este
    resultados["sunset_visible"] = aspect > 180 or aspect < 10   # Orientación oeste

    return resultados
```

### 2. Stealth Score Compuesto

```python
def calcular_stealth_score(geo: dict) -> float:
    """
    Score 0-1 de discreción basado en factores geográficos.
    0 = muy expuesto (centro ciudad, carretera principal)
    1 = invisible (bosque denso, lejos de todo)
    """
    factores = []

    # Distancia a edificios (más lejos = más discreto)
    dist_build = geo.get("dist_nearest_building_m", 0)
    if dist_build > 500: factores.append(1.0)
    elif dist_build > 200: factores.append(0.7)
    elif dist_build > 50: factores.append(0.4)
    else: factores.append(0.1)

    # Densidad de edificios en 500m
    builds_500 = geo.get("buildings_500m", 0)
    if builds_500 == 0: factores.append(1.0)
    elif builds_500 < 5: factores.append(0.7)
    elif builds_500 < 20: factores.append(0.3)
    else: factores.append(0.05)

    # Tipo de carretera más cercana
    road_type = geo.get("road_type_nearest", "unknown")
    road_scores = {
        "motorway": 0.1, "primary": 0.2, "secondary": 0.4,
        "tertiary": 0.6, "residential": 0.3, "track": 0.9,
        "path": 1.0, "unknown": 0.5
    }
    factores.append(road_scores.get(road_type, 0.5))

    # Cobertura vegetal (más vegetación = más oculto)
    veg = geo.get("vegetation_cover", 0.5)
    factores.append(veg)

    # Distancia a la carretera
    dist_road = geo.get("dist_nearest_road_m", 0)
    if dist_road > 200: factores.append(1.0)
    elif dist_road > 50: factores.append(0.6)
    elif dist_road > 10: factores.append(0.3)
    else: factores.append(0.1)

    return round(sum(factores) / len(factores), 3) if factores else 0.5
```

### 3. Estimación de Ruido

```python
def estimar_ruido(geo: dict) -> dict:
    """Estima ruido ambiental sin medición directa."""

    # Ruido de carretera
    dist_road = geo.get("dist_nearest_road_m", 1000)
    road_type = geo.get("road_type_nearest", "unknown")

    base_noise = {"motorway": 0.95, "primary": 0.7, "secondary": 0.5,
                  "tertiary": 0.3, "residential": 0.2, "track": 0.05}
    noise_road = base_noise.get(road_type, 0.3)

    # Atenuación por distancia (cada 100m pierde ~50%)
    attenuation = max(0, 1 - (dist_road / 500))
    noise_road *= attenuation

    # Ruido urbano
    builds = geo.get("buildings_500m", 0)
    noise_urban = min(1.0, builds / 50)

    return {
        "noise_road": round(noise_road, 3),
        "noise_urban": round(noise_urban, 3),
        "noise_combined": round(max(noise_road, noise_urban), 3),
    }
```

---

## Queries OSM con Overpass

```python
import httpx

async def query_osm_entorno(lat: float, lon: float, radio_m: int = 500) -> dict:
    """Consulta edificios, carreteras y amenities cercanas vía Overpass."""

    query = f"""
    [out:json][timeout:10];
    (
      way["building"](around:{radio_m},{lat},{lon});
      way["highway"](around:{radio_m},{lat},{lon});
      node["amenity"="fuel"](around:5000,{lat},{lon});
      node["shop"="supermarket"](around:5000,{lat},{lon});
      node["amenity"="hospital"](around:10000,{lat},{lon});
      way["boundary"="protected_area"](around:1000,{lat},{lon});
    );
    out center count;
    """

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=15
        )
        return r.json()
```

> [!WARNING]
> Overpass tiene rate limits. Para 500K spots, usar un dump PBF local con `osmium` + PostGIS en vez de queries live.

---

## Pipeline Batch con DEM

```python
import rasterio
from rasterio.transform import rowcol
import numpy as np

class DEMAnalyzer:
    def __init__(self, dem_path: str):
        self.dem = rasterio.open(dem_path)

    def get_elevation(self, lat: float, lon: float) -> float:
        row, col = rowcol(self.dem.transform, lon, lat)
        return float(self.dem.read(1)[row, col])

    def get_slope_aspect(self, lat: float, lon: float, window=3) -> tuple:
        """Calcula pendiente y orientación en un punto."""
        row, col = rowcol(self.dem.transform, lon, lat)

        # Ventana 3x3 alrededor del punto
        data = self.dem.read(1, window=rasterio.windows.Window(
            col - window//2, row - window//2, window, window
        ))

        # Gradientes
        dy, dx = np.gradient(data, self.dem.res[1], self.dem.res[0])
        slope = np.degrees(np.arctan(np.sqrt(dx[1,1]**2 + dy[1,1]**2)))
        aspect = np.degrees(np.arctan2(-dx[1,1], dy[1,1]))
        if aspect < 0:
            aspect += 360

        return round(slope, 2), round(aspect, 2)
```

---

## Almacenamiento DEM Inteligente

NO descargar todo el DEM de Europa (50 GB).

**Estrategia: tiles bajo demanda + caché**

```python
DEM_CACHE_DIR = Path("/data/dem_cache")

async def get_dem_tile(lat: float, lon: float) -> Path:
    """Descarga el tile DEM de 1°×1° que contiene el punto."""
    tile_lat = int(lat)
    tile_lon = int(lon)
    ns = "N" if tile_lat >= 0 else "S"
    ew = "E" if tile_lon >= 0 else "W"

    filename = f"Copernicus_DSM_30_{ns}{abs(tile_lat):02d}_{ew}{abs(tile_lon):03d}.tif"
    local = DEM_CACHE_DIR / filename

    if local.exists():
        return local

    url = f"https://prism-dem-open.copernicus.eu/pd-desk-open-access/prismDownload/{filename}"
    # ... descargar ...

    return local
```

---

## Integración con Enrichment

Los datos geo se inyectan en el embedding textual:

```python
def enriquecer_texto_con_geo(texto: str, geo: dict) -> str:
    """Añade contexto geográfico al texto para embedding."""
    extras = []

    if geo.get("elevation_m"):
        extras.append(f"altitud {geo['elevation_m']:.0f}m")
    if geo.get("dist_coast_km") and geo["dist_coast_km"] < 5:
        extras.append("cerca de la costa")
    if geo.get("stealth_geo_score", 0) > 0.7:
        extras.append("lugar oculto y discreto")
    if geo.get("noise_combined", 1) < 0.2:
        extras.append("zona muy silenciosa")
    if geo.get("protected_area"):
        extras.append(f"dentro de {geo.get('protected_area_name', 'zona protegida')}")

    if extras:
        texto += ". " + ", ".join(extras)

    return texto
```

---

## Métricas de Éxito

| Métrica | Objetivo |
|---|---|
| Spots con geo analysis | ≥ 90% |
| Precisión stealth score | > 75% (validación manual 50 spots) |
| Precisión sun calc | > 85% |
| Tiempo proceso por spot | < 200ms (con DEM cacheado) |
| Almacenamiento DEM cache | < 10 GB (tiles bajo demanda) |
