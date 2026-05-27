"""Parser y validador del JSON que devuelve Gemini para el enrichment v2.

Aislado del cliente de red para poder testear sin tocar la API.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Señales válidas (debe coincidir con db/schema.sql signal_types tras migration_phase3_v2.sql)
VALID_SIGNALS: set[str] = {
    # numéricas existentes
    "quietness", "noise", "road_noise", "party_noise", "train_noise",
    "safety", "police_risk", "theft_risk",
    "beauty", "cleanliness",
    "large_vehicle", "road_quality",
    "crowd_level", "wind_exposure", "stealth",
    "cell_coverage", "mosquitoes",
    # booleanas existentes
    "sea_view", "mountain_view", "lake_nearby",
    "shade_morning", "shade_afternoon",
    "overnight_safe", "wild_camping_legal",
    "dog_friendly", "family_friendly", "accessible_pmr",
    "water_working", "electricity_working", "dump_station_working",
    # text categóricas v2
    "noise_source", "parking_capacity",
}

NUMERIC_SIGNALS: set[str] = {
    "quietness", "noise", "road_noise", "party_noise", "train_noise",
    "safety", "police_risk", "theft_risk",
    "beauty", "cleanliness",
    "large_vehicle", "road_quality",
    "crowd_level", "wind_exposure", "stealth",
    "cell_coverage", "mosquitoes",
}

BOOLEAN_SIGNALS: set[str] = {
    "sea_view", "mountain_view", "lake_nearby",
    "shade_morning", "shade_afternoon",
    "overnight_safe", "wild_camping_legal",
    "dog_friendly", "family_friendly", "accessible_pmr",
    "water_working", "electricity_working", "dump_station_working",
}

TEXT_SIGNALS: set[str] = {"noise_source", "parking_capacity"}

# Vocabulario controlado para text-signals
NOISE_SOURCE_VOCAB = {"highway", "road", "train", "airport", "sea", "wind",
                      "party", "industry", "crowd", "other"}
PARKING_CAPACITY_VOCAB = {"small", "medium", "large"}


class ParseError(Exception):
    """Respuesta de Gemini malformada."""


@dataclass
class ValidatedClaim:
    signal: str
    value: Any            # float | bool | str
    confidence: float
    review_id: int | None  # None = "description" (descripciones del spot, no review)
    excerpt: str
    raw: dict


@dataclass
class ValidatedEnrichment:
    claims: list[ValidatedClaim]
    summary: str | None       # v4: single English narrative (was summary_es/summary_en)
    tags: list[str]
    best_for: list[str]
    best_season: str | None
    avoid_season: str | None
    errors: list[str] = field(default_factory=list)  # non-fatal warnings

    # ---- v3 compat shims (read-only) — temporary, helps tests/ingest during migration
    @property
    def summary_es(self) -> str | None:
        """Deprecated v3 field. v4 emits English only; returns None.

        Old code that reads parsed.summary_es will see None; new code uses .summary.
        """
        return None

    @property
    def summary_en(self) -> str | None:
        """Deprecated v3 field. Mapped to .summary (now English-only)."""
        return self.summary


_MARKDOWN_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_markdown_fence(text: str) -> str:
    return _MARKDOWN_FENCE.sub("", text).strip()


def _coerce_review_id(value: Any) -> int | None:
    """Acepta int directo, str numérica, o "description"/"services"/"desc"/null → None.

    Nota: "services" (v3) y "description" se mapean a None — el observed_at
    se calcula como NOW() en ingest_v2 al no tener review_id concreto.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool es int en python; descartar
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("description", "desc", "descriptions", "services", "service", ""):
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _coerce_numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "si", "sí", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def _validate_claim(raw: dict, errors: list[str]) -> ValidatedClaim | None:
    signal = raw.get("signal")
    if not isinstance(signal, str) or signal not in VALID_SIGNALS:
        errors.append(f"claim signal inválido o desconocido: {signal!r}")
        return None

    conf = raw.get("confidence", 0.7)
    try:
        conf_f = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        errors.append(f"claim confidence inválida en {signal}: {conf!r}")
        conf_f = 0.5

    excerpt = str(raw.get("excerpt") or "")[:500]
    review_id = _coerce_review_id(raw.get("review_id"))

    raw_value = raw.get("value")
    if signal in NUMERIC_SIGNALS:
        v = _coerce_numeric(raw_value)
        if v is None:
            errors.append(f"claim numeric con value no parseable: {signal}={raw_value!r}")
            return None
        if v < 0.0 or v > 1.0:
            errors.append(f"claim {signal} fuera de rango [0,1]: {v} (clamped)")
            v = max(0.0, min(1.0, v))
        value = v
    elif signal in BOOLEAN_SIGNALS:
        b = _coerce_bool(raw_value)
        if b is None:
            errors.append(f"claim boolean con value no parseable: {signal}={raw_value!r}")
            return None
        value = b
    elif signal == "noise_source":
        if not isinstance(raw_value, str):
            errors.append(f"noise_source value debe ser string: {raw_value!r}")
            return None
        s = raw_value.strip().lower()
        if s not in NOISE_SOURCE_VOCAB:
            errors.append(f"noise_source fuera de vocabulario: {s!r} → 'other'")
            s = "other"
        value = s
    elif signal == "parking_capacity":
        if not isinstance(raw_value, str):
            errors.append(f"parking_capacity value debe ser string: {raw_value!r}")
            return None
        s = raw_value.strip().lower()
        if s not in PARKING_CAPACITY_VOCAB:
            errors.append(f"parking_capacity fuera de vocabulario: {s!r} (descartado)")
            return None
        value = s
    else:
        # No debería pasar (cubrimos VALID_SIGNALS). Defensa.
        errors.append(f"signal sin handler: {signal}")
        return None

    return ValidatedClaim(
        signal=signal,
        value=value,
        confidence=conf_f,
        review_id=review_id,
        excerpt=excerpt,
        raw=raw,
    )


