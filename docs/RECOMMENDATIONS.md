# GeoSpots — Recomendaciones Priorizadas

Análisis de deuda técnica, riesgos y oportunidades de mejora. Ordenado por impacto/urgencia.

---

## PRIORIDAD ALTA — Bugs y Riesgos Activos

### 1. `campingcarinfos` en scheduler pero sin implementación
**Impacto**: Cualquier `--all` falla con `ModuleNotFoundError` sin aviso claro.  
**Esfuerzo**: Bajo.  
**Acción**: O crear `scraper/sources/campingcarinfos.py` (mínimo una semana de trabajo) o eliminarlo de `SOURCES` en `scheduler.py` hasta que esté listo. La opción rápida es eliminarlo del dict.

### 2. `normalized` vs `normalized_data` — campo duplicado en `source_records`
**Impacto**: Doble escritura en DB, queries ambiguas, consume espacio innecesario.  
**Esfuerzo**: Bajo (migration + grep para asegurarse de que nada lee `normalized`).  
**Acción**: Migration que copia datos de `normalized` a `normalized_data` donde `normalized_data` es NULL, luego `DROP COLUMN normalized`. Verificar primero que ningún código lee `normalized` directamente.

### 3. Funciones duplicadas en `enrichment/worker.py` y `scraper/db.py`
**Impacto**: Bugs corregidos en un lugar no se propagan al otro; comportamiento inconsistente.  
**Afectado**: `_insert_claim()`, `_insert_observation()` (o equivalentes).  
**Esfuerzo**: Medio.  
**Acción**: Mover las funciones de inserción de claims/observaciones a un módulo compartido (`scraper/db.py` o un nuevo `shared/db_phase3.py`) que tanto el scraper como el enrichment worker puedan importar.

### 4. STAYFREE_XSRF_TOKEN caduca sin aviso
**Impacto**: El scraper de StayFree falla silenciosamente con 403 o 419. Los logs muestran error pero no indican que es el token.  
**Esfuerzo**: Bajo.  
**Acción**: En `stayfree.py`, detectar explícitamente el código 403/419 y loguear un mensaje claro: `"STAYFREE_XSRF_TOKEN ha caducado — renovar en .env"`.

### 5. Endpoint `debug_furgovw` expuesto en producción
**Impacto**: Expone información interna del sistema (conteos, IDs, estado de integridad).  
**Esfuerzo**: Mínimo.  
**Acción**: Mover a endpoint con prefijo `/admin/` o eliminar si ya no es necesario. Como mínimo, documentar en `CLAUDE.md` que existe y por qué.

---

## PRIORIDAD ALTA — Robustez del Pipeline

### 6. `reconciliar.py` — sin modo incremental
**Impacto**: A medida que la base crece (ya puede haber >100K spots multi-fuente), el scan completo tarda minutos y bloquea la DB.  
**Esfuerzo**: Medio.  
**Acción**: Añadir un campo `reconciled_at TIMESTAMPTZ` a `spots`. El job filtra `WHERE reconciled_at IS NULL OR reconciled_at < updated_at` para procesar solo los spots que cambiaron desde la última reconciliación.

### 7. `generate_active_grid()` vacío en DB vacía
**Impacto**: Un entorno nuevo (dev, staging) no puede arrancar scrapers estándar porque la grilla activa depende de spots existentes, pero no hay spots porque nunca se scrapeó.  
**Esfuerzo**: Bajo.  
**Acción**: En `base.py`, si `generate_active_grid()` retorna lista vacía, usar un grid de bootstrap hardcodeado para Europa central (e.g. 20 celdas de 5° × 5° cubriendo el continente). Documentarlo como comportamiento esperado.

### 8. `run_all_sources()` es puramente secuencial
**Impacto**: Un ciclo completo de scraping tarda ~horas aunque la mayoría del tiempo es I/O (HTTP).  
**Esfuerzo**: Medio.  
**Acción**: Agrupar fuentes por tipo: las que usan `generate_active_grid` pueden correr en paralelo entre sí (compiten por celdas distintas). Fuentes "globales" (furgovw, ioverlander) pueden correr en paralelo con cualquier otra. Usar `asyncio.gather()` con grupos.

### 9. Sin validación de coordenadas en `normalize()`
**Impacto**: Spots con lat=0/lon=0 (valor por defecto de muchas APIs cuando falla la geolocación) entran en la DB y aparecen en el Golfo de Guinea.  
**Esfuerzo**: Mínimo.  
**Acción**: En `AbstractSource.run()`, añadir validación antes de insertar: `if not (-90 <= norm['lat'] <= 90 and -180 <= norm['lon'] <= 180): continue`. También descartar `lat=0 AND lon=0`.

---

## PRIORIDAD MEDIA — Calidad de Datos

### 10. Geohash7 existe pero no se usa en dedup
**Impacto**: El dedup usa `ST_DWithin` con índice GIST, que funciona bien. El geohash podría acelerar una pre-búsqueda pero actualmente solo sirve de clustering visual.  
**Esfuerzo**: Bajo.  
**Acción**: No es urgente, pero si el dedup se vuelve el cuello de botella: añadir pre-filtro por geohash6 (celdas adyacentes) antes del `ST_DWithin`. Documentar el no-uso actual en `CLAUDE.md`.

### 11. `tipo='otro'` acumula spots sin clasificar
**Impacto**: Los spots con `tipo='otro'` no pueden ser filtrados correctamente en `/search`, ni el vector search los clasifica bien.  
**Esfuerzo**: Medio-alto.  
**Acción**: Añadir un job de "tipo inference" que use el semantic_state y las descripciones disponibles para reclasificar spots con `tipo='otro'`. Gemini Flash puede hacer esto en batch económico: "dado este nombre y descripción, ¿es camping, wildcamp, area, parking o naturaleza?"

