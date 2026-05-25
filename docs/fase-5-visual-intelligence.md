# Fase 5 — Visual Intelligence
## Fotos como fuente de evidencia sensorial con Gemini Vision

> **Prerrequisito**: Phase 3 (spot_semantic_state poblado) + Phase 4 (embeddings vectoriales operativos)
> **Paradigma**: Las fotos son otro tipo de sensor — observaciones visuales que alimentan el mismo motor de estimación de estado.

---

## El Problema

Las reviews son observaciones **textuales** de humanos. Pero las fotos capturan información que **ninguna review describe**:

- ¿Hay árboles (sombra real, no percibida)?
- ¿Se ve el mar/montaña/lago?
- ¿Es asfalto, hierba o tierra?
- ¿Parece urban o rural?
- ¿Hay señales de prohibición?
- ¿Qué tamaño de vehículos aparcan ahí?
- ¿El entorno parece seguro o abandonado?
- ¿Cuántos vehículos hay (crowd)?

### Datos reales disponibles (Mayo 2026)

| Métrica | Valor |
|---|---|
| Spots con URLs de fotos | **338,888** (47% del total) |
| Total URLs de fotos | **1,143,459** |
| Media fotos/spot | **3.4** |
| Almacenadas en | `spots.fotos_urls` (JSONB array de URLs) |
| Fotos descargadas | **0** (todo en URLs externas) |
| Análisis visual | **0** (no existe pipeline) |

---

## Decisión Arquitectónica: CLIP vs Gemini Vision

| Aspecto | CLIP/SigLIP (plan viejo) | Gemini 2.0 Flash Vision (plan nuevo) |
|---|---|---|
| **Modelo** | ViT-B/32 local | Gemini 2.0 Flash (multimodal) |
| **Requiere GPU** | Sí (o muy lento en CPU) | No — API cloud |
| **Calidad clasificación** | ★★★☆☆ (prompt engineering frágil) | ★★★★★ (entiende contexto completo) |
| **Multiidioma** | ❌ (prompts en inglés) | ✅ |
| **Coste** | $0 local / $14 en GPU cloud | ~$0.0013/foto ($1.50/1K fotos) |
| **Output** | Scores float por prompt pair | JSON estructurado con señales de Phase 3 |
| **Integración Phase 3** | Requiere mapping manual → signal_types | Directo: extrae claims como el NLP |
| **Embedding visual** | Sí (512 dims, búsqueda multimodal) | No (pero no necesitamos — text embeddings de Phase 4 son suficientes) |
| **Hardware** | GTX 1650 4GB: ~3 fotos/s (lento) | Sin límite local |

**Decisión: Gemini 2.0 Flash Vision**

1. **Consistencia**: Usa el mismo modelo que Phase 3 (claim extraction) → señales uniformes
2. **Calidad**: Gemini entiende "esto parece un parking ilegal con riesgo de multa" — CLIP no
3. **Sin GPU**: La GTX 1650 haría 1.14M fotos en ~4.4 días. Gemini las hace en horas.
4. **Integración directa**: Las observaciones visuales entran como `extracted_claims` con `extractor='gemini_vision_2'`

### Coste estimado del batch completo

- No analizamos las 1.14M fotos — seleccionamos **2 fotos representativas por spot**
- 338K spots × 2 fotos = **~677K imágenes**
- Gemini Flash Vision: ~350 tokens/imagen + ~200 tokens prompt + ~100 tokens output
- Input: 677K × 550 tokens = 372M tokens × $0.10/1M = $37.20
- Output: 677K × 100 tokens = 67.7M tokens × $0.40/1M = $27.08
- **Total batch: ~$65**
- **Mensual incremental**: ~5K spots nuevos × 2 fotos = ~$1

> [!IMPORTANT]
> $65 es significativamente más que Phase 3 ($6) o Phase 4 ($0.18). Estrategia: procesar **solo spots HOT** (≥10 reviews, ~30K spots) primero = ~$6. Expandir gradualmente.

