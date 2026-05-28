"""Batch worker for pending review enrichment.

Throttling para DeepSeek V4 Flash (provider por defecto para bulk):
- ENRICHMENT_CONCURRENCY (env, default 8): llamadas LLM concurrentes.
  DeepSeek no tiene RPM/RPD duros, pero limitar concurrencia controla costes
  y evita saturar la pool de conexiones.
- Backoff exponencial en errores transitorios (429, 5xx, timeout).
- Parada automática si MAX_CONSECUTIVE_LLM_ERRORS fallos seguidos
  (señal de que la API no está disponible o el crédito se agotó).
- Progreso logueado cada PROGRESS_EVERY reviews.

Para Gemini free tier (ENRICHMENT_PROVIDER=gemini, sin billing):
  Poner ENRICHMENT_CONCURRENCY=1 y añadir delay manual:
  ENRICHMENT_INTER_REQUEST_DELAY=4.3  (→ 14 RPM, bajo el límite de 15)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import asyncpg
from loguru import logger

from .claim_extractor import extract_claims
from .dsl_generator import generate_review_dsl
from .observation_normalizer import normalize_claims
from .review_cleaner import clean_review_full
from .state_aggregator import update_semantic_state

# ── Configuración de throttling ──────────────────────────────────────────────

# Cuántas llamadas LLM simultáneas como máximo.
# DeepSeek bulk: 8-10 está bien. Gemini free: 1.
_DEFAULT_CONCURRENCY = int(os.environ.get("ENRICHMENT_CONCURRENCY", "8"))

# Delay en segundos entre llamadas LLM cuando provider=gemini y free tier.
# 0 = sin delay adicional (DeepSeek u otro provider con billing).
_INTER_REQUEST_DELAY = float(os.environ.get("ENRICHMENT_INTER_REQUEST_DELAY", "0"))

# Cuántos errores LLM consecutivos (sin ningún éxito entre medio) antes de abortar el batch.
_MAX_CONSECUTIVE_ERRORS = int(os.environ.get("ENRICHMENT_MAX_CONSECUTIVE_ERRORS", "20"))

# Loguear progreso cada N reviews procesadas.
_PROGRESS_EVERY = int(os.environ.get("ENRICHMENT_PROGRESS_EVERY", "100"))

# Tiempo máximo de espera (segundos) en backoff antes de reintentar.
_MAX_BACKOFF = float(os.environ.get("ENRICHMENT_MAX_BACKOFF", "120"))


# ── DB helpers ───────────────────────────────────────────────────────────────

def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _dsn() -> str:
    _load_dotenv()
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "25433")
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB", "geospots")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER", "geospots")
    password = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD", "geospots")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


async def create_pool(max_size: int = 4) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_dsn(), min_size=1, max_size=max_size)


async def _insert_claim(conn, review: dict, claim: dict, pipeline_run_id: str) -> int:
    return await conn.fetchval(
        """
        INSERT INTO extracted_claims (
            review_id, spot_id, signal_type, raw_value, extraction_confidence,
            extractor_name, extractor_version, pipeline_run_id, excerpt
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        review["id"],
        review["spot_id"],
        claim["signal"],
        str(claim["value"]),
        float(claim.get("confidence", 1.0)),
        claim.get("extractor_name", "regex_v1"),
        claim.get("extractor_version", "phase3-2026-05-23"),
        pipeline_run_id,
        claim.get("excerpt"),
    )


