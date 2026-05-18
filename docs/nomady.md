# 🏔️ Nomady Scraper

## 📖 Información General
**Nomady** es una plataforma de origen suizo diseñada para acercar el campismo a la naturaleza de forma sostenible y respetuosa. Con un enfoque muy cuidado en la estética y el respeto medioambiental, Nomady conecta a propietarios de terrenos rurales, bosques, viñedos y granjas con viajeros que buscan lugares de pernocta exclusivos, alejados de las aglomeraciones de los campings tradicionales. Se ha convertido en una plataforma de referencia (junto con Campspace) para el "campismo privado" en Centroeuropa (Suiza, Alemania, Austria, etc.).

## 🛠️ Arquitectura y Funcionamiento
El scraper `nomady.py` es, a nivel de eficiencia, la **joya de la corona** del sistema GeoSpots. Durante el análisis de su red (Ingeniería Inversa), descubrimos que su frontend de mapas utiliza una estrategia de red muy agresiva pero tremendamente beneficiosa para nosotros.

1. **El Santo Grial (Volcado Global)**:
   - En lugar de obligar al cliente web a pedir datos por trozos cada vez que se mueve el mapa, Nomady emite una única petición `GET` a un endpoint secreto: `https://api.nomady.camp/cabin/public-compressed-v2`.
   - Este endpoint devuelve **la base de datos completa y absoluta de Nomady en el mundo**, en un solo archivo JSON comprimido.
2. **Sin Paginación ni Cuadrículas**:
   - Gracias a este descubrimiento, el scraper de Nomady no necesita lidiar con recursividad, ni Bounding Boxes, ni parámetros de paginación (`offset` o `page`). Una única petición HTTP que dura menos de 2 segundos descarga miles de parcelas privadas, con todas sus propiedades anidadas.

## 🧠 Lógica de Mapeo y Normalización
El JSON de Nomady es extremadamente rico y limpio. La normalización es muy directa:
- **Tipología Avanzada**: Nomady clasifica sus parcelas según el tipo de pernocta. El scraper examina el array `types`:
  - Si incluye `hut` (cabañas), se etiqueta como `camping`.
  - Si incluye `caravan`, `medium_vehicle` o `large_vehicle`, se etiqueta como `area_ac` (apto para autocaravanas).
  - Si es solo para tiendas (`tent`), se etiqueta como `naturaleza`.
- **Servicios Limpios**: Las banderas booleanas son literales y se mapean uno a uno: `drinkingWater` (agua potable), `power` (electricidad), `regularToilet` (wc), `regularShower` (ducha), `blackWater` / `greyWater` (vaciados).
- **Gratuidad Inexistente**: Al ser un marketplace puro de alquiler privado, se fuerza `gratuito = False` en absolutamente todos los registros.
- **URL Canónica**: Se reconstruye el enlace de reserva combinando la base de su web con el campo `slug` del spot (`https://nomady.camp/en/c/{slug}`).

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Cierre del Endpoint Global (API Hardening)**:
   - Este es el riesgo principal. Entregar la base de datos entera en una sola petición `/public-compressed-v2` es una práctica de red que muchas empresas acaban abandonando cuando su volumen de datos crece demasiado (por impacto en sus servidores y riesgo de scraping corporativo). Si Nomady decide pasar a un sistema de celdas geográficas Vector Tiles (como hace Mapbox), este scraper quedará obsoleto y requerirá una reescritura total.
2. **Ausencia de Reviews Extensas**:
   - Este volcado masivo inicial contiene el cálculo de la nota media y algunos datos básicos, pero no incluye el texto de las reseñas de los usuarios. Extraer las reseñas requeriría peticiones individuales por spot, lo cual sacrificaría la velocidad extrema de este scraper.
3. **Limitación de Fotografías**:
   - Para no sobrecargar nuestra base de datos ni ralentizar la respuesta del frontend, aunque Nomady envíe arrays con hasta 20 imágenes por parcela, el scraper está configurado para almacenar estrictamente un máximo de 5 URLs de su CDN.

---
**Estado Actual:** Integrado, operativo y optimizado al extremo. Es el scraper más rápido de todo el ecosistema (1 Petición HTTP = 100% de la Base de Datos).