---

## Integración con Phase 3: Fotos como Sensor

Las fotos se tratan **exactamente como las reviews** en el pipeline de estimación de estado:

```
foto_url → descarga thumbnail → Gemini Vision → extracted_claims
         → observation_normalizer → state_aggregator → spot_semantic_state
```

La diferencia: `extraction_confidence` de fotos suele ser más alta que reviews para señales visuales (sombra, vistas, superficie), porque **la foto no miente**.

### Flujo completo

```
┌───────────────────────────────────────────────────────────┐
│  PASO 1: SELECCIÓN DE FOTOS (photo_selector.py)           │
│  - De spots.fotos_urls, seleccionar 2 mejores por spot    │
│  - Priorizar: exterior > interior, día > noche            │
│  - Descargar thumbnail 512px webp → /data/photos/         │
└──────────────────────┬────────────────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────────────────┐
│  PASO 2: ANÁLISIS VISUAL (visual_analyzer.py)             │
│  - Enviar a Gemini Flash Vision con prompt estructurado   │
│  - Extraer señales como extracted_claims                   │
│  - extractor = 'gemini_vision_2'                          │
│  - extraction_confidence = 0.85 (fotos son fiables)       │
└──────────────────────┬────────────────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────────────────┐
│  PASO 3: NORMALIZACIÓN + AGREGACIÓN (reutiliza Phase 3)   │
│  - observation_normalizer.py → normalized_observations    │
│  - state_aggregator.py → spot_semantic_state (UPSERT)    │
│  - Las señales visuales se mezclan con las textuales      │
│  - El decaimiento temporal NO aplica a fotos recientes    │
└───────────────────────────────────────────────────────────┘
```

---

## Schema: Nueva tabla `photo_analysis`

```sql
CREATE TABLE IF NOT EXISTS photo_analysis (
    id              SERIAL PRIMARY KEY,
    spot_id         INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    photo_url       TEXT NOT NULL,
    photo_index     SMALLINT NOT NULL DEFAULT 0,      -- Posición en fotos_urls
    local_path      TEXT,                              -- /data/photos/{spot_id}/{idx}.webp

    -- Resultado crudo del análisis Gemini Vision
    analysis_raw    JSONB,                             -- JSON completo de Gemini

    -- Scores visuales materializados (para queries rápidos)
    has_trees       REAL,         -- 0-1 (probabilidad de sombra)
    has_water       REAL,         -- Mar, lago, río visible
    has_mountain    REAL,
    has_beach       REAL,
    is_urban        REAL,
    is_rural        REAL,
    parking_surface TEXT,         -- 'asphalt', 'gravel', 'grass', 'dirt', 'mixed'
    vehicle_count   SMALLINT,    -- Vehículos visibles en la foto
    vehicle_types   TEXT[],      -- ['van', 'motorhome', 'car']
    has_facilities  BOOLEAN,     -- WC, fuentes, servicios visibles
    has_prohibition BOOLEAN,     -- Señales de prohibido aparcar/pernoctar
    aesthetic_score REAL,        -- Belleza visual 0-1
    crowd_visible   REAL,        -- Personas/vehículos visibles → masificación

    -- Metadatos
    analyzer        TEXT NOT NULL DEFAULT 'gemini_vision_2',
    claims_generated BOOLEAN DEFAULT FALSE,  -- TRUE cuando se crearon extracted_claims
    processed_at    TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(spot_id, photo_url)
);

CREATE INDEX IF NOT EXISTS idx_photo_spot ON photo_analysis(spot_id);
CREATE INDEX IF NOT EXISTS idx_photo_unprocessed ON photo_analysis(claims_generated) WHERE claims_generated = FALSE;
```

---

## Prompt de Análisis Visual

