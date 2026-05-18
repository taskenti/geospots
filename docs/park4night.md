# 🏕️ Park4Night Scraper

## 📖 Información General
**park4night** (lanzada en 2011) es, indiscutiblemente, la aplicación reina del movimiento "vanlife". Con más de 6 millones de descargas, es la plataforma colaborativa líder en Europa y el mundo para la búsqueda de lugares de pernocta, áreas de descanso y espacios en plena naturaleza. Su fortaleza radica en el inmenso volumen de reseñas actualizadas casi en tiempo real por una comunidad hiperactiva, lo que la convierte en una herramienta imprescindible para viajes improvisados y "slow travel". 

## 🛠️ Arquitectura y Funcionamiento
El scraper `park4night.py` es uno de los motores más agresivos de la infraestructura de GeoSpots. Debido a la popularidad de la plataforma, sus medidas anti-scraping son superiores a la media. Para sortear estas defensas, hemos diseñado una arquitectura basada en **búsqueda por malla de puntos (Point-Grid Search)** y **aleatorización**.

1. **La API de Invitado (Guest API)**:
   - El script ataca directamente a la versión `V4.1` de la API de invitados (`guest.park4night.com`), lo que nos permite extraer datos sin necesidad de generar tokens de sesión por usuario.
2. **Generación de Malla y Aleatoriedad**:
   - En lugar de enviar un cuadro límite (Bounding Box) como otros scrapers, el script genera miles de coordenadas exactas (puntos `lat/lon`) barriendo toda Europa (desde Lat 35.0 hasta 71.5, con saltos de `0.25` grados).
   - Para evitar que los firewalls de Park4Night detecten un patrón de escaneo lineal, **se baraja aleatoriamente** el array de puntos (`random.shuffle(puntos)`). De este modo, la API recibe una petición desde Noruega, y la siguiente desde el sur de España.
3. **Resiliencia (Tenacity)**:
   - Implementa un sistema de reintentos con "Backoff Exponencial" mediante la librería `tenacity`. Si el servidor responde con un error HTTP `429 (Too Many Requests)`, el scraper entra en hibernación automática durante 60 segundos antes de reanudar el trabajo.
4. **Extracción de Reseñas**:
   - Tras descubrir un spot en el mapa, si detecta que tiene comentarios, lanza una segunda petición al endpoint `commGet.php` para descargar las reseñas completas (fechas, estrellas y texto) e insertarlas en nuestra base de datos relacional.

## 🧠 Lógica de Mapeo y Normalización
- **Códigos de Tipo**: Park4Night usa letras para definir lugares. Nuestro script traduce `"A"` a `area_ac`, `"P"` a `parking`, `"C"` a `camping` y `"N"` a `naturaleza`.
- **Dimensiones**: Extrae atributos físicos muy valorados por los usuarios como el límite de altura (`hauteur_limite`) para guardarlo en el campo `altura_max_m`.
- **Servicios**: Traduce los campos booleanos (`point_eau`, `electricite`, `eau_noire`) a las flags nativas de GeoSpots.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Bloqueo Definitivo de IP (IP Ban)**:
   - Al lanzar miles de peticiones `GET` contra los endpoints `lieuxGetFilter.php` y `commGet.php`, el riesgo de ser baneados a nivel de red por un WAF (Web Application Firewall) como Cloudflare es crítico. Aunque controlamos los errores 429, un escaneo intensivo desde una IP residencial/NAS puede acabar en lista negra permanente.
2. **Dependencia de una API Abierta (Guest API Volatility)**:
   - Estamos explotando un endpoint diseñado para usuarios no registrados. Si la empresa desarrolladora (AppMob) decide forzar la autenticación obligatoria para ver el mapa o cambia a una API cifrada, el scraper quedará inutilizado inmediatamente.
3. **Carga en la Base de Datos**:
   - Dado que P4N tiene cientos de miles de reseñas de alta calidad, la doble pasada del scraper (primero el lugar, luego descargar todos sus comentarios) genera un flujo masivo de transacciones `INSERT` en PostgreSQL.
4. **Fotos en Calidad "Thumb"**:
   - Aunque logramos capturar las fotos (`link_large` y `link_thumb`), el nivel de compresión que aplica P4N hace que, visualmente, no tengan el mismo estándar premium que las imágenes extraídas de Nomady o Campspace.

---
**Estado Actual:** Integrado y operativo. Considerado un "Scraper Pesado" que debe ejecutarse de forma esporádica o mediante proxies rotatorios para asegurar la supervivencia de la IP del servidor.
