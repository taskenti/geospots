"""LLM prompt templates for Phase 3.

v1 (review-level, regex-first fallback) → kept for backwards compatibility.
v2 (spot-level, Gemini-first via Batch API + context caching) → active path.
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════
# v2: Spot-level prompts (1 LLM call por spot, Batch API)
# ═══════════════════════════════════════════════════════════════

ENRICHMENT_VERSION = 3  # v3: añade bloque SERVICIOS + reglas tricky + calibración confidence + idioma forzado.

# Catálogo completo (sin descripciones largas — el LLM ya conoce el dominio).
# Mantener sincronizado con db/schema.sql signal_types y enrichment/signal_registry.py.
SIGNAL_CATALOG_V2 = """\
NUMERIC (0.0-1.0):
  quietness          tranquilidad (0=ruidoso, 1=silencio total)
  noise              ruido general
  road_noise         ruido de carretera
  party_noise        ruido de gente/fiesta
  train_noise        ruido de tren
  safety             seguridad percibida
  police_risk        riesgo de policia/multa
  theft_risk         riesgo de robos
  beauty             belleza del entorno
  cleanliness        limpieza
  large_vehicle      apto >7m (0=imposible, 1=perfecto)
  road_quality       calidad del acceso (0=intransitable, 1=asfalto)
  crowd_level        masificacion
  wind_exposure      exposicion al viento
  stealth            discrecion para pernocta
  cell_coverage      cobertura movil
  mosquitoes         mosquitos (estacional)

BOOLEAN:
  sea_view             vistas al mar
  mountain_view        vistas a montaña
  lake_nearby          lago/rio cercano
  shade_morning        sombra por la mañana
  shade_afternoon      sombra por la tarde
  overnight_safe       pernocta posible sin problemas
  wild_camping_legal   acampada libre legal
  dog_friendly         apto perros
  family_friendly      apto familias
  accessible_pmr       accesible movilidad reducida
  water_working        agua operativa AHORA
  electricity_working  electricidad operativa AHORA
  dump_station_working vaciado aguas operativo AHORA

TEXT (categóricos):
  noise_source         fuente de ruido. Valores: highway|road|train|airport|sea|wind|party|industry|crowd|other
  parking_capacity     capacidad. Valores: small|medium|large
"""

SYSTEM_PROMPT_V2 = f"""\
Eres un analista experto en spots para autocaravanas y furgonetas camper.
Recibes el contexto de UN spot (datos + servicios estructurados + descripciones
+ reviews ordenadas por relevancia temporal, mas recientes primero) y devuelves
un JSON estructurado con afirmaciones explicitas, resumen narrativo y tags.

═══ REGLAS DE EXTRACCION ═══

1. NO inventes. Solo afirma lo que el texto soporta literal o por implicacion clara.
2. Cada claim cita review_id de origen, o "description" si viene de descripciones,
   o "services" si viene del bloque SERVICIOS estructurado.
3. Da mas peso a reviews recientes. Si reviews antiguas y recientes contradicen,
   prioriza recientes y mencionalo en summary.
4. Negacion, sarcasmo e ironia importan: "no muy tranquilo" != "tranquilo".
5. Scores numericos en 0.0-1.0; booleanos solo con evidencia clara.
6. Si una señal no tiene soporte, NO la incluyas (no metas null).
7. NO REPITAS: MAXIMO 1 claim por signal_type. Si varias reviews dicen lo mismo,
   emite UN solo claim con el review_id mas representativo (el mas reciente o
   explicito) y refleja el consenso en el value.
   EXCEPCION: noise_source admite varios claims (un valor distinto por claim).
8. Idiomas en reviews: es/en/fr/de/it/nl/pt. Entiende todos.

═══ REGLAS PARA SEÑALES ESPECIFICAS (las que más fallan) ═══

9. wild_camping_legal:
   - True SOLO si TEXTO menciona explicitamente legalidad ("autorizado",
     "permitido", "area habilitada", "señalizado para autocaravanas",
     "P + caravana", "wohnmobil erlaubt", "stationnement autorisé").
   - "Gratis" o "gratuito" NO implica legal.
   - Cartel o mencion de prohibicion = False, AUNQUE sea parcial
     ("prohibido motorhomes en un lado", "no overnight signs").
   - Si no hay evidencia clara en ninguna direccion, OMITE el claim.

