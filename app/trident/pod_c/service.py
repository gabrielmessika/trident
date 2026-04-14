from __future__ import annotations

from collections import deque

from app.settings import PodCConfig
from app.trident.market_clusters import normalize_cluster_names
from app.trident.pod_c.signals import TradfiTrendContext, TradfiTrendSignal


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


class TradfiTrendService:
    """Detects directional Tradfi setups on HL symbols using shared TRIDENT snapshots."""

    def __init__(self, config: PodCConfig) -> None:
        self.config = config
        self._notional_history: dict[str, deque[float]] = {}
        self._trade_count_history: dict[str, deque[int]] = {}
        self._allowed_clusters = normalize_cluster_names(config.allowed_market_clusters)

    def update_history(
        self,
        symbol: str,
        bucket_notional_usd: float,
        bucket_trade_count: int,
    ) -> None:
        lookback = max(self.config.activity_lookback, 3)
        if symbol not in self._notional_history:
            self._notional_history[symbol] = deque(maxlen=lookback)
            self._trade_count_history[symbol] = deque(maxlen=lookback)
        self._notional_history[symbol].append(bucket_notional_usd)
        self._trade_count_history[symbol].append(bucket_trade_count)

    def activity_ratio(self, symbol: str, current_notional_usd: float) -> float:
        history = self._notional_history.get(symbol)
        if not history or len(history) < 3:
            return 1.0
        avg = sum(history) / len(history)
        if avg <= 0:
            return 1.0
        return current_notional_usd / avg

    def trade_count_ratio(self, symbol: str, current_trade_count: int) -> float:
        history = self._trade_count_history.get(symbol)
        if not history or len(history) < 3:
            return 1.0
        avg = sum(history) / len(history)
        if avg <= 0:
            return 1.0
        return current_trade_count / avg

    def is_eligible_symbol(self, symbol: str, market_cluster: str) -> bool:
        normalized_cluster = str(market_cluster).strip().lower()
        return bool(normalized_cluster) and normalized_cluster in self._allowed_clusters

    def evaluate(self, context: TradfiTrendContext) -> TradfiTrendSignal | None:
        if not self._passes_filters(context):
            return None

        continuation = self._build_continuation_signal(context)
        reclaim = self._build_reclaim_signal(context)
        candidates = [signal for signal in (continuation, reclaim) if signal is not None]
        if not candidates:
            return None
        best = max(candidates, key=lambda item: item.confidence)
        if not self._passes_cluster_strategy(context, best):
            return None
        if best.confidence < self.config.min_confidence:
            return None
        return best

    def evaluate_many(self, contexts: list[TradfiTrendContext]) -> list[TradfiTrendSignal]:
        signals: list[TradfiTrendSignal] = []
        for context in contexts:
            signal = self.evaluate(context)
            if signal is not None:
                signals.append(signal)
        return sorted(signals, key=lambda item: item.confidence, reverse=True)

    def _passes_filters(self, context: TradfiTrendContext) -> bool:
        if not self.is_eligible_symbol(context.symbol, context.market_cluster):
            return False
        if not context.cluster_aligned:
            return False
        if context.spread_bps > self.config.max_spread_bps:
            return False
        if abs(context.funding_rate) > self.config.max_abs_funding_rate:
            return False
        if context.bucket_notional_usd < self.config.min_bucket_notional_usd:
            return False
        if context.bucket_trade_count < self.config.min_bucket_trade_count:
            return False
        if abs(context.vwap_distance_bps) > self.config.max_vwap_distance_bps:
            return False
        if context.activity_ratio < self.config.min_activity_ratio:
            return False
        return True

    def _build_continuation_signal(
        self,
        context: TradfiTrendContext,
    ) -> TradfiTrendSignal | None:
        direction = self._aligned_direction(context)
        if direction is None:
            return None
        if abs(context.trend_bps) < self.config.min_trend_bps:
            return None
        if abs(context.structure_score) < self.config.min_structure_score:
            return None
        if abs(context.vwap_distance_bps) > self.config.min_reclaim_distance_bps:
            return None
        components = self._confidence_components(context, reclaim=False)
        components["setup_bonus"] = 0.08
        confidence = round(self._aggregate_confidence(components), 3)
        return TradfiTrendSignal(
            symbol=context.symbol,
            side=direction,
            setup=f"tradfi_continuation_{direction}",
            confidence=confidence,
            entry_price=context.price,
            market_cluster=context.market_cluster,
            cluster_leader=context.cluster_leader,
            confidence_components=components,
        )

    def _build_reclaim_signal(
        self,
        context: TradfiTrendContext,
    ) -> TradfiTrendSignal | None:
        direction = self._aligned_direction(context)
        if direction is None:
            return None
        if abs(context.structure_score) < self.config.min_structure_score * 0.8:
            return None
        reclaim_distance = abs(context.vwap_distance_bps)
        if reclaim_distance < self.config.min_reclaim_distance_bps:
            return None
        if direction == "long" and context.vwap_distance_bps >= 0:
            return None
        if direction == "short" and context.vwap_distance_bps <= 0:
            return None
        components = self._confidence_components(context, reclaim=True)
        components["setup_bonus"] = 0.06
        confidence = round(self._aggregate_confidence(components), 3)
        return TradfiTrendSignal(
            symbol=context.symbol,
            side=direction,
            setup=f"tradfi_reclaim_{direction}",
            confidence=confidence,
            entry_price=context.price,
            market_cluster=context.market_cluster,
            cluster_leader=context.cluster_leader,
            confidence_components=components,
        )

    def _aligned_direction(self, context: TradfiTrendContext) -> str | None:
        trend_direction = 1 if context.ema_fast > context.ema_slow else -1
        structure_direction = 1 if context.structure_score >= 0 else -1
        flow_direction = 1 if (context.trade_flow_bias + context.book_imbalance) >= 0 else -1
        score = trend_direction + structure_direction + flow_direction
        if score >= 1:
            return "long"
        if score <= -1:
            return "short"
        return None

    def _passes_cluster_strategy(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        if not self.config.cluster_aware_v2_enabled:
            return True
        cluster = str(context.market_cluster).strip().lower()
        if cluster == "oil":
            return self._is_oil_long_pullback(context, signal)
        if cluster == "silver":
            return self._is_silver_breakout_long(context, signal)
        if cluster == "index":
            return self._is_index_breakout_long(context, signal)
        return False

    def _is_oil_long_pullback(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        return (
            signal.setup == "tradfi_continuation_long"
            and signal.side == "long"
            and context.trend_bps >= 8.0
            and context.structure_score >= 0.18
            and context.trade_flow_bias >= 0.02
            and -4.0 <= context.vwap_distance_bps <= -0.5
            and context.bucket_range_bps >= 18.0
            and context.spread_bps <= 3.0
        )

    def _is_silver_breakout_long(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        return (
            signal.setup == "tradfi_continuation_long"
            and signal.side == "long"
            and context.trend_bps >= 10.0
            and context.structure_score >= 0.20
            and context.trade_flow_bias >= 0.03
            and 1.0 <= context.vwap_distance_bps <= 6.0
            and context.bucket_range_bps >= 18.0
            and context.spread_bps <= 2.0
        )

    def _is_index_breakout_long(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        return (
            signal.setup == "tradfi_continuation_long"
            and signal.side == "long"
            and context.trend_bps >= 8.0
            and context.structure_score >= 0.18
            and context.trade_flow_bias >= 0.02
            and 1.0 <= context.vwap_distance_bps <= 6.0
            and context.bucket_range_bps >= 16.0
            and context.spread_bps <= 2.5
        )

    def _confidence_components(
        self,
        context: TradfiTrendContext,
        *,
        reclaim: bool,
    ) -> dict[str, float]:
        trend_quality = _clamp(abs(context.trend_bps) / max(self.config.min_trend_bps * 3.0, 12.0))
        structure_quality = _clamp(
            abs(context.structure_score) / max(self.config.min_structure_score * 2.5, 0.4)
        )
        flow_quality = _clamp(abs(context.trade_flow_bias + context.book_imbalance))
        spread_quality = _clamp(
            1.0 - context.spread_bps / max(self.config.max_spread_bps, 1.0)
        )
        activity_quality = _clamp(
            max(context.activity_ratio, context.trade_count_ratio)
            / max(self.config.min_activity_ratio * 2.0, 1.0)
        )
        reclaim_quality = _clamp(
            abs(context.vwap_distance_bps)
            / max(self.config.min_reclaim_distance_bps * 2.5, 10.0)
        )
        components = {
            "trend_quality": round(trend_quality, 4),
            "structure_quality": round(structure_quality, 4),
            "flow_quality": round(flow_quality, 4),
            "spread_quality": round(spread_quality, 4),
            "activity_quality": round(activity_quality, 4),
            "reclaim_quality": round(reclaim_quality if reclaim else 0.35, 4),
        }
        return components

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["trend_quality"] * 0.24
            + components["structure_quality"] * 0.22
            + components["flow_quality"] * 0.20
            + components["activity_quality"] * 0.16
            + components["spread_quality"] * 0.10
            + components["reclaim_quality"] * 0.08
            + components.get("setup_bonus", 0.0)
        )

    def reset(self) -> None:
        self._notional_history.clear()
        self._trade_count_history.clear()
