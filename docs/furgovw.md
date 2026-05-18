# 🚐 Furgovw Scraper

## 📖 Información General
**Furgovw** es el foro español más legendario y veterano sobre el mundo de las furgonetas camper. Además de la comunidad, albergan el mapa de "Furgoperfectos", una base de datos mítica curada a mano por los moderadores del foro a lo largo de décadas. Aunque su tecnología es clásica (un foro SMF - Simple Machines Forum), la calidad, veracidad y exclusividad de sus puntos de pernocta en España y Europa la convierten en una fuente de oro para el proyecto GeoSpots.

## 🛠️ Arquitectura y Funcionamiento
El scraper `furgovw.py` es uno de los más complejos y sofisticados de GeoSpots, operando en **tres fases secuenciales** (API, RSS y Web Scraping) para reconstruir datos estructurados a partir de un foro antiguo.

1. **Fase 1: API Global y Parseo de Texto**:
   - Ataca a `api.php?getEverything=""`, descargando un JSON masivo.
   - **El Problema del Body**: El JSON no devuelve booleanos limpios, sino el texto crudo del mensaje del foro. El scraper incluye la función `_parsear_body` que lee línea por línea buscando patrones (ej. `agua: si`, `vaciado: no`) para transformarlos en campos nativos.
   - **Anomalía Geográfica**: La API de Furgovw tiene las **coordenadas invertidas** por un error histórico en su programación. Nuestro scraper corrige este fallo mapeando `lat = raw["lng"]` y `lon = raw["lat"]`.

2. **Fase 2: RSS y Comentarios (Reseñas)**:
   - Dado que la API no devuelve los comentarios, el scraper ataca los feeds RSS (`.xml`) de las 40 secciones geográficas del foro.
   - Lee los últimos mensajes y los asocia al spot correspondiente para crear nuestro array de reseñas. Incorpora un "fallback" de expresiones regulares (Regex) por si el XML del foro viene roto con caracteres inválidos.

3. **Fase 3: La Papelera y Cross-Reference Penalizer**:
   - **Board 88 (La Papelera)**: Furgovw mueve a una sección oculta los sitios que han sido prohibidos por los ayuntamientos, que han sido destruidos o donde multan. El scraper hace scraping puro de HTML de todas las páginas de esta papelera.
   - Cuando encuentra un sitio en la papelera, le inyecta la bandera: `⚠️ Lugar retirado de Furgoperfectos`.
   - **Inteligencia Geoespacial**: Mediante PostGIS, el scraper busca si en Park4Night u otras apps tenemos un punto a menos de 100 metros del punto prohibido de Furgovw. Si es así, mancha el punto de Park4Night con un aviso de peligro.

## 🧠 Lógica de Mapeo y Normalización
- **Tipología Fija**: Casi todos los Furgoperfectos se asumen por defecto como `naturaleza` (con `gratuito = True`), salvo que el texto indique lo contrario explícitamente.
- **Enlace de Autoridad**: Se reconstruye el enlace oficial al hilo del foro (`web = https://www.furgovw.org/foro/index.php?topic={topic_id}.0`).

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Parseo Textual Frágil**:
   - Como extraemos los servicios (agua, luz) leyendo el texto libre escrito por un forero hace años (`agua: si`), si el moderador no siguió la plantilla exacta (ej. puso `AguaPotable - Tenemos`), el script no lo reconocerá y dejará el servicio en nulo.
2. **Dependencia de la Estructura SMF**:
   - El escaneo de la papelera y el RSS depende íntegramente de la estructura de URLs de Simple Machines Forum (`board=88.20`, `action=.xml`). Si Furgovw migra su foro a otra tecnología (ej. XenForo, Discourse), las fases 2 y 3 del scraper se romperán inmediatamente.
3. **Fotos Restringidas**:
   - Furgovw protege sus galerías de imágenes. Solo extraemos la imagen de portada si está alojada externamente o si el enlace viene en claro. Muchas fotos alojadas en sus propios servidores requieren estar logueado y se pierden.

---
**Estado Actual:** Integrado, operativo y extremadamente robusto gracias a los sistemas de fallback XML y corrección de coordenadas. Su lógica de "Penalización Cruzada por Papelera" es pionera en el proyecto.