async def _insert_observation(conn, claim_id: int, review: dict, obs) -> int:
    return await conn.fetchval(
        """
        INSERT INTO normalized_observations (
            claim_id, spot_id, signal_type, value_num, value_bool, value_text,
            extraction_confidence, source_confidence, reviewer_confidence,
            observation_weight, observed_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        claim_id,
        review["spot_id"],
        obs.signal_type,
        obs.value_num,
        obs.value_bool,
        obs.value_text,
        obs.extraction_confidence,
        obs.source_confidence,
        obs.reviewer_confidence,
        obs.observation_weight,
        obs.observed_at,
    )


async def fetch_pending_reviews(conn, batch_size: int,
                                countries: list[str] | None = None) -> list[dict]:
    """Selecciona reviews pendientes con `r.llm_processed = FALSE`.

    Pre-Sprint 4 (T1.4/smoke Andorra) — `countries` filtra por ISO-2 vía
    JOIN a `spots.country_iso`. None = sin filtro (comportamiento legacy).
    Los ISO se comparan en minúsculas (tal como se almacenan en `spots`).
    """
    if countries:
        normalized = [c.lower() for c in countries]
        country_join = "JOIN spots s ON s.id = r.spot_id"
        country_filter = "AND s.country_iso = ANY($2)"
        params: list = [batch_size, normalized]
    else:
        country_join = ""
        country_filter = ""
        params = [batch_size]

    sql = f"""
    SELECT r.id, r.texto, r.texto_original, r.source, r.spot_id, r.fecha,
           COALESCE(sc.review_quality, 1.0) AS source_confidence,
           COALESCE(st.temperature, 'cold') AS temperature
    FROM reviews r
    {country_join}
    LEFT JOIN source_credibility sc ON sc.source = r.source
    LEFT JOIN spot_temperature st ON st.id = r.spot_id
    WHERE COALESCE(r.llm_processed, FALSE) = FALSE
      AND COALESCE(r.texto, r.texto_original) IS NOT NULL
      AND length(COALESCE(r.texto, r.texto_original)) > 3
      {country_filter}
    ORDER BY
      CASE COALESCE(st.temperature, 'cold')
        WHEN 'hot' THEN 0
        WHEN 'warm' THEN 1
        ELSE 2
      END,
      r.fecha DESC NULLS LAST,
      r.id DESC
    LIMIT $1
    """
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ── LLM call con retry + backoff ─────────────────────────────────────────────

async def _extract_claims_with_retry(
    text: str,
    review: dict,
    use_llm: bool,
    semaphore: asyncio.Semaphore,
    consecutive_errors: list[int],   # mutable contador compartido [n]
) -> list[dict]:
    """Llama a extract_claims con control de concurrencia y backoff exponencial.

    - Adquiere el semáforo antes de llamar al LLM (respeta ENRICHMENT_CONCURRENCY).
    - Si hay delay configurado (Gemini free tier), espera tras liberar.
    - En error transitorio (429, 5xx, timeout): backoff exponencial hasta _MAX_BACKOFF.
    - Incrementa consecutive_errors[0] en cada fallo; lo resetea en cada éxito.
    - Si consecutive_errors[0] >= _MAX_CONSECUTIVE_ERRORS lanza RuntimeError
      para que process_pending_reviews aborte el batch.
    """
    # Regex no necesita semáforo — es local y gratuito.
    from .claim_extractor import extract_claims_regex
    regex_result = extract_claims_regex(text)
    n_regex = len(regex_result)

    # Misma lógica de escalado que extract_claims():
    # - Texto < 120 chars → nunca LLM independientemente de los claims.
    # - Texto ≥ 120 chars + regex ≥ 3 claims → cobertura suficiente, no escalar.
    # - Texto ≥ 120 chars + regex 0-2 claims → LLM para complementar.
    # - use_llm=False → solo regex siempre.
    if not use_llm or len(text) < 120:
        return regex_result
    if n_regex >= 3:
        return regex_result

    # LLM path — necesita semáforo.
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        async with semaphore:
            try:
                result = await extract_claims(text, review, use_gemini=True)
                consecutive_errors[0] = 0          # éxito → reset contador
                if _INTER_REQUEST_DELAY > 0:
                    await asyncio.sleep(_INTER_REQUEST_DELAY)
                return result
            except Exception as exc:
                err_str = str(exc)
                # Detectar quota agotada (Gemini spending cap) — no reintentar.
                if "RESOURCE_EXHAUSTED" in err_str and "spending cap" in err_str.lower():
                    logger.error(
                        "[enrichment] Quota/spending cap agotado. "
                        "Abortando batch para no seguir quemando crédito."
                    )
                    consecutive_errors[0] = _MAX_CONSECUTIVE_ERRORS
                    raise RuntimeError("spending_cap_exhausted") from exc

                consecutive_errors[0] += 1
                if consecutive_errors[0] >= _MAX_CONSECUTIVE_ERRORS:
                    logger.error(
                        f"[enrichment] {_MAX_CONSECUTIVE_ERRORS} errores consecutivos. "
                        "Abortando batch. Revisar conectividad y crédito de la API."
                    )
                    raise RuntimeError("max_consecutive_errors_reached") from exc

                if attempt < max_attempts:
                    wait = min(_MAX_BACKOFF, 2 ** attempt)   # 2s, 4s, 8s
                    logger.warning(
                        f"[enrichment] LLM error (intento {attempt}/{max_attempts}), "
                        f"reintento en {wait:.0f}s: {exc}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        f"[enrichment] LLM fallido tras {max_attempts} intentos: {exc}"
                    )
                    raise RuntimeError("llm_extraction_failed") from exc

    return []


# ── Procesamiento de una review ───────────────────────────────────────────────

async def process_review(
    conn,
    review: dict,
    pipeline_run_id: str,
    dry_run: bool = False,
    use_llm: bool = True,
    semaphore: asyncio.Semaphore | None = None,
    consecutive_errors: list[int] | None = None,
) -> dict:
    raw_text = review.get("texto_original") or review.get("texto")
    cleaned = clean_review_full(raw_text)
    if not cleaned.informativo:
        if not dry_run:
            await conn.execute(
                """
                UPDATE reviews SET
                    texto_original = COALESCE(texto_original, texto),
                    texto_limpio = $1,
                    cleaned = TRUE,
                    informativo = FALSE,
                    llm_processed = TRUE,
                    llm_analysis = COALESCE(llm_analysis, '{}'::jsonb) || $2::jsonb
                WHERE id = $3
                """,
                cleaned.texto_limpio,
                json.dumps({"reason": "non_informative", "pipeline_run_id": pipeline_run_id}),
                review["id"],
            )
        return {"claims": 0, "observations": 0, "informativo": False}

    if semaphore is not None and consecutive_errors is not None:
        claims = await _extract_claims_with_retry(
            cleaned.texto_limpio, review, use_llm, semaphore, consecutive_errors
        )
    else:
        # Fallback sin throttling (tests, dry-run rápido)
        claims = await extract_claims(cleaned.texto_limpio, review, use_gemini=use_llm)

    observations = normalize_claims(
        claims,
        source_confidence=review.get("source_confidence", 1.0),
        reviewer_confidence=1.0,
        observed_at=review.get("fecha") or datetime.now(timezone.utc),
    )
    review_dsl = generate_review_dsl(claims)
    if dry_run:
        return {"claims": len(claims), "observations": len(observations), "informativo": True}

    async with conn.transaction():
        await conn.execute(
            """
            UPDATE reviews SET
                texto_original = COALESCE(texto_original, texto),
                texto_limpio = $1,
                texto_dsl = $2,
                cleaned = TRUE,
                informativo = TRUE,
                idioma = COALESCE(idioma, $3),
                llm_analysis = $4::jsonb
            WHERE id = $5
            """,
            cleaned.texto_limpio,
            review_dsl,
            cleaned.idioma,
            json.dumps({"claims": claims, "pipeline_run_id": pipeline_run_id}),
            review["id"],
        )
        for claim, obs in zip(claims, observations):
            claim_id = await _insert_claim(conn, review, claim, pipeline_run_id)
            await _insert_observation(conn, claim_id, review, obs)
            await update_semantic_state(conn, review["spot_id"], obs)
        await conn.execute("UPDATE reviews SET llm_processed = TRUE WHERE id = $1", review["id"])
    return {"claims": len(claims), "observations": len(observations), "informativo": True}


# ── Batch principal ───────────────────────────────────────────────────────────

async def process_pending_reviews(
    pool,
    batch_size: int = 100,
    dry_run: bool = False,
    use_llm: bool = True,
    concurrency: int | None = None,
    countries: list[str] | None = None,
) -> dict:
    pipeline_run_id = str(uuid.uuid4())
    stats = {
        "reviews": 0, "informative": 0, "claims": 0,
        "observations": 0, "errors": 0,
        "pipeline_run_id": pipeline_run_id,
    }

    concurrency = concurrency or _DEFAULT_CONCURRENCY
    semaphore = asyncio.Semaphore(concurrency)
    consecutive_errors: list[int] = [0]    # lista mutable para pasar por referencia

    async with pool.acquire() as conn:
        pending = await fetch_pending_reviews(conn, batch_size, countries=countries)

    if not pending:
        logger.info("[enrichment] No hay reviews pendientes.")
        return stats

    total = len(pending)
    logger.info(
        f"[enrichment] Iniciando batch: {total} reviews | "
        f"concurrency={concurrency} | inter_delay={_INTER_REQUEST_DELAY}s | "
        f"provider={os.environ.get('ENRICHMENT_PROVIDER','gemini')} | "
        f"run={pipeline_run_id[:8]}"
    )
    t_start = time.monotonic()

    for i, review in enumerate(pending, 1):
        try:
            async with pool.acquire() as conn:
                result = await process_review(
                    conn, review, pipeline_run_id,
                    dry_run=dry_run,
                    use_llm=use_llm,
                    semaphore=semaphore,
                    consecutive_errors=consecutive_errors,
                )
            stats["reviews"] += 1
            stats["informative"] += int(result["informativo"])
            stats["claims"] += result["claims"]
            stats["observations"] += result["observations"]

            if i % _PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - t_start
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                logger.info(
                    f"[enrichment] {i}/{total} reviews | "
                    f"{rate:.1f} rev/s | ETA {eta/60:.1f}min | "
                    f"claims={stats['claims']} obs={stats['observations']} "
                    f"errors={stats['errors']}"
                )

        except RuntimeError as exc:
            # max_consecutive_errors o spending_cap — abortar el batch limpiamente
            logger.error(f"[enrichment] Batch abortado: {exc} (procesadas {i-1}/{total})")
            stats["errors"] += 1
            break
        except Exception as exc:
            stats["errors"] += 1
            logger.exception(f"[enrichment] Error en review {review.get('id')}: {exc}")

    elapsed = time.monotonic() - t_start
    logger.info(
        f"[enrichment] Batch completado en {elapsed:.0f}s | "
        f"reviews={stats['reviews']} informativas={stats['informative']} "
        f"claims={stats['claims']} obs={stats['observations']} errors={stats['errors']}"
    )
    return stats


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GeoSpots enrichment worker")
    parser.add_argument("--batch-size", type=int, default=1000,
                        help="Reviews a procesar por ejecución (default 1000)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="Llamadas LLM simultáneas (default: ENRICHMENT_CONCURRENCY env o 8)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Procesa pero no escribe en DB")
    parser.add_argument("--no-llm", action="store_true",
                        help="Solo regex, sin llamadas LLM (gratis)")
    # Alias legacy
    parser.add_argument("--no-gemini", action="store_true",
                        help="Alias de --no-llm (compatibilidad)")
    parser.add_argument(
        "--country", type=str, default=None,
        help="ISO-2 code(s), coma-separados (ej. AD o ES,PT). "
             "Filtra reviews por spots.country_iso. Pre-Sprint 4 (smoke Andorra).",
    )
    args = parser.parse_args(argv)

    use_llm = not (args.no_llm or args.no_gemini)

    countries: list[str] | None = None
    if args.country:
        countries = [c.strip().upper() for c in args.country.split(",") if c.strip()]

    pool = await create_pool()
    try:
        stats = await process_pending_reviews(
            pool,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            use_llm=use_llm,
            concurrency=args.concurrency,
            countries=countries,
        )
        logger.info(f"[enrichment] stats={stats}")
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
