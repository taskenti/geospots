# Fase 1 — Data Empire
## Adquisición masiva: 10+ fuentes, 500K+ spots

---

## Objetivo

Pasar de 5 fuentes/125K spots a **10+ fuentes/500K+ spots**, cubriendo la totalidad de Europa con la mayor densidad de datos posible.

---

## Inventario de Fuentes

### Tier 1 — Joyas (ya integradas o en progreso)

| Fuente | Estado | Tipo acceso | Spots estimados | Valor |
|---|---|---|---|---|
| **Park4Night** | ✅ Integrado | API interna JSON | ~110K | ★★★★★ |
| **iOverlander** | ✅ Integrado | KMZ offline | ~31K (WW) | ★★★★☆ |
| **CamperContact** | 🔧 Listo | API interna `/search/results` | ~30K | ★★★★★ |
| **Furgovw** | ✅ Integrado | API JSON + scraping | ~4K (ES) | ★★★★☆ |
| **AreasAC** | ✅ Integrado | HTML scraping | ~600 (ES) | ★★★★☆ |
| **OSM** | ✅ Integrado | Overpass API | ~3K (tags camping) | ★★★☆☆ |

### Tier 2 — Siguiente oleada

| Fuente | Tipo acceso | Dificultad | Spots estimados | Cobertura |
|---|---|---|---|---|
| **CaraMaps** | API interna (bbox JSON) | ★★☆☆☆ | ~25K | Francia, España, Italia |
| **SearchForSites** | HTML SSR / API | ★★☆☆☆ | ~8K | UK, Europa occidental |
| **StayFree** | API interna (app móvil) | ★★★☆☆ | ~15K | Europa general |
| **Campy** | API interna | ★★★☆☆ | ~10K | Alemania, Austria, Suiza |

### Tier 3 — Expansión

| Fuente | Tipo acceso | Dificultad | Spots estimados | Cobertura |
|---|---|---|---|---|
| **Stellplatz Radar** | API/HTML | ★★★☆☆ | ~5K | DACH (Alemania/Austria/CH) |
| **Campernight** | API moderna | ★★☆☆☆ | ~5K | Europa general |
| **Roadsurfer Spots** | API interna | ★★★☆☆ | ~3K | Terrenos privados rurales |
| **NomadWiki/Wikicamps** | Mixto | ★★★★☆ | ~8K | Australasia + Europa |
| **Camping.info** | HTML SSR | ★★★☆☆ | ~30K | Campings oficiales Europa |

### Tier 4 — Datos abiertos (siempre disponibles)

| Fuente | Tipo acceso | Dificultad | Datos |
|---|---|---|---|
| **OpenStreetMap** (ampliado) | Overpass API | ★☆☆☆☆ | Tags: `tourism=camp_site`, `amenity=parking`, `tourism=caravan_site` |
| **Wikidata** | SPARQL | ★★☆☆☆ | Campings con coordenadas |
| **EU Open Data** | CSV/GeoJSON | ★☆☆☆☆ | Áreas de descanso autopista |

---

## Clasificación por Tipo de Acceso

### A) APIs internas (JSON/REST) — Lo mejor

```
Usuario mueve mapa → App hace fetch → Endpoint devuelve JSON
```

Ya dominamos esta técnica (P4N, CamperContact). Patrón:
1. DevTools → Network → Fetch/XHR
2. Mover mapa → Capturar request
3. Copy as cURL
4. Reproducir en Python con httpx

**Aplica a:** CaraMaps, StayFree, Campy, Roadsurfer, Campernight

### B) HTML SSR — Clásico

Páginas renderizadas en servidor. BeautifulSoup + httpx.

**Aplica a:** SearchForSites, Camping.info, Stellplatz Radar

### C) Tiles vectoriales / GeoJSON

Mapas que cargan datos como tiles (Mapbox/Leaflet):
```
/tiles/{z}/{x}/{y}.pbf
/api/geojson?bbox=...
```

**Aplica a:** Algunas versiones de CaraMaps, OSM

### D) Apps móviles (reverse engineering)

La técnica más potente. Muchas webs esconden datos, pero la app los expone.

```
1. mitmproxy como proxy HTTPS
2. Instalar CA cert en dispositivo/emulador
3. Usar la app normalmente
4. Capturar TODOS los endpoints
```

**Aplica a:** StayFree, Campy, apps que no tienen web pública

---

## Arquitectura del Directorio de Scrapers

