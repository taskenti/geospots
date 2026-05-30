# RUNBOOK — Motor de Verdad Geoespacial (Provenance + Google + OSM)

> **Fuente única de verdad de "qué falta por hacer".** Este archivo se actualiza
> EN CADA COMMIT de la iniciativa. Si algo está en código pero NO marcado como
> aplicado abajo, es que falta ejecutarlo contra la DB / configurar el entorno.
>
> Plan de diseño completo: `docs/plan-verdad-geoespacial.md`.
> Sprint 4 (PBF local) ✅ — España al 100% (52K spots) en 4 min, modo local.
> Última actualización: 2026-05-30 (Sprints 0, 1, 2, 3 y 4 en código; aplicados y
> validados contra la DB real excepto Sprint 1/Google que requiere API key).

## ✅ VALIDACIÓN 2026-05-30 (contra DB real)

- Migraciones aplicadas: google_api, provenance, entity_keys, geo_osm (idempotentes, OK).
- **Backfill claves**: 82.699 telefono_norm + 59.807 web_domain (osm_id=0, no hay
  source_records osm).
- **Reconciliar**: 142K spots multifuente → **93,2% cobertura provenance**
  (132.547 spots). Conflictos detectados: tipo 41.844, web 41.418, telefono 14.777…
- **geo_osm** (piloto ES, batch 8): 4 spots con contexto OSM real (p.ej. agua 67m,
  súper 167m). Overpass público devolvió 504/429 en los otros 4 → backoff los manejó.
- **Ficha `/spot/264928`**: bloques `provenance` (con confidence/sources/conflict) y
  `geo` (OSM) servidos correctamente.
- **Endpoints admin**: coverage/provenance, conflicts/count, coverage/geo, dedup/audit,
  google/budget → OK (tras `docker compose restart api` para recargar código).
- 🐛 **Bug encontrado y reparado por la auditoría**: `extract_domain`/`_limpiar_web` no
  excluían varios agregadores (campendium.com 32K spots, womo-stellplatz.eu 21K,
  furgovw.org, nomady.camp) ni plataformas genéricas (facebook, fs.usda.gov, koa…).
  Se añadieron a `EXCLUDED_DOMAINS` + nuevo `NON_IDENTITY_DOMAINS`. Tras re-backfill,
  el mayor grupo de web_domain bajó de 32.324 a 78 spots.
- ⏸ **Sprint 1 (Google) NO validado**: `.env` sin `GOOGLE_MAPS_API_KEY`. Endpoints
  responden (presupuesto a $0). Pendiente de configurar clave + billing.

### Búsqueda semántica — Canales C/B/A (código listo)
- ✅ **Canal C**: la respuesta LLM menciona el entorno cercano.
- ✅ **Canal B**: 13 filtros `max_dist_*_km` (intent LLM + SQL sobre nearby_osm/nearby_spots).
- ✅ **Canal A (código)**: contexto geo en el texto del embedding; modelo corregido a
  `gemini-embedding-001` (text-embedding-004 fue RETIRADO → 404) con
  output_dimensionality=768 + task_type. `nightly_embeddings --country --loop`.
- ⛔ **BLOQUEADO**: generar embeddings da `429 RESOURCE_EXHAUSTED` — el proyecto
  Gemini tiene el **spend cap mensual agotado**. Acciones:
  1. Subir el cap en https://ai.studio/spend (coste: ES ~$0.14, ~50K spots ~$1.2).
  2. `docker compose exec enrichment python -m jobs.nightly_embeddings --country es --loop`
  3. Luego el resto sin `--country` (FR/DE/… sin geo, solo semántico — degradación OK).
  Hasta generar embeddings, `/search/semantic` devuelve vacío (JOIN spot_embeddings).
- ⚠ **Colisión con sesión paralela**: el refactor (sin commitear) de `reconciliar.py`
  (`_canon_value`) convirtió `telefono` en campo de voto ponderado → el test
  `test_full_rankfirst_confidence_is_base_score_margin_none` (mío, Sprint 0) falla
  porque asumía rank-first. Decidir diseño (telefono voto vs rank-first) y ajustar
  el test. NO tocado para no interferir con el WIP paralelo.

