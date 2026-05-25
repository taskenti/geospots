# 🚐 Camperstop Scraper

**Camperstop** es una de las plataformas y aplicaciones más consolidadas en Europa para los usuarios de autocaravanas. Desarrollada originalmente por la editorial holandesa Facile Media, cuenta con un catálogo verificado de más de 15,000 áreas de servicio y pernocta en más de 30 países europeos, siendo especialmente fuerte en Países Bajos, Alemania, Bélgica y Francia.

El scraper `camperstop.py` explota su API técnica móvil oculta para realizar una descarga masiva de alta eficiencia.

## 🛠️ Descubrimientos de Ingeniería Inversa

Durante el análisis del tráfico de red de su aplicación móvil (basada en Flutter/Dart), identificamos las siguientes características de su API:

### 1. Requisito de Idioma/Cultura
La API responderá con un error `HTTP 500 (No language specified)` a menos que se envíe una cabecera indicando la cultura regional requerida:
```http
culturecode: en-GB
```

### 2. Formato de Coordenadas de Búsqueda
El endpoint principal de camperstops requiere que la posición geográfica del cliente sea enviada en el cuerpo de la petición `POST` en un formato de cadena serializado con la clave `latLng`:
```json
{
  "latLng": "41.17129,-2.4313"
}
```

### 3. Sincronización Masiva en 1 Petición
Si se invoca el endpoint de obtención de camperstops `/getcamperstops` enviando una coordenada de búsqueda válida mediante `latLng`, pero **no** se restringe el parámetro `"distance"`, el servidor responde devolviendo la **base de datos completa a nivel mundial (más de 13,400 registros) en un solo array JSON**.
Esto permite realizar una sincronización inicial y periódica ultraeficiente sin necesidad de escanear por cuadrícula geográfica ni sufrir bloqueos por exceso de peticiones.

## 🗂️ Mapeo y Normalización

### Tipos de Spots
Camperstop define 15 tipos de localizaciones en su base de datos. Se mapean de la siguiente forma a las categorías canónicas de GeoSpots:
*   `Motorhome stopover` (1), `Motorhome park` (3), `Outside campsite` (4), `At farm/vineyard` (6), `At restaurant` (7), `Overnight stay at company/enterprise` (8), `At spa` (9), `Motorhome service` (12), `At harbour/marina` (13), `Overnight stay in private area` (15) ➔ **`area_ac`**
*   `Campsite` (5), `Camperstop on campsite` (14) ➔ **`camping`**
*   `Tolerated place` (2) ➔ **`wild`**
*   `At zoo/museum/amusement parc` (10), `Parking only` (11) ➔ **`parking`**

### Puntuaciones y Comentarios
*   **Ratings:** El campo `averageScore` almacena la valoración en una escala de 0 a 10. Se divide por `2.0` para almacenarse en la columna `master_rating` en escala de 5 estrellas.
*   **Reviews:** El endpoint `/getreviews/{id}/en-GB` devuelve las valoraciones del spot. En este caso, el servidor responde con un código de éxito no estándar **`HTTP 256`**, el cual se procesa adecuadamente como exitoso en el cliente.

## 🚦 Concurrencia y Rate Limiting
La API expone en sus cabeceras un límite de tasa estricto (`X-RateLimit-Limit: 60` y `X-RateLimit-Remaining`). Para cumplir con el límite de 60 peticiones por minuto, el pipeline de descarga de reviews desacoplado utiliza un retardo mínimo de **1.2 segundos** por petición asíncrona.
