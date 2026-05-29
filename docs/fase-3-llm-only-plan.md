# Fase 3 — Plan LLM-only (Opción A)

**Estado:** 📋 Documentado, NO implementado. Es la arquitectura objetivo si GeoSpots
pasa a producción o genera ingresos.
**Fecha:** 2026-05-29
**Autor del trigger:** el usuario, tras detectar el FP estructural
`police_risk=0.85` en *"Parfait, superbe et propre. La police fait quelques rondes."*

---

## 0. TL;DR

El pipeline regex (`claim_extractor.extract_claims_regex`) es **ciego a la polaridad
contextual** y solo captura señales con keyword explícita. Tras 37 bugs corregidos
en 8 sprints sigue produciendo FP estructurales que NO son arreglables con más
needles. La **Opción A** elimina el regex del path review-level y manda **toda**
review informativa al LLM, que entiende contexto, polaridad y multi-tópico.

- **Coste batch completo (4.5M reviews):** ~**$182** con DeepSeek V4 Flash (vs ~$113 híbrido).
- **Coste España solo:** ~**$24** LLM-only (vs ~$10.5 híbrido).
- **Delta total:** +~$70 sobre el modelo híbrido actual.
- **Lo que compras:** señales correctas en polaridad, captura de señales implícitas,
  multi-tópico real, y **borrado de las ~450 líneas de PATTERNS + 37 bugs de
  mantenimiento**.

> **NO implementar sin el hard-stop de presupuesto.** Restricción permanente del
> usuario: *"Después de un batch que mandemos nosotros se tiene que parar, esto no
> puede suceder; si no tengo límites mañana me despierto con un desfalco gordo."*
> El cap duro de llamadas (`llm_calls` en `worker.py`) y el abort por errores
> consecutivos son OBLIGATORIOS y deben probarse antes de cualquier batch real.

---

## 1. Por qué el regex no basta (motivación)

### 1.1 Ceguera de polaridad (el caso que disparó esto)

```
review: "Parfait, superbe et propre. La police fait quelques rondes."
regex:  ve "police" → police_risk=0.85 (RIESGO)
real:   las rondas policiales son SEGURIDAD (positivo)
```

El regex casa keywords, no significado. "police" puede ser:
- **riesgo** — *"police fined us / nos echaron"*
- **tranquilidad** — *"police does rounds / la zona está vigilada"*

Lo mismo con `overnight` (mención positiva vs prohibición), `security` (good vs poor),
`closed` (spot vs servicio puntual). Hemos parcheado cada caso con needles negativos
explícitos y ventanas de negación, pero es un juego de whack-a-mole: cada idioma y
cada giro nuevo es un FP potencial.

### 1.2 Falsos negativos (señales implícitas)

```
review: "se sienten seguros porque la policía hace rondas"
regex:  NO hay needle de safety positivo aquí → NO emite safety=0.85
LLM:    infiere safety=alto del contexto
```

El regex solo ve lo que tiene keyword. Pierde toda inferencia: *"dormimos del tirón"*
(quietness sin la palabra "tranquilo"), *"el dueño nos trató genial"* (welcome/service),
*"ni un alma en todo el finde"* (crowd bajo sin "vacío").

### 1.3 Multi-tópico

```
review: "Carretera fácil, sin problemas con vecinos y buen acceso al río"
regex:  capta quietness (de "sin problemas"... a veces) y poco más
LLM:    road_quality=alto + quietness=alto + river_nearby=true
```

El regex SÍ extrae varias señales (no es first-match), pero solo las que tienen
keyword. Una frase densa con 3-4 conceptos implícitos se queda en 1.

### 1.4 Coste de mantenimiento

`PATTERNS` son ~450 líneas, 4 idiomas, y han generado 37 bugs catalogados
(`memory/enrichment_audit_bugs.md`). Cada needle nuevo arriesga FP por substring,
polisemia o polaridad. El LLM elimina toda esa superficie de mantenimiento.

---

## 2. Qué cambia (arquitectura)

### 2.1 Pipeline actual (híbrido — Opción B vigente tras Sprint 8)

```
review.texto → clean → extract_claims_regex
  ├─ texto < 120 chars                    → solo regex
  ├─ texto ≥ 120 + regex ≥ 3 claims        → solo regex
  ├─ texto ≥ 120 + regex 0-2 claims        → LLM complementa
  └─ menciona police_risk (Opción B)       → fuerza LLM
→ normalized_observations → update_semantic_state
```

### 2.2 Pipeline LLM-only (Opción A)

```
review.texto → clean → [¿informativo? ¿len ≥ FLOOR?] → extract_claims_llm
→ normalized_observations → update_semantic_state
```

- **Se elimina** la rama regex de `worker._extract_claims_with_retry` y de
  `claim_extractor.extract_claims`.
- **Se conserva** `extract_claims_regex` SOLO como fallback offline / smoke-test /
  modo degradado sin presupuesto (flag `ENRICHMENT_MODE=regex_only`).
- Toda review informativa con `len(texto_limpio) ≥ ENRICHMENT_LLM_MIN_CHARS`
  (sugerido **40**) va al LLM. Por debajo del floor no aporta señal útil y se marca
  procesada sin claims.
