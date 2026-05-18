# 🏕️ Campspace Scraper

## 📖 Información General
**Campspace** es una popular plataforma europea (muy fuerte en Países Bajos y Bélgica) diseñada para facilitar el "microcamping" sostenible. Permite a particulares alquilar su jardín, terreno rural o pequeño bosque a viajeros en tienda de campaña, furgoneta camper o bicicleta. Al igual que Nomady y Vansite, es una plataforma de pernocta **privada y de pago**, destacando por su apuesta por el turismo lento ("slow travel") y la conexión local.

## 🛠️ Arquitectura y Funcionamiento
El scraper `campspace.py` utiliza una técnica de ingesta directa basada en la **extracción de la capa de marcadores del mapa**, lo que lo convierte en un scraper sumamente ligero y rápido, pero con limitaciones en la profundidad de los datos.

1. **Endpoint de Capa de Mapa**:
   - Se ataca al endpoint `https://campspace.com/en/discover/campsites?_format=json`, el cual está diseñado exclusivamente para alimentar el mapa interactivo de su frontend.
2. **Paginación Lineal Clásica**:
   - En lugar de usar cuadrículas complejas, el script itera secuencialmente usando el parámetro `&page=0, 1, 2...`
   - Incorpora un mecanismo de autodefensa contra bucles infinitos: guarda un registro de los IDs procesados en la memoria (`seen_ids`). Si detecta que una página devuelve datos pero ninguno es nuevo (es decir, la API ha empezado a repetir los mismos resultados en bucle en lugar de dar un error 404), el scraper corta la iteración de forma segura.

## 🧠 Lógica de Mapeo y Normalización
Debido a la naturaleza del endpoint elegido (mapa rápido), la normalización es muy restrictiva y basada en inferencias absolutas:
- **Tipología Forzada**: Todos los puntos entrantes se clasifican incondicionalmente como `naturaleza`, ya que el propósito de Campspace es acampar fuera de campings comerciales masivos.
- **Gratuidad Negada**: Se fuerza `gratuito = False` por defecto. Es un marketplace de transacciones económicas.
- **Enrutamiento Directo**: En lugar de reconstruir URLs artificialmente, la API afortunadamente nos provee el campo `href` directo a la ficha de la parcela.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Escasez Extrema de Metadatos (Ceguera de Servicios)**:
   - Este es el mayor defecto de este scraper. Al consumir el JSON rápido del mapa, **no obtenemos ni fotografías ni listado de servicios** (agua, electricidad, duchas, si aceptan perros, etc.). En GeoSpots, las parcelas de Campspace aparecerán como puntos ciegos (solo sabremos su ubicación y su enlace de reserva).
2. **Ignorancia del Tipo de Vehículo**:
   - Campspace aloja desde terrenos inmensos para autocaravanas hasta pequeños patios traseros donde solo cabe una tienda de campaña pequeña. Al no extraer las características completas de la ficha, podríamos estar mostrando puntos en el mapa a viajeros en autocaravana en los que físicamente no caben.
3. **Paginación Rota (Drupal/Symfony Quirks)**:
   - El parámetro `_format=json` es un clásico de frameworks como Drupal. A veces, estos endpoints no soportan paginación real para consultas sin filtros y devuelven simplemente los primeros "N" resultados destacados. Si esto ocurre, el scraper solo estará extrayendo una muestra muy pequeña del total de parcelas existentes.

---
**Estado Actual:** Integrado y operativo. Cumple su función como inyector rápido de marcadores, pero sería el candidato principal para una futura reescritura que acceda a la ficha detallada de cada parcela (Scraping de Nivel 2).
