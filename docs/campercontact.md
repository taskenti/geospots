# 🚐 Campercontact Scraper

## 📖 Información General
**Campercontact** es una aplicación móvil líder en Europa, desarrollada por la NKC (Nederlandse Kampeerauto Club). Cuenta con una base de datos de más de 60.000 ubicaciones en 58 países, incluyendo áreas de autocaravanas, campings y aparcamientos. Es conocida por la altísima fiabilidad de sus datos, precios actualizados y un volumen masivo de reseñas (más de 1 millón de usuarios activos). En su versión PRO+, ofrece navegación avanzada y filtros detallados.

## 🛠️ Arquitectura y Funcionamiento
El scraper `campercontact.py` se basa en realizar ingeniería inversa a la API interna utilizada por el mapa interactivo de su plataforma web. A diferencia de iOverlander, este scraper funciona "en vivo" escaneando el mapa de Europa mediante una **cuadrícula dinámica (Grid Subdivision)**.

1. **Endpoint Principal**: Ataca a `https://services.campercontact.com/search/results/list`.
2. **Sistema de Cuadrícula Recursiva (Grid)**:
   - El scraper de base (heredado de `AbstractSource`) le pasa coordenadas de un cuadro (Bounding Box).
   - Si la API responde indicando que hay **más de 50 resultados** (`total > 50`) en ese cuadro, el scraper automáticamente **subdivide la celda en 4 cuadrantes más pequeños** y vuelve a lanzar las peticiones recursivamente, hasta lograr cuadros que devuelvan 50 resultados o menos (o hasta llegar a una resolución mínima de 0.1 grados).
3. **Manejo de Cabeceras (Fingerprinting)**:
   - Utiliza una cabecera oculta específica (`x-feature-flags: microcamping`) que exige su backend moderno para devolver la lista completa.
4. **Fechas Simuladas**:
   - Para engañar al motor de reservas de la plataforma, el scraper inyecta dinámicamente el día de hoy (`fromDate`) y el de mañana (`toDate`) simulando una búsqueda de disponibilidad para 2 personas y 0 mascotas.

## 🧠 Lógica de Mapeo y Normalización
- **Tipología (`poiType`)**: Traduce `camperplace`, `motorhome` y `service` a `area_ac`; `nature` o `wild` a `naturaleza`.
- **Detección de Gratuidad**: Evalúa el nodo `priceRange`. Si `min == 0`, se marca como gratuito (`gratuito = True`), permitiendo que nuestro mapa lo etiquete como área libre de pago.
- **Enriquecimiento**: Extrae de forma limpia el `rating_promedio` y `num_reviews` del nodo de filtros de la API, además de generar la URL canónica (`permalink`) para que el usuario pueda visitar la ficha original.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **El Riesgo de las Fechas Simuladas (Disponibilidad / Estacionalidad)**:
   - **Peligro Crítico**: Al inyectar fechas (hoy y mañana) en la petición, la API podría ocultar campings o áreas que estén **cerradas por temporada** (ej. en invierno) o que estén **completamente llenas** para esa noche. Esto significa que un barrido en enero extraerá un mapa diferente al de un barrido en julio.
2. **Explosión Recursiva del Grid**:
   - En zonas de altísima densidad de campings (ej. la costa de Francia o Países Bajos), la recursividad del mapa puede generar cientos de peticiones en cuestión de segundos, lo que expone a la IP del NAS a ser bloqueada (Rate Limiting o Ban) por los servidores de Campercontact.
3. **Carencia de Fotos y Atributos Específicos**:
   - El endpoint del mapa (`search/results/list`) devuelve datos resumidos (título, precio, rating, tipo). No incluye la lista de servicios (agua, vaciado, electricidad) ni el array de fotografías en alta calidad. Obtener esta información obligaría a hacer una segunda petición `GET` por cada uno de los 60.000 puntos, lo cual es inviable por tiempo y riesgo de bloqueo.
4. **Cambio de API (API Volatility)**:
   - Dado que Campercontact renovó su app a finales de 2025, si alteran su estructura de rutas, su validación de CORS o eliminan el `x-feature-flags`, el scraper devolverá inmediatamente errores HTTP 400 o 403.

---
**Estado Actual:** Integrado y operativo mediante búsqueda recursiva de cuadrículas, requiriendo monitorización para no disparar bloqueos de IP.
