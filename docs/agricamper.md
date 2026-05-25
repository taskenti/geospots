# 🚜 Agricamper Italia Scraper

## 📖 Información General
**Agricamper** es la red de hospedaje rural y agroturismo exclusiva de Italia, equivalente a redes como France Passion o España Discovery. Consiste en una comunidad de granjas, viñedos, queserías, bodegas, apiarios y picaderos que ofrecen pernoctación legal y gratuita durante un máximo de 24 horas a los autocaravanistas que forman parte de la asociación (mediante la compra de una tarjeta digital/suscripción anual).

Es una fuente premium muy codiciada debido a que los spots están 100% verificados, son seguros y ofrecen una experiencia inmersiva en la Italia rural.

## 🛠️ Arquitectura y Funcionamiento
A diferencia de los scrapers que requieren navegación por grid geográfico o scraping de DOM complejo, Agricamper cuenta con un endpoint REST directo y optimizado de tipo "bulk" que expone la base de datos completa de spots del mapa interactivo:

- **Endpoint de Descarga Masiva (fiches)**:
  `https://www.agricamper.com/wp-json/interactive-map/v1/fiches`
- **Operación**:
  - Se ejecuta en una única petición GET que devuelve un array JSON con los 600+ hosts registrados.
  - La respuesta pesa ~6.7 MB y contiene metadatos de geolocalización, información de contacto, traducción integrada de descripciones, fotos y amenities.
  - Al procesar todo en memoria, no requiere peticiones de grid y la carga en el servidor de destino es mínima.

## 🧠 Lógica de Mapeo y Normalización
- **Tipología**:
  - Por defecto, todos los hosts se catalogan como `"parking_privado"` (estancia privada regulada por anfitriones).
  - Si en `fiche_typologie_label` se encuentra la tipología `"Agricamping"`, se mapea como `"camping"`.
- **Condición de Gratuidad**:
  - Se fuerza `gratuito = False` y se inyecta en `precio_info` el texto *"Agricamper membership required (annual subscription)"*. `precio_aprox` queda `NULL` porque la membresía anual fija no se traduce en un precio por noche; poner 0.0 era contradictorio con "requiere pago".
- **Nombre canónico**:
  - Se usa `nom_societe` directamente (e.g. "Cantina Fosso degli Angeli"), sin prefijo "Agricamper -" — la fuente ya queda registrada en `spots.fuentes[]` y el prefijo solo añadía ruido al mapa y al ranking de búsqueda.
- **Región**:
  - El API devuelve provincias en formato "Benevento (BN)". Se extrae solo el nombre limpio antes de los paréntesis.
- **Idiomas y Traduccón**:
  - El API proporciona traducciones pre-compiladas en su propiedad `fiche_traduction`.
  - Mapeamos directamente los diccionarios a las columnas de base de datos correspondientes: `descripcion_it` (`it_IT`), `descripcion_en` (`en_EN`), `descripcion_fr` (`fr_FR`), `descripcion_de` (`de_DE`), y `descripcion_nl` (`nl_NL`).
- **Amenities / Servicios**:
  - Se evalúa la lista `fiche_service_label` para asociar los booleanos del esquema:
    - `"Water"` ➔ `agua_potable`
    - `"Electrical connection"` ➔ `electricidad`
    - `"Showers"` ➔ `ducha`
    - `"WC"` ➔ `wc_publico`
    - `"WC drain"` ➔ `vaciado_negras`
    - `"Water drain"` ➔ `vaciado_grises`
    - `"Wi-Fi"` ➔ `wifi`
    - `"Dogs accepted (kept on a leash)"` / `"Dogs accepted"` ➔ `perros`
    - `"Illuminated"` ➔ `iluminacion`
- **Fotos**:
  - Se recorre la estructura de `fiche_photo` buscando las URLs optimizadas (`optimisee`) para `main_photo`, `external_photo`, `interior_photo`, etc.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)
1. **Endpoint Público en WordPress**:
  - El endpoint `/wp-json/interactive-map/v1/fiches` no requiere autenticación API actualmente. Si en el futuro Agricamper decide asegurar este endpoint detrás de un middleware JWT de WordPress o un nonce dinámico, la descarga bulk fallará.
2. **Ausencia de Reseñas**:
  - La plataforma no almacena ni expone comentarios en el mapa público. Por tanto, `download_reviews` no procesa ninguna reseña, limitando la fuente a enriquecimiento de spots e información técnica.

---
**Estado Actual:** Integrado y operativo. Se ejecuta en un solo lote con una frecuencia baja recomendada (e.g. semanal).
