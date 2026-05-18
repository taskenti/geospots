# 🍷 Portugal EasyCamp Scraper

## 📖 Información General
**Portugal EasyCamp** es una red "boutique" especializada en agroturismo (viñedos, granjas, fincas agrícolas y turismo rural) exclusiva de Portugal. El modelo de negocio es único: la pernocta para autocaravanas es gratuita (máximo 24h) a condición de adquirir un "pack de bienvenida" (vino, aceite de oliva, miel u otros productos artesanales) directamente de los agricultores. Es una plataforma pequeña pero de altísimo valor añadido para turistas que buscan experiencias gastronómicas y locales huyendo de las áreas masificadas.

## 🛠️ Arquitectura y Funcionamiento
A diferencia de plataformas como CaraMaps o Nomady que cuentan con APIs públicas, Portugal EasyCamp es un sitio construido sobre WordPress con la plantilla comercial "CityTours". Los datos del mapa no se cargan por una API REST, sino que se incrustan (hardcodean) en el HTML de la página. Por tanto, el scraper `portugaleasycamp.py` utiliza una técnica de **Auditoría de Sitemap + Extracción Regex DOM**.

1. **La Puerta Trasera (Sitemap)**:
   - Dado que el mapa principal ofusca los datos con scripts de optimización (Autoptimize), el scraper lee directamente el archivo `https://portugaleasycamp.com/tour-sitemap.xml`.
   - Extrae el catálogo completo de URLs de las fincas (aproximadamente 60-80 enlaces únicos).
2. **Scraping de Nivel 2 (Extracción de HTML)**:
   - El script visita cada página individual secuencialmente.
   - En lugar de usar pesadas librerías de parseo HTML, utiliza Expresiones Regulares (Regex) muy eficientes para buscar el objeto Javascript `markersData` inyectado en el HTML.
   - Extrae directamente `location_latitude`, `location_longitude`, `name`, y `map_image_url`.
3. **Caza del Precio (Coste del Pack)**:
   - Rastrea en el HTML la tabla de reserva para extraer el precio estimado del producto de la granja (buscando la clase `<td class="text-right total-cost">`).

## 🧠 Lógica de Mapeo y Normalización
- **Tipología**: Todas las fincas se marcan como `naturaleza` (agroturismo).
- **Condición de Gratuidad**: Se marca `gratuito = False` de manera forzada. Aunque el aparcamiento es "técnicamente" gratis, requiere obligatoriamente una compra previa (transacción económica).
- **Inyección de Descripción**: El scraper no extrae la larga descripción de la web, sino que genera una propia altamente estandarizada: *"Finca agroturística de Portugal EasyCamp. Pernocta legal (24h) garantizada mediante la compra de su pack de bienvenida. Coste aprox. del pack: XX€."*

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Dependencia Total de la Plantilla (Theme Vulnerability)**:
   - Este es el riesgo principal. El scraper busca variables muy específicas de JavaScript (ej. `location_latitude:`). Si Portugal EasyCamp decide cambiar su plantilla de WordPress (CityTours) a una diferente, o instalan un nuevo plugin de mapas, el Regex fallará silenciosamente y no se extraerán coordenadas.
2. **Fragilidad del DOM (HTML Changes)**:
   - El precio se extrae apuntando a la clase `class="text-right total-cost"`. Un simple rediseño CSS de la web (cambio del nombre de la clase) provocará que las áreas se listen sin precio.
3. **Falsos Positivos en la Tabla de Precios**:
   - Si la página HTML añade otras tablas con la misma clase en futuras actualizaciones, el Regex podría capturar un precio incorrecto.

---
**Estado Actual:** Integrado y operativo. Realiza un scraping suave (Rate Limit: 2s) para no afectar al rendimiento de este pequeño servidor WordPress.
