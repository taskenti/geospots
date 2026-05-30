# Plan: Motor de Verdad Geoespacial — Provenance, Contexto OSM y Entity Resolution

> Estado: propuesta aprobada para implementación por sprints.
> Fecha: 2026-05-30.
> Origen: síntesis de tres análisis independientes (Claude + 2 LLMs externos) sobre
> "usar OSM y Google como conciliadores/verificadores de conflictos".

---

## Estado de implementación

- ✅ **Sprint 0 (backend)** — `spot_field_provenance` + refactor `reconciliar`
  (`_reconciliar_campo_full`) + escritura batch + `/spot/:id` expone `provenance`.
- ✅ **Sprint 0 (UI operador)** — `admin.html`: panel "Reconciliación & Procedencia"
  con botón Reconciliar, cobertura provenance y contador de conflictos. Endpoints:
  `POST /admin/reconciliar/run`, `GET /admin/coverage/provenance`,
  `GET /admin/conflicts/count`. Scheduler ejecuta el job `reconciliar` desde la cola.
- ✅ **Sprint 5 v1 (ficha viajero)** — `pwa/spot.html` (pestaña enlazada desde
  index/admin): buscador, mapa Leaflet, contacto/servicios con **badges de procedencia**
  (✓ verificado / ⚠ conflicto + atribución Google), bloque de **contexto OSM** desde
  `spot_geo` y reseñas. Se adelantó a petición del usuario; se enriquecerá cuando
  Sprints 1/3 aporten más datos.
- ✅ **Sprint 1 (Google desempatador + ancla)** — `google_maps_api`: cola consciente de
  conflictos (incluye spots con `conflict_detected` en telefono/web, priorizados),
  detección de colisiones de `place_id` → `dedup_log` (candidato a merge, NO auto-merge).
  Provenance se realimenta vía `--reconciliar` (Google escribe su `source_record` con
  base_score 0.90 → la próxima reconciliación lo incorpora). UI operador en `admin.html`:
  panel Google con **gauge de presupuesto** mensual, conflictos procesados y colisiones.
  Endpoints: `GET /admin/google/budget`, `GET /admin/dedup/collisions`.
- ⏳ Pendiente: aplicar migraciones + correr contra DB real (entorno aún sin configurar).
  Luego Sprint 2 (entity resolution: auditoría dedup + anclas).

## TL;DR — La conclusión del debate

Los tres análisis convergen en lo mismo una vez se conoce la arquitectura real:

1. **El motor de verdad ya existe** (reconciliación ponderada, observaciones atómicas,
   decay temporal, `spot_field_overrides`, conflictos). El cuello de botella **no** es
   construir un "evidence engine".
2. **Google no puede ser verificador universal** — $200/mes ≈ 4.080 Place Details. Solo
   sirve como **desempatador racionado de alto valor** y como **ancla de identidad**
   (`google_place_id`).
3. **OSM no sirve para verificar contacto** (tags `phone`/`website` irregulares) pero es
   **una mina como capa de contexto/proximidad** (`spot_geo`, hoy a 0 filas).
4. **No migrar a JSON embebido** (`phone: {value, confidence}`) — rompe el hot path de
   PostGIS/SQL/embeddings. Columnas planas + **tabla lateral de procedencia**.
5. **Entity resolution es el núcleo del valor**, pero el ROI inmediato no está en
   rehacerlo, sino en (a) persistir la confianza que ya se calcula y (b) convertir 250k
   spots en spots **contextualizados espacialmente**.

### Decisiones tomadas (supuestos — revertibles)

| Decisión | Elección | Por qué |
|---|---|---|
| Infra OSM | **Híbrido**: piloto Overpass (ES) → PBF→PostGIS si valida | Evita invertir disco/RAM de NAS antes de probar ROI |
| Entity resolution | **Anclas + auditoría**, sin re-merge masivo del 1M | Re-merge masivo es irreversible y arriesgado sin métricas |
| Provenance | **Alto valor + dinámicos** (~10-12 campos) | Mejor señal/ruido; tabla manejable (~2-3M filas) |

---

## Principios de diseño (lo que NO se hace)

- ❌ **No** mover atributos canónicos a JSON con confianza embebida. Rompe filtros SQL,
  índices PostGIS y embeddings.
- ❌ **No** usar Google como verificador en tiempo real de todos los conflictos.
- ❌ **No** usar OSM como autoridad de teléfono/web/email.
- ❌ **No** lanzar una campaña de re-merge sobre el ~1M de spots sin métricas + quarantine.
- ✅ Columnas planas canónicas (rápidas) + capas laterales de procedencia/evidencia/geo.
- ✅ `place_id`/`osm_id` como **señales fuertes**, nunca como verdad absoluta única
  (caso peligroso: "Camping A entrada principal" vs "Área AC asociada" → entidades
  distintas en Google/OSM y ambas correctas).

