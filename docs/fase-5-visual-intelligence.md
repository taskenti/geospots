# Fase 5 — Visual Intelligence
## Fotos como fuente de datos con CLIP

---

## La Idea

Las fotos de un spot contienen información que NINGUNA review ni descripción captura:

- ¿Hay árboles (sombra)?
- ¿Se ve el mar?
- ¿Es un parking de asfalto o hierba?
- ¿Parece urban o rural?
- ¿Hay señales de prohibición?
- ¿Qué tamaño tienen los vehículos que aparcan?
- ¿Parece seguro o abandonado?

Con modelos de visión como CLIP/SigLIP, extraemos esta información automáticamente.

---

## Pipeline de Fotos

```mermaid
graph TD
    A[URLs en fotos_urls JSONB] --> B[Descargar thumbnail 512px webp]
    B --> C[Almacenar en /data/photos/{spot_id}/]
    C --> D[CLIP embedding 512 dims]
    D --> E[Clasificación visual por prompts]
    E --> F[INSERT photo_analysis]
    F --> G[Agregar a spot_enrichments.visual_*]
```

---

## Tabla `photo_analysis`

```sql
CREATE TABLE photo_analysis (
    id          SERIAL PRIMARY KEY,
    spot_id     INT REFERENCES spots(id) ON DELETE CASCADE,
    photo_url   TEXT NOT NULL,
    local_path  TEXT,

    -- Embedding visual (CLIP ViT-B/32)
    embedding   vector(512),

    -- Clasificación visual automática
    has_trees       REAL,    -- 0-1
    has_water       REAL,    -- mar, lago, río
    has_mountain    REAL,
    has_beach       REAL,
    has_forest      REAL,
    is_urban        REAL,
    is_rural        REAL,
    parking_surface TEXT,    -- "asphalt", "gravel", "grass", "dirt"
    has_vehicles    BOOLEAN,
    vehicle_size    TEXT,    -- "small_van", "large_motorhome", "mixed"
    has_facilities  BOOLEAN, -- WC, fuentes visibles
    has_signs       BOOLEAN, -- Señales de prohibición/regulación
    aesthetic_score REAL,    -- Belleza visual 0-1

    processed_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(spot_id, photo_url)
);

CREATE INDEX idx_photo_spot ON photo_analysis(spot_id);
CREATE INDEX idx_photo_emb ON photo_analysis
    USING hnsw (embedding vector_cosine_ops);
```

---

## Clasificación Visual con CLIP

CLIP permite comparar imágenes contra textos arbitrarios sin entrenamiento:

```python
import clip
import torch
from PIL import Image

model, preprocess = clip.load("ViT-B/32")

# Prompts de clasificación
VISUAL_PROMPTS = {
    "has_trees":    ["a place with many trees", "a shadowy parking with trees"],
    "has_water":    ["a place near the sea", "a lake view", "a river nearby"],
    "has_mountain": ["mountain view", "hills in background"],
    "has_beach":    ["a beach", "sandy shore"],
    "has_forest":   ["dense forest", "woodland area"],
    "is_urban":     ["urban area", "city street", "buildings"],
    "is_rural":     ["rural countryside", "remote area", "nature"],
}

NEGATIVE_PROMPTS = {
    "has_trees":    ["a bare parking lot", "no vegetation"],
    "has_water":    ["no water visible", "dry land"],
    "is_urban":     ["countryside", "nature", "wilderness"],
    "is_rural":     ["city", "urban", "downtown"],
}

def clasificar_foto(imagen_path: str) -> dict:
    image = preprocess(Image.open(imagen_path)).unsqueeze(0)

    scores = {}
    for attr, pos_prompts in VISUAL_PROMPTS.items():
        neg_prompts = NEGATIVE_PROMPTS.get(attr, ["something else"])
        all_prompts = pos_prompts + neg_prompts

        text_tokens = clip.tokenize(all_prompts)

        with torch.no_grad():
            image_features = model.encode_image(image)
            text_features = model.encode_text(text_tokens)
            similarity = (image_features @ text_features.T).softmax(dim=-1)

        # Score = promedio de similitud con prompts positivos
        pos_score = similarity[0, :len(pos_prompts)].mean().item()
        scores[attr] = round(pos_score, 3)

    return scores
```

