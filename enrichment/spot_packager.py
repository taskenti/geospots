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

# v4: dedup config (conservador — solo para casi-duplicados literales/semánticos)
DEDUP_MIN_REVIEWS = 15            # No aplica dedup en spots pequeños — cada review puede ser señal única
DEDUP_EMBEDDING_THRESHOLD = 0.90  # Cosine ≥ 0.90 = casi idéntico semánticamente (cross-language safe)
DEDUP_JACCARD_THRESHOLD = 0.85    # Fallback si embeddings no disponibles
DEDUP_LEN_RATIO = 0.5             # Ambas reviews longitud similar (ratio min/max) — laxo para cross-lang

# Modelo de embeddings multilingual ligero (~250MB, 50+ idiomas).
# Lazy loaded en _get_embedding_model() para no romper imports si la lib no está.
EMBEDDING_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


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


# ─────────────────────────────────────────────────────────────────────
# v4: Dedup conservador — elimina copias literales casi exactas
# ─────────────────────────────────────────────────────────────────────
# Estrategia: embeddings multilingual (cross-language) como path principal.
# Fallback a Jaccard si sentence-transformers no está instalado.
# Conservador por diseño: solo elimina casi-duplicados (threshold 0.90+),
# no toca spots con <15 reviews, mantiene la review más reciente del cluster.

_EMBEDDING_MODEL = None
_EMBEDDING_BACKEND = None  # "sentence-transformers" | "jaccard-fallback" | None (no probado todavía)


def _get_embedding_model():
    """Lazy load del modelo multilingual. Devuelve None si la lib no está instalada.

    Esto permite que el sistema funcione sin sentence-transformers, cayendo a Jaccard.
    """
    global _EMBEDDING_MODEL, _EMBEDDING_BACKEND
    if _EMBEDDING_BACKEND is not None:
        return _EMBEDDING_MODEL  # ya probado (puede ser None si falló)

    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        from loguru import logger
        logger.info(f"[spot_packager] cargando modelo embeddings {EMBEDDING_MODEL_NAME}...")
        _EMBEDDING_MODEL = SentenceTransformer(EMBEDDING_MODEL_NAME)
        _EMBEDDING_BACKEND = "sentence-transformers"
        logger.info("[spot_packager] modelo embeddings listo")
    except Exception as exc:
        from loguru import logger
        logger.warning(
            f"[spot_packager] sentence-transformers no disponible ({exc}). "
            "Dedup caerá a Jaccard. Instala con: pip install sentence-transformers"
        )
        _EMBEDDING_MODEL = None
        _EMBEDDING_BACKEND = "jaccard-fallback"
    return _EMBEDDING_MODEL


def _tokenize_for_jaccard(text: str) -> set[str]:
    """Tokenización simple para Jaccard (fallback). Tokens >2 chars."""
    if not text:
        return set()
    import re
    cleaned = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    return {t for t in cleaned.split() if len(t) > 2}


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def _which_kept(reviews: list[dict], i: int, j: int, len_i: int, len_j: int) -> int:
    """Decide cuál de dos reviews duplicadas conservar.

    Prioridad: review más reciente > más larga > la primera por orden de entrada.
    Devuelve el índice (i o j) que se queda; el otro se descarta.
    """
    ri, rj = reviews[i], reviews[j]
    fi, fj = ri.get("fecha"), rj.get("fecha")
    if fi and fj:
        return i if fi >= fj else j
    if fi and not fj:
        return i
    if fj and not fi:
        return j
    # Sin fechas: la más larga
    if len_i != len_j:
        return i if len_i >= len_j else j
    return i  # tiebreak: orden de entrada


def _dedup_via_embeddings(
    reviews: list[dict],
    *,
    threshold: float,
    len_ratio: float,
    model,
) -> tuple[list[dict], int, list[dict]]:
    """Dedup usando embeddings multilingüe. Cross-language safe.

    Devuelve (kept, n_discarded, debug_info).
    """
    texts = [_review_text(r) for r in reviews]
    lens = [len(t) for t in texts]
    # Embeddings batch (rápido). Normalizar para cosine = dot product.
    embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    n = len(reviews)
    discarded: set[int] = set()
    debug: list[dict] = []

    for i in range(n):
        if i in discarded:
            continue
        if not texts[i]:
            discarded.add(i)
            continue
        for j in range(i + 1, n):
            if j in discarded:
                continue
            if not texts[j]:
                continue
            # Filtro rápido por ratio de longitud
            li, lj = lens[i], lens[j]
            if li and lj:
                shorter, longer = (li, lj) if li <= lj else (lj, li)
                if shorter / longer < len_ratio:
                    continue
            # Cosine similarity (vectores ya normalizados)
            sim = float((embs[i] * embs[j]).sum())
            if sim >= threshold:
                keep = _which_kept(reviews, i, j, li, lj)
                drop = j if keep == i else i
                discarded.add(drop)
                debug.append({
                    "kept_review_id": reviews[keep].get("id"),
                    "dropped_review_id": reviews[drop].get("id"),
                    "similarity": round(sim, 3),
                })
                if drop == i:
                    break  # i ya no entra al kept set

    kept = [reviews[k] for k in range(n) if k not in discarded]
    return kept, n - len(kept), debug


