# 🌍 Google Maps Scraper

> ⚠️ **Estado: experimental / opt-in**. Este scraper NO se ejecuta en el container scraper estándar — vive en un servicio docker separado (`gmaps`) porque requiere Playwright + Chromium (~700MB extra). Diseñado para enriquecer spots ya conocidos por otras fuentes, no para descubrir nuevos.

## 📖 Propósito
Google Maps es el agregador masivo de POIs y reviews por excelencia. Para GeoSpots su valor está en **enriquecer** spots ya descubiertos por otras fuentes (Park4Night, CamperContact, etc.) con:
- Rating global de Google (escala 0-5 estrellas)
- Cantidad total de reviews
- Web oficial del negocio
- Teléfono
- Texto y rating individual de reviews recientes

Como Google no expone API gratuita para scraping masivo de reviews (Places API es de pago y limitada), recurrimos a **scraping headless** del frontend.

## 🛠️ Arquitectura

### Aislado en servicio docker propio
```
docker-compose.yml
  ├── db, scraper, enrichment, api  (running 24/7, ligeros)
  └── gmaps  (profile=gmaps, on-demand)
```

El servicio `gmaps` parte de `mcr.microsoft.com/playwright/python:v1.49.0-jammy` que ya trae Chromium + dependencias nativas. No participa en `docker-compose up -d` por el `profiles: ["gmaps"]`, solo arranca cuando lo invocas explícitamente.

### Build y ejecución

```bash
# Build (solo la primera vez — ~5-8 min)
docker-compose --profile gmaps build gmaps

# Ejecutar el enriquecimiento (toma 50 spots por defecto)
docker-compose --profile gmaps run --rm gmaps python scheduler.py --google_maps
```

### Pipeline
1. **Selección de candidatos**: query SQL toma los 50 spots con más `total_reviews` que NO tienen `google_maps` en `fuentes[]`. Prioriza spots populares (más probable que existan en Google).
2. **Para cada spot**:
   - Construye URL `https://www.google.com/maps/search/{nombre}/@{lat},{lon},15z?hl=es`
   - Inyecta cookie `SOCS` para evitar el banner de consentimiento de cookies
   - Espera al DOM y comprueba si hay lista de resultados múltiples o ficha directa
   - **Reconciliación**: si hay múltiples resultados, encuentra el más cercano (`haversine_distance <= 150m`) con nombre similar (`SequenceMatcher >= 0.75`)
   - Click en el resultado correcto
   - Extrae: nombre, rating, num_reviews, web, teléfono
   - Click en pestaña "Opiniones" / "Reviews" / "Reseñas" (intenta los 3 idiomas)
   - Scroll del feed para cargar más reviews (4 scrolls de 800px)
   - Parse de cada `div[data-review-id]`: autor, rating, texto, fecha relativa

### Anti-detección aplicada
- User-Agent realista (Chrome 120)
- Locale `es-ES`, timezone `Europe/Madrid`
- Viewport `1280×800`
- Cookie SOCS pre-inyectada
- `--disable-blink-features=AutomationControlled`
- `Object.defineProperty(navigator, 'webdriver', undefined)`
- `rate_limit = 5s` entre spots

## ⚠️ Limitaciones críticas

### 1. Selectores CSS obfuscated y volátiles
Google Maps usa nombres de clase hash-based (`F7nice`, `kvwXae`, `wiu59c`, `m6QErb`, `d4r55`). Estos cambian cada **3-6 meses** sin aviso. Cuando rompen, el scraper sigue corriendo pero las reviews extraídas caen a **0 silenciosamente**.

**Auditoría recomendada**: tras cada run, verificar `stats.reviews_nuevas > 0`. Si baja a 0, abrir Google Maps en un navegador, inspeccionar el DOM actual de un review, actualizar los selectores en `google_maps.py:330-360`.

### 2. Captchas
Google detecta scraping intensivo y muestra reCAPTCHA. El scraper **no los resuelve**. Si aparecen:
- Reducir `rate_limit` a 10s+
- Bajar `LIMIT 50` a `LIMIT 10`
- Usar VPN/proxy rotatorio
- Esperar 24h (los captchas suelen levantarse)

### 3. IP bans permanentes
Google puede banear la IP de forma persistente. Mitigaciones:
- No correr desde IP residencial valiosa
- Usar proxy residential pool si volumen alto
- Considerar la **Google Places API oficial** ($0.017/place + $0.005/review) si necesitas datos de >1000 spots

