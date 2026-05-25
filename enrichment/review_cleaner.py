"""Review cleaning and lightweight language detection."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

BOILERPLATE_PATTERNS = [
    r"^\s*(merci|thanks|thank you|gracias|danke|grazie|super|ok|top|perfecto|genial)[\s!.]*$",
    r"^\s*(merci|thanks|gracias|danke|grazie)?\s*(super|great|nice|bon|buen|good)\s+(endroit|place|sitio|spot|platz)[\s!.]*$",
    r"^\s*[0-9\s.,!?\-_/]+$",
]

NOISE_RE = re.compile(r"\s+")
HTML_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


@dataclass(frozen=True)
class CleanedReview:
    texto_limpio: str
    informativo: bool
    idioma: str | None = None


def detect_language(text: str | None) -> str | None:
    if not text:
        return None
    try:
        from langdetect import detect

        return detect(text)
    except Exception:
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


def clean_review_full(text: str | None) -> CleanedReview:
    texto_limpio, informativo = clean_review(text)
    idioma = detect_language(texto_limpio) if informativo else None
    return CleanedReview(texto_limpio=texto_limpio, informativo=informativo, idioma=idioma)
