from __future__ import annotations

from collections import deque

from app.settings import PodCConfig
from app.trident.pod_c.signals import SqueezeContext, SqueezeSignal


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


class SqueezeBreakoutService:
    """Detects volatility squeeze then trades the breakout direction."""

    def __init__(self, config: PodCConfig) -> None:
        self.config = config
        self._vol_history: dict[str, deque[float]] = {}
        self._trade_count_history: dict[str, deque[int]] = {}

    def update_history(self, symbol: str, bucket_range_bps: float, bucket_trade_count: int) -> None:
        lookback = max(self.config.squeeze_lookback, 3)
        if symbol not in self._vol_history:
            self._vol_history[symbol] = deque(maxlen=lookback)
            self._trade_count_history[symbol] = deque(maxlen=lookback)
        self._vol_history[symbol].append(bucket_range_bps)
        self._trade_count_history[symbol].append(bucket_trade_count)

    def squeeze_ratio(self, symbol: str, current_range_bps: float) -> float:
        history = self._vol_history.get(symbol)
        if not history or len(history) < 3:
            return 1.0
        avg = sum(history) / len(history)
        if avg <= 0:
            return 1.0
        return current_range_bps / avg

    def volume_ratio(self, symbol: str, current_trade_count: int) -> float:
        history = self._trade_count_history.get(symbol)
        if not history or len(history) < 3:
            return 1.0
        avg = sum(history) / len(history)
        if avg <= 0:
            return 1.0
        return current_trade_count / avg

    def evaluate(self, context: SqueezeContext) -> SqueezeSignal | None:
        if not self._passes_filters(context):
            return None
        components = self._confidence_components(context)
        confidence = round(self._aggregate_confidence(components), 3)
        if confidence < self.config.min_confidence:
            return None
        side = self._determine_side(context)
        return SqueezeSignal(
            symbol=context.symbol,
            side=side,
            setup=f"squeeze_breakout_{side}",
            confidence=confidence,
            entry_price=context.price,
            market_cluster=context.market_cluster,
            confidence_components=components,
        )

    def evaluate_many(self, contexts: list[SqueezeContext]) -> list[SqueezeSignal]:
        signals: list[SqueezeSignal] = []
        for context in contexts:
            signal = self.evaluate(context)
            if signal is not None:
                signals.append(signal)
        return sorted(signals, key=lambda item: item.confidence, reverse=True)

    def _passes_filters(self, context: SqueezeContext) -> bool:
        if context.spread_bps > self.config.max_spread_bps:
            return False
        if context.squeeze_ratio < self.config.breakout_multiplier:
            return False
        flow_score = self._flow_alignment_unsigned(context)
        if flow_score < self.config.min_flow_alignment:
            return False
        if context.volume_ratio < self.config.min_volume_spike * 0.8:
            return False
        return True

    def _determine_side(self, context: SqueezeContext) -> str:
        net_flow = (context.trade_flow_bias + context.book_imbalance) / 2.0
        return "long" if net_flow > 0 else "short"

    def _confidence_components(self, context: SqueezeContext) -> dict[str, float]:
        breakout_strength = _clamp(
            (context.squeeze_ratio - 1.0) / max(self.config.breakout_multiplier - 1.0, 0.5)
        )
        flow_quality = self._flow_alignment_unsigned(context)
        volume_quality = _clamp(
            (context.volume_ratio - 1.0) / max(self.config.min_volume_spike - 1.0, 0.5)
        )
        spread_quality = _clamp(1.0 - context.spread_bps / max(self.config.max_spread_bps, 1.0))
        structure_quality = _clamp(abs(context.structure_score))
        return {
            "breakout_strength": round(breakout_strength, 4),
            "flow_quality": round(flow_quality, 4),
            "volume_quality": round(volume_quality, 4),
            "spread_quality": round(spread_quality, 4),
            "structure_quality": round(structure_quality, 4),
        }

    def _flow_alignment_unsigned(self, context: SqueezeContext) -> float:
        return _clamp(abs(context.trade_flow_bias + context.book_imbalance) / 2.0 * 2.0)

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["breakout_strength"] * 0.30
            + components["flow_quality"] * 0.30
            + components["volume_quality"] * 0.20
            + components["spread_quality"] * 0.10
            + components["structure_quality"] * 0.10
        )

    def reset(self) -> None:
        self._vol_history.clear()
        self._trade_count_history.clear()