### ✅ Sprint 4 — PBF local (RESUELTO; sustituye a Overpass para bulk)

**España al 100% — 52.072 spots con contexto OSM en 4 min (0 errores), modo local.**
- `osm_pois`: 101.949 POIs ES importados del PBF (agua 42.947, farmacia 17.611,
  súper 17.525, mirador 11.641, gasolinera 11.537, vaciado 688).
- `geo_context.py` ahora tiene **modo local** (KNN sobre `osm_pois`) + fallback
  Overpass. `GEO_OSM_MODE=auto` elige local si hay datos del país.
- Overpass queda solo como fallback/piloto. Los **3 crons de Overpass se ELIMINARON**
  (eran crons de sesión de Claude, no sobrevivían — decisión errónea, corregida).
- POIs viven en `osm_pois`, tabla SEPARADA de `spots`. NUNCA son spots. Solo se
  consultan para calcular distancias en `spot_geo`. Verificado: ningún `tipo` de spot
  es farmacia/agua/súper/mirador/vaciado.

**Contexto extensible (1a+1b) — aplicado 2026-05-30:**
- `spot_geo.nearby_osm` / `nearby_spots` (JSONB) sustituyen a las columnas dist_*_km.
  Añadir categoría = 1 línea en `geo_context.CATEGORIES` + re-import + re-run (sin migración).
- 11 categorías OSM (agua, vaciado, súper, gasolinera, farmacia, mirador, panadería,
  lavandería, restaurante, recarga EV, playa). osm_pois ES: **182.001 POIs**.
- 1b (`nearby_spots`): área AC / camping / spot con vaciado más cercanos (KNN sobre
  nuestros spots). 52.441/52.477 spots ES con contexto de spots.
- Re-run completo ES: 52.477 spots en 5.5 min, modo local.
- La ficha y el Canal C (respuesta LLM) ya iteran ambos diccionarios.

**Para añadir una categoría OSM nueva:** editar `geo_context.CATEGORIES`, luego
`import_osm_pbf --country es` (re-parsea) + `DELETE FROM spot_geo WHERE source LIKE 'osm%'`
+ `scheduler.py --geo_osm` (recomputa).

**Pasos para otro país (FR/IT/DE…):**
```bash
# 1. Descargar PBF de Geofabrik (ej. francia)
#    https://download.geofabrik.de/europe/france-latest.osm.pbf → C:\geospots\data\
# 2. Import (MSYS_NO_PATHCONV=1 evita que Git Bash mangle /data)
MSYS_NO_PATHCONV=1 docker compose exec -T scraper python -m jobs.import_osm_pbf --country fr
# 3. Geo local del país (auto-detecta osm_pois)
docker compose exec -T -e GEO_OSM_COUNTRY=fr scraper python scheduler.py --geo_osm
```
Requisitos en container (ya en Dockerfile/requirements tras rebuild): `libexpat1` + `osmium`.
El .pbf se puede borrar tras importar (regenerable).

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

### Sprint 2 — Entity resolution: anclas + auditoría (sin re-merge masivo)
- `db/migration_entity_keys.sql` — columnas `spots.telefono_norm`, `web_domain`, `osm_id` + índices.
- `db.py` — `normalize_phone()`, `extract_domain()` (excluye agregadores), `EXCLUDED_DOMAINS`
  a nivel módulo; `find_spot_cercano` acepta `osm_id`/`place_id` y los usa como **ancla de
  identidad exacta** (únicos por entidad). `base.run()` los pasa.
- `jobs/backfill_entity_keys.py` — puebla las 3 claves (regenerable, idempotente).
- `api/main.py` — `GET /admin/dedup/audit` (candidatos a duplicado por ancla compartida).
- `pwa/admin.html` — panel "Entity resolution (auditoría)".
- `tests/test_entity_keys.py` — tests de normalize_phone/extract_domain.

### Sprint 3 — Motor geoespacial OSM (piloto Overpass)
- `db/migration_geo_osm.sql` — columnas `spot_geo.dist_drinking_water_km`,
  `dist_dump_station_km`, `dist_pharmacy_km`, `dist_viewpoint_km` + índice.
