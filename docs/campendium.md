# 🏕️ Campendium Scraper

**Campendium** es una plataforma de reseñas de campings, áreas de RV, acampada libre y pernoctas en Norteamérica (EE.UU. y Canadá). Propiedad de Roadtrippers, ofrece un mapa interactivo con miles de POIs y reseñas detalladas de usuarios.

El scraper `campendium.py` está estructurado en dos fases: un escaneo por tiles de mapa (zoom 8) para descubrimiento rápido de spots, y una segunda fase desacoplada de enriquecimiento con detalles y descarga de reseñas.

---

## 🛠️ Descubrimientos de Ingeniería Inversa

### 1. Fase 1: Endpoint de Tiles (Mapa)

```
GET https://maps.campendium.com/api/v2/tiles/{zoom}/{x}/{y}?limit=80
```

**Headers obligatorios:**
```http
user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...
accept: application/json, text/javascript, */*; q=0.01
referer: https://maps.campendium.com/
```

**Parámetros:**
| Parámetro | Valor | Descripción |
|---|---|---|
| `zoom` | `8` | Nivel de zoom OSM (8 es óptimo para evitar truncamiento de 80 features) |
| `x`, `y` | `int` | Coordenadas de tile OSM |
| `limit` | `80` | Máximo de features por tile (límite del servidor) |

**Estructura de respuesta:**
GeoJSON `FeatureCollection` con propiedades por feature:
- `id`: ID numérico del POI
- `name`: Nombre del campamento
- `city`, `state`: Localización
- `combined_avg_rating`: Rating promedio (1-5)
- `reviews_count`: Número de reseñas
- `filters_appearance.label`: Categoría visual (ej: "Public Land", "RV Park")
- `place_detail`: Amenidades básicas (`pets`, `wifi`, `restrooms`, `showers`, etc.)
- `primary_image_url`: URL de imagen principal
- `path`: Ruta relativa para la URL web

**Nota:** El endpoint `POST /api/v2/pois/search` fue descartado porque requiere `search_text` no vacío (HTTP 422), haciéndolo inútil para escaneo de fondo.

---

### 2. Fase 2: Detalles y Reseñas por POI

```
GET https://maps.campendium.com/api/v1/pois/{poi_id}
```

Retorna JSON con la ficha técnica completa del POI incluyendo:
- `description`: Descripción textual en inglés
- `website`: URL oficial del campamento
- `place_detail`: Amenidades detalladas con campos booleanos (como strings)
- `comments`: Array de reseñas con texto, ratings sub-categoría, datos de autor, y señal celular

**Campos clave de `place_detail`:**
| Campo | Tipo | Descripción |
|---|---|---|
| `wifi` | `"true"/"false"` | WiFi disponible |
| `pets` | `"true"/"false"` | Mascotas permitidas |
| `restrooms` | `"true"/"false"` | Aseos disponibles |
| `showers` | `"true"/"false"` | Duchas disponibles |
| `dump_station` | `"true"/"false"` | Estación de vaciado |
| `sewer_hookup` | `"true"/"false"` | Conexión a desagüe |
| `full_hookup` | `"true"/"false"` | Hookup completo |
| `fifty_amp` | `"true"/"false"` | Conexión eléctrica 50A |
| `big_rigs` | `"true"/"false"` | Acceso para vehículos grandes |
| `pull_through` | `"true"/"false"` | Parcelas drive-through |
| `tent_sites` | `"true"/"false"` | Parcelas para tiendas |
| `laundry` | `"true"/"false"` | Lavandería |

**Campos clave de cada `comment`:**
| Campo | Tipo | Descripción |
|---|---|---|
| `id` | `int` | ID único de la reseña |
| `text` | `string` | Texto de la reseña |
| `reviewer_rating` | `string` | Rating general (1-5) |
| `author_name` | `string` | Nombre del autor |
| `created_at` | `ISO 8601` | Fecha de creación |
| `access_rating`, `cleanliness_rating`, `location_rating`, `noise_rating`, `scenery_rating`, `site_quality_rating` | `int` | Sub-ratings (1-5) |
| `nightly_rate` | `float` | Precio por noche reportado |

---

## 🗂️ Mapeo y Normalización

### Tipos de Spots

Se deducen del campo `filters_appearance.label` del tile:
- **`area_ac`**: Si label contiene "RV Park" o "Dump Station"
- **`wild`**: Si label contiene "Public Land", "Free Camping", "BLM", "National Forest"
- **`parking`**: Si label contiene "Walmart", "Overnight Parking", "Rest Area", "Casino"
- **`camping`**: Para el resto de categorías

### Amenidades Mapeadas

| Campo Campendium (`place_detail`) | Campo GeoSpots |
|---|---|
| `restrooms` | `wc_publico` |
| `showers` | `ducha` |
| `wifi` | `wifi` |
| `pets` | `perros` |
| `big_rigs` | `acceso_grandes` |
| `fifty_amp` / `full_hookup` | `electricidad` |
| `dump_station` / `sewer_hookup` | `vaciado_grises` y `vaciado_negras` |

---

## 🔄 Credibilidad en Reconciliación

En `reconciliar.py`, `campendium` se posiciona justo después de `thedyrt` en la mayoría de campos norteamericanos:
- **`descripcion_en`:** Segunda prioridad (después de `thedyrt`).
- **`canonical_name` / `tipo`:** Alta (después de `thedyrt`).
- **`master_rating`:** Alta (después de `thedyrt`).
- **Amenidades:** Media-alta.
- **`base_score`:** 0.85 en `source_credibility` (cobertura: US, CA).

---

## 🚦 Parámetros de Operación

- **Scraper (spots):** `rate_limit = 1.0s` entre peticiones de tile. Utiliza tiles a zoom 8 con `grid_step = 2.0°`. Grid restringido a Norteamérica (`lat: [10°, 75°]`, `lon: [-170°, -50°]`).
- **Reviews & Detalles:** Limitador de trabajadores concurrentes a 3-5. Manejo de `HTTP 429` (esperando 60s). Rate limiting de `1.0s` por petición.
- **Dedup radius:** `100m`.
