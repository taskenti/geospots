"""Léxico multilingüe ponderado (T2.1 / D5+D6 del plan de hardening Phase 3).

Propósito
---------
Algunas palabras "culturalmente cargadas" tienen una severidad fuerte y poco
ambigua en su idioma original que un LLM entrenado mayoritariamente en inglés
puede infravalorar. Ej: NL "bouwput" (pozo/foso de obra) = obras pesadas; un
modelo angloparlante puede tratarlo como ruido leve. Este módulo ancla un
*prior léxico* para 5 señales clave en 6 idiomas y lo combina con el juicio
contextual del LLM:

    final = D6_BLEND_LLM_WEIGHT * llm_score + LEXICON_BLEND_WEIGHT * lexical_prior
          = 0.7 * llm_score + 0.3 * lexical_prior          (D6)

Diseño (alineado con principios operativos del plan)
----------------------------------------------------
- Funciones **puras**, sin estado, sin I/O. Testeable en aislamiento.
- Solo *re-pondera* claims que el pipeline (regex o LLM) YA produjo. NO inventa
  claims nuevos: el plan limita T2.1 a `claim_extractor.py` y a un blend de
  confianza, no a un extractor adicional de señales.
- El blend se aplica UNA sola vez, a nivel de claim ya fusionado (idempotencia:
  0.3*prior + 0.7*x no es idempotente, así que nunca se aplica dos veces).
- Matching robusto a acentos (las reviews vienen con y sin tildes/umlauts):
  se normaliza con `unicodedata` (NFKD + strip de diacríticos) y se compara en
  minúsculas con límites de palabra laxos (substring sobre texto folded).

Mapeo D5 (concepto) -> señal real del registro (`signal_registry.py`)
---------------------------------------------------------------------
    construction    -> spot_closed = "true"        (obras que inutilizan el spot)
    closure         -> spot_closed = "true"         (cierre permanente/temporal)
    noise_source    -> noise       = "0.8"          (fuente de ruido fuerte)
    police_pressure -> police_risk = "0.85"         (presión policial / multas)
    wild_camping    -> wild_camping_legal = true/false  (polaridad explícita)
"""

from __future__ import annotations

import unicodedata

# --- Pesos del blend (D6) ----------------------------------------------------
LEXICON_BLEND_WEIGHT: float = 0.3
D6_BLEND_LLM_WEIGHT: float = 1.0 - LEXICON_BLEND_WEIGHT  # 0.7


