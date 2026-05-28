"""Canonicalizador de tags (T1.5 — Sprint 2 hardening).

Funciona contra dos tablas:
  `canonical_tags(canonical_id PK, aliases TEXT[], category)` — vocabulario válido.
  `unknown_tags(tag PK, first_seen, last_seen, occurrence_count, reviewed)` —
    tracking de tags emitidos por el LLM que no mapean a ningún canonical.

API:
  - `normalize_raw_tag(raw)`             — lowercase + strip + kebab-case
  - `await load_canonical_index(conn)`   — carga índice {alias|canonical → canonical_id}
  - `canonicalize_tag(raw, index)`       — síncrono, opera sobre el índice cacheado
  - `await canonicalize_batch(conn, tags, *, register_unknown=True)`
       → (canonical_ids: list[str], unknown_raws: list[str])

Cache:
  El índice se carga UNA vez por proceso. Si el job mensual de promoción mueve
  filas de unknown_tags a canonical_tags, hay que reiniciar el worker (o llamar
  `invalidate_canonical_index()` antes del siguiente batch).

NO normaliza en DB: los tags ya canonicalizados se persisten tal cual los devuelve
el índice — kebab-case, lowercase, sin sufijos.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from loguru import logger


# ── Normalización ─────────────────────────────────────────────────────

_WORD_SPLIT = re.compile(r"[\s_/]+")
_NON_KEBAB = re.compile(r"[^a-z0-9\-]")
_MULTI_HYPHEN = re.compile(r"-{2,}")


def normalize_raw_tag(raw) -> str:
    """Reduce un tag bruto al formato canónico de búsqueda.

    - lowercase + strip
    - separadores `_`, `/`, espacio → `-`
    - retira cualquier char fuera de `[a-z0-9-]`
    - colapsa `--…` → `-`
    - trim `-` en bordes
    - devuelve "" si tras normalizar queda vacío
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        try:
            raw = str(raw)
        except Exception:
            return ""
    s = raw.strip().lower()
    if not s:
        return ""
    s = _WORD_SPLIT.sub("-", s)
    s = _NON_KEBAB.sub("", s)
    s = _MULTI_HYPHEN.sub("-", s).strip("-")
    return s


# ── Índice en memoria ─────────────────────────────────────────────────
#
# Estructura: dict[str → str]
#   key   = canonical_id o cualquiera de sus aliases (ya normalizados a kebab)
#   value = canonical_id (forma persistida en spot_semantic_state.tags)
#
# Se inicializa la primera vez que el ingest llama a `canonicalize_batch`.
# Procesos largos (worker, orchestrator) pagan UNA carga.

_INDEX: dict[str, str] | None = None
_INDEX_LOADED_AT: datetime | None = None


def invalidate_canonical_index() -> None:
    """Limpia el índice cacheado. Útil tras correr el job de promoción."""
    global _INDEX, _INDEX_LOADED_AT
    _INDEX = None
    _INDEX_LOADED_AT = None


async def load_canonical_index(conn, *, force: bool = False) -> dict[str, str]:
    """Carga el índice desde DB. No-op si ya está cargado salvo `force=True`."""
    global _INDEX, _INDEX_LOADED_AT
    if _INDEX is not None and not force:
        return _INDEX

    rows = await conn.fetch("SELECT canonical_id, aliases FROM canonical_tags")
    idx: dict[str, str] = {}
    for r in rows:
        canonical = r["canonical_id"]
        norm_canon = normalize_raw_tag(canonical)
        if not norm_canon:
            continue
        idx[norm_canon] = canonical
        for alias in (r["aliases"] or []):
            na = normalize_raw_tag(alias)
            if na and na not in idx:
                idx[na] = canonical
    _INDEX = idx
    _INDEX_LOADED_AT = datetime.now(timezone.utc)
    logger.info(
        f"[tag_canonicalizer] índice cargado: {len(rows)} canónicos / "
        f"{len(idx)} entradas (incluye aliases)"
    )
    return idx


# ── Resolución ────────────────────────────────────────────────────────


def canonicalize_tag(raw, index: dict[str, str]) -> str | None:
    """Resuelve `raw` a canonical_id usando el índice ya cargado.

    Devuelve `None` si no hay match — el caller debería registrar en unknown_tags.
    """
    key = normalize_raw_tag(raw)
    if not key:
        return None
    return index.get(key)


