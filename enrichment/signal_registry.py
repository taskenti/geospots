"""Signal type registry backed by Postgres with a static fallback."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SignalType:
    id: str
    value_type: str
    half_life_days: int
    aggregation_strategy: str
    importance_weight: float = 1.0
    parent_id: str | None = None


STATIC_SIGNALS = {
    "noise": SignalType("noise", "numeric", 90, "weighted_mean", 1.5),
    "road_noise": SignalType("road_noise", "numeric", 90, "weighted_mean", 1.0, "noise"),
    "party_noise": SignalType("party_noise", "numeric", 30, "weighted_mean", 1.0, "noise"),
    "train_noise": SignalType("train_noise", "numeric", 730, "weighted_mean", 0.8, "noise"),
    "quietness": SignalType("quietness", "numeric", 365, "weighted_mean", 1.5),
    "beauty": SignalType("beauty", "numeric", 36500, "weighted_mean", 1.2),
    "cleanliness": SignalType("cleanliness", "numeric", 60, "weighted_mean", 0.8),
    "safety": SignalType("safety", "numeric", 365, "weighted_mean", 1.5),
    "police_risk": SignalType("police_risk", "numeric", 60, "weighted_mean", 2.0, "safety"),
    "theft_risk": SignalType("theft_risk", "numeric", 90, "weighted_mean", 2.0, "safety"),
    "sea_view": SignalType("sea_view", "boolean", 36500, "consensus_boolean", 0.5, "beauty"),
    "mountain_view": SignalType("mountain_view", "boolean", 36500, "consensus_boolean", 0.5, "beauty"),
    "lake_nearby": SignalType("lake_nearby", "boolean", 36500, "consensus_boolean", 0.3, "beauty"),
    "shade_morning": SignalType("shade_morning", "boolean", 1825, "consensus_boolean", 0.4),
    "shade_afternoon": SignalType("shade_afternoon", "boolean", 1825, "consensus_boolean", 0.4),
    "large_vehicle": SignalType("large_vehicle", "numeric", 36500, "weighted_mean", 0.6),
    "road_quality": SignalType("road_quality", "numeric", 1825, "weighted_mean", 0.5),
    "overnight_safe": SignalType("overnight_safe", "boolean", 120, "consensus_boolean", 2.0),
    "crowd_level": SignalType("crowd_level", "numeric", 30, "weighted_mean", 1.0),
    "wind_exposure": SignalType("wind_exposure", "numeric", 730, "weighted_mean", 0.6),
    "stealth": SignalType("stealth", "numeric", 365, "weighted_mean", 0.8),
}


class SignalRegistry:
    def __init__(self) -> None:
        self._signals = dict(STATIC_SIGNALS)

    async def load(self, conn) -> "SignalRegistry":
        rows = await conn.fetch(
            """
            SELECT id, parent_id, value_type, half_life_days, aggregation_strategy, importance_weight
            FROM signal_types
            """
        )
        if rows:
            self._signals = {
                r["id"]: SignalType(
                    id=r["id"],
                    parent_id=r["parent_id"],
                    value_type=r["value_type"],
                    half_life_days=r["half_life_days"],
                    aggregation_strategy=r["aggregation_strategy"],
                    importance_weight=float(r["importance_weight"] or 1.0),
                )
                for r in rows
            }
        return self

    def get(self, signal_id: str) -> SignalType | None:
        return self._signals.get(signal_id)

    def all(self) -> dict[str, SignalType]:
        return dict(self._signals)


registry = SignalRegistry()
