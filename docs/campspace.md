# 🏕️ Campspace Scraper

## 📖 Información General
**Campspace** es una popular plataforma europea (muy fuerte en Países Bajos y Bélgica) diseñada para facilitar el "microcamping" sostenible. Permite a particulares alquilar su jardín, terreno rural o pequeño bosque a viajeros en tienda de campaña, furgoneta camper o bicicleta. Al igual que Nomady y Vansite, es una plataforma de pernocta **privada y de pago**, destacando por su apuesta por el turismo lento ("slow travel") y la conexión local.

## 🛠️ Arquitectura y Funcionamiento
El scraper `campspace.py` utiliza un pipeline híbrido en dos fases diseñado para máxima eficiencia y exhaustividad:

1. **Fase 1: Descarga Global Ligera**:
   - Se ataca al endpoint `https://campspace.com/en/discover/campsites?_format=json`, el cual provee el listado completo (aproximadamente 4,000 spots) en una sola petición.
   - El script controla la paginación lineal y previene bucles infinitos comparando IDs en memoria (`seen_ids`). Al detectar que una página no aporta registros nuevos (porque el API devuelve el mismo conjunto repetido), el bucle de Fase 1 termina de forma segura.
   - Los spots se guardan inicialmente en la tabla `spots` (mapeados como `naturaleza` y `gratuito = False`) y se registran en `source_records`.

2. **Fase 2: Enriquecimiento Asíncrono y de Reviews**:
   - Tras obtener el listado global, se seleccionan los registros que no han sido enriquecidos (`details_fetched IS NULL`).
   - Se procesan en paralelo de forma asíncrona concurrente mediante una cola de tareas (`asyncio.Queue`) y un grupo de trabajadores concurrentes (`enrich_worker`).
   - **Enriquecimiento de Ficha**: Descarga el HTML de la página web del spot (`href`) y mediante `BeautifulSoup` extrae metadatos (número de plazas, fotos, descripción en inglés) y amenities específicas (agua potable, vaciado de grises/negras, electricidad, ducha, wifi, WC público, perros).
   - **Ingesta de Reviews (Opiniones)**: Si el spot tiene un identificador de espacio (`space_id`), se consulta el endpoint de AJAX `https://campspace.com/en/reviews/<spaceId>` para parsear las reviews (autor, rating de estrellas, fecha y texto), insertándolas en la tabla `reviews`. Se evita duplicación con un hash MD5 determinista único.

## 🧠 Lógica de Mapeo y Normalización
- **Tipología Forzada**: Todos los puntos entrantes se clasifican incondicionalmente como `naturaleza`.
- **Gratuidad Negada**: Se fuerza `gratuito = False` por defecto.
- **Deduplicación de Reviews**: Los IDs de las opiniones se construyen como `cs_<spaceId>_<hash_MD5_autor_fecha_texto>` para asegurar unicidad ante re-ejecuciones.
- **Limpieza de Datos de spots**: Se eliminan del diccionario del spot enviado a `enriquecer_spot` las claves que no tengan correspondencia exacta con las columnas de la tabla `spots` (como `host_name` y `space_id`), reteniéndolas únicamente en los metadatos de `source_records`.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)
1. **Consumo de Cuota de Conexiones**:
   - La Fase 2 realiza dos llamadas HTTP por spot (ficha de detalle + endpoint de opiniones). Se aplica un retraso controlado (`rate_limit`) y concurrencia moderada para evitar bloqueos por Cloudflare u otros mecanismos de protección del servidor.

---
**Estado Actual:** Auditado y actualizado. La Fase 2 está completamente integrada, permitiendo almacenar opiniones de usuarios, fotos y amenities completas de forma robusta e integrada en la base de datos PostgreSQL.
