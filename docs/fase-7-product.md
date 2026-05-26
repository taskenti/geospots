# Fase 7 — The Product
## PWA 2.0: El Google Maps camper que no existe

> **Prerrequisito**: Fases 3-6 operativas — `spot_semantic_state` poblado, embeddings generados, análisis visual y geo disponibles.

---

## La Visión

Nadie quiere abrir 7 apps. Nadie quiere buscar en 7 mapas.

GeoSpots se convierte en **la única app que necesitas** porque:
- Tiene TODOS los spots de TODAS las fuentes (**723K spots, 18+ fuentes**)
- Sabe más de cada spot que la propia fuente original (Phase 3 state estimation)
- Responde preguntas en lenguaje natural que ninguna app puede responder (Phase 4 vector search)
- Funciona offline en medio de la nada
- Se actualiza sola cada noche con nuevas reviews y cambios de estado

---

## Estado actual de la infraestructura (Mayo 2026)

| Componente | Estado | Listo para producto |
|---|---|---|
| **723K spots** canónicos multi-fuente | ✅ | ✅ |
| **466K reviews** ingestadas | ✅ | ✅ |
| `spot_semantic_state` pipeline | ✅ Phase 3 implementada | ⏳ Batch pendiente |
| Embeddings vectoriales | ✅ Phase 4 implementada | ⏳ Pendiente batch Phase 3 |
| Análisis visual (Gemini Vision) | ✅ Phase 5 diseñada | ⏳ Pendiente impl. |
| Geo analysis (DEM + OSM) | ✅ Phase 6 diseñada | ⏳ Pendiente impl. |
| API FastAPI `/search/semantic` | ✅ Phase 4 implementada | ✅ |
| PWA frontend | 🟡 Básica (MapLibre) | ⏳ Requiere Phase 7 |

---

## Flujo de Usuario Final

```
Usuario: "sitio tranquilo cerca del mar con sombra, que pueda ir con el perro"
           │
           ▼
     [Gemini Flash: intent extraction]
     → filters: {quietness_min: 0.7, overnight_safe: true, perros: true}
     → semantic_query: "quiet spot near sea with shade, dog friendly"
           │
           ├──► [PostGIS: spots en radio 50km]
           │
           ├──► [SQL: filtros semánticos sobre spot_semantic_state]
           │    quietness_score >= 0.7 AND overnight_safe = TRUE AND perros = TRUE
           │
           └──► [pgvector: cosine ranking sobre embeddings]
                    │
                    ▼
              Top 20 spots
                    │
                    ▼
         [Gemini + semantic_dsl → respuesta natural]
                    │
                    ▼
         📱 Mapa con pins + fichas enriquecidas
```

---

## Componentes de la PWA 2.0

### 1. Mapa Inteligente

```
┌──────────────────────────────────────┐
│  🔍 "sitio tranquilo cerca del mar"  │
├──────────────────────────────────────┤
│                                      │
│          [MAPA MapLibre]             │
│                                      │
│   🟢 gratuito  🔵 área AC           │
│   🟠 camping   ⚪ parking            │
│                                      │
│  ┌────────────────────────────────┐  │
│  │ 📍 Playa de Oyambre            │  │
│  │ ★★★★☆ 4.2 · 🆓 · 💧 · 🐕   │  │
│  │ 😌 0.82 · 🌅 0.91 · 🥷 0.55  │  │
│  │ 3 fuentes · 47 reviews         │  │
│  └────────────────────────────────┘  │
│                                      │
├──────────────────────────────────────┤
│ 😌 Tranq  🌊 Mar  🌲 Bosque  🥷 Stealth │
│ 🆓 Gratis  💧 Agua  🐕 Perros  ⚡     │
└──────────────────────────────────────┘
```

**Diferencias clave vs. versión actual:**
- Barra de búsqueda en lenguaje natural (no solo filtros booleanos)
- Mini-scores de Phase 3 visibles directamente en el popup (`quietness_score`, `beauty_score`)
- Chips de filtro semántico mapean a columnas de `spot_semantic_state`
- Indicador de confianza (nº de fuentes + reviews)

### 2. Ficha de Spot Enriquecida