def _dedup_via_jaccard(
    reviews: list[dict],
    *,
    threshold: float,
    len_ratio: float,
) -> tuple[list[dict], int, list[dict]]:
    """Dedup vía Jaccard (fallback si embeddings no disponibles)."""
    tokens_list = [_tokenize_for_jaccard(_review_text(r)) for r in reviews]
    lens = [len(_review_text(r)) for r in reviews]
    n = len(reviews)
    discarded: set[int] = set()
    debug: list[dict] = []

    for i in range(n):
        if i in discarded:
            continue
        ti = tokens_list[i]
        if not ti:
            discarded.add(i)
            continue
        for j in range(i + 1, n):
            if j in discarded:
                continue
            tj = tokens_list[j]
            if not tj:
                continue
            li, lj = lens[i], lens[j]
            if li and lj:
                shorter, longer = (li, lj) if li <= lj else (lj, li)
                if shorter / longer < len_ratio:
                    continue
            sim = _jaccard(ti, tj)
            if sim >= threshold:
                keep = _which_kept(reviews, i, j, li, lj)
                drop = j if keep == i else i
                discarded.add(drop)
                debug.append({
                    "kept_review_id": reviews[keep].get("id"),
                    "dropped_review_id": reviews[drop].get("id"),
                    "similarity": round(sim, 3),
                })
                if drop == i:
                    break

    kept = [reviews[k] for k in range(n) if k not in discarded]
    return kept, n - len(kept), debug


def dedup_reviews(
    reviews: list[dict],
    *,
    min_reviews: int = DEDUP_MIN_REVIEWS,
    embedding_threshold: float = DEDUP_EMBEDDING_THRESHOLD,
    jaccard_threshold: float = DEDUP_JACCARD_THRESHOLD,
    len_ratio: float = DEDUP_LEN_RATIO,
    force_jaccard: bool = False,
) -> tuple[list[dict], int]:
    """Dedup conservador de reviews casi-idénticas.

    Estrategia:
      - Si len(reviews) < min_reviews → no toca nada (spots pequeños intactos).
      - Embeddings multilingual (sentence-transformers MiniLM) si disponible.
        Threshold 0.90: pilla copias exactas, traducciones casi-literales,
        casi-paráfrasis cross-language; NO pilla reviews distintas con tema similar.
      - Fallback Jaccard si la lib no está instalada (threshold 0.85).
      - De un cluster duplicado, mantiene la review más reciente; tiebreak por longitud.
      - Filtra pares con longitudes muy distintas (len_ratio) — evita meter review
        corta dentro de review larga.

    Devuelve (reviews_dedupadas, n_descartadas).
    """
    if len(reviews) < min_reviews:
        return list(reviews), 0

    model = None if force_jaccard else _get_embedding_model()

    if model is not None:
        kept, n_discarded, _ = _dedup_via_embeddings(
            list(reviews),
            threshold=embedding_threshold,
            len_ratio=len_ratio,
            model=model,
        )
    else:
        kept, n_discarded, _ = _dedup_via_jaccard(
            list(reviews),
            threshold=jaccard_threshold,
            len_ratio=len_ratio,
        )

    return kept, n_discarded


# Compat: alias por si algún test importa el nombre antiguo
def dedup_reviews_by_jaccard(reviews: list[dict], **kwargs) -> tuple[list[dict], int]:
    """Compat alias — fuerza Jaccard (sin embeddings)."""
    kwargs.setdefault("force_jaccard", True)
    return dedup_reviews(reviews, **kwargs)


def select_reviews_for_prompt(
    reviews: Iterable[dict],
    max_tokens: int = DEFAULT_MAX_REVIEW_TOKENS,
    text_char_limit: int = REVIEW_TEXT_CHAR_LIMIT,
    apply_dedup: bool = True,
) -> list[dict]:
    """Selecciona reviews para el prompt v2/v4 respetando un budget de tokens.

    Orden de prioridad:
      1. Peso temporal (decay) — desc
      2. Rating disponible — desc (los extremos suelen ser más informativos)
      3. Longitud de texto — desc (textos cortos casi nunca aportan)

    v4: aplica dedup conservador antes de la selección (solo si len(reviews) >= 15
    y solo elimina casi-idénticos).

    Devuelve la lista (puede ser vacía) con `texto_limpio` recortado a
    `text_char_limit` chars para que entre en el budget.
    """
    reviews_list = list(reviews)
    if apply_dedup:
        reviews_list, n_dropped = dedup_reviews(reviews_list)
        if n_dropped > 0:
            from loguru import logger
            logger.debug(f"[spot_packager] dedup: {n_dropped} duplicados eliminados de {len(reviews_list) + n_dropped}")

    candidates: list[tuple[float, int, int, dict]] = []
    for r in reviews_list:
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