async def canonicalize_batch(
    conn,
    raw_tags,
    *,
    register_unknown: bool = True,
    dedup: bool = True,
) -> tuple[list[str], list[str]]:
    """Procesa una lista de raw tags del LLM.

    Returns:
      (canonical_ids, unknown_raws)

    `canonical_ids` preserva el orden de entrada y elimina duplicados si `dedup`.
    `unknown_raws` es la lista de los tags normalizados que no mapearon a nada.

    Si `register_unknown=True`, los tags desconocidos se UPSERT-ean en `unknown_tags`
    (incrementa `occurrence_count`, refresca `last_seen`).
    """
    if not raw_tags:
        return [], []

    index = await load_canonical_index(conn)

    seen: set[str] = set()
    canonical_out: list[str] = []
    unknown_out: list[str] = []

    for raw in raw_tags:
        norm = normalize_raw_tag(raw)
        if not norm:
            continue
        canonical = index.get(norm)
        if canonical:
            if not dedup or canonical not in seen:
                canonical_out.append(canonical)
                seen.add(canonical)
        else:
            unknown_out.append(norm)

    if register_unknown and unknown_out:
        await _record_unknown_tags(conn, unknown_out)

    return canonical_out, unknown_out


async def _record_unknown_tags(conn, raws) -> None:
    """UPSERT en unknown_tags. Idempotente: incrementa contador en cada llamada."""
    # asyncpg no expone executemany para ON CONFLICT con UPDATE de forma limpia
    # → usamos unnest para batch en una sola sentencia.
    await conn.execute(
        """
        INSERT INTO unknown_tags (tag, first_seen, last_seen, occurrence_count, reviewed)
        SELECT t, NOW(), NOW(), 1, FALSE
        FROM unnest($1::TEXT[]) AS t
        ON CONFLICT (tag) DO UPDATE
        SET last_seen = NOW(),
            occurrence_count = unknown_tags.occurrence_count + 1
        """,
        list(raws),
    )


# ── Helpers para el job de promoción mensual (T2.4) ───────────────────


async def list_top_unknown(conn, *, limit: int = 20, reviewed: bool = False) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT tag, occurrence_count, first_seen, last_seen, reviewed
        FROM unknown_tags
        WHERE reviewed = $1
        ORDER BY occurrence_count DESC, last_seen DESC
        LIMIT $2
        """,
        reviewed, limit,
    )
    return [dict(r) for r in rows]


async def promote_unknown_to_canonical(
    conn,
    tag: str,
    *,
    canonical_id: str | None = None,
    category: str | None = None,
) -> None:
    """Promueve un unknown_tag a canonical. Si `canonical_id` es None, se usa
    el `tag` tal cual (normalizado) como ID.

    - Si ya existe un canonical con `canonical_id` igual, AÑADE el tag como alias.
    - Si no existe, crea la fila canonical y marca el unknown como reviewed=TRUE.
    """
    norm_tag = normalize_raw_tag(tag)
    target_id = normalize_raw_tag(canonical_id) if canonical_id else norm_tag
    if not target_id:
        raise ValueError(f"tag/canonical_id no normalizable: tag={tag!r} canonical_id={canonical_id!r}")

    existing = await conn.fetchrow(
        "SELECT aliases FROM canonical_tags WHERE canonical_id = $1", target_id,
    )
    if existing:
        # Append al array si no estaba ya
        cur_aliases = list(existing["aliases"] or [])
        if norm_tag != target_id and norm_tag not in cur_aliases:
            cur_aliases.append(norm_tag)
            await conn.execute(
                "UPDATE canonical_tags SET aliases = $1 WHERE canonical_id = $2",
                cur_aliases, target_id,
            )
    else:
        await conn.execute(
            """
            INSERT INTO canonical_tags (canonical_id, aliases, category)
            VALUES ($1, $2, $3)
            """,
            target_id,
            [norm_tag] if norm_tag != target_id else [],
            category,
        )

    await conn.execute(
        "UPDATE unknown_tags SET reviewed = TRUE WHERE tag = $1", norm_tag,
    )
    invalidate_canonical_index()
