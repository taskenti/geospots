# Fase 6 — Geo Intelligence
## Análisis geoespacial: terreno, sol, stealth, ruido y acceso como sensores computados

> **Prerrequisito**: Phase 3 operativa (pipeline de estimación de estado funcionando)
> **Paradigma**: Las coordenadas son otro sensor — observaciones computadas desde datos abiertos que alimentan `spot_semantic_state`.

---

## La Idea

Sin mirar UNA sola review ni foto, se puede inferir mucho de un spot solo con sus coordenadas + datos abiertos:

| Pregunta | Fuente de datos | Señal Phase 3 que alimenta |
|---|---|---|
| ¿Tiene sombra por la mañana? | DEM (elevación) + cálculo solar | `shade_morning`, `shade_afternoon` |
| ¿Es visible desde la carretera? | OSM roads + distancia | `stealth` |
| ¿Hay casas cerca? | OSM buildings | `stealth`, `quietness` |
| ¿Cuánto ruido hay? | Tipo + distancia carretera + edificios | `noise`, `road_noise`, `quietness` |
| ¿Es empinado? | DEM pendiente | `large_vehicle`, `road_quality` |
| ¿Es zona protegida? | OSM boundaries | `overnight_safe`, `police_risk` |
| ¿Cerca del mar/lago? | OSM + PostGIS | `sea_view`, `lake_nearby`, `beauty` |

**Todo gratuito y computable offline. Coste: $0.**

### Integración con Phase 3

La diferencia clave vs. el plan viejo: los datos geo **NO van a una tabla aislada**. Se inyectan como `extracted_claims` con `extractor='geo_computed_v1'` y alimentan el mismo `spot_semantic_state` que las reviews y fotos.

```
coordenadas + DEM + OSM → geo_analyzer.py → extracted_claims (extractor='geo_computed_v1')
                                           → observation_normalizer → state_aggregator
                                           → spot_semantic_state (UPSERT)
```

Las observaciones geo tienen **extraction_confidence alta** (0.90-0.95) porque son datos factuales, no opiniones. Y **no decaen temporalmente** — la pendiente del terreno no cambia.

---

## Datos en producción (Mayo 2026)

| Tabla | Estado |
|---|---|
| `spot_geo` | ✅ Existe, 30 columnas, **0 filas** |
| `spots` con coordenadas | **723,734** spots activos |
| DEM tiles descargados | **0** |
| OSM datos procesados | **0** |

---

## Tabla `spot_geo` (ya existe)

La tabla ya está definida y creada en la DB con esta estructura:

```sql
-- Ya existe en producción, NO crear de nuevo
-- spot_geo: 30 columnas para datos geoespaciales computados
-- PK: spot_id → spots(id) ON DELETE CASCADE

-- Terreno: elevation_m, slope_degrees, aspect_degrees, terrain_type
-- Sol: sun_morning_summer/winter, sun_afternoon_summer/winter
-- Stealth: dist_nearest_building_m, dist_nearest_road_m, road_type_nearest,
--          buildings_100m, buildings_500m, vegetation_cover, stealth_geo_score
-- Acceso: dist_motorway_km, dist_fuel_km, dist_supermarket_km, dist_hospital_km
-- Entorno: dist_coast_km, dist_lake_km, dist_river_km, landuse_type,
--          protected_area, protected_area_name
-- Ruido: noise_road, noise_urban, noise_combined
-- Meta: processed_at
```

**No se necesita migración SQL.** La tabla ya tiene todo lo necesario.

---

## Fuentes de Datos Abiertos

| Fuente | Datos | Resolución | Tamaño | Estrategia |
|---|---|---|---|---|
| **Copernicus DEM GLO-30** | Elevación | 30m | ~50 GB (EU) | Tiles bajo demanda + caché |
| **OpenStreetMap** | Edificios, carreteras, amenities, landuse, protected_areas | Variable | ~25 GB (planet PBF) | Overpass para batch pequeño, PBF local para batch masivo |
| **Corine Land Cover** | Uso del suelo | 100m | ~2 GB | Descarga completa (pequeño) |

> [!TIP]
> **NO descargar todo Europa de golpe.** Procesar por tiles/regiones bajo demanda. Cachear DEM por geohash. Para OSM, usar Overpass API para los primeros 10K spots, luego importar PBF a PostGIS local para batch masivo.