```
scraper/
├── config.py              # Configuración central
├── db.py                  # ORM unificado
├── scheduler.py           # Orquestador
├── reconciliar.py         # Motor de reconciliación
│
├── sources/               # ← NUEVO: cada fuente aislada
│   ├── base.py            # Clase base AbstractSource
│   ├── park4night.py
│   ├── campercontact.py
│   ├── ioverlander.py
│   ├── caramaps.py
│   ├── searchforsites.py
│   ├── stayfree.py
│   ├── campy.py
│   ├── furgovw.py
│   ├── areasac.py
│   ├── osm.py
│   ├── stellplatz.py
│   ├── campernight.py
│   └── roadsurfer.py
│
├── raw/                   # ← NUEVO: almacén de datos crudos
│   └── {source}/{date}/   # JSON lines por celda
│
└── grid.py                # ← NUEVO: cuadrícula Europa reutilizable
```

### Clase base `AbstractSource`

```python
class AbstractSource(ABC):
    name: str
    rate_limit: float  # segundos entre requests
    grid_step: float   # grados por celda

    @abstractmethod
    async def fetch_cell(self, client, tl_lat, tl_lon, br_lat, br_lon) -> list[dict]:
        """Descarga spots de una celda bbox."""

    @abstractmethod
    def normalize(self, raw: dict) -> dict:
        """Convierte raw → formato canónico."""

    async def run(self, pool, config, log_id):
        """Pipeline completo: grid → fetch → normalize → dedup → store."""
```

---

## Cuadrícula Europa Optimizada

```python
# grid.py — Generador de celdas reutilizable
EU_BOUNDS = {
    "lat_min": 34.0,  # Sur de Creta
    "lat_max": 71.5,  # Norte de Noruega
    "lon_min": -25.0, # Azores
    "lon_max": 45.0,  # Urales
}

def generate_grid(step: float = 1.0):
    """Genera celdas (tl_lat, tl_lon, br_lat, br_lon)."""
    lat = EU_BOUNDS["lat_max"]
    while lat > EU_BOUNDS["lat_min"]:
        lon = EU_BOUNDS["lon_min"]
        while lon < EU_BOUNDS["lon_max"]:
            yield (
                round(lat, 4),
                round(lon, 4),
                round(lat - step, 4),
                round(lon + step, 4),
            )
            lon += step
        lat -= step
```

Tamaño de cuadrícula por densidad:
- **Zonas rurales (Escandinavia, Este):** 2° (~220 km)
- **Zonas medias (Francia, España rural):** 1° (~111 km)
- **Zonas densas (Costa, ciudades):** 0.5° (~55 km)
- **Zonas ultra-densas (Riviera, Costa del Sol):** 0.25° (~28 km)

---

## Pipeline RAW

```
fetch_cell() → raw JSON → guardar en raw/{source}/{date}.jsonl → normalize → dedup → DB
```

> [!IMPORTANT]
> **Todo raw se guarda SIEMPRE.** El fichero JSONL por fuente/fecha es el backup inmutable. Si la DB se corrompe, se puede reconstruir desde raw.

---

## Protección Anti-Ban

| Técnica | Implementación |
|---|---|
| Rate limiting | `asyncio.Semaphore(N)` + `asyncio.sleep(delay)` por fuente |
| User-Agent rotation | Pool de 10+ UAs reales (Chrome, Safari, Firefox) |
| Headers realistas | Copiar exactos de DevTools (referer, origin, sec-ch-ua) |
| Proxy rotation | Opcional: pool de proxies residenciales si hay bloqueos |
| Horario humano | No scrapear de 2am-6am (patrón bot) |
| Backoff exponencial | `tenacity` con retry 3x, backoff 2/4/8 segundos |
| Sesión persistente | Cookies + auth token renovable si necesario |

---

## Prioridades de Implementación

```
SEMANA 1-2: CaraMaps + SearchForSites (APIs fáciles, gran volumen)
SEMANA 3:   StayFree + Campy (reverse engineering app)
SEMANA 4:   Stellplatz + Campernight + Roadsurfer
SEMANA 5:   OSM ampliado + datos abiertos EU
SEMANA 6:   Consolidación, re-scrape completo, raw backup
```

---

## Métricas de Éxito

| Métrica | Objetivo |
|---|---|
| Fuentes activas | ≥ 10 |
| Spots totales | ≥ 500.000 |
| Cobertura geográfica | 100% Europa (todos los países) |
| Multi-fuente (≥2 fuentes) | ≥ 30% del total |
| Raw backup | 100% de todo lo descargado |
| Tasa de errores scraping | < 1% |
