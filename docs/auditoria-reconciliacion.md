# Auditoría profunda — Motor de reconciliación multi-fuente

**Fecha:** 2026-05-30
**Alcance:** `scraper/reconciliar.py`, `db/migration_phase3_v4e.sql`, `db/migration_provenance.sql`, `db/schema.sql` (tabla `spots` y `source_records`), `scraper/db.py` (`_limpiar_web`, `normalize_phone`, `extract_domain`).
**Modo:** auditoría exhaustiva previa a corrección. **No se ha modificado código.** Cada hallazgo lleva verificación contra el fuente real, severidad y propuesta de rediseño (DDL + Python/SQL). Al final, plan por sprints.

> Nota metodológica: he verificado las 11 discrepancias de la auditoría previa (Gemini Flash) línea a línea. La mayoría son correctas; **dos están matizadas a la baja** (su severidad real es menor de lo reportado) y a todas les falta el detalle de implementación correcta en PostgreSQL. Añado 9 hallazgos nuevos que la primera pasada no detectó (concurrencia, índices/bloat, limpieza de estado lateral, coerción de tipos en el bind, recuperación de overrides).

---

## Parte A — Verificación de los 11 hallazgos previos

| # | Hallazgo | Veredicto | Severidad real |
|---|---|---|---|
| 1 | Campos Fase 3/4 ausentes de `CREDIBILITY` | ✅ Confirmado | Alta (completitud) |
| 2 | `_limpiar_web` sobre el ganador, no candidatos | ✅ Confirmado | Media-Alta |
| 3 | Voto numérico compara como string exacto | ✅ Confirmado | Alta |
| 4 | Empate técnico perpetúa valor de baja credibilidad | ✅ Confirmado (con matiz) | Media |
| 5 | `conflictos` no se limpia al resolverse | ✅ Confirmado | Media |
| 6 | N+1 en `compute_temporal_overrides` | ✅ Confirmado (el job entero es N+1) | Alta (escala) |
| 7 | Columna `active` GENERATED siempre TRUE | ✅ Confirmado | **Baja** (latente, no roto en prod) |
| 8 | Fuentes inactivas pesan 0.5 | ✅ Confirmado | Media |
| 9 | Sin reconciliación incremental | ✅ Confirmado | Alta (escala) |
| 10 | Overrides duplicados con canónico ya FALSE | ✅ Confirmado | Baja-Media |
| 11 | Overrides solo soportan FALSE | ✅ Confirmado | Baja (mejora) |

### Detalle y correcciones a la valoración previa

**#1 — Campos Fase 3/4 fuera de `CREDIBILITY`.** Confirmado. [reconciliar.py:409](scraper/reconciliar.py:409) itera `for campo in CREDIBILITY:`. El dict (`reconciliar.py:30-64`) no contiene `piscina, lavanderia, gas_recharge, restaurant, juegos_ninos, mirador, zona_protegida, online_booking, winter_friendly, apto_motos, mtb_friendly, surf_friendly, fishing, climbing, hiking_nearby, amperaje, n_enchufes, max_noches, idiomas_hablados, productos_venta, email, fotos_urls, servicios_extras` — todas existen en `spots` ([schema.sql:93-141](db/schema.sql:93)). Consecuencia: estos campos conservan el valor de la **primera** fuente que tocó el spot vía `enriquecer_spot()` (que usa `COALESCE`, first-write-wins) y nunca se reconcilian. No es solo "valores nulos": es first-write-wins silencioso sin auditoría de conflicto.

**#2 — orden de limpieza de `web`.** Confirmado. En el path rank-first ([reconciliar.py:166-176](scraper/reconciliar.py:166)) se elige el primer source de `CREDIBILITY["web"]` con valor no nulo, y solo *después* ([reconciliar.py:419-422](scraper/reconciliar.py:419)) se aplica `_limpiar_web()`. Si el ganador trae un dominio de agregador (`EXCLUDED_DOMAINS`, `db.py:126`), se anula a `None` y se descarta una web oficial válida de una fuente peor rankeada. El mismo defecto degrada `spot_field_provenance`: el `supporting_sources`/`confidence` se calculan sobre el candidato sucio.

