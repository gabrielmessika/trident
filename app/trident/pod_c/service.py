from __future__ import annotations

from app.settings import PodCConfig
from app.trident.pod_c.signals import EventRaiderContext, EventRaiderSignal


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


class EventRaiderService:
    """Event-driven follower service for Pod C."""

    def __init__(self, config: PodCConfig) -> None:
        self.config = config

    def evaluate(self, context: EventRaiderContext) -> EventRaiderSignal | None:
        if not self._passes_filters(context):
            return None

        components = self._confidence_components(context)
        return EventRaiderSignal(
            symbol=context.symbol,
            side=context.side,
            setup=f"lead_lag_{context.side}",
            confidence=round(self._aggregate_confidence(components), 3),
            entry_price=context.price,
            leader_symbol=context.leader_symbol,
            confidence_components=components,
        )

    def evaluate_many(self, contexts: list[EventRaiderContext]) -> list[EventRaiderSignal]:
        signals: list[EventRaiderSignal] = []
        for context in contexts:
            signal = self.evaluate(context)
            if signal is not None:
                signals.append(signal)
        return sorted(signals, key=lambda item: item.confidence, reverse=True)

    def _passes_filters(self, context: EventRaiderContext) -> bool:
        if not context.btc_aligned:
            return False
        if context.spread_bps > self.config.max_spread_bps:
            return False
        if context.lag_bps < self.config.min_lag_bps:
            return False
        if context.side == "long":
            if context.structure_score < -0.2:
                return False
        else:
            if context.structure_score > 0.2:
                return False
        return True

    def _confidence_components(self, context: EventRaiderContext) -> dict[str, float]:
        impulse_quality = _clamp(
            (abs(context.leader_impulse_bps) - self.config.impulse_threshold_bps)
            / max(self.config.impulse_threshold_bps, 1.0),
        )
        lag_quality = _clamp(context.lag_bps / max(self.config.impulse_threshold_bps, 1.0))
        spread_quality = _clamp(1.0 - context.spread_bps / max(self.config.max_spread_bps, 1.0))
        structure_quality = _clamp(0.5 + abs(context.structure_score) * 0.5)
        flow_alignment = _clamp(
            0.5 + ((context.trade_flow_bias + context.book_imbalance) / 2.0) * 0.5
        )
        if context.side == "short":
            flow_alignment = _clamp(
                0.5 + ((-context.trade_flow_bias - context.book_imbalance) / 2.0) * 0.5
            )
        return {
            "impulse_quality": round(impulse_quality, 4),
            "lag_quality": round(lag_quality, 4),
            "spread_quality": round(spread_quality, 4),
            "structure_quality": round(structure_quality, 4),
            "flow_alignment": round(flow_alignment, 4),
        }

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["impulse_quality"] * 0.35
            + components["lag_quality"] * 0.30
            + components["spread_quality"] * 0.10
            + components["structure_quality"] * 0.10
            + components["flow_alignment"] * 0.15
        )