def _validate_str_list(value: Any, max_items: int = 12, max_len: int = 60) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, str):
            continue
        s = item.strip().lower()
        if not s:
            continue
        out.append(s[:max_len])
        if len(out) >= max_items:
            break
    return out


def _optional_str(value: Any, max_len: int = 1000) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s[:max_len] if s else None
    return None


def parse_enrichment_response(text: str) -> ValidatedEnrichment:
    """Parsea y valida la respuesta JSON de Gemini para enrichment v2.

    Lanza `ParseError` si el JSON no se puede parsear.
    Errores de claims individuales se acumulan en `result.errors` (no fatal).
    """
    if not text or not text.strip():
        raise ParseError("respuesta vacía")

    cleaned = _strip_markdown_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON malformado: {e}") from e

    if not isinstance(data, dict):
        raise ParseError(f"raíz no es objeto: {type(data).__name__}")

    errors: list[str] = []
    raw_claims = data.get("claims", [])
    if not isinstance(raw_claims, list):
        errors.append("'claims' no es lista (ignorado)")
        raw_claims = []

    claims: list[ValidatedClaim] = []
    for raw in raw_claims:
        if not isinstance(raw, dict):
            errors.append(f"claim no es objeto: {raw!r}")
            continue
        vc = _validate_claim(raw, errors)
        if vc:
            claims.append(vc)

    # v4: prefer "summary" (English). Fallback to legacy "summary_en" or
    # "summary_es" for transitional robustness if a model still emits old keys.
    summary = (
        _optional_str(data.get("summary"))
        or _optional_str(data.get("summary_en"))
        or _optional_str(data.get("summary_es"))
    )

    return ValidatedEnrichment(
        claims=claims,
        summary=summary,
        tags=_validate_str_list(data.get("tags")),
        best_for=_validate_str_list(data.get("best_for")),
        best_season=_optional_str(data.get("best_season"), max_len=100),
        avoid_season=_optional_str(data.get("avoid_season"), max_len=100),
        errors=errors,
    )
