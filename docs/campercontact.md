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

## 🧠 Lógica de Mapeo y Normalización
- **Tipología (`poiType`)**: Traduce `camperplace`, `motorhome` y `service` a `area_ac`; `nature` o `wild` a `naturaleza`.
- **Detección de Gratuidad**: Evalúa el nodo `priceRange`. Si `min == 0`, se marca como gratuito (`gratuito = True`), permitiendo que nuestro mapa lo etiquete como área libre de pago.
- **Enriquecimiento Básico (Fase 1)**: Extrae de forma limpia el `rating_promedio` y `num_reviews` del nodo de filtros de la API, además de generar la URL canónica (`permalink`) para que el usuario pueda visitar la ficha original.
- **Enriquecimiento Profundo y Reviews (Fase 2)**: Para cada spot ingestado en el grid, se descarga asíncronamente su página web y se parsea el payload de datos JSON incrustado por Next.js (`self.__next_f.push`), extrayendo el listado detallado de servicios (amenities), fotos en alta resolución, descripciones multilingües, datos de contacto (teléfono, email, web) y opiniones de los usuarios, que se guardan en la tabla `reviews`.

## 🛠️ Mejoras y Solución de Carencias (Mayo 2026)

1. **Eliminación del Sesgo Estacional (Bypass de Disponibilidad)**:
   - Se removieron los parámetros de fecha (`fromDate`, `toDate`), plazas (`persons`, `babies`) y mascotas (`pets`) en las consultas al grid. Esto obliga a la API a devolver todos los spots registrados, independientemente de si están abiertos, cerrados por temporada o completos.
2. **Implementación de Fase 2 (Detalles y Opiniones asíncronos)**:
   - Se desarrolló un pipeline asíncrono concurrente con un pool de trabajadores (`enrich_worker`) que consume spots sin detalles marcados (`details_fetched IS NULL`), descarga la ficha web, y parsea los payloads Next.js.
   - Extrae amenities (agua potable, vaciado de negras/grises, electricidad, ducha, wifi, wc público y admisión de perros), seguridad, iluminación, capacidad de plazas y descripciones completas en español, inglés, francés, alemán, italiano y holandés.
   - Popula de forma masiva y eficiente la tabla `reviews` con valoraciones individuales, textos originales e idiomas del autor, previniendo duplicados (`ON CONFLICT DO NOTHING`).
3. **Optimización de Inserción y Prevención de Sobrescritura**:
   - `enriquecer_spot` en la capa de datos actualiza únicamente campos que sean `NULL`, asegurando que CamperContact complemente sin sobrescribir fuentes primarias de mayor credibilidad como Park4Night.

---
**Estado Actual:** Totalmente optimizado, sin sesgos de disponibilidad e ingestando detalles complejos y reviews en segundo plano de manera 100% automatizada.
