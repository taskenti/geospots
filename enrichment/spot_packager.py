"""Selection and packaging of reviews for spot-level v2 enrichment.

Responsabilidades:
1. `temporal_weight(fecha)` — decay para priorización.
2. `select_reviews_for_prompt(reviews, max_tokens)` — top-N por peso, respeta budget.
3. `has_rich_description(spot)` — ¿procesar spot sin reviews pero con texto util?
4. `estimate_tokens(text)` — aproximación (sin tiktoken para no añadir dependencia).
5. `fetch_spot_with_reviews(conn, spot_id)` — query estándar para alimentar el prompt.

NO llama al LLM. NO escribe en DB. Solo selecciona y formatea.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable

# Budget conservador: ~3.500 tokens útiles para reviews+descripciones.
# El resto va a system prompt cacheado + user prompt frame.
DEFAULT_MAX_REVIEW_TOKENS = 3500
# Texto por review en el prompt: recortar a este largo (chars).
REVIEW_TEXT_CHAR_LIMIT = 400
# Mínimo de reviews para considerar enriquecimiento (también puede aceptarse con descripción rica).
MIN_REVIEWS_FOR_ENRICHMENT = 3
# Mínimo de chars en descripciones reconciliadas para procesar spot sin reviews.
MIN_DESCRIPTION_CHARS = 200


def temporal_weight(fecha) -> float:
    """Peso temporal de una review por edad.

    Acepta `date`, `datetime` (tz-aware o naive) o `None`.
    Returns 0.0-1.0.
    """
    if fecha is None:
        return 0.3  # Fecha desconocida → peso bajo pero no nulo

    if isinstance(fecha, datetime):
        dt = fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)
    elif isinstance(fecha, date):
        dt = datetime(fecha.year, fecha.month, fecha.day, tzinfo=timezone.utc)
    else:
        return 0.3

    age_days = (datetime.now(timezone.utc) - dt).days
    if age_days < 0:        return 1.0   # Fecha en el futuro (raro) — tratar como muy reciente
    if age_days < 365:      return 1.0
    if age_days < 730:      return 0.8
    if age_days < 1095:     return 0.5
    if age_days < 1825:     return 0.3
    return 0.1


def estimate_tokens(text: str | None) -> int:
    """Aproximación rápida: ~4 chars por token para texto latino.

    No usa tiktoken (no aplica a Gemini de todos modos). Es un estimador
    conservador para no superar el budget del prompt.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def _review_text(r: dict) -> str:
    """Devuelve el mejor texto disponible de una review."""
    return (r.get("texto_limpio") or r.get("texto") or r.get("texto_original") or "").strip()


def _truncate(text: str, max_chars: int = REVIEW_TEXT_CHAR_LIMIT) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def select_reviews_for_prompt(
    reviews: Iterable[dict],
    max_tokens: int = DEFAULT_MAX_REVIEW_TOKENS,
    text_char_limit: int = REVIEW_TEXT_CHAR_LIMIT,
) -> list[dict]:
    """Selecciona reviews para el prompt v2 respetando un budget de tokens.

    Orden de prioridad:
      1. Peso temporal (decay) — desc
      2. Rating disponible — desc (los extremos suelen ser más informativos)
      3. Longitud de texto — desc (textos cortos casi nunca aportan)

    Devuelve la lista (puede ser vacía) con `texto_limpio` recortado a
    `text_char_limit` chars para que entre en el budget.
    """
    candidates: list[tuple[float, int, int, dict]] = []
    for r in reviews:
        text = _review_text(r)
        if not text or len(text) < 4:
            continue
        weight = temporal_weight(r.get("fecha"))
        if weight <= 0:
            continue
        rating = float(r.get("rating") or 0.0)
        candidates.append((weight, int(rating * 10), len(text), {**r, "texto_limpio": text}))

    # Orden estable: por peso, luego rating, luego longitud (todos desc).
    candidates.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)

    selected: list[dict] = []
    tokens_used = 0
    for _w, _r, _l, r in candidates:
        truncated_text = _truncate(r["texto_limpio"], text_char_limit)
        line_tokens = estimate_tokens(truncated_text) + 20  # 20 tokens overhead por header
        if tokens_used + line_tokens > max_tokens:
            # Si todavía no hemos seleccionado nada, fuerza al menos 1 para no descartar el spot
            if not selected:
                r2 = dict(r)
                r2["texto_limpio"] = _truncate(r["texto_limpio"], text_char_limit // 2)
                selected.append(r2)
            break
        r2 = dict(r)
        r2["texto_limpio"] = truncated_text
        selected.append(r2)
        tokens_used += line_tokens

    return selected


def has_rich_description(spot: dict, min_chars: int = MIN_DESCRIPTION_CHARS) -> bool:
    """¿El spot tiene descripciones suficientes para enriquecer sin reviews?"""
    total = 0
    for lang in ("es", "en", "fr", "de", "it", "nl", "pt"):
        text = spot.get(f"descripcion_{lang}") or ""
        total += len(text.strip())
        if total >= min_chars:
            return True
    return False


def should_enrich(spot: dict, n_reviews: int) -> tuple[bool, str]:
    """Decide si un spot es candidato para v2 enrichment.

    Returns (decision, reason).
    """
    if n_reviews >= MIN_REVIEWS_FOR_ENRICHMENT:
        return True, f"has_{n_reviews}_reviews"
    if has_rich_description(spot):
        return True, "rich_description_only"
    return False, f"insufficient_signal (reviews={n_reviews})"


# ─────────────────────────────────────────────────────────────────────
# Helpers de DB (opcional — pueden vivir aquí o en scraper/db.py)
# ─────────────────────────────────────────────────────────────────────


async def fetch_spot_for_enrichment(conn, spot_id: int) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT id, canonical_name, slug, lat, lon, country_iso, tipo, subtipo,
               fuentes, total_reviews, master_rating,
               descripcion_es, descripcion_en, descripcion_fr, descripcion_de,
               descripcion_it, descripcion_nl, descripcion_pt,
               -- v3: servicios reconciliados (hechos estructurados de las fuentes)
               gratuito, precio_aprox, precio_info,
               agua_potable, vaciado_negras, vaciado_grises, electricidad,
               ducha, wifi, wc_publico,
               acceso_grandes, num_plazas, altura_max_m, temporada_apertura
        FROM spots
        WHERE id = $1 AND activo = TRUE
        """,
        spot_id,
    )
    return dict(row) if row else None


async def fetch_reviews_for_enrichment(conn, spot_id: int, hard_limit: int = 60) -> list[dict]:
    """Trae candidatas de DB ordenadas por fecha desc (luego packager las re-ordena).

    `hard_limit` corta antes del prompt para no traer 500 reviews innecesariamente.
    Si un spot tiene >60 reviews, las 60 más recientes son suficientes
    (el packager además aplica decay y trunca a ~10-15).
    """
    rows = await conn.fetch(
        """
        SELECT id, spot_id, source, source_review_id, texto, texto_original, texto_limpio,
               rating, autor, fecha, idioma, llm_processed
        FROM reviews
        WHERE spot_id = $1
          AND COALESCE(texto_limpio, texto, texto_original) IS NOT NULL
          AND length(COALESCE(texto_limpio, texto, texto_original)) > 3
        ORDER BY fecha DESC NULLS LAST, id DESC
        LIMIT $2
        """,
        spot_id,
        hard_limit,
    )
    return [dict(r) for r in rows]
