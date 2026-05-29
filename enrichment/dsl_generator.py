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
    "wind_exposure": "shelter",
    "cleanliness": "clean",
    "sea_view": "sea",
    "mountain_view": "mountain",
    "shade_morning": "shade_am",
    "shade_afternoon": "shade_pm",
    "overnight_safe": "overnight",
    "large_vehicle": "bigveh",
    "lake_nearby": "lake",
    # Señales de texto v2 (BUG-16) — antes nunca llegaban al DSL.
    "noise_source": "noisesrc",
    "parking_capacity": "parking",
}

# BUG-POLARITY (2026-05-29): el DSL usa polaridad "signo = bueno(+)/malo(-),
# magnitud = intensidad" centrada en 0.5. Para señales donde valor alto = PEOR
# (ruido, riesgo, masificación) la contribución a la bondad del spot se invierte
# (contrib = 0.5 - valor) para que un riesgo alto se lea como negativo. Antes
# SOLO se invertía wind_exposure → cualquier otra señal de riesgo/molestia se
# renderizaba con polaridad positiva (`theft:+1.0` leído como "muy seguro"
# cuando significaba "robo máximo"), envenenando embeddings y /search/semantic.
# wind_exposure se mapea al concepto positivo `shelter` en DSL_KEYS.
HIGHER_IS_WORSE = {
    "noise", "road_noise", "party_noise", "train_noise",
    "police_risk", "theft_risk", "crowd_level", "wind_exposure",
}
# Alias retrocompatible (algún import externo podría referenciarlo).
DSL_INVERTED_SIGNALS = HIGHER_IS_WORSE


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
            num = float(val)
            # Contribución a la "bondad" del spot, centrada en 0.5.
            #   señal buena (quiet/beauty/safe...): contrib = valor - 0.5
            #   señal mala  (noise/theft/police...): contrib = 0.5 - valor
            # signo = bueno(+)/malo(-); magnitud = intensidad escalada a 0..1.
            contrib = (0.5 - num) if key in HIGHER_IS_WORSE else (num - 0.5)
            sign = "+" if contrib >= 0 else "-"
            mag = min(1.0, abs(contrib) * 2.0)
            parts.append(f"{abbr}:{sign}{mag:.1f}")
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
