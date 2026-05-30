# Diseño: Distancias por carretera + Contexto enriquecido + POIs de doble naturaleza

> Documento de estudio (no implementado salvo donde se indique). Surge del debate
> sobre cómo el contexto geoespacial llega al usuario vía búsqueda LLM.
> Estado: 2026-05-30. Canal C (mencionar entorno en la respuesta) ya implementado.

---

## 0. Recordatorio del flujo de consumo

```
Usuario (NL + su lat/lon) → /search/semantic
  → extraer_intencion (LLM)  : NL → sql_filters + semantic_query
  → embed_texts              : semantic_query → vector
  → SQL (spots⨝embeddings⨝state⨝geo) : radio + filtros, ORDER BY similitud
  → generar_respuesta (LLM)  : top spots → recomendación en lenguaje natural
```
El usuario nunca ve la DB; ve la frase final. Todo lo de abajo sirve a ese flujo.

---

## 1. Fuentes de contexto: NO solo 6 categorías OSM

Hoy `osm_pois` tiene 6 categorías. Hay tres fuentes de "cosas cercanas de interés",
y dos las estamos infrautilizando:

### 1a. OSM (osm_pois) — ampliable trivialmente
Añadir a `geo_context.CATEGORIES` + re-import del PBF (barato):
panadería, restaurante, lavandería, recarga EV, recarga gas/butano, ferretería,
hospital/centro médico, playa (`natural=beach`), lago/río, sendero (`route=hiking`),
parada de bus/tren, cajero, punto limpio. Cada una es 1 línea en el mapa de tags.

### 1b. Nuestros propios spots — GRATIS, ya en DB (infrautilizado)
Tenemos 861K spots. Proximidad relevante para un camper:
- "área AC con vaciado más cercana" (KNN sobre `spots WHERE vaciado_negras`)
- "gasolinera-spot más cercana", "camping más cercano"
Esto sale de la propia tabla `spots` con KNN PostGIS — sin OSM, sin coste de API.
**Decisión**: añadir un canal de contexto "spots cercanos por servicio" además de osm_pois.

### 1c. Datos desperdiciados en los scrapers (auditar)
Sospecha del usuario: varios scrapers traen amenities/servicios en `raw_data` que
`normalize()` descarta. Acción: **spike de auditoría** sobre `source_records.raw_data`
por fuente → mapear qué señales útiles se están tirando (lavandería, gas, EV, etc.)
y rescatarlas a columnas/servicios_extras. (raw_data es inmutable → recomputable.)

---

## 2. Distancia por CARRETERA (el tema caro) — estudio

Hoy todo es línea recta (`ST_Distance` / haversine). El usuario quiere distancia real
de conducción. **Clave: separar dos necesidades muy distintas.**

### (A) Usuario → spot, en QUERY TIME  ← HACER, alto valor, barato
"¿Cuánto conduzco hasta llegar al sitio?" Es 1 origen (usuario) × N candidatos
(~20-50 tras filtrar). Una sola matriz 1×N. Es **lo que decide al usuario**
("a 35 min" vs "5 km en línea recta cruzando una montaña").

- Motor recomendado: **OSRM** (reusa el PBF de España que ya tenemos).
  - Pipeline: `osrm-extract` (perfil) → `osrm-partition` → `osrm-customize` (MLD).
  - Sirve por HTTP (`osrm-routed`). Servicio **`/table`**: matriz 1×N en una llamada, ms.
- Integración: en `buscar_spots`, tras rankear por similitud, tomar top-N y pedir
  OSRM `/table` desde (lat,lon) del usuario → añadir `driving_km`/`driving_min` a cada
  spot; re-ordenar o mostrar. **Fallback a línea recta** si OSRM no responde.
- **Perfil autocaravana** (diferencial enorme): OSRM permite perfiles Lua con
  restricciones de altura/peso/anchura → rutas APTAS para camper (evitar puentes bajos,
  calles estrechas). Esto solo lo road-routing puede dar; es un valor único del producto.