```
┌──────────────────────────────────────┐
│ 📍 ÁREA AC GRATUITA                  │
│ Playa de las Catedrales              │
│ ★★★★☆ 4.3 · 142 reviews · 4 fuentes│
│ P4N · CamperContact · iOv · Furgo   │
├──────────────────────────────────────┤
│ 📸 [foto1]  [foto2]  [foto3]        │
├──────────────────────────────────────┤
│ ─── SCORES IA (Phase 3) ───          │
│ 😌 Tranquilidad    ████████░░  82%  │
│ 🛡️ Seguridad       ██████░░░░  61%  │
│ 🌅 Belleza          █████████░  95%  │
│ 🥷 Stealth          ███░░░░░░░  33%  │
│ 🔇 Silencio         ███████░░░  71%  │
│ 👥 Masificación     ██████████ 100% ⚠│
├──────────────────────────────────────┤
│ ─── ENTORNO GEO (Phase 6) ───        │
│ 🏖️ Costa: 200m  🌲 Bosque: 1.2km   │
│ ☀️ Sol mañana: 5h  ⛰️ Alt: 45m      │
│ 🅿️ Acceso: asfalto  🏞️ Zona protegida│
├──────────────────────────────────────┤
│ ─── SERVICIOS ───                    │
│ 🆓 Gratuito  💧 Agua  🚽 WC        │
│ 🐕 Perros OK  ⛺ Área AC            │
├──────────────────────────────────────┤
│ ─── RESUMEN IA ───                   │
│ "Área amplia frente a la playa de   │
│  las Catedrales. Muy turístico en   │
│  verano (julio-agosto lleno).        │
│  Fuera de temporada tranquilo.       │
│  Policía pasa ocasionalmente."       │
├──────────────────────────────────────┤
│ ─── REVIEWS RECIENTES ───            │
│ [CamperContact, mayo 2026] ★★★★★    │
│ "Reformaron los baños, muy limpio"  │
│ [P4N, marzo 2026] ★★★★☆            │
│ "Tranquilo fuera de temporada"       │
│                                      │
│ ⚠️ CONFLICTO: P4N dice gratis,      │
│ CC indica €5 en temporada alta      │
├──────────────────────────────────────┤
│ 💬 Preguntar  🗺️ Navegar  ❤️ Guardar│
└──────────────────────────────────────┘
```

### 3. Filtros Semánticos por Mood

Cada chip mapea directamente a columnas de `spot_semantic_state` y `spots`:

```python
MOOD_FILTERS = {
    "😌 Tranquilo":    {"sss.quietness_score": (">=", 0.7)},
    "🌊 Playa":        {"geo.dist_coast_km": ("<=", 2.0)},
    "🌲 Bosque":       {"geo.landuse_type": ("=", "forest")},
    "🥷 Discreto":     {"sss.stealth_score": (">=", 0.6)},
    "🌅 Vistas":       {"sss.beauty_score": (">=", 0.8)},
    "🐕 Perros":       {"s.perros": ("=", True)},
    "🆓 Gratis":       {"s.gratuito": ("=", True)},
    "💧 Agua":         {"s.agua_potable": ("=", True)},
    "⚡ Servicios":    {"s.electricidad": ("=", True)},
    "👨‍👩‍👧 Familias":    {"sss.crowd_level_score": ("<=", 0.5), "sss.safety_score": (">=", 0.7)},
    "🏕️ Sin gente":   {"sss.crowd_level_score": ("<=", 0.3)},
    "🔒 Pernocta OK":  {"sss.overnight_safe": ("=", True)},
}
```

### 4. Modo Offline Completo

Para funcionar sin cobertura:

```
┌─────────────────────────────────────┐
│        DESCARGAR ZONA               │
│                                     │
│  📍 Mi posición actual              │
│  📏 Radio: [25km] [50km] [100km]   │
│                                     │
│  Incluir:                           │
│  ☑ Tiles de mapa               12MB│
│  ☑ Spots + semantic_state       3MB│
│  ☑ semantic_dsl (búsqueda)      1MB│
│  ☐ Fotos thumbnails            45MB│
│  ☐ Reviews completas            8MB│
│                                     │
│  Total estimado: 16 MB              │
│  [DESCARGAR ZONA]                   │
└─────────────────────────────────────┘
```

**Implementación:**
- **Tiles mapa**: PMTiles + Protomaps (un solo archivo por bbox)
- **Spots + state**: IndexedDB con `spot_semantic_state` pre-filtrado por bbox
- **Búsqueda offline**: `semantic_dsl` compacto + búsqueda local por keywords
- **Sin embeddings offline**: Demasiado pesados (~300MB para 300K spots). Offline usa filtros SQL sobre `semantic_dsl`.

### 5. Chat con el Mapa

```
Usuario: "¿Hay algún sitio seguro donde pueda quedarme 2 noches?"
Bot: "Sí, te recomiendo:
      1. Área AC Oyambre (2.3km) — overnight_safe ✅, police_risk bajo,
         sin eventos recientes. 142 pernoctas confirmadas.
      2. Parking Las Gaviotas (5.1km) — gratuito, sin problemas
         reportados en últimos 60 días."

Usuario: "¿El primero tiene sombra por la tarde?"
Bot: "Según análisis de terreno (DEM), orientación oeste con vegetación
      moderada → sombra parcial de 16h en adelante en verano."
```

