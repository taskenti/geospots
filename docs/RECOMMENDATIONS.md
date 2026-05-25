# GeoSpots — Recomendaciones Priorizadas

Análisis de deuda técnica, riesgos y oportunidades de mejora. Ordenado por impacto/urgencia.

---

## PRIORIDAD ALTA — Bugs y Riesgos Activos

### 1. ~~`campingcarinfos` en scheduler pero sin implementación~~ ✅ RESUELTO (2026-05-25)
Implementado en `scraper/sources/campingcarinfos.py` con descarga global única (ZIP+ASCII). Primera carga: 24,132 spots en 103s, 0 errores, 83% deduplicados con spots existentes.

**Pendiente — reviews de campingcarinfos**: el bulk download solo trae coordenadas y categoría. La web sí tiene comentarios, pero solo accesibles por scraping HTML por cada CCI ID (24K páginas, ~7h a 1 req/s). Implementar como `download_reviews()` separado solo si se necesita más profundidad semántica para spots únicamente cubiertos por CCI (~11K spots).

### 2. `normalized` vs `normalized_data` — campo duplicado en `source_records`
**Impacto**: Doble escritura en DB, queries ambiguas, consume espacio innecesario.  
**Esfuerzo**: Bajo (migration + grep para asegurarse de que nada lee `normalized`).  
**Acción**: Migration que copia datos de `normalized` a `normalized_data` donde `normalized_data` es NULL, luego `DROP COLUMN normalized`. Verificar primero que ningún código lee `normalized` directamente.

### 3. Funciones duplicadas en `enrichment/worker.py` y `scraper/db.py`
**Impacto**: Bugs corregidos en un lugar no se propagan al otro; comportamiento inconsistente.  
**Afectado**: `_insert_claim()`, `_insert_observation()` (o equivalentes).  
**Esfuerzo**: Medio.  
**Acción**: Mover las funciones de inserción de claims/observaciones a un módulo compartido (`scraper/db.py` o un nuevo `shared/db_phase3.py`) que tanto el scraper como el enrichment worker puedan importar.

### 4. ~~STAYFREE_AUTHORIZATION/API_TOKEN caducan sin aviso~~ ✅ RESUELTO (2026-05-25)
Añadido `_log_token_expired()` helper que se llama desde todos los puntos donde se hacen requests a la API privada. Detecta HTTP 401/403/419 (en lugar de solo 401), muestra un banner muy visible con instrucciones paso a paso para regenerar tanto `STAYFREE_AUTHORIZATION` (JWT del navegador) como `STAYFREE_API_TOKEN` (vía MITM del APK). También corregida la doc obsoleta que mencionaba `STAYFREE_XSRF_TOKEN` (variable que nunca existió en el código).

### 5. ~~Endpoint `debug_furgovw` expuesto en producción~~ ✅ RESUELTO (2026-05-25)
Eliminado. La información que devolvía la cubre `/dashboard` (counts) y `psql` directo (samples). Era código muerto tras la estabilización de furgovw.

---

## PRIORIDAD ALTA — Robustez del Pipeline

### 6. `reconciliar.py` — sin modo incremental
**Impacto**: A medida que la base crece (ya puede haber >100K spots multi-fuente), el scan completo tarda minutos y bloquea la DB.  
**Esfuerzo**: Medio.  
**Acción**: Añadir un campo `reconciled_at TIMESTAMPTZ` a `spots`. El job filtra `WHERE reconciled_at IS NULL OR reconciled_at < updated_at` para procesar solo los spots que cambiaron desde la última reconciliación.

### 7. ~~`generate_active_grid()` vacío en DB vacía~~ ✅ RESUELTO (2026-05-25)
El fallback existía pero era catastrófico: generaba grid global con step normal (~48.600 celdas con step=1°), suficiente para tirar APIs y banear IPs. Reemplazado por bootstrap **coarse sobre EU_BOUNDS** con step inflado a 5° (~72-110 celdas según la fuente). Tras la primera ingesta, el grid activo normal toma el relevo automáticamente. Loguea WARNING explícito indicando que es bootstrap.

### 8. `run_all_sources()` es puramente secuencial
**Impacto**: Un ciclo completo de scraping tarda ~horas aunque la mayoría del tiempo es I/O (HTTP).  
**Esfuerzo**: Medio.  
**Acción**: Agrupar fuentes por tipo: las que usan `generate_active_grid` pueden correr en paralelo entre sí (compiten por celdas distintas). Fuentes "globales" (furgovw, ioverlander) pueden correr en paralelo con cualquier otra. Usar `asyncio.gather()` con grupos.

### 9. ~~Sin validación de coordenadas en `normalize()`~~ ✅ RESUELTO (2026-05-25)
Añadido `AbstractSource.coords_validas(lat, lon)` como staticmethod centralizada. Rechaza None, NaN, fuera de rango y (0,0). Aplicada en `base.run()` y en los 3 scrapers con `run()` propio (ioverlander, furgovw, campingcarinfos). DB ya estaba limpia gracias a validaciones ad-hoc previas; este cambio garantiza que cualquier scraper futuro hereda la protección sin tener que implementarla.

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

### 14. ~~`/points` endpoint sin paginación~~ ✅ RESUELTO (2026-05-25)
Añadidos parámetros obligatorios `north/south/east/west` + `limit` (default 5000, max 20000) + filtros opcionales `tipo`/`gratuito`. Usa el índice GIST sobre `spots.geog` vía `ST_MakeEnvelope` (rápido incluso para bbox de Europa entera). Cuando el bbox supera el límite, devuelve los puntos con mayor `master_rating` y marca `truncated: true` para que el cliente decida si necesita acercar zoom. Rechaza explícitamente bbox que cruza el antimeridiano (400) para evitar full-scan. La respuesta incluye `total_in_bbox` para que el frontend muestre "X de Y" sin tener que volver a contar.

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
| 1 | ~~campingcarinfos sin implementación~~ ✅ RESUELTO | ALTO | Mínimo |
| 5 | ~~debug endpoint en producción~~ ✅ RESUELTO | MEDIO | Mínimo |
| 9 | ~~Validar coordenadas en normalize()~~ ✅ RESUELTO | ALTO | Mínimo |
| 4 | ~~Aviso claro token StayFree~~ ✅ RESUELTO | MEDIO | Mínimo |
| 6 | Reconciliar incremental | ALTO | Medio |
| 7 | ~~Bootstrap grid vacío~~ ✅ RESUELTO | ALTO | Bajo |
| 2 | Eliminar campo normalized duplicado | MEDIO | Bajo |
| 13 | Trigger para spots.fuentes | MEDIO | Bajo |
| 14 | ~~Paginar /points~~ ✅ RESUELTO | ALTO | Bajo |
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

**Quick wins**: todos resueltos (items 1, 4, 5, 7, 9, 14). Próximos pasos sugeridos: items 2 (duplicate column), 3 (duplicated DB functions), 13 (trigger para spots.fuentes), 16 (concurrencia en worker).