def _fold(text: str) -> str:
    """Minúsculas + sin diacríticos, para matching robusto entre idiomas.

    'Baustelle' -> 'baustelle'; 'fermé' -> 'ferme'; 'Lärm' -> 'larm'.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# --- Léxico: (signal, value) -> { término_folded: prior } --------------------
# El prior es la severidad/confianza intrínseca del término en su idioma.
# Solo términos poco ambiguos y culturalmente cargados; los genéricos ya los
# cubre el regex de claim_extractor (no se duplican aquí salvo refuerzo fuerte).
#
# 5 conceptos D5 x 6 idiomas (EN/ES/FR/NL/DE/IT). ~150 entradas.
_RAW_LEXICON: dict[tuple[str, str], dict[str, float]] = {
    # ── construction -> spot_closed=true ─────────────────────────────────────
    ("spot_closed", "true"): {
        # EN
        "construction site": 0.90, "building site": 0.90, "roadworks": 0.85,
        "under construction": 0.88, "excavation": 0.85, "dug up": 0.82,
        # ES
        "obras": 0.85, "en obras": 0.90, "zona de obras": 0.92,
        "excavacion": 0.85, "movimiento de tierras": 0.88, "escombros": 0.80,
        # FR
        "chantier": 0.90, "en travaux": 0.90, "travaux en cours": 0.88,
        "terrassement": 0.85, "excavation": 0.85,
        # NL  (bouwput = foso de obra, muy fuerte)
        "bouwput": 0.95, "bouwplaats": 0.90, "in aanbouw": 0.88,
        "wegwerkzaamheden": 0.85, "graafwerk": 0.82,
        # DE
        "baustelle": 0.92, "im bau": 0.88, "bauarbeiten": 0.88,
        "erdarbeiten": 0.82, "ausgehoben": 0.82,
        # IT
        "cantiere": 0.90, "in costruzione": 0.88, "lavori in corso": 0.88,
        "scavo": 0.82, "lavori stradali": 0.85,
        # ── closure -> spot_closed=true ──────────────────────────────────────
        # EN
        "permanently closed": 0.95, "no longer exists": 0.92,
        "gates locked": 0.85, "access blocked": 0.85, "barrier closed": 0.82,
        # ES
        "cerrado permanentemente": 0.95, "ya no existe": 0.92,
        "barrera cerrada": 0.85, "acceso bloqueado": 0.85, "clausurado": 0.90,
        # FR
        "fermé définitivement": 0.95, "definitivement ferme": 0.95,
        "barriere fermee": 0.85, "acces bloque": 0.85, "condamne": 0.85,
        # NL
        "permanent gesloten": 0.95, "definitief gesloten": 0.95,
        "afgesloten": 0.85, "slagboom dicht": 0.82, "bestaat niet meer": 0.92,
        # DE
        "dauerhaft geschlossen": 0.95, "endgultig geschlossen": 0.95,
        "schranke geschlossen": 0.85, "gesperrt": 0.85, "abgeriegelt": 0.85,
        # IT
        "chiuso definitivamente": 0.95, "non esiste piu": 0.92,
        "sbarra chiusa": 0.82, "accesso bloccato": 0.85, "interdetto": 0.85,
    },
    # ── noise_source -> noise=0.8 ────────────────────────────────────────────
    ("noise", "0.8"): {
        # EN
        "motorway noise": 0.88, "highway noise": 0.88, "train noise": 0.85,
        "railway noise": 0.85, "traffic noise": 0.82, "noisy all night": 0.88,
        "factory noise": 0.85, "airport noise": 0.85,
        # ES
        "ruido de carretera": 0.85, "ruido de autopista": 0.88,
        "ruido de trenes": 0.85, "ruido de trafico": 0.82,
        "muy ruidoso": 0.85, "ruido toda la noche": 0.90,
        # FR
        "bruit de la route": 0.85, "bruit d'autoroute": 0.88,
        "bruit de train": 0.85, "tres bruyant": 0.85, "bruit toute la nuit": 0.90,
        # NL
        "verkeerslawaai": 0.88, "snelweglawaai": 0.88, "treinlawaai": 0.85,
        "erg lawaaierig": 0.85, "lawaai hele nacht": 0.90,
        # DE
        "verkehrslarm": 0.85, "autobahnlarm": 0.90, "bahnlarm": 0.85,
        "sehr laut": 0.85, "larm die ganze nacht": 0.90, "strassenlarm": 0.85,
        # IT
        "rumore della strada": 0.85, "rumore autostrada": 0.88,
        "rumore dei treni": 0.85, "molto rumoroso": 0.85,
        "rumore tutta la notte": 0.90,
    },
    # ── police_pressure -> police_risk=0.85 ──────────────────────────────────
    ("police_risk", "0.85"): {
        # EN
        "police moved us on": 0.92, "fined by police": 0.92, "got a fine": 0.88,
        "told to leave": 0.85, "kicked out": 0.88, "evicted": 0.88,
        "police came": 0.80,
        # ES
        "nos echo la policia": 0.92, "nos multaron": 0.92, "multa": 0.85,
        "guardia civil": 0.82, "nos echaron": 0.88, "vino la policia": 0.80,
        "prohibido pernoctar": 0.85,
        # FR
        "la police nous a fait partir": 0.92, "amende": 0.88,
        "gendarmerie": 0.82, "expulses": 0.88, "interdit de stationner": 0.85,
        # NL
        "politie stuurde ons weg": 0.92, "boete": 0.88, "weggestuurd": 0.85,
        "politie kwam": 0.80, "verboden te overnachten": 0.85,
        # DE
        "polizei schickte uns weg": 0.92, "bussgeld": 0.88, "strafe": 0.85,
        "weggeschickt": 0.85, "polizei kam": 0.80, "ubernachten verboten": 0.85,
        # IT
        "la polizia ci ha mandato via": 0.92, "multa": 0.85,
        "carabinieri": 0.82, "cacciati": 0.88, "vietato pernottare": 0.85,
    },
    # ── wild_camping (allowed) -> wild_camping_legal=true ────────────────────
    ("wild_camping_legal", "true"): {
        # EN
        "wild camping allowed": 0.88, "free camping allowed": 0.85,
        "legal to wild camp": 0.88, "overnight allowed": 0.82,
        # ES
        "acampada libre permitida": 0.88, "pernocta permitida": 0.85,
        "se puede acampar": 0.82, "acampada permitida": 0.85,
        # FR
        "camping sauvage autorise": 0.88, "bivouac autorise": 0.85,
        "stationnement autorise la nuit": 0.82,
        # NL
        "wildkamperen toegestaan": 0.88, "overnachten toegestaan": 0.82,
        "vrij kamperen mag": 0.85,
        # DE
        "wildcampen erlaubt": 0.88, "ubernachten erlaubt": 0.82,
        "frei stehen erlaubt": 0.85,
        # IT
        "campeggio libero consentito": 0.88, "pernottamento consentito": 0.82,
        "sosta libera consentita": 0.85,
    },
    # ── wild_camping (forbidden) -> wild_camping_legal=false ─────────────────
    ("wild_camping_legal", "false"): {
        # EN
        "wild camping forbidden": 0.90, "no wild camping": 0.88,
        "camping prohibited": 0.88, "no overnight": 0.85,
        # ES
        "acampada prohibida": 0.90, "prohibido acampar": 0.90,
        "camping prohibido": 0.88, "prohibido pernoctar": 0.88,
        # FR
        "camping sauvage interdit": 0.90, "bivouac interdit": 0.88,
        "stationnement interdit la nuit": 0.85, "interdit de camper": 0.90,
        # NL
        "wildkamperen verboden": 0.90, "kamperen verboden": 0.88,
        "overnachten verboden": 0.85,
        # DE
        "wildcampen verboten": 0.90, "campen verboten": 0.88,
        "ubernachten verboten": 0.85, "freistehen verboten": 0.88,
        # IT
        "campeggio libero vietato": 0.90, "vietato campeggiare": 0.90,
        "vietato pernottare": 0.88, "sosta vietata di notte": 0.85,
    },
}

# Pre-folded para no re-normalizar términos en cada llamada.
LEXICON: dict[tuple[str, str], dict[str, float]] = {
    sv: {_fold(term): prior for term, prior in terms.items()}
    for sv, terms in _RAW_LEXICON.items()
}


def covered_signals() -> set[str]:
    """Señales (sin value) cubiertas por el léxico. Útil para tests/diagnóstico."""
    return {signal for (signal, _value) in LEXICON}


def lexical_prior(text: str, signal: str, value: str) -> float | None:
    """Prior léxico [0,1] para (signal, value) si algún término aparece en `text`.

    Devuelve el prior MÁXIMO entre los términos que matchean (el más severo),
    o None si ningún término del léxico para esa (signal,value) está presente.
    """
    terms = LEXICON.get((signal, str(value)))
    if not terms:
        return None
    folded = _fold(text)
    best: float | None = None
    for term, prior in terms.items():
        if term in folded:
            if best is None or prior > best:
                best = prior
    return best


def blend_confidence(text: str, signal: str, value: str, llm_score: float) -> float:
    """Combina el prior léxico con el score del LLM/regex según D6.

    Si no hay prior (sin término cargado en el texto), devuelve `llm_score`
    inalterado. Si lo hay: `0.7*llm_score + 0.3*lexical_prior`, recortado a [0,1].
    """
    prior = lexical_prior(text, signal, value)
    if prior is None:
        return llm_score
    blended = D6_BLEND_LLM_WEIGHT * llm_score + LEXICON_BLEND_WEIGHT * prior
    return max(0.0, min(1.0, blended))


def apply_lexicon_blend(text: str, claims: list[dict]) -> list[dict]:
    """Re-pondera la confianza de los claims que matchean el léxico (in-place safe).

    Solo toca `confidence` de claims cuyo (signal, value) esté en el léxico y
    cuyo texto contenga un término cargado. NO añade ni elimina claims.
    Anota `lexicon_blended=True` en los claims afectados para trazabilidad.
    Devuelve la misma lista (mutada) por conveniencia.
    """
    for c in claims:
        signal = c.get("signal")
        value = c.get("value")
        if signal is None or value is None:
            continue
        prior = lexical_prior(text, str(signal), str(value))
        if prior is None:
            continue
        old = float(c.get("confidence", 0.7))
        c["confidence"] = max(0.0, min(1.0, D6_BLEND_LLM_WEIGHT * old + LEXICON_BLEND_WEIGHT * prior))
        c["lexicon_blended"] = True
    return claims
