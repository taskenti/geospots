# 🏕️ FreeCampsites.net Scraper

## 📖 Información General
**FreeCampsites.net** es una comunidad veterana de camping gratuito centrada en **Norteamérica** (US, Canadá, México y un poco de Centroamérica/Caribe). Se especializa en spots de acampada libre en bosques públicos (BLM, National Forest), parkings nocturnos y pequeños RV parks. Es una fuente con muchos años de comentarios acumulados y una de las pocas con buena cobertura de **boondocking** real en EE.UU.

## 🛠️ Arquitectura y Funcionamiento

### Endpoints
- **Búsqueda de spots por radio**:
  `GET freecampsites.net/wp-content/themes/freecampsites/androidApp.php?location=(lat,lon)&coordinates=(lat,lon)&advancedSearch={}`
- **Reviews vía WordPress JSON API**:
  `GET freecampsites.net/wp-json/wp/v2/comments?post={post_id}&per_page=100`

### Pipeline
1. **Grid activo filtrado por Norteamérica** (`generate_active_grid` overrideado): lat 24-72, lon -170 a -50. Spots fuera de NA se descartan.
2. **`fetch_cell`**: query al endpoint mobile con centro de celda + radio implícito. Retry triple con backoff exponencial 5/10/20s ante HTTP 429 o errores de red.
3. **`normalize`**: extracción del `resultList` del JSON-en-texto (regex `\{.*\}`).
4. **`download_reviews`**: itera todos los `source_records` y descarga comentarios vía wp-json. Usa `upsert_review` (contador real).

## 🗂️ Mapeo y Normalización

### Tipos (heurística por keywords del nombre + color del icon)
| Detección | → tipo GeoSpots |
|---|---|
| "parking" / "lot" / "aparcamiento" en nombre | `parking_publico` |
| "RV Park" / "Resort" / "Campground" / "Camping" en nombre | `camping` |
| Icon URL contiene "green" (FC marca free camping con tent-green icon) | `wild` |
| Resto | `camping` (default conservador) |

### Servicios (inferidos del `excerpt` plain-text)
| GeoSpots | Keywords |
|---|---|
| `agua_potable` | water, potable, drinking |
| `electricidad` | electric, electricity, hookup, amp |
| `wifi` | wifi, wi-fi, internet |
| `ducha` | shower, showers |
| `wc_publico` | toilet, restroom, latrine, outhouse, privy |
| `perros` | dog, dogs, pet, pets, leash |
| `acceso_grandes` | rv, motorhome, big rig, slide-out |

### Países (texto libre → ISO2)
`COUNTRY_NAME_TO_ISO` dict cubre US, USA, U.S.A., Canada, Mexico, México, Belize, Guatemala, Honduras, Nicaragua, Costa Rica, Panama, Puerto Rico, Bahamas, Cuba. Desconocidos → NULL para que el trigger PostGIS clasifique.

### Rating
API devuelve `ratings_average` en escala **0-5**. GeoSpots usa **0-10** → multiplicado × 2 (max 10).
`ratings_count` → `num_reviews`.

## 🔧 Auditoría Mayo 2026

### Estado pre-auditoría
- 2.248 source_records, 2.229 spots
- Distribución tipos: wild (1081), camping (738), **parking (382, legacy)**, area_ac (28)
- countries en DB: us (1588), ca (582), mx (59) — el trigger PostGIS los clasificó porque el scraper original NO los leía del raw
- 1 zombie en `scraper_log`

### Estado API
**Caída durante la auditoría**: el endpoint `androidApp.php` da errores vacíos en 3 reintentos. wp-json/comments también caído. Auditoría hecha con **sample real extraído de `source_records.raw_data`** (validado contra el flow real).

### Bugs detectados y arreglados

| # | Bug | Impacto |
|---|---|---|
| 1 | **`country` field del raw NO se leía** | Todos los spots tenían country_iso vacío hasta que el trigger PostGIS clasificaba post-hoc. El campo viene como "Canada", "United States", "México" |
| 2 | **`region` mal mapeado** — usaba el `region` de raw que se machacaba con keywords de tipo | `region` (state/province) ahora se preserva |
| 3 | **`rating_promedio` sin escalar** — API usa 0-5, GeoSpots usa 0-10 | 2229 spots tenían rating en escala vieja, incoherente con resto de fuentes |
| 4 | **`tipo` "parking" legacy** | 382 spots con tipo viejo. Cambiados a `parking_publico` |
| 5 | **Heurística tipo: icon green tomaba precedencia sobre nombre "parking"** | "City Parking lot" → wild (incorrecto). Ahora prioriza nombre |
| 6 | `gratuito` no manejaba "Fee"/"paid" | Solo detectaba "free", el resto quedaba None |
| 7 | `web` (campo `url` del raw) no se mapeaba | Spots sin URL al original |
| 8 | `nombre = raw.get("name", "Campsite")` falla con `.lower()` si name es None | Defensive: `(raw.get("name") or "Campsite")` |
| 9 | `float(lat)` raw sin try/except | Defensive parse |

### Validación
Test sintético con sample real + 9 casos edge (sin red, API caída):

```
✓ sample real Canada: country=ca, rating 2.83→5.66, parking_publico, gratuito, web
✓ United States→us, Campground→camping, Fee→False
✓ USA→us, RV Park→camping
✓ México→mx, green icon→wild, 4.5→9.0
✓ Atlantis→None (país desconocido)
✓ country None → country_iso None
✓ rating None → no crash, num_reviews=0
✓ name=None defensive → 'Campsite'
✓ sin id → None
✓ coords None → None
```
**10/10 asserts pasados**.

### Cleanup DB aplicado
- 1560 spots solo-freecampsites con rating en escala vieja → re-escalado x2 (max 10)
- 282 spots con `tipo='parking'` legacy → `parking_publico`
- 1 zombie de scraper_log limpiado

## ⚠️ Notas operativas
- **Endpoints inestables**: durante esta auditoría tanto `androidApp.php` como `wp-json/comments` daban errores vacíos. El retry triple con backoff cubre fallos puntuales pero un scrape completo puede requerir varias re-ejecuciones.
- **Circuit breaker (2026-05-29)**: tras `CIRCUIT_THRESHOLD=8` fallos de conexión consecutivos (`ConnectError`/`ReadTimeout`/etc.), `fetch_cell` abre el circuito y devuelve `[]` sin seguir martilleando el host. Se resetea al primer éxito. Evita que un host caído consuma todo el run en timeouts.
- La fuente está limitada a **Norteamérica** por el filtro en `generate_active_grid`. Spots fuera de NA se descartan automáticamente.

---
**Estado Actual:** Auditado y hardened. Datos en DB re-escalados a convención 0-10. Cuando la API recupere disponibilidad, el próximo run rellenará correctamente country, region y web en los 2.248 spots existentes (vía COALESCE en `enriquecer_spot`).
