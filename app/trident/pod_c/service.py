from __future__ import annotations

from collections import deque

from app.settings import PodCConfig
from app.trident.market_clusters import normalize_cluster_names
from app.trident.pod_c.signals import TradfiTrendContext, TradfiTrendSignal


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _direction_label(value: int) -> str:
    return "long" if value >= 0 else "short"


def _trend_bucket(value: float) -> str:
    absolute = abs(value)
    if absolute >= 18.0:
        return "strong"
    if absolute >= 10.0:
        return "medium"
    return "soft"


def _structure_bucket(value: float) -> str:
    absolute = abs(value)
    if absolute >= 0.30:
        return "strong"
    if absolute >= 0.18:
        return "medium"
    return "soft"


def _activity_bucket(value: float) -> str:
    if value >= 1.8:
        return "high"
    if value >= 1.2:
        return "normal"
    return "soft"


def _flow_bucket(value: float) -> str:
    absolute = abs(value)
    if absolute >= 0.18:
        return "strong"
    if absolute >= 0.08:
        return "medium"
    return "soft"


def _vwap_bucket(value: float) -> str:
    if value <= -4.0:
        return "deep_pullback"
    if value < 0.0:
        return "pullback"
    if value <= 2.0:
        return "neutral"
    return "extension"


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

    def review_context(self, context: TradfiTrendContext) -> dict[str, object]:
        direction = self._aligned_direction(context)
        filter_reasons = self._filter_reasons(context)
        setup_details = self._review_setup_details(context, preferred_side=direction)
        if filter_reasons:
            return {
                "symbol": context.symbol,
                "status": "filtered",
                "preferred_side": direction or "neutral",
                "reason_summary": "; ".join(filter_reasons),
                "setup_details": setup_details,
                "confidence_components": {},
            }

        candidates = [
            signal
            for signal in (
                self._build_continuation_signal(context),
                self._build_reclaim_signal(context),
            )
            if signal is not None
        ]
        if not candidates:
            return {
                "symbol": context.symbol,
                "status": "filtered",
                "preferred_side": direction or "neutral",
                "reason_summary": "no_continuation_or_reclaim_setup",
                "setup_details": setup_details,
                "confidence_components": {},
            }

        best = max(candidates, key=lambda item: item.confidence)
        setup_details.update(
            {
                "candidate_setup": best.setup,
                "candidate_side": best.side,
                "candidate_confidence": best.confidence,
                "cluster_strategy": self._cluster_strategy_name(
                    context,
                    setup=best.setup,
                    side=best.side,
                ),
            }
        )
        if not self._passes_cluster_strategy(context, best):
            return {
                "symbol": context.symbol,
                "status": "filtered",
                "preferred_side": best.side,
                "setup": best.setup,
                "confidence": best.confidence,
                "reason_summary": "cluster_strategy_not_matched",
                "setup_details": setup_details,
                "confidence_components": dict(best.confidence_components),
            }
        if best.confidence < self.config.min_confidence:
            return {
                "symbol": context.symbol,
                "status": "filtered",
                "preferred_side": best.side,
                "setup": best.setup,
                "confidence": best.confidence,
                "reason_summary": (
                    f"confidence_below_min {best.confidence:.3f} < "
                    f"{self.config.min_confidence:.3f}"
                ),
                "setup_details": setup_details,
                "confidence_components": dict(best.confidence_components),
            }
        return {
            "symbol": context.symbol,
            "status": "signaled",
            "side": best.side,
            "setup": best.setup,
            "confidence": best.confidence,
            "reason_summary": "signal_ready",
            "setup_details": dict(best.setup_details),
            "confidence_components": dict(best.confidence_components),
        }

    def _passes_filters(self, context: TradfiTrendContext) -> bool:
        return not self._filter_reasons(context)

    def _filter_reasons(self, context: TradfiTrendContext) -> list[str]:
        reasons: list[str] = []
        if not self.is_eligible_symbol(context.symbol, context.market_cluster):
            reasons.append(f"cluster_not_allowed:{context.market_cluster}")
        if not context.cluster_aligned:
            reasons.append("cluster_not_aligned")
        if context.spread_bps > self.config.max_spread_bps:
            reasons.append(
                f"spread_too_wide {context.spread_bps:.2f}>{self.config.max_spread_bps:.2f}"
            )
        if abs(context.funding_rate) > self.config.max_abs_funding_rate:
            reasons.append(
                f"funding_too_large {abs(context.funding_rate):.6f}>"
                f"{self.config.max_abs_funding_rate:.6f}"
            )
        if context.bucket_notional_usd < self.config.min_bucket_notional_usd:
            reasons.append(
                f"bucket_notional_below_min {context.bucket_notional_usd:.2f}<"
                f"{self.config.min_bucket_notional_usd:.2f}"
            )
        if context.bucket_trade_count < self.config.min_bucket_trade_count:
            reasons.append(
                f"bucket_trade_count_below_min {context.bucket_trade_count}<"
                f"{self.config.min_bucket_trade_count}"
            )
        if abs(context.vwap_distance_bps) > self.config.max_vwap_distance_bps:
            reasons.append(
                f"vwap_distance_too_far {abs(context.vwap_distance_bps):.2f}>"
                f"{self.config.max_vwap_distance_bps:.2f}"
            )
        if context.activity_ratio < self.config.min_activity_ratio:
            reasons.append(
                f"activity_below_min {context.activity_ratio:.2f}<"
                f"{self.config.min_activity_ratio:.2f}"
            )
        return reasons

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
            setup_details=self._setup_details(
                context,
                side=direction,
                setup=f"tradfi_continuation_{direction}",
                reclaim=False,
            ),
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
            setup_details=self._setup_details(
                context,
                side=direction,
                setup=f"tradfi_reclaim_{direction}",
                reclaim=True,
            ),
            confidence_components=components,
        )

    def _aligned_direction(self, context: TradfiTrendContext) -> str | None:
        trend_direction, structure_direction, flow_direction = self._direction_components(context)
        score = trend_direction + structure_direction + flow_direction
        if score >= 1:
            return "long"
        if score <= -1:
            return "short"
        return None

    def _direction_components(self, context: TradfiTrendContext) -> tuple[int, int, int]:
        trend_direction = 1 if context.ema_fast >= context.ema_slow else -1
        structure_direction = 1 if context.structure_score >= 0 else -1
        flow_direction = 1 if (context.trade_flow_bias + context.book_imbalance) >= 0 else -1
        return trend_direction, structure_direction, flow_direction

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
        if cluster == "gold":
            return self._is_gold_breakout_long(context, signal)
        if cluster == "index":
            return self._is_index_breakout_long(context, signal)
        return False

    def _is_oil_long_pullback(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        flow_support = context.trade_flow_bias + context.book_imbalance
        return (
            signal.setup == "tradfi_continuation_long"
            and signal.side == "long"
            and context.trend_bps >= 9.0
            and context.structure_score >= 0.24
            and context.trade_flow_bias >= 0.25
            and -2.6 <= context.vwap_distance_bps <= -1.0
            and context.activity_ratio >= 1.7
            and 0.75 <= flow_support <= 1.15
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

    def _is_gold_breakout_long(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        return (
            signal.setup == "tradfi_continuation_long"
            and signal.side == "long"
            and context.cluster_regime == "TrendExpansion"
            and context.global_regime in {"TrendExpansion", "PanicSqueeze"}
            and context.trend_bps >= 8.0
            and context.structure_score >= 0.22
            and context.trade_flow_bias >= 0.02
            and 0.5 <= context.vwap_distance_bps <= 3.5
            and context.activity_ratio >= 1.1
            and context.bucket_range_bps >= 14.0
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

    def _cluster_strategy_name(
        self,
        context: TradfiTrendContext,
        *,
        setup: str,
        side: str,
    ) -> str:
        cluster = str(context.market_cluster).strip().lower()
        if setup == "tradfi_continuation_long" and side == "long":
            if cluster == "oil":
                return "oil_pullback_long"
            if cluster == "silver":
                return "silver_breakout_long"
            if cluster == "gold":
                return "gold_breakout_long"
            if cluster == "index":
                return "index_breakout_long"
        if setup == "tradfi_reclaim_long" and side == "long":
            return f"{cluster}_reclaim_long" if cluster else "reclaim_long"
        if setup == "tradfi_reclaim_short" and side == "short":
            return f"{cluster}_reclaim_short" if cluster else "reclaim_short"
        return f"{cluster or 'tradfi'}_{setup}"

    def _setup_details(
        self,
        context: TradfiTrendContext,
        *,
        side: str,
        setup: str,
        reclaim: bool,
    ) -> dict[str, float | str | bool]:
        trend_direction, structure_direction, flow_direction = self._direction_components(context)
        alignment_score = trend_direction + structure_direction + flow_direction
        flow_support_score = context.trade_flow_bias + context.book_imbalance
        side_bias = 1 if side == "long" else -1
        flow_alignment = "supportive" if flow_direction == side_bias else "fading"
        if abs(flow_support_score) < 0.05:
            flow_alignment = "neutral"
        return {
            "global_regime": context.global_regime or context.regime,
            "cluster_regime": context.cluster_regime or context.regime,
            "market_cluster": context.market_cluster,
            "cluster_leader": context.cluster_leader,
            "cluster_aligned": context.cluster_aligned,
            "btc_aligned": context.btc_aligned,
            "reclaim_context": reclaim,
            "cluster_strategy": self._cluster_strategy_name(
                context,
                setup=setup,
                side=side,
            ),
            "trend_bps": round(context.trend_bps, 4),
            "structure_score": round(context.structure_score, 4),
            "vwap_distance_bps": round(context.vwap_distance_bps, 4),
            "spread_bps": round(context.spread_bps, 4),
            "funding_rate": round(context.funding_rate, 8),
            "bucket_range_bps": round(context.bucket_range_bps, 4),
            "bucket_trade_count": float(context.bucket_trade_count),
            "bucket_notional_usd": round(context.bucket_notional_usd, 4),
            "activity_ratio": round(context.activity_ratio, 4),
            "trade_count_ratio": round(context.trade_count_ratio, 4),
            "book_imbalance": round(context.book_imbalance, 4),
            "trade_flow_bias": round(context.trade_flow_bias, 4),
            "flow_support_score": round(flow_support_score, 4),
            "alignment_score": float(alignment_score),
            "trend_direction": _direction_label(trend_direction),
            "structure_direction": _direction_label(structure_direction),
            "flow_direction": _direction_label(flow_direction),
            "flow_alignment": flow_alignment,
            "trend_bucket": _trend_bucket(context.trend_bps),
            "structure_bucket": _structure_bucket(context.structure_score),
            "vwap_bucket": _vwap_bucket(context.vwap_distance_bps),
            "activity_bucket": _activity_bucket(context.activity_ratio),
            "trade_count_bucket": _activity_bucket(context.trade_count_ratio),
            "flow_bucket": _flow_bucket(flow_support_score),
            "external_reference_available": context.external_reference_source_count > 0,
            "external_reference_price": round(context.external_reference_price or 0.0, 8),
            "external_reference_source_count": float(context.external_reference_source_count),
            "external_reference_sources": context.external_reference_sources,
            "external_reference_symbol": context.external_reference_symbol,
            "external_reference_time": context.external_reference_time,
            "external_reference_age_seconds": round(
                float(context.external_reference_age_seconds or 0.0),
                4,
            ),
            "external_reference_max_deviation_bps": round(
                context.external_reference_max_deviation_bps,
                4,
            ),
            "external_premium_bps": round(context.external_premium_bps, 4),
            "external_momentum_60s_bps": round(context.external_momentum_60s_bps, 4),
            "external_momentum_300s_bps": round(context.external_momentum_300s_bps, 4),
            "external_alignment_score": round(context.external_alignment_score, 4),
        }

    def _review_setup_details(
        self,
        context: TradfiTrendContext,
        *,
        preferred_side: str | None,
    ) -> dict[str, float | str | bool]:
        trend_direction, structure_direction, flow_direction = self._direction_components(context)
        alignment_score = trend_direction + structure_direction + flow_direction
        flow_support_score = context.trade_flow_bias + context.book_imbalance
        return {
            "global_regime": context.global_regime or context.regime,
            "cluster_regime": context.cluster_regime or context.regime,
            "market_cluster": context.market_cluster,
            "cluster_leader": context.cluster_leader,
            "cluster_aligned": context.cluster_aligned,
            "btc_aligned": context.btc_aligned,
            "preferred_side": preferred_side or "neutral",
            "trend_bps": round(context.trend_bps, 4),
            "structure_score": round(context.structure_score, 4),
            "vwap_distance_bps": round(context.vwap_distance_bps, 4),
            "spread_bps": round(context.spread_bps, 4),
            "funding_rate": round(context.funding_rate, 8),
            "bucket_range_bps": round(context.bucket_range_bps, 4),
            "bucket_trade_count": float(context.bucket_trade_count),
            "bucket_notional_usd": round(context.bucket_notional_usd, 4),
            "activity_ratio": round(context.activity_ratio, 4),
            "trade_count_ratio": round(context.trade_count_ratio, 4),
            "book_imbalance": round(context.book_imbalance, 4),
            "trade_flow_bias": round(context.trade_flow_bias, 4),
            "flow_support_score": round(flow_support_score, 4),
            "alignment_score": float(alignment_score),
            "trend_direction": _direction_label(trend_direction),
            "structure_direction": _direction_label(structure_direction),
            "flow_direction": _direction_label(flow_direction),
            "trend_bucket": _trend_bucket(context.trend_bps),
            "structure_bucket": _structure_bucket(context.structure_score),
            "vwap_bucket": _vwap_bucket(context.vwap_distance_bps),
            "activity_bucket": _activity_bucket(context.activity_ratio),
            "trade_count_bucket": _activity_bucket(context.trade_count_ratio),
            "flow_bucket": _flow_bucket(flow_support_score),
            "external_reference_available": context.external_reference_source_count > 0,
            "external_reference_price": round(context.external_reference_price or 0.0, 8),
            "external_reference_source_count": float(context.external_reference_source_count),
            "external_reference_sources": context.external_reference_sources,
            "external_reference_symbol": context.external_reference_symbol,
            "external_reference_time": context.external_reference_time,
            "external_reference_age_seconds": round(
                float(context.external_reference_age_seconds or 0.0),
                4,
            ),
            "external_reference_max_deviation_bps": round(
                context.external_reference_max_deviation_bps,
                4,
            ),
            "external_premium_bps": round(context.external_premium_bps, 4),
            "external_momentum_60s_bps": round(context.external_momentum_60s_bps, 4),
            "external_momentum_300s_bps": round(context.external_momentum_300s_bps, 4),
            "external_alignment_score": round(context.external_alignment_score, 4),
        }

    def reset(self) -> None:
        self._notional_history.clear()
        self._trade_count_history.clear()