**#3 — voto numérico quebradizo.** Confirmado y es **síntoma de un problema más amplio**: `_vote_key` ([reconciliar.py:124-129](scraper/reconciliar.py:124)) hace `str(v)` sin canonicalizar. No solo `15`≠`15.0`; también `True`≠`"true"`≠`"yes"`, `"Si"`≠`"sí"`, etc. La raíz es **ausencia de canonicalización de valores antes de votar** (ver rediseño #3, generaliza la solución).

**#4 — empate perpetúa baja credibilidad.** Confirmado, con matiz: `KEEP_EXISTING` ([reconciliar.py:215-216](scraper/reconciliar.py:215)) deja la columna intacta. El riesgo es real *solo si* el valor preexistente vino de una fuente poco fiable vía `enriquecer_spot`. La corrección correcta no es "imponer siempre el ganador" (rompería el propósito del margen anti-ruido), sino **desempatar por credibilidad de la fuente testigo** cuando ambas opciones empatadas superan a la fuente que escribió el valor actual.

**#5 — `conflictos` no se limpia.** Confirmado. [reconciliar.py:433](scraper/reconciliar.py:433) `if updates or conflictos:`. Cuando un conflicto se resuelve (ahora `conflictos == []`) y no hay `updates`, el bloque se salta y la columna `spots.conflictos` conserva la lista vieja. Mismo defecto, no señalado antes, en `spot_field_provenance`: ver hallazgo nuevo **N3**.

**#6 — N+1.** Confirmado, y peor de lo descrito: **el job completo es N+1**. Por spot: `SELECT source_records` (`reconciliar.py:393`), luego en `compute_temporal_overrides` un `fetchrow` de semantic_state (`:289`) + por cada field un `fetchval SELECT {field} FROM spots` (`:328`) + un `INSERT` (`:331`). Para 142K spots multifuente con 3-4 campos de señal → ~142K × (1 + 1 + 2·k) round-trips de red secuenciales en una sola conexión.

**#7 — `active` GENERATED.** Confirmado a nivel DDL ([migration_phase3_v4e.sql:41](db/migration_phase3_v4e.sql:41)): `GENERATED ALWAYS AS (expires_at > created_at) STORED`, con `expires_at = created_at + ttl` (futuro) ⇒ constante TRUE. **Matiz crítico que la auditoría previa omitió:** PostgreSQL **prohíbe** funciones no-IMMUTABLE (como `NOW()`) en columnas generadas, así que *no se puede arreglar* metiendo `NOW()` ahí — hay que eliminar la columna. Y **la severidad real es baja**: el único consumidor en producción, [api/main.py:279](api/main.py:279), ya filtra por `expires_at > NOW()` y **no** usa `active`. Es una trampa latente (cualquier dashboard/analítica futura que filtre `active = TRUE` se equivocará) + coste de almacenamiento STORED inútil, no un fallo activo.

**#8 — fuentes inactivas pesan 0.5.** Confirmado. `load_credibility` ([reconciliar.py:251-257](scraper/reconciliar.py:251)) filtra `WHERE active = TRUE`, pero `_reconciliar_campo_full` ([reconciliar.py:181](scraper/reconciliar.py:181), `:196`) hace `credibility.get(source, 0.5)`: cualquier fuente no presente en el dict —incluidas las desactivadas— vota con 0.5. Además el ranking hardcoded (`CREDIBILITY`) las sigue considerando en rank-first. Una fuente desactivada por mala calidad sigue influyendo.

**#9 — sin incremental.** Confirmado. [reconciliar.py:383-386](scraper/reconciliar.py:383) selecciona todos los `activo AND array_length(fuentes,1)>1` sin filtro temporal. No existe columna `reconciled_at` en `spots` (verificado en `schema.sql`). Con ~142K spots y crecimiento, cada corrida reescanea todo.

**#10 — overrides redundantes.** Confirmado. `compute_temporal_overrides` lee el canónico (`reconciliar.py:328`) pero **nunca lo usa** para decidir; inserta el override aunque el canónico ya sea `FALSE` (override "agua rota" sobre un spot que ya dice "sin agua" = ruido).

