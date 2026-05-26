"""Cliente Gemini Batch API para enrichment v2.

Flujo:
  1. `build_request(spot_id, user_prompt, cache_name)` → dict con la request.
  2. `submit_batch(requests, model)` → batch_name (persiste en enrichment_batches).
  3. `poll_batch(batch_name)` → state ('running'|'succeeded'|'failed'|'partial').
  4. `iter_results(batch_name)` → yield (key, payload_text | error_dict).

El SDK de google-genai (>=1.0) expone `client.batches.create / get / list_results`.
Como las APIs pueden variar entre versiones del SDK, encapsulamos cada operación
en una función pequeña que puede ajustarse sin tocar el resto del pipeline.

NO ingesta resultados — eso es PR 4 (enrichment/ingest_v2.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Iterable, Iterator

from loguru import logger

from .gemini_cache import DEFAULT_MODEL, _genai_client, _run_in_thread

# Estados externos (Gemini) → estados internos
STATE_MAP = {
    "JOB_STATE_QUEUED": "pending",
    "JOB_STATE_PENDING": "pending",
    "JOB_STATE_RUNNING": "running",
    "JOB_STATE_SUCCEEDED": "succeeded",
    "JOB_STATE_FAILED": "failed",
    "JOB_STATE_CANCELLED": "cancelled",
    "JOB_STATE_EXPIRED": "failed",
    "JOB_STATE_PARTIALLY_SUCCEEDED": "partial",
}


@dataclass
class BatchSubmission:
    batch_name: str
    spot_ids: list[int]
    enrichment_version: int
    llm_model: str
    n_requested: int


def build_request(spot_id: int, user_prompt: str, cache_name: str | None) -> dict:
    """Construye el payload de UNA request para Batch API.

    `key` se usa para correlacionar respuesta ↔ spot.
    """
    config: dict = {
        "response_mime_type": "application/json",
        "temperature": 0.2,
    }
    if cache_name:
        config["cached_content"] = cache_name
    # Si no hay cache_name, el caller debe haber metido el system_instruction
    # en el SYSTEM_PROMPT vía batch-wide config (ver submit_batch).

    return {
        "key": f"spot_{spot_id}",
        "request": {
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "config": config,
        },
    }


def _extract_batch_name(batch_obj) -> str:
    """El objeto que devuelve client.batches.create puede tener `.name` o similar."""
    name = getattr(batch_obj, "name", None)
    if name:
        return str(name)
    # Algunas versiones devuelven dict-like
    if isinstance(batch_obj, dict):
        return str(batch_obj.get("name") or batch_obj.get("batch_name") or "")
    raise RuntimeError(f"no se pudo extraer name del batch: {batch_obj!r}")


def _submit_batch_sync(requests: list[dict], model: str, display_name: str) -> str:
    client = _genai_client()
    # google-genai >=1.0: client.batches.create(model=..., src=...)
    # `src` admite lista inline de requests o referencia a archivo subido.
    # Para volúmenes <100MB usamos inline; para masivos subiríamos via files API.
    payload = {
        "model": model,
        "src": requests,
        "config": {"display_name": display_name},
    }
    batch = client.batches.create(**payload)
    return _extract_batch_name(batch)


async def submit_batch(
    conn,
    requests: list[dict],
    spot_ids: list[int],
    *,
    enrichment_version: int,
    model: str = DEFAULT_MODEL,
    display_name: str | None = None,
) -> BatchSubmission:
    """Envía un batch y registra en `enrichment_batches`."""
    if len(requests) != len(spot_ids):
        raise ValueError(f"requests ({len(requests)}) y spot_ids ({len(spot_ids)}) deben coincidir")
    if not requests:
        raise ValueError("requests vacío")

    display_name = display_name or f"geospots-v{enrichment_version}-{int(time.time())}"
    logger.info(f"[gemini_batch] enviando batch {display_name} n={len(requests)}")

    batch_name = await _run_in_thread(_submit_batch_sync, requests, model, display_name)

    await conn.execute(
        """
        INSERT INTO enrichment_batches
            (batch_name, enrichment_version, llm_model, spot_ids, state, n_requested)
        VALUES ($1, $2, $3, $4, 'pending', $5)
        ON CONFLICT (batch_name) DO NOTHING
        """,
        batch_name,
        enrichment_version,
        model,
        spot_ids,
        len(requests),
    )

    logger.info(f"[gemini_batch] batch creado: {batch_name}")
    return BatchSubmission(
        batch_name=batch_name,
        spot_ids=list(spot_ids),
        enrichment_version=enrichment_version,
        llm_model=model,
        n_requested=len(requests),
    )


def _get_batch_state_sync(batch_name: str) -> tuple[str, dict]:
    """Devuelve (internal_state, raw_dict)."""
    client = _genai_client()
    batch = client.batches.get(name=batch_name)
    raw_state = getattr(batch, "state", None)
    if hasattr(raw_state, "name"):  # enum
        raw_state = raw_state.name
    internal = STATE_MAP.get(str(raw_state), "running")

    # Stats si disponibles
    stats = {}
    for attr in ("total_request_count", "completed_request_count",
                 "failed_request_count", "pending_request_count"):
        v = getattr(batch, attr, None)
        if v is not None:
            stats[attr] = int(v)

    return internal, stats


async def poll_batch(
    conn,
    batch_name: str,
    *,
    interval_seconds: float = 300.0,
    max_wait_seconds: float = 26 * 3600,  # 26h (Batch API SLA es 24h)
) -> str:
    """Polling con backoff suave hasta estado terminal. Devuelve estado final."""
    start = time.time()
    last_logged = 0.0

    while True:
        internal, stats = await _run_in_thread(_get_batch_state_sync, batch_name)

        await conn.execute(
            """
            UPDATE enrichment_batches
            SET state = $2,
                n_succeeded = $3,
                n_failed = $4
            WHERE batch_name = $1
            """,
            batch_name,
            internal,
            stats.get("completed_request_count"),
            stats.get("failed_request_count"),
        )

        if internal in ("succeeded", "failed", "partial", "cancelled"):
            await conn.execute(
                "UPDATE enrichment_batches SET completed_at = NOW() WHERE batch_name = $1",
                batch_name,
            )
            logger.info(f"[gemini_batch] {batch_name} → {internal} ({stats})")
            return internal

        elapsed = time.time() - start
        if elapsed > max_wait_seconds:
            await conn.execute(
                "UPDATE enrichment_batches SET state='failed', error_msg='timeout', completed_at=NOW() WHERE batch_name = $1",
                batch_name,
            )
            logger.error(f"[gemini_batch] timeout esperando {batch_name} ({elapsed:.0f}s)")
            return "failed"

        # Log progress cada ~10 min
        if elapsed - last_logged > 600:
            logger.info(f"[gemini_batch] {batch_name} state={internal} elapsed={elapsed:.0f}s stats={stats}")
            last_logged = elapsed

        await asyncio.sleep(interval_seconds)


def _iter_results_sync(batch_name: str) -> Iterator[dict]:
    """Yield dicts con {key, text?, error?, usage?}."""
    client = _genai_client()
    batch = client.batches.get(name=batch_name)

    # Caminos posibles según SDK:
    # 1. batch.dest.inlined_responses[]
    # 2. batch.results / iterable
    inlined = None
    dest = getattr(batch, "dest", None)
    if dest is not None:
        inlined = getattr(dest, "inlined_responses", None)
    if inlined is None:
        inlined = getattr(batch, "results", None)
    if inlined is None:
        # Fallback: file output. No implementado en v1 (queda pendiente para volúmenes grandes).
        raise NotImplementedError(
            f"batch {batch_name} usa output por archivo; no soportado en v1. "
            "Implementar download via files API."
        )

    for item in inlined:
        key = getattr(item, "key", None) or (item.get("key") if isinstance(item, dict) else None)
        response = getattr(item, "response", None) or (item.get("response") if isinstance(item, dict) else None)
        error = getattr(item, "error", None) or (item.get("error") if isinstance(item, dict) else None)

        if error:
            yield {"key": key, "error": str(error)}
            continue

        # Extraer texto. response puede ser GenerateContentResponse o dict.
        text = ""
        usage = {}
        if response is not None:
            text = getattr(response, "text", None) or ""
            if not text and isinstance(response, dict):
                # buscar text en candidates[0].content.parts[0].text
                try:
                    text = response["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError, TypeError):
                    text = ""
            meta = getattr(response, "usage_metadata", None)
            if meta is not None:
                for k in ("prompt_token_count", "candidates_token_count",
                          "cached_content_token_count", "total_token_count"):
                    v = getattr(meta, k, None)
                    if v is not None:
                        usage[k] = int(v)

        yield {"key": key, "text": text, "usage": usage}


async def iter_results(batch_name: str) -> list[dict]:
    """Devuelve TODOS los resultados de un batch como lista de dicts.

    No es streaming (los batches inline ya están en memoria del SDK). Sí
    estructurados como list[dict] con keys: `key`, `text`/`error`, `usage`.
    """
    return await _run_in_thread(lambda: list(_iter_results_sync(batch_name)))


def parse_key_to_spot_id(key: str) -> int | None:
    """`spot_42` → 42."""
    if not key or not isinstance(key, str):
        return None
    if key.startswith("spot_"):
        try:
            return int(key[5:])
        except ValueError:
            return None
    try:
        return int(key)
    except ValueError:
        return None