10. large_vehicle:
    - Restricciones de longitud/altura BAJAN el score, no lo suben.
    - "max 7m", "non oltre 7m", "no superior a X", "altura limitada",
      "barriers", "low clearance" → score 0.2-0.4.
    - Solo > 0.7 si reviews mencionan explicitamente acceso de
      camiones, autocaravanas grandes (>7m), 5th wheels, integrales largos.
    - Si SERVICIOS da altura_max_m: usa ese dato como anclaje
      (>3.0m = bueno; 2.0-2.5m = muy restrictivo).

11. water_working / electricity_working / dump_station_working (CRITICO):
    - SIEMPRE emite estos claims si el bloque SERVICIOS tiene dato (Si/No),
      independientemente de si hay reviews que lo confirmen. Son hechos duros
      reconciliados de las fuentes. NO los omitas porque "sean obvios".
    - Patron por defecto (sin contradiccion en reviews):
        SERVICIOS "Agua: Si"  → water_working=true,  conf 0.85, review_id="services"
        SERVICIOS "Agua: No"  → water_working=false, conf 0.9,  review_id="services"
        SERVICIOS "Electricidad: Si" → electricity_working=true, conf 0.85, review_id="services"
        SERVICIOS "Vaciado grises: Si" o "Vaciado negras: Si" → dump_station_working=true, conf 0.85, review_id="services"
    - Si review reciente CONTRADICE SERVICIOS, la review gana:
        SERVICIOS "Agua: Si" + review "el grifo no funciona" → false, conf 0.8, review_id=N
    - Si SERVICIOS no menciona el servicio Y ninguna review lo menciona → OMITE.
    - En campings/areas AC con muchos servicios, los 3 claims working DEBEN
      aparecer (uno por cada servicio listado en SERVICIOS).

12. police_risk / theft_risk:
    - UNA sola review reciente con evidencia clara basta. No exijas consenso.
    - "La policia nos movio", "ronda policial", "nos despertaron a las 3am",
      "nos robaron las bicis", "rompieron ventana" → claim con conf 0.7-0.8.
    - police_risk y theft_risk son escalas 0=sin riesgo, 1=alto riesgo.

13. Idiomas en outputs:
    - summary_es DEBE estar en español. summary_en DEBE estar en inglés.
    - Independientemente del idioma de las reviews originales. TRADUCE si hace falta.
    - tags y best_for en español, minusculas.
    - best_season / avoid_season en español: "primavera", "verano", "invierno",
      "otoño", o rango breve "junio-agosto", "abril-octubre". null si no claro.

═══ CALIBRACION DE CONFIDENCE ═══

  0.9-1.0: afirmacion literal en 2+ reviews recientes coincidentes, o dato duro
           de SERVICIOS confirmado por review reciente.
  0.7-0.8: afirmacion literal en 1 review reciente, o dato de SERVICIOS solo.
  0.5-0.6: inferencia razonable a partir de contexto.
  <0.5  : OMITIR el claim (no aporta valor con esa incertidumbre).

═══ SEÑALES PERMITIDAS ═══
{SIGNAL_CATALOG_V2}

═══ RESUMEN Y TAGS ═══

- summary_es / summary_en: 2-3 frases. Factual y EQUILIBRADO. NO marketing.
  Menciona aspectos negativos relevantes (ruido, robos, restricciones, mosquitos)
  si las reviews los citan. Menciona cambios temporales si los hay.
- tags: 3-8 palabras clave en minusculas español, sin duplicados semanticos
  (no pongas "gratis" Y "gratuito"; elige uno).
- best_for: 1-4 perfiles en español (parejas, familias, overlanding, perros,
  fotografia, surferos, ciclistas, trabajo-remoto...).
- best_season / avoid_season: solo si las reviews lo soportan.

═══ EJEMPLO DE INTERPRETACION TRICKY ═══

Si reviews dicen:
  [review_id=42] "Parking gratuito junto al super, max 7m. Cartel de prohibido
  motorhomes en la entrada principal pero la gente aparca por detras sin problema."
  [review_id=43] "Nos despertaron policia a las 4am pidiendo que nos moviesemos."

Claims correctos:
  - large_vehicle=0.3 (restriccion 7m), review_id=42, conf=0.8
  - wild_camping_legal=false (cartel de prohibicion), review_id=42, conf=0.7
  - police_risk=0.7 (intervencion reciente), review_id=43, conf=0.8
  - gratuito... no es un signal_type valido — esto va en summary y tags.

═══ FORMATO DE SALIDA (JSON estricto, sin markdown, sin comentarios) ═══

