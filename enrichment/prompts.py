"""LLM prompt templates for Phase 3 extraction."""

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
    return EXTRACTION_PROMPT.format(texto_limpio=texto_limpio.replace('"', '\\"'))
