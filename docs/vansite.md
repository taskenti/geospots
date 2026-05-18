# 🚐 Vansite Scraper

## 📖 Información General
**Vansite** es una plataforma orientada a conectar a viajeros en furgoneta o autocaravana con propietarios de terrenos privados, granjas y parcelas en la naturaleza. Sigue la misma filosofía de "marketplace privado" que Campspace o Nomady, pero con un fuerte enfoque técnico basado en tecnología de terceros: toda su infraestructura de reservas y bases de datos está delegada en **Sharetribe** (una popular plataforma "Software as a Service" para construir marketplaces al estilo Airbnb).

## 🛠️ Arquitectura y Funcionamiento
El scraper `vansite.py` presenta uno de los mayores desafíos de serialización y formato de todo el sistema GeoSpots. Al estar construido sobre la Flex API de Sharetribe, los datos no viajan en JSON estándar, sino en un dialecto ofuscado.

1. **Búsqueda Continental Paginada**:
   - Ataca directamente a la API genérica de Sharetribe: `https://flex-api.sharetribe.com/v1/api/listings/query`.
   - Se le inyecta un Bounding Box (`bounds`) gigante que cubre todo el continente europeo (`71.5,-25.0, 34.0,45.0`).
   - Se establece una paginación lineal masiva (`per_page: 100`, `page: 1, 2, 3...`), barriendo la totalidad de la plataforma mediante un bucle simple hasta que la respuesta viene vacía.
2. **Traducción al vuelo de Transit JSON**:
   - Sharetribe, al estar programado con el lenguaje Clojure en su backend, utiliza un formato de datos llamado **Transit JSON**. Este formato convierte los diccionarios en listas planas con metadatos extraños como `["^ ", "~:id", "~u62d91553-a967..."]` para ahorrar bytes en la red.
   - Para no añadir dependencias pesadas al contenedor Docker de GeoSpots, el scraper incluye un traductor en memoria (`transit_to_dict`) que, mediante recursividad, limpia y transforma este dialecto a un diccionario estándar de Python sobre la marcha.

## 🧠 Lógica de Mapeo y Normalización
- **Extracción de Identificadores**: Los UUIDs vienen prefijados por `~u`. El scraper los limpia automáticamente antes de usarlos como `source_id`.
- **Precios e Inferencia**: Evalúa el nodo `~:price`, que viene en forma de array `["~#mn", [1000, "EUR"]]`. Si el primer valor es `0`, fuerza el spot como gratuito; de lo contrario, se asume que es un alquiler privado de pago.
- **Categorización**: Si el atributo público `~:category` de Sharetribe es `campsite`, se etiqueta en GeoSpots como `camping`. El resto se catalogan como `naturaleza`.
- **URL Dinámica**: Como no podemos extraer la URL directa, construimos una estándar basada en su ID interno: `https://vansite.eu/l/{id}`.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Autenticación y Tokens Nativos (El Riesgo 401)**:
   - Sharetribe Flex API está diseñado para funcionar con *Client IDs* o *Bearer Tokens* anónimos. Si Vansite eleva sus políticas de seguridad en CORS o exige la validación estricta de un Token de Sesión en la cabecera `Authorization`, este scraper devolverá masivamente Errores HTTP 401/403 y se detendrá.
2. **Extracción de Fotos Desactivada**:
   - El protocolo Transit JSON extrae los datos relacionales (como las fotos) separándolos en un bloque `included` muy complejo, y referenciándolos por IDs abstractos. Para garantizar la velocidad y no romper el scraper con parseos complejos, la extracción de fotografías está temporalmente **omitida** (el array `fotos_urls` queda vacío).
3. **Pérdida de Filtros Específicos (Servicios)**:
   - Por esta misma complicación del formato Transit, no estamos extrayendo a bajo nivel los booleanos de servicios como electricidad, agua o duchas. Vansite actualmente ingresa en GeoSpots como un "Punto en el mapa con información y precio", pero ciego a nivel de *amenities* detalladas.

---
**Estado Actual:** Integrado y funcional mediante parseo nativo de Transit JSON. Requiere monitorización atenta a los códigos de respuesta (Riesgo Alto de 403 Forbidden).
