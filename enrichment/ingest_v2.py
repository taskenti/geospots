"""Ingesta de respuestas LLM v2 spot-level → DB.

Flujo en una transacción:
  1. Mapear cada ValidatedClaim → fila en extracted_claims (con extractor distinguible).
  2. Normalizar claim → insertar fila en normalized_observations (peso decayed).
  3. recompute_spot_state(spot_id) — recalcula scores agregados desde TODAS las observations.
  4. Materializar columnas v2: noise_sources[], parking_capacity, last_observation_at.
  5. Update narrative fields: summary_en (English only in v4), tags, best_for,
     best_season, avoid_season. (summary_es column kept NULL — deprecated, the API
     layer is responsible for translation when needed.)
  6. Set enrichment_version, llm_model.

Diseño:
  - extractor_name = '{provider}_spot_v2' permite filtrar/distinguir runs y eventualmente revertir.
  - extractor_version = ENRICHMENT_VERSION (string) — al cambiar la versión los claims viejos siguen
    existiendo (audit trail) pero el agregado los pondera por decay.
  - observed_at del claim: si el claim cita review_id, usamos reviews.fecha; si es 'description',
    usamos NOW().
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from .gemini_response_parser import ValidatedClaim, ValidatedEnrichment
from .observation_normalizer import normalize_claim
from .prompts import ENRICHMENT_VERSION
from .signal_registry import STATIC_SIGNALS
from .state_aggregator import recompute_spot_state
from .state_resolver import AlertPayload, refresh_active_alert_types, upsert_alert
from .v2_materializer import (
    aggregate_noise_sources,
    aggregate_parking_capacity,
    compute_last_observation_at,
)


@dataclass
class IngestStats:
    spot_id: int
    claims_inserted: int
    observations_inserted: int
    narrative_updated: bool
    enrichment_version: int
    llm_model: str
    pipeline_run_id: str
    # v6 (T1.4 + T1.4b)
    alerts_upserted: int = 0
    spot_function_set: bool = False
    spot_geo_updated: bool = False


def _extractor_name(provider: str) -> str:
    """`gemini_spot_v2` o `deepseek_spot_v2`."""
    return f"{provider}_spot_v2"


def _extractor_version() -> str:
    return f"v{ENRICHMENT_VERSION}"


async def _resolve_observed_at(conn, review_id: int | None,
                               fallback: datetime | None = None) -> datetime:
    """Si el claim cita review_id, usamos su fecha; si no, fallback/NOW."""
    if review_id is not None:
        row = await conn.fetchrow("SELECT fecha FROM reviews WHERE id = $1", review_id)
        if row and row["fecha"]:
            fecha = row["fecha"]
            if hasattr(fecha, "year") and not hasattr(fecha, "hour"):
                # date → datetime
                return datetime(fecha.year, fecha.month, fecha.day, tzinfo=timezone.utc)
            if fecha.tzinfo is None:
                return fecha.replace(tzinfo=timezone.utc)
            return fecha
    return fallback or datetime.now(timezone.utc)


async def _insert_claim(conn, spot_id: int, claim: ValidatedClaim,
                       provider: str, pipeline_run_id: str) -> int:
    return await conn.fetchval(
        """
        INSERT INTO extracted_claims (
            review_id, spot_id, signal_type, raw_value, extraction_confidence,
            extractor_name, extractor_version, pipeline_run_id, excerpt
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        claim.review_id,  # puede ser NULL para claims que vienen de descriptions
        spot_id,
        claim.signal,
        str(claim.value),
        claim.confidence,
        _extractor_name(provider),
        _extractor_version(),
        pipeline_run_id,
        claim.excerpt,
    )


