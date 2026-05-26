"""Orquestador de enrichment v2 spot-level con pool de concurrencia.

Diseño:
  - Sin Batch API: paralelo síncrono via asyncio + thread pool.
  - DeepSeek soporta 2500 concurrent; Gemini ~10-60 (depende de tier).
    Default conservador: 20. Ajustable vía env / flag.
  - Cada spot procesa en su propia conexión asyncpg (acquire/release).
  - Errores por spot NO matan el run — se loguean y se siguen.
  - Tracking en `enrichment_batches`: una fila por run con stats acumulados.
  - Idempotencia: si un spot falla a mitad, la transacción interna de
    ingest_v2 hace rollback. Reintentar el spot funciona limpio.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger

from .gemini_response_parser import ParseError, parse_enrichment_response
from .ingest_v2 import ingest_spot_enrichment
from .llm_provider import (
    call_deepseek_sync,
    call_gemini_sync,
    estimate_cost,
    get_active_model,
    get_provider_name,
)
from .prompts import ENRICHMENT_VERSION, build_spot_user_prompt
from .spot_packager import (
    fetch_reviews_for_enrichment,
    fetch_spot_for_enrichment,
    select_reviews_for_prompt,
    should_enrich,
)


# Tandas geográficas (orden definido en el plan)
COUNTRY_TIERS = {
    1: ["ES"],
    2: ["PT"],
    3: ["FR"],
    4: ["DE"],
    5: ["IT"],
    6: ["GB", "UK"],  # ISO usa GB; legacy UK
    7: ["US"],
    99: None,  # resto del mundo
}


@dataclass
class RunStats:
    pipeline_run_id: str
    batch_db_id: int | None = None
    spots_requested: int = 0
    spots_succeeded: int = 0
    spots_failed: int = 0
    spots_skipped: int = 0
    claims_total: int = 0
    tokens_input_total: int = 0
    tokens_output_total: int = 0
    cost_estimated_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


async def select_candidates(
    conn,
    *,
    countries: list[str] | None = None,
    limit: int = 1000,
    enrichment_version: int = ENRICHMENT_VERSION,
) -> list[int]:
    """Selecciona spot_ids candidatos a enrichment v2.

    Criterios (orden de prioridad):
      1. enrichment_version < current
      2. stale = TRUE
      3. ≥ 5 reviews nuevas desde último aggregate
      4. last_aggregated_at < NOW() - 18 months
      5. nunca enriched + ≥3 reviews

    `countries`: lista de ISO codes para filtrar (None = sin filtro).
    """
    if countries:
        # spots.country_iso se almacena en minúsculas (trigger de clasificación)
        normalized = [c.lower() for c in countries]
        country_filter = "AND s.country_iso = ANY($3)"
        params = [enrichment_version, limit, normalized]
    else:
        country_filter = ""
        params = [enrichment_version, limit]

    # Optimización: usar s.total_reviews (materializado) en vez de COUNT(*).
    # Para n_new usamos EXISTS con OFFSET 4 que corta en cuanto encuentra la 5ª.
    sql = f"""
    SELECT s.id AS spot_id
    FROM spots s
    LEFT JOIN spot_semantic_state sss ON sss.spot_id = s.id
    WHERE s.activo = TRUE
      AND COALESCE(s.total_reviews, 0) >= 3
      {country_filter}
      AND (
        sss.spot_id IS NULL
        OR COALESCE(sss.enrichment_version, 0) < $1
        OR COALESCE(sss.stale, FALSE) = TRUE
        OR sss.last_aggregated_at < NOW() - INTERVAL '18 months'
        OR EXISTS (
            SELECT 1 FROM (
                SELECT 1 FROM reviews r
                WHERE r.spot_id = s.id
                  AND r.first_seen > COALESCE(sss.last_aggregated_at, '1970-01-01'::timestamptz)
                LIMIT 5 OFFSET 4
            ) sub
        )
      )
    ORDER BY
        CASE WHEN COALESCE(sss.enrichment_version, 0) < $1 THEN 0 ELSE 1 END,
        COALESCE(sss.stale, FALSE) DESC,
        s.total_reviews DESC NULLS LAST
    LIMIT $2
    """
    rows = await conn.fetch(sql, *params)
    return [r["spot_id"] for r in rows]


async def _create_batch_row(conn, run_id: str, spot_ids: list[int],
                            enrichment_version: int, model: str) -> int:
    return await conn.fetchval(
        """
        INSERT INTO enrichment_batches
            (batch_name, enrichment_version, llm_model, spot_ids,
             state, n_requested)
        VALUES ($1, $2, $3, $4, 'running', $5)
        RETURNING id
        """,
        f"run-{run_id}",
        enrichment_version,
        model,
        spot_ids,
        len(spot_ids),
    )


async def _update_batch_row(conn, batch_id: int, stats: RunStats, final_state: str) -> None:
    await conn.execute(
        """
        UPDATE enrichment_batches SET
            state              = $2,
            n_succeeded        = $3,
            n_failed           = $4,
            tokens_input       = $5,
            tokens_output      = $6,
            cost_estimated_usd = $7,
            error_msg          = $8,
            completed_at       = NOW()
        WHERE id = $1
        """,
        batch_id,
        final_state,
        stats.spots_succeeded,
        stats.spots_failed,
        stats.tokens_input_total,
        stats.tokens_output_total,
        stats.cost_estimated_usd,
        ("; ".join(stats.errors[:5]))[:1000] if stats.errors else None,
    )


async def _call_llm(provider: str, user_prompt: str, model: str):
    """Wrapper común: ejecuta llamada síncrona del SDK en thread."""
    if provider == "deepseek":
        return await asyncio.to_thread(call_deepseek_sync, user_prompt, model=model)
    return await asyncio.to_thread(call_gemini_sync, user_prompt, model=model)


async def _process_one_spot(pool, spot_id: int, *, provider: str, model: str,
                            pipeline_run_id: str, semaphore: asyncio.Semaphore,
                            stats: RunStats, max_retries: int = 2) -> None:
    """Procesa un spot. Errores se acumulan en `stats`, no se propagan."""
    async with semaphore:
        async with pool.acquire() as conn:
            try:
                spot = await fetch_spot_for_enrichment(conn, spot_id)
                if not spot:
                    stats.spots_skipped += 1
                    return
                reviews_raw = await fetch_reviews_for_enrichment(conn, spot_id)
                decision, reason = should_enrich(spot, len(reviews_raw))
                if not decision:
                    stats.spots_skipped += 1
                    logger.debug(f"[orchestrator] skip {spot_id}: {reason}")
                    return
                selected = select_reviews_for_prompt(reviews_raw)
                user_prompt = build_spot_user_prompt(spot, selected)
            except Exception as exc:
                stats.spots_failed += 1
                stats.errors.append(f"spot={spot_id} fetch: {exc}")
                logger.error(f"[orchestrator] fetch failed spot={spot_id}: {exc}")
                return

        # LLM + parse + ingest (con reintentos en errores transitorios)
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= max_retries:
            try:
                resp = await _call_llm(provider, user_prompt, model)
                parsed = parse_enrichment_response(resp.text)
                async with pool.acquire() as conn:
                    ingest_stats = await ingest_spot_enrichment(
                        conn, spot_id, parsed,
                        provider=resp.provider,
                        llm_model=resp.model,
                        pipeline_run_id=pipeline_run_id,
                    )
                cost = estimate_cost(resp.model, resp.usage)
                stats.spots_succeeded += 1
                stats.claims_total += ingest_stats.claims_inserted
                stats.tokens_input_total += int(resp.usage.get("prompt_token_count", 0))
                stats.tokens_output_total += int(resp.usage.get("candidates_token_count", 0))
                stats.cost_estimated_usd += cost
                logger.debug(f"[orchestrator] ok spot={spot_id} claims={ingest_stats.claims_inserted} cost=${cost:.5f}")
                return
            except ParseError as exc:
                last_exc = exc
                attempt += 1
                logger.warning(f"[orchestrator] parse error spot={spot_id} attempt={attempt}: {exc}")
                if attempt > max_retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 10))
            except Exception as exc:
                last_exc = exc
                attempt += 1
                logger.warning(f"[orchestrator] LLM/ingest error spot={spot_id} attempt={attempt}: {exc}")
                if attempt > max_retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 10))

        stats.spots_failed += 1
        stats.errors.append(f"spot={spot_id}: {type(last_exc).__name__}: {last_exc}")


async def run_enrichment(
    pool,
    *,
    countries: list[str] | None = None,
    limit: int = 1000,
    concurrency: int = 20,
    provider: str | None = None,
    model: str | None = None,
    dry_run: bool = False,
) -> RunStats:
    """Punto de entrada del orquestador.

    Devuelve `RunStats` con métricas acumuladas.
    """
    provider = provider or get_provider_name()
    model = model or get_active_model()
    pipeline_run_id = str(uuid.uuid4())
    stats = RunStats(pipeline_run_id=pipeline_run_id)

    async with pool.acquire() as conn:
        spot_ids = await select_candidates(conn, countries=countries, limit=limit)
    stats.spots_requested = len(spot_ids)
    logger.info(
        f"[orchestrator] run={pipeline_run_id} provider={provider} model={model} "
        f"countries={countries} concurrency={concurrency} candidates={len(spot_ids)}"
    )

    if not spot_ids:
        stats.completed_at = datetime.now(timezone.utc)
        return stats

    if dry_run:
        logger.info(f"[orchestrator] DRY RUN — no se llama LLM. candidates={spot_ids[:10]}...")
        stats.spots_skipped = len(spot_ids)
        stats.completed_at = datetime.now(timezone.utc)
        return stats

    async with pool.acquire() as conn:
        stats.batch_db_id = await _create_batch_row(
            conn, pipeline_run_id, spot_ids, ENRICHMENT_VERSION, model
        )

    sem = asyncio.Semaphore(concurrency)
    start = time.time()
    tasks = [
        _process_one_spot(
            pool, spot_id,
            provider=provider, model=model,
            pipeline_run_id=pipeline_run_id,
            semaphore=sem, stats=stats,
        )
        for spot_id in spot_ids
    ]
    # Log progress periódico
    progress_task = asyncio.create_task(_log_progress(stats, len(spot_ids), start))
    try:
        await asyncio.gather(*tasks)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    stats.completed_at = datetime.now(timezone.utc)
    final_state = "succeeded" if stats.spots_failed == 0 else (
        "partial" if stats.spots_succeeded > 0 else "failed"
    )
    if stats.batch_db_id is not None:
        async with pool.acquire() as conn:
            await _update_batch_row(conn, stats.batch_db_id, stats, final_state)

    elapsed = time.time() - start
    logger.info(
        f"[orchestrator] DONE run={pipeline_run_id} "
        f"requested={stats.spots_requested} ok={stats.spots_succeeded} "
        f"failed={stats.spots_failed} skipped={stats.spots_skipped} "
        f"claims={stats.claims_total} cost=${stats.cost_estimated_usd:.4f} "
        f"elapsed={elapsed:.1f}s"
    )
    return stats


async def _log_progress(stats: RunStats, total: int, start_ts: float) -> None:
    """Log de progreso cada 30s."""
    try:
        while True:
            await asyncio.sleep(30)
            done = stats.spots_succeeded + stats.spots_failed + stats.spots_skipped
            elapsed = time.time() - start_ts
            rate = done / max(elapsed, 1.0)
            eta = (total - done) / max(rate, 0.01)
            logger.info(
                f"[orchestrator] progress: {done}/{total} "
                f"(ok={stats.spots_succeeded} fail={stats.spots_failed} skip={stats.spots_skipped}) "
                f"rate={rate:.1f}/s eta={eta:.0f}s cost=${stats.cost_estimated_usd:.4f}"
            )
    except asyncio.CancelledError:
        return