{{
  "claims": [
    {{"signal": "<id>", "value": <num|bool|text>, "confidence": <0-1>,
      "review_id": <int|"description"|"services">, "excerpt": "<fragmento <=120 chars>"}}
  ],
  "summary_es": "<string>",
  "summary_en": "<string>",
  "tags": ["<tag>", ...],
  "best_for": ["<perfil>", ...],
  "best_season": "<string|null>",
  "avoid_season": "<string|null>"
}}

Si no hay informacion suficiente para nada: {{"claims":[],"summary_es":null,"summary_en":null,"tags":[],"best_for":[],"best_season":null,"avoid_season":null}}
"""


def _fmt_bool_es(b) -> str:
    if b is True:
        return "Sí"
    if b is False:
        return "No"
    return "?"


def _build_servicios_block(spot: dict) -> list[str]:
    """Construye el bloque SERVICIOS con los datos estructurados de las fuentes.

    Devuelve [] si no hay ningún dato — el LLM entiende que no hay info.
    """
    # Recoger todos los campos relevantes (pueden ser None)
    gratuito       = spot.get("gratuito")
    precio_aprox   = spot.get("precio_aprox")
    precio_info    = (spot.get("precio_info") or "").strip()
    agua           = spot.get("agua_potable")
    grises         = spot.get("vaciado_grises")
    negras         = spot.get("vaciado_negras")
    electricidad   = spot.get("electricidad")
    ducha          = spot.get("ducha")
    wifi           = spot.get("wifi")
    wc             = spot.get("wc_publico")
    acceso_grandes = spot.get("acceso_grandes")
    num_plazas     = spot.get("num_plazas")
    altura_max     = spot.get("altura_max_m")
    temporada      = (spot.get("temporada_apertura") or "").strip()

    # Si TODO es None/vacío, no emitimos el bloque
    todos_los_valores = [
        gratuito, precio_aprox, precio_info, agua, grises, negras,
        electricidad, ducha, wifi, wc, acceso_grandes, num_plazas,
        altura_max, temporada,
    ]
    if all(v is None or v == "" for v in todos_los_valores):
        return []

    out: list[str] = ["SERVICIOS (datos estructurados de fuentes — hechos, no inferir):"]

    # Línea 1: precio
    precio_partes = []
    if gratuito is True:
        precio_partes.append("Gratuito: Sí")
    elif gratuito is False:
        precio_partes.append("Gratuito: No")
    if precio_aprox is not None:
        precio_partes.append(f"Precio aprox: ~{precio_aprox:.0f}€")
    if precio_info:
        precio_partes.append(f"Info precio: {precio_info[:100]}")
    if precio_partes:
        out.append("  " + " | ".join(precio_partes))

    # Línea 2: aguas
    agua_partes = []
    if agua is not None:
        agua_partes.append(f"Agua potable: {_fmt_bool_es(agua)}")
    if grises is not None:
        agua_partes.append(f"Vaciado grises: {_fmt_bool_es(grises)}")
    if negras is not None:
        agua_partes.append(f"Vaciado negras: {_fmt_bool_es(negras)}")
    if agua_partes:
        out.append("  " + " | ".join(agua_partes))

    # Línea 3: comodidades
    com_partes = []
    if electricidad is not None:
        com_partes.append(f"Electricidad: {_fmt_bool_es(electricidad)}")
    if ducha is not None:
        com_partes.append(f"Ducha: {_fmt_bool_es(ducha)}")
    if wc is not None:
        com_partes.append(f"WC público: {_fmt_bool_es(wc)}")
    if wifi is not None:
        com_partes.append(f"Wifi: {_fmt_bool_es(wifi)}")
    if com_partes:
        out.append("  " + " | ".join(com_partes))

    # Línea 4: capacidad / acceso
    cap_partes = []
    if num_plazas is not None:
        cap_partes.append(f"Plazas: ~{num_plazas}")
    if altura_max is not None:
        cap_partes.append(f"Altura máx: {altura_max:.1f}m")
    if acceso_grandes is not None:
        cap_partes.append(f"Acceso vehículos grandes: {_fmt_bool_es(acceso_grandes)}")
    if cap_partes:
        out.append("  " + " | ".join(cap_partes))

    # Línea 5: temporada
    if temporada:
        out.append(f"  Apertura: {temporada[:100]}")

    out.append("")  # línea en blanco al final
    return out


def build_spot_user_prompt(spot: dict, reviews: list[dict]) -> str:
    """User prompt para enriquecimiento spot-level v2/v3.

    `spot` debe traer: id, canonical_name, tipo, country_iso, lat, lon, fuentes (list),
    descripcion_es/en/fr/de/it/nl/pt (opcional), y los campos de servicios v3
    (gratuito, agua_potable, electricidad, num_plazas, altura_max_m, etc.).
    `reviews` ya viene ordenado y recortado por spot_packager.select_reviews_for_prompt.
    """
    fuentes = spot.get("fuentes") or []
    if isinstance(fuentes, str):
        fuentes = [fuentes]

    lines = [
        f"SPOT id={spot['id']}",
        f'Nombre: "{(spot.get("canonical_name") or "").strip()}"',
        f"Tipo: {spot.get('tipo') or 'otro'}",
        f"Pais: {spot.get('country_iso') or '?'}",
        f"Coordenadas: {spot.get('lat'):.4f}, {spot.get('lon'):.4f}",
        f"Fuentes: {', '.join(fuentes) if fuentes else '?'}",
        "",
    ]

    # v3: bloque SERVICIOS estructurado (antes de descripciones)
    lines.extend(_build_servicios_block(spot))

    desc_blocks = []
    for lang in ("es", "en", "fr", "de", "it", "nl", "pt"):
        txt = (spot.get(f"descripcion_{lang}") or "").strip()
        if txt:
            desc_blocks.append(f"[{lang.upper()}] {txt[:600]}")
    if desc_blocks:
        lines.append("DESCRIPCIONES:")
        lines.extend(desc_blocks)
        lines.append("")

    if reviews:
        lines.append(f"REVIEWS (n={len(reviews)}, ordenadas por relevancia temporal):")
        for r in reviews:
            fecha = r.get("fecha")
            fecha_str = fecha.strftime("%Y-%m") if hasattr(fecha, "strftime") else (str(fecha)[:7] if fecha else "?")
            stars = ("★" * int(r["rating"])) if r.get("rating") else ""
            source = r.get("source") or "?"
            texto = (r.get("texto_limpio") or r.get("texto") or r.get("texto_original") or "").strip().replace("\n", " ")
            lines.append(f"[review_id={r['id']}] [{fecha_str}] [{source}] {stars} {texto}")
    else:
        lines.append("REVIEWS: (ninguna disponible — extraer solo de descripciones y servicios)")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# v1: Review-level prompt (LEGACY — se mantiene para compatibilidad)
# ═══════════════════════════════════════════════════════════════

ALLOWED_SIGNALS = (
    "quietness, noise, road_noise, police_risk, safety, theft_risk, beauty, "
    "sea_view, mountain_view, shade_morning, shade_afternoon, large_vehicle, "
    "road_quality, overnight_safe, crowd_level, wind_exposure, stealth, cleanliness"
)

EXTRACTION_PROMPT = """Eres un analizador de reviews de camper/vanlife.
Extrae SOLO afirmaciones explicitas sobre senales definidas.
NO inventes informacion que no este en el texto.

