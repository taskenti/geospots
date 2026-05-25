# CampingCar Park — Fuente de Datos GeoSpots

## Descripción

[CampingCar Park](https://www.campingcarpark.com) es la mayor red de áreas de servicio y estacionamiento para autocaravanas en Europa, con presencia principal en Francia y expansión a España, Portugal, Bélgica, Alemania y otros países. Gestiona ~900+ áreas propias con datos estructurados de alta calidad.

## Arquitectura del Scraper

### Tipo de Ingesta
**Bulk API** — No utiliza grid. Descarga la lista completa de IDs desde un endpoint de status y luego enriquece cada uno con su endpoint de detalle.

### Endpoints

| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/api/v1/stay/locations/status` | GET | Lista de todos los locationId con su estado (opened/closed/planned) |
| `/shop-api/locations/{id}` | GET | Detalle completo del spot (nombre, coords, servicios, precios, fotos, prohibiciones, sanitarios) |
| `/shop-api/locations/{id}/reviews?limit=20&skip=0` | GET | Reviews paginadas (array JSON, 20 por página) |

**Base URL:** `https://gateway.feature.campingcarpark.com`

### Flujo de Ejecución

```
1. GET /api/v1/stay/locations/status → ~906 IDs
2. Para cada ID (concurrente, semáforo=5):
   GET /shop-api/locations/{id} → normalize() → dedup → INSERT/UPDATE
3. download_reviews():
   Para cada spot con reviews pendientes:
   GET /shop-api/locations/{id}/reviews?limit=20&skip=N → upsert_review()
```

### Notas Técnicas

- **SSL**: Se desactiva la verificación SSL (`verify=False`) porque el gateway interno usa certificados que Python no puede validar directamente.
- **Rate Limiting**: 100ms entre llamadas + semáforo de 5 workers concurrentes.
- **Paginación de Reviews**: Páginas de 20 reviews con parámetro `skip`. Se para cuando `len(reviews) < page_size`.

## Mapeo de Campos

| Campo GeoSpots | Campo API CCP | Transformación |
|----------------|---------------|----------------|
| `source_id` | `id` | `str(id)` |
| `nombre` | `publicName` / `name` | Preferencia a `publicName` |
| `lat` / `lon` | `latitude` / `longitude` | `float()` |
| `tipo` | — | Fijo `"area_ac"` |
| `gratuito` | — | Fijo `False` |
| `precio_aprox` | `currentPrice.allTaxesIncludedParkingPrice` | `float()` |
| `precio_info` | — | `f"{precio_aprox:.2f} EUR"` |
| `web` | `linkUrl` | Prefijo `https://www.campingcarpark.com` |
| `fotos_urls` | `images[].mobileUrl` | Max 8 fotos |
| `descripcion_fr` | `description` + `surroundingsDescription` | Concatenados |
| `agua_potable` | `services` | `"water" in services` |
| `electricidad` | `services` / `electricalOutletCount` | `"electricity" in services` o `count > 0` |
| `wifi` | `services` | `"wifi" in services` |
| `vaciado_grises/negras` | `services` | `"drain" in services` |
| `wc_publico` | `sanitaryDetails.WC` + `sanitaryOpening.toiletCount` | `count > 0` |
| `ducha` | `sanitaryDetails.shower` + `sanitaryOpening.showerCount` | `count > 0` |
| `perros` | `prohibitions.dog` | `not dog` (True si no prohibido) |
| `acceso_grandes` | `prohibitions.vehicleMore9m` | `not vehicleMore9m` |
| `num_plazas` | `totalPitchesNumber` | `int()` |
| `rating_promedio` | `averageRating` | `float()` |
| `num_reviews` | `reviewsNumber` | `int()` |
| `country_iso` | `countryCode` | Mapeo `FR→fr`, `ES→es`, etc. |
| `region` | `region` | Directo |

## Mapeo de Reviews

| Campo | Campo API | Transformación |
|-------|-----------|----------------|
| `source_review_id` | `id` | `f"ccp_{id}"` |
| `texto` | `title` + `comment` | Concatenados con `: ` |
| `rating` | `rating` | `float()` |
| `autor` | `author` | Directo |
| `fecha` | `createdAt` | `datetime.fromisoformat()` |
| `idioma` | `language` | Directo (fallback `"fr"`) |

## Credibilidad

En `reconciliar.py`, `campingcarpark` ocupa la **primera posición** en la mayoría de campos europeos:

- **Máxima prioridad**: tipo, precio, servicios (agua, electricidad, wifi, ducha, WC, vaciado), num_plazas, perros, acceso_grandes, canonical_name, master_rating, descripcion_fr
- **Score base**: 0.90 (máximo entre fuentes europeas)
- **Review quality**: 0.85
- **Coverage region**: `['EU']`

## Campos Adicionales Disponibles (no mapeados)

- `securiplace`: Si tiene sistema de seguridad Securiplace
- `maxNightCount`: Número máximo de noches permitidas
- `tariffs[]`: Lista completa de tarifas por temporada
- `touristTaxes[]`: Impuestos turísticos aplicables
- `benefits`, `shops`, `markets`, `events`: Información turística textual
- `authorizedVehicles`: Tipos de vehículos permitidos (campers, vans, caravans, tents)
- `adjacentLocations[]`: Áreas cercanas de la red CCP
- `isBookable`: Si acepta reservas online
- `customersProfile`: Perfiles de cliente aceptados (ccowner, truck, van)