### 4. Cookie SOCS hardcoded
El value de la cookie `SOCS` puede caducar/cambiar. Si todos los spots fallan en "page failed to load", regenerar:
1. Visitar maps.google.com en Chrome
2. Aceptar cookies
3. DevTools → Application → Cookies → copiar `SOCS` value
4. Pegar en `google_maps.py:128`

## 🗂️ Mapeo y Normalización

| Campo Google | → GeoSpots | Notas |
|---|---|---|
| URL hex `0x...:0x...` (FTID) | `source_id` | CID canónico Google. Único por spot. Fallback a SHA1 de URL completa si no presente |
| `h1` del DOM | `nombre` (canonical_name) | Tras reconciliación spatial+textual con `>=75%` similarity |
| `div.F7nice > span > span` | `rating_promedio` | Escala 0-5, NO se convierte a 0-10 (al ser enrichment debería normalizarse) |
| `div.F7nice > span:nth-child(2)` | `num_reviews` | |
| `a[data-item-id="authority"]` | `web` | URL oficial del negocio |
| `button[data-tooltip="Copiar el número de teléfono"]` | `telefono` | i18n-dependent; ya no funciona si Google cambia el tooltip a otro idioma |
| `div[data-review-id]` | reviews[] | El atributo data-review-id ES estable (no obfuscated) |

### Reviews — campos extraídos por card
- `div.d4r55` → autor
- `span.kvwXae[aria-label]` → estrellas (extrae primer dígito del aria-label)
- `span.wiu59c` → texto
- `span.rsqawe` → fecha relativa ("hace 2 meses", "1 año ago", etc.)
- `data-review-id` → ID único → `source_review_id = "gmaps_{rev_id}"`

Fecha relativa convertida a `DATE` defensivamente. Si el regex no encuentra cantidad → `fecha=NULL` (NO `date.today()` para evitar falsos "recientes").

## 🔧 Auditoría Mayo 2026

### Estado pre-auditoría
- **Playwright NO instalado** en el container scraper estándar → el `run()` nunca se ejecutó completo
- 11 source_records con `source_id = "gmaps_{spot_id}"` (fallback colisionante)
- 0 reviews descargadas en producción

### Bugs detectados y arreglados

| # | Bug | Impacto |
|---|---|---|
| 1 | **Playwright no instalado** | El scraper crasheaba con `ModuleNotFoundError` en cuanto se llamaba a `run()` |
| 2 | **`source_id = "gmaps_{spot_id}"` colisionante** | Si dos spots de geospots se asociaban erróneamente al mismo POI de Google, conflicto en `UNIQUE(source, source_id)` |
| 3 | **`fecha_db = date.today()` por default** cuando regex no parseaba la fecha relativa | Todas las reviews mal parseadas aparecían como "hoy" → falsos recientes en el ranking semántico |
| 4 | **Browser cleanup no garantizado** ante excepciones en medio del loop | Fugas de procesos chrome zombi |
| 5 | Sin guardia `PLAYWRIGHT_AVAILABLE` | Si alguien hace `--all` desde el container scraper normal, crash uncatchable. Ahora aborta limpio con mensaje claro |

### Fixes aplicados
1. **Servicio docker separado `gmaps`** (Dockerfile.gmaps + requirements-gmaps.txt + profile en compose)
2. **Import defensivo**: `try: from playwright... except ImportError`. El módulo se importa sin Playwright; el `run()` aborta con mensaje claro si falta.
3. **`source_id` único garantizado**: CID hex de la URL > FTID > SHA1 de URL completa. Nunca `gmaps_{spot_id}`.
4. **Fecha**: `fecha_db = None` cuando no se reconoce el formato relativo (soporta día/semana/mes/año en ES/EN).
5. **`try/finally` cleanup**: `context.close()` y `browser.close()` envueltos en try/except para liberar Chromium aunque haya excepciones.
6. **Args Chromium**: añadido `--disable-dev-shm-usage` (reduce signals de automation).

### Cleanup DB
- 11 source_records con `source_id` colisionante eliminados
- 11 spots: `google_maps` removido del array `fuentes[]`

### Validación
**No reproducible sin Playwright** instalado y running. Tests sintéticos no aplican porque la lógica clave es DOM scraping. Para validar:
1. Build del servicio: `docker-compose --profile gmaps build gmaps`
2. Ejecutar: `docker-compose --profile gmaps run --rm gmaps python scheduler.py --google_maps`
3. Verificar `stats.reviews_nuevas > 0` en logs
4. Si =0 tras varios runs, los selectores CSS están obsoletos (sección "Limitaciones")

---
**Estado Actual:** Aislado en servicio `gmaps` opt-in. Código hardened pero pendiente de verificación real con Playwright en producción. Considerado experimental hasta primer run exitoso con reviews descargadas.
