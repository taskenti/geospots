# 🚙 iOverlander Scraper

## 📖 Información General
**iOverlander** (lanzada en 2014) es una de las bases de datos comunitarias más grandes a nivel mundial para viajeros *overland*, aventureros y campistas. Con más de 250.000 ubicaciones en 190 países, destaca especialmente por su volumen de sitios de acampada libre (*wild camping*), servicios remotos (mecánicos, fronteras, agua) y su funcionamiento puramente colaborativo. 

Tras años de ser un proyecto mantenido por voluntarios, en 2024 se lanzó **iOverlander 2** (actualmente en v2.6.x), reconstruida desde cero y respaldada por un pequeño equipo remunerado. La aplicación original ("Legacy") tiene programado su cierre técnico en abril de 2025. A pesar de incorporar funciones Pro y suscripciones para mapas satelitales offline, el núcleo de su inmensa base de datos sigue siendo el pilar de acceso libre para la comunidad nómada mundial.

## 🛠️ Arquitectura y Funcionamiento
A nivel técnico, este scraper difiere significativamente del resto del ecosistema GeoSpots. En lugar de atacar una API REST o un endpoint GraphQL en vivo, el módulo `IOverlanderSource` funciona como un **proceso de ingesta offline**.

1. **Origen de los datos**: Los datos originales provienen de volcados oficiales de iOverlander en formato propietario de Garmin (`.gpi` de ~170MB). 
2. **Transformación previa**: Este archivo requiere una conversión previa (usualmente con herramientas como `GPSBabel`) para transformarlo a un formato manejable como `.kmz` (KML comprimido).
3. **Ingesta (El Scraper)**: 
   - El script `ioverlander.py` busca el archivo montado en el contenedor en la ruta `/data/ioverlander.kmz`.
   - Descomprime el archivo al vuelo usando `zipfile`.
   - Parsea el árbol XML interno (`.kml`) utilizando `xml.etree.ElementTree`.
   - Aplica un filtro de "Bounding Box" europeo (Lat: 35.0 a 72.0, Lon: -25.0 a 45.0) para descartar decenas de miles de puntos de otros continentes y optimizar la inserción en nuestra base de datos.
   - Aplica un radio de deduplicación de `100.0` metros mediante PostGIS (`ST_DWithin`).

## 🧠 Lógica de Mapeo y Normalización
Dado que el formato KML exportado es fundamentalmente texto plano y coordenadas, el scraper implementa una lógica de inferencia heurística:
- **Tipología (`_inferir_tipo`)**: Determina si el punto es `naturaleza`, `camping`, `area_ac` o `parking` buscando subcadenas específicas en el nombre del lugar (ej. "wild camping", "campsite", "dump station").
- **Atributos (`_inferir_bool`)**: Los servicios (`agua_potable`, `electricidad`, `wifi`, `ducha`, `wc_publico`, `gratuito`) se extraen escaneando la descripción del punto con diccionarios de palabras clave multilingües (`KW_AGUA = ["water", "agua", "wasser", "eau", "acqua", "potable"]`).

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Falsos Positivos en Atributos (Regex/Keyword Parsing)**:
   - Al buscar palabras clave en texto plano, descripciones como *"No hay agua"* o *"Without water"* activarán el *flag* de `agua_potable=True` porque el script solo detecta la presencia de la palabra "agua" o "water". Esto requiere una futura implementación de PLN o Regex negativo.
2. **Obsolescencia y Mantenimiento Manual (Data Stagnation)**:
   - Puesto que no consulta una API viva, los datos se quedarán obsoletos con el tiempo. El administrador del sistema (el usuario) debe descargar manualmente el archivo de iOverlander, convertirlo y reemplazar el `ioverlander.kmz` en el NAS para que las futuras ejecuciones del scheduler actualicen los datos.
3. **Carencia de Multimedia estructurada**:
   - Este volcado no incluye URLs directas a las fotos de los usuarios en la plataforma, limitando el atractivo visual de estos puntos en el frontend de GeoSpots en comparación con otras fuentes (ej. Nomady o CamperContact).
4. **Dependencia de Memoria**:
   - Cargar y parsear un árbol XML de 170MB entero en memoria mediante `ElementTree` puede generar picos de uso de RAM en el contenedor del NAS durante el ciclo de parseo.

---
**Estado Actual:** Integrado y operativo. Se estima la ingesta de ~31,000 puntos en Europa.