**#11 — solo overrides FALSE.** Confirmado por diseño ([reconciliar.py:307-309](scraper/reconciliar.py:307) `if score is not False: continue`). Es una limitación, no un bug. Prioridad baja.

---

## Parte B — Hallazgos nuevos (no detectados en la primera pasada)

**N1 — Condición de carrera lectura/escritura sin transacción ni snapshot.**
`job_reconciliar` usa **una sola conexión** del pool y cada `await conn.execute` **autocommit** (no hay `async with conn.transaction()`). Entre el `SELECT source_records` (`reconciliar.py:393`) y el `UPDATE spots` (`:450`) puede pasar tiempo arbitrario; mientras, el daemon de scraping ejecuta `upsert_source_record` / `enriquecer_spot` sobre el mismo spot. Resultado: el reconciliador puede escribir un canónico basado en `source_records` ya obsoletos (TOCTOU), o reescribir un valor que el scraper acaba de cambiar. No hay `SELECT … FOR UPDATE` ni aislamiento `REPEATABLE READ`. Además, si el spot se borra/mergea (ON DELETE CASCADE) a mitad del bucle largo, el `INSERT` de override revienta y se cuenta como `errores` sin diagnóstico.

**N2 — Coerción de tipos en el bind del UPDATE (riesgo de fallo silencioso por spot).**
El valor ganador se bindea crudo al `UPDATE spots SET {campo} = $n` (`reconciliar.py:436-450`). Si `normalized_data` trae `precio_aprox` o `num_plazas` como **string** (`"15"`), asyncpg intenta bindear `str` a `REAL`/`INT` → excepción, capturada por el `try/except` por-spot (`:477`) → el spot entero queda sin reconciliar y solo suma a `errores`. Pérdida silenciosa de reconciliación para spots con tipos sucios. Va de la mano con #3 (canonicalización debe incluir cast al tipo de la columna).

**N3 — `spot_field_provenance` acumula estado obsoleto igual que `conflictos`.**
El upsert de provenance ([reconciliar.py:457-470](scraper/reconciliar.py:457)) solo se ejecuta para campos que entran en `updates` (valor no `None` y no `KEEP_EXISTING`). Si en una corrida posterior un campo pasa a `KEEP_EXISTING`/`None`, su fila de provenance **no se actualiza** y conserva `conflict_detected = TRUE` viejo. El índice parcial `idx_sfp_conflict` (`migration_provenance.sql:43`) sirve entonces filas obsoletas a la cola del "desempatador de Google", que procesará conflictos ya resueltos. Es el gemelo de #5 en la tabla lateral.

**N4 — Overrides no se retiran cuando la señal se recupera.**
Si el agua vuelve a funcionar (`score` pasa a `True`, o el signal desaparece de `signals_data`), `compute_temporal_overrides` simplemente **no toca** la fila FALSE existente; esta sobrevive hasta `expires_at` (= half_life, p.ej. 60 días). La API seguirá mostrando "agua rota" hasta dos meses tras la recuperación. No hay paso de expiración/limpieza activa. Combina con #7 (no hay forma fiable de marcar inactivo) y #11.