### (B) Spot → amenity cercano (las distancias de contexto)  ← NO precomputar por carretera
"agua a 180m, súper a 700m". Bulk: 52K spots × N categorías. Por carretera sería
carísimo (matrices masivas, recomputar al añadir POIs).
- Para amenities **cercanos (<1-1.5km)**, la línea recta es buen proxy (la diferencia
  con carretera es pequeña a esas distancias).
- **Decisión**: mantener línea recta para el contexto "X cerca". Solo considerar
  carretera si un caso concreto lo justifica (p.ej. "súper a 3km" donde un río/autovía
  cambian mucho la ruta) — pero no por defecto.

### Estudio pendiente (antes de implementar A)
1. ¿Cabe OSRM en el NAS? Medir RAM de `osrm-extract` sobre Spain (perfil car) y disco
   del grafo (~1-2 GB estimado).
2. Perfil camper: ¿partimos del perfil car y le añadimos restricciones, o usamos un
   perfil truck/hgv como base? Definir altura/peso/anchura por defecto y si son
   parametrizables por usuario (su vehículo).
3. Latencia real de `/table` con N=50 en el NAS.
4. ¿Un servicio OSRM por país (es, fr…) o uno con varios PBF mergeados?

---

## 3. POIs de DOBLE NATURALEZA (mirador, área recreativa, zona de baño, piscina)

Observación del usuario: un "mirador" es a la vez **contexto** (buenas vistas cerca) Y
muchas veces **un spot para dormir** (el parking del mirador). Igual con áreas
recreativas, zonas de baño, piscinas… Esto matiza el "los POIs NUNCA son spots".

**Regla de oro: NO auto-promover OSM a spot.** La mayoría de miradores no son
pernoctables y OSM no dice si puedes dormir ahí. Promover automáticamente metería
basura en el modelo canónico (justo lo que el usuario quiere evitar). En su lugar:

1. **Default: contexto** (osm_pois), como ahora.
2. **Enriquecer spots existentes (cross-reference determinista):** si un spot está a
   <50-80m de un `osm_pois` de tipo mirador/playa/lago → marcar un atributo en el spot
   (`es_mirador`, `junto_a_playa`, `sea_view`…). Hoy el pipeline semántico infiere
   algunos (sea_view, lake_nearby) desde reviews; cruzarlo con geo lo hace **determinista
   y sin depender de que alguien lo mencione**. Alto valor, bajo riesgo.
3. **Cola de descubrimiento (review queue, NUNCA auto-merge):** features OSM con
   potencial de pernocta (mirador con parking asociado, área recreativa con parking)
   que NINGUNA fuente camper cubre → candidatos a spot nuevo, a revisión manual (mismo
   patrón que la cola de colisiones place_id del Sprint 1). Esto convierte OSM en una
   fuente de **descubrimiento de spots** sin contaminar.

Categorías con esta doble naturaleza a tratar: `tourism=viewpoint`, `leisure=park`,
`leisure=swimming_area`/`natural=beach` (zonas de baño), `leisure=swimming_pool`,
`tourism=picnic_site`, `amenity=parking` junto a cualquiera de los anteriores.

---

## 4. Orden propuesto

1. ✅ **Canal C** (hecho): la respuesta LLM menciona el entorno cercano.
2. **Ampliar categorías OSM** (1a) + **contexto desde nuestros spots** (1b) — barato,
   multiplica el valor del entorno que ya menciona el Canal C.
3. **Canal B** (filtros geo en intent+SQL) con el set ampliado.
4. **Cross-reference de enriquecimiento** (3.2) — determinista, bajo riesgo.
5. **Spike OSRM** (2A): medir viabilidad en NAS + perfil camper. Si OK → distancia por
   carretera usuario→spot en query time. **El diferencial gordo del producto.**
6. **Auditoría raw_data** (1c) y **cola de descubrimiento** (3.3) — cuando haya tiempo.
7. **Canal A** (re-embed con contexto en el texto) — al final; aún no hay embeddings
   "serios" generados, así que sin coste hundido.
