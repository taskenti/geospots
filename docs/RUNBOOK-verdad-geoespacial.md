# RUNBOOK — Motor de Verdad Geoespacial (Provenance + Google + OSM)

> **Fuente única de verdad de "qué falta por hacer".** Este archivo se actualiza
> EN CADA COMMIT de la iniciativa. Si algo está en código pero NO marcado como
> aplicado abajo, es que falta ejecutarlo contra la DB / configurar el entorno.
>
> Plan de diseño completo: `docs/plan-verdad-geoespacial.md`.
> Última actualización: 2026-05-30 (Sprints 0 y 1 en código).

---

## ✅ APLICADO EN CÓDIGO (commiteado — no requiere acción tuya)

### Iniciativa Google API (turno 1)
- `scraper/sources/google_maps_api.py` — fuente Places API (New), enriquecimiento
  dirigido de contacto. Reviews en punto muerto.
- `db/migration_google_api.sql` — columnas `spots.google_place_id`,
  `direccion_formateada`, `google_last_refreshed` + `source_credibility` +
  `fuentes_config` para `google_maps_api`.
- `scheduler.py` — registrada `google_maps_api`.
- `reconciliar.py` — jerarquía de contacto (telefono/web/direccion) + `_limpiar_web`.
- Fix: `google_maps.py` (DOM) ahora acepta `job_id` (antes → TypeError en cola).
- `.env.example` — variables `GOOGLE_MAPS_*`.

### Sprint 0 — Procedencia/confianza por campo
- `db/migration_provenance.sql` — tabla `spot_field_provenance`.
- `reconciliar.py` — `_reconciliar_campo_full` (valor + sources + confidence +
  margin + conflict), `PROVENANCE_FIELDS`, escritura batch.
- `api/main.py` — `/spot/:id` expone `provenance` y `geo`; endpoints
  `POST /admin/reconciliar/run`, `GET /admin/coverage/provenance`,
  `GET /admin/conflicts/count`.
- `scheduler.py` — ejecuta el job `reconciliar` desde la cola.
- `pwa/admin.html` — panel "Reconciliación & Procedencia".

### Sprint 1 — Google desempatador + ancla de identidad
- `google_maps_api.py` — cola consciente de conflictos (`conflict_detected` en
  telefono/web, priorizados) + detección de colisiones de `place_id` → `dedup_log`
  (candidato a merge, NO auto-merge).
- `api/main.py` — `GET /admin/google/budget`, `GET /admin/dedup/collisions`.
- `pwa/admin.html` — panel "Google" (gauge presupuesto + colisiones).

### Ficha viajero (Sprint 5 v1, adelantado)
- `pwa/spot.html` — buscador + mapa + badges de procedencia + contexto OSM + reviews.
- `pwa/index.html` — pestaña enlazando a la ficha.

---

## ⏳ PENDIENTE DE APLICAR (acciones manuales — entorno aún sin configurar)

Ejecutar **en este orden**. Marca `[x]` a medida que lo hagas.

- [ ] **1. Configurar `.env`** (Google Places API):
  ```
  GOOGLE_MAPS_API_KEY=...            # con "Places API (New)" + FACTURACIÓN activa en GCP
  GOOGLE_MAPS_DAILY_BUDGET=135
  GOOGLE_REFRESH_DAYS=30
  GOOGLE_MATCH_DISTANCE_M=150
  GOOGLE_MATCH_NAME_SIM=0.6
  GOOGLE_MAPS_RATE_LIMIT=0.2
  ```
  ⚠ Sin facturación activa la API devuelve error aunque te quedes dentro del crédito.
  Configura además presupuesto + cuota diaria en la consola de GCP.

- [ ] **2. Aplicar migraciones** (idempotentes), en orden:
  ```bash
  psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_google_api.sql
  psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_provenance.sql
  ```

- [ ] **3. Primera reconciliación** (puebla `spot_field_provenance` + marca conflictos):
  ```bash
  docker-compose exec scraper python scheduler.py --reconciliar
  # o el botón "▶ Reconciliar ahora" en admin.html
  ```
  Verifica en admin.html → panel "Reconciliación & Procedencia": cobertura > 0%.