---

## SPRINT 0 — Persistir procedencia y confianza (ROI #1, esfuerzo bajo)

**Problema:** `reconciliar.py` ya calcula voto ponderado, margen de desempate, fuentes de
soporte y detección de conflicto — pero **lo descarta** al escribir un valor plano en
`spots`. La API y los LLMs no tienen acceso a "qué fuentes respaldan este dato y con qué
fiabilidad".

**Entregables**

1. **Migración** `db/migration_provenance.sql`:
   ```sql
   CREATE TABLE IF NOT EXISTS spot_field_provenance (
       spot_id            INT REFERENCES spots(id) ON DELETE CASCADE,
       field              TEXT NOT NULL,
       winning_value      TEXT,                 -- truncado a 500 chars
       confidence         REAL NOT NULL DEFAULT 1.0,
       consensus_margin   REAL,                 -- (winner_w - second_w)/total
       supporting_sources TEXT[] DEFAULT '{}',
       conflict_detected  BOOLEAN DEFAULT FALSE,
       updated_at         TIMESTAMPTZ DEFAULT NOW(),
       PRIMARY KEY (spot_id, field)
   );
   CREATE INDEX IF NOT EXISTS idx_sfp_spot ON spot_field_provenance(spot_id);
   CREATE INDEX IF NOT EXISTS idx_sfp_conflict ON spot_field_provenance(conflict_detected)
       WHERE conflict_detected = TRUE;
   ```

2. **Refactor de `reconciliar._reconciliar_campo`** — devolver metadata en **una sola
   pasada** (no duplicar la función como propuso el 3er LLM; eso recalcula el voto dos
   veces). Firma nueva: `(value | KEEP_EXISTING | None, sources: list[str], confidence: float, margin: float, conflict: bool)`.
   - Reutilizar el `rank_pos` dict ya existente (O(1)) en vez de `rank.index()` en bucle.
   - **Semántica de confianza honesta** (mejora sobre el 0.5/1.0 hardcoded del 3er LLM):
     - Campos de voto ponderado: `confidence = winner_w / total` (cuota de peso),
       `margin = (winner_w - second_w)/total`.
     - Campos rank-first (web, telefono, direccion): `confidence = base_score` de la
       fuente ganadora (normalizado), `supporting_sources` = todas las fuentes con el
       mismo valor. `conflict = (#valores distintos > 1)`.

3. **`job_reconciliar`**: acumular `provenance_rows` y `executemany` con
   `ON CONFLICT (spot_id, field) DO UPDATE`. Restringir a `PROVENANCE_FIELDS`:
   ```python
   PROVENANCE_FIELDS = {
       "web", "telefono", "direccion_formateada", "tipo", "gratuito",
       "precio_aprox", "agua_potable", "electricidad",
       "vaciado_negras", "vaciado_grises",
   }
   ```

4. **API** `/spot/:id`: añadir bloque `provenance` (map field → {value, confidence,
   sources, conflict}). Habilita atribución Google (TOS) y respuestas LLM tipo *"el
   teléfono es fiable: coincide en Google, areasac y la web oficial"*.

**Criterio de done:** spots multifuente tienen filas en `spot_field_provenance`; la API
las expone; un spot con conflicto conocido muestra `conflict_detected=true` y las fuentes
en disputa.

**Riesgo / mitigación:** bloat → restringido a ~10 campos × solo spots multifuente
(`array_length(fuentes,1)>1`), ~2-3M filas (trivial para Postgres). VACUUM normal.

---

## SPRINT 1 — Google como desempatador de conflictos + ancla de identidad

Extiende el `google_maps_api` ya integrado (enriquecimiento dirigido, budget-gated).

**Entregables**

1. **Cola por conflicto**: ampliar la query de candidatos de `google_maps_api` para
   incluir también spots con `spot_field_provenance.conflict_detected = TRUE` en
   `telefono`/`web` (no solo los que tienen el campo NULL), priorizados por valor.
2. **place_id como ancla de identidad**: al fijar `google_place_id`, detectar colisiones
   (dos spots distintos → mismo place_id) y **registrarlas en `dedup_log`** como
   `manual_review=TRUE` (candidato a merge). **NO auto-merge.**
3. **Realimentar provenance**: cuando Google confirma uno de los valores en conflicto,
   añadirlo a `supporting_sources` y subir `confidence` del campo.

**Criterio de done:** conflictos de telefono/web en campings/áreas AC se arbitran dentro
del budget; colisiones de place_id quedan logueadas para revisión (no fusionadas).