```python
VISION_PROMPT = """Analiza esta foto de un lugar para pernoctar con autocaravana/furgoneta camper.

Extrae SOLO lo que VES en la imagen. No inventes información.

Responde JSON:
{
  "scene_type": "parking|nature|beach|mountain|urban|rural|roadside",
  "surface": "asphalt|gravel|grass|dirt|mixed|unknown",
  "signals": [
    {"signal": "<signal_type_id>", "value": "<valor>", "confidence": <0-1>, "reason": "<qué ves>"}
  ],
  "vehicles": {
    "count": <int>,
    "types": ["van", "motorhome", "car"]
  },
  "prohibition_signs": <true|false>,
  "facilities_visible": <true|false>,
  "aesthetic_score": <0-1>,
  "description_short": "<1 frase describiendo el lugar>"
}

SEÑALES QUE PUEDES EXTRAER (signal_type_id):
- shade_morning / shade_afternoon: hay árboles/estructura que den sombra (boolean: "true"/"false")
- sea_view: se ve el mar (boolean)
- mountain_view: se ven montañas (boolean)
- lake_nearby: se ve un lago (boolean)
- beauty: belleza del entorno (numeric: "0.0"-"1.0")
- cleanliness: limpieza visible (numeric)
- crowd_level: masificación visible (numeric)
- large_vehicle: espacio para vehículos >7m (numeric)
- road_quality: calidad del acceso visible (numeric)
- stealth: discreción del lugar (numeric)
- safety: sensación de seguridad visual (numeric)
- overnight_safe: parece posible pernoctar (boolean)

Solo incluye señales que puedas deducir de la imagen. Si no se ve, no lo incluyas."""
```

---

## Pipeline de Selección de Fotos

No todas las fotos son útiles. Muchas son interiores de la furgo, selfies, o platos de comida.

```python
async def seleccionar_fotos(pool, batch_size=500):
    """
    Selecciona las 2 mejores fotos por spot para análisis visual.
    Estrategia: primera y última foto (suelen ser exterior/panorámica).
    """
    async with pool.acquire() as conn:
        spots = await conn.fetch("""
            SELECT s.id, s.fotos_urls
            FROM spots s
            LEFT JOIN photo_analysis pa ON pa.spot_id = s.id
            WHERE s.activo = TRUE
              AND s.fotos_urls IS NOT NULL
              AND s.fotos_urls != '[]'::jsonb
              AND pa.spot_id IS NULL  -- Sin análisis previo
            ORDER BY s.total_reviews DESC  -- HOT spots primero
            LIMIT $1
        """, batch_size)

    results = []
    for spot in spots:
        urls = spot['fotos_urls']  # JSONB array
        if not urls or len(urls) == 0:
            continue

        # Seleccionar: primera (portada) + última (suele ser panorámica)
        selected = [urls[0]]
        if len(urls) > 1:
            selected.append(urls[-1])

        for idx, url in enumerate(selected):
            results.append({
                "spot_id": spot['id'],
                "photo_url": url,
                "photo_index": idx
            })

    return results
```

---

## Descarga de Thumbnails

```python
import httpx
from pathlib import Path
from PIL import Image
from io import BytesIO

PHOTOS_DIR = Path("/data/photos")

async def descargar_thumbnail(url: str, spot_id: int, idx: int) -> str | None:
    """Descarga foto, reescala a 512px webp para análisis."""
    dest = PHOTOS_DIR / str(spot_id) / f"{idx}.webp"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        return str(dest)

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return None

            img = Image.open(BytesIO(r.content))
            img.thumbnail((512, 512))
            img.save(dest, "webp", quality=75)
            return str(dest)
    except Exception:
        return None
```

### Almacenamiento estimado

| Escenario | Fotos | Tamaño/foto | Total |
|---|---|---|---|
| Solo HOT spots (30K × 2) | 60K | ~30 KB | **1.8 GB** |
| Todos los spots con fotos (338K × 2) | 677K | ~30 KB | **20 GB** |