---

## Descarga Eficiente de Fotos

```python
import httpx
from pathlib import Path

PHOTOS_DIR = Path("/data/photos")

async def descargar_thumbnail(url: str, spot_id: int, idx: int) -> str | None:
    """Descarga foto, reescala a 512px webp."""
    dest = PHOTOS_DIR / str(spot_id) / f"{idx}.webp"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        return str(dest)

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return None

            # Reescalar con Pillow
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(r.content))
            img.thumbnail((512, 512))
            img.save(dest, "webp", quality=75)

            return str(dest)
    except Exception:
        return None
```

### Almacenamiento estimado

| Fotos | Tamaño 512px webp | Total |
|---|---|---|
| 500K spots × 2 fotos promedio | ~30 KB/foto | **~30 GB** |

> [!TIP]
> 30 GB es asumible en el NAS. Si crece mucho, guardar solo las 2 mejores fotos por spot.

---

## Búsqueda Multimodal (Texto → Imagen)

```python
async def buscar_por_descripcion_visual(conn, query: str, lat, lon, radio_km=50):
    """Busca spots cuyas fotos se parecen a una descripción visual."""

    # Embedding de texto con CLIP
    text_tokens = clip.tokenize([query])
    with torch.no_grad():
        query_embedding = model.encode_text(text_tokens)[0].tolist()

    rows = await conn.fetch("""
        SELECT DISTINCT ON (pa.spot_id)
            s.id, s.canonical_name, s.lat, s.lon,
            1 - (pa.embedding <=> $1::vector) AS visual_similarity
        FROM photo_analysis pa
        JOIN spots s ON s.id = pa.spot_id
        WHERE s.activo = TRUE
          AND ST_DWithin(s.geog, ST_SetSRID(ST_MakePoint($3, $2), 4326)::geography, $4)
        ORDER BY pa.spot_id, visual_similarity DESC
        LIMIT 20
    """, query_embedding, lat, lon, radio_km * 1000)

    return [dict(r) for r in rows]
```

**Ejemplo:** "parking con vista al mar y árboles" → busca fotos similares a esa descripción.

---

## Agregación Visual → Enrichment

Cuando un spot tiene 3+ fotos analizadas, se agregan los scores:

```python
def agregar_visual_scores(fotos: list[dict]) -> dict:
    """Promedia scores visuales de todas las fotos de un spot."""
    if not fotos:
        return {}

    campos = ["has_trees", "has_water", "has_mountain", "has_beach",
              "has_forest", "is_urban", "is_rural", "aesthetic_score"]

    resultado = {}
    for campo in campos:
        valores = [f[campo] for f in fotos if f.get(campo) is not None]
        if valores:
            resultado[f"visual_{campo}"] = round(sum(valores) / len(valores), 3)

    return resultado
```

Estos scores visuales se inyectan en `spot_enrichments` y en el texto para embedding vectorial.

---

## Hardware Necesario

| Componente | Requisito |
|---|---|
| CLIP ViT-B/32 | ~400 MB VRAM / 2 GB RAM (CPU posible pero lento) |
| Procesamiento | ~10 fotos/segundo en GPU, ~1/segundo en CPU |
| 1M fotos en CPU | ~12 días → GPU recomendada |
| 1M fotos en GPU | ~28 horas |

> [!IMPORTANT]
> Para el NAS sin GPU: procesar en lotes nocturnos o usar un servicio cloud temporal (Hetzner GPU ~$0.50/hora → 1M fotos ≈ $14).

---

## Métricas de Éxito

| Métrica | Objetivo |
|---|---|
| Fotos descargadas | ≥ 60% de spots con fotos |
| Fotos con embedding | 100% de las descargadas |
| Precisión clasificación visual | > 80% |
| Almacenamiento total | < 50 GB |
