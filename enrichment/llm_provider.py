"""Abstracción de provider LLM para enrichment v2.

Soporta:
  - gemini  (google-genai SDK, Batch API, context caching)
  - deepseek (OpenAI-compatible REST, sin batch — paraleliza sync)

Selección vía env:
  ENRICHMENT_PROVIDER = "gemini" | "deepseek"  (default: gemini)
  GEMINI_ENRICHMENT_MODEL  (default: gemini-2.5-flash-lite)
  DEEPSEEK_ENRICHMENT_MODEL (default: deepseek-v4-flash)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from .prompts import SYSTEM_PROMPT_V2


@dataclass
class LLMResponse:
    text: str
    usage: dict[str, int]
    provider: str
    model: str


def get_provider_name() -> str:
    return os.environ.get("ENRICHMENT_PROVIDER", "gemini").lower()


def get_active_model() -> str:
    provider = get_provider_name()
    if provider == "deepseek":
        return os.environ.get("DEEPSEEK_ENRICHMENT_MODEL", "deepseek-v4-flash")
    return os.environ.get("GEMINI_ENRICHMENT_MODEL", "gemini-2.5-flash-lite")


# ───────────────────────────────────────────────────────────────────
# DeepSeek (OpenAI-compatible)
# ───────────────────────────────────────────────────────────────────

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_TIMEOUT = 120.0


def call_deepseek_sync(
    user_prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.2,
    thinking: bool = False,
    api_key: str | None = None,
) -> LLMResponse:
    """Llamada síncrona DeepSeek. JSON output forzado.

    `thinking=False` desactiva el modo razonamiento (default ON en v4) para
    evitar output tokens innecesarios y respuestas no-JSON.
    """
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY no definida")

    model = model or os.environ.get("DEEPSEEK_ENRICHMENT_MODEL", "deepseek-v4-flash")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_V2},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        # Modo thinking se controla en deepseek-v4 vía parámetro custom.
        # Si la API rechaza el flag, lo retira el bloque except.
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }

    with httpx.Client(timeout=DEEPSEEK_TIMEOUT) as client:
        resp = client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload),
        )
        if resp.status_code == 400 and "thinking" in resp.text.lower():
            # Reintentar sin el campo thinking (no soportado en algunos modelos)
            payload.pop("thinking", None)
            resp = client.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                content=json.dumps(payload),
            )
        resp.raise_for_status()
        data = resp.json()

    text = ""
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"[deepseek] respuesta sin choices: {data}")
        raise RuntimeError(f"DeepSeek respuesta inválida: {e}")

    usage_raw = data.get("usage") or {}
    usage = {
        "prompt_token_count": int(usage_raw.get("prompt_tokens", 0)),
        "candidates_token_count": int(usage_raw.get("completion_tokens", 0)),
        "total_token_count": int(usage_raw.get("total_tokens", 0)),
        "cached_content_token_count": int(
            (usage_raw.get("prompt_cache_hit_tokens") or 0)
        ),
    }
    return LLMResponse(text=text, usage=usage, provider="deepseek", model=model)


# ───────────────────────────────────────────────────────────────────
# Gemini (síncrono, vía gemini_cache.call_gemini_once_sync)
# ───────────────────────────────────────────────────────────────────


def call_gemini_sync(user_prompt: str, *, cache_name: str | None = None,
                    model: str | None = None, temperature: float = 0.2) -> LLMResponse:
    from .gemini_cache import DEFAULT_MODEL, call_gemini_once_sync

    model = model or DEFAULT_MODEL
    text, usage = call_gemini_once_sync(
        user_prompt, cache_name=cache_name, model=model, temperature=temperature
    )
    return LLMResponse(text=text, usage=usage, provider="gemini", model=model)


# ───────────────────────────────────────────────────────────────────
# Dispatch por provider activo
# ───────────────────────────────────────────────────────────────────


def call_llm_sync(user_prompt: str, **kwargs) -> LLMResponse:
    """Llamada síncrona al provider activo (definido por env)."""
    provider = get_provider_name()
    if provider == "deepseek":
        return call_deepseek_sync(user_prompt, **kwargs)
    return call_gemini_sync(user_prompt, **kwargs)


# ───────────────────────────────────────────────────────────────────
# Estimación de coste (informativa)
# ───────────────────────────────────────────────────────────────────

# $/M tokens. Cache miss para input.
PRICING = {
    # Gemini
    "gemini-2.5-flash":      {"in": 0.30, "out": 2.50, "cache_hit": 0.075},
    "gemini-2.5-flash-lite": {"in": 0.10, "out": 0.40, "cache_hit": 0.025},
    "gemini-flash-latest":   {"in": 0.30, "out": 2.50, "cache_hit": 0.075},
    # DeepSeek
    "deepseek-v4-flash":     {"in": 0.14, "out": 0.28, "cache_hit": 0.0028},
    "deepseek-v4-pro":       {"in": 0.435, "out": 0.87, "cache_hit": 0.003625},
}


def estimate_cost(model: str, usage: dict) -> float:
    p = PRICING.get(model)
    if not p:
        return 0.0
    in_tokens = usage.get("prompt_token_count", 0)
    out_tokens = usage.get("candidates_token_count", 0)
    cached = usage.get("cached_content_token_count", 0)
    uncached = max(0, in_tokens - cached)
    return (
        uncached * p["in"] / 1e6
        + cached * p["cache_hit"] / 1e6
        + out_tokens * p["out"] / 1e6
    )