---

## Arquitectura del Pipeline

```
┌───────────────────────────────────────────────────────────┐
│  PASO 1: DEM ANALYSIS (dem_analyzer.py)                   │
│  - Descarga tile Copernicus 1°×1° bajo demanda           │
│  - Calcula: elevation, slope, aspect, terrain_type        │
│  - Cachea tiles en /data/dem_cache/                       │
└──────────────────────┬────────────────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────────────────┐
│  PASO 2: OSM ANALYSIS (osm_analyzer.py)                   │
│  - Overpass API (batch pequeño) o PBF local (masivo)      │
│  - Calcula: edificios cercanos, carreteras, amenities     │
│  - Distancias: costa, lago, río, motorway, fuel, super   │
│  - Detecta: protected areas, landuse type                 │
└──────────────────────┬────────────────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────────────────┐
│  PASO 3: COMPUTED SCORES (geo_scorer.py)                  │
│  - Calcula horas de sol con pvlib/suncalc                 │
│  - Estima ruido por distancia + tipo carretera            │
│  - Calcula stealth_geo_score compuesto                    │
│  - Guarda todo en spot_geo                                │
└──────────────────────┬────────────────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────────────────┐
│  PASO 4: CLAIMS INJECTION (geo_claim_injector.py)         │
│  - Convierte datos de spot_geo → extracted_claims         │
│  - extractor = 'geo_computed_v1'                          │
│  - extraction_confidence = 0.90-0.95                      │
│  - Marca spot_semantic_state como stale                   │
│  - El state_aggregator de Phase 3 hace el resto           │
└───────────────────────────────────────────────────────────┘
```

---

## Algoritmos

### 1. Cálculo Solar

```python
from pvlib import solarposition
import pandas as pd

def calcular_horas_sol(lat: float, lon: float, elevation: float,
                       aspect: float, slope: float) -> dict:
    """Calcula horas de sol directo por temporada."""
    resultados = {}

    for season, date_str in [("summer", "2026-07-15"), ("winter", "2026-01-15")]:
        times = pd.date_range(date_str, periods=24*4, freq="15min", tz="UTC")
        solar = solarposition.get_solarposition(times, lat, lon, elevation)

        sun_up = solar[solar["elevation"] > 0]
        morning = sun_up[sun_up.index.hour < 13]
        afternoon = sun_up[sun_up.index.hour >= 13]

        # Ajustar por pendiente/orientación
        morning_factor = max(0, 1 - abs(aspect - 90) / 180) if slope > 5 else 1.0
        afternoon_factor = max(0, 1 - abs(aspect - 270) / 180) if slope > 5 else 1.0

        resultados[f"sun_morning_{season}"] = round(len(morning) / 4 * morning_factor, 1)
        resultados[f"sun_afternoon_{season}"] = round(len(afternoon) / 4 * afternoon_factor, 1)

    return resultados
```

### 2. Stealth Score Compuesto

