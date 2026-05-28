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
    system_prompt: str | None = None,
    response_format: str = "json",
) -> LLMResponse:
    """Llamada síncrona DeepSeek. JSON output forzado por default.

    `thinking=False` desactiva el modo razonamiento (default ON en v4) para
    evitar output tokens innecesarios y respuestas no-JSON.

    `system_prompt=None` → SYSTEM_PROMPT_V2 (compat orchestrator v2).
    `system_prompt=""`   → sin mensaje system (el user_prompt lleva las instrucciones).
    `response_format="text"` → no fuerza JSON (búsqueda libre, summaries, etc.).
    """
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY no definida")

    model = model or os.environ.get("DEEPSEEK_ENRICHMENT_MODEL", "deepseek-v4-flash")

    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_V2

    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        # T1.1: subido de 1500 → 2500 — outputs `very_rich` (camping con 30+
        # servicios + 8 frases de summary + arrays largos) podían quedar truncados
        # con 1500, lo que se manifiesta como parse error silencioso aguas abajo.
        # 2500 ≈ 1.5KB de JSON, suficiente para el peor caso observado.
        "max_tokens": 2500,
        # Modo thinking se controla en deepseek-v4 vía parámetro custom.
        # Si la API rechaza el flag, lo retira el bloque except.
        "thinking": {"type": "enabled" if thinking else "disabled"},
    }
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}

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
    # T1.8 — log per-call cache_hit_ratio. La persistencia en `llm_call_metrics`
    # vive en el caller (orchestrator_v2._process_one_spot) — solo allí sabemos
    # spot_id/country/pipeline_run_id.
    prompt_t = usage["prompt_token_count"]
    cached_t = usage["cached_content_token_count"]
    ratio = (cached_t / prompt_t) if prompt_t > 0 else 0.0
    logger.info(
        f"[deepseek.usage] model={model} prompt={prompt_t} cached={cached_t} "
        f"completion={usage['candidates_token_count']} cache_hit_ratio={ratio:.3f}"
    )
    return LLMResponse(text=text, usage=usage, provider="deepseek", model=model)


# ───────────────────────────────────────────────────────────────────
# Gemini (síncrono, vía gemini_cache.call_gemini_once_sync)
# ───────────────────────────────────────────────────────────────────


def call_gemini_sync(user_prompt: str, *, cache_name: str | None = None,
                    model: str | None = None, temperature: float = 0.2,
                    system_prompt: str | None = None,
                    response_format: str = "json") -> LLMResponse:
    from .gemini_cache import DEFAULT_MODEL, call_gemini_once_sync

    model = model or DEFAULT_MODEL
    text, usage = call_gemini_once_sync(
        user_prompt, cache_name=cache_name, model=model, temperature=temperature,
        system_prompt=system_prompt, response_format=response_format,
    )
    return LLMResponse(text=text, usage=usage, provider="gemini", model=model)


# ───────────────────────────────────────────────────────────────────
# Dispatch por provider activo
# ───────────────────────────────────────────────────────────────────


def call_llm_sync(user_prompt: str, **kwargs) -> LLMResponse:
    """Llamada síncrona al provider activo (definido por env).

    kwargs reenviados a call_deepseek_sync/call_gemini_sync:
      model, temperature, system_prompt, response_format, (deepseek: thinking, api_key,
      gemini: cache_name).
    """
    provider = get_provider_name()
    if provider == "deepseek":
        # cache_name es específico de Gemini; lo descartamos para deepseek.
        kwargs.pop("cache_name", None)
        return call_deepseek_sync(user_prompt, **kwargs)
    # thinking es específico de DeepSeek; lo descartamos para Gemini.
    kwargs.pop("thinking", None)
    kwargs.pop("api_key", None)
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
    "deepseek-chat":         {"in": 0.14, "out": 0.28, "cache_hit": 0.0028},
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
