# StayFree — Documentación del Scraper

## Resumen

| Campo | Valor |
|---|---|
| **Archivo** | `sources/stayfree.py` |
| **Clase** | `StayFreeSource` |
| **Nombre interno** | `stayfree` |
| **Tipo de acceso** | API web pública (sin autenticación) |
| **Cobertura** | Europa + Norte de África (~38 países) |
| **Estrategia** | Paginación por país + tipo de spot |
| **Rate limit** | 2 s entre peticiones |
| **Dedup radio** | 50 m |

---

## Descubrimiento del Endpoint

La app StayFree es una **WebView** (no nativa). Se identificó con:
```
User-Agent: Mozilla/5.0 ... Chrome/124 (wv)
Origin:     https://localhost
```

Endpoint capturado con Chrome DevTools → Red → Fetch/XHR:

```
GET https://www.stayfree.app/api/spots
```

### Parámetros disponibles

| Parámetro | Tipo | Descripción |
|---|---|---|
| `spotType` | string | Tipo de spot (ver tabla abajo). Un solo valor por petición. |
| `maxResults` | int | **Máximo real: 100.** Con 200+ devuelve 503. |
| `locale` | string | Idioma de la respuesta (`es`, `en`, etc.) |
| `sort` | string | Ordenación: `rating`, `distance` |
| `locationCountry` | string | Código ISO 3166-1 alpha-2 (ej. `ES`, `FR`) |
| `page` | int | Paginación (0-indexed). Ausente = página 0. |

### Límites confirmados

| maxResults | Resultado |
|---|---|
| 20 | ✅ 200 OK |
| 50 | ✅ 200 OK |
| 100 | ✅ 200 OK |
| 200 | ❌ 503 Service Unavailable |

---

## Tipos de Spot

| `spotType` API | `tipo` GeoSpots | Descripción |
|---|---|---|
| `WILD_SPOT` | `naturaleza` | Acampada libre en naturaleza |
| `PARKING_FREE` | `area_ac` | Parking gratuito para autocaravanas |
| `PARKING_CAMPER` | `area_ac` | Parking específico para campers |
| `PARKING_CAMPER_ACS` | `area_ac` | Área de servicio oficial (ACS) |
| `CAMPING` | `camping` | Camping convencional |
| `CAMPING_ACS` | `camping` | Camping con área de servicio |
| `CAMPING_PRIVATE` | `camping` | Camping privado |
| `AGROTOURISM` | `otro` | Agroturismo / fincas |

---

## Estructura de Respuesta

```json
[
  {
    "_id": "60db71c48703bfd3c71a374e",
    "name": "Lugar junto al lago",
    "features": {
      "ENVIRONMENT_LAKE": true,
      "SANITARY_WATER": true,
      "SANITARY_TOILET": false
    },
    "ratings": {
      "overall_rating": 5,
      "close_to_nature_rating": 4.9,
      "tranquility_rating": 5,
      "view_rating": 4.9,
      "total": 22
    },
    "priceValue": null,
    "isTopSpot": true,
    "bookable": false,
    "ratingScore": 5,
    "ratingCount": 22,
    "description": "...",
    "city": "Vanga",
    "country": "SE",
    "imageUrl": "https://res.cloudinary.com/staywild/..."
  }
]
```

> [!NOTE]
> El campo `location.coordinates` (GeoJSON `[lon, lat]`) solo aparece en la respuesta detallada de un spot individual (`GET /api/spots/:id`), **no** en la lista. Para obtener coordenadas exactas se necesita una segunda petición por spot o usar el endpoint de detalle.

---

## Mapa de Features → Campos GeoSpots

| Feature API | Campo normalizado |
|---|---|
| `SANITARY_WATER` | `agua_potable` |
| `SANITARY_TOILET` | `wc_publico` |
| `SANITARY_SHOWER` | `ducha` |
| `SANITARY_ELECTRICITY` | `electricidad` |
| `SANITARY_DUMP_STATION` | `vaciado_negras` |
| `SANITARY_GREY_WATER` | `vaciado_grises` |
| `SANITARY_BLACK_WATER` | `vaciado_negras` |

---

## Países Objetivo

```
Europa Occidental:  ES, FR, PT, IT, DE, AT, CH, BE, NL, LU
Islas Británicas:   GB, IE
Escandinavia:       DK, SE, NO, FI, IS
Europa del Este:    PL, CZ, SK, HU, RO, BG, HR, SI, RS, BA
Mediterráneo:       GR, TR, CY, MT
Bálticos:           EE, LV, LT
Balcanes:           AL, MK, ME
Norte de África:    MA, TN
```

---

## 💬 Pipeline de Reseñas (Mayo 2026)
Se ha implementado el descargador desacoplado `download_reviews` en `stayfree.py` que realiza las siguientes operaciones:
1. **Endpoint de Detalle**: `GET https://www.stayfree.app/api/spots/{source_id}`. Este endpoint sirve tanto para enriquecer las coordenadas exactas y fotos como para descargar las valoraciones.
2. **Estructura e Identificación**: Como las valoraciones de StayFree no cuentan con una clave ID única en el JSON, se genera una clave compuesta `f"stayfree_{source_id}_{index}"` basada en el índice posicional para evitar duplicidades.
3. **Mapeo de Campos**:
   - Texto: Extraído del campo `comment`, `text` o `texto`.
   - Calificación: Extraída de `overall_rating` o `rating` (escala 1-5).
   - Autor: Extraído de `owner_id` o `username`.
   - Fecha: Parseada desde `timestamp` o `date`.
   - Idioma: Identificado dinámicamente con `detect_language`.
4. **Enriquecimiento de Imágenes**: El pipeline extrae el listado de imágenes (`photos` / `images` / `imageUrl`) y las añade al spot, realizando un merge con las URLs existentes hasta un máximo de 15 imágenes.
5. **Manejo de Errores y HTTP 503**:
   - Si la petición devuelve 404 o 410, el spot se omite.
   - Si devuelve 503 (límite de peticiones de Cloudflare / backend), el worker realiza un tiempo de espera (`asyncio.sleep(10)`) y reintenta la petición.

## Notas de Ingeniería Inversa

- **Cloudflare activo**: `Server: cloudflare`, `CF-RAY` en headers de respuesta. El WAF bloquea peticiones con `maxResults > 100` o sin `User-Agent` de navegador.
- **SSL Pinning en la app Android**: La versión Android de la app usa fijación de certificados. El endpoint fue descubierto usando la **versión web** (`stayfree.app`) con Chrome DevTools, evitando completamente el emulador.
- **Protección CORS**: El servidor acepta `Origin: *` en respuestas, pero las peticiones deben incluir `sec-fetch-*` headers para no ser bloqueadas como bots.
- **Backend**: Cloudflare + servidor propio (dominio `api.stayfree.app`). Las imágenes se sirven desde Cloudinary (`res.cloudinary.com/staywild`).
