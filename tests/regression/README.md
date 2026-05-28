# Regression suite v1 — Phase 3 hardening pre-batch

Cumple **T0.2** del plan `docs/fase-3-hardening-pre-batch.md`. Detecta regresiones cualitativas en el output del pipeline LLM spot-level antes y después de cada cambio del Sprint 1-3.

## Uso

```bash
# Ver todos los casos definidos
python -m tests.regression.semantic_suite list

# Filtrar por categoría
python -m tests.regression.semantic_suite list --category obras_temporales

# Ejecutar aserciones contra el estado actual de la DB
python -m tests.regression.semantic_suite check

# Snapshot del estado actual de un caso (baseline)
python -m tests.regression.semantic_suite snapshot --case grau_roig_obras

# Snapshot de todos
python -m tests.regression.semantic_suite snapshot
```

`check` devuelve exit code 1 si **cualquier aserción `hard` falla**. Las `band` solo avisan. Las `soft` son notas para revisión humana mensual.

## Estado actual

La suite tiene ~20 casos definidos. Sólo 2 tienen `spot_id` fijado (Grau Roig 85057 + Andorra Campers TODO). El resto requieren identificar el spot real en la DB usando el `locator_hint` de cada caso.

**Antes de Sprint 4 (smoke Andorra) hay que rellenar al menos:**
- `andorra_campers_workshop` — taller mal clasificado (T1.4b)
- `obras_temporales_2` — segundo spot con obras (T1.4)
- 3 contradicciones servicios/reviews (T1.2)
- 3 multilingüe (NL, DE, FR)
- `smoke_v4_any` — un spot estable enriquecido v4

El resto pueden quedar TODO hasta Tier 2 sin bloquear el batch.

## Cómo rellenar un TODO

1. Mira el `locator_hint` del caso en `semantic_suite.py`.
2. Ejecuta la query SQL contra la DB (`psql -h localhost -p 25433 ...`).
3. Escoge un spot_id estable (idealmente con varias reviews, no demasiado reciente).
4. Reemplaza `spot_id=None` por el ID escogido.
5. Commit del cambio.
6. Re-corre `python -m tests.regression.semantic_suite check --case <case_id>`.

## Cómo añadir un caso nuevo

Sigue el patrón de la lista `CASES`. Cada caso necesita:
- `case_id` único (slug)
- `category` (agrupa el reporte)
- `description` breve
- `spot_id` o `locator_hint`
- `requires_tasks` si depende de migraciones/código futuro (T1.4, etc.)
- `hard` / `bands` / `soft` con aserciones

Para aserciones complejas, escribe un helper en la sección "Helpers de aserción" y úsalo. Cada helper devuelve:
- `True` → pasa
- `str` → falla con mensaje
- `None` → SKIP (precondición no cumplida — ej. columna o tabla no existe aún)

## Snapshots como baseline

`snapshot` guarda el estado actual de cada caso (campos públicos de `spot_semantic_state` + `spots` + `spot_geo` + `spot_alerts`) como JSON en `snapshots/`. Tras cada cambio significativo, ejecutar `snapshot` y comparar manualmente con git diff o regenerar tras validar.

**No se usa para detectar regresiones por igualdad exacta.** Es referencia humana para entender qué cambió.

## Limitaciones conocidas

- **No llama al LLM.** Lee siempre del estado actual de la DB. Para validar un cambio de prompt: primero re-enriquece los spots con `orchestrator_v2 --force-spot-ids <ids>`, después corre `check`.
- **Heurísticas blandas para cronología.** `chronology_not_inverted` es una heurística textual; refuerza con revisión humana (`soft` tier).
- **Skip silencioso cuando faltan columnas/tablas.** Si T1.4 aún no merged, las aserciones que requieran `spot_alerts` devuelven SKIP, no FAIL. Eso es intencional — la suite no debe romper la build solo porque migraciones aún no se aplicaron.
