# Fase 3 — Plan de Sprints de Remediación

> Origen: auditoría profunda del pipeline de enriquecimiento (sesiones 2026-05-28 y 2026-05-29).
> 37 bugs confirmados sobre DB en vivo (~125K spots activos, ~5M reviews, 203 spots con `summary_en`, 49.940 con `spot_semantic_state`).
> Catálogo completo en `memory/enrichment_audit_bugs.md`. Spots de validación en `memory/conflictive_spots.md`.

## Principio rector

**Arreglar señales ANTES de generar embeddings.** `spot_embeddings` está vacío (0 filas) → el DSL envenenado NO ha propagado a vectores todavía. Cada sprint que toca señales debe completarse y validarse contra los 10 spots conflictivos antes de pasar al siguiente. Phase 4 (embeddings) se ejecuta SOLO al final.

**Inmutables vs regenerables** (ver CLAUDE.md): los fixes regeneran `normalized_observations` y `spot_semantic_state` desde `extracted_claims`/`raw_data`. Nunca tocar `raw_data`, `reviews.texto`, ni claims con `review_id IS NOT NULL`.

**Orden de prioridad declarado:** BUG-21 → BUG-01 → BUG-02 → BUG-06 → BUG-07 → BUG-03 → BUG-11 → BUG-29 → BUG-22 → resto FUERTE/MEDIO → LEVE.

---

## Sprint 0 — Seguridad operativa / presupuesto (BUG-21) — CRÍTICO, BLOQUEANTE

**Objetivo:** que ningún batch pueda volver a quemar presupuesto sin control. Tras un batch lanzado por nosotros, el sistema PARA. Sin excepciones.

**Estado al iniciar:**
- ✅ `docker-compose.yml`: contenedor `enrichment` con `restart: "no"` + `command: ["sleep", "infinity"]` (no bucle continuo).
- ✅ `worker.py`: 5 capas (CLI gate `--enable-llm`+`--country`/`--allow-global`, hard cap `--max-llm-calls`, detección `spending cap`/`RESOURCE_EXHAUSTED`, abort por errores consecutivos, defaults seguros).
- ❌ `orchestrator_v2` / `nightly_enrichment_v2.py`: SIN tope de coste USD, SIN scope obligatorio (correr sin flag = TODOS los países en silencio), SIN abort por spending_cap. Sólo `--limit` (default 500) acota el gasto.

**Tareas:**
1. `orchestrator_v2.run_enrichment`: nuevo parámetro `max_cost_usd: float | None`.
2. `RunStats`: flag `abort: bool`.
3. `_process_one_spot`: guard al inicio — si `stats.abort` o `cost_estimated_usd >= max_cost_usd` → `spots_skipped++` y return (drenaje barato, asyncio es mono-hilo).
4. `_process_one_spot`: en el handler de excepción, detectar `RESOURCE_EXHAUSTED`/`spending cap`/`quota` → `stats.abort = True` (hard stop, no reintentar).
5. `nightly_enrichment_v2.py`: `--max-cost-usd` **obligatorio**; scope explícito **obligatorio** (uno de `--country`/`--tier`/`--rest`; nunca ALL silencioso).

**Validación:** dry-run + un batch real pequeño (Andorra) verificando que el run se detiene al alcanzar el tope.

---

## Sprint 1 — Léxico / needles (matching por substring) — CRÍTICO

Raíz común: `claim_extractor.PATTERNS` usa `if needle in lowered` sin límites de palabra ni polaridad.

| Bug | Fix |
|-----|-----|
| BUG-01 | `"lac"` → `lake_nearby`: límites de palabra; excluir place/emplacement/black. 84.8% FP (BUG-33). |
| BUG-02 | `"fine"` → `police_risk`: distinguir adjetivo inglés de multa. 89.9% FP. |
| BUG-14 | `"sheltered from wind"` en lista `wind_exposure=0.85` → INVERSIÓN de polaridad, debe ser 0.1. |
| BUG-18 | `"lleno"` → crowd: excluir relleno, "lleno de baches". |
| BUG-13 | `"sale"` (EN) → cleanliness dirty (ES "sale"≠sucio). |
| BUG-20 | `"security"` → safety=0.85 en contexto neutro/negativo. |
| BUG-19 | `"molestos"` → youth_trouble (charcos molestos). |
| BUG-24 | `"río cercano"` → lake_nearby (LLM confunde río con lago). |

**Acción:** reescribir matching a regex con `\b` y listas de exclusión; tabla de polaridad explícita por señal. Re-extraer claims afectados desde `reviews.texto` (regenerable).

**Validación:** lake_nearby FP < 5% en muestra; spots Andorra (30252, 85063) sin lago.

---

## Sprint 2 — Negación multi-idioma — FUERTE

