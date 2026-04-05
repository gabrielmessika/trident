from __future__ import annotations

from app.trident.pod_a.filters import passes_anchor_filters
from app.trident.pod_a.signals import AnchorTrendContext, AnchorTrendSignal

MIN_SETUP_STRUCTURE_SCORE = 0.40
MAX_PULLBACK_DISTANCE_BPS = 25.0
PULLBACK_ANCHOR_BPS = 12.0


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


class AnchorTrendService:
    """Minimal Pod A signal generator for trend-following setups."""

    def evaluate(self, context: AnchorTrendContext) -> AnchorTrendSignal | None:
        if not passes_anchor_filters(context):
            return None

        if self._is_long_setup(context):
            components = self._confidence_components(context)
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="long",
                setup="trend_pullback_long",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                confidence_components=components,
            )

        if self._is_short_setup(context):
            components = self._confidence_components(context)
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="short",
                setup="trend_pullback_short",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                confidence_components=components,
            )

        return None

    def evaluate_many(
        self,
        contexts: list[AnchorTrendContext],
    ) -> list[AnchorTrendSignal]:
        signals: list[AnchorTrendSignal] = []
        for context in contexts:
            signal = self.evaluate(context)
            if signal is not None:
                signals.append(signal)
        return sorted(signals, key=lambda item: item.confidence, reverse=True)

    def best_signal(
        self,
        contexts: list[AnchorTrendContext],
    ) -> AnchorTrendSignal | None:
        signals = self.evaluate_many(contexts)
        return signals[0] if signals else None

    def _is_long_setup(self, context: AnchorTrendContext) -> bool:
        return (
            context.structure_score >= MIN_SETUP_STRUCTURE_SCORE
            and context.price >= context.ema_fast >= context.ema_slow
            and context.vwap_distance_bps >= -MAX_PULLBACK_DISTANCE_BPS
        )

    def _is_short_setup(self, context: AnchorTrendContext) -> bool:
        return (
            context.structure_score <= -MIN_SETUP_STRUCTURE_SCORE
            and context.price <= context.ema_fast <= context.ema_slow
            and context.vwap_distance_bps <= MAX_PULLBACK_DISTANCE_BPS
        )

    def _confidence_components(self, context: AnchorTrendContext) -> dict[str, float]:
        ema_separation_bps = (
            abs(context.ema_fast - context.ema_slow) / context.price * 10_000
            if context.price > 0
            else 0.0
        )
        structure_quality = _clamp((abs(context.structure_score) - 0.30) / 0.35)
        trend_quality = _clamp((ema_separation_bps - 5.0) / 25.0)
        pullback_quality = _clamp(
            1.0 - abs(abs(context.vwap_distance_bps) - PULLBACK_ANCHOR_BPS) / 18.0,
        )
        spread_quality = _clamp(1.0 - context.spread_bps / 8.0)
        funding_quality = _clamp(1.0 - abs(context.funding_rate) / 0.0005)
        return {
            "structure_quality": round(structure_quality, 4),
            "trend_quality": round(trend_quality, 4),
            "pullback_quality": round(pullback_quality, 4),
            "spread_quality": round(spread_quality, 4),
            "funding_quality": round(funding_quality, 4),
        }

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["structure_quality"] * 0.40
            + components["trend_quality"] * 0.25
            + components["pullback_quality"] * 0.20
            + components["spread_quality"] * 0.10
            + components["funding_quality"] * 0.05
        )