- `scraper/geo_context.py` — `run_geo_osm`: Overpass alrededor de cada spot del país
  piloto → distancia al amenity más cercano por categoría → upsert `spot_geo`
  (source='osm_overpass'). Rate-limit + backoff + abort tras 10 errores.
- `scheduler.py` — job `geo_osm` desde cola y CLI `--geo_osm`.
- `api/main.py` — `POST /admin/geo/run`, `GET /admin/coverage/geo`.
- `pwa/admin.html` — panel "Contexto OSM (piloto)" (cobertura + botón).
- `pwa/spot.html` — ficha muestra agua/vaciado/súper/gasolinera/farmacia/mirador.
- `tests/test_geo_context.py` — tests de categorize/nearest/build_query/haversine.
- `.env.example` — variables `GEO_OSM_*`.

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
  psql -h localhost -p 25433 -U geospots -d geospots -f db/migration_entity_keys.sql
  ```

- [ ] **2b. Backfill de claves de entidad** (Sprint 2 — telefono_norm/web_domain/osm_id):
  ```bash
  docker-compose exec scraper python -m jobs.backfill_entity_keys
  ```
  Luego en admin.html → panel "Entity resolution": revisar candidatos a duplicado.

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

- [ ] **8. Contexto geoespacial OSM** (Sprint 3 — piloto ES):
  ```bash
  psql ... -f db/migration_geo_osm.sql                 # columnas spot_geo
  docker-compose exec scraper python scheduler.py --geo_osm   # o botón en admin.html
  ```
  ⚠ Overpass público es frágil: empezar con `GEO_OSM_BATCH` bajo y `GEO_OSM_RATE`≥1.5s.
  Verifica en admin.html → panel "Contexto OSM": cobertura sube; ficha viajero muestra
  "Agua a X km · Súper a Y km · Mirador a Z km".

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

- [x] **Sprint 2 — Entity resolution** ✅ (en código): claves normalizadas + backfill +
  anclas exactas (osm_id/place_id) en `find_spot_cercano` + endpoint/UI de auditoría.
  SIN re-merge masivo. Pendiente: aplicar migración + backfill (pasos 2 y 2b).
- [x] **Sprint 3 — Motor OSM (piloto ES)** ✅ (en código): `spot_geo` poblado vía
  Overpass por categoría (agua/vaciado/súper/gasolinera/farmacia/mirador); panel de
  cobertura + ficha viajero. Pendiente: aplicar migración + correr (paso 8).
  Falta (mejora futura): integrar el contexto en embeddings + `/search/semantic`.
- [ ] **Sprint 4 — Escalado geo (PBF→PostGIS)**: condicional a que el piloto valide.
- [ ] **Sprint 5 — Ficha viajero v2**: enriquecer cuando haya datos reales de
  provenance + spot_geo (v1 ya en `pwa/spot.html`).

---

## 🧾 Historial de commits de la iniciativa

> Rama: `feature/provenance-google-geo`. Se actualiza en cada commit.

| Commit | Contenido |
|---|---|
| `8353aea` | Fix (validación): exclusión de agregadores/plataformas en web_domain + backfill keyset robusto. |
| `116108e` | Sprint 3: motor geoespacial OSM (piloto Overpass) + ficha con contexto. |
| `c82d743` | Sprint 2: entity resolution (claves normalizadas + anclas + auditoría + backfill). |
| `d212dd5` | Sprints 0-1 + ficha viajero (spot.html) + plan + este runbook. (Agrupado con cleanup_dedup_shells.py por un add concurrente.) |
| _(turno previo, en main)_ | google_maps_api source + migration_google_api.sql + fix job_id google_maps DOM + .env.example + CLAUDE.md |

---

## 🔍 Comandos de verificación (no tocan DB)

```bash
python -m py_compile api/main.py scraper/scheduler.py scraper/reconciliar.py scraper/sources/google_maps_api.py
python -m tests.test_source_signatures      # 30/30 fuentes aceptan job_id
python -m pytest tests/test_reconciliar.py -q
```