| Bug | Fix |
|-----|-----|
| BUG-04 | `"dormir"/"overnight"` en contexto prohibitivo → overnight_safe=true (446 FP). |
| BUG-05 | `"safe"` substring de "unsafe" → safety 0.85 y 0.2 simultáneo. |
| BUG-15 | `"hay agua"` en "no hay agua" → water_working true y false. |
| BUG-28 | Alemán: `"nicht ruhig"/"unruhig"` → quietness=0.9 (128 FP). |
| BUG-29 | Negación general: 197 safety FP (inseguro/unsafe), 137 cleanliness FP, 306 overnight_safe con prohibición. |
| BUG-36 | Agua congelada estacional → water_working=false permanente (18 claims). |

**Acción:** ventana de negación por idioma (ES no/nunca/prohibido, EN no/not/un-, DE nicht/kein/un-, FR ne...pas) antes de asignar polaridad. Detección de estacionalidad para water_working (no marcar rotura permanente por helada).

---

## Sprint 3 — Temporal / fechas — CRÍTICO (BUG-07) + FUERTE

| Bug | Fix |
|-----|-----|
| BUG-07 | Decay anclado a `detected_at` no `valid_from` → evento 2015 decae desde 2026. |
| BUG-22 | scraped_facts `observed_at` = timestamp de ingesta, no fecha de publicación → pisa reviews más recientes. |
| BUG-10/17 | furgovw NULL fecha → observed_at=today (3.956 reviews tratadas como frescas). |
| BUG-26 | NULL fecha devuelve 0 en queries "reviews después de X" → impide invalidar alertas. |
| BUG-31 | 13 reviews futuras (hasta 2033) de womostell → age_days=0, recency_boost=1.5. |
| BUG-09 | Mislabel de idioma en caramaps (FR→ES, ~5.000 reviews). |

**Acción:** anclar decay a `valid_from`/fecha de publicación; NULL fecha → peso reducido, no "hoy"; clamp de fechas futuras; corregir detección de idioma.

---

## Sprint 4 — Agregación / confianza — CRÍTICO (BUG-06)

| Bug | Fix |
|-----|-----|
| BUG-06 | Boolean confidence=1.0 con una sola observación sin oposición. |
| BUG-25 | consensus_confidence INVERTIDO y topado en ~0.5 (364 obs=0.22, 2 obs=0.50). |
| BUG-16 | Señales de texto (parking_capacity, noise_source) descartadas por el aggregator. |
| BUG-27 | DSL `wind:+0.8` ambiguo (alto=malo) vs convención `+`=bueno. |
| BUG-35 | Casing boolean inconsistente (True/False vs true/false). |

**Acción:** rediseñar fórmula de consensus (monotónica creciente con nº observaciones); escalar confidence boolean por soporte; manejar señales de texto en el aggregator; normalizar casing; documentar/corregir convención de signo del DSL.

---

## Sprint 5 — Cierres y ciclo de vida de alertas — CRÍTICO (BUG-11)

| Bug | Fix |
|-----|-----|
| BUG-03 | construction/obras → spot_closed (1.368 spots). |
| BUG-08 | Cierres parciales (WC/restaurante) → spot_closed (32%). |
| BUG-11 | spot_closed irreversible sin observación opuesta. |
| BUG-32 | Alertas stale nunca se auto-resuelven aunque el LLM lo note. |

**Regla de negocio (usuario):** si alguien dice "cerrado" pero hay reviews posteriores → descartar el claim de cerrado. Construcción dura 1-6 meses, no 11 años.

**Acción:** spot_closed se limpia con evidencia de review posterior; distinguir cierre parcial de total; alertas con TTL por tipo y auto-resolución por narrativa LLM + ausencia de menciones recientes.

---

## Sprint 6 — Duplicación / estructural / metadatos — MEDIO/LEVE

| Bug | Fix |
|-----|-----|
| BUG-30 | Doble-enrichment v4+v6 (102 spots, claims duplicados). |
| BUG-37 | Sin constraint único en extracted_claims (causa raíz de BUG-30). |
| BUG-23 | LLM repite STATIC_CONTEXT como review_claims (558 claims, double-count). |
| BUG-34 | source_credibility.total_records=0 en todas las fuentes. |
| BUG-12 | Tags no canonicalizados (dog-friendly vs dog friendly). |
| — | Contradicción estructural: 1.240 spots overnight_safe=true AND police_risk>0.7. |

**Acción:** UNIQUE constraint + dedup en re-enrichment; filtrar STATIC_CONTEXT del output LLM; poblar total_records (sync_db.py); canonicalizar tags; regla de coherencia overnight vs police.

---

## Sprint 7 — Regeneración final

1. Recompute completo de `normalized_observations` + `spot_semantic_state` desde claims corregidos.
2. Validar los 10 spots conflictivos (`memory/conflictive_spots.md`).
3. Regenerar DSL.
4. **SOLO entonces:** generar embeddings (Phase 4) sobre estado limpio.
5. Detección de eventos sobre estado limpio.

---

## Validación transversal (cada sprint)

```sql
SELECT id FROM spots WHERE id = ANY(ARRAY[30252,85063,179854,221624,272617,4533,329717,439305,5049,182524]);
```
Re-enriquecer vía orchestrator_v2 (con `--max-cost-usd` acotado) y comparar `spot_semantic_state` antes/después.