---

## Worker de Análisis Visual

```python
import google.genai as genai
import json, base64

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

async def analizar_foto_gemini(local_path: str) -> dict:
    """Envía foto a Gemini Vision y obtiene análisis estructurado."""
    with open(local_path, "rb") as f:
        image_bytes = f.read()

    response = await client.aio.models.generate_content(
        model="gemini-2.0-flash",
        contents=[
            {"text": VISION_PROMPT},
            {"inline_data": {"mime_type": "image/webp", "data": base64.b64encode(image_bytes).decode()}}
        ]
    )

    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        return None


async def process_visual_batch(pool, batch_size=50):
    """Worker: selecciona fotos, descarga, analiza, genera claims."""

    # 1. Seleccionar fotos pendientes
    fotos = await seleccionar_fotos(pool, batch_size)
    if not fotos:
        return {"processed": 0}

    processed = 0
    for foto in fotos:
        # 2. Descargar thumbnail
        local_path = await descargar_thumbnail(
            foto["photo_url"], foto["spot_id"], foto["photo_index"]
        )
        if not local_path:
            continue

        # 3. Analizar con Gemini Vision
        analysis = await analizar_foto_gemini(local_path)
        if not analysis:
            continue

        async with pool.acquire() as conn:
            async with conn.transaction():
                # 4. Guardar análisis en photo_analysis
                pa_id = await conn.fetchval("""
                    INSERT INTO photo_analysis (
                        spot_id, photo_url, photo_index, local_path,
                        analysis_raw, has_trees, has_water, has_mountain,
                        has_beach, is_urban, is_rural, parking_surface,
                        vehicle_count, vehicle_types, has_facilities,
                        has_prohibition, aesthetic_score, crowd_visible
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                    ON CONFLICT (spot_id, photo_url) DO UPDATE SET
                        analysis_raw = $5, processed_at = NOW()
                    RETURNING id
                """,
                    foto["spot_id"], foto["photo_url"], foto["photo_index"], local_path,
                    json.dumps(analysis),
                    # Extraer scores del analysis
                    _extract_score(analysis, "has_trees"),
                    _extract_score(analysis, "has_water"),
                    _extract_score(analysis, "has_mountain"),
                    _extract_score(analysis, "has_beach"),
                    _extract_score(analysis, "is_urban"),
                    _extract_score(analysis, "is_rural"),
                    analysis.get("surface"),
                    analysis.get("vehicles", {}).get("count"),
                    analysis.get("vehicles", {}).get("types"),
                    analysis.get("facilities_visible"),
                    analysis.get("prohibition_signs"),
                    analysis.get("aesthetic_score"),
                    _extract_signal_value(analysis, "crowd_level"),
                )

                # 5. Generar extracted_claims (se integran con Phase 3)
                for signal in analysis.get("signals", []):
                    await conn.execute("""
                        INSERT INTO extracted_claims (
                            review_id, spot_id, signal_type,
                            raw_value, extraction_confidence,
                            extractor_name, extractor_version,
                            excerpt
                        ) VALUES (
                            NULL, $1, $2, $3, $4,
                            'gemini_vision_2', '1.0',
                            $5
                        )
                    """,
                        foto["spot_id"],
                        signal["signal"],
                        str(signal["value"]),
                        signal.get("confidence", 0.85),
                        signal.get("reason", "visual analysis"),
                    )

                # 6. Marcar para re-agregación
                await conn.execute("""
                    UPDATE spot_semantic_state SET stale = TRUE WHERE spot_id = $1
                """, foto["spot_id"])

                await conn.execute("""
                    UPDATE photo_analysis SET claims_generated = TRUE WHERE id = $1
                """, pa_id)

        processed += 1

    return {"processed": processed}
```

