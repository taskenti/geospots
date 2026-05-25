# 🏕️ Campy Scraper

## 📖 Información General
**Campy** es una app de "microcamping" europea con foco en la región DACH (Alemania, Austria, Suiza), aunque también cubre puntos de Francia, Italia y los Países Bajos. Su nicho son granjas pequeñas, viñedos y terrenos privados que admiten campistas para estancias cortas (1-3 noches). Se diferencia de plataformas como Park4Night en que **todos los spots están verificados por el equipo** y son de pago a través de la propia app.

Cobertura observada en producción: ~150 spots por celda 1°×1° en zona alpina densa. Los tipos predominantes son `camp` (74%) y `microcamping` (12%).

## 🛠️ Arquitectura y Funcionamiento

### Endpoint GraphQL

```
POST https://graphql-server-132719581042.europe-west1.run.app/
Content-Type: application/json
```

Una sola query GraphQL `LocationsWithinRadius` con parámetros lat/lng/radius/filters. El scraper convierte cada celda del grid (1°×1°) en una llamada centrada con radio de 90km (cubre los ~78km de la diagonal de la celda con margen).

### Pipeline
- Sin `run()` propio: usa el de `AbstractSource`. El grid activo (dilatación dinámica de celdas con spots existentes) genera puntos centrales que se mapean a `fetch_cell` → POST GraphQL.
- Sin `download_reviews()`: la API pública de Campy no expone reseñas (el sistema de reviews es interno de la app y no se sirve por GraphQL anónimo).

## 🗂️ Mapeo y Normalización

### Tipos
La API devuelve tipos reales: `camp`, `van`, `microcamping`. Mapeo:
- `camp` / `camping` / `microcamping` → `camping`
- `van` → `area_ac`
- `parking` → `parking_publico` *(defensivo, no observado en producción)*
- otros → `naturaleza`

### Campos siempre poblados (validados sobre 150 spots reales)
| Campo Campy | GeoSpots | Notas |
|---|---|---|
| `uid` | `source_id` | string, formato `<timestamp><country>` |
| `title` | `nombre` | con fallback "Campy Spot" |
| `latitude`/`longitude` | `lat`/`lon` | con `coords_validas` (heredado de base) |
| `type` | `tipo` | mapeo arriba |
| `campsite_campy_rating` | `rating_promedio` | rating del equipo Campy (no users) |
| `image` | `fotos_urls[]` | 1 imagen principal |
| `description` | `descripcion_<lang>` | con detect_language |
| `address` / `city` | `region` | preferimos city, fallback address |

### Campos siempre nulos en la API pública (no usar)
- `price`: el precio solo se carga en el checkout privado de la app
- `places`, `rating`, `country`, `facilities[]`, `dateOpenFrom`, `dateOpenTo`, `camperSize`, `isTopQuality`
- Por eso `gratuito = None` (desconocido), nunca `False` por defecto

## 🔧 Auditoría y Fixes Mayo 2026

### Estado pre-auditoría
Scraper recién creado por Gemini, 150 source_records descargados, `fuentes_config.activa = false` (desactivada por el usuario antes de validar). 4 bugs detectados:

| # | Bug | Impacto | Fix |
|---|---|---|---|
| 1 | `microcamping` no mapeado → cae en "naturaleza" | 13 spots mal clasificados como naturaleza siendo agroturismo | Mapeo extendido. UPDATE SQL aplicado sobre los 13 ya en DB. |
| 2 | `gratuito = False` cuando `price` viene null | 37 spots informativos como "gratuitos" siendo desconocido (precio en checkout privado) | `gratuito = None` por defecto. UPDATE SQL aplicado sobre los 37 ya en DB. |
| 3 | `web = "https://campy.app/"` fijo (no específico del spot) | URL inútil que confunde al usuario en el panel | Eliminado, queda `None` hasta que Campy exponga permalinks |
| 4 | `rating` y `country` siempre null en API → 0 ratings capturados | Sin métricas de calidad ni clasificación geográfica desde la fuente | Uso de `campsite_campy_rating` (150/150 poblado). country queda al trigger PostGIS por lat/lon |

### Validación post-fix
Sobre 150 spots vivos de la API:
- `microcamping_as_camping`: 18/18 ✓
- `gratuito_None`: 150/150 ✓ (0 falsos positivos de gratuito)
- `rating_set`: 150/150 ✓
- `region_set`: 150/150 ✓
- DB roundtrip 30 spots: 0 errores

### Cleanup DB
- 37 `gratuito=False` falsos → NULL
- 13 spots `tipo=naturaleza` mal puestos por microcamping → `tipo=camping`
- `fuentes_config.activa` reactivado a `true`

---
**Estado Actual:** Auditado y operativo. La API es estable y los fixes preservan la integridad ante los campos siempre-null de la API pública.
