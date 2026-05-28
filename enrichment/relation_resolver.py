"""Resolución de cross_references LLM → `spot_relations` (T2.6 — Tier 2 hardening).

El orchestrator v4 emite `cross_references[]` cuando una review menciona OTRO
lugar ("parking del telesilla", "River shopping center nearby", "el camping de
al lado tiene agua"). Cada mención es solo texto libre — aquí la resolvemos a un
`spot_id` real y la materializamos en `spot_relations`.

Estrategia de resolución (barata, sin LLM extra):
  - candidatos = spots dentro de `max_distance_m` del spot origen (índice GIST geog)
  - ranking = similitud trigram de `canonical_name` vs `mentioned_name` (índice GIN trgm)
  - se acepta el mejor por encima de `min_similarity`; si nada supera el umbral → None
    (no forzamos match: mejor 0 relaciones que una relación falsa).

`confidence` de la relación = similitud de nombre (proxy de cuán seguros estamos
de que el lugar mencionado es ese spot). `source='llm_review_inference'`.

Relaciones simétricas (`same_complex`, `walking_distance`) se insertan también en
sentido inverso con `bidirectional=TRUE`.

REGENERABLE: las filas con source != 'manual' se pueden borrar y rehacer.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from .gemini_response_parser import ValidatedCrossRef, ValidatedEnrichment

# Relaciones cuyo significado es simétrico → se inserta también la inversa.
_SYMMETRIC_RELATIONS = {"same_complex", "walking_distance"}

DEFAULT_MAX_DISTANCE_M = 5000     # radio de búsqueda de candidatos
DEFAULT_MIN_SIMILARITY = 0.30     # umbral pg_trgm (0..1) para aceptar el match


@dataclass
class ResolvedRelation:
    related_spot_id: int
    related_name: str
    distance_m: int
    similarity: float


async def resolve_cross_reference(
    conn,
    origin_spot_id: int,
    mentioned_name: str,
    *,
    max_distance_m: int = DEFAULT_MAX_DISTANCE_M,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> ResolvedRelation | None:
    """Resuelve un nombre mencionado al spot real más probable cerca del origen.

    Devuelve None si no hay candidato lo bastante parecido (no forzamos match).
    """
    name = (mentioned_name or "").strip()
    if not name:
        return None
    row = await conn.fetchrow(
        """
        WITH origin AS (SELECT geog FROM spots WHERE id = $1)
        SELECT s.id,
               s.canonical_name,
               ST_Distance(o.geog, s.geog) AS dist_m,
               similarity(s.canonical_name, $2) AS sim
        FROM spots s, origin o
        WHERE s.id <> $1
          AND ST_DWithin(o.geog, s.geog, $3)
          AND similarity(s.canonical_name, $2) >= $4
        ORDER BY sim DESC, dist_m ASC
        LIMIT 1
        """,
        origin_spot_id, name, max_distance_m, min_similarity,
    )
    if not row:
        return None
    return ResolvedRelation(
        related_spot_id=row["id"],
        related_name=row["canonical_name"],
        distance_m=int(round(row["dist_m"])),
        similarity=float(row["sim"]),
    )


async def _upsert_relation(
    conn, spot_id: int, related_spot_id: int, relation_type: str,
    *, distance_m: int, confidence: float, source: str, bidirectional: bool,
) -> None:
    await conn.execute(
        """
        INSERT INTO spot_relations
            (spot_id, related_spot_id, relation_type, distance_m,
             bidirectional, confidence, source, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
        ON CONFLICT (spot_id, related_spot_id, relation_type) DO UPDATE SET
            distance_m = EXCLUDED.distance_m,
            bidirectional = EXCLUDED.bidirectional,
            confidence = GREATEST(spot_relations.confidence, EXCLUDED.confidence),
            source = EXCLUDED.source,
            updated_at = NOW()
        """,
        spot_id, related_spot_id, relation_type, distance_m,
        bidirectional, round(confidence, 2), source,
    )


async def ingest_cross_references(
    conn,
    origin_spot_id: int,
    parsed: ValidatedEnrichment,
    *,
    source: str = "llm_review_inference",
    max_distance_m: int = DEFAULT_MAX_DISTANCE_M,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> int:
    """Resuelve e inserta todas las `cross_references` del enrichment.

    Devuelve el número de relaciones efectivamente persistidas (incluye las
    inversas de relaciones simétricas). Las menciones que no resuelven a ningún
    spot se ignoran silenciosamente (no son error).
    """
    refs: list[ValidatedCrossRef] = parsed.cross_references
    if not refs:
        return 0

    count = 0
    seen: set[tuple[int, str]] = set()  # (related_spot_id, relation_type) ya procesados
    for ref in refs:
        resolved = await resolve_cross_reference(
            conn, origin_spot_id, ref.mentioned_name,
            max_distance_m=max_distance_m, min_similarity=min_similarity,
        )
        if resolved is None:
            logger.debug(
                f"[relations] spot={origin_spot_id} sin match para "
                f"'{ref.mentioned_name}' ({ref.relation_type})"
            )
            continue
        if resolved.related_spot_id == origin_spot_id:
            continue  # self-relation (CHECK lo rechazaría igualmente)
        key = (resolved.related_spot_id, ref.relation_type)
        if key in seen:
            continue
        seen.add(key)

        bidir = ref.relation_type in _SYMMETRIC_RELATIONS
        await _upsert_relation(
            conn, origin_spot_id, resolved.related_spot_id, ref.relation_type,
            distance_m=resolved.distance_m, confidence=resolved.similarity,
            source=source, bidirectional=bidir,
        )
        count += 1
        logger.debug(
            f"[relations] spot={origin_spot_id} → {resolved.related_spot_id} "
            f"({ref.relation_type}, sim={resolved.similarity:.2f}, {resolved.distance_m}m)"
        )

        if bidir:
            await _upsert_relation(
                conn, resolved.related_spot_id, origin_spot_id, ref.relation_type,
                distance_m=resolved.distance_m, confidence=resolved.similarity,
                source=source, bidirectional=True,
            )
            count += 1

    return count
