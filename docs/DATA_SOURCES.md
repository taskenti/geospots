# GeoSpots — Fuentes de Datos

## Tabla de Fuentes

| Nombre | URL base | Formato | Estrategia de grid | Frecuencia | Estado | Tipo principal |
|---|---|---|---|---|---|---|
| **park4night** | `guest.park4night.com/services/V4.1` | JSON REST | Quadtree adaptativo (0.125°–1°) | Semanal | Activo | Camperismo/Wildcamp |
| **campercontact** | `www.campercontact.com` | JSON REST + HTML | BBox recursiva (1°→0.5° si >50) | Semanal | Activo | Camping/Área |
| **ioverlander** | Archivo KMZ offline | KMZ/KML | Import único (no grid) | Manual | Activo | Offroad/Overlanding |
| **furgovw** | `furgovw.com/api` | JSON REST + RSS | Un solo request global | Semanal | Activo (bug lat/lng) | Furgonetas España |
| **areasac** | `areasac.com` | HTML scraping | URLs estáticas | Mensual | Activo | Áreas servicio España |
| **osm** | Overpass API | JSON | Radio circular 60km | Semanal | Activo | OSM amenity tags |
| **searchforsites** | `searchforsites.co.uk` | JSON REST | BBox estándar | Mensual | Activo | UK/IE |
| **wtmg** | `welcometomygarden.org` | JSON REST | BBox estándar | Semanal | Activo | Jardines privados |
| **nomady** | `nomady.camp` | JSON REST | BBox estándar | Mensual | Activo | DACH |
| **campspace** | `campspace.com` | JSON REST | BBox estándar | Mensual | Activo | Europa |
| **roadsurfer** | `roadsurfer.com` | JSON REST | BBox estándar | Mensual | Activo | Europa |
| **vansite** | `vansite.fr` | JSON REST | BBox estándar | Mensual | Activo | Francia |
| **caramaps** | `www.caramaps.com` | JSON REST | BBox estándar | Mensual | Activo | Francia/Europa |
| **stayfree** | `app.stayfree.eu` | JSON REST | BBox estándar | Mensual | Activo (requiere XSRF) |Europa |
| **promobil** | `www.promobil.de` | JSON REST | BBox estándar | Mensual | Activo | DACH |
| **camperstop** | `www.camperstop.com` | JSON REST | BBox estándar | Mensual | Activo | Europa |
| **alpacacamping** | `www.alpacacamping.de` | JSON REST | BBox estándar | Mensual | Activo | DACH |
| **womostell** | `www.womostell.de` | JSON REST | BBox estándar | Mensual | Activo | DACH |
| **thedyrt** | `thedyrt.com` | JSON REST | BBox estándar | Mensual | Activo | USA/CAN |
| **campingcarinfos** | `www.campingcar-infos.com` | ZIP+ASCII (POI) | Descarga global única | Mensual | Activo | EU (43 países, ~24K spots) |
| **agricamper** | `www.agricamper.com/wp-json/interactive-map/v1/fiches` | JSON bulk (WP REST) | Descarga global única | Semanal | Activo | IT (~605 fiches agroturismo) |
| **campendium** | `maps.campendium.com/api/v2/tiles/{z}/{x}/{y}` | GeoJSON tiles + REST detail | Tiles OSM zoom 8 + bbox NA | Mensual | Activo | US/CA (cobertura amplia) |
| **campingcarpark** | `gateway.feature.campingcarpark.com` | Bulk API gateway (status + detail) | Lista global IDs + detalle por ID | Semanal | Activo | EU (~906 áreas oficiales red CCP) |
| **campy** | `graphql-server-...run.app` | GraphQL (LocationsWithinRadius) | Radio 90km en grid 1° | Semanal | Activo | DACH + EU (microcamping) |
| **bobilguiden** | `api.bobilguiden.no/places/mobile` | Bulk JSON único | Descarga global (no grid) | Semanal | Activo | NO/SE/FI/DK (~1936 spots, NO=95%) |
| **portugaleasycamp** | — | — | — | — | Stub vacío | Portugal |

---

## Schema de Normalización Común

Todos los scrapers deben retornar un dict compatible con este schema. Los campos `None` no se persisten.

