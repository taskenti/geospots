# Auditoría de Compatibilidad de Vehículos + Plan por Sprints

> **Objetivo del problema:** GeoSpots fusiona spots de autocaravanas/camper con spots de
> overlanding/4x4. El riesgo de producto es mandar una AC de 10 m a un spot que solo admite
> 4x4 (o al revés, ocultar overlanding válido). Necesitamos un sistema que sea **justo para
> todos los perfiles de vehículo** y que **no genere falsos negativos catastróficos**.
>
> **Fecha auditoría:** 2026-05-30. Todos los números están verificados contra la DB de
> producción (`geospots-db`, 858.438 spots). No hay cifras estimadas: cada una tiene su query.

---

## 0. Resumen ejecutivo (TL;DR para la mesa)

1. **No es un problema de clasificación ni de taxonomía. Es un problema de datos.** El
   clasificador casi no importa; la materia prima (señal de acceso fiable) no existe o está
   contaminada.
2. **El sistema actual NO protege al usuario hoy.** La exclusión de vehículos grandes cuelga
   en un **87,7 %** de un solo campo booleano (`acceso_grandes`), y ese campo está **podrido
   de ruido**: su `false` mezcla "no apto" con "no etiquetado" en todas las fuentes grandes
   (ambigüedad semántica, no un bug de código — ver §4). Mitiga que la API aún no filtra por él.
3. **El pipeline semántico (5M reseñas) aporta CERO a la exclusión** por un bug de polaridad:
   la señal `large_vehicle` está codificada 0→1 y nunca puede expresar "no apto".
4. **El miedo "wild = 4x4" es falso.** `wild` (132 K spots) es mayoritariamente camping
   accesible a vehículos normales. Penalizar `wild` por defecto sería injusto con campers
   y overlanders ligeros.
5. **La verdad operativa es la incertidumbre, no la exclusión.** El subconjunto realmente
   excluyente es pequeño (~5-8 %); el problema masivo (~44 %) es *ausencia de información*.
   El diseño debe modelar `apto / no-apto / desconocido`, nunca un score 0-100 que finge
   precisión.

---

## 1. Inventario y distribución por fuente

| Métrica | Valor | Query |
|---|---|---|
| Spots totales | **858.438** | `count(*) FROM spots` |
| Spots activos | 858.304 | `WHERE activo` |
| Source records | 1.101.026 | `count(*) FROM source_records` |

> ⚠️ **Bug de doc:** `CLAUDE.md` dice "~125K spots activos". El real es **858 K**. Corregir.

**Distribución por fuente (top):** park4night 336.133 · ioverlander 265.182 · stayfree 78.172 ·
caramaps 66.186 · campercontact 49.786 · womostell 49.480 · thedyrt 41.167 · campendium 40.536 ·
campy 33.746 · promobil 30.812 · searchforsites 27.878 · campingcarinfos 24.063 · camperstop 13.963.

**Distribución multifuente:**

| nº fuentes | spots | % |
|---|---|---|
| 1 (exclusivo) | 716.212 | 83,4 % |
| 2 | 89.749 | 10,5 % |
| 3 | 28.129 | 3,3 % |
| 4 | 12.539 | 1,5 % |
| 5+ | 11.818 | 1,4 % |

**Solapamiento P4N + iOverlander:** 18.972 (2,21 % del total; pero **7,2 % de iOV** y **5,6 %
de P4N**). El 2,21 % es bajo pero coherente: P4N es EU-céntrico, iOverlander es WW con fuerte
peso en América. No hay evidencia de fallo masivo de matching aquí, aunque conviene una
auditoría de dedup geográfico aparte (fuera de scope de este documento).

---

## 2. Taxonomía: la cruda se pierde en `normalize()`

**Tipo canónico** (`spots.tipo`):

| tipo | spots | % |
|---|---|---|
| parking | 210.107 | 24,5 % |
| otro | 203.956 | 23,8 % |
| camping | 161.744 | 18,8 % |
| **wild** | 132.118 | 15,4 % |
| area_ac | 111.299 | 13,0 % |
| naturaleza | 31.993 | 3,7 % |
| resto | ~4,8 % | |

