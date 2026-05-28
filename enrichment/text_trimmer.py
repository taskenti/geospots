"""Strip social filler from review text before sending to LLM.

El objetivo es reducir tokens malgastados en frases sin contenido semántico
(agradecimientos, despedidas, recomendaciones genéricas) mientras se preserva
todo el contenido que puede generar claims.

Uso:
    from .text_trimmer import trim_for_llm
    trimmed = trim_for_llm(original_text)

La función es idempotente y nunca lanza excepciones. Si el resultado queda
demasiado corto (< MIN_REMAINING_CHARS), devuelve el texto original para no
perder potenciales claims.
"""

from __future__ import annotations

import re

# Longitud mínima del texto post-trim para aceptar el resultado.
# Si tras el trim el texto queda muy corto podría ser que las "frases filler"
# eran en realidad todo el contenido útil → devolver original.
MIN_REMAINING_CHARS = 40

# ── Patrones de frases filler ─────────────────────────────────────────────────
# Cada patrón es una regex que matchea una frase filler completa (sin ^ / $)
# con anchors \b o delimitadores de frase para evitar falsos positivos.
# re.IGNORECASE aplicado a todos.

_FILLER_PATTERNS: list[str] = [
    # Agradecimientos genéricos al municipio / propietario
    r"(?:muchas?\s+)?gracias\s+(?:al\s+)?(?:municipio|ayuntamiento|pueblo|town|propietario|owner|gestores?|al\s+municipio)[^.!?]*[.!?]?",
    r"thank\s+you\s+(?:to\s+the\s+)?(?:town|municipality|owner|council)[^.!?]*[.!?]?",
    r"merci\s+(?:à\s+la\s+)?(?:mairie|municipalité|commune|propriétaire)[^.!?]*[.!?]?",
    r"danke\s+(?:an\s+die\s+)?(?:gemeinde|stadt|besitzer)[^.!?]*[.!?]?",

    # "Volveremos" y variantes
    r"(?:sin\s+duda\s+)?(?:volver(?:emos|é|á)|we(?:'ll|\s+will)\s+(?:be\s+back|return|come\s+back)|on\s+revien(?:dra|t)|wir\s+(?:kommen\s+)?wieder)[^.!?]*[.!?]?",

    # Recomendaciones sin contenido
    r"(?:muy\s+)?recomend(?:ado|able|amos|amos\s+totalmente)[.!]?",
    r"(?:highly\s+)?recommend(?:ed|able)?[.!]?",
    r"(?:je\s+)?recommande(?:\s+vivement)?[.!]?",
    r"empfehlenswert[.!]?",

    # Despedidas / saludos de cierre
    r"(?:un\s+)?saludo[s]?[.!]?",
    r"hasta\s+(?:la\s+próxima|pronto|otra)[.!]?",
    r"see\s+you\s+(?:again|next\s+time)[.!]?",
    r"bonne\s+route[.!]?",
    r"gute\s+reise[.!]?",

    # Frases de visita completamente vacías
    r"(?:una\s+)?visita\s+(?:muy\s+)?agradable[.!]?",
    r"(?:a\s+)?nice\s+(?:little\s+)?(?:stop|place|spot)[.!]?",
    r"(?:un\s+)?buen\s+(?:lugar|sitio)\s+para\s+parar[.!]?",

    # Solo "5 estrellas" o "10/10" sin contexto
    r"\b[5⭐]{1,5}\s*(?:estrellas|stars|étoiles|sterne)\b[.!]?",
    r"\b10\s*/\s*10\b[.!]?",

    # "Como siempre" / "de siempre" vacíos
    r"como\s+siempre[,.]?",

    # Texto de metadatos de plataforma que a veces se cuela
    r"(?:fuente|source|via)\s*:\s*\S+",
    r"google\s+maps\s+review",
    r"posted\s+(?:on|via)\s+\S+",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _FILLER_PATTERNS]

# Patrón para colapsar espacios múltiples / saltos de línea tras el strip
_WHITESPACE_RE = re.compile(r"\s{2,}")


def trim_for_llm(text: str) -> str:
    """Elimina frases filler del texto y devuelve versión compacta para LLM.

    Garantías:
    - Nunca devuelve texto más corto que MIN_REMAINING_CHARS si el original
      era más largo (evita eliminar contenido real).
    - Si el texto original es corto (≤ MIN_REMAINING_CHARS), lo devuelve tal cual.
    - Idempotente.
    """
    if not text or len(text) <= MIN_REMAINING_CHARS:
        return text

    trimmed = text
    for pattern in _COMPILED:
        trimmed = pattern.sub(" ", trimmed)

    # Limpiar espacios sobrantes
    trimmed = _WHITESPACE_RE.sub(" ", trimmed).strip()

    # Salvaguarda: si el resultado queda muy corto, es señal de que eliminamos
    # demasiado → devolver original.
    if len(trimmed) < MIN_REMAINING_CHARS:
        return text.strip()

    return trimmed


def trim_ratio(text: str) -> float:
    """Devuelve la fracción de texto eliminado (0.0 = nada, 1.0 = todo).
    Útil para logging y métricas.
    """
    if not text:
        return 0.0
    trimmed = trim_for_llm(text)
    return 1.0 - len(trimmed) / len(text)