### Campos Obligatorios

| Campo | Tipo | Descripción |
|---|---|---|
| `nombre` | `str` | Nombre del lugar (puede ser genérico) |
| `lat` | `float` | Latitud WGS84 |
| `lon` | `float` | Longitud WGS84 |
| `tipo` | `str` | Categoría semántica (ver Tipos) |
| `source` | `str` | Clave de fuente (e.g. `'park4night'`) |
| `source_id` | `str` | ID único en la fuente original |

### Campos Opcionales — Identificación

| Campo | Tipo | Descripción |
|---|---|---|
| `pais` | `str` | Código ISO 2 letras (ej. `'ES'`) |
| `region` | `str` | Región/provincia |
| `ciudad` | `str` | Ciudad más cercana |
| `direccion` | `str` | Dirección postal si disponible |

### Campos Opcionales — Servicios (booleanos)

| Campo | Descripción |
|---|---|
| `agua_potable` | Agua potable disponible |
| `electricidad` | Conexión eléctrica |
| `ducha` | Duchas disponibles |
| `wifi` | WiFi disponible |
| `wc_publico` | Baños públicos |
| `vaciado_negras` | Punto vaciado aguas negras |
| `vaciado_grises` | Punto vaciado aguas grises |
| `perros` | Admite perros |
| `acceso_grandes` | Accesible para vehículos grandes |
| `iluminacion` | Iluminación nocturna |
| `seguridad` | Zona considerada segura |
| `reserva_req` | Requiere reserva previa |

### Campos Opcionales — Precio

| Campo | Tipo | Descripción |
|---|---|---|
| `gratuito` | `bool` | Si es gratuito |
| `precio_aprox` | `float` | Precio aproximado en EUR |
| `precio_info` | `str` | Texto libre sobre precio |

### Campos Opcionales — Contenido

| Campo | Tipo | Descripción |
|---|---|---|
| `descripcion_es` | `str` | Descripción en español |
| `descripcion_en` | `str` | Descripción en inglés |
| `descripcion_fr` | `str` | Descripción en francés |
| `descripcion_de` | `str` | Descripción en alemán |
| `fotos_urls` | `list[str]` | URLs de fotos |
| `tags` | `list[str]` | Etiquetas libres |
| `temporada_apertura` | `str` | Texto sobre temporada |
| `horario` | `str` | Horario de acceso |
| `contacto` | `str` | Teléfono/email |
| `url_fuente` | `str` | URL directa al listing |

### Campos Opcionales — Métricas

| Campo | Tipo | Descripción |
|---|---|---|
| `rating_promedio` | `float` | Rating 0-10 (normalizado desde la escala de la fuente) |
| `num_reviews` | `int` | Número de reviews |
| `num_plazas` | `int` | Plazas para vehículos |
| `altura_max_m` | `float` | Altura máxima en metros |

### Campos Opcionales — Reviews

| Campo | Tipo | Descripción |
|---|---|---|
| `reviews` | `list[dict]` | Reviews (ver schema de reviews) |

---

## Tipos de Spot

El campo `tipo` usa estos valores canónicos:

| Valor | Descripción |
|---|---|
| `camping` | Camping oficial con servicios |
| `area` | Área de servicio para autocaravanas |
| `wildcamp` | Camping libre/natural sin instalaciones |
| `parking` | Aparcamiento apto para pernocta |
| `picnic` | Área de picnic/descanso |
| `naturaleza` | Espacio natural protegido o paraje |
| `jardin` | Jardín privado (WTMG) |
| `granja` | Granja que acepta campistas |
| `otro` | No clasificado (pendiente de enriquecer) |

Valores legacy en CHECK constraint: `naturaleza`, `parking`, `picnic` (compatibilidad con datos anteriores).

---

## Mapeo por Fuente

### park4night

```
CODIGO_MAP: {
  1→'wildcamp', 2→'area', 3→'camping', 4→'parking', 5→'otro',
  6→'picnic', 7→'jardin', 8→'granja', 9→'naturaleza'
}
```