> [!IMPORTANT]
> **`review_id = NULL` en extracted_claims**: Las observaciones visuales NO vienen de una review sino de una foto. La columna `review_id` debe ser `NULLABLE` en el schema de Phase 3. Cambio necesario:
> ```sql
> ALTER TABLE extracted_claims ALTER COLUMN review_id DROP NOT NULL;
> ```

---

## Agregación Visual → spot_semantic_state

Las claims visuales se agregan **automáticamente** por el `state_aggregator.py` de Phase 3 (ya existe). No hay código nuevo — el agregador procesa todos los `extracted_claims` sin importar si vienen de reviews o fotos.

La diferencia de peso:

| Fuente | extraction_confidence típica | Justificación |
|---|---|---|
| Review (regex) | 0.95 | Patrón explícito detectado |
| Review (Gemini NLP) | 0.70-0.90 | LLM infiriendo de texto |
| Foto (Gemini Vision) | 0.85 | La foto muestra la realidad directamente |

Las señales visuales **refuerzan o contradicen** las textuales. Ejemplo:
- 5 reviews dicen "con sombra" + foto muestra parking sin árboles → contradicción detectada
- El `contradiction_strategy` de `shade_morning` es `majority_consensus` → si la foto es reciente, gana

---

## Estructura de Archivos

```
c:\geospots\
├── enrichment/
│   ├── photo_selector.py        # ← NUEVO: selección de fotos por spot
│   ├── visual_analyzer.py       # ← NUEVO: análisis Gemini Vision + claims
│   ├── embedding_generator.py   # Phase 4: regenerar embedding tras visual update
│   └── ...                      # Phase 3: claim_extractor, normalizer, aggregator
├── jobs/
│   ├── nightly_visual.py        # ← NUEVO: cron batch visual (HOT spots primero)
│   └── ...
└── db/
    └── migration_phase5.sql     # ← NUEVO: CREATE photo_analysis + ALTER extracted_claims
```

---

## Estimación de Costes

### Batch inicial (estrategia gradual)

| Fase | Spots | Fotos | Coste Gemini | Storage |
|---|---|---|---|---|
| **Fase 5a**: Solo HOT (≥10 reviews) | ~30K | 60K | **~$6** | 1.8 GB |
| **Fase 5b**: WARM (3-9 reviews) | ~80K | 160K | **~$16** | 4.8 GB |
| **Fase 5c**: Todos con fotos | 338K | 677K | **~$65** | 20 GB |

### Mensual (incremental)

| Operación | Volumen | Coste |
|---|---|---|
| Spots nuevos con fotos | ~5K × 2 fotos | ~$1 |
| Re-análisis por fotos nuevas | ~1K fotos | ~$0.10 |
| **Total mensual** | | **~$1.10** |

---

## Métricas de Éxito

| Métrica | Objetivo |
|---|---|
| Spots con análisis visual | ≥ 80% de HOT spots |
| Precisión visual (validación manual 100 spots) | > 85% |
| Señales visuales que contradicen reviews | Detectar ≥ 50% de contradicciones |
| Almacenamiento thumbnails | < 25 GB |
| Latencia análisis/foto | < 2s (Gemini API) |

---

## Orden de Implementación

1. **ALTER** `extracted_claims.review_id` → nullable (`migration_phase5.sql`)
2. **Crear** tabla `photo_analysis` (`migration_phase5.sql`)
3. **Implementar** `photo_selector.py` — selección + descarga thumbnails
4. **Implementar** `visual_analyzer.py` — Gemini Vision + claims
5. **Test** con 50 spots HOT: verificar claims visuales en DB
6. **Correr** `state_aggregator` sobre spots con claims visuales → verificar que `spot_semantic_state` se actualiza
7. **Implementar** `nightly_visual.py` — cron batch
8. **Batch Fase 5a**: 30K HOT spots (~$6)
9. **Validar** métricas de precisión con 100 spots manuales
10. **Expandir** a Fase 5b/5c si los resultados son buenos
