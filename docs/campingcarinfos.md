# 📍 Campingcar-Infos Scraper

## 📖 Información General
**Campingcar-Infos** es una web francesa veterana especializada en áreas de servicio para autocaravanas en Europa. Su valor diferencial es la **cobertura geográfica**: mantiene un catálogo de ~24.000 POIs en 43 países, mucho más extenso que el de otras fuentes francesas como CaraMaps. La comunidad valida los puntos con frecuencia y publica un dump POI actualizado en formato propietario para GPS antiguos (TomTom OV2, ASCII).

A cambio de esa amplitud, la profundidad de cada ficha es mínima: solo coordenadas, categoría y localidad. Sin servicios, sin precios, sin fotos, sin reviews (las reviews existen en la web pero no en el bulk download). Por eso encaja como **fuente complementaria de cobertura** — útil para descubrir spots que Park4Night o CamperContact no tienen, no como fuente de datos enriquecidos.

## 🛠️ Arquitectura y Funcionamiento
La estrategia es la más simple del proyecto: un solo request HTTP a un endpoint que devuelve un ZIP con todos los POIs del mundo.

- **Endpoint de Descarga Global**:
  `https://www.campingcar-infos.com/Francais/creepoigpstotal.php`
- **Operación**:
  - GET único sin parámetros. Devuelve un ZIP de ~1.1 MB.
  - Dentro hay 9 archivos `.asc` (uno por categoría más un `ATOTALES_CCI.asc` combinado) y un PDF de instalación irrelevante.
  - Se extrae `ATOTALES_CCI.asc` y se parsea línea por línea con regex.
  - No usa grid, no necesita semáforo, no requiere reintentos. Completa en ~100 segundos sobre 24K POIs.

## 🧠 Lógica de Mapeo y Normalización
Cada línea del ASCII tiene formato:
```
LON,LAT,"<CATEGORIA> <PAIS_FR> <LOCALIDAD>  [(<CP>)]  Aire CCI <ID>"
```

Ejemplo: `1.48905,42.5535,"AC ANDORRE LA MASSANA  (AD400 ) Aire CCI 33603"`

- **Categorías CCI → tipo GeoSpots** (`CATEGORY_MAP` en `campingcarinfos.py`):
  - `AC` (Aire Communale) → `area_ac`, marcada como gratis por defecto
  - `APCC` (Aire Payante Camping Car) → `area_ac`, marcada como NO gratis
  - `AS` (Aire de Service) → `area_ac`
  - `AA` (Aire d'Accueil) → `area_ac`
  - `ACF` (Aire Camping Ferme), `ACS` (Aire Camping Site) → `camping`
  - `APN` (Aire Privée Nuit) → `parking_privado`, no gratis
  - `ASN` (Aire Stationnement Nuit) → `parking_publico`, gratis
- **Países**:
  - El segundo token de la descripción es el país en francés y mayúsculas (`ESPAGNE`, `ALLEMAGNE`, `MAROC`...). Se mapea contra `COUNTRY_ISO` dict a códigos ISO2. Cobertura: 43 países europeos + Marruecos, Túnez, etc. Los ~190 POIs con país sin mapear caen en el trigger geográfico de PostGIS de igual modo.
- **Nombre canónico**:
  - Se concatena `Localidad (CP)` con `.title()` para legibilidad. Si no hay localidad, se usa `CCI <ID>`.
- **Web**:
  - Cada POI tiene una URL pública en `cherchgps.php?cci=<ID>` que se guarda en `spots.web`.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)
1. **Datos minimalistas**:
   - No hay servicios (agua, electricidad, ducha), ni precios reales, ni fotos, ni reviews, ni descripciones. La fuente solo aporta existencia + categoría + coordenadas. Por eso `base_score = 0.78` y `review_quality = 0.50` en `source_credibility` — útil para presencia pero no para reconciliación de campos ricos.
2. **Endpoint legacy sin contrato**:
   - El endpoint `creepoigpstotal.php` está pensado para usuarios que cargan POIs en TomTom/Garmin. No tiene versión, ni documentación, ni Content-Type apropiado (devuelve un ZIP con `Content-Type: text/plain`). Si la web migra a un sistema más moderno o limita a usuarios registrados, el scraper romperá sin aviso.
3. **Reviews descartadas en bulk**:
   - La web SÍ tiene comentarios de campistas, pero solo accesibles vía HTML por cada CCI ID individual (24K páginas). Implementar `download_reviews()` requeriría ~7h de scraping a 1 req/s. Pendiente como mejora futura — ver `docs/RECOMMENDATIONS.md` #1.
4. **Categorización rudimentaria**:
   - Las heurísticas de gratis/no-gratis por categoría (`AC` → gratis, `APN` → no gratis) son aproximaciones. Spots concretos pueden contradecirlo. La reconciliación posterior con fuentes más fiables corrige estos casos.

## 📊 Métricas de la Primera Carga (2026-05-25)
- 24.132 POIs parseados del ZIP (0 errores de parseo tras los fixes iniciales)
- 3.972 spots nuevos creados
- 20.160 deduplicados con spots existentes (**83% de cross-coverage** con otras fuentes)
- Tiempo total: 103 segundos
- Distribución por tipo: 12.455 `area_ac`, 7.404 `parking` (legacy), 3.256 `camping`, 457 `parking_publico`, 287 `parking_privado`, resto naturaleza/wild

---
**Estado Actual:** Integrado y operativo. Frecuencia recomendada: mensual (el dump se actualiza con cada nueva contribución de la comunidad).