SENALES PERMITIDAS:
- quietness: tranquilidad (0.0=muy ruidoso, 1.0=silencio total)
- noise: ruido general (0.0=silencio, 1.0=insoportable)
- road_noise: ruido de carretera especifico
- police_risk: riesgo policia/multa (0.0=nulo, 1.0=seguro)
- safety: seguridad general (0.0=peligroso, 1.0=muy seguro)
- theft_risk: riesgo robos (0.0=nulo, 1.0=alto)
- beauty: belleza entorno (0.0=feo, 1.0=espectacular)
- sea_view: vistas al mar (boolean)
- mountain_view: vistas montana (boolean)
- shade_morning: sombra manana (boolean)
- shade_afternoon: sombra tarde (boolean)
- large_vehicle: apto >7m (0.0=imposible, 1.0=perfecto)
- road_quality: estado camino (0.0=intransitable, 1.0=asfalto perfecto)
- overnight_safe: pernocta posible (boolean)
- crowd_level: masificacion (0.0=vacio, 1.0=lleno)
- wind_exposure: viento (0.0=protegido, 1.0=muy expuesto)
- stealth: discrecion (0.0=muy visible, 1.0=invisible)
- cleanliness: limpieza (0.0=sucio, 1.0=impecable)

TEXTO:
"{texto_limpio}"

Responde SOLO JSON, sin markdown:
{{"claims":[{{"signal":"<id>","value":"<valor>","confidence":<0-1>,"excerpt":"<fragmento>"}}]}}
Si no hay senales: {{"claims":[]}}
"""


def build_extraction_prompt(texto_limpio: str) -> str:
    """v1 (legacy)."""
    return EXTRACTION_PROMPT.format(texto_limpio=texto_limpio.replace('"', '\\"'))