```python
def calcular_stealth_score(geo: dict) -> float:
    """
    Score 0-1 de discreción geográfica.
    0 = muy expuesto (centro ciudad, autopista al lado)
    1 = invisible (bosque denso, lejos de todo)
    """
    factores = []

    # Distancia a edificios
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

    # Cobertura vegetal
    factores.append(geo.get("vegetation_cover", 0.5))

    # Distancia a carretera
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

## Inyección de Claims Geográficos

El puente entre `spot_geo` y el pipeline de estimación Phase 3:

```python
def geo_to_claims(spot_id: int, geo: dict) -> list[dict]:
    """
    Convierte datos geográficos en extracted_claims para Phase 3.
    Estos claims tienen confidence alta y NO decaen temporalmente.
    """
    claims = []

    # Ruido → quietness (invertido: más ruido = menos quietness)
    noise = geo.get("noise_combined")
    if noise is not None:
        claims.append({
            "spot_id": spot_id,
            "signal_type": "quietness",
            "raw_value": str(round(1.0 - noise, 2)),  # Invertir: noise → quietness
            "extraction_confidence": 0.70,  # Menor que reviews directas
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"noise_road={geo.get('noise_road')}, "
                       f"noise_urban={geo.get('noise_urban')}, "
                       f"road={geo.get('road_type_nearest')}, "
                       f"dist={geo.get('dist_nearest_road_m')}m"
        })

    # Road noise específico
    if geo.get("noise_road") is not None:
        claims.append({
            "spot_id": spot_id,
            "signal_type": "road_noise",
            "raw_value": str(round(geo["noise_road"], 2)),
            "extraction_confidence": 0.85,
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"road_type={geo.get('road_type_nearest')}, dist={geo.get('dist_nearest_road_m')}m"
        })

    # Stealth geográfico
    stealth = geo.get("stealth_geo_score")
    if stealth is not None:
        claims.append({
            "spot_id": spot_id,
            "signal_type": "stealth",
            "raw_value": str(stealth),
            "extraction_confidence": 0.80,
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"buildings_500m={geo.get('buildings_500m')}, "
                       f"vegetation={geo.get('vegetation_cover')}, "
                       f"dist_road={geo.get('dist_nearest_road_m')}m"
        })

    # Vistas: mar, lago, montaña (inferido por proximidad, NO confirmado visualmente)
    if geo.get("dist_coast_km") is not None and geo["dist_coast_km"] < 1.0:
        claims.append({
            "spot_id": spot_id,
            "signal_type": "sea_view",
            "raw_value": "true",
            "extraction_confidence": 0.60,  # Baja: estar cerca NO garantiza vista
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"dist_coast={geo['dist_coast_km']:.1f}km"
        })

    if geo.get("dist_lake_km") is not None and geo["dist_lake_km"] < 0.5:
        claims.append({
            "spot_id": spot_id,
            "signal_type": "lake_nearby",
            "raw_value": "true",
            "extraction_confidence": 0.75,
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"dist_lake={geo['dist_lake_km']:.1f}km"
        })

    # Acceso vehículos grandes (basado en pendiente)
    slope = geo.get("slope_degrees")
    if slope is not None:
        if slope > 15:
            lv_score = 0.1  # Muy empinado, imposible
        elif slope > 8:
            lv_score = 0.4
        elif slope > 3:
            lv_score = 0.7
        else:
            lv_score = 0.95
        claims.append({
            "spot_id": spot_id,
            "signal_type": "large_vehicle",
            "raw_value": str(lv_score),
            "extraction_confidence": 0.75,
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"slope={slope}°, aspect={geo.get('aspect_degrees')}°"
        })

    # Zona protegida → riesgo de policía/pernocta
    if geo.get("protected_area") is True:
        claims.append({
            "spot_id": spot_id,
            "signal_type": "police_risk",
            "raw_value": "0.6",
            "extraction_confidence": 0.70,
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"protected_area: {geo.get('protected_area_name', 'desconocida')}"
        })
        claims.append({
            "spot_id": spot_id,
            "signal_type": "overnight_safe",
            "raw_value": "false",
            "extraction_confidence": 0.60,
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"protected_area: {geo.get('protected_area_name', 'desconocida')}"
        })

    # Belleza (heurística: costa + montaña + vegetación + no urbano)
    beauty_factors = []
    if geo.get("dist_coast_km", 999) < 2: beauty_factors.append(0.8)
    if geo.get("dist_lake_km", 999) < 1: beauty_factors.append(0.7)
    if geo.get("vegetation_cover", 0) > 0.6: beauty_factors.append(0.6)
    if geo.get("landuse_type") == "forest": beauty_factors.append(0.7)
    if geo.get("buildings_500m", 0) < 3: beauty_factors.append(0.5)
    if beauty_factors:
        claims.append({
            "spot_id": spot_id,
            "signal_type": "beauty",
            "raw_value": str(round(sum(beauty_factors) / len(beauty_factors), 2)),
            "extraction_confidence": 0.50,  # Muy especulativo
            "extractor_name": "geo_computed_v1",
            "extractor_version": "1.0",
            "excerpt": f"coast={geo.get('dist_coast_km')}km, veg={geo.get('vegetation_cover')}, "
                       f"landuse={geo.get('landuse_type')}"
        })

    return claims
```

> [!IMPORTANT]
> **extraction_confidence de geo claims es DELIBERADAMENTE más baja** que la de reviews directas para la mayoría de señales. Estar a 500m de la costa no garantiza vista al mar. Estar en zona protegida no garantiza multa. Los datos geo son **priors bayesianos** que las reviews y fotos pueden confirmar o contradecir. El `state_aggregator` de Phase 3 pondera todo automáticamente.

---

## Queries OSM con Overpass

```python
import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

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
        r = await client.post(OVERPASS_URL, data={"data": query}, timeout=15)
        return r.json()
