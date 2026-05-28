"""Tiered claim extraction: regex first, LLM (provider-agnostic) as optional fallback."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from loguru import logger

from .llm_provider import call_llm_sync, get_active_model, get_provider_name
from .multilingual_lexicon import apply_lexicon_blend
from .prompts import build_extraction_prompt
from .text_trimmer import trim_for_llm

EXTRACTOR_VERSION = "phase3-2026-05-27"


@dataclass(frozen=True)
class ExtractedClaim:
    signal: str
    value: str
    confidence: float
    excerpt: str
    extractor_name: str = "regex_v1"
    extractor_version: str = EXTRACTOR_VERSION

    def as_dict(self) -> dict:
        return {
            "signal": self.signal,
            "value": self.value,
            "confidence": self.confidence,
            "excerpt": self.excerpt,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
        }


PATTERNS: list[tuple[str, str, float, tuple[str, ...]]] = [
    # ── QUIETNESS / NOISE ────────────────────────────────────────────────────────
    ("quietness", "0.9", 0.86, (
        "tranquil", "tranquilo", "tranquila", "calm", "quiet", "silenc",
        "ruhig", "calme", "peaceful", "pacífico", "pacifica", "sereno",
        "no noise", "sin ruido", "sehr ruhig", "très calme", "dormimos bien",
        "slept well", "dormimos genial", "noche tranquila", "peaceful night",
        "didn't hear anything", "no se oye nada", "silencieux",
    )),
    ("quietness", "0.2", 0.84, (
        "ruidoso", "noisy", "bruyant", "loud", "noise all night",
        "couldn't sleep", "no pudimos dormir", "no dejaron dormir",
        "pas dormi", "laut", "sehr laut", "ruido toda la noche",
    )),
    ("noise", "0.8", 0.84, (
        "ruido", "noise", "loud", "bruit", "laerm", "lärm", "geraeusch",
        "bruit de", "rumor", "noisy", "ruidoso",
    )),
    ("road_noise", "0.8", 0.88, (
        "carretera", "autopista", "road noise", "traffic", "trafico", "trucks",
        "highway noise", "ruido de trafico", "coches", "autoroute",
        "motorway", "autobahn", "freeway noise", "tren", "train noise",
        "paso de trenes", "railway",
    )),
    ("party_noise", "0.8", 0.82, (
        "fiesta", "party", "music", "musica alta", "bass", "drunk",
        "borrachos", "botellon", "fete", "lärm von party",
        "loud music", "musique forte", "boombox",
    )),
    # ── POLICE / SECURITY ────────────────────────────────────────────────────────
    ("police_risk", "0.85", 0.9, (
        "policia", "police", "multa", "fine", "verboten", "expuls", "evicted",
        "kicked out", "nos echaron", "nos multaron", "cops", "guardia civil",
        "gendarm", "carabinieri", "moved on", "asked us to leave",
        "told to leave", "nos pidieron que", "no pernoctar", "prohibido",
        "towed", "grua", "busse", "amende",
    )),
    ("police_risk", "0.1", 0.85, (
        "no police", "sin policia", "no problems with police",
        "police never came", "nadie nos molestó", "left alone",
        "nadie vino", "nobody bothered",
    )),
    # ── THEFT / SAFETY ───────────────────────────────────────────────────────────
    ("theft_risk", "0.85", 0.9, (
        "robo", "robbed", "theft", "break in", "broken into", "stolen",
        "nos robaron", "nos entraron", "window smashed", "luna rota",
        "cristal roto", "burglar", "thieves", "ladrones",
        "all stolen", "everything stolen", "vole", "volado",
    )),
    ("safety", "0.85", 0.82, (
        "seguro", "safe", "security", "sicher", "sentimos seguros",
        "nos sentimos seguros", "felt safe", "safe place", "lugar seguro",
        "bien sécurisé", "sicher gefühlt",
    )),
    ("safety", "0.2", 0.82, (
        "inseguro", "unsafe", "dangerous", "peligroso",
        "no me senti seguro", "didn't feel safe", "sketchy", "sospechoso",
        "poco seguro", "gefährlich",
    )),
    ("youth_trouble", "0.8", 0.85, (
        "local youths", "joyriders", "drug dealer", "antisocial",
        "mendigando", "begging", "yobs", "chavs", "junkies",
        "pandilla", "grupo de jovenes", "jóvenes molestando",
        "molestos", "borrachos molestando", "aggressive",
        "agresivo", "nos amenazaron", "threatening",
    )),
    # ── BEAUTY / VIEWS ────────────────────────────────────────────────────────────
    ("beauty", "0.9", 0.82, (
        "bonito", "beautiful", "spectacular", "precioso", "amazing view",
        "belle vue", "stunning", "gorgeous", "impresionante",
        "increible", "increíble", "maravilloso", "hermoso", "preciosidad",
        "espectacular", "wunderschön", "magnifique", "superbe",
        "breathtaking", "vistas increibles", "incredible views",
        "paraíso", "paradise", "idyllic", "idílico",
    )),
    ("beauty", "0.15", 0.75, (
        "feo", "ugly", "horrible vista", "no hay vistas", "sin encanto",
        "poco bonito",
    )),
    # ── CLEANLINESS ──────────────────────────────────────────────────────────────
    ("cleanliness", "0.85", 0.8, (
        "limpio", "clean", "propre", "sauber", "muy limpio", "bien limpio",
        "spotless", "impecable", "bien entretenu", "gepflegt",
        "aseos limpios", "clean toilets", "clean bathrooms",
    )),
    ("cleanliness", "0.15", 0.82, (
        "sucio", "dirty", "trash", "basura", "garbage", "sale",
        "mugre", "mugriento", "cochino", "asqueroso", "filthy",
        "lots of rubbish", "lleno de basura", "mucha basura",
        "schmutzig", "dreckig", "dégoûtant",
    )),
    # ── VIEWS: SEA / MOUNTAIN / LAKE ─────────────────────────────────────────────
    ("sea_view", "true", 0.88, (
        "vistas al mar", "sea view", "ocean view", "vue mer", "meerblick",
        "views of the sea", "vistas oceano", "vista al océano",
        "frente al mar", "overlooks the sea", "sea views",
        "oceano", "vista al mar", "junto al mar",
    )),
    ("mountain_view", "true", 0.86, (
        "vistas a montana", "mountain view", "vue montagne", "bergblick",
        "views of the mountains", "vistas a la sierra", "vistas a los picos",
        "montañas", "mountain views", "alpine views",
    )),
    ("lake_nearby", "true", 0.84, (
        "lago", "lake", "lac", "see nearby", "junto al lago",
        "next to a lake", "lakeside", "orilla del lago",
        # "reserva" eliminado — falso positivo masivo ("hicimos una reserva", "zona de reserva natural")
        # "embalse" eliminado — ambiguo (embalse = presa, no siempre bañable)
        "loch", "fjord", "pantano",
        "alongside the lake", "au bord du lac", "am see",
    )),
    # ── BEACH ACCESS ─────────────────────────────────────────────────────────────
    ("beach_access", "true", 0.87, (
        "beach access", "acceso a la playa", "playa al lado",
        "steps to beach", "beach nearby", "walk to beach",
        "playa a pie", "a la playa andando", "2 min to beach",
        "direct beach access", "junto a la playa",
        "beach within walking", "playa cercana", "close to beach",
        "next to the beach", "al lado de la playa",
    )),
    # ── RIVER / WATER NEARBY ─────────────────────────────────────────────────────
    ("river_nearby", "true", 0.82, (
        "river nearby", "rio cerca", "junto al rio", "next to a river",
        "estuary", "canal nearby", "loch nearby", "burn nearby",
        "arroyo", "stream nearby", "creek nearby", "riverbend",
        "ribera del", "orilla del rio",
    )),
    # ── DARK SKY / STARGAZING ────────────────────────────────────────────────────
    ("dark_sky", "true", 0.85, (
        "stargazing", "starry sky", "starry night", "stars amazing",
        "dark sky", "cielo oscuro", "milky way", "via lactea",
        "vía láctea", "stars were incredible", "amazing stars",
        "incredible stars", "see the stars", "ver las estrellas",
        "full of stars", "lleno de estrellas", "ciel étoilé",
        "sternenhimmel", "star gazing", "no light pollution",
        "sin contaminacion luminica",
    )),
    # ── HIKING / CYCLING ─────────────────────────────────────────────────────────
    ("hiking_nearby", "true", 0.80, (
        "hiking nearby", "trails nearby", "walking trails", "rutas de senderismo",
        "senderos cerca", "good walks", "buenas rutas", "trail access",
        "hiking trails", "walking distance to trails",
        "rutas cercanas", "buen senderismo", "walks nearby",
        "footpaths", "camino cercano", "rutas de montaña",
    )),
    ("cycling_nearby", "true", 0.78, (
        "cycling nearby", "bike trail", "biketrail", "carril bici",
        "cycling path", "good for cycling", "bike path nearby",
        "ciclovía", "ciclovia", "ruta ciclista", "bicicleta",
        "piste cyclable", "radweg",
    )),
    # ── SHADE ────────────────────────────────────────────────────────────────────
    ("shade_morning", "true", 0.75, (
        "sombra por la manana", "morning shade", "shade in the morning",
        "sombra a primera hora",
    )),
    ("shade_afternoon", "true", 0.75, (
        "sombra por la tarde", "afternoon shade", "shade in the afternoon",
        "sombra por las tardes", "shaded afternoon",
    )),
    # ── LARGE VEHICLE ────────────────────────────────────────────────────────────
    ("large_vehicle", "0.85", 0.82, (
        ">7m", "large motorhome", "big rig", "autocaravana grande",
        "grandes vehiculos", "7m fine", "7.5m fine", "8m ok",
        "big vehicle ok", "no problem with large", "cabía bien",
        "cabe perfectamente", "room for big vans", "suitable for large",
        "7m sin problema", "grandes autocaravanas sin problema",
    )),
    ("large_vehicle", "0.15", 0.82, (
        "no apto para grandes", "too narrow", "narrow access", "not for large",
        "impossible with large", "no caben grandes", "tight for big",
        ">7m no", "not suitable for large", "no pasan grandes",
        "difícil para vehículos grandes", "dificil para grandes",
        "no apta para autocaravanas grandes", "tight entrance",
        "entrada estrecha", "paso estrecho",
    )),
    # ── HEIGHT RESTRICTION ───────────────────────────────────────────────────────
    ("height_restriction", "2.0", 0.9, (
        "2m height", "height 2m", "2.0m barrier", "2m barrier",
        "altura maxima 2", "altura 2m",
    )),
    ("height_restriction", "2.5", 0.9, (
        "2.5m height", "height 2.5", "2.5m barrier", "2.5 barrier",
        "altura maxima 2.5", "altura 2.5m",
    )),
    ("height_restriction", "3.0", 0.9, (
        "3m height", "height 3m", "3.0m barrier", "3m barrier",
        "altura maxima 3", "altura 3m", "3m restriction",
        "3 metre height", "hauteur 3m",
    )),
    ("height_restriction", "0.5", 0.82, (
        "height restriction", "altura maxima", "barrier at entrance",
        "barrera de altura", "barra de altura", "height bar",
        "restricted height", "hauteur limitée", "höhenbeschränkung",
    )),
    # ── ROAD QUALITY ─────────────────────────────────────────────────────────────
    ("road_quality", "0.85", 0.8, (
        "buen acceso", "good road", "asphalt", "asfalto",
        "easy access", "fácil acceso", "acceso facil",
        "bien asfaltado", "paved road", "good track",
        "easy to find", "buena carretera",
    )),
    ("road_quality", "0.2", 0.82, (
        "mal camino", "bad road", "dirt track", "bumpy", "gravel road",
        "very muddy", "barro", "potholes", "baches", "rough track",
        "churned up", "camino de tierra", "pista de tierra",
        "mal estado", "road bad", "schlechte strasse",
        "route dégradée", "piste cahoteuse", "poca calle", "pistin",
    )),
    # ── OVERNIGHT ────────────────────────────────────────────────────────────────
    ("overnight_safe", "true", 0.86, (
        "pernocta", "overnight", "slept", "dormir", "night without problem",
        "pernoctamos", "dormimos aqui", "dormimos aquí",
        "passed the night", "spent the night", "noche sin problemas",
        "sin problemas por la noche",
    )),
    ("overnight_safe", "false", 0.9, (
        "no overnight", "no pernocta", "prohibido pernoctar",
        "overnight forbidden", "no se puede pernoctar",
        "camping verboten", "no camping", "no se puede acampar",
        "moved on at night", "toldos cerrados", "prohibido acampar",
        "nicht übernachten", "nacht verboten",
    )),
    # ── SPOT CLOSED ──────────────────────────────────────────────────────────────
    ("spot_closed", "true", 0.88, (
        "closed", "cerrado", "fermé", "geschlossen",
        "permanently closed", "cerrado permanentemente",
        "ya no existe", "no longer exists", "no longer open",
        "spot closed", "parking cerrado", "zona cerrada",
        "gates locked", "barrera cerrada", "acceso bloqueado",
        "blocked access", "construction", "obras",
    )),
    # ── CROWD LEVEL ──────────────────────────────────────────────────────────────
    ("crowd_level", "0.85", 0.78, (
        "lleno", "crowded", "busy", "packed", "masificado",
        "muy concurrido", "too many vans", "full of motorhomes",
        "lleno de autocaravanas", "no habia sitio", "no había sitio",
        "overflowing", "chock full",
    )),
    ("crowd_level", "0.15", 0.76, (
        "empty", "vacio", "nadie", "alone", "sin gente",
        "only campers", "we were alone", "solos",
        "nadie mas", "we were the only", "no other vans",
        "éramos los únicos", "éramos solos", "habia poca gente",
        "nadie más", "vacío", "desierto", "tuvimos el sitio para nosotros",
    )),
    # ── WIND ─────────────────────────────────────────────────────────────────────
    ("wind_exposure", "0.85", 0.78, (
        "windy", "mucho viento", "ventoso", "exposed to wind",
        "very windy", "couldn't sleep wind", "viento intenso",
        "strong wind", "wind all night", "viento toda la noche",
        "sheltered from wind", "abrigado del viento",
        "wind battered", "viento racheado", "gusts",
    )),
    ("wind_exposure", "0.1", 0.76, (
        "sheltered", "abrigado", "abrigo del viento", "no wind",
        "sin viento", "protected from wind",
    )),
    # ── CELL COVERAGE ────────────────────────────────────────────────────────────
    ("cell_coverage", "0.9", 0.80, (
        "good signal", "good cell", "4g here", "4g ok", "5g here",
        "cobertura buena", "buena cobertura", "señal buena",
        "wifi ok", "cobertura 4g", "internet ok", "señal perfecta",
        "signal perfect", "full bars", "buena señal",
    )),
    ("cell_coverage", "0.1", 0.82, (
        "no signal", "sin cobertura", "no reception",
        "sin señal", "no hay cobertura", "no hay señal",
        "dead zone", "zona sin cobertura", "without signal",
        "kein empfang", "pas de réseau", "no network",
    )),
    # ── WATER / SHOWER / FACILITIES ──────────────────────────────────────────────
    ("water_working", "true", 0.82, (
        "agua potable", "drinking water", "water works", "agua funciona",
        "hay agua", "water available", "free water",
    )),
    ("water_working", "false", 0.84, (
        "no water", "sin agua", "no hay agua", "agua cortada",
        "water not working", "agua no funciona", "no drinking water",
    )),
    ("shower_working", "true", 0.80, (
        "duchas bien", "showers work", "hot shower", "good showers",
        "ducha caliente", "buenas duchas", "showers clean",
        "duchas limpias", "free showers", "duchas gratis",
    )),
    ("shower_working", "false", 0.82, (
        "no showers", "sin duchas", "ducha no funciona",
        "shower broken", "cold shower", "ducha fria",
        "ducha fría", "showers not working", "no hay duchas",
        "douche froide", "douche hors service",
    )),
    ("electricity_working", "true", 0.80, (
        "electicity works", "electricidad funciona", "enchufes funcionan",
        "power ok", "electric hookup ok",
    )),
    ("electricity_working", "false", 0.82, (
        "no electricity", "sin electricidad", "no power",
        "electricity not working", "enchufes no funcionan",
        "current not working", "corriente no funciona",
    )),
    ("dump_station_working", "true", 0.80, (
        "vaciado funciona", "dump station works", "cassette ok",
        "vaciado disponible", "punto de vaciado funciona",
    )),
    ("dump_station_working", "false", 0.84, (
        "vaciado no funciona", "dump station broken",
        "dump station closed", "vaciado cerrado",
        "no vaciado", "point de vidange fermé",
    )),
    # ── STEALTH / DISCRETION ─────────────────────────────────────────────────────
    ("stealth", "0.85", 0.76, (
        "discreto", "hidden", "stealth", "apartado",
        "out of sight", "fuera de vista", "sin ser visto",
        "poco transitado", "nobody sees you", "nadie nos vio",
        "no se ve desde la calle",
    )),
    # ── MOSQUITOES / INSECTS ─────────────────────────────────────────────────────
    ("mosquitoes", "0.85", 0.72, (
        "mosquito", "mosquitos", "mücken", "moustiques",
        "lots of insects", "muchos insectos", "midges", "biting insects",
        "horsefly", "tábanos", "tabanos",
    )),
    ("mosquitoes", "0.1", 0.70, (
        "no mosquitoes", "sin mosquitos", "no insects",
        "no bugs", "no midges",
    )),
    # ── DOG / FAMILY ─────────────────────────────────────────────────────────────
    ("dog_friendly", "true", 0.78, (
        "dog friendly", "dogs welcome", "perros bienvenidos",
        "good for dogs", "dogs allowed", "perros permitidos",
        "chiens acceptés", "hundefreundlich",
    )),
    ("dog_friendly", "false", 0.80, (
        "no dogs", "no perros", "dogs not allowed", "perros no",
        "no se admiten perros", "chiens interdits",
    )),
    ("family_friendly", "true", 0.75, (
        "family friendly", "good for families", "kids love it",
        "perfecto para niños", "ideal para familias",
        "niños pueden bañarse", "playground nearby",
    )),
    # ── WILD CAMPING / LEGAL ─────────────────────────────────────────────────────
    ("wild_camping_legal", "true", 0.80, (
        "camping libre", "wild camping ok", "free camping allowed",
        "pernocta libre", "acampar libre",
        "legal to wild camp", "camping allowed here",
    )),
    ("wild_camping_legal", "false", 0.85, (
        "camping prohibido", "no wild camping", "acampar prohibido",
        "camping interdit", "camping verboten",
    )),
    # ── PARKING CAPACITY ─────────────────────────────────────────────────────────
    ("parking_capacity", "big", 0.75, (
        "large car park", "parking grande", "plenty of space",
        "mucho espacio", "lots of room", "spacious parking",
        "aparcamiento amplio",
    )),
    ("parking_capacity", "small", 0.72, (
        "small parking", "parking pequeño", "only a few vans",
        "pocas plazas", "few spots", "limited spaces",
        "aparcamiento pequeño",
    )),
]


def _excerpt(text: str, needle: str, window: int = 80) -> str:
    pos = text.lower().find(needle.lower())
    if pos < 0:
        return text[:window]
    start = max(0, pos - window // 2)
    end = min(len(text), pos + len(needle) + window // 2)
    return text[start:end].strip()


def extract_claims_regex(text: str) -> list[dict]:
    lowered = text.lower()
    claims: list[ExtractedClaim] = []
    seen: set[tuple[str, str]] = set()
    for signal, value, confidence, needles in PATTERNS:
        for needle in needles:
            if needle in lowered:
                key = (signal, value)
                if key in seen:
                    continue
                seen.add(key)
                claims.append(ExtractedClaim(signal, value, confidence, _excerpt(text, needle)))
                break
    return [claim.as_dict() for claim in claims]


def _parse_json_response(text: str, extractor_name: str) -> list[dict]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE).strip()
    data = json.loads(cleaned)
    raw_claims = data.get("claims", []) if isinstance(data, dict) else []
    claims = []
    for item in raw_claims:
        signal = item.get("signal")
        value = item.get("value")
        if not signal or value is None:
            continue
        claims.append(
            ExtractedClaim(
                signal=str(signal),
                value=str(value),
                confidence=float(item.get("confidence", 0.7)),
                excerpt=str(item.get("excerpt", ""))[:500],
                extractor_name=extractor_name,
            ).as_dict()
        )
    return claims


async def extract_claims_llm(text: str) -> list[dict]:
    """Llamada directa al LLM activo (sin retry — el retry lo gestiona el worker).

    Provider y modelo vienen de ENV (ENRICHMENT_PROVIDER,
    GEMINI_ENRICHMENT_MODEL, DEEPSEEK_ENRICHMENT_MODEL).
    Lanza la excepción original en lugar de tragarla, para que el caller
    (worker._extract_claims_with_retry) pueda hacer backoff y contar errores.
    El texto se pasa por trim_for_llm() antes del prompt para eliminar filler.
    """
    text = trim_for_llm(text)
    prompt = build_extraction_prompt(text)
    resp = await asyncio.to_thread(
        call_llm_sync,
        prompt,
        system_prompt="",
        response_format="json",
    )
    extractor_name = f"llm_{resp.provider}"
    return _parse_json_response(resp.text or "", extractor_name=extractor_name)


# Alias retro-compatible para tests/jobs antiguos que lo importan
extract_claims_gemini = extract_claims_llm


async def extract_claims(text: str, review: dict | None = None, use_gemini: bool = True) -> list[dict]:
    """`use_gemini` mantiene el nombre por compat; activa el fallback LLM
    (sea Gemini o DeepSeek según ENRICHMENT_PROVIDER).

    Lógica de escalado al LLM:
    - Texto < 120 chars: nunca al LLM (demasiado corto para extraer algo útil).
    - Texto ≥ 120 chars + regex ≥ 3 claims: cobertura suficiente, no escalar.
    - Texto ≥ 120 chars + regex 0-2 claims: escalar al LLM para capturar señales
      que las keywords no cubren (texto descriptivo, idiomas menos comunes, etc.).
    - use_gemini=False: solo regex siempre.
    """
    regex_claims = extract_claims_regex(text)
    n_regex = len(regex_claims)

    if not use_gemini:
        return _blend_lexicon(text, regex_claims)
    # Texto demasiado corto: nunca al LLM independientemente de los claims.
    if len(text) < 120:
        return _blend_lexicon(text, regex_claims)
    # Cobertura suficiente con regex solo.
    if n_regex >= 3:
        return _blend_lexicon(text, regex_claims)

    # Texto sustancial (≥120 chars) con 0-2 claims regex: escalar al LLM.
    llm_claims = await extract_claims_llm(text)
    if not llm_claims:
        return _blend_lexicon(text, regex_claims)

    # Merge: LLM complementa al regex; no duplicar (signal, value) ya encontrado.
    seen = {(c["signal"], c["value"]) for c in regex_claims}
    merged = list(regex_claims)
    for c in llm_claims:
        key = (c["signal"], c["value"])
        if key not in seen:
            merged.append(c)
            seen.add(key)
    return _blend_lexicon(text, merged)


def _blend_lexicon(text: str, claims: list[dict]) -> list[dict]:
    """Aplica el blend léxico multilingüe (T2.1/D6) UNA sola vez sobre el
    resultado final de extract_claims. Reponderar dos veces no es idempotente
    (0.3*prior + 0.7*x), por eso se aplica solo aquí, nunca dentro de
    extract_claims_regex/llm.
    """
    return apply_lexicon_blend(text, claims)