**Endpoint**: `/search/semantic` (Phase 4) + `semantic_events` (Phase 3) para alertas activas.

---

## Arquitectura Técnica Completa

```
┌────────────────────────────────────────────────────────────┐
│                      PWA (MapLibre)                         │
│  MapLibre GL JS  ·  Service Worker  ·  IndexedDB offline  │
└───────────────────────────┬────────────────────────────────┘
                            │ HTTPS
┌───────────────────────────▼────────────────────────────────┐
│                   FastAPI (puerto 18889)                     │
│  /points  /spot/{id}  /search  /search/semantic            │
│  /search/visual  /dashboard  /health                        │
└───┬──────────────┬──────────────┬──────────────┬───────────┘
    │              │              │              │
    ▼              ▼              ▼              ▼
PostgreSQL    Gemini Flash    Gemini Vision   Google Embed
(PostGIS +    (chat, intent,  (foto analysis) (text-embedding
 pgvector)     summaries)                      -004)

┌────────────────────────────────────────────────────────────┐
│               Pipeline nocturno (crons)                     │
│                                                            │
│  enrichment.worker   → reviews → extracted_claims         │
│  enrichment.worker   → claims → normalized_observations   │
│  state_aggregator    → observations → spot_semantic_state │
│  embedding_generator → state → spot_embeddings            │
│  visual_analyzer     → fotos → photo_analysis + claims    │
│  geo_analyzer        → coords → spot_geo + claims         │
│  event_detector      → bursts → semantic_events           │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│               Scrapers (18+ fuentes)                        │
│  park4night · campercontact · caramaps · promobil         │
│  roadsurfer · stayfree · furgovw · vansite · ...          │
│  → reviews + source_records → spots canónicos             │
└────────────────────────────────────────────────────────────┘
```

---

## Nuevos Endpoints API para Phase 7