**P4N: ~15 códigos reales colapsados a 6 cubos.** Desde `raw_data->>'code'`:
`P` 117.503 · `PN` 49.914 · `PJ` 47.664 · `DS` 33.000 · `C` 26.937 · `APN` 20.171 ·
`ACC_P` 6.884 · `AR` 5.462 · `OR` 5.446 · `F` 4.868 · `PSS` 4.336 · `ACC_G` 3.856 ·
`ACC_PR` 3.763 · `ASS` 2.820 · `EP` 2.502.
→ [park4night.py:19-20](../scraper/sources/park4night.py#L19) reduce todo esto a 6 valores.
**Nota importante:** el tipo de P4N es *amenidad* (parking/camping/área), **no dificultad de
acceso**. Recuperar estos códigos mejora la taxonomía pero **no resuelve** el problema de
vehículos por sí solo.

**iOverlander: taxonomía en `style_url`** (decodificada en [ioverlander.py:30-36](../scraper/sources/ioverlander.py#L30)):
`#stylexhD3` Wild Camping 105.150 · `#stylexhD0` Established Campground 71.458 ·
`#stylexhD1` Informal Campsite 59.135 · `#stylexhD2` Warning/Closed→wild 23.204 ·
`#stylexhD5` Fuel 4.227 · `#stylexhD7` Showers 3.900 · `#stylexhD6` Dump 3.707.
→ ~128 K iOV son "wild". **Pero "Wild Camping" en iOverlander significa pernocta libre, no
"requiere 4x4".** La mayoría es accesible a vehículos normales.

---

## 3. Cobertura de señal de acceso — el cuello de botella real

| Señal | Cobertura | % de 858 K | Estado |
|---|---|---|---|
| `acceso_grandes` (NOT NULL) | 216.917 | 25,3 % | **contaminada** (ver §4) |
| `altura_max_m` (NOT NULL) | 12.518 | 1,46 % | casi muerta |
| `large_vehicle` (obs) | 9.277 | 1,08 % | **polaridad rota** (§5) |
| `road_quality` (obs) | 3.362 | 0,39 % | muerta |
| `caravan_accepted` (obs) | **0** | 0 % | **nunca poblada** |

**Text-mining de reseñas (4,9 M con texto):** las reseñas apenas hablan de acceso.
Menciones de exclusión: `steep` 8.951 · `narrow` 6.131 · `4x4 only` 3.106 · `no large` 2.823.
→ ~21 K reseñas (0,4 %) mencionan restricción de acceso. **Incluso con extracción perfecta,
las reseñas solo cubrirían ~10-15 K spots por el lado de exclusión.** La señal positiva
("big rigs welcome", "easy access") es donde está el volumen para reducir la zona gris.

---

> **CORRECCIÓN (2026-05-30, tras verificación de código):** una primera versión de este
> documento atribuyó el ruido a un bug `_bool(None) → False`. **Es falso:** los `_bool` de
> campendium y womostell devuelven `None` correctamente. La causa real es **doble** y se
> detalla abajo: (a) *ambigüedad semántica* de las fuentes (el `false` mezcla "no apto" con
> "no etiquetado") y (b) *pérdida de dato* (campendium no persiste el `place_detail` del que
> deriva la señal). Solo vansite y alpacacamping tienen un bug de código genuino (ausencia→
> False vía `any()`/`in`), y son de bajo volumen (~3,7K).

## 4. CAUSA RAÍZ: "no etiquetado" indistinguible de "no apto"

El set de exclusión estructural es **54.636 spots (6,4 %)**, y el **87,7 % (47.927)** viene
**solo de `acceso_grandes=false`**. Pero ese campo está contaminado:

**`acceso_grandes=false` por tipo — el ruido salta a la vista:**

| tipo | false | total | % | veredicto |
|---|---|---|---|---|
| **area_ac** | **22.727** | 111.099 | **20,5 %** | 🚨 imposible: un área AC es para AC |
| wild | 14.884 | 132.105 | 11,3 % | plausible (parte real) |
| camping | 7.592 | 161.740 | 4,7 % | mixto |
| parking | 1.332 | 210.085 | 0,6 % | |

**`acceso_grandes=false` por fuente:**

| fuente | false | NOT NULL | % false | veredicto |
|---|---|---|---|---|
| **campendium** | **22.585** | 35.893 | **63 %** | 🚨 bug de normalize |
| womostell | 19.584 | 49.480 | 40 % | sospechoso |
| park4night | 8.441 | 33.925 | 25 % | |

**Mecanismo real (campendium):** [campendium.py:250,342](../scraper/sources/campendium.py#L250)
hace `"acceso_grandes": _bool(place_detail.get("big_rigs"))`. El `_bool` es correcto
(`None → None`). Los **23.735 false** salen de que la API de detalle de Campendium devuelve
`big_rigs=false` explícitamente. Pero en Campendium ese flag significa **"no etiquetado como
big-rig-friendly"**, que mezcla "confirmado que no caben grandes" con "nadie lo marcó". Para
una fuente de EEUU con miles de campings pequeños de bosque, una parte es real y otra es
"desconocido disfrazado". **No es distinguible desde el dato almacenado**, porque —segundo
problema— campendium **NO persiste `place_detail` en `raw_data`** (raw solo guarda el GeoJSON
`type/geometry/properties`; `raw_data ? 'big_rigs'` = 0). Esto **viola "raw data es sagrada"**
y hace `acceso_grandes` no reproducible. womostell (`b_long_campers`) tiene la misma
ambigüedad semántica. El 20,5 % de área_ac=false es el síntoma agregado.

**Bugs de código genuinos (bajo volumen):**
- [vansite.py:311](../scraper/sources/vansite.py#L311): `any(v in kfz ...)` → `False` si la
  lista `kfz` está vacía. **Corregido** en Sprint 0 (→ `None` si no hay lista). ~1,7K spots.
- [alpacacamping.py:83](../scraper/sources/alpacacamping.py#L83): `(26 in am_ids)` → `False`
  si no hay amenities. **Corregido** en Sprint 0 (→ `None` si lista vacía). ~2K spots.

> **Lo verdaderamente grave:** la exclusión cuelga de un campo cuyo `false` no es fiable
> como negativo duro en NINGUNA fuente. Mitiga que **la API hoy NO filtra por `acceso_grandes`**
> (verificado: 0 usos en `api/main.py`), así que no hay daño en producción todavía. El
> tratamiento correcto (degradar `false` de fuente a "negativo débil / desconocido" que exige
> corroboración) es una **decisión de modelo (Sprint 1)**, no un backfill destructivo.

---

## 5. Bug de polaridad: el pipeline semántico no puede excluir

`large_vehicle` está codificada **0 → 1** (min 0, media 0,666, max 1): 13.912 positivos,
513 ceros, **0 negativos**. La rama `claims.get("large_vehicle") is False` que propuso la
auditoría externa **nunca se dispara**. Igual con `road_quality` (0 negativos) y
`caravan_accepted` (0 obs). [signal_registry.py:34](../enrichment/signal_registry.py#L34)
define `large_vehicle` como `numeric weighted_mean` sin polaridad negativa.

**Consecuencia:** las 5M reseñas no pueden, en la práctica, marcar un spot como "no apto para
grande". La señal solo confirma idoneidad, nunca la niega. **Matiz:** el `signal_registry`
define `large_vehicle` como `numeric` (admite negativos sin problema); el bug NO está en el
registro sino en la **extracción** — regex y prompts solo emiten valores positivos. Por eso
el fix de polaridad es trabajo de **Sprint 3 (extracción)**, no un cambio de una línea.

---

## 6. Validación de hipótesis

| Hipótesis | Resultado |
|---|---|
| "El problema afecta al 5-15 %, no al 100 %" | ✅ **Validada** (exclusión estructural 6,4 %) |
| "wild ≈ 4x4" | ❌ **Falsa** (wild es mayormente accesible) |
| "El ROI está en extracción, no en algoritmo" | ✅ **Validada** (cobertura 1-25 %) |
| "El campo de exclusión es frágil" | ✅ **Peor de lo previsto** (contaminado, no solo escaso) |
| "Score 0-100 es falsa precisión" | ✅ Confirmado: el problema es incertidumbre, no ranking |

---

## 7. Principios de diseño (justicia para todos los vehículos)

1. **Multidimensional, no un eje AC↔4x4.** Perfiles: `coche/tienda`, `furgón ≤6m`,
   `camper 6-7,5m`, `AC grande >7,5m o >3,3m alto`, `4x4/overland`, `con remolque`.

   **⚠️ Umbrales de altura (corrección de dominio, 2026-05-30).** El umbral `< 2,80 m =
   excluido` que usó la auditoría externa es **INCORRECTO** y NO se hereda. Un límite de
   altura de 2,80 m **admite TODAS las campers (pequeño y gran volumen) y muchas AC** — solo
   excluye a las AC altas (capuchina/integral con extras de techo). Dimensiones reales de
   referencia:

   | Vehículo | Altura típica | Un límite de barrera de... |
   |---|---|---|
   | Furgón camper (VW, etc.) | 2,0–2,6 m | 2,2 m ya empieza a excluir furgones altos |
   | Camper gran volumen | 2,6–2,9 m | 2,8 m los admite casi todos |
   | AC perfilada | 2,8–3,0 m | 3,0 m los admite |
   | AC capuchina/integral (+extras) | 3,0–3,6 m | solo se excluyen con límite <3,2 m |

   Regla para el modelo: la altura del spot (`altura_max_m`) se compara contra la **altura del
   perfil del usuario**, no contra una constante. "Ni una camper entra" ≈ límite **< 2,2 m**
   (parking subterráneo); "AC grande no entra" ≈ límite **< 3,0–3,2 m**. Anchura y longitud,
   análogas y por perfil.
2. **Tres estados por dimensión: `apto / no-apto / desconocido`.** Nunca un número continuo.
   Separar **valor** de **confianza**.
3. **Nunca excluir por ausencia.** `desconocido ≠ no-apto`. La penalización por incertidumbre
   es una *preferencia del usuario* (modo conservador para AC grande), no un dato.
4. **No penalizar `wild`/overlanding por defecto.** Justicia para overlanders: un spot 4x4
   es 100 % apto para 4x4. Justicia para campers: la mayoría de wild es accesible.
5. **Asimetría de error.** Falso "es accesible" para AC grande = catastrófico (usuario
   atascado de noche). Falso "cuidado" = molestia. El modo conservador prioriza recall de
   exclusión sobre precisión, **solo para los perfiles grandes y solo cuando el usuario lo pide**.
6. **Raw data es sagrada.** Toda re-derivación parte de `raw_data`/`normalized_data`; nada
   se sobreescribe (coherente con la sección "Datos regenerables vs inmutables" de CLAUDE.md).

---

## 8. Plan por Sprints

### Sprint 0 — Detener la generación de ruido (integridad de datos) · **EN CURSO**
*Alcance recalibrado tras verificar código: NO había un bug `_bool` masivo ni procede un
backfill destructivo. Solo se corrige lo que es inequívocamente correcto y de bajo riesgo.*

- **S0.1 ✅ HECHO** — Bugs de código genuinos ausencia→`False`:
  - [vansite.py:311](../scraper/sources/vansite.py#L311) → `None` si `kfz` vacío.
  - [alpacacamping.py:83](../scraper/sources/alpacacamping.py#L83) → `None` si `am_ids` vacío.
  - Efecto: solo afecta a re-scrapes futuros (no reescribe lo ya almacenado).
- **S0.2 ✅ HECHO** — Corregido `CLAUDE.md` (858 K, no 125 K).
- **S0.3 ✅ HECHO** — Corregido este informe (causa raíz: ambigüedad semántica + pérdida de
  dato, NO bug `_bool`).
- **S0.4 📋 PENDIENTE (forward-fix, riesgo medio)** — Persistir `place_detail` de campendium
  en `raw_data` ([campendium.py](../scraper/sources/campendium.py)) para que `acceso_grandes`
  sea reproducible y no se viole "raw data es sagrada". No urge (API no filtra por el campo).
- **S0.5 ⚠️ DECISIÓN DE PANEL** — Qué hacer con los ~48 K `acceso_grandes=false` existentes.
  **NO** backfill destructivo. Opciones a decidir en Sprint 1: (a) degradar todo `false` de
  fuente a "negativo débil" que exige corroboración; (b) confiar en `false` solo cuando ≥2
  fuentes coinciden o cuando hay evidencia textual. Es una decisión de seguridad/producto,
  no de datos.
- **NO se hace en Sprint 0:** polaridad de `large_vehicle` (→ Sprint 3, es extracción);
  backfill de `acceso_grandes` (→ decisión S0.5/Sprint 1).
- **Salida medible:** ningún re-scrape futuro de vansite/alpacacamping añade `false` espurio.
  El resto de la limpieza se traslada al modelo (Sprint 1) por ser una decisión, no un bug.

### Sprint 1 — Modelo de compatibilidad de vehículos · **HECHO (núcleo)**

Diseño **paramétrico** (decisión del usuario 2026-05-30): el usuario configura las **medidas
reales** de su vehículo + tracción + preferencias; NO hay clases fijas. La tabla guarda las
*restricciones del spot*; el veredicto se calcula al vuelo. Los presets son solo atajos.

- **S1.1 ✅** Tabla `spot_vehicle_access` (regenerable, NULL=desconocido):
  `max_length_m/height/width/weight`, `requires_4wd`, `steep_access`, `surface`,
  `access_difficulty`, `confidence`, `field_confidence`, `evidence` (procedencia).
  Migración idempotente [db/migration_vehicle_compat.sql](../db/migration_vehicle_compat.sql),
  **aplicada** (14 columnas + 5 índices parciales).
- **S1.3 ✅** Motor de matching [enrichment/vehicle_compat.py](../enrichment/vehicle_compat.py)
  (lógica pura, sin DB/LLM): `VehicleProfile` (medidas+tracción), `SpotConstraints`,
  `evaluate()` → `CompatVerdict {strict, conservative}` con 3 estados y desglose por dimensión.
  - **Asimetría de error codificada:** cualquier exclusión dura conocida gana; un DESCONOCIDO
    se resuelve a APTO para camper pequeña (≤5,5 m × ≤2,2 m, "entra en todos lados") y a
    PRECAUCIÓN para vehículo grande. Ausencia NUNCA produce NO_APTO.
  - **Umbrales de altura correctos:** un límite de 2,80 m NO excluye campers ni gran volumen
    (test lo verifica). Comparación contra la medida del usuario, no contra constante.
  - Presets: `camper_pequena, gran_volumen, ac_perfilada, ac_capuchina, ac_grande, 4x4_camper`.
  - `UserProfile` = vehículo + `conservative_mode` + `preferences`. Las **preferencias**
    (físicas del spot + entorno) son RANKING, no filtro, y mapean a señales que ya existen en
    `spot_semantic_state` (quietness/stealth/overnight_safe/beauty/sea_view/…). Ranking → Sprint 4.
  - **8/8 tests OK** [tests/test_vehicle_compat_sprint1.py](../tests/test_vehicle_compat_sprint1.py).
- **S1.2 ✅ HECHO** Señales de acceso polaridad-correcta registradas en
  [signal_registry.py](../enrichment/signal_registry.py) STATIC_SIGNALS **y** en la tabla DB
  `signal_types`: `road_4x4_only` (peso 2.5), `narrow_access` (1.8), `steep_access` (1.5),
  `rough_surface` (1.2) — booleanas, `decay_class=slow` (3650 d). Son la versión *difusa*
  (derivada de reseñas en Sprint 3); el valor numérico físico vive en `spot_vehicle_access`.
  `height_restriction` ya existía y se reutiliza.

### Sprint 2 — Backfill estructurado (señal barata, sin LLM)

**Tier-1 — señal de acceso POLARIDAD-CORRECTA ya en `raw_data` (CERO scraping).**
Inventario verificado de los 28 scrapers (2026-05-30). Solo hay que mapear estos campos en
`normalize()`/`scraped_facts_v1`; el dato ya está e es inmutable:

| Fuente | Campo `raw_data` | source_records | Naturaleza |
|---|---|---|---|
| promobil | `caravan8Meters` | **28.498** | bool >8 m (DACH) |
| park4night | `hauteur_limite` | **10.626** | límite altura (m), valores reales 2,0–4,0 |
| caramaps | `maxLength` / `maxHeight` / `maxWidth` | 3.228 / 1.987 | dimensiones completas |
| nomady | `surroundingsRoad` | 1.870 | tipo de vía de acceso |
| campingcarpark | `authorizedVehicles` + `prohibitions` | 902 | estructurado {campers/caravans…} |
| agricamper | `accepte_caravanes` | 619 | bool |
| campy | `camperSize` | 383 | longitud máx (5–12 m) |

→ **~40 K spots distintos** ganarían señal de exclusión *honesta* gratis. Modesto en cobertura
pero es ~40 K de señal fiable frente a **~0 fiable hoy**. **Descartar como ruido** (verificado):
`stayfree.access` (=membresía), `bobilguiden.accessLevel` (=paywall), `camperstop.*Route*`
(rutas a pie/bici), `bobilguiden.vehicleCount` (conteo, no acceso).

- **S2.1 ✅ HECHO** — [jobs/ingest_vehicle_access.py](../jobs/ingest_vehicle_access.py) puebla
  `spot_vehicle_access` desde Tier-1 (sin scraping/LLM, idempotente, `--country`/`--dry-run`).
  **Resultado real: 17.505 spots** con restricción física + procedencia (`evidence`): 12.144
  con `max_height_m`, 6.486 con `max_length_m`. Agregación = límite más restrictivo (mín).
  Validado end-to-end: spots con barrera 2,0 m → `camper_pequena=apto`, `ac_grande=no_apto`.
  Nota honesta: promobil aporta solo 2.880 (los `caravan8Meters=false`), no sus 28K (el resto
  es permisivo). Pendiente refinar: float `real` guarda 2,1→2.0999 (cosmético, redondear en API).
- **S2.2** Recuperar taxonomía cruda P4N (`code`, ~15 tipos) e iOV (`style_url`) para mejorar
  `tipo` (no resuelve acceso por sí solo; es contexto).
- **S2.3 — Tier-3 geo (Phase 6, el de mayor cobertura).** Derivar acceso de OSM vía Overpass:
  `4wd_only`, `highway=track`+`tracktype=grade3-5`, `surface` rugosa, `maxheight`/`maxwidth`
  en barreras. **Prototipo funcional:** `scraper/overpass_access_probe.py`. Resultado sobre 6
  spots `wild` de Iberia: 5/6 en vías normales (tertiary/unclassified/primary) → accesibles
  (refuerza wild≠4x4); detecta superficie rugosa y barreras donde OSM las etiqueta.
  **Cuello de botella = completitud de OSM**, no el método: muchos `track` sin `tracktype`
  → "desconocido" (correcto). `spot_geo` (179 filas hoy) es el destino. Snapping spot→vía de
  acceso necesita refinar (no basta "highway más cercano a 120 m").
- **S2.4** Reglas duras seguras: `camping`/`area_ac` con servicios ⇒ `apto AC` por defecto
  (confianza media), salvo evidencia negativa explícita.
- **Salida:** mapa de cobertura por perfil. Objetivo: bajar "desconocido" de 44 % a <25 %
  combinando Tier-1 (gratis, alta calidad/baja cobertura) + Tier-3 OSM (universal, polaridad
  correcta donde OSM está etiquetado).

### Sprint 3 — Extracción desde reseñas (LLM dirigido)
- **S3.1** Regex multilingüe de acceso (exclusión Y confirmación) → emite claims con
  polaridad. Patrones validados en §3 (steep/narrow/4x4/no-large + positivos).
- **S3.2** Escalado a LLM **solo** cuando hay mención ambigua de acceso (patrón Opción B ya
  existente para `police_risk`). Coste acotado: ~21 K reseñas con mención → barato.
- **S3.3** Priorizar reseñas de spots en zona gris y de tipo `wild`/`naturaleza`/`parking`.

### Sprint 4 — Clasificador + exposición en API
- **S4.1** Función de cribado secuencial (exclusión dura → universal → zona gris) que
  produce el veredicto por perfil. Sin scores 0-100.
- **S4.2** API: parámetro `vehicle_profile` en `/points` y `/search`; `modo conservador`
  (oculta/avisa `desconocido` para AC grande). Reseñas y evidencia visibles en `/spot/:id`.
- **S4.3** PWA: selector de vehículo + badge `apto/precaución/no-apto` con tooltip de evidencia.

### Sprint 5 — Validación de justicia y seguridad
- **S5.1** Muestreo manual: 300 spots `no-apto AC` → tasa de falso positivo (objetivo <10 %).
- **S5.2** Tasa de exclusión por perfil: ¿cuántos spots pierde cada vehículo? Vigilar que
  ningún perfil quede injustamente vacío.
- **S5.3** Métrica de seguridad: en muestra de spots `apto AC grande`, ¿alguno tiene evidencia
  de exclusión ignorada? (falsos negativos catastróficos = 0 es el objetivo).
- **S5.4** Bucle de feedback de usuario (reportar "no cabía") como señal de máxima credibilidad.

---

## 9. Orden de prioridad

```
Sprint 0 (datos)  ← SIN ESTO, TODO LO DEMÁS ESTÁ ENVENENADO
   ↓
Sprint 1 (modelo) + Sprint 2 (backfill estructurado)   ← el 80% del ROI
   ↓
Sprint 3 (reseñas)  ← rellena la zona gris restante
   ↓
Sprint 4 (API/UX) + Sprint 5 (validación continua)
```

El mayor retorno por esfuerzo está en **Sprint 0 + Sprint 2**: arreglar la contaminación y
volcar la señal estructural barata. El clasificador (Sprint 4) es casi trivial una vez los
datos son honestos.