# ─────────────────────────────────────────────────────────────────────
# v4d: Richness adaptativo del summary
# ─────────────────────────────────────────────────────────────────────
# El summary debe ser proporcional a la cantidad de datos disponibles.
# Spots simples (parking de pueblo con 3 reviews) → 2 frases.
# Spots ricos (camping con 30 servicios + 60 reviews multi-lang) → 6-8 frases.

_RICHNESS_SERVICE_FIELDS = (
    "gratuito", "precio_aprox", "precio_info",
    "agua_potable", "vaciado_negras", "vaciado_grises", "electricidad",
    "ducha", "wifi", "wc_publico", "acceso_grandes",
    "num_plazas", "altura_max_m", "temporada_apertura",
    "perros", "iluminacion", "seguridad", "reserva_req",
    "web", "telefono", "email",
)
_RICHNESS_LANGS = ("es", "en", "fr", "de", "it", "nl", "pt")

# Umbrales del bucket (score 0.0-1.0)
# Calibración basada en distribución real:
#  - minimal: spot fantasma (<0.10) — 0 reviews, 0 servicios
#  - simple:  caso común (parking pequeño con 3-5 reviews, pocos servicios)
#  - medium:  area_ac típica (10-20 reviews, servicios básicos completos)
#  - rich:    camping con muchos servicios y reviews (15+ servicios)
#  - very_rich: corner cases (30+ reviews + 15+ servicios + multilang + 3+ fuentes)
RICHNESS_LEVELS = (
    ("minimal",   0.00),
    ("simple",    0.10),
    ("medium",    0.30),
    ("rich",      0.55),
    ("very_rich", 0.80),
)


def compute_richness(spot: dict, selected_reviews: list[dict]) -> tuple[float, str]:
    """Score 0-1 + bucket de riqueza del spot.

    Pondera: reviews seleccionadas (post-dedup), servicios no-null,
    descripciones por idioma, número de fuentes. Saturación logarítmica para que
    spots con valores extremos no dominen.

    Returns (score, level) where level ∈ {minimal, simple, medium, rich, very_rich}.
    """
    n_reviews = len(selected_reviews)
    n_services = sum(
        1 for f in _RICHNESS_SERVICE_FIELDS
        if spot.get(f) is not None and spot.get(f) != ""
    )
    n_descriptions = sum(
        1 for lang in _RICHNESS_LANGS
        if (spot.get(f"descripcion_{lang}") or "").strip()
    )
    fuentes = spot.get("fuentes") or []
    if isinstance(fuentes, str):
        fuentes = [fuentes]
    n_sources = len(fuentes)

    score = (
        min(n_reviews / 20.0, 1.0)      * 0.50  # 20+ reviews → max contribución
        + min(n_services / 15.0, 1.0)   * 0.30  # 15+ servicios → max
        + min(n_descriptions / 4.0, 1.0) * 0.10  # 4+ idiomas → max
        + min(n_sources / 3.0, 1.0)      * 0.10  # 3+ fuentes → max
    )
    score = round(score, 3)

    level = "minimal"
    for name, threshold in RICHNESS_LEVELS:
        if score >= threshold:
            level = name
    return score, level


# Instrucciones de longitud por bucket — el LLM las verá en el user prompt
_RICHNESS_INSTRUCTIONS = {
    "minimal":
        "Generate a VERY SHORT summary (1-2 sentences). Data is limited "
        "(few reviews, few services). Stick to what's directly stated.",
    "simple":
        "Generate a 2-3 sentence summary. Focus on the most relevant facts.",
    "medium":
        "Generate a 3-5 sentence summary covering services, atmosphere, "
        "and any notable considerations.",
    "rich":
        "Generate a 5-7 sentence summary covering: overview, services, "
        "access/surroundings, atmosphere/crowd, and considerations/negatives. "
        "Stay factual, no marketing tone.",
    "very_rich":
        "Generate a 6-8 sentence summary. This spot has rich data "
        "(many services, many reviews, possibly multilingual). Cover: "
        "(1) overview, (2) services in detail, (3) access and surroundings, "
        "(4) atmosphere/crowd, (5) notable considerations or negatives, "
        "(6) best-fit profile. Stay factual, no marketing fluff. "
        "Mention temporal changes if reviews suggest them.",
}


def summary_instruction_for(level: str) -> str:
    return _RICHNESS_INSTRUCTIONS.get(level, _RICHNESS_INSTRUCTIONS["simple"])


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
               -- v3: servicios reconciliados básicos
               gratuito, precio_aprox, precio_info,
               agua_potable, vaciado_negras, vaciado_grises, electricidad,
               ducha, wifi, wc_publico,
               acceso_grandes, num_plazas, altura_max_m, temporada_apertura,
               -- v4b: campos extra que ya estaban en la tabla
               perros, iluminacion, seguridad, reserva_req,
               web, telefono, email,
               -- v4c: amenidades extra rescatadas de raw_data
               piscina, lavanderia, gas_recharge, restaurant, juegos_ninos,
               mirador, zona_protegida, online_booking, winter_friendly, apto_motos,
               mtb_friendly, surf_friendly, fishing, climbing, hiking_nearby,
               amperaje, n_enchufes, max_noches,
               idiomas_hablados, productos_venta, servicios_extras
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