- [ ] **4. Enriquecimiento Google** (SOLO con billing activo y budget probado):
  ```bash
  docker-compose exec scraper python scheduler.py --google_maps_api
  # o el botón "▶ Enriquecer con Google" en admin.html
  ```
  Vigila el gauge de presupuesto en admin.html → panel "Google".

- [ ] **5. Reconciliar de nuevo** (incorpora lo que Google confirmó/desempató):
  ```bash
  docker-compose exec scraper python scheduler.py --reconciliar
  ```

- [ ] **6. Verificar UI**:
  - admin.html: paneles con datos reales (cobertura, conflictos, presupuesto, colisiones).
  - spot.html: buscar un spot enriquecido → badges "✓ verificado" / "⚠ conflicto" + "vía Google".

- [ ] **7. Revisar colisiones de place_id** (cola de merge manual): `GET /admin/dedup/collisions`.
  NO se fusionan solas — decisión manual (Sprint 2 dará la auditoría).

---

## ⚠ RECORDATORIOS PERMANENTES (gotchas)

1. **El orden importa**: `--reconciliar` (genera conflictos) → `google_maps_api`
   (los consume) → `--reconciliar` (incorpora Google). Saltarse el primer reconciliar
   = Google solo rellena huecos, no desempata.
2. **`spot_field_provenance` debe existir** antes del primer `--reconciliar`, o el
   `executemany` falla. (migración paso 2).
3. **Google reviews = punto muerto permanente** (TOS + sin texto masivo). La fuente
   NO descarga reviews por diseño.
4. **No auto-merge**: las colisiones de `place_id` son candidatos, nunca fusión
   automática. Hasta tener la auditoría del Sprint 2, revisar a mano.
5. **Presupuesto Google**: ~$49/1000 spots (1 search + 1 details). $200/mes ≈ 4080
   spots. El gauge en admin.html calcula el gasto del mes desde `scraper_log`.
6. **No arrancar el contenedor `enrichment` en loop sin throttling** si hay billing
   en Gemini (ver CLAUDE.md § "Lecciones aprendidas").
7. **Provenance solo para spots multifuente** (`array_length(fuentes,1)>1`) y ~10
   campos de alto valor (`PROVENANCE_FIELDS` en reconciliar.py).

---

## 📋 PENDIENTE DE IMPLEMENTAR (próximos sprints — código)

- [ ] **Sprint 2 — Entity resolution**: auditoría de dedup (precisión/recall sobre
  muestra desde `dedup_log`), normalización de telefono (E.164) + web (dominio) +
  captura `osm_id` como señales fuertes en `find_spot_cercano`. SIN re-merge masivo.
  UI: panel de auditoría dedup en admin.html.
- [ ] **Sprint 3 — Motor OSM (piloto ES)**: poblar `spot_geo` vía Overpass cacheado
  para spots ES; integrar en embeddings + `/spot` + `/search/semantic`. UI: estado
  pipeline geo (% poblado). NOTA: `spot_geo` ya existe con columnas (`dist_*_km`,
  elevation, noise, protected_area) — la ficha viajero ya las pinta.
- [ ] **Sprint 4 — Escalado geo (PBF→PostGIS)**: condicional a que el piloto valide.
- [ ] **Sprint 5 — Ficha viajero v2**: enriquecer cuando haya datos reales de
  provenance + spot_geo (v1 ya en `pwa/spot.html`).

---

## 🧾 Historial de commits de la iniciativa

> Rama: `feature/provenance-google-geo`. Se actualiza en cada commit.

| Commit | Contenido |
|---|---|
| `d212dd5` | Sprints 0-1 + ficha viajero (spot.html) + plan + este runbook. (Agrupado con cleanup_dedup_shells.py por un add concurrente.) |
| _(turno previo, en main)_ | google_maps_api source + migration_google_api.sql + fix job_id google_maps DOM + .env.example + CLAUDE.md |

---

## 🔍 Comandos de verificación (no tocan DB)

```bash
python -m py_compile api/main.py scraper/scheduler.py scraper/reconciliar.py scraper/sources/google_maps_api.py
python -m tests.test_source_signatures      # 30/30 fuentes aceptan job_id
python -m pytest tests/test_reconciliar.py -q
```
