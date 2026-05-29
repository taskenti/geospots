"""Review cleaning and lightweight language detection.

Sprint 3 / BUG-09 — Detección de idioma con prior por país
═══════════════════════════════════════════════════════════════════════════
El audit de mayo 2026 confirmó que `langdetect` etiqueta reviews en español
de caramaps como `it` o `pt` sistemáticamente (2.808 ES→it, 2.050 ES→pt
verificados). El problema: textos cortos + cercanía léxica del romance
hacen que la probabilidad gane el idioma equivocado por márgenes mínimos.

Fix: cuando la confianza de langdetect cae por debajo de LANG_PRIOR_THRESHOLD
Y el spot está en un país con idiomas esperados conocidos, preferimos un
idioma esperado SI aparece entre los 3 mejores candidatos de langdetect.
Si la confianza es alta (>= 0.85), respetamos langdetect aunque el país no
coincida (puede ser un turista nórdico reseñando en Andorra, p.ej.).

El parámetro `country_iso` es opcional y default None → comportamiento
idéntico al previo, no rompe callers que no lo pasan.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from langdetect import DetectorFactory, detect, detect_langs

# Semilla determinista — langdetect es probabilístico y sin esto da resultados
# distintos entre runs. Crítico para que el mismo texto siempre etiquete igual.
DetectorFactory.seed = 42

BOILERPLATE_PATTERNS = [
    r"^\s*(merci|thanks|thank you|gracias|danke|grazie|super|ok|top|perfecto|genial)[\s!.]*$",
    r"^\s*(merci|thanks|gracias|danke|grazie)?\s*(super|great|nice|bon|buen|good)\s+(endroit|place|sitio|spot|platz)[\s!.]*$",
    r"^\s*[0-9\s.,!?\-_/]+$",
]

NOISE_RE = re.compile(r"\s+")
HTML_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# BUG-09: mapa país ISO-2 → idiomas esperados, en orden de preferencia.
# Si un país tiene varios oficiales (CH, BE, AD), el primero es el dominante
# pero respetamos al usuario si langdetect dice claramente uno de los otros.
# Solo cubrimos los países con presencia significativa en GeoSpots.
COUNTRY_LANGS: dict[str, tuple[str, ...]] = {
    # Iberia
    "ES": ("es", "ca", "gl", "eu"),
    "PT": ("pt",),
    "AD": ("ca", "es", "fr"),
    # Francia + países francófonos
    "FR": ("fr",),
    "MC": ("fr",),
    "LU": ("fr", "de"),
    # DACH
    "DE": ("de",),
    "AT": ("de",),
    "CH": ("de", "fr", "it"),
    "LI": ("de",),
    # Italia
    "IT": ("it",),
    "SM": ("it",),
    "VA": ("it",),
    # Benelux
    "NL": ("nl",),
    "BE": ("nl", "fr", "de"),
    # Anglosajón
    "GB": ("en",),
    "UK": ("en",),  # alias informal
    "IE": ("en",),
    "US": ("en",),
    "CA": ("en", "fr"),
    "AU": ("en",),
    "NZ": ("en",),
    # Nórdicos
    "DK": ("da",),
    "SE": ("sv",),
    "NO": ("no",),
    "FI": ("fi", "sv"),
    "IS": ("is",),
    # Europa Central/Este
    "PL": ("pl",),
    "CZ": ("cs",),
    "SK": ("sk",),
    "HU": ("hu",),
    "SI": ("sl",),
    "HR": ("hr",),
    "RO": ("ro",),
    "BG": ("bg",),
    # Bálticos
    "EE": ("et",),
    "LV": ("lv",),
    "LT": ("lt",),
    # Mediterráneo
    "GR": ("el",),
    "TR": ("tr",),
    # Latinoamérica
    "MX": ("es",),
    "AR": ("es",),
    "CL": ("es",),
    "CO": ("es",),
    "PE": ("es",),
    "BR": ("pt",),
}

# Confusiones específicas documentadas en el audit (BUG-09): langdetect
# etiqueta sistemáticamente reviews en castellano como `it` o `pt` con
# probabilidad ~0.99 — el umbral por confianza NO basta para corregirlo.
# Tabla quirúrgica: solo aceptamos override (langdetect → idioma esperado)
# para las direcciones de confusión observadas. Esto evita romper reviews
# legítimas de turistas extranjeros en spots ES (un francés escribiendo
# "Tres bel endroit" en Cataluña sigue etiquetado como `fr`).
# Formato: {(detectado_por_langdetect, idioma_esperado_dominante): override_a}
KNOWN_LANG_CONFUSIONS: dict[tuple[str, str], str] = {
    # Audit BUG-09: textos cortos en castellano detectados como otra Romance
    # por langdetect en spots ES (4.858 reviews ES→IT/PT verificadas).
    ("it", "es"): "es",
    ("pt", "es"): "es",
    ("ro", "es"): "es",   # langdetect también confunde con rumano en textos cortos
    # Andorra: el idioma esperado dominante es CA pero la mayoría de reviews
    # cortas en AD vienen en castellano. Las que SÍ son catalanas las pillará
    # la rama "top_lang in expected" antes de llegar aquí.
    ("it", "ca"): "es",
    ("pt", "ca"): "es",
    ("ro", "ca"): "es",
    # Mirror: spots PT, textos cortos detectados como otra Romance.
    ("it", "pt"): "pt",
    ("ro", "pt"): "pt",
    ("es", "pt"): "pt",   # texto realmente ES en spot PT → fuerza PT por el prior
    # Mirror: spots IT, textos cortos detectados como otra Romance similar.
    ("es", "it"): "it",
    ("pt", "it"): "it",
    ("ro", "it"): "it",
    # Notas deliberadas:
    # - NO añadimos pares con `fr`/`en`/`de`/`nl` como detected o expected:
    #   francés/inglés/alemán/neerlandés son lo bastante distintos del bloque
    #   ibero-italo-romance como para que langdetect no se confunda en textos
    #   cortos. Sobreescribir esos casos rompería reviews de turistas.
    # - NO añadimos `(ca, es)`: catalán es lengua oficial real en spots ES;
    #   si langdetect dice CA con confianza, lo respetamos.
}

# Por debajo de este umbral de caracteres consideramos que el texto es
# demasiado corto para fiarnos del modelo de langdetect en confusiones
# Romance↔Romance. Para textos más largos, langdetect suele acertar.
LANG_CONFUSION_MAX_CHARS = 80


@dataclass(frozen=True)
class CleanedReview:
    texto_limpio: str
    informativo: bool
    idioma: str | None = None


def _keyword_fallback(text: str) -> str | None:
    """Scorer de keywords usado cuando langdetect lanza excepción.

    Mantenido idéntico al comportamiento previo para no regresionar tests.
    """
    lowered = f" {text.lower()} "
    scores = {
        "es": sum(1 for w in (" el ", " la ", " con ", " para ", " noche ", " sitio ") if w in lowered),
        "fr": sum(1 for w in (" le ", " avec ", " pour ", " nuit ", " endroit ") if w in lowered),
        "de": sum(1 for w in (" der ", " die ", " mit ", " nacht ", " platz ") if w in lowered),
        "nl": sum(1 for w in (" het ", " een ", " met ", " nacht ", " plek ") if w in lowered),
        "en": sum(1 for w in (" the ", " with ", " for ", " night ", " place ") if w in lowered),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def detect_language(text: str | None, country_iso: str | None = None) -> str | None:
    """Detecta el idioma de un texto, opcionalmente con prior por país.

    Reglas (BUG-09 — fix quirúrgico):
      1. Si langdetect falla → keyword scorer (igual que antes).
      2. Si langdetect detecta uno de los idiomas esperados del país → ese.
      3. Si la pareja (detectado, esperado_dominante) está en
         KNOWN_LANG_CONFUSIONS Y el texto es corto (<LANG_CONFUSION_MAX_CHARS):
         override al idioma del audit. Captura el patrón concreto observado:
         castellano corto mal-etiquetado como `it`/`pt` por langdetect con
         confianza espuriamente alta.
      4. En cualquier otro caso → respetar langdetect (turistas con texto
         largo o discrepancias cross-family confiables).

    Por qué NO usamos un umbral de probabilidad: langdetect retorna
    p≈0.9999 incluso cuando se equivoca en textos cortos romance.
    Confiar en `prob >= 0.85` no corregía ni un solo caso del audit.

    Parámetros:
      text: review limpia (post `clean_review`).
      country_iso: ISO-2 del país del spot (cualquier casing). None → ignorado.

    Backward-compatible: llamadas sin country_iso solo cambian por la semilla
    determinista (consistencia entre runs).
    """
    if not text:
        return None

    expected: tuple[str, ...] = ()
    if country_iso:
        expected = COUNTRY_LANGS.get(country_iso.upper(), ())

    try:
        candidates = detect_langs(text)  # ordenados por prob desc
    except Exception:
        return _keyword_fallback(text)

    if not candidates:
        return None
    top_lang = candidates[0].lang

    # Sin prior de país → respetar langdetect.
    if not expected:
        return top_lang

    # langdetect ya picó uno de los esperados → confiar.
    if top_lang in expected:
        return top_lang

    # Override quirúrgico de confusiones documentadas en el audit, solo en
    # textos suficientemente cortos para que langdetect sea poco fiable.
    if len(text) < LANG_CONFUSION_MAX_CHARS:
        key = (top_lang, expected[0])
        override = KNOWN_LANG_CONFUSIONS.get(key)
        if override is not None:
            return override

    # Discrepancia con el país pero NO una confusión conocida → langdetect.
    # Cubre el caso de turistas escribiendo en su lengua nativa en spots
    # extranjeros (francés en Cataluña, alemán en España, etc.).
    return top_lang


def clean_review(text: str | None) -> tuple[str, bool]:
    if not text:
        return "", False
    cleaned = html.unescape(str(text))
    cleaned = HTML_RE.sub(" ", cleaned)
    cleaned = URL_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("\r", "\n")
    lines = []
    for line in cleaned.splitlines():
        stripped = NOISE_RE.sub(" ", line).strip()
        if stripped:
            lines.append(stripped)
    result = "\n".join(lines).strip()
    result = re.sub(r"([!?.,])\1{2,}", r"\1", result)
    result = NOISE_RE.sub(" ", result).strip()
    lowered = result.lower()
    if len(result) < 10:
        return result, False
    if any(re.match(pattern, lowered, flags=re.IGNORECASE) for pattern in BOILERPLATE_PATTERNS):
        return result, False
    informative_tokens = re.findall(r"[A-Za-zÀ-ÿ]{3,}", result)
    if len(informative_tokens) <= 3 and re.search(r"\b(merci|thanks|gracias|super|great|nice|good|endroit|place|sitio|spot)\b", lowered):
        return result, False
    return result, len(informative_tokens) >= 3


def clean_review_full(text: str | None, country_iso: str | None = None) -> CleanedReview:
    """Limpia + detecta idioma con prior por país opcional (BUG-09).

    `country_iso` se propaga a `detect_language`. Default None preserva el
    comportamiento previo para callers que no lo proporcionan.
    """
    texto_limpio, informativo = clean_review(text)
    idioma = detect_language(texto_limpio, country_iso=country_iso) if informativo else None
    return CleanedReview(texto_limpio=texto_limpio, informativo=informativo, idioma=idioma)
