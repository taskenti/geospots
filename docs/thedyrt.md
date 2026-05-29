# ⛺ The Dyrt Scraper

**The Dyrt** es la plataforma de camping y pernocta al aire libre más grande de Estados Unidos. Cubre principalmente campings establecidos (públicos y privados), áreas de autocaravanas (RV parks) y áreas de acampada libre (dispersed camping) en Norteamérica.

El scraper `thedyrt.py` está estructurado en dos fases: un escaneo geográfico inicial rápido de spots y una segunda fase desacoplada de enriquecimiento de ficha técnica (detalles) y descarga de reseñas.

---

## 🛠️ Descubrimientos de Ingeniería Inversa

### 1. Fase 1: Endpoint de Búsqueda (BBox)

```
GET https://thedyrt.com/api/v9/locations/search-results
```

**Headers obligatorios:**
```http
accept: application/vnd.api+json
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...
```

**Parámetros de consulta:**
| Parámetro | Valor | Descripción |
|---|---|---|
| `filter[search][bbox]` | `sw_lon,sw_lat,ne_lon,ne_lat` | Delimitación del área de búsqueda georeferenciada |
| `sort` | `recommended` | Criterio de ordenación |
| `page[number]` | `1, 2, 3...` | Número de página (1-indexado) |
| `page[size]` | `500` | Tamaño de página (máximo 500) |

**Estructura de respuesta:**
Cumple con la especificación de JSON-API, devolviendo un array principal `data` y metadatos de paginación en `meta`:
- `meta.page-count`: Total de páginas.
- `meta.record-count`: Total de registros encontrados.

---

### 2. Fase 2: Enriquecimiento de Detalles por ID

```
GET https://thedyrt.com/api/v9/campgrounds/{location_id}
```
Retorna la ficha técnica detallada con amenities booleanas estructuradas y capacidad total de plazas.

---

### 3. Fase 2: Descarga de Reseñas

```
GET https://thedyrt.com/api/v9/reviews
```

**Parámetros de consulta:**
| Parámetro | Valor | Descripción |
|---|---|---|
| `filter[subject_id]` | `{location_id}` | ID del campground a consultar |
| `page[number]` | `1, 2...` | Paginación de reviews |
| `page[size]` | `100` | Lote de reviews por página |

---

## 🗂️ Mapeo y Normalización

### Tipos de Spots

Se deducen combinando `category`, `accommodation-dispersed` y `pin-type`:
- **`wild`**: Si `category == "dispersed"` o `accommodation-dispersed` es `True`.
- **`area_ac`**: Si `pin-type == "rv_park"`.
- **`camping`**: Para el resto de casos ( established public/private campings).

### Amenidades Mapeadas (Ficha Detallada)

| Atributo Nativa | Campo GeoSpots |
|---|---|
| `water-hookups` o `drinking-water` | `agua_potable` |
| `electric-hookups` / `thirty-amp-hookups` / `fifty-amp-hookups` | `electricidad` |
| `toilets` | `wc_publico` |
| `showers` | `ducha` |
| `wifi` | `wifi` |
| `pets-allowed` | `perros` |
| `big-rig-friendly` | `acceso_grandes` |
| `reservable` o `permit-required` | `reserva_req` |
| `sanitary-dump` | `vaciado_grises` y `vaciado_negras` |
| `campsites-count` | `num_plazas` |

---

## 🔄 Credibilidad en Reconciliación

En `reconciliar.py`, `thedyrt` posee la máxima prioridad para los campos textuales en inglés y una prioridad intermedia-alta para los campos geográficos en el continente americano:
- **`descripcion_en`:** Máxima prioridad (por encima de `park4night` y `stayfree`).
- **`canonical_name` / `tipo`:** Alta.
- **Rating / Reviews:** Alta (escala 1-5 directa).
- **Amenidades:** Media-alta.

---

## 🚦 Parámetros de Operación

- **Scraper (spots):** `rate_limit = 1.0s` entre peticiones de búsqueda. Utiliza un tamaño de cuadrícula activa de `grid_step = 2.0` grados.
- **Reintentos de gateway (2026-05-29):** `fetch_cell` reintenta los `502`/`503`/`504` transitorios con backoff exponencial (5s→10s→20s, máx 3 intentos) y resetea el contador tras éxito. Antes, un 502 puntual descartaba la celda entera.
- **Reviews & Detalles:** Limitador de trabajadores concurrentes a 3. Manejo inteligente de error `HTTP 429` (esperando 60s antes de reintentar) y rate limiting de `1.0s` por petición.
- **Dedup radius:** `100m`.
