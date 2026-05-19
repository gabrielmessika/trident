from __future__ import annotations

from app.settings import AppConfig, load_config
from app.trident.pod_a.filters import (
    max_abs_funding_rate_for_cluster,
    max_spread_bps_for_cluster,
    passes_anchor_filters,
)
from app.trident.pod_a.setups import (
    ema_separation_bps,
    flow_alignment_short,
    is_bos_retest_long,
    is_bos_retest_short,
    is_liquidity_sweep_reclaim_long,
    is_liquidity_sweep_reclaim_short,
    is_vwap_reclaim_long,
    is_vwap_reclaim_short,
    nearest_resistance_level,
    nearest_support_level,
)
from app.trident.pod_a.symbol_mode import active_symbol_mode
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
        "structure_ready": context.structure_ready,
        "range_high_1h": round(context.range_high_1h, 8),
        "range_low_1h": round(context.range_low_1h, 8),
        "swing_high_1h": round(context.swing_high_1h, 8),
        "swing_low_1h": round(context.swing_low_1h, 8),
        "bos_long_confirmed": context.bos_long_confirmed,
        "bos_short_confirmed": context.bos_short_confirmed,
        "ichimoku_bias_score": round(context.ichimoku_bias_score, 4),
        "supertrend_direction": context.supertrend_direction,
        "stoch_rsi_k": round(context.stoch_rsi_k, 4),
        "cci20": round(context.cci20, 4),
        "vwap_reclaim_score": round(context.vwap_reclaim_score, 4),
        "prev_ema50_ready_1h": context.prev_ema50_ready_1h,
        "prev_rsi14_1h": round(context.prev_rsi14_1h, 4),
        "prev_ema20_distance_ema50_1h_pct": round(
            context.prev_ema20_distance_ema50_1h_pct,
            4,
        ),
        "entry_vs_open_1h_bps": round(context.entry_vs_open_1h_bps, 4),
        "prev_ema50_ready_4h": context.prev_ema50_ready_4h,
        "prev_rsi14_4h": round(context.prev_rsi14_4h, 4),
        "prev_ema50_distance_4h_pct": round(context.prev_ema50_distance_4h_pct, 4),
        "rsi21_4h": round(context.rsi21_4h, 4),
        "ema50_distance_4h_pct": round(context.ema50_distance_4h_pct, 4),
        "ema50_distance_4h_atr": round(context.ema50_distance_4h_atr, 4),
        "macd_hist_4h": round(context.macd_hist_4h, 8),
        "macd_hist_delta_4h": round(context.macd_hist_delta_4h, 8),
        "upper_wick_ratio_4h": round(context.upper_wick_ratio_4h, 4),
        "lower_wick_ratio_4h": round(context.lower_wick_ratio_4h, 4),
        "bb_position_4h": round(context.bb_position_4h, 4),
        "btc_overextension_score": round(context.btc_overextension_score, 4),
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


