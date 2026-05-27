"""Tiered claim extraction: regex first, LLM (provider-agnostic) as optional fallback."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from loguru import logger

from .llm_provider import call_llm_sync, get_active_model, get_provider_name
from .prompts import build_extraction_prompt

EXTRACTOR_VERSION = "phase3-2026-05-23"


@dataclass(frozen=True)
class ExtractedClaim:
    signal: str
    value: str
    confidence: float
    excerpt: str
    extractor_name: str = "regex_v1"
    extractor_version: str = EXTRACTOR_VERSION

    def as_dict(self) -> dict:
        return {
            "signal": self.signal,
            "value": self.value,
            "confidence": self.confidence,
            "excerpt": self.excerpt,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
        }


PATTERNS: list[tuple[str, str, float, tuple[str, ...]]] = [
    ("quietness", "0.9", 0.86, ("tranquil", "tranquilo", "calm", "quiet", "silenc", "ruhig", "calme")),
    ("quietness", "0.2", 0.84, ("ruidoso", "noisy", "bruyant", "loud", "noise all night")),
    ("noise", "0.8", 0.84, ("ruido", "noise", "loud", "bruit", "laerm", "lärm")),
    ("road_noise", "0.8", 0.88, ("carretera", "autopista", "road noise", "traffic", "trafico", "trucks")),
    ("police_risk", "0.85", 0.9, ("policia", "police", "multa", "fine", "verboten", "expuls", "evicted")),
    ("theft_risk", "0.85", 0.9, ("robo", "robbed", "theft", "break in", "broken into", "stolen")),
    ("safety", "0.85", 0.82, ("seguro", "safe", "security", "sicher", "sentimos seguros")),
    ("safety", "0.2", 0.82, ("inseguro", "unsafe", "dangerous", "peligroso")),
    ("beauty", "0.9", 0.82, ("bonito", "beautiful", "spectacular", "precioso", "amazing view", "belle vue")),
    ("cleanliness", "0.85", 0.8, ("limpio", "clean", "propre", "sauber")),
    ("cleanliness", "0.15", 0.82, ("sucio", "dirty", "trash", "basura", "garbage", "sale")),
    ("sea_view", "true", 0.88, ("vistas al mar", "sea view", "ocean view", "vue mer", "meerblick")),
    ("mountain_view", "true", 0.86, ("vistas a montana", "mountain view", "vue montagne", "bergblick")),
    ("lake_nearby", "true", 0.84, ("lago", "lake", "lac", "see nearby")),
    ("shade_morning", "true", 0.75, ("sombra por la manana", "morning shade")),
    ("shade_afternoon", "true", 0.75, ("sombra por la tarde", "afternoon shade")),
    ("large_vehicle", "0.85", 0.82, (">7m", "large motorhome", "big rig", "autocaravana grande", "grandes vehiculos")),
    ("large_vehicle", "0.15", 0.82, ("no apto para grandes", "too narrow", "narrow access", "not for large")),
    ("road_quality", "0.85", 0.8, ("buen acceso", "good road", "asphalt", "asfalto")),
    ("road_quality", "0.2", 0.82, ("mal camino", "bad road", "dirt track", "bumpy", "gravel road")),
    ("overnight_safe", "true", 0.86, ("pernocta", "overnight", "slept", "dormir", "night without problem")),
    ("overnight_safe", "false", 0.9, ("no overnight", "no pernocta", "prohibido pernoctar", "overnight forbidden")),
    ("crowd_level", "0.85", 0.78, ("lleno", "crowded", "busy", "packed", "masificado")),
    ("crowd_level", "0.15", 0.76, ("empty", "vacio", "nadie", "alone", "sin gente")),
    ("wind_exposure", "0.85", 0.78, ("windy", "mucho viento", "ventoso", "exposed to wind")),
    ("stealth", "0.85", 0.76, ("discreto", "hidden", "stealth", "apartado")),
]


def _excerpt(text: str, needle: str, window: int = 80) -> str:
    pos = text.lower().find(needle.lower())
    if pos < 0:
        return text[:window]
    start = max(0, pos - window // 2)
    end = min(len(text), pos + len(needle) + window // 2)
    return text[start:end].strip()


def extract_claims_regex(text: str) -> list[dict]:
    lowered = text.lower()
    claims: list[ExtractedClaim] = []
    seen: set[tuple[str, str]] = set()
    for signal, value, confidence, needles in PATTERNS:
        for needle in needles:
            if needle in lowered:
                key = (signal, value)
                if key in seen:
                    continue
                seen.add(key)
                claims.append(ExtractedClaim(signal, value, confidence, _excerpt(text, needle)))
                break
    return [claim.as_dict() for claim in claims]


def _parse_json_response(text: str, extractor_name: str) -> list[dict]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE).strip()
    data = json.loads(cleaned)
    raw_claims = data.get("claims", []) if isinstance(data, dict) else []
    claims = []
    for item in raw_claims:
        signal = item.get("signal")
        value = item.get("value")
        if not signal or value is None:
            continue
        claims.append(
            ExtractedClaim(
                signal=str(signal),
                value=str(value),
                confidence=float(item.get("confidence", 0.7)),
                excerpt=str(item.get("excerpt", ""))[:500],
                extractor_name=extractor_name,
            ).as_dict()
        )
    return claims


async def extract_claims_llm(text: str) -> list[dict]:
    """Fallback LLM. Provider y modelo vienen de ENV (ENRICHMENT_PROVIDER,
    GEMINI_ENRICHMENT_MODEL, DEEPSEEK_ENRICHMENT_MODEL). El prompt de extracción
    ya contiene las instrucciones completas, así que se pasa como user_prompt y
    se omite el system message (system_prompt="").
    """
    try:
        prompt = build_extraction_prompt(text)
        resp = await asyncio.to_thread(
            call_llm_sync,
            prompt,
            system_prompt="",
            response_format="json",
        )
        extractor_name = f"llm_{resp.provider}"
        return _parse_json_response(resp.text or "", extractor_name=extractor_name)
    except Exception as exc:
        logger.warning(
            f"[enrichment] LLM extraction failed "
            f"(provider={get_provider_name()} model={get_active_model()}): {exc}"
        )
        return []


# Alias retro-compatible para tests/jobs antiguos que lo importan
extract_claims_gemini = extract_claims_llm


async def extract_claims(text: str, review: dict | None = None, use_gemini: bool = True) -> list[dict]:
    """`use_gemini` mantiene el nombre por compat; activa el fallback LLM
    (sea Gemini o DeepSeek según ENRICHMENT_PROVIDER)."""
    regex_claims = extract_claims_regex(text)
    if regex_claims or not use_gemini:
        return regex_claims
    return await extract_claims_llm(text)