Añadir a [api/main.py](file:///c:/geospots/api/main.py):

### `/spot/{id}/full` — Ficha completa enriquecida

```python
@app.get("/spot/{spot_id}/full")
async def spot_full(spot_id: int):
    """Devuelve toda la información disponible de un spot para la ficha."""
    async with pool.acquire() as conn:
        spot = await conn.fetchrow("""
            SELECT
                s.*,
                sss.quietness_score, sss.safety_score, sss.beauty_score,
                sss.police_risk_score, sss.stealth_score, sss.crowd_level_score,
                sss.overnight_safe, sss.semantic_dsl, sss.signals_data,
                sss.summary_es, sss.summary_en, sss.tags, sss.best_for,
                sss.total_observations, sss.consensus_confidence,
                sg.elevation_m, sg.dist_coast_km, sg.dist_lake_km,
                sg.stealth_geo_score, sg.noise_combined, sg.protected_area,
                sg.protected_area_name, sg.landuse_type,
                sg.sun_morning_summer, sg.sun_afternoon_summer
            FROM spots s
            LEFT JOIN spot_semantic_state sss ON sss.spot_id = s.id
            LEFT JOIN spot_geo sg ON sg.spot_id = s.id
            WHERE s.id = $1
        """, spot_id)

        reviews = await conn.fetch("""
            SELECT source, rating, autor, fecha, texto_limpio, idioma
            FROM reviews
            WHERE spot_id = $1 AND informativo = TRUE
            ORDER BY fecha DESC NULLS LAST
            LIMIT 10
        """, spot_id)

        events = await conn.fetch("""
            SELECT event_type, severity, first_seen, expires_at
            FROM semantic_events
            WHERE spot_id = $1 AND active = TRUE
        """, spot_id)

        sources = await conn.fetch("""
            SELECT source, source_url, total_reviews
            FROM source_records
            WHERE spot_id = $1
        """, spot_id)

    return {
        "spot": dict(spot),
        "reviews": [dict(r) for r in reviews],
        "events": [dict(e) for e in events],
        "sources": [dict(s) for s in sources],
    }
```

### `/spot/{id}/ask` — Chat con un spot concreto

```python
@app.post("/spot/{spot_id}/ask")
async def spot_ask(spot_id: int, question: str = Body(...)):
    """Responde preguntas sobre un spot específico."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT s.canonical_name, sss.semantic_dsl, sss.summary_es,
                   sss.signals_data, sg.dist_coast_km, sg.elevation_m,
                   sg.sun_morning_summer, sg.protected_area_name
            FROM spots s
            LEFT JOIN spot_semantic_state sss ON sss.spot_id = s.id
            LEFT JOIN spot_geo sg ON sg.spot_id = s.id
            WHERE s.id = $1
        """, spot_id)

    context = f"""Spot: {row['canonical_name']}
DSL semántico: {row['semantic_dsl']}
Resumen: {row['summary_es'] or 'No disponible'}
Distancia costa: {row['dist_coast_km']}km
Altitud: {row['elevation_m']}m
Zona protegida: {row['protected_area_name'] or 'No'}"""

    response = await client.aio.models.generate_content(
        model="gemini-2.0-flash",
        contents=f"Datos del spot:\n{context}\n\nPregunta: {question}\n\nResponde de forma concisa."
    )
    return {"answer": response.text}
```

### `/map/events` — Alertas activas en una región

```python
@app.get("/map/events")
async def map_events(lat: float, lon: float, radio_km: float = 100):
    """Devuelve eventos activos (police_burst, theft_spree) en un radio."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT se.event_type, se.severity, se.first_seen, se.expires_at,
                   s.id, s.canonical_name, s.lat, s.lon
            FROM semantic_events se
            JOIN spots s ON s.id = se.spot_id
            WHERE se.active = TRUE
              AND ST_DWithin(
                  s.geog,
                  ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography,
                  $3
              )
            ORDER BY se.severity DESC
        """, lat, lon, radio_km * 1000)
    return [dict(r) for r in rows]
```

---

## Tablas Nuevas para Phase 7

```sql
-- Favoritos y visitas del usuario
CREATE TABLE IF NOT EXISTS user_favorites (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,   -- hash anónimo del dispositivo
    spot_id     INT NOT NULL REFERENCES spots(id) ON DELETE CASCADE,
    notes       TEXT,
    visited     BOOLEAN DEFAULT FALSE,
    visit_date  DATE,
    rating_user SMALLINT CHECK (rating_user BETWEEN 1 AND 5),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, spot_id)
);

CREATE INDEX IF NOT EXISTS idx_fav_user ON user_favorites(user_id);

-- Rutas planificadas
CREATE TABLE IF NOT EXISTS user_routes (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    spots       INT[] NOT NULL,   -- Array ordenado de spot_ids
    total_km    REAL,
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Estructura de Archivos Phase 7

```
c:\geospots\
├── api/
│   └── main.py        MODIFICAR: añadir /spot/{id}/full, /spot/{id}/ask,
│                                  /map/events, favoritos, rutas
├── pwa/               MODIFICAR: UI completa Phase 7
│   ├── index.html     Mapa principal + barra búsqueda natural
│   ├── spot.html      Ficha enriquecida completa
│   ├── offline.html   Gestión de zonas offline
│   └── sw.js          Service Worker para offline
└── db/
    └── migration_phase7.sql   CREATE user_favorites, user_routes
```

---

## Monetización (Cuando llegue el momento)

### Modelo Freemium

| Tier | Funcionalidades | Precio |
|---|---|---|
| **Free** | Mapa + filtros básicos + 10 consultas IA/día | $0 |
| **Pro** | Búsqueda natural ilimitada + offline + chat + rutas | €2.99/mes |
| **Pro+** | API access + export GPX + alertas proximidad + sin ads | €6.99/mes |

### B2B / API

| Producto | Cliente | Precio |
|---|---|---|
| API spots enriquecidos | Apps de navegación, alquiler campers | €0.01/query |
| Dataset bulk mensual | Agregadores, investigadores | €500/mes |
| White-label widget | Webs de turismo, booking | €200/mes |

---

## Métricas de Éxito del Producto

| Métrica | Objetivo (6 meses post-lanzamiento) |
|---|---|
| Spots en DB | > 750.000 |
| Spots con `spot_semantic_state` | > 300.000 |
| Spots con embeddings | > 250.000 |
| Spots con geo analysis | > 400.000 |
| Latencia búsqueda natural | < 1.5 segundos |
| Latencia ficha completa | < 300ms |
| Modo offline funcional | ✅ (tiles + semantic_state) |
| Fuentes integradas | ≥ 15 |
| PWA instalable iOS/Android/Desktop | ✅ |

---

## Lo que hace ÚNICO a este proyecto

1. **Nadie más fusiona 18+ fuentes** en un spot canónico con deduplicación real
2. **Nadie más tiene state estimation temporal** — las señales decaen, los eventos expiran, el estado evoluciona
3. **Nadie más combina** reviews + fotos + geodatos en el mismo pipeline de evidencia
4. **Búsqueda vectorial geoespacial** con filtros semánticos pre-computados → 10x más rápido que solo pgvector
5. **`semantic_dsl`** permite prompts LLM 85% más baratos → buscador IA económicamente viable a escala

> "Park4Night tiene los spots. CamperContact tiene los precios. Furgovw tiene la comunidad española. iOverlander tiene el offgrid.
>
> **Nosotros tenemos TODO fusionado, enriquecido con IA, actualizado cada noche, y buscable por voz.**
>
> Y lo mismo sirve para setas, pesca, surf, fotografía nocturna, astronomía — cualquier actividad que necesite encontrar EL lugar perfecto."