- El **blend léxico multilingüe** (`apply_lexicon_blend`) se mantiene: reponderar la
  confianza de los claims del LLM con el léxico no estorba y es barato (local).

### 2.3 Relación con orchestrator_v2

`orchestrator_v2` (spot-level) ya es LLM-only en su contexto. La Opción A NO lo
sustituye: son capas distintas.
- **worker.py LLM-only** = señales numéricas/booleanas por review (granular, datadas).
- **orchestrator_v2** = narrativa + claims de calidad con visión de las 35 reviews.

Decisión a tomar en producción: si orchestrator_v2 cubre el 100% de spots con ≥3
reviews, el worker LLM-only solo aporta valor para spots con 1-2 reviews y para
señales datadas finas (event_detector). Evaluar antes de pagar los dos.

---

## 3. Modelo de coste (DeepSeek V4 Flash)

Base de cálculo (auditoría 2026-05-28, `jobs/audit_llm_volume.py`):
- 4.54M reviews pendientes con texto.
- ~70.4% (3.20M) tienen ≥120 chars.
- Tasa de escalado híbrido: 61.7% de las ≥120 → ~2.8M llamadas.
- DeepSeek V4 Flash: **~$113 / 2.8M llamadas ≈ $0.0000404/llamada** (≈$40.4/millón).
- Tokens medios: ~517 input (system≈400 + texto≈67 + frame≈50) + ~150 output.

| Escenario | Reviews al LLM | Llamadas | Coste DeepSeek |
|---|---|---|---|
| **Híbrido (actual)** | solo 0-2 claims & ≥120 | ~2.8M | **~$113** |
| **LLM-only (≥40 chars)** | ~todas informativas | ~4.5M | **~$182** |
| Delta | +1.7M | | **+~$70** |

### Desglose por país (estimación; España ≈13% del volumen)

| País | Reviews (aprox) | Híbrido | LLM-only |
|---|---|---|---|
| España (ES) | ~585K | ~$10.5 | **~$24** |
| Francia (FR) | ~700K | ~$13 | ~$28 |
| Alemania (DE) | ~500K | ~$9 | ~$20 |
| Italia (IT) | ~450K | ~$8 | ~$18 |

> Cifras orientativas; el coste real depende de la longitud media por país y del
> precio vigente de DeepSeek. Recalcular con `jobs/audit_llm_volume.py --country XX`
> antes de lanzar.

**Conclusión económica:** el delta para España es ~$13. Es ruido frente al valor de
tener señales correctas. La barrera nunca fue el dinero, sino el **riesgo de fuga de
presupuesto sin tope** — que se resuelve con los controles del §5, no evitando el LLM.

---

## 4. Implementación (cuando se decida ejecutar)

### 4.1 Cambios de código