class AnchorTrendService:
    """Minimal Pod A signal generator for trend-following setups."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or load_config("config/trident.toml")
        self._allowed_setups = {
            item.strip() for item in self._config.pod_a.allowed_setups if item.strip()
        }
        self._disabled_setups = {
            item.strip() for item in self._config.pod_a.disabled_setups if item.strip()
        }

    def evaluate(self, context: AnchorTrendContext) -> AnchorTrendSignal | None:
        if not passes_anchor_filters(context):
            return None

        if is_bos_retest_long(context) and context.bos_long_confirmed:
            components = self._confidence_components(context, "long")
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
            components = self._confidence_components(context, "short")
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
            components = self._confidence_components(context, "long")
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
            components = self._confidence_components(context, "short")
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
            components = self._confidence_components(context, "long")
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
            components = self._confidence_components(context, "short")
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
            components = self._confidence_components(context, "long")
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
            components = self._confidence_components(context, "short")
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

        if self._is_reversal_fade_short(context):
            components = self._confidence_components(context, "short")
            components["setup_bonus"] = 0.09
            resistance_level = nearest_resistance_level(context) or 0.0
            support_level = nearest_support_level(context) or 0.0
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="short",
                setup="reversal_fade_short",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=short_invalidation_price(
                    price=max(context.price, resistance_level or context.price),
                    ema_slow=max(context.ema_fast, context.ema_slow),
                    bucket_range_bps=max(context.bucket_range_bps, 24.0),
                ),
                setup_details=_with_regime(
                    context,
                    {
                        "family": "reversal_fade",
                        "structure_score": round(context.structure_score, 4),
                        "candles_ready": context.candles_ready,
                        "trend_1h_bps": round(context.trend_1h_bps, 4),
                        "trend_4h_bps": round(context.trend_4h_bps, 4),
                        "rejection_flow": round(flow_alignment_short(context), 4),
                        "resistance_level_1h": round(resistance_level, 8),
                        "support_level_1h": round(support_level, 8),
                    },
                ),
                confidence_components=components,
            )

        if self._setup_allowed_for_symbol("ichimoku_continuation_long", context.symbol) and self._is_ichimoku_continuation_long(context):
            components = self._confidence_components(context, "long")
            components["setup_bonus"] = 0.07
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="long",
                setup="ichimoku_continuation_long",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=long_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=max(context.bucket_range_bps, 18.0),
                ),
                setup_details=_with_regime(
                    context,
                    {
                        "family": "ichimoku_continuation",
                        "structure_score": round(context.structure_score, 4),
                        "candles_ready": context.candles_ready,
                        "trend_1h_bps": round(context.trend_1h_bps, 4),
                        "trend_4h_bps": round(context.trend_4h_bps, 4),
                        "mtf_bias_score": round(context.mtf_bias_score, 4),
                    },
                ),
                confidence_components=components,
            )

        if self._setup_allowed_for_symbol("ichimoku_continuation_short", context.symbol) and self._is_ichimoku_continuation_short(context):
            components = self._confidence_components(context, "short")
            components["setup_bonus"] = 0.07
            return AnchorTrendSignal(
                symbol=context.symbol,
                side="short",
                setup="ichimoku_continuation_short",
                confidence=round(self._aggregate_confidence(components), 3),
                entry_price=context.price,
                market_cluster=context.market_cluster,
                cluster_leader=context.cluster_leader,
                invalidation_price=short_invalidation_price(
                    price=context.price,
                    ema_slow=context.ema_slow,
                    bucket_range_bps=max(context.bucket_range_bps, 18.0),
                ),
                setup_details=_with_regime(
                    context,
                    {
                        "family": "ichimoku_continuation",
                        "structure_score": round(context.structure_score, 4),
                        "candles_ready": context.candles_ready,
                        "trend_1h_bps": round(context.trend_1h_bps, 4),
                        "trend_4h_bps": round(context.trend_4h_bps, 4),
                        "mtf_bias_score": round(context.mtf_bias_score, 4),
                    },
                ),
                confidence_components=components,
            )

        if self._is_long_setup(context):
            components = self._confidence_components(context, "long")
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
                setup_details=_with_regime(
                    context,
                    {
                        "family": "trend_pullback",
                        "structure_score": round(context.structure_score, 4),
                        "candles_ready": context.candles_ready,
                        "trend_1h_bps": round(context.trend_1h_bps, 4),
                        "trend_4h_bps": round(context.trend_4h_bps, 4),
                    },
                ),
                confidence_components=components,
            )

        if self._is_short_setup(context):
            components = self._confidence_components(context, "short")
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
                setup_details=_with_regime(
                    context,
                    {
                        "family": "trend_pullback",
                        "structure_score": round(context.structure_score, 4),
                        "candles_ready": context.candles_ready,
                        "trend_1h_bps": round(context.trend_1h_bps, 4),
                        "trend_4h_bps": round(context.trend_4h_bps, 4),
                    },
                ),
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

    def review_context(self, context: AnchorTrendContext) -> dict[str, object]:
        candidates = {
            "bos_retest_long": is_bos_retest_long(context),
            "bos_retest_short": is_bos_retest_short(context),
            "liquidity_sweep_reclaim_long": is_liquidity_sweep_reclaim_long(context),
            "liquidity_sweep_reclaim_short": is_liquidity_sweep_reclaim_short(context),
            "vwap_reclaim_long": is_vwap_reclaim_long(context),
            "vwap_reclaim_short": is_vwap_reclaim_short(context),
            "reversal_fade_short": self._is_reversal_fade_short(context),
            "ichimoku_continuation_long": self._setup_allowed_for_symbol(
                "ichimoku_continuation_long",
                context.symbol,
            )
            and self._is_ichimoku_continuation_long(context),
            "ichimoku_continuation_short": self._setup_allowed_for_symbol(
                "ichimoku_continuation_short",
                context.symbol,
            )
            and self._is_ichimoku_continuation_short(context),
            "trend_pullback_long": self._is_long_setup(context),
            "trend_pullback_short": self._is_short_setup(context),
        }
        ready_setups = [name for name, is_ready in candidates.items() if is_ready]
        preferred_side = "long" if context.structure_score >= 0 else "short"
        failure_reasons = self._failure_reasons(context, preferred_side)
        reason_summary = (
            ", ".join(name.replace("_", " ") for name in ready_setups[:2])
            if ready_setups
            else ", ".join(self._humanize_reason(name) for name in failure_reasons[:3])
        )
        return {
            "symbol": context.symbol,
            "status": "signaled" if ready_setups else "filtered",
            "preferred_side": preferred_side,
            "candidate_setups": ready_setups,
            "failure_reasons": failure_reasons,
            "reason_summary": reason_summary,
            "context": {
                "regime": context.regime,
                "structure_score": round(context.structure_score, 4),
                "vwap_distance_bps": round(context.vwap_distance_bps, 4),
                "ichimoku_bias_score": round(context.ichimoku_bias_score, 4),
                "supertrend_direction": context.supertrend_direction,
                "stoch_rsi_k": round(context.stoch_rsi_k, 4),
                "cci20": round(context.cci20, 4),
                "vwap_reclaim_score": round(context.vwap_reclaim_score, 4),
            },
        }

    def _is_long_setup(self, context: AnchorTrendContext) -> bool:
        return (
            self._setup_regime_allowed(context)
            and context.structure_score >= self._min_setup_structure_score()
            and self._ema_stack_bullish(context)
            and context.vwap_distance_bps >= -MAX_PULLBACK_DISTANCE_BPS
            and self._passes_indicator_vetoes(context, "long")
        )

    def _is_short_setup(self, context: AnchorTrendContext) -> bool:
        return (
            self._setup_regime_allowed(context)
            and context.structure_score <= -self._min_setup_structure_score()
            and self._ema_stack_bearish(context)
            and context.vwap_distance_bps <= MAX_PULLBACK_DISTANCE_BPS
            and self._passes_indicator_vetoes(context, "short")
        )

    def _setup_regime_allowed(self, context: AnchorTrendContext) -> bool:
        allowed = {
            item.strip()
            for item in self._config.pod_a.setup_allowed_regimes
            if item.strip()
        }
        return not allowed or context.regime in allowed

    def _min_setup_structure_score(self) -> float:
        return max(float(self._config.pod_a.min_setup_structure_score), 0.0)

    def _ema_tolerance(self, context: AnchorTrendContext) -> float:
        return max(context.price, 0.0) * max(
            float(self._config.pod_a.setup_ema_tolerance_bps),
            0.0,
        ) / 10_000.0

    def _ema_stack_bullish(self, context: AnchorTrendContext) -> bool:
        tolerance = self._ema_tolerance(context)
        return (
            context.price + tolerance >= context.ema_fast
            and context.ema_fast + tolerance >= context.ema_slow
        )

    def _ema_stack_bearish(self, context: AnchorTrendContext) -> bool:
        tolerance = self._ema_tolerance(context)
        return (
            context.price <= context.ema_fast + tolerance
            and context.ema_fast <= context.ema_slow + tolerance
        )

    def _setup_allowed_for_symbol(self, setup: str, symbol: str | None) -> bool:
        symbol_mode = active_symbol_mode(self._config.pod_a, symbol)
        symbol_mode_allowed_setups = (
            {item.strip() for item in symbol_mode.allowed_setups if item.strip()}
            if symbol_mode is not None
            else set()
        )
        if symbol_mode_allowed_setups and setup not in symbol_mode_allowed_setups:
            return False
        if (
            self._allowed_setups
            and setup not in self._allowed_setups
            and setup not in symbol_mode_allowed_setups
        ):
            return False
        if setup in self._disabled_setups and setup not in symbol_mode_allowed_setups:
            return False
        return True

    def _is_ichimoku_continuation_long(self, context: AnchorTrendContext) -> bool:
        return (
            context.regime == "TrendExpansion"
            and context.candles_ready
            and context.structure_score >= 0.25
            and context.price >= context.ema_fast >= context.ema_slow
            and context.trend_1h_bps >= 0.0
            and context.trend_4h_bps >= -5.0
            and context.mtf_bias_score >= 5.0
            and context.ichimoku_bias_score >= 0.25
            and context.supertrend_direction > 0
            and 0.35 <= context.stoch_rsi_k <= 0.95
            and context.vwap_reclaim_score >= -0.10
        )

    def _is_ichimoku_continuation_short(self, context: AnchorTrendContext) -> bool:
        return (
            context.regime == "TrendExpansion"
            and context.candles_ready
            and context.structure_score <= -0.25
            and context.price <= context.ema_fast <= context.ema_slow
            and context.trend_1h_bps <= 0.0
            and context.trend_4h_bps <= 5.0
            and context.mtf_bias_score <= -5.0
            and context.ichimoku_bias_score <= -0.25
            and context.supertrend_direction < 0
            and 0.05 <= context.stoch_rsi_k <= 0.65
            and context.vwap_reclaim_score <= 0.10
        )

    def _is_reversal_fade_short(self, context: AnchorTrendContext) -> bool:
        config = self._config.pod_a.reversal_fade
        if not config.enabled:
            return False
        if context.market_cluster != "crypto":
            return False
        if config.allowed_regimes and context.regime not in config.allowed_regimes:
            return False
        if not context.structure_ready:
            return False
        resistance_level = nearest_resistance_level(context)
        support_level = nearest_support_level(context)
        if resistance_level is None or support_level is None:
            return False
        resistance_distance_bps = abs((context.price - resistance_level) / context.price * 10_000.0)
        support_distance_bps = (context.price - support_level) / context.price * 10_000.0
        overextended = (
            context.stoch_rsi_k >= config.min_stoch_rsi_k
            or context.cci20 >= config.min_cci20
        )
        return (
            context.trend_1h_bps >= config.min_trend_1h_bps
            and context.trend_4h_bps >= config.min_trend_4h_bps
            and context.price <= context.ema_fast
            and context.ema_fast >= context.ema_slow
            and resistance_distance_bps <= config.max_distance_from_resistance_bps
            and support_distance_bps >= config.min_target_to_support_bps
            and flow_alignment_short(context) >= config.min_rejection_flow
            and context.supertrend_direction <= 0
            and context.vwap_reclaim_score <= config.max_vwap_reclaim_score
            and overextended
        )

    def _passes_indicator_vetoes(self, context: AnchorTrendContext, side: str) -> bool:
        if context.market_cluster != "crypto":
            return True
        if side == "long":
            return (
                context.supertrend_direction >= 0
                and context.ichimoku_bias_score >= -0.15
                and context.vwap_reclaim_score >= -0.10
                and not (context.stoch_rsi_k >= 0.94 and context.cci20 >= 180.0)
            )
        return (
            context.supertrend_direction <= 0
            and context.ichimoku_bias_score <= 0.15
            and context.vwap_reclaim_score <= 0.10
            and not (context.stoch_rsi_k <= 0.06 and context.cci20 <= -180.0)
        )

    def _failure_reasons(self, context: AnchorTrendContext, side: str) -> list[str]:
        reasons: list[str] = []
        if not context.cluster_aligned:
            reasons.append("cluster_not_aligned")
        if context.spread_bps > max_spread_bps_for_cluster(context.market_cluster):
            reasons.append("spread_too_wide")
        if abs(context.funding_rate) > max_abs_funding_rate_for_cluster(context.market_cluster):
            reasons.append("funding_too_extreme")
        if side == "long":
            if not self._setup_regime_allowed(context):
                reasons.append("regime_not_trend_expansion")
            if context.structure_score < self._min_setup_structure_score():
                reasons.append("structure_too_weak_for_long")
            if not self._ema_stack_bullish(context):
                reasons.append("ema_stack_not_bullish")
            if context.vwap_distance_bps < -MAX_PULLBACK_DISTANCE_BPS:
                reasons.append("pullback_too_deep")
            if context.market_cluster == "crypto":
                if context.supertrend_direction < 0:
                    reasons.append("supertrend_against")
                if context.ichimoku_bias_score < -0.15:
                    reasons.append("ichimoku_against")
                if context.vwap_reclaim_score < -0.10:
                    reasons.append("vwap_reclaim_weak")
                if context.stoch_rsi_k >= 0.94 and context.cci20 >= 180.0:
                    reasons.append("market_overextended")
        else:
            if not self._setup_regime_allowed(context):
                reasons.append("regime_not_trend_expansion")
            if context.structure_score > -self._min_setup_structure_score():
                reasons.append("structure_too_weak_for_short")
            if not self._ema_stack_bearish(context):
                reasons.append("ema_stack_not_bearish")
            if context.vwap_distance_bps > MAX_PULLBACK_DISTANCE_BPS:
                reasons.append("pullback_too_deep")
            if context.market_cluster == "crypto":
                if context.supertrend_direction > 0:
                    reasons.append("supertrend_against")
                if context.ichimoku_bias_score > 0.15:
                    reasons.append("ichimoku_against")
                if context.vwap_reclaim_score > 0.10:
                    reasons.append("vwap_reclaim_weak")
                if context.stoch_rsi_k <= 0.06 and context.cci20 <= -180.0:
                    reasons.append("market_overextended")
        if not reasons:
            reasons.append("no_setup_family_match")
        return reasons

    def _humanize_reason(self, reason: str) -> str:
        return reason.replace("_", " ")

    def _directional_confirmation_quality(self, context: AnchorTrendContext, side: str) -> float:
        direction = 1.0 if side == "long" else -1.0
        ichimoku_quality = _clamp(0.5 + context.ichimoku_bias_score * direction * 0.5)
        if context.supertrend_direction == 0:
            supertrend_quality = 0.5
        else:
            supertrend_quality = 1.0 if context.supertrend_direction == int(direction) else 0.0
        reclaim_quality = _clamp(0.5 + context.vwap_reclaim_score * direction * 0.5)
        return round(
            ichimoku_quality * 0.40 + supertrend_quality * 0.35 + reclaim_quality * 0.25,
            4,
        )

    def _extension_quality(self, context: AnchorTrendContext, side: str) -> float:
        if side == "long":
            stoch_quality = (
                1.0
                if context.stoch_rsi_k <= 0.82
                else _clamp(1.0 - (context.stoch_rsi_k - 0.82) / 0.18)
            )
            cci_quality = (
                1.0
                if context.cci20 <= 120.0
                else _clamp(1.0 - (context.cci20 - 120.0) / 120.0)
            )
        else:
            stoch_quality = 1.0 if context.stoch_rsi_k >= 0.18 else _clamp(context.stoch_rsi_k / 0.18)
            cci_quality = 1.0 if context.cci20 >= -120.0 else _clamp((context.cci20 + 240.0) / 120.0)
        return round(stoch_quality * 0.55 + cci_quality * 0.45, 4)

    def _confidence_components(self, context: AnchorTrendContext, side: str) -> dict[str, float]:
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
        confirmation_quality = self._directional_confirmation_quality(context, side)
        extension_quality = self._extension_quality(context, side)
        return {
            "structure_quality": round(structure_quality, 4),
            "trend_quality": round(trend_quality, 4),
            "pullback_quality": round(pullback_quality, 4),
            "spread_quality": round(spread_quality, 4),
            "funding_quality": round(funding_quality, 4),
            "mtf_quality": round(mtf_quality, 4),
            "structure_break_quality": round(structure_break_quality, 4),
            "confirmation_quality": confirmation_quality,
            "extension_quality": extension_quality,
            "setup_bonus": 0.0,
        }

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["structure_quality"] * 0.28
            + components["trend_quality"] * 0.18
            + components["pullback_quality"] * 0.14
            + components["spread_quality"] * 0.08
            + components["funding_quality"] * 0.05
            + components["mtf_quality"] * 0.08
            + components["structure_break_quality"] * 0.08
            + components.get("confirmation_quality", 0.5) * 0.07
            + components.get("extension_quality", 0.5) * 0.04
            + components.get("setup_bonus", 0.0)
        )
