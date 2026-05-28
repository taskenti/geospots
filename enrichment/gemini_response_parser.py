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
class ValidatedAlert:
    """T1.4 — alerta tipada con lifecycle. Persiste en `spot_alerts`."""
    alert_type: str
    severity: float           # 0..1
    valid_from: str           # "YYYY-MM" o "YYYY-MM-DD"; ingest_v2 lo parsea a DATE
    confidence: float         # 0..1
    source_review_ids: list[int]
    summary: str | None
    raw: dict


# Vocabulario de alert_type aceptado por el parser.
# Items fuera de este set se registran en errors y se descartan.
ALERT_TYPE_VOCAB: set[str] = {
    "construction", "closed_season", "access_restricted",
    "temporary_ban", "natural_hazard", "event_overflow",
    "permanently_closed",
}

# T1.4b — vocabularios de clasificación funcional.
SPOT_FUNCTION_VOCAB: set[str] = {
    "overnight_primary", "overnight_tolerated", "service_only",
    "shop_workshop", "transit", "daytime_only",
}
AUTHORIZATION_STATUS_VOCAB: set[str] = {
    "official", "tolerated", "sign_authorized", "illegal", "unknown",
}


@dataclass
class ValidatedEnrichment:
    claims: list[ValidatedClaim]
    summary: str | None       # v4: single English narrative (was summary_es/summary_en)
    tags: list[str]
    best_for: list[str]
    best_season: str | None
    avoid_season: str | None
    errors: list[str] = field(default_factory=list)  # non-fatal warnings

    # v6 (T1.4 + T1.4b) — campos opcionales. Default vacíos/None para no romper
    # cualquier consumidor pre-v6 que solo lee `claims` + narrativa.
    alerts: list[ValidatedAlert] = field(default_factory=list)
    spot_function: str | None = None
    is_overnight_viable: bool | None = None
    authorization_status: str | None = None
    elevation_m: int | None = None
    terrain_type: str | None = None
    slope_degrees: int | None = None

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
    """Parsea y valida la respuesta JSON del LLM para enrichment v2/v5.

    v5 (T1.2): schema cambia de `claims[]` → `review_claims[]` + `contradicted_static_facts[]`.
    Ambos arrays se fusionan en `ValidatedEnrichment.claims` para minimizar cambios
    aguas abajo (ingest_v2 ya tolera review_id NULL → NOW()).

    Reglas:
      - `review_claims`: cada item DEBE tener `review_id` entero. Items con
        review_id NULL (incluido "services"/"description" coerced to None) se
        RECHAZAN — es el bug que T1.2 cierra.
      - `contradicted_static_facts`: cada item DEBE tener `review_id` entero
        (un contradiction sin review citada no es contradiction).
      - Legacy `claims[]` (v4): se sigue aceptando como fallback transitorio —
        si el LLM emite el schema viejo, no rompemos. Items con review_id NULL
        se conservan (extractor_name='services'/'description' implicit).

    Lanza `ParseError` si el JSON no se puede parsear.
    Errores de items individuales se acumulan en `result.errors` (no fatal).
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
    claims: list[ValidatedClaim] = []

    # ── v5 schema: review_claims + contradicted_static_facts ────────────
    has_v5_schema = "review_claims" in data or "contradicted_static_facts" in data

    raw_review_claims = data.get("review_claims", []) if has_v5_schema else []
    if not isinstance(raw_review_claims, list):
        errors.append("'review_claims' no es lista (ignorado)")
        raw_review_claims = []

    for raw in raw_review_claims:
        if not isinstance(raw, dict):
            errors.append(f"review_claim no es objeto: {raw!r}")
            continue
        vc = _validate_claim(raw, errors)
        if vc is None:
            continue
        # v5 hard rule: review_claims sin review_id concreto se rechazan.
        if vc.review_id is None:
            errors.append(
                f"review_claim rechazado: signal={vc.signal} sin review_id "
                f"(probable re-emisión de STATIC_CONTEXT)"
            )
            continue
        claims.append(vc)

    raw_contradictions = data.get("contradicted_static_facts", []) if has_v5_schema else []
    if not isinstance(raw_contradictions, list):
        errors.append("'contradicted_static_facts' no es lista (ignorado)")
        raw_contradictions = []

    for raw in raw_contradictions:
        if not isinstance(raw, dict):
            errors.append(f"contradicted_static_fact no es objeto: {raw!r}")
            continue
        vc = _validate_claim(raw, errors)
        if vc is None:
            continue
        # contradiction sin review_id no es contradiction — la rechazamos.
        if vc.review_id is None:
            errors.append(
                f"contradicted_static_fact rechazado: signal={vc.signal} sin review_id"
            )
            continue
        claims.append(vc)

    # ── Fallback legacy v4: campo `claims[]` ────────────────────────────
    if not has_v5_schema:
        raw_claims = data.get("claims", [])
        if not isinstance(raw_claims, list):
            errors.append("'claims' no es lista (ignorado)")
            raw_claims = []
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

    # ── v6 (T1.4 + T1.4b) parsing — alerts + functional fields + geo ────
    alerts = _parse_alerts(data.get("alerts"), errors)
    spot_function = _validated_enum(
        data.get("spot_function"), SPOT_FUNCTION_VOCAB,
        "spot_function", errors,
    )
    is_overnight_viable = _coerce_bool(data.get("is_overnight_viable"))
    authorization_status = _validated_enum(
        data.get("authorization_status"), AUTHORIZATION_STATUS_VOCAB,
        "authorization_status", errors,
    )
    elevation_m = _coerce_int_in_range(
        data.get("elevation_m"), lo=-500, hi=9000, field="elevation_m", errors=errors,
    )
    terrain_type = _optional_str(data.get("terrain_type"), max_len=40)
    slope_degrees = _coerce_int_in_range(
        data.get("slope_degrees"), lo=0, hi=45, field="slope_degrees", errors=errors,
    )

    return ValidatedEnrichment(
        claims=claims,
        summary=summary,
        tags=_validate_str_list(data.get("tags")),
        best_for=_validate_str_list(data.get("best_for")),
        best_season=_optional_str(data.get("best_season"), max_len=100),
        avoid_season=_optional_str(data.get("avoid_season"), max_len=100),
        errors=errors,
        alerts=alerts,
        spot_function=spot_function,
        is_overnight_viable=is_overnight_viable,
        authorization_status=authorization_status,
        elevation_m=elevation_m,
        terrain_type=terrain_type,
        slope_degrees=slope_degrees,
    )


# ──────────────────────────────────────────────────────────────────────
# v6 helpers — alerts + enum + range validators
# ──────────────────────────────────────────────────────────────────────


def _parse_alerts(raw: Any, errors: list[str]) -> list[ValidatedAlert]:
    """Parsea `alerts[]` del schema v6. Rechaza alerts sin source_review_ids."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append("'alerts' no es lista (ignorado)")
        return []
    out: list[ValidatedAlert] = []
    for item in raw:
        if not isinstance(item, dict):
            errors.append(f"alert no es objeto: {item!r}")
            continue

        alert_type = item.get("alert_type")
        if not isinstance(alert_type, str) or alert_type.strip() not in ALERT_TYPE_VOCAB:
            errors.append(f"alert_type fuera de vocabulario: {alert_type!r}")
            continue
        alert_type = alert_type.strip()

        sev = _coerce_numeric(item.get("severity"))
        if sev is None or sev < 0 or sev > 1:
            errors.append(f"alert.severity inválida: {item.get('severity')!r}")
            continue

        conf = _coerce_numeric(item.get("confidence"))
        if conf is None or conf < 0 or conf > 1:
            errors.append(f"alert.confidence inválida: {item.get('confidence')!r}")
            continue

        valid_from = _optional_str(item.get("valid_from_inferred"), max_len=10)
        if not valid_from:
            errors.append(f"alert.valid_from_inferred ausente para {alert_type}")
            continue

        raw_ids = item.get("source_review_ids") or []
        if not isinstance(raw_ids, list):
            errors.append(f"alert.source_review_ids no es lista: {raw_ids!r}")
            continue
        review_ids: list[int] = []
        for rid in raw_ids:
            rid_int = _coerce_review_id(rid)
            if rid_int is not None:
                review_ids.append(rid_int)
        if not review_ids:
            errors.append(
                f"alert {alert_type} rechazada: source_review_ids vacío "
                "(toda alerta debe citar al menos una review)"
            )
            continue

        summary = _optional_str(item.get("summary"), max_len=300)

        out.append(ValidatedAlert(
            alert_type=alert_type,
            severity=float(sev),
            valid_from=valid_from,
            confidence=float(conf),
            source_review_ids=review_ids,
            summary=summary,
            raw=item,
        ))
    return out


def _validated_enum(value: Any, vocab: set[str], field: str, errors: list[str]) -> str | None:
    """Devuelve `value` si está en `vocab` (lowercase strip), None si ausente,
    log + None si fuera de vocabulario."""
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{field} no es string: {value!r}")
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v not in vocab:
        errors.append(f"{field} fuera de vocabulario: {value!r}")
        return None
    return v


def _coerce_int_in_range(value: Any, *, lo: int, hi: int, field: str, errors: list[str]) -> int | None:
    """Coerciona a int y valida rango. Errores no fatales, devuelve None si inválido."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = int(value)
    elif isinstance(value, str):
        try:
            v = int(float(value.strip()))
        except ValueError:
            errors.append(f"{field} no parseable: {value!r}")
            return None
    else:
        errors.append(f"{field} tipo inválido: {type(value).__name__}")
        return None
    if v < lo or v > hi:
        errors.append(f"{field} fuera de rango [{lo},{hi}]: {v}")
        return None
    return v