### 12. `master_rating` no está normalizado entre fuentes
**Impacto**: Park4night usa escala 0-5 (×2 en normalize), pero otras fuentes pueden usar 0-10, 1-5, o porcentaje. Si `reconciliar.py` mezcla valores sin normalizar, los ratings son incorrectos.  
**Esfuerzo**: Medio.  
**Acción**: Auditar todos los `normalize()` para verificar que retornan `rating_promedio` en escala 0-10. Añadir comentario en `AbstractSource` documentando la escala esperada.

### 13. `fuentes` array en `spots` puede desincronizarse
**Impacto**: `spots.fuentes` es un array que se actualiza en `enriquecer_spot()`, pero si se insertan/eliminan `source_records` directamente en DB, el array queda desactualizado.  
**Esfuerzo**: Bajo.  
**Acción**: Añadir trigger `AFTER INSERT OR DELETE ON source_records` que actualice `spots.fuentes` automáticamente, eliminando la dependencia del código Python.

---

## PRIORIDAD MEDIA — Escalabilidad

### 14. `/points` endpoint sin paginación
**Impacto**: Retorna TODOS los spots activos al mapa. A 500K+ spots, esto transfiere MBs de JSON y puede tumbar el cliente.  
**Esfuerzo**: Bajo.  
**Acción**: Añadir parámetro `bbox` obligatorio o implementar clustering server-side por geohash. Alternativamente, limitar a los N spots más relevantes (mayor rating + más recientes) dentro de un viewport.

### 15. `review_score` y `reviewer_confidence` no se usan
**Impacto**: El weight de las observaciones debería ser `extractor_conf × source_conf × reviewer_conf`, pero `reviewer_confidence` actualmente vale 1.0 para todos.  
**Esfuerzo**: Medio.  
**Acción**: Implementar scoring de reviewers basado en: número de reviews previas, ratio de reviews informativas, antigüedad del perfil. Esto mejora la calidad del semantic_state sin cambiar el schema.

### 16. Worker de enrichment sin concurrencia configurable
**Impacto**: El worker procesa reviews una por una en el batch. Con `GEMINI_API_KEY` con rate limit generoso, podría procesar N en paralelo.  
**Esfuerzo**: Bajo.  
**Acción**: En `worker.py`, usar `asyncio.gather()` con semáforo configurable (`--concurrency 5`) en vez de loop secuencial.

---

## PRIORIDAD BAJA — Mejoras Técnicas

### 17. Tests automatizados inexistentes
**Impacto**: Cualquier cambio en `normalize()`, `find_spot_cercano()` o `reconciliar()` puede introducir regresiones silenciosas.  
**Esfuerzo**: Alto.  
**Acción mínima**: Tests unitarios para `normalize()` de cada scraper (fixtures de raw JSON → assert dict canónico). Tests de integración para `find_spot_cercano()` con casos límite de exclusión de tipos.

### 18. `scraper_log.stats` no incluye duración por celda
**Impacto**: No se puede identificar qué regiones son lentas o tienen muchos errores.  
**Esfuerzo**: Bajo.  
**Acción**: Añadir `{"celda_mas_lenta_s": X, "errores_por_region": {...}}` al stats dict de `finish_scraper_log`.

### 19. Múltiples versiones de Python en `__pycache__`
**Impacto**: Indica que el código se ejecuta con diferentes intérpretes Python (3.12 y 3.14 detectados). Puede causar incompatibilidades.  
**Esfuerzo**: Mínimo.  
**Acción**: Unificar en `.python-version` o `pyproject.toml`. Limpiar `__pycache__` y añadir a `.gitignore` si no lo está.

### 20. `CLAUDE.md` y docs no están en el flujo de CI
**Impacto**: La documentación se desactualiza cuando cambia el código.  
**Esfuerzo**: Bajo.  
**Acción**: Añadir en pre-commit hook o CI: verificar que el dict `SOURCES` en `scheduler.py` coincide con la tabla en `DATA_SOURCES.md`. Automatizable con un script simple.

---

## Resumen por Esfuerzo vs Impacto

| # | Recomendación | Impacto | Esfuerzo |
|---|---|---|---|
| 1 | campingcarinfos sin implementación | ALTO | Mínimo |
| 5 | debug endpoint en producción | MEDIO | Mínimo |
| 9 | Validar coordenadas en normalize() | ALTO | Mínimo |
| 4 | Aviso claro token StayFree | MEDIO | Mínimo |
| 6 | Reconciliar incremental | ALTO | Medio |
| 7 | Bootstrap grid vacío | ALTO | Bajo |
| 2 | Eliminar campo normalized duplicado | MEDIO | Bajo |
| 13 | Trigger para spots.fuentes | MEDIO | Bajo |
| 14 | Paginar /points | ALTO | Bajo |
| 16 | Concurrencia en worker | MEDIO | Bajo |
| 3 | Deduplicar funciones DB | MEDIO | Medio |
| 8 | Paralelizar run_all_sources | MEDIO | Medio |
| 12 | Normalizar master_rating | ALTO | Medio |
| 11 | Inferir tipo='otro' | MEDIO | Medio-alto |
| 17 | Tests automatizados | ALTO | Alto |
| 15 | Reviewer confidence real | BAJO | Medio |
| 10 | Geohash en dedup | BAJO | Bajo |
| 18 | Stats de duración por celda | BAJO | Bajo |
| 19 | Unificar versión Python | BAJO | Mínimo |
| 20 | Docs en CI | BAJO | Bajo |

**Quick wins (máximo impacto, mínimo esfuerzo)**: items 1, 5, 9, 4, 7, 14 — todos completables en una sesión de trabajo.
