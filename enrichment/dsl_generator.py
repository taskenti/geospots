"""Compact semantic DSL generation."""

from __future__ import annotations

DSL_KEYS = {
    "quietness": "quiet",
    "noise": "noise",
    "road_noise": "road_noise",
    "party_noise": "party_noise",
    "train_noise": "train_noise",
    "police_risk": "police",
    "theft_risk": "theft",
    "beauty": "beauty",
    "safety": "safe",
    "crowd_level": "crowd",
    "stealth": "stealth",
    "road_quality": "road",
    "wind_exposure": "wind",
    "cleanliness": "clean",
    "sea_view": "sea",
    "mountain_view": "mountain",
    "shade_morning": "shade_am",
    "shade_afternoon": "shade_pm",
    "overnight_safe": "overnight",
    "large_vehicle": "bigveh",
    "lake_nearby": "lake",
}


def _extract_value(value: object) -> object:
    if isinstance(value, dict):
        if "score" in value:
            return value["score"]
        if "value" in value:
            return value["value"]
    return value


def generate_spot_dsl(semantic_state: dict) -> str:
    parts: list[str] = []
    for key, abbr in DSL_KEYS.items():
        val = _extract_value(semantic_state.get(key))
        if val is None:
            continue
        if isinstance(val, bool):
            parts.append(f"{abbr}:{'T' if val else 'F'}")
        elif isinstance(val, (int, float)):
            sign = "+" if float(val) >= 0.5 else "-"
            parts.append(f"{abbr}:{sign}{float(val):.1f}")
        elif isinstance(val, str) and val:
            safe = val.replace(" ", "_")[:24]
            parts.append(f"{abbr}:{safe}")
    return " ".join(parts)


def generate_review_dsl(claims: list[dict]) -> str:
    state = {}
    for claim in claims:
        signal = claim.get("signal") or claim.get("signal_type")
        if not signal:
            continue
        state[signal] = claim.get("value") or claim.get("raw_value")
    return generate_spot_dsl(state)