> **Estado: el toggle ya está IMPLEMENTADO (Sprint 8.1).** Lo que queda pendiente para
> la Opción A productiva es la auditoría de prompt (#3) y el filtro por `signal_registry`
> (#4) — no la mecánica de modos.

1. **Punto único de decisión** `claim_extractor.should_escalate_to_llm(text, n_regex,
   force_llm, mode, llm_min_chars)` + `resolve_enrichment_mode(mode)`. Usados por
   `extract_claims` (path sin throttling) y por `worker._extract_claims_with_retry`
   (path con semáforo/cap) → ambos caminos coherentes (regla CLAUDE.md). Lógica:
   ```python
   if mode == "regex_only": return False
   if mode == "llm_only":   return len(text) >= llm_min_chars   # floor bajo (~30)
   # hybrid (Opción B): force_llm → True; si no, escala solo si len>=120 y n_regex<3
   ```
   `extract_claims(..., mode=None)` y el worker aceptan `mode`; `--mode` (CLI) y
   `ENRICHMENT_MODE` (env) lo seleccionan. `use_gemini=False`/`regex_only` siguen
   siendo override duro (coste 0).

2. **Tope de presupuesto intacto**: en `llm_only` se salta el gate de n_regex y el de
   <120, pero el hard cap (`--max-llm-calls`, CAPA 4) se aplica IGUAL — el batch aborta
   al alcanzarlo. Test: `tests/test_enrichment_mode.py`.

3. **`prompts.build_extraction_prompt`** — auditar que el system prompt liste TODAS
   las señales del `signal_registry` con su polaridad y rango de valores, porque en
   LLM-only no hay regex que las "siembre". Incluir ejemplos del caso policía
   (patrulla=seguridad, multa=riesgo) para anclar la polaridad.

4. **`signal_registry`** — usarlo como fuente única de señales válidas; descartar en
   `_parse_json_response` cualquier `signal` que el LLM invente fuera del registro.

### 4.2 Variables de entorno nuevas

| Variable | Default | Descripción |
|---|---|---|
| `ENRICHMENT_MODE` | `hybrid` | `hybrid` \| `llm_only` \| `regex_only` |
| `ENRICHMENT_LLM_MIN_CHARS` | `40` | Floor de longitud para escalar en `llm_only` |
| `ENRICHMENT_LLM_CALL_CAP` | (obligatorio en batch) | Tope duro de llamadas por ejecución |

---

## 5. Controles de presupuesto (OBLIGATORIOS — no negociable)

Estos controles YA existen en `worker.py` y deben estar activos y probados antes de
cualquier batch LLM-only:

1. **Hard cap por batch** (`llm_calls=[hechas, cap]`): aborta con
   `RuntimeError("llm_call_cap_reached")` al alcanzar el tope. El incremento es
   optimista (los retries cuentan contra el cap). → calcular `cap` = reviews del país
   × 1.05 de margen, NUNCA "ilimitado".
2. **Abort por errores consecutivos** (`ENRICHMENT_MAX_CONSECUTIVE_ERRORS`, default 20).
3. **Detección de spending cap** (`RESOURCE_EXHAUSTED` + "spending cap") → abort inmediato.
4. **El contenedor `enrichment` NO debe correr con `restart: unless-stopped` en batch
   LLM-only** sin un cap externo: procesa en bucle y puede quemar cuota. Lanzar como
   job one-shot con `--batch-size` acotado y `ENRICHMENT_LLM_CALL_CAP`.
5. **Smoke test obligatorio en Andorra** (~200 reviews) antes de cada país nuevo:
   verifica que el cap dispara y que el coste por review está en rango esperado.

> Checklist pre-batch: `cap` definido · smoke test pasado · billing alert configurado
> en el provider · `ENRICHMENT_MODE=llm_only` confirmado · país acotado con `--country`.

---

## 6. Validación de calidad (antes de confiar en LLM-only)

1. **A/B sobre los 10 spots conflictivos** (`memory/conflictive_spots.md`):
   re-extraer con `llm_only` y comparar señales vs híbrido vs verdad manual.
   Esperado: police_risk correcto en patrullas, safety positivo capturado,
   multi-tópico completo.
2. **Muestra ciega de 100 reviews** etiquetadas a mano → precision/recall por señal
   LLM-only vs híbrido. Aceptar solo si precision ≥ híbrido en las señales de
   polaridad ambigua.
3. **Consistencia**: re-correr el mismo lote dos veces y medir drift de claims
   (el LLM no es determinista; fijar `temperature=0` y validar varianza < umbral).
4. **Coste real vs estimado**: medir $/1000 reviews en el smoke test y extrapolar
   antes de lanzar el país completo.

---

## 7. Rollout sugerido

1. Implementar `ENRICHMENT_MODE` + floor + auditar prompt (§4).
2. Smoke test Andorra en `llm_only` con cap = 250. Validar coste y cap.
3. A/B en los 10 spots conflictivos (§6.1). Si pasa, seguir.
4. España (`--country ES`, cap ≈ 615K). Medir coste real.
5. Comparar resultado ES con el híbrido previo (señales agregadas en
   `spot_semantic_state`). Si la calidad sube y el coste cuadra, continuar país a país
   en el orden de CLAUDE.md (PT → FR → DE → IT → UK → US → resto).
6. Una vez validado globalmente: retirar `PATTERNS` del path productivo (dejar como
   `regex_only` de emergencia) y cerrar los 37 bugs como obsoletos.

---

## 8. Estado actual (Sprint 8 — Opción B, puente hacia A)

Mientras no se ejecute la Opción A, el sistema corre en **modo híbrido con la mejora
de Opción B** ya implementada:
- `police_risk` ya NO se emite por regex (`_AMBIGUOUS_POLARITY_SIGNALS`); su mención
  fuerza escalado al LLM vía `text_mentions_ambiguous_signal()`.
- `overnight_safe` se resuelve por polaridad dentro del regex (la prohibición anula la
  mención positiva), a coste cero.
- Tests: `tests/test_polarity_sprint8.py`.

**Sprint 8.1 — toggle de modo wireado (2026-05-29):**
- `ENRICHMENT_MODE` (env) + `--mode {hybrid,llm_only,regex_only}` (CLI del worker)
  seleccionan el modo. Decisión centralizada en `should_escalate_to_llm` →
  `extract_claims` y `worker._extract_claims_with_retry` comparten el mismo gate.
- Panel: `POST /admin/enrichment/run_worker/{country}?mode&max_llm_calls&batch_size`
  lanza el worker (review-level) con el modo elegido. `max_llm_calls` es OBLIGATORIO
  para modos con LLM (se traslada a `--max-llm-calls`; el batch aborta al alcanzarlo).
  UI: barra "⚙️ Worker" en `pwa/admin_enrichment.html` (selector modo + tope LLM +
  batch) y botón "▶ Worker" por país, junto al "▶ Lanzar V2" (orchestrator spot-level).
- Tests: `tests/test_enrichment_mode.py`.
- Falta para Opción A productiva: auditoría de prompt (§4.1 #3) + filtro `signal_registry`
  + validación de calidad (§6). La mecánica de modos ya no es bloqueante.

La Opción A es la evolución natural: generaliza el principio de Opción B
(*"si la polaridad importa, decídela con el LLM"*) a **todas** las señales.