```

> [!WARNING]
> Overpass tiene rate limits estrictos. Para batch masivo (723K spots), **NO usar Overpass**. Importar el PBF de Europa a PostGIS local con `osm2pgsql` y hacer queries espaciales directas. Overpass solo para testing y spots individuales bajo demanda.

### Estrategia de escala para OSM

| Escenario | Spots | Método | Tiempo estimado |
|---|---|---|---|
| Testing (100 spots) | 100 | Overpass API | ~5 min |
| Fase 6a: HOT spots | 30K | Overpass con throttling (1 req/s) | ~8 horas |
| Fase 6b: Todos | 723K | PBF local → PostGIS | ~4 horas (con índices) |

---

## DEM: Descarga de Tiles Bajo Demanda

```python
from pathlib import Path
import rasterio
from rasterio.transform import rowcol
import numpy as np

DEM_CACHE_DIR = Path("/data/dem_cache")

async def get_dem_tile(lat: float, lon: float) -> Path:
    """Descarga tile DEM Copernicus 1°×1° bajo demanda."""
    tile_lat = int(lat)
    tile_lon = int(lon)
    ns = "N" if tile_lat >= 0 else "S"
    ew = "E" if tile_lon >= 0 else "W"

    filename = f"Copernicus_DSM_30_{ns}{abs(tile_lat):02d}_{ew}{abs(tile_lon):03d}.tif"
    local = DEM_CACHE_DIR / filename

    if local.exists():
        return local

    # Descargar de Copernicus Open Access Hub
    url = f"https://prism-dem-open.copernicus.eu/pd-desk-open-access/prismDownload/{filename}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url)
        if r.status_code == 200:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(r.content)

    return local


class DEMAnalyzer:
    def __init__(self, dem_path: str):
        self.dem = rasterio.open(dem_path)

    def get_terrain(self, lat: float, lon: float) -> dict:
        """Calcula elevación, pendiente y orientación."""
        row, col = rowcol(self.dem.transform, lon, lat)
        elevation = float(self.dem.read(1)[row, col])

        # Ventana 3x3 para gradientes
        window = rasterio.windows.Window(col - 1, row - 1, 3, 3)
        data = self.dem.read(1, window=window)

        dy, dx = np.gradient(data, self.dem.res[1], self.dem.res[0])
        slope = np.degrees(np.arctan(np.sqrt(dx[1,1]**2 + dy[1,1]**2)))
        aspect = np.degrees(np.arctan2(-dx[1,1], dy[1,1]))
        if aspect < 0:
            aspect += 360

        # Clasificar terreno
        if slope < 2: terrain = "flat"
        elif slope < 8: terrain = "gentle"
        elif slope < 20: terrain = "hillside"
        else: terrain = "steep"

        return {
            "elevation_m": round(elevation, 1),
            "slope_degrees": round(slope, 2),
            "aspect_degrees": round(aspect, 2),
            "terrain_type": terrain
        }
```

### Almacenamiento DEM

| Región | Tiles necesarios | Tamaño estimado |
|---|---|---|
| Europa occidental (ES/FR/DE/IT/PT) | ~200 tiles | ~8 GB |
| Europa completa | ~500 tiles | ~20 GB |
| Bajo demanda (solo spots activos) | ~150 tiles | **~6 GB** |

---

## Integración con Embeddings (Phase 4)

Los datos geo enriquecen el texto para embedding en `construir_texto_para_embedding()`:

```python
def enriquecer_texto_con_geo(texto: str, geo: dict) -> str:
    """Añade contexto geográfico al texto de embedding."""
    extras = []

    if geo.get("elevation_m"):
        extras.append(f"altitud {geo['elevation_m']:.0f}m")
    if geo.get("dist_coast_km") is not None and geo["dist_coast_km"] < 5:
        extras.append("cerca de la costa")
    if geo.get("dist_lake_km") is not None and geo["dist_lake_km"] < 2:
        extras.append("cerca de un lago")
    if geo.get("stealth_geo_score", 0) > 0.7:
        extras.append("lugar oculto y discreto geográficamente")
    if geo.get("noise_combined", 1) < 0.2:
        extras.append("zona geográficamente silenciosa")
    if geo.get("protected_area"):
        extras.append(f"dentro de {geo.get('protected_area_name', 'zona protegida')}")
    if geo.get("terrain_type") == "flat":
        extras.append("terreno llano")
    if geo.get("landuse_type") == "forest":
        extras.append("en zona de bosque")

    if extras:
        texto += ". Geo: " + ", ".join(extras)

    return texto