**N5 — Bloat de tabla e índices por UPDATE masivo no-HOT.**
Cada corrida full-scan (#9) hace `UPDATE spots SET <muchas columnas>, updated_at=NOW()` sobre hasta 142K filas → 142K tuplas muertas/corrida. Los upserts en `spot_field_provenance` tocan `conflict_detected`, columna indexada por `idx_sfp_conflict` → **rompe HOT update** y engorda el índice. `spot_field_overrides` con `UNIQUE(spot_id,field,source_signal_type)` + `idx_sfo_expires` igual. Sin tuning de autovacuum para estas tablas, la corrida full genera presión de vacuum y bloat sostenido. La solución incremental (#9) lo mitiga de raíz; adicionalmente conviene `fillfactor` < 100 en `spots` si la reconciliación sigue siendo full.

**N6 — `_detectar_conflictos` ignora `DB_TO_NORM_KEY` y repite el string-compare frágil.**
[reconciliar.py:229-243](scraper/reconciliar.py:229) compara `set(str(v) …)` y lee `data.get(campo)` con el nombre **de columna** (no el de normalized_data). Para `CONFLICT_FIELDS` actuales coincide por casualidad (gratuito, agua_potable…), pero es inconsistente con `_reconciliar_campo_full` (que sí mapea vía `DB_TO_NORM_KEY`) y heredará #3 si se añade un campo numérico a `CONFLICT_FIELDS`.

**N7 — Falta índice para el `SELECT` de spots multifuente.**
La query de selección (`reconciliar.py:383`) filtra `activo = TRUE AND array_length(fuentes,1) > 1`. No hay índice que lo cubra → seq scan de `spots` (~142K+ activos). Con incremental (#9) el filtro pasará a `reconciled_at < greatest(source last_seen)`, que necesita índice propio.

**N8 — `confidence` de `spots` nunca se actualiza desde la reconciliación.**
El reconciliador calcula `confidence` por campo y lo persiste en provenance, pero `spots.confidence` (default 0.5, `schema.sql:148`) jamás se recomputa. No es bug funcional pero es deuda: el campo aparenta significar algo y está muerto.

**N9 — `_load_half_lives` cachea en variable global de módulo sin reset garantizado entre procesos/llamadas concurrentes.**
`_HALF_LIVES_CACHE` (`reconciliar.py:116`) se resetea al inicio de `job_reconciliar` (`:380-381`), pero es estado global mutable de módulo: si dos jobs corrieran en el mismo proceso (o en tests), hay riesgo de cruce. Menor dado el daemon single-job, pero frágil.

---

## Parte C — Propuestas de rediseño (DDL + código)

### Rediseño #1 — Reconciliar todos los campos de `spots`, no solo `CREDIBILITY`

Separar **qué campos reconciliar** (todas las columnas de servicio/booleanas + numéricas) de **el ranking de desempate** (`CREDIBILITY`, que puede quedar parcial). Para campos booleanos sin ranking explícito, usar voto ponderado puro y, en empate, la credibilidad de la fuente.

```python
# Campos booleanos de servicio que deben reconciliarse aunque no tengan ranking explícito
BOOLEAN_SERVICE_FIELDS = {
    "piscina","lavanderia","gas_recharge","restaurant","juegos_ninos","mirador",
    "zona_protegida","online_booking","winter_friendly","apto_motos","mtb_friendly",
    "surf_friendly","fishing","climbing","hiking_nearby",
}
NUMERIC_EXTRA_FIELDS = {"amperaje","n_enchufes","max_noches"}

# Conjunto efectivo a reconciliar (orden estable)
RECONCILE_FIELDS = list(CREDIBILITY) + sorted(BOOLEAN_SERVICE_FIELDS | NUMERIC_EXTRA_FIELDS)
WEIGHTED_VOTE_FIELDS |= BOOLEAN_SERVICE_FIELDS | NUMERIC_EXTRA_FIELDS
```

Y en el job, iterar `for campo in RECONCILE_FIELDS:`. Para campos sin entrada en `CREDIBILITY`, `rank` queda `[]` y el desempate de witness cae en el primer source que lo aportó — aceptable; mejor: desempatar por `credibility` (ver #4). `idiomas_hablados`/`productos_venta` (arrays) y `fotos_urls`/`servicios_extras` (JSONB) **no** entran a voto: requieren estrategia de *unión* (merge), no de mayoría — fuera de scope de este sprint, anotado como deuda.

### Rediseño #2 — Limpiar candidatos de `web` ANTES de rankear/votar

```python
# En _reconciliar_campo_full, normalizar por campo antes de construir candidatos.
def _sanitize_value(campo, v):
    if campo == "web":
        return _limpiar_web(v)        # devuelve None si es agregador
    return v

# rank-first:
for fuente in CREDIBILITY.get(campo, []):
    v = _sanitize_value(campo, records.get(fuente, {}).get(norm_key))
    if v is not None:
        winner_val, winner_src = v, fuente
        break
```

Así una web de agregador del source top no "gana y se descarta": se ignora y el siguiente source con web válida gana. Eliminar entonces la limpieza post-hoc de `reconciliar.py:419-422`.

### Rediseño #3 — Canonicalización tipada de valores (raíz de #3, N2, N6)

Una función única de canonicalización por campo, usada tanto en `_vote_key` como en el bind final:

```python
def _canon_value(campo, v):
    """Normaliza el valor a su tipo canónico antes de votar y de escribir."""
    if v is None:
        return None
    if campo in NUMERIC_INT_FIELDS:      # num_plazas, amperaje, n_enchufes, max_noches
        try: return int(float(v))
        except (TypeError, ValueError): return None
    if campo in NUMERIC_REAL_FIELDS:     # precio_aprox, altura_max_m, master_rating
        try: return round(float(v), 2)
        except (TypeError, ValueError): return None
    if campo in BOOLEAN_FIELDS:
        if isinstance(v, bool): return v
        s = str(v).strip().lower()
        if s in {"true","1","yes","si","sí","oui","ja"}: return True
        if s in {"false","0","no","non","nein"}: return False
        return None
    return v

def _vote_key(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, default=str)
    if isinstance(v, float):
        return repr(round(v, 2))   # 15 y 15.0 → misma clave
    return str(v)
```

Aplicar `_canon_value` al leer `data.get(norm_key)` en `_reconciliar_campo_full` **y** en `_detectar_conflictos` (cierra N6). El valor ganador ya sale tipado → el bind del UPDATE no falla (cierra N2). Para `num_plazas` con dispersión (10 vs 12) seguir tratándolo como categórico es correcto (no queremos promediar plazas); el margen decide. Para `precio_aprox`, considerar *bucketing* a redondeo de 1€ antes de votar para no dispersar por céntimos.

### Rediseño #4 — Desempate por credibilidad en empate técnico

En lugar de `KEEP_EXISTING` ciego, cuando `margin < TIE_MARGIN` comparar la credibilidad de la fuente testigo del ganador con la del valor canónico actual. Necesita conocer qué fuente escribió el canónico → la provenance lo provee. Versión sin dependencia de provenance (más simple): si las dos opciones más votadas provienen ambas de fuentes con credibilidad > umbral, imponer la de mayor credibilidad de witness en vez de mantener:

```python
if margin < TIE_MARGIN:
    # Empate técnico: si el ganador por peso está respaldado por una fuente
    # de credibilidad alta, imponerlo igualmente (evita perpetuar valores de
    # fuentes débiles ya escritos en spots). Si no, KEEP_EXISTING.
    w_src, w_val = witnesses[winner_key]
    if credibility.get(w_src, 0.0) >= HIGH_CRED_THRESHOLD:   # p.ej. 0.85
        return w_val, w_src, sources_by_val[winner_key], confidence, margin, conflict
    return KEEP_EXISTING, None, sources_by_val[winner_key], confidence, margin, conflict
```

`HIGH_CRED_THRESHOLD` configurable. Esto NO elimina el margen anti-ruido para fuentes mediocres; solo evita que un empate entre dos fuentes fiables ceda ante un valor heredado de osm/ioverlander.

### Rediseño #5 — Limpiar `conflictos` y provenance obsoleta siempre (cierra #5, N3)

Cambiar la condición de escritura para que **siempre** se sincronice `conflictos` (incluido el caso "lista vacía"):

```python
# Reemplaza `if updates or conflictos:`
sets, vals, idx = [], [], 1
for campo, valor in updates.items():
    sets.append(f"{campo} = ${idx}"); vals.append(valor); idx += 1
sets.append(f"conflictos = ${idx}::jsonb"); vals.append(json.dumps(conflictos)); idx += 1
sets.append("reconciled_at = NOW()")     # ver #9
vals.append(spot_id)
await conn.execute(
    f"UPDATE spots SET {', '.join(sets)}, updated_at = NOW() WHERE id = ${idx}", *vals)
```

Para N3, hacer el upsert de provenance también para campos resueltos: bien recalculando `conflict_detected=FALSE` cuando el campo deja de tener conflicto, bien con un `DELETE FROM spot_field_provenance WHERE spot_id=$1 AND field <> ALL($2)` de los campos ya no presentes. Recomiendo recalcular y upsertar todos los `PROVENANCE_FIELDS` siempre (no solo los que entran en `updates`).

### Rediseño #6 — Eliminar N+1: cargar en bloque y escribir en bloque

Tres cambios:

1. **Traer el spot y sus source_records de una vez**, evitando el `fetchval SELECT {field} FROM spots` por campo. Cargar `SELECT * FROM spots WHERE id=$1` una vez (o, mejor, en el batch — ver punto 3) y leer el canónico de ahí.

2. **Procesar por lotes** con un cursor sobre spots y, por lote de N (p.ej. 1000), traer todos los `source_records` y `spot_semantic_state` con `WHERE spot_id = ANY($1)`:

```python
records_by_spot = defaultdict(dict)
for r in await conn.fetch(
    "SELECT spot_id, source, normalized_data FROM source_records WHERE spot_id = ANY($1::int[])",
    batch_ids):
    nd = r["normalized_data"]; nd = json.loads(nd) if isinstance(nd, str) else nd
    records_by_spot[r["spot_id"]][r["source"]] = nd

signals_by_spot = {r["spot_id"]: r["signals_data"] for r in await conn.fetch(
    "SELECT spot_id, signals_data FROM spot_semantic_state "
    "WHERE spot_id = ANY($1::int[]) AND stale = FALSE", batch_ids)}
```

3. **Escribir overrides y provenance con `executemany`** (ya se hace para provenance; replicar para overrides), y los UPDATE de spots con `execute_many` o un `UPDATE … FROM (VALUES …)` por lote. `compute_temporal_overrides` pasa a recibir el dict `signals` y el canónico ya en memoria — cero queries dentro de la función.

Esto reduce de O(spots × campos) round-trips a O(spots / batch) round-trips.

### Rediseño #7 — Arreglar la columna `active` (cierra #7)

PostgreSQL no permite `NOW()` en columna generada. Eliminarla y exponer el estado vía vista o columna calculada en query:

```sql
BEGIN;
ALTER TABLE spot_field_overrides DROP COLUMN IF EXISTS active;

-- Estado en tiempo de consulta (preferido): índice parcial para overrides vigentes
CREATE INDEX IF NOT EXISTS idx_sfo_active
    ON spot_field_overrides (spot_id, field)
    WHERE expires_at > NOW();   -- NOTA: el predicado se evalúa al construir el índice;
-- para "vigente ahora" en queries, filtrar siempre expires_at > NOW() (ya lo hace la API).

-- Alternativa expresiva: vista
CREATE OR REPLACE VIEW spot_field_overrides_active AS
    SELECT * FROM spot_field_overrides WHERE expires_at > NOW();
COMMIT;
```

> Ojo: un índice parcial con `WHERE expires_at > NOW()` **no** se mantiene "vivo" (NOW() se congela al crearlo). El patrón correcto es: índice normal sobre `expires_at` (ya existe, `idx_sfo_expires`) + filtro `expires_at > NOW()` en la query. La vista es la opción más limpia para consumidores que hoy esperaban `active`.

### Rediseño #8 — Ignorar fuentes inactivas de verdad (cierra #8)

Cargar **todas** las fuentes con su estado y pasar un set de activas; en `_reconciliar_campo_full`, saltar records de fuentes inactivas y no usar 0.5 por defecto para ellas:

```python
async def load_credibility(conn):
    rows = await conn.fetch("SELECT source, base_score, active FROM source_credibility")
    weights = {r["source"]: float(r["base_score"]) for r in rows if r["active"]}
    active_sources = {r["source"] for r in rows if r["active"]}
    return weights, active_sources

# en el bucle de votación:
for source, data in records.items():
    if source not in active_sources:      # fuente desactivada → ignorar por completo
        continue
    ...
```

Decisión de diseño: una fuente **no listada** en `source_credibility` (nueva, sin fila) ≠ una desactivada. Para la primera, mantener default 0.5 (comportamiento actual). Para la segunda (fila con `active=FALSE`), peso 0 / ignorar. El set `active_sources` distingue ambos casos sólo si toda fuente conocida tiene fila; conviene un assert/log de fuentes presentes en records y ausentes de `source_credibility`.

### Rediseño #9 — Reconciliación incremental (cierra #9, habilita N5/N7)

```sql
BEGIN;
ALTER TABLE spots ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ;

-- Índice para seleccionar "multifuente con cambios desde la última reconciliación".
-- num_fuentes ya es columna generada (schema.sql:123) → indexable directamente.
CREATE INDEX IF NOT EXISTS idx_spots_reconcile_pending
    ON spots (reconciled_at)
    WHERE activo = TRUE AND num_fuentes > 1;
COMMIT;
```

Selección incremental: un spot necesita reconciliarse si alguno de sus `source_records` tiene `last_seen > spots.reconciled_at` (o `reconciled_at IS NULL`):

```sql
SELECT s.id
FROM spots s
WHERE s.activo = TRUE AND s.num_fuentes > 1
  AND (
    s.reconciled_at IS NULL
    OR EXISTS (
      SELECT 1 FROM source_records sr
      WHERE sr.spot_id = s.id AND sr.last_seen > s.reconciled_at
    )
  );
```

El `EXISTS` se apoya en `idx_sr_spot` (existe) + `last_seen`. Para grandes volúmenes, considerar índice `(spot_id, last_seen)` en `source_records`. El `UPDATE` del rediseño #5 ya setea `reconciled_at = NOW()`. Modo `--full` opcional para forzar reescaneo completo tras cambios de lógica/`CREDIBILITY`.

### Rediseño #10 — No insertar overrides redundantes (cierra #10)

Con el canónico ya en memoria (rediseño #6), saltar cuando coincide:

```python
# dentro del bucle de fields, tras leer canonical desde el dict del spot en memoria
if canonical is False:        # ya decimos "no funciona / no hay": override redundante
    continue
# overridden_value se generaliza con #11:
overridden = bool(score)      # score es False aquí; con #11 puede ser True
if canonical == overridden:   # el override no cambia nada
    continue
```

### Rediseño #11 — Overrides positivos y retirada al recuperar (cierra #11, N4)

Permitir `score True` además de `False`, y **retirar/expirar** overrides cuando la señal contradice el override vigente:

```python
for signal_id, fields in SIGNAL_TO_FIELDS.items():
    s = signals.get(signal_id)
    if not isinstance(s, dict):
        # señal desaparecida → expirar overrides de estos fields para este spot
        expire_rows.extend((spot_id, f, signal_id) for f in fields)
        continue
    score = s.get("score")
    if score not in (True, False):
        continue
    # ... thresholds ...
    for field in fields:
        if canonical_of(field) == score:        # cierra #10 generalizado
            expire_rows.append((spot_id, field, signal_id)); continue
        upsert_rows.append((spot_id, field, canonical_of(field), score, ...))
```

Y un paso de expiración por lote:

```sql
UPDATE spot_field_overrides SET expires_at = NOW()
WHERE (spot_id, field, source_signal_type) IN (SELECT * FROM unnest($1::int[], $2::text[], $3::text[]));
```

Esto resuelve N4 (recuperación) sin esperar al half_life. La API, que ya filtra `expires_at > NOW()`, deja de mostrar el override de inmediato.

### Rediseño N1 — Concurrencia y atomicidad

- Envolver el procesamiento de **cada spot (o cada lote)** en `async with conn.transaction():` para que UPDATE de spots + overrides + provenance sean atómicos.
- Tomar lock optimista por spot: `SELECT id, <campos>, updated_at FROM spots WHERE id = ANY($1) FOR NO KEY UPDATE` al inicio del lote, o aislamiento `REPEATABLE READ` en la transacción del lote. `FOR NO KEY UPDATE` no bloquea lecturas y evita que el scraper pise el spot a mitad.
- Releer `source_records` dentro de la misma transacción que el UPDATE para cerrar el TOCTOU.
- Tratar `FK violation` por spot borrado a mitad como `skip` silencioso (no `errores`).

### Rediseño N5 — Bloat / autovacuum

Con incremental (#9) el volumen de UPDATEs por corrida baja drásticamente. Adicional:

```sql
ALTER TABLE spots SET (fillfactor = 90);   -- deja hueco para HOT updates
ALTER TABLE spot_field_provenance SET (autovacuum_vacuum_scale_factor = 0.05);
ALTER TABLE spot_field_overrides  SET (autovacuum_vacuum_scale_factor = 0.05);
```

Evitar incluir en `idx_sfp_conflict` columnas que cambian a menudo si se busca HOT; alternativamente aceptar el coste y subir agresividad de autovacuum como arriba.

---

## Parte D — Plan por sprints

Ordenado por (riesgo de corrupción/pérdida de datos) × (coste). Cada sprint es desplegable y testeable de forma independiente.

### Sprint R1 — Correcciones de corrección de datos (sin DDL)
*Objetivo: dejar de perder/perpetuar datos. Bajo riesgo, alto valor.*
- #2 limpiar `web` antes de votar (Rediseño #2).
- #3 + N2 + N6 canonicalización tipada (Rediseño #3).
- #5 limpiar `conflictos` siempre (Rediseño #5, parte `conflictos`).
- #8 ignorar fuentes inactivas (Rediseño #8).
- Tests: `tests/test_reconciliar.py` (ampliar) — casos web-agregador, `15`/`15.0`, fuente inactiva, conflicto resuelto→limpio.

### Sprint R2 — Cobertura de campos + desempate (sin DDL)
- #1 reconciliar todos los campos booleanos/numéricos (Rediseño #1).
- #4 desempate por credibilidad (Rediseño #4).
- N3 sincronizar provenance siempre (Rediseño #5, parte provenance).
- Validar volumen de escritura antes de desplegar (puede subir mucho el nº de campos tocados).

### Sprint R3 — DDL de overrides + semántica positiva
- #7 eliminar columna `active` + vista (Rediseño #7).
- #10 + #11 + N4 overrides positivos, redundancia y retirada (Rediseños #10/#11).
- Migración idempotente nueva: `db/migration_reconcile_v2.sql` (DROP active, vista, índices).

### Sprint R4 — Escala: incremental + batch + concurrencia
- #9 columna `reconciled_at` + índice + selección incremental (Rediseño #9).
- #6 batch loading/writing, eliminar N+1 (Rediseño #6).
- N1 transacción por lote + `FOR NO KEY UPDATE` (Rediseño N1).
- N7 índice de selección, N5 autovacuum/fillfactor.
- Bench antes/después sobre snapshot de ~142K multifuente.

### Deuda anotada (fuera de scope inmediato)
- Estrategia de **merge** (no voto) para `idiomas_hablados`, `productos_venta`, `fotos_urls`, `servicios_extras`.
- N8: recomputar `spots.confidence` desde provenance, o eliminar el campo si no se usa.
- N9: encapsular `_HALF_LIVES_CACHE` en el contexto del job en vez de global de módulo.
- `precio_aprox`: evaluar bucketing por 1€ antes de votar.

---

## Apéndice — DDL consolidado (idempotente)

```sql
-- db/migration_reconcile_v2.sql
BEGIN;

-- #7: la columna generada `active` es siempre TRUE y no es arreglable con NOW()
ALTER TABLE spot_field_overrides DROP COLUMN IF EXISTS active;
CREATE OR REPLACE VIEW spot_field_overrides_active AS
    SELECT * FROM spot_field_overrides WHERE expires_at > NOW();

-- #9: reconciliación incremental
ALTER TABLE spots ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_spots_reconcile_pending
    ON spots (reconciled_at) WHERE activo = TRUE AND num_fuentes > 1;

-- N7/N4: soporte a EXISTS de source_records por last_seen
CREATE INDEX IF NOT EXISTS idx_sr_spot_lastseen
    ON source_records (spot_id, last_seen);

-- N5: control de bloat
ALTER TABLE spots SET (fillfactor = 90);
ALTER TABLE spot_field_provenance SET (autovacuum_vacuum_scale_factor = 0.05);
ALTER TABLE spot_field_overrides  SET (autovacuum_vacuum_scale_factor = 0.05);

COMMIT;
```
