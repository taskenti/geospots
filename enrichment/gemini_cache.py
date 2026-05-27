"""Gestión del context cache de Gemini para el system prompt v2.

El system prompt v2 (~870 tokens) es idéntico en todas las llamadas de un
batch. Cacheándolo en Gemini context cache se paga ~25% del coste de input
sobre esos tokens. Persistimos el `cache_name` en DB para reutilizar entre
batches mientras esté vigente.

Mínimo TTL recomendado: 1h. Lo refrescamos cuando expires_at - now < 10 min.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger

from .prompts import ENRICHMENT_VERSION, SYSTEM_PROMPT_V2

DEFAULT_MODEL = os.environ.get("GEMINI_ENRICHMENT_MODEL", "gemini-2.5-flash-lite")
DEFAULT_CACHE_TTL_SECONDS = 3600
REFRESH_MARGIN_SECONDS = 600  # refrescar si quedan <10 min


@dataclass
class CachedSystem:
    cache_name: str
    enrichment_version: int
    llm_model: str
    expires_at: datetime
    token_count: int | None = None


def _genai_client():
    """Lazy import + lazy client para no romper tests que no llaman API."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY no definida en entorno")
    from google import genai  # type: ignore

    return genai.Client(api_key=api_key)


async def _select_active_cache(conn, version: int, model: str) -> CachedSystem | None:
    row = await conn.fetchrow(
        """
        SELECT cache_name, enrichment_version, llm_model, expires_at, cache_token_count
        FROM enrichment_cache_state
        WHERE enrichment_version = $1
          AND llm_model = $2
          AND expires_at > NOW() + INTERVAL '10 minutes'
        ORDER BY expires_at DESC
        LIMIT 1
        """,
        version,
        model,
    )
    if not row:
        return None
    return CachedSystem(
        cache_name=row["cache_name"],
        enrichment_version=row["enrichment_version"],
        llm_model=row["llm_model"],
        expires_at=row["expires_at"],
        token_count=row["cache_token_count"],
    )


def _create_cache_sync(model: str, ttl_seconds: int) -> tuple[str, int | None]:
    """Crea cache en Gemini (síncrono — el SDK no es async en esta versión).

    Devuelve (cache_name, token_count|None).
    """
    client = _genai_client()
    # google-genai SDK >=1.0: client.caches.create(...)
    # El contenido cacheable se pasa como system_instruction (formato preferido)
    # o como contents[]. Probamos system_instruction; si la API lo rechaza, fallback.
    try:
        cache = client.caches.create(
            model=model,
            config={
                "system_instruction": SYSTEM_PROMPT_V2,
                "ttl": f"{ttl_seconds}s",
                "display_name": f"geospots-enrichment-v{ENRICHMENT_VERSION}",
            },
        )
    except Exception as exc:
        # Fallback: algunos SDKs usan contents en lugar de system_instruction.
        logger.warning(f"[gemini_cache] create con system_instruction falló: {exc} — probando contents")
        cache = client.caches.create(
            model=model,
            config={
                "contents": [{"role": "user", "parts": [{"text": SYSTEM_PROMPT_V2}]}],
                "ttl": f"{ttl_seconds}s",
                "display_name": f"geospots-enrichment-v{ENRICHMENT_VERSION}",
            },
        )

    cache_name = getattr(cache, "name", None) or getattr(cache, "cached_content_name", None)
    if not cache_name:
        raise RuntimeError(f"cache creado pero sin name: {cache!r}")

    # Token count puede venir en cache.usage_metadata.total_token_count o similar.
    token_count = None
    usage = getattr(cache, "usage_metadata", None)
    if usage is not None:
        token_count = getattr(usage, "total_token_count", None)

    return cache_name, token_count


async def ensure_system_cache(
    conn,
    *,
    model: str = DEFAULT_MODEL,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    version: int = ENRICHMENT_VERSION,
) -> CachedSystem:
    """Devuelve un cache vigente del system prompt v2; lo crea si no hay.

    El cache es por (enrichment_version, model). Si bumpeas `ENRICHMENT_VERSION`
    el lookup deja de encontrar caches viejos automáticamente.
    """
    cached = await _select_active_cache(conn, version, model)
    if cached:
        logger.debug(f"[gemini_cache] reutilizando {cached.cache_name} (expira {cached.expires_at})")
        return cached

    logger.info(f"[gemini_cache] creando nuevo cache para v{version}/{model} (ttl={ttl_seconds}s)")
    cache_name, token_count = await _run_in_thread(_create_cache_sync, model, ttl_seconds)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    await conn.execute(
        """
        INSERT INTO enrichment_cache_state
            (enrichment_version, llm_model, cache_name, cache_token_count, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (cache_name) DO UPDATE SET
            expires_at = EXCLUDED.expires_at,
            cache_token_count = COALESCE(EXCLUDED.cache_token_count, enrichment_cache_state.cache_token_count)
        """,
        version,
        model,
        cache_name,
        token_count,
        expires_at,
    )

    logger.info(f"[gemini_cache] creado {cache_name} ({token_count} tokens) hasta {expires_at}")
    return CachedSystem(
        cache_name=cache_name,
        enrichment_version=version,
        llm_model=model,
        expires_at=expires_at,
        token_count=token_count,
    )


async def _run_in_thread(fn, *args, **kwargs):
    """Pequeño wrapper para llamadas síncronas del SDK."""
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


# ──────────────────────────────────────────────────────────────────────
# Llamada síncrona puntual (sanity check / debug / urgencias)
# NO usa Batch API. Útil para validar el prompt antes de invertir en batch.
# ──────────────────────────────────────────────────────────────────────


def call_gemini_once_sync(
    user_prompt: str,
    *,
    cache_name: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
    system_prompt: str | None = None,
    response_format: str = "json",
) -> tuple[str, dict]:
    """Llamada síncrona puntual. Devuelve (texto_respuesta, usage_metadata_dict).

    Si `cache_name` está, usa context caching. Si no, mete el system prompt inline.

    `system_prompt=None` → SYSTEM_PROMPT_V2 (compat orchestrator v2).
    `system_prompt=""`   → sin system_instruction (el user_prompt lleva las instrucciones).
    `response_format="text"` → no fuerza JSON.
    """
    client = _genai_client()
    config: dict = {"temperature": temperature}
    if response_format == "json":
        config["response_mime_type"] = "application/json"

    if cache_name:
        config["cached_content"] = cache_name
    else:
        if system_prompt is None:
            system_prompt = SYSTEM_PROMPT_V2
        if system_prompt:
            config["system_instruction"] = system_prompt

    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=config,
    )

    text = getattr(response, "text", "") or ""
    usage = {}
    meta = getattr(response, "usage_metadata", None)
    if meta is not None:
        for k in ("prompt_token_count", "candidates_token_count",
                  "cached_content_token_count", "total_token_count"):
            v = getattr(meta, k, None)
            if v is not None:
                usage[k] = int(v)
    return text, usage
