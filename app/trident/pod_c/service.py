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
            market_cluster=context.market_cluster,
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
        if not context.cluster_aligned:
            return False
        max_spread_bps = self._max_spread_bps(context.market_cluster)
        if context.spread_bps > max_spread_bps:
            return False
        min_required_lag = max(
            self._min_lag_bps(context.market_cluster),
            self._impulse_threshold_bps(context.market_cluster) * 0.6,
        )
        if context.lag_bps < min_required_lag:
            return False
        if abs(context.leader_impulse_bps) < self._impulse_threshold_bps(context.market_cluster) * 1.1:
            return False
        if abs(context.follower_move_bps) > abs(context.leader_impulse_bps) * 0.75:
            return False
        flow_alignment = self._flow_alignment_score(context)
        if flow_alignment < 0.45:
            return False
        if context.side == "long":
            if context.structure_score < -0.1:
                return False
        else:
            if context.structure_score > 0.1:
                return False
        return True

    def _confidence_components(self, context: EventRaiderContext) -> dict[str, float]:
        impulse_threshold = self._impulse_threshold_bps(context.market_cluster)
        max_spread_bps = self._max_spread_bps(context.market_cluster)
        impulse_quality = _clamp(
            (abs(context.leader_impulse_bps) - impulse_threshold)
            / max(impulse_threshold, 1.0),
        )
        lag_quality = _clamp(context.lag_bps / max(impulse_threshold, 1.0))
        spread_quality = _clamp(1.0 - context.spread_bps / max(max_spread_bps, 1.0))
        structure_quality = _clamp(0.5 + abs(context.structure_score) * 0.5)
        flow_alignment = self._flow_alignment_score(context)
        return {
            "impulse_quality": round(impulse_quality, 4),
            "lag_quality": round(lag_quality, 4),
            "spread_quality": round(spread_quality, 4),
            "structure_quality": round(structure_quality, 4),
            "flow_alignment": round(flow_alignment, 4),
        }

    def _flow_alignment_score(self, context: EventRaiderContext) -> float:
        signed_flow = (context.trade_flow_bias + context.book_imbalance) / 2.0
        if context.side == "short":
            signed_flow *= -1.0
        return _clamp(0.5 + signed_flow * 0.5)

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["impulse_quality"] * 0.35
            + components["lag_quality"] * 0.30
            + components["spread_quality"] * 0.10
            + components["structure_quality"] * 0.10
            + components["flow_alignment"] * 0.15
        )

    def _impulse_threshold_bps(self, market_cluster: str) -> float:
        if market_cluster == "index":
            return max(self.config.impulse_threshold_bps * 0.8, 8.0)
        if market_cluster == "gold":
            return max(self.config.impulse_threshold_bps * 0.9, 8.0)
        return self.config.impulse_threshold_bps

    def _min_lag_bps(self, market_cluster: str) -> float:
        if market_cluster == "index":
            return max(self.config.min_lag_bps * 0.8, 3.0)
        if market_cluster == "gold":
            return max(self.config.min_lag_bps * 0.9, 3.5)
        return self.config.min_lag_bps

    def _max_spread_bps(self, market_cluster: str) -> float:
        if market_cluster == "index":
            return max(self.config.max_spread_bps * 0.8, 3.0)
        if market_cluster == "gold":
            return max(self.config.max_spread_bps * 0.9, 4.0)
        return self.config.max_spread_bps