**Riesgo:** quemar budget → reusar `GOOGLE_MAPS_DAILY_BUDGET`; los conflictos compiten con
los huecos por la misma cuota (priorizar conflicto sobre hueco).

---

## SPRINT 2 — Entity resolution: anclas + auditoría (sin re-merge masivo)

**Objetivo:** medir la calidad real del dedup y reforzar el matching **futuro** con
identificadores externos, sin tocar el backlog de forma destructiva.

**Entregables**

1. **Captura de claves de unión**: normalizar `telefono` (E.164) y `web` (dominio raíz)
   como columnas/índices; capturar `osm_id` donde la fuente OSM lo aporte.
2. **Auditoría de dedup**: job que muestrea pares cercanos y mide
   precisión/recall a partir de `dedup_log`; dashboard de tasas de falso-merge y
   falso-duplicado. **Sin esto, cualquier cambio en el dedup es a ciegas.**
3. **Señales fuertes en `find_spot_cercano`** (para nuevas ingestas, ponderadas, no
   absolutas): mismo `place_id`/`osm_id`/telefono-normalizado/dominio → fuerte evidencia
   de merge; pero seguir exigiendo coherencia espacial + nominal + de tipo. Guardar
   explícitamente los fallos conocidos:
   - Campings grandes con varias entradas (>100 m) → no partir.
   - Nombres genéricos ("Camping Municipal", "Aire de service") → exigir señal extra.

**Criterio de done:** existen números de precisión/recall sobre muestra; las ingestas
nuevas usan anclas; **cero** merges masivos sobre el 1M existente.

**Riesgo:** un ancla mal aplicada fusiona entidades correctas → todo merge por ancla pasa
por `dedup_log`/quarantine antes de consolidar.

---

## SPRINT 3 — Motor geoespacial OSM (piloto ES, Overpass) — ROI #2 (mayor diferenciación)

**Objetivo:** convertir spots en **spots contextualizados**. Esto es lo que casi nadie
tiene y no depende de usuarios, reseñas, LLM ni Google.

**Entregables**

1. **Finalizar schema `spot_geo`** (Phase 6) con distancias al vecino más cercano:
   `nearest_water_m`, `nearest_dump_station_m`, `nearest_supermarket_m`,
   `nearest_hiking_m`, `nearest_viewpoint_m`, `nearest_beach_m`, `nearest_bird_hide_m`,
   `nearest_fuel_m`, `nearest_pharmacy_m`, + `computed_at`.
2. **Pipeline piloto (solo ES)**: para cada spot, consulta Overpass cacheada en un radio
   (p.ej. 3 km) → calcula distancias al amenity más cercano por categoría → puebla
   `spot_geo`. Rate-limit + cache agresivo (Overpass es frágil).
3. **Integración**: incluir el contexto en (a) el texto compuesto de embeddings,
   (b) `/spot/:id`, (c) la respuesta de `/search/semantic`
   (*"Área gratuita. Agua a 180 m. Súper a 700 m. Observatorio de aves a 1.4 km."*).

**Criterio de done:** spots ES con `spot_geo` poblado; la respuesta semántica incluye
contexto de vecindario; validación de calidad sobre muestra (distancias plausibles).

**Riesgo:** Overpass no escala a 250k → es **piloto deliberado** para validar el modelo de
datos y el valor antes de invertir en PBF.

---

## SPRINT 4 — Escalado geoespacial PBF→PostGIS (condicional al piloto)

**Solo si el piloto del Sprint 3 valida el ROI.**

**Entregables**

1. Import de PBF (país→continente) a PostGIS vía `osm2pgsql`/`imposm`.
2. Sustituir Overpass por queries KNN locales (`<->` / `ST_DWithin`) — ilimitadas y
   rápidas.
3. Backfill de `spot_geo` para todos los spots; job nocturno incremental para spots
   nuevos/movidos.

**Criterio de done:** `spot_geo` poblado a escala mundial/europea; sin dependencia de
Overpass; refresh incremental.

**Riesgo:** disco/RAM de NAS. Mitigación: importar solo capas de amenities relevantes
(no el PBF completo), por región, con `--slim` y filtros de tags.

---

## Capa UI / observabilidad (transversal — un entregable por sprint)

Regla: **ningún sprint backend se da por cerrado sin su botón/indicador en la PWA.**
No hay "sprint UI" al final; cada sprint expone su propia superficie.

Estado del frontend hoy (auditado 2026-05-30):
- `pwa/index.html`, `pwa/admin.html`, `pwa/admin_enrichment.html` son **paneles de
  operador**. `admin.html` lista fuentes desde `/admin/scrapers` y las ejecuta con
  `POST /admin/scrapers/{nombre}/run`, con estado de worker, barra de progreso y
  prioritize/cancel.
