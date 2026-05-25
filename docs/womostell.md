# 🚐 Womo Stellplatz Finder (camping-app.eu)

**Womo Stellplatz Finder** (también conocido bajo el backend de `camping-app.eu`) es una de las plataformas de pernocta de autocaravanas más populares del espacio DACH (Alemania, Austria, Suiza), ofreciendo cobertura global con una fuerte densidad en Europa Central.

El scraper `womostell.py` aprovecha el backend de base de datos distribuida **Turso** (LibSQL) mediante el endpoint de pipeline SQL directo expuesto en la app móvil. Esto permite una extracción masiva directa mediante consultas SQL estándar.

---

## 🛠️ Descubrimientos de Ingeniería Inversa

### 1. Endpoint de Consulta Directa (Turso Pipeline)

```
POST https://ca-sites-letsgo.aws-eu-west-1.turso.io/v2/pipeline
```

**Headers obligatorios:**
```http
Content-Type: application/json
authorization: Bearer eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9...
```
*(Token JWT estático capturado del tráfico de la app móvil versión 9.6.3)*

**Cuerpo de Petición (Ejemplo de consulta por lotes):**
```json
{
  "requests": [
    {
      "type": "execute",
      "stmt": {
        "sql": "SELECT COUNT(*) FROM places WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
      }
    },
    {
      "type": "close"
    }
  ]
}
```

---

### 2. Estructura de Tablas en Turso

#### Tabla `places` (Spots)
Contiene la base de datos de spots con columnas de amenities pre-procesadas como booleanos (`1` o `0`) y geolocalización directa:
- `place_id`: ID único numérico (clave primaria).
- `name`: Nombre del lugar.
- `latitude` / `longitude`: Coordenadas geográficas decimales.
- `place_type_id`: Categoría nativa del lugar.
- `city`: Región/Ciudad.
- `description`: Descripción textual en alemán.
- `price`: Precio aproximado en EUR.
- `capacity`: Capacidad (número de plazas).
- `images`: Lista separada por comas de nombres de archivos de imagen.
- `open_from` / `open_to`: Rango de meses de apertura (1-12).
- `rm`: Nota promedio precalculada (escala 1-5).
- `int_id`: ID alfanumérico corto para la URL pública.

#### Tabla `place_ratings` (Reseñas)
Almacena las valoraciones de los usuarios:
- `id`: ID único de la reseña.
- `place_id`: ID del spot relacionado.
- `name`: Nombre del autor.
- `ratingtext`: Comentario de texto.
- `ratingtime`: Timestamp de publicación (`YYYY-MM-DD HH:MM:SS`).
- `published`: Indicador de estado (`1` = visible).
- `r1` a `r5`: Calificaciones individuales de sub-criterios (escala 1-5).

---

## 🗂️ Mapeo y Normalización

### Tipos de Spots

| `place_type_id` | Tipo GeoSpots | Descripción Nativa |
|---|---|---|
| 1 | `parking` | Parkplatz (Aparcamiento general) |
| 2, 3 | `area_ac` | Stellplatz / Wohnmobilstellplatz / Campingähnlicher |
| 4 | `camping` | Camping |
| 5 | `naturaleza` | Wildnis / freie Natur (Acampada libre) |
| 6, 7 | `parking` | Autobahnraststätte / Supermarkt (Autovías/Supermercados) |
| 8, 9 | `area_ac` | Weingut / Bauernhof / Hotel (Bodegas/Granjas/Hoteles) |
| 10 | `parking` | Museum / Freizeit (Museos/Lugares de ocio) |
| 11 | `area_ac` | Hafen / Marina (Puertos deportivos) |
| 12 | `wild` | Tolerierter Platz (Lugar tolerado) |

### Amenidades Mapeadas

Las columnas booleanas nativas `b_*` se convierten de enteros (`1` = `True`, `0` = `False`, `None` = desconocido):
- `b_water` → `agua_potable`
- `b_electricity` → `electricidad`
- `b_chemical_wc` → `vaciado_negras`
- `b_disposal` → `vaciado_grises`
- `b_wc` → `wc_publico`
- `b_shower` → `ducha`
- `b_wifi` → `wifi`
- `b_animals_allowed` → `perros`
- `b_long_campers` → `acceso_grandes`
- `b_reservation` → `reserva_req`

### Precios y URLs

- `price` → `precio_aprox` (ej: `12.50`) y se almacena en `precio_info` como `"12.50 EUR"`.
- `homepage` o URL corta pública construida:
  `https://www.womo-stellplatz.eu/place/{int_id}`
- **Imágenes CDN:** Las imágenes se formatean usando el patrón:
  `https://nbg1.your-objectstorage.com/caimg/{place_id}/webp_mid/{filename}`

---

## 🔄 Credibilidad en Reconciliación

En `reconciliar.py`, `womostell` tiene prioridad intermedia-alta dado que su base de datos es de carácter estructurado y depurado:
- **`precio_info` / `precio_aprox`:** Alta (posicionado junto a campercontact).
- **`tipo`:** Alta (mayor credibilidad que ioverlander o park4night).
- **Amenidades:** Media-alta.
- **Descripción:** Alta para el campo `descripcion_de`.

---

## 🚦 Parámetros de Operación

- **Scraper (spots):** Descarga secuencial por lotes de 500 spots (`ORDER BY place_id LIMIT 500 OFFSET offset`). Tasa de refresco rápida con un retardo de `0.2s` entre peticiones Turso.
- **Reviews:** Descarga en lotes de 200 `place_id` usando la consulta `WHERE place_id IN ({placeholders})`.
- **Dedup radius:** `60m` (coordenadas de alta precisión nativas de Turso).

---

## 📊 Resultados de Ingesta

| Métrica | Valor |
|---|---|
| Total spots en plataforma | 49,314 |
| Spots nuevos creados | 18,065 |
| Spots existentes actualizados | 31,249 |
| Errores de ingesta | 0 |
| Tiempo total de ingesta (spots) | 901 segundos |