```

Esta función se llama desde `embedding_generator.py` de Phase 4 para que los embeddings capturen también el contexto geográfico.

---

## Estructura de Archivos

```
c:\geospots\
├── enrichment/
│   ├── geo/                         # ← NUEVO submódulo
│   │   ├── __init__.py
│   │   ├── dem_analyzer.py          # Descarga tiles + elevación/slope/aspect
│   │   ├── osm_analyzer.py          # Overpass queries + parsing
│   │   ├── geo_scorer.py            # Stealth, ruido, sol, scores compuestos
│   │   └── geo_claim_injector.py    # spot_geo → extracted_claims
│   ├── embedding_generator.py       # Phase 4: MODIFICAR para incluir geo
│   └── ...
├── jobs/
│   └── batch_geo.py                 # ← NUEVO: batch geoanálisis
└── db/
    └── (no migration needed — spot_geo ya existe)
```

---

## Estimación de Costes

| Recurso | Coste |
|---|---|
| Copernicus DEM | **$0** (open data) |
| OpenStreetMap | **$0** (open data) |
| Corine Land Cover | **$0** (open data) |
| Overpass API | **$0** (gratis con rate limit) |
| Almacenamiento DEM cache | ~6 GB disco |
| Gemini API | **$0** (no se usa LLM en esta fase) |
| **Total** | **$0** |

### Tiempo de procesamiento

| Fase | Spots | Método | Tiempo |
|---|---|---|---|
| DEM (elevation/slope/aspect) | 723K | Rasterio batch | ~2 horas |
| OSM (edificios/carreteras) | 30K HOT | Overpass throttled | ~8 horas |
| OSM (todos) | 723K | PBF local + PostGIS | ~4 horas |
| Sol (pvlib) | 723K | CPU batch | ~6 horas |
| Scores compuestos | 723K | Python puro | ~30 min |
| Claims injection | 723K | SQL batch | ~15 min |

---

## Dependencias

```
# enrichment/requirements.txt — añadir
rasterio>=1.3.0             # Lectura de GeoTIFF (DEM)
pvlib>=0.11.0               # Cálculo de posición solar
numpy>=1.26.0               # Gradientes para slope/aspect
```

No se necesitan: GDAL CLI, osm2pgsql (solo si se hace PBF masivo), ni GPU.

---

## Métricas de Éxito

| Métrica | Objetivo |
|---|---|
| Spots con geo analysis completo | ≥ 90% de spots activos |
| Precisión stealth_geo_score | > 75% (validación manual 50 spots) |
| Precisión sun calc | > 85% (comparar con pvlib reference) |
| Claims geo inyectados | ≥ 5 claims/spot promedio |
| Tiempo proceso/spot (con DEM cacheado) | < 200ms |
| Almacenamiento DEM cache | < 10 GB |

---

## Orden de Implementación

1. **Crear** submódulo `enrichment/geo/`
2. **Implementar** `dem_analyzer.py` — descarga tiles + terrain analysis
3. **Test** con 100 spots: verificar elevation/slope/aspect correctos
4. **Implementar** `osm_analyzer.py` — Overpass queries
5. **Implementar** `geo_scorer.py` — stealth, ruido, sol
6. **Test** con 50 spots: validar stealth_score y noise manualmente
7. **Implementar** `geo_claim_injector.py` — spot_geo → extracted_claims
8. **Correr** `state_aggregator` → verificar que `spot_semantic_state` se actualiza con geo claims
9. **Batch Fase 6a**: 30K HOT spots (DEM + Overpass)
10. **Batch Fase 6b**: 723K spots (DEM + PBF local)
11. **Actualizar** `embedding_generator.py` (Phase 4) para incluir geo en texto de embedding
12. **Regenerar** embeddings de spots con geo data nuevo