- **No existe** ficha de spot ni mapa de viajero (el MapLibre+chat del CLAUDE.md no
  está en el repo). Eso es el Sprint 5.

**Gratis tras la migración:** `google_maps_api` aparece automáticamente como fila en
`admin.html` (está en `fuentes_config`), con su botón ▶ Run + progreso. `has_reviews_support`
debe quedar `false` para que NO muestre botón de reviews (punto muerto por diseño).

UI por sprint (en `admin.html`, salvo Sprint 5):

| Sprint | Indicador / botón | Endpoint API necesario |
|---|---|---|
| 0 | **Cobertura provenance** (% spots multifuente con provenance) + **contador de conflictos** por campo | `/admin/coverage/provenance`, `/admin/conflicts/count` |
| 0 | **Botón "Reconciliar"** + estado (reconciliar NO es una `fuente`, no aparece solo) | `POST /admin/reconciliar/run` + entrada en cola |
| 1 | **Gauge de presupuesto Google** ($ gastado mes / llamadas restantes) + nº conflictos resueltos | `/admin/google/budget` |
| 1 | **Cola de colisiones place_id** (candidatos a merge) con revisión manual | `/admin/dedup/collisions` |
| 2 | **Panel de auditoría dedup** (precisión/recall sobre muestra) | `/admin/dedup/audit` |
| 3 | **Estado pipeline OSM** (piloto ES: % spots con `spot_geo`, último run) | `/admin/coverage/geo` |
| 4 | Conmutar fuente geo (Overpass↔PBF) + progreso de backfill | `/admin/geo/status` |

**Nota de TOS (Google):** allí donde la UI muestre un dato cuya `supporting_sources`
incluya `google_maps_api`, mostrar atribución a Google. El bloque `provenance` de
`/spot/:id` ya trae las fuentes para renderizar el badge.

---

## SPRINT 5 — Ficha de viajero (frontend nuevo) [separado, posterior]

**Por qué separado:** no existe hoy y es un build de frontend completo; tiene más sentido
una vez que provenance + `spot_geo` tienen datos reales que enseñar (tras Sprints 0 y 3).

**Entregables**
1. Vista de detalle de spot (mapa + ficha) consumiendo `/spot/:id`.
2. **Badges de procedencia**: por campo, "✓ verificado" (alta confianza, varias fuentes)
   vs "⚠ en conflicto" (`conflict_detected`), con tooltip de fuentes. Atribución Google
   donde aplique.
3. **Bloque de contexto OSM**: "Agua a 180 m · Súper a 700 m · Observatorio de aves a
   1.4 km" desde `spot_geo`.
4. (Opc.) Integrar `/search/semantic` para la respuesta narrativa que ya combina todo.

**Criterio de done:** un viajero ve la fiabilidad de cada dato y el contexto del
vecindario sin leer JSON.

---

## Orden de ROI y dependencias

```
Sprint 0 (provenance)  ─┬─► Sprint 1 (Google desempatador, usa conflict_detected)
                        └─► Sprint 2 (entity res: provenance ayuda a auditar)
Sprint 3 (OSM piloto ES) ───► Sprint 4 (PBF a escala)  [condicional]

(cada sprint 0-4 incluye su UI de operador en admin.html)
Sprint 5 (ficha de viajero) ──► tras 0 y 3 (necesita datos reales que enseñar)
```

- **0 → 1 → 2** son la cadena de "verdad del dato" (barato, transversal).
- **3 → 4** son la cadena de "contexto espacial" (mayor diferenciación, más infra).
- 0 y 3 pueden ir en paralelo si hay manos; 4 nunca antes de validar 3.
- **UI de operador** va dentro de cada sprint; la **ficha de viajero** (Sprint 5) es un
  build de frontend aparte, posterior a tener datos de provenance + `spot_geo`.

## Métricas de éxito

| Métrica | Baseline | Objetivo |
|---|---|---|
| Campos con provenance poblada (spots multifuente) | 0 | >90% de PROVENANCE_FIELDS |
| Conflictos de telefono/web resueltos por Google/mes | 0 | dentro del budget, priorizados |
| Precisión de dedup (muestra auditada) | desconocida | **medida** (gate para Sprint 2b futuro) |
| Spots con `spot_geo` (ES, piloto) | 0 | >80% de spots ES activos |
| Colisiones place_id detectadas (candidatos merge) | 0 | logueadas, no auto-fusionadas |

## Lo que explícitamente queda fuera (de momento)

- Re-merge masivo del backlog de ~1M spots (requiere métricas del Sprint 2 + quarantine).
- `web_contact_extractor` para emails (job independiente, segunda iteración).
- Google como fuente de reviews (punto muerto permanente por TOS).
