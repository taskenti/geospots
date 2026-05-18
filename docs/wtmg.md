# 🏕️ Welcome To My Garden (WTMG) Scraper

## 📖 Información General
**Welcome To My Garden (WTMG)** es una red sin ánimo de lucro originaria de Bélgica que conecta a ciudadanos dispuestos a ofrecer un rincón de su jardín de forma **completamente gratuita** a viajeros ecológicos (ciclistas, senderistas o campistas lentos). A diferencia de otras plataformas de pago como Nomady o Campspace, WTMG mantiene el espíritu altruista del intercambio cultural y la acampada sostenible en lugares privados. Es extremadamente popular en Europa occidental.

## 🛠️ Arquitectura y Funcionamiento
El scraper `wtmg.py` se aprovecha de una exposición arquitectónica de la plataforma web: la web de WTMG no cuenta con un backend clásico (API REST propia), sino que conecta directamente su Frontend a una base de datos **Google Cloud Firestore**.

1. **Firestore REST API**:
   - El scraper realiza peticiones `POST` al endpoint oficial de Google: `https://firestore.googleapis.com/v1/projects/wtmg-production/...:runQuery`.
   - Utiliza una API Key pública que hemos extraído inspeccionando el tráfico de su web.
2. **Consultas Nativas NoSQL**:
   - El payload del POST no es un formulario, sino una `structuredQuery` de Firestore. Le pedimos a Google la colección `campsites`, filtrando directamente por los documentos que tengan el campo booleano `listed = True`.
3. **Paginación por Cursor (Cursor Pagination)**:
   - Para no colapsar la petición, extraemos lotes de **1000 jardines**. En cada iteración guardamos el nombre interno del último documento procesado (`last_doc_name`), y lo enviamos en la siguiente petición como el parámetro `startAt`.

## 🧠 Lógica de Mapeo y Normalización
- **Deserialización Firestore**: La base de datos de Google devuelve los datos en un formato extremadamente verboso (ej. `{"stringValue": "Hola"}` en lugar de `"Hola"`). Hemos creado una función recursiva `_extract_firestore_value()` que aplana toda la respuesta a diccionarios estándar de Python.
- **Hack de Identidad**: Al ser jardines privados, la plataforma no les pone nombres como "Camping Los Pinos". El scraper genera un nombre artificial: `Jardín WTMG - [ID]`.
- **Hack de la Descripción**: Para que el usuario final pueda leer la descripción del anfitrión en nuestra App (GeoSpots), el scraper la inyecta artificialmente en la base de datos haciéndola pasar por una "Review" cuyo autor es "Anfitrión del jardín".
- **Adivinación de Fotos**: En lugar de hacer una segunda petición para obtener el enlace descargable de la foto, el scraper predice la ruta estática del bucket de Firebase Storage inyectando el ID del jardín y el nombre del archivo.

## ⚠️ Peligros y Carencias (Riesgos Conocidos)

1. **Rotación de la API Key (Key Revocation)**:
   - Este es el talón de Aquiles de este scraper. Dependemos de la `API_KEY` incrustada en nuestro código. Si los administradores de WTMG deciden rotar sus claves de Firebase por seguridad, el scraper devolverá un Error HTTP 403 (Permiso Denegado) hasta que capturemos la nueva clave de su web.
2. **Bloqueo por Cuotas de Google (Firebase Billing)**:
   - Al usar el endpoint público, las peticiones que hacemos se descuentan directamente del plan de facturación de WTMG en Google Cloud. Si nuestro scraper o el uso orgánico supera sus cuotas diarias de lectura de Firestore, Google cortará el acceso a la colección temporalmente.
3. **Vulnerabilidad de los Enlaces a Fotos**:
   - Hemos construido la URL del Firebase Storage "a mano" (`firebasestorage.googleapis.com/v0/b/...`). Si cambian las políticas de permisos del bucket, las reglas de CORS o la estructura de carpetas, las fotos dejarán de cargar en nuestro mapa.

---
**Estado Actual:** Integrado y operativo mediante acceso directo a base de datos NoSQL de Google. Rápido y preciso, pero altamente dependiente de la arquitectura actual del cliente.
