# 🦙 Alpaca Camping Scraper

**Alpaca Camping** es una plataforma de reservas de alojamientos al aire libre de origen alemán. Ofrece principalmente propiedades privadas con encanto: granjas, glamping, fincas y campings boutique. Está orientada al mercado DACH (Alemania, Austria, Suiza) aunque cubre propiedades a nivel mundial.

El scraper `alpacacamping.py` utiliza la API de búsqueda ElasticSearch que alimenta su app móvil para descargar la base de datos completa (~2,182 propiedades globales) mediante paginación.

---

## 🛠️ Descubrimientos de Ingeniería Inversa

### 1. Endpoint de Búsqueda (API Pública)

```
GET https://search.alpacacamping.de/api/search
```

**Headers obligatorios:**
```http
accept: application/json
user-agent: okhttp/4.12.0
x-api-key: 6qJIPHk0q1tCw38g2wvQlbIwsNik
```

**Parámetros de consulta:**
| Parámetro | Valor | Descripción |
|---|---|---|
| `min_lat` | `-90.0` | Latitud mínima del bounding box |
| `max_lat` | `90.0` | Latitud máxima del bounding box |
| `min_long` | `-180.0` | Longitud mínima del bounding box |
| `max_long` | `180.0` | Longitud máxima del bounding box |
| `property_type` | `1` | Tipo de propiedad (1 = todos los campings/spots) |
| `size` | `200` | Resultados por página (máximo 200) |
| `page` | `1, 2, 3...` | Número de página (1-indexado) |

**⚠️ Bug de paginación detectado:** El parámetro `from` (estilo ElasticSearch) NO funciona como offset. La API ignora `from=200`, `from=400`, etc. y siempre devuelve la misma primera página. La paginación correcta es mediante el parámetro `page=N` (1-indexado). Con 2,182 spots y `size=200`, se necesitan **11 peticiones** para descargar la base de datos completa.

**Estructura de respuesta:**
```json
{
  "total": 4773,
  "total_hits": 2182,
  "hits": [...]
}
```
- `total_hits` es el número real de resultados del bounding box.
- `total` puede ser mayor (total del índice global).

---

### 2. Páginas de Detalle (Reviews)

Las reseñas no se exponen por API. Se extraen del HTML de la página de detalle pública:

```
GET https://www.alpacacamping.de/properties/{source_id}
```

**Estrategia de extracción:** La página HTML embebe dumps crudos del modelo Eloquent de PHP (`App\Models\Reviews`) dentro de comentarios HTML:

```html
<!-- <pre>object(App\Models\Reviews)#XXX ...
  ["attributes":protected]=>
  array(N) {
    ["id"]=> int(10205)
    ["rating"]=> int(9)
    ["message"]=> string(N) "..."
    ["created_at"]=> string(19) "2024-03-15 10:30:00"
    ...
  }
</pre> -->
```

El método `_parse_reviews_html` extrae los datos estructurados de estos comentarios mediante regex y los alinea con los nombres de autores visibles en el DOM.

---

## 🗂️ Mapeo y Normalización

### Tipos de Spots

El campo `amenities_infos.id` (lista de IDs de amenidades) determina el tipo:
- **`area_ac`**: Tipo por defecto para la mayoría de propiedades.
- **`naturaleza`**: Cuando el amenity ID 27 (Tents) está presente Y los IDs 25 (Motorhome) y 28 (Caravan) **no** están presentes.

### Amenidades Mapeadas

| ID Amenidad | Campo GeoSpots |
|---|---|
| 14, 238 | `agua_potable` |
| 13, 223 | `electricidad` |
| 20 | `vaciado_grises` |
| 21 | `vaciado_negras` |
| 16, 284 | `wc_publico` |
| 17, 476 | `ducha` |
| 1 | `wifi` |
| 26 | `acceso_grandes` |
| 4 | `perros = False` (no dogs) |
| 41, 315, 229, 231 | `perros = True` (dogs allowed) |

### Precios

- `property_price.price` → `precio_aprox` (float)
- `property_price.currency_code` + precio → `precio_info` (string, e.g. `"45.0 EUR"`)
- Alpaca Camping es una **plataforma de pago**, por lo que `gratuito = False` siempre.

### Descripción

- `property_description.summary` → `descripcion_de` (mayoritariamente en alemán)

### Ratings

- `avg_rating` → `rating_promedio` (escala 0-10, se almacena directamente sin conversión ya que la app móvil usa esta escala)
- `reviews_count` → `num_reviews`

---

## 🔄 Credibilidad en Reconciliación

`alpacacamping` tiene posición intermedia-alta en la matriz `CREDIBILITY` de `reconciliar.py`:

| Campo | Posición relativa |
|---|---|
| `precio_info`, `precio_aprox`, `gratuito` | Alta (después de `promobil`) |
| `tipo` | Media (después de `campspace`) |
| `agua_potable`, `electricidad`, `ducha`, etc. | Media (después de `stayfree`) |
| `descripcion_de` | Alta (después de `promobil`) |
| `master_rating` | Media (después de `stayfree`) |
| `canonical_name` | Media (después de `stayfree`) |

---

## 🚦 Rate Limiting

- **Scraper (spots):** `rate_limit = 1.0s` entre páginas (11 peticiones totales).
- **Reviews:** 3 workers asíncronos concurrentes, `rate_limit = 1.0s` por worker. Sin rate limit explícito en la API pública de la web.
- **Dedup radius:** `80m` (propiedades privadas bien georreferenciadas).

---

## 📊 Resultados de Ingesta

| Métrica | Valor |
|---|---|
| Total propiedades en plataforma | ~2,182 |
| Pages necesarias (size=200) | 11 |
| Tiempo de ingesta total | ~73 segundos |
| Spots nuevos creados | 519 |
| Spots existentes actualizados | 1,663 |
| Errores | 0 |
| Fuente dominante | Alemania, Austria, Suiza |
