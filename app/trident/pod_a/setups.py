from __future__ import annotations

from app.trident.pod_a.signals import AnchorTrendContext


MIN_BOS_STRUCTURE_SCORE = 0.55
MAX_BOS_RETEST_DISTANCE_BPS = 12.0
MIN_BOS_EMA_SEPARATION_BPS = 5.0
MIN_RECLAIM_FLOW = 0.12
MIN_SWEEP_RANGE_BPS = 18.0
MIN_SWEEP_RANGE_BPS_EARLY = 60.0
MAX_VWAP_RECLAIM_DISTANCE_BPS = 8.0
MIN_VWAP_RECLAIM_STRUCTURE = 0.48
MIN_EARLY_TREND_MTF_BIAS = 15.0


def ema_separation_bps(context: AnchorTrendContext) -> float:
    if context.price <= 0:
        return 0.0
    return abs(context.ema_fast - context.ema_slow) / context.price * 10_000.0


def flow_alignment_long(context: AnchorTrendContext) -> float:
    return (context.trade_flow_bias + context.book_imbalance) / 2.0


def flow_alignment_short(context: AnchorTrendContext) -> float:
    return (-context.trade_flow_bias - context.book_imbalance) / 2.0


def distance_to_level_bps(price: float, level: float) -> float:
    if price <= 0 or level <= 0:
        return 0.0
    return ((price - level) / price) * 10_000.0


def nearest_resistance_level(context: AnchorTrendContext) -> float | None:
    candidates = [
        level
        for level in (context.swing_high_1h, context.range_high_1h)
        if level > context.price > 0
    ]
    if not candidates:
        return None
    return min(candidates)


def nearest_support_level(context: AnchorTrendContext) -> float | None:
    candidates = [
        level
        for level in (context.swing_low_1h, context.range_low_1h)
        if context.price > level > 0
    ]
    if not candidates:
        return None
    return max(candidates)


def allows_long_mtf(context: AnchorTrendContext) -> bool:
    if not context.candles_ready:
        return True
    return (
        context.trend_1h_bps >= 0.0
        and context.trend_4h_bps >= -10.0
        and context.mtf_bias_score >= -5.0
    )


def allows_short_mtf(context: AnchorTrendContext) -> bool:
    if not context.candles_ready:
        return True
    return (
        context.trend_1h_bps <= 0.0
        and context.trend_4h_bps <= 10.0
        and context.mtf_bias_score <= 5.0
    )


def allows_long_structural_regime(context: AnchorTrendContext) -> bool:
    if context.regime == "TrendExpansion":
        return allows_long_mtf(context)
    if context.regime not in {"RangeAuction", "DeadZone"}:
        return False
    if not context.candles_ready:
        return False
    return (
        context.trend_1h_bps >= 0.0
        and context.trend_4h_bps >= -5.0
        and context.mtf_bias_score >= MIN_EARLY_TREND_MTF_BIAS
    )


def allows_short_structural_regime(context: AnchorTrendContext) -> bool:
    if context.regime == "TrendExpansion":
        return allows_short_mtf(context)
    if context.regime not in {"RangeAuction", "DeadZone"}:
        return False
    if not context.candles_ready:
        return False
    return (
        context.trend_1h_bps <= 0.0
        and context.trend_4h_bps <= 5.0
        and context.mtf_bias_score <= -MIN_EARLY_TREND_MTF_BIAS
    )


def min_sweep_range_bps(context: AnchorTrendContext) -> float:
    if context.regime == "TrendExpansion":
        return MIN_SWEEP_RANGE_BPS
    return MIN_SWEEP_RANGE_BPS_EARLY


def is_bos_retest_long(context: AnchorTrendContext) -> bool:
    structure_ok = context.structure_score >= 0.72
    if context.structure_ready and context.swing_high_1h > 0:
        structure_ok = (
            context.bos_long_confirmed
            or distance_to_level_bps(context.price, context.swing_high_1h) >= -12.0
        )
    return (
        allows_long_structural_regime(context)
        and context.structure_score >= MIN_BOS_STRUCTURE_SCORE
        and context.price >= context.ema_fast >= context.ema_slow
        and -MAX_BOS_RETEST_DISTANCE_BPS <= context.vwap_distance_bps <= MAX_BOS_RETEST_DISTANCE_BPS
        and ema_separation_bps(context) >= MIN_BOS_EMA_SEPARATION_BPS
        and structure_ok
    )


def is_bos_retest_short(context: AnchorTrendContext) -> bool:
    structure_ok = context.structure_score <= -0.72
    if context.structure_ready and context.swing_low_1h > 0:
        structure_ok = (
            context.bos_short_confirmed
            or distance_to_level_bps(context.price, context.swing_low_1h) <= 12.0
        )
    return (
        allows_short_structural_regime(context)
        and context.structure_score <= -MIN_BOS_STRUCTURE_SCORE
        and context.price <= context.ema_fast <= context.ema_slow
        and -MAX_BOS_RETEST_DISTANCE_BPS <= context.vwap_distance_bps <= MAX_BOS_RETEST_DISTANCE_BPS
        and ema_separation_bps(context) >= MIN_BOS_EMA_SEPARATION_BPS
        and structure_ok
    )


def is_vwap_reclaim_long(context: AnchorTrendContext) -> bool:
    return (
        allows_long_structural_regime(context)
        and context.structure_score >= MIN_VWAP_RECLAIM_STRUCTURE
        and context.price >= context.ema_fast >= context.ema_slow
        and -MAX_VWAP_RECLAIM_DISTANCE_BPS <= context.vwap_distance_bps <= MAX_VWAP_RECLAIM_DISTANCE_BPS
        and flow_alignment_long(context) >= 0.06
        and ema_separation_bps(context) >= 4.0
    )


def is_vwap_reclaim_short(context: AnchorTrendContext) -> bool:
    return (
        allows_short_structural_regime(context)
        and context.structure_score <= -MIN_VWAP_RECLAIM_STRUCTURE
        and context.price <= context.ema_fast <= context.ema_slow
        and -MAX_VWAP_RECLAIM_DISTANCE_BPS <= context.vwap_distance_bps <= MAX_VWAP_RECLAIM_DISTANCE_BPS
        and flow_alignment_short(context) >= 0.06
        and ema_separation_bps(context) >= 4.0
    )


def is_liquidity_sweep_reclaim_long(context: AnchorTrendContext) -> bool:
    return (
        allows_long_structural_regime(context)
        and context.structure_score >= 0.50
        and context.price >= context.ema_fast >= context.ema_slow
        and -24.0 <= context.vwap_distance_bps <= 6.0
        and context.bucket_range_bps >= min_sweep_range_bps(context)
        and flow_alignment_long(context) >= MIN_RECLAIM_FLOW
    )


def is_liquidity_sweep_reclaim_short(context: AnchorTrendContext) -> bool:
    return (
        allows_short_structural_regime(context)
        and context.structure_score <= -0.50
        and context.price <= context.ema_fast <= context.ema_slow
        and -6.0 <= context.vwap_distance_bps <= 24.0
        and context.bucket_range_bps >= min_sweep_range_bps(context)
        and flow_alignment_short(context) >= MIN_RECLAIM_FLOW
    )