Campos mapeados: nombre, lat, lon, tipo, gratuito, precio_aprox, precio_info, agua_potable, electricidad, ducha, wifi, wc_publico, vaciado_negras, vaciado_grises, perros, acceso_grandes, altura_max_m, iluminacion, seguridad, num_plazas, rating_promedio, num_reviews, fotos_urls, descripcion_fr, descripcion_en, temporada_apertura, url_fuente

Rating: escala 0-5 → multiplicado por 2 para normalizar a 0-10.

### campercontact

Campos mapeados: nombre, lat, lon, tipo, gratuito, precio_aprox, precio_info, agua_potable, electricidad, ducha, wifi, wc_publico, vaciado_negras, vaciado_grises, perros, acceso_grandes, iluminacion, seguridad, num_plazas, reserva_req, rating_promedio, num_reviews, fotos_urls, descripcion_en, descripcion_fr, descripcion_de, temporada_apertura, url_fuente

Detalle adicional vía HTML scraping: amenidades extendidas desde página Next.js (extrae JSON de `__next_f.push`).

### ioverlander

KMZ offline con KML Placemarks. STYLE_MAP convierte URLs de estilo KML a tipos GeoSpots:
```
STYLE_MAP: {
  'camp'→'camping', 'wild'→'wildcamp', 'service'→'area',
  'parking'→'parking', 'scenic'→'naturaleza', ...
}
```

EXCLUDED_STYLES: tarjetas SIM, cajeros, ferrys, tiendas — filtrados antes de insertar.

### furgovw

**BUG CONOCIDO**: la API retorna los campos geográficos con lat y lng invertidos. Compensado explícitamente en `normalize()` con swap. No corregir sin verificar que la API siga devolviendo los campos invertidos.

3 fases por ejecución:
1. Request global a `/api/spots` → lista completa
2. RSS por foro → extrae reviews/posts como texto
3. Scrape de "papelera" → marca spots retirados con `advertencia`

### osm

OVERPASS_QUERY busca 8 combinaciones de tags:
- `tourism=camp_site`
- `tourism=caravan_site`
- `amenity=parking` (solo si `motorcar=yes` o nombre relevante)
- `leisure=picnic_site`
- etc.

Radio de búsqueda: 60km centrado en puntos de grid generados por `generate_active_grid`.

### stayfree

Requiere `STAYFREE_XSRF_TOKEN` en `.env`. Token caduca periódicamente — si las requests fallan con 403/419, regenerar token iniciando sesión en la web.

### campingcarinfos

Web francesa con cobertura europea. Descarga global única vía `creepoigpstotal.php` que devuelve un ZIP con archivos `.asc` separados por categoría (AC, ACF, ACS, APCC, APN, AS, ASN, AA) y un `ATOTALES_CCI.asc` combinado.

Formato de cada línea:
```
LON,LAT,"<CATEGORIA> <PAIS_FR> <LOCALIDAD>  [(<CP>)]  Aire CCI <ID>"
```

Mapeo de categorías → tipo GeoSpots:
- `AC`, `APCC`, `AS`, `AA` → `area_ac` (área de servicios)
- `ACF`, `ACS` → `camping`
- `APN` → `parking_privado`
- `ASN` → `parking_publico`

Solo aporta: coordenadas, categoría, país, localidad. **No incluye** servicios, precios, fotos, reviews ni descripciones — es una fuente complementaria pura. Idónea para cross-validation con otras fuentes (en la primera carga real, 83% de spots ya existían en la DB).

`base_score = 0.78`, `review_quality = 0.50`, `geo_accuracy = 0.85`.

---

## Schema de Reviews

```python
{
    "source": str,             # fuente origen
    "source_review_id": str,  # ID único en la fuente
    "spot_id": int,           # FK a spots.id
    "texto_original": str,    # texto raw tal cual llegó
    "rating": float,          # 0-10 normalizado
    "fecha": date,            # fecha de la review
    "autor": str,             # nombre/nick del autor (opcional)
    "idioma": str,            # detectado por langdetect
}
```

---

## Estrategia de Merge y Deduplicación

### 1. Deduplicación Geoespacial (`db.py:find_spot_cercano`)

```
ST_DWithin(spots.geog, punto_nuevo, radio_metros)
ORDER BY distancia ASC
LIMIT 1
```

Tres capas de decisión en orden:

| Condición | Acción |
|---|---|
| Distancia < 20m | Fusión directa (error GPS normal) — EXCEPTO si tipos son mutuamente excluyentes |
| 20m ≤ dist ≤ 100m + tipos excluyentes | NO fusionar (crea spot nuevo) |
| 20m ≤ dist ≤ 100m + similitud_nombre ≥ 0.35 | Fusionar |
| 20m ≤ dist ≤ 100m + similitud_nombre < 0.35 | NO fusionar |

**Grupos de exclusión** (tipos que NUNCA se fusionan entre sí):
```python
EXCLUSION_GROUPS = {
    "camping": {"wildcamp", "naturaleza", "parking"},
    "wildcamp": {"camping", "area", "parking"},
    "area": {"wildcamp", "naturaleza", "parking"},
    "parking": {"camping", "wildcamp", "area", "naturaleza"},
}
```

**Similitud de nombre**: función `similarity()` de pg_trgm. Umbral 0.35 elegido empíricamente.

**Nota**: el campo `geohash7` existe en la tabla `spots` pero NO se usa en el proceso de dedup. Solo sirve para clustering visual en el mapa.

### 2. Enriquecimiento vs Creación

```
spot existente encontrado?
  SÍ → enriquecer_spot(spot_id, norm, fuente)
         UPDATE ... SET campo = COALESCE(campo_actual, valor_nuevo)
         Solo rellena NULLs. La primera fuente que llega "gana".
         Excepciones: tipo='otro' → se actualiza; JSONB arrays vacíos → se actualizan
  NO → crear_spot(norm)
         INSERT con todos los campos disponibles
```

### 3. Reconciliación Multi-fuente (`reconciliar.py`)

Corre sobre spots con ≥2 fuentes. Aplica `CREDIBILITY` dict:
- Por campo, itera la lista de fuentes en orden de confianza
- La primera fuente que tiene valor no-nulo "gana" para ese campo
- Actualiza directamente en `spots` con `UPDATE SET campo = valor`

**Limitación**: scan completo sin modo incremental. A 500K+ spots puede tomar varios minutos.

Conflictos detectados en campos `["gratuito", "precio_info", "agua_potable", "electricidad", "num_plazas", "tipo"]` se guardan en `spots.conflictos JSONB`.

### 4. Source Records

Independientemente del merge, cada fuente siempre tiene su registro en `source_records`:
```
ON CONFLICT (source, source_id) DO UPDATE SET
    normalized_data = EXCLUDED.normalized_data,
    raw_data = EXCLUDED.raw_data,
    updated_at = NOW()
```

El `raw_data` es inmutable conceptualmente — nunca se modifica el JSON original de la fuente.

---

## Credibilidad por Fuente

`source_credibility` en DB tiene `base_score` por fuente. Valores altos:

| Fuente | Score | Razón |
|---|---|---|
| park4night | 0.92 | Gran comunidad, datos verificados |
| campercontact | 0.90 | Base de datos europea consolidada |
| ioverlander | 0.85 | Comunidad overlanding, fotos reales |
| areasac | 0.85 | Datos oficiales áreas servicio España |
| promobil | 0.85 | Revista especializada DACH |
| thedyrt | 0.82 | Comunidad norteamericana activa |
| campspace | 0.80 | Reservas verificadas |
| osm | 0.70 | Alta cobertura, baja profundidad de datos |
| furgovw | 0.65 | Comunidad española, datos informales |
| wtmg | 0.75 | Verificación de anfitriones |

La credibilidad también determina el orden en `CREDIBILITY` dict de `reconciliar.py` para cada campo específico.

---

## Añadir una Nueva Fuente

Ver [DEVELOPMENT.md](DEVELOPMENT.md) para el procedimiento paso a paso.

Resumen rápido:
1. Crear `scraper/sources/nueva_fuente.py` heredando `AbstractSource`
2. Implementar `fetch_cell()` y `normalize()` retornando schema estándar
3. Registrar en `scheduler.py:SOURCES`
4. Añadir a `CREDIBILITY` en `reconciliar.py` para cada campo relevante
5. Añadir a `source_credibility` en DB (INSERT en migration o sync_db.py)
6. Documentar en esta tabla
