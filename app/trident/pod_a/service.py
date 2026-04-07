from __future__ import annotations

from app.trident.pod_a.filters import (
    max_abs_funding_rate_for_cluster,
    max_spread_bps_for_cluster,
    passes_anchor_filters,
)
from app.trident.pod_a.setups import (
    ema_separation_bps,
    is_bos_retest_long,
    is_bos_retest_short,
    is_liquidity_sweep_reclaim_long,
    is_liquidity_sweep_reclaim_short,
    is_vwap_reclaim_long,
    is_vwap_reclaim_short,
)
from app.trident.pod_a.structure import long_invalidation_price, short_invalidation_price
from app.trident.pod_a.signals import AnchorTrendContext, AnchorTrendSignal

MIN_SETUP_STRUCTURE_SCORE = 0.40
MAX_PULLBACK_DISTANCE_BPS = 25.0
PULLBACK_ANCHOR_BPS = 12.0


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _with_regime(context: AnchorTrendContext, details: dict[str, float | str | bool]) -> dict[str, float | str | bool]:
    return {
        **details,
        "regime": context.regime,
    }


class AnchorTrendService:
    """Minimal Pod A signal generator for trend-following setups."""

    def evaluate(self, context: AnchorTrendContext) -> AnchorTrendSignal | None:
        if not passes_anchor_filters(context):
            return None

        if is_bos_retest_long(context) and context.bos_long_confirmed:
            components = self._confidence_components(context)
            components["setup_bonus"] = 0.08
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="long",
                setup="bos_retest_long",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=long_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=context.bucket_range_bps,
                ),
                setup_details={
                    **_with_regime(
                        context,
                        {
                            "family": "bos_retest",
                            "ema_separation_bps": round(ema_separation_bps(context), 4),
                            "mtf_bias_score": round(context.mtf_bias_score, 4),
                            "bos_long_confirmed": context.bos_long_confirmed,
                            "swing_high_1h": round(context.swing_high_1h, 8),
                        },
                    )
                },
                confidence_components=components,
            )

        if is_bos_retest_short(context) and context.bos_short_confirmed:
            components = self._confidence_components(context)
            components["setup_bonus"] = 0.08
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="short",
                setup="bos_retest_short",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=short_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=context.bucket_range_bps,
                ),
                setup_details={
                    **_with_regime(
                        context,
                        {
                            "family": "bos_retest",
                            "ema_separation_bps": round(ema_separation_bps(context), 4),
                            "mtf_bias_score": round(context.mtf_bias_score, 4),
                            "bos_short_confirmed": context.bos_short_confirmed,
                            "swing_low_1h": round(context.swing_low_1h, 8),
                        },
                    )
                },
                confidence_components=components,
            )

        if is_liquidity_sweep_reclaim_long(context):
            components = self._confidence_components(context)
            components["setup_bonus"] = 0.06
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="long",
                setup="liquidity_sweep_reclaim_long",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=long_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=max(context.bucket_range_bps, 30.0),
                ),
                setup_details={
                    **_with_regime(
                        context,
                        {
                            "family": "liquidity_sweep_reclaim",
                            "bucket_range_bps": round(context.bucket_range_bps, 4),
                            "flow_alignment": round(
                                (context.trade_flow_bias + context.book_imbalance) / 2.0,
                                4,
                            ),
                            "mtf_bias_score": round(context.mtf_bias_score, 4),
                        },
                    )
                },
                confidence_components=components,
            )

        if is_liquidity_sweep_reclaim_short(context):
            components = self._confidence_components(context)
            components["setup_bonus"] = 0.06
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="short",
                setup="liquidity_sweep_reclaim_short",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=short_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=max(context.bucket_range_bps, 30.0),
                ),
                setup_details={
                    **_with_regime(
                        context,
                        {
                            "family": "liquidity_sweep_reclaim",
                            "bucket_range_bps": round(context.bucket_range_bps, 4),
                            "flow_alignment": round(
                                (-context.trade_flow_bias - context.book_imbalance) / 2.0,
                                4,
                            ),
                            "mtf_bias_score": round(context.mtf_bias_score, 4),
                        },
                    )
                },
                confidence_components=components,
            )

        if is_vwap_reclaim_long(context):
            components = self._confidence_components(context)
            components["setup_bonus"] = 0.04
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="long",
                setup="vwap_reclaim_long",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=long_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=max(context.bucket_range_bps, 20.0),
                ),
                setup_details={
                    **_with_regime(
                        context,
                        {
                            "family": "vwap_reclaim",
                            "flow_alignment": round(
                                (context.trade_flow_bias + context.book_imbalance) / 2.0,
                                4,
                            ),
                            "mtf_bias_score": round(context.mtf_bias_score, 4),
                        },
                    )
                },
                confidence_components=components,
            )

        if is_vwap_reclaim_short(context):
            components = self._confidence_components(context)
            components["setup_bonus"] = 0.04
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="short",
                setup="vwap_reclaim_short",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=short_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=max(context.bucket_range_bps, 20.0),
                ),
                setup_details={
                    **_with_regime(
                        context,
                        {
                            "family": "vwap_reclaim",
                            "flow_alignment": round(
                                (-context.trade_flow_bias - context.book_imbalance) / 2.0,
                                4,
                            ),
                            "mtf_bias_score": round(context.mtf_bias_score, 4),
                        },
                    )
                },
                confidence_components=components,
            )

        if is_bos_retest_long(context):
            components = self._confidence_components(context)
            components["setup_bonus"] = 0.08
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="long",
                setup="bos_retest_long",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=long_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=context.bucket_range_bps,
                ),
                setup_details={
                    **_with_regime(
                        context,
                        {
                            "family": "bos_retest",
                            "ema_separation_bps": round(ema_separation_bps(context), 4),
                            "mtf_bias_score": round(context.mtf_bias_score, 4),
                            "bos_long_confirmed": context.bos_long_confirmed,
                            "swing_high_1h": round(context.swing_high_1h, 8),
                        },
                    )
                },
                confidence_components=components,
            )

        if is_bos_retest_short(context):
            components = self._confidence_components(context)
            components["setup_bonus"] = 0.08
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="short",
                setup="bos_retest_short",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=short_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=context.bucket_range_bps,
                ),
                setup_details={
                    **_with_regime(
                        context,
                        {
                            "family": "bos_retest",
                            "ema_separation_bps": round(ema_separation_bps(context), 4),
                            "mtf_bias_score": round(context.mtf_bias_score, 4),
                            "bos_short_confirmed": context.bos_short_confirmed,
                            "swing_low_1h": round(context.swing_low_1h, 8),
                        },
                    )
                },
                confidence_components=components,
            )

        if self._is_long_setup(context):
            components = self._confidence_components(context)
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="long",
                setup="trend_pullback_long",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=long_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=max(context.bucket_range_bps, 18.0),
                ),
                setup_details=_with_regime(context, {"family": "trend_pullback"}),
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
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=short_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=max(context.bucket_range_bps, 18.0),
                ),
                setup_details=_with_regime(context, {"family": "trend_pullback"}),
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
            context.regime == "TrendExpansion"
            and context.structure_score >= MIN_SETUP_STRUCTURE_SCORE
            and context.price >= context.ema_fast >= context.ema_slow
            and context.vwap_distance_bps >= -MAX_PULLBACK_DISTANCE_BPS
        )

    def _is_short_setup(self, context: AnchorTrendContext) -> bool:
        return (
            context.regime == "TrendExpansion"
            and context.structure_score <= -MIN_SETUP_STRUCTURE_SCORE
            and context.price <= context.ema_fast <= context.ema_slow
            and context.vwap_distance_bps <= MAX_PULLBACK_DISTANCE_BPS
        )

    def _confidence_components(self, context: AnchorTrendContext) -> dict[str, float]:
        separation_bps = ema_separation_bps(context)
        structure_quality = _clamp((abs(context.structure_score) - 0.30) / 0.35)
        trend_quality = _clamp((separation_bps - 5.0) / 25.0)
        pullback_quality = _clamp(
            1.0 - abs(abs(context.vwap_distance_bps) - PULLBACK_ANCHOR_BPS) / 18.0,
        )
        spread_quality = _clamp(
            1.0 - context.spread_bps / max_spread_bps_for_cluster(context.market_cluster)
        )
        funding_quality = _clamp(
            1.0
            - abs(context.funding_rate)
            / max(max_abs_funding_rate_for_cluster(context.market_cluster), 1e-9)
        )
        mtf_quality = 0.5
        if context.candles_ready:
            mtf_quality = _clamp(0.5 + context.mtf_bias_score / 120.0)
        structure_break_quality = 0.5
        if context.structure_ready:
            if context.bos_long_confirmed or context.bos_short_confirmed:
                structure_break_quality = 1.0
            elif context.swing_high_1h > 0 or context.swing_low_1h > 0:
                structure_break_quality = 0.7
        return {
            "structure_quality": round(structure_quality, 4),
            "trend_quality": round(trend_quality, 4),
            "pullback_quality": round(pullback_quality, 4),
            "spread_quality": round(spread_quality, 4),
            "funding_quality": round(funding_quality, 4),
            "mtf_quality": round(mtf_quality, 4),
            "structure_break_quality": round(structure_break_quality, 4),
            "setup_bonus": 0.0,
        }

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["structure_quality"] * 0.31
            + components["trend_quality"] * 0.20
            + components["pullback_quality"] * 0.15
            + components["spread_quality"] * 0.10
            + components["funding_quality"] * 0.05
            + components["mtf_quality"] * 0.10
            + components["structure_break_quality"] * 0.09
            + components.get("setup_bonus", 0.0)
        )