async def _insert_observation(conn, claim_id: int, spot_id: int, obs) -> int:
    return await conn.fetchval(
        """
        INSERT INTO normalized_observations (
            claim_id, spot_id, signal_type, value_num, value_bool, value_text,
            extraction_confidence, source_confidence, reviewer_confidence,
            observation_weight, observed_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        claim_id, spot_id, obs.signal_type,
        obs.value_num, obs.value_bool, obs.value_text,
        obs.extraction_confidence, obs.source_confidence, obs.reviewer_confidence,
        obs.observation_weight, obs.observed_at,
    )


async def _update_narrative_and_materialized(conn, spot_id: int,
                                             parsed: ValidatedEnrichment,
                                             *,
                                             enrichment_version: int,
                                             llm_model: str) -> None:
    """Update post-recompute: campos narrativos + materializadas v2."""
    # Releer observations frescas (incluyen las recién insertadas) para materializar
    obs_rows = await conn.fetch(
        """
        SELECT signal_type, value_num, value_bool, value_text, observation_weight, observed_at
        FROM normalized_observations
        WHERE spot_id = $1
        """,
        spot_id,
    )
    observations = [dict(r) for r in obs_rows]

    noise_sources = aggregate_noise_sources(observations) or None
    parking_capacity = aggregate_parking_capacity(observations)
    last_obs_at = await compute_last_observation_at(conn, spot_id)

    # v4: parsed.summary es un único string en inglés. Lo escribimos en summary_en.
    # summary_es se deja NULL (deprecated — el cliente API traducirá si necesita).
    # parsed.summary_es es un property que devuelve None (compat shim del parser v4).
    await conn.execute(
        """
        UPDATE spot_semantic_state SET
            summary_es          = $2,
            summary_en          = $3,
            tags                = $4,
            best_for            = $5,
            best_season         = $6,
            avoid_season        = $7,
            noise_sources       = $8,
            parking_capacity    = $9,
            last_observation_at = $10,
            enrichment_version  = $11,
            llm_model           = $12,
            stale               = FALSE,
            updated_at          = NOW()
        WHERE spot_id = $1
        """,
        spot_id,
        parsed.summary_es,  # v4: None (shim devuelve None) — futuro: drop column
        parsed.summary or parsed.summary_en,  # v4 canonical: parsed.summary (English)
        parsed.tags or None,
        parsed.best_for or None,
        parsed.best_season,
        parsed.avoid_season,
        noise_sources,
        parking_capacity,
        last_obs_at,
        enrichment_version,
        llm_model,
    )


async def _upsert_spot_functional_fields(
    conn, spot_id: int, parsed: ValidatedEnrichment,
) -> bool:
    """T1.4b — escribe spot_function, is_overnight_viable, authorization_status
    en `spots` SOLO si el LLM los emitió. COALESCE preserva valores legacy si el
    LLM no opina (NULL = "no determinado").

    Devuelve True si al menos un campo se tocó.
    """
    if (parsed.spot_function is None
            and parsed.is_overnight_viable is None
            and parsed.authorization_status is None):
        return False
    await conn.execute(
        """
        UPDATE spots
        SET spot_function        = COALESCE($2, spot_function),
            is_overnight_viable  = COALESCE($3, is_overnight_viable),
            authorization_status = COALESCE($4, authorization_status)
        WHERE id = $1
        """,
        spot_id,
        parsed.spot_function,
        parsed.is_overnight_viable,
        parsed.authorization_status,
    )
    return True


async def _upsert_spot_geo_from_llm(
    conn, spot_id: int, parsed: ValidatedEnrichment,
) -> bool:
    """T1.4b/D8 — escribe valores geofísicos a `spot_geo`. NO sobreescribe si
    ya hay otro source (DEM/OSM ganan al LLM).

    Crea la fila si no existe. Devuelve True si tocó algo.
    """
    if (parsed.elevation_m is None
            and parsed.terrain_type is None
            and parsed.slope_degrees is None):
        return False

    existing = await conn.fetchrow(
        "SELECT source FROM spot_geo WHERE spot_id = $1", spot_id,
    )
    if existing is None:
        await conn.execute(
            """
            INSERT INTO spot_geo (spot_id, elevation_m, terrain_type, slope_degrees, source)
            VALUES ($1, $2, $3, $4, 'llm_v6')
            """,
            spot_id, parsed.elevation_m, parsed.terrain_type, parsed.slope_degrees,
        )
        return True

    # Si la fila ya viene de DEM/OSM/manual, NO la sobreescribimos con LLM.
    src = (existing["source"] or "").strip().lower()
    if src in ("dem", "osm", "manual"):
        return False

    # Source NULL o 'llm_*' → COALESCE con los valores del LLM (no pisa NOT NULL legacy)
    await conn.execute(
        """
        UPDATE spot_geo
        SET elevation_m   = COALESCE($2, elevation_m),
            terrain_type  = COALESCE($3, terrain_type),
            slope_degrees = COALESCE($4, slope_degrees),
            source        = 'llm_v6'
        WHERE spot_id = $1
        """,
        spot_id, parsed.elevation_m, parsed.terrain_type, parsed.slope_degrees,
    )
    return True


async def _upsert_alerts_from_llm(
    conn, spot_id: int, parsed: ValidatedEnrichment, *, llm_model: str,
) -> int:
    """T1.4 — vuelca `parsed.alerts[]` a `spot_alerts` vía state_resolver.upsert_alert.

    Devuelve el número de alerts efectivamente persistidas.
    """
    if not parsed.alerts:
        return 0
    detected_by = f"llm_v{ENRICHMENT_VERSION}"
    count = 0
    for va in parsed.alerts:
        payload = AlertPayload.from_validated(va)
        if payload is None:
            logger.warning(
                f"[ingest_v2] alert descartada spot={spot_id} tipo={va.alert_type}: "
                f"valid_from_inferred no parseable ({va.valid_from!r})"
            )
            continue
        await upsert_alert(conn, spot_id, payload, detected_by=detected_by)
        count += 1
    if count:
        # Refrescar materializada active_alert_types en spot_semantic_state
        await refresh_active_alert_types(conn, spot_id)
    return count


async def ingest_spot_enrichment(
    conn,
    spot_id: int,
    parsed: ValidatedEnrichment,
    *,
    provider: str,
    llm_model: str,
    pipeline_run_id: str | None = None,
    enrichment_version: int = ENRICHMENT_VERSION,
    source_confidence: float = 1.0,
) -> IngestStats:
    """Ingesta atómica de un enrichment v2 para un spot.

    `conn` debe ser una conexión asyncpg. Se abre transacción internamente.
    `parsed` viene de `gemini_response_parser.parse_enrichment_response`.
    `provider` ∈ {'gemini', 'deepseek'} — solo se usa para el extractor_name.
    """
    pipeline_run_id = pipeline_run_id or str(uuid.uuid4())
    stats = IngestStats(
        spot_id=spot_id,
        claims_inserted=0,
        observations_inserted=0,
        narrative_updated=False,
        enrichment_version=enrichment_version,
        llm_model=llm_model,
        pipeline_run_id=pipeline_run_id,
    )

    async with conn.transaction():
        # 1. Claims → observations
        for claim in parsed.claims:
            stype = STATIC_SIGNALS.get(claim.signal)
            if not stype:
                logger.warning(f"[ingest_v2] signal desconocido tras parser: {claim.signal} (skip)")
                continue

            observed_at = await _resolve_observed_at(conn, claim.review_id)
            obs = normalize_claim(
                {"signal": claim.signal, "value": claim.value, "confidence": claim.confidence},
                source_confidence=source_confidence,
                reviewer_confidence=1.0,
                observed_at=observed_at,
            )
            if obs is None:
                logger.debug(f"[ingest_v2] claim no normalizable: {claim.signal}={claim.value}")
                continue

            claim_id = await _insert_claim(conn, spot_id, claim, provider, pipeline_run_id)
            stats.claims_inserted += 1
            await _insert_observation(conn, claim_id, spot_id, obs)
            stats.observations_inserted += 1

        # 2. Recompute scores agregados (escribe la fila base en spot_semantic_state)
        await recompute_spot_state(conn, spot_id)

        # 3. Update post-recompute: narrative + materializadas v2 + version/model
        await _update_narrative_and_materialized(
            conn, spot_id, parsed,
            enrichment_version=enrichment_version,
            llm_model=llm_model,
        )
        stats.narrative_updated = True

        # 4. v6 (T1.4b) — clasificación funcional en `spots`
        stats.spot_function_set = await _upsert_spot_functional_fields(
            conn, spot_id, parsed,
        )

        # 5. v6 (T1.4b/D8) — geofísicos en `spot_geo`
        stats.spot_geo_updated = await _upsert_spot_geo_from_llm(
            conn, spot_id, parsed,
        )

        # 6. v6 (T1.4) — alerts → spot_alerts + refresh active_alert_types
        stats.alerts_upserted = await _upsert_alerts_from_llm(
            conn, spot_id, parsed, llm_model=llm_model,
        )

    logger.info(
        f"[ingest_v2] spot={spot_id} provider={provider} model={llm_model} "
        f"claims={stats.claims_inserted} obs={stats.observations_inserted} "
        f"alerts={stats.alerts_upserted} func_set={stats.spot_function_set} "
        f"geo_set={stats.spot_geo_updated} run={pipeline_run_id}"
    )
    return stats
