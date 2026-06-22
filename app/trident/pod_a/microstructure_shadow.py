from __future__ import annotations

import math

from app.trident.pod_a.filters import max_spread_bps_for_cluster
from app.trident.pod_a.signals import AnchorTrendContext


def microstructure_shadow_setup_details(
    context: AnchorTrendContext,
    *,
    side: str,
) -> dict[str, float | str | bool]:
    """Research-only entry microstructure quality score for Pod A."""

    direction = -1.0 if str(side).lower() == "short" else 1.0
    max_spread = max(max_spread_bps_for_cluster(context.market_cluster), 0.1)
    spread_score = _declining_score(context.spread_bps, good=max_spread * 0.25, bad=max_spread)

    flow_value = direction * (
        context.trade_flow_bias * 0.50
        + context.book_imbalance * 0.30
        + context.delta_trade_flow_bias * 0.12
        + context.delta_book_imbalance * 0.08
    )
    flow_score = _rising_score(flow_value, bad=-0.15, good=0.25)

    microprice_value = direction * context.microprice_dislocation_bps
    microprice_missing = abs(context.microprice_dislocation_bps) <= 1e-9
    microprice_score = 0.5 if microprice_missing else _rising_score(
        microprice_value,
        bad=-0.80,
        good=1.20,
    )

    depth_ratio = _depth_ratio(context, direction=direction)
    depth_missing = depth_ratio is None
    depth_score = 0.5 if depth_missing else _rising_score(depth_ratio, bad=0.35, good=0.65)

    bucket_notional_usd = (
        context.bucket_notional_usd
        if context.bucket_notional_usd > 0.0
        else max(context.bucket_volume, 0.0) * max(context.price, 0.0)
    )
    notional_presence = _clamp(
        0.0
        if bucket_notional_usd <= 0.0
        else math.log10(max(bucket_notional_usd, 1.0)) / math.log10(10_000.0)
    )
    trade_presence = _clamp(context.bucket_trade_count / 30.0)
    volume_accel = _clamp((context.volume_ratio - 0.5) / 2.5)
    trade_accel = _clamp((context.trade_count_ratio - 0.5) / 2.5)
    activity_score = _clamp(
        notional_presence * 0.45
        + trade_presence * 0.25
        + volume_accel * 0.15
        + trade_accel * 0.15
    )

    range_score = _range_score(context.bucket_range_bps, context.realized_vol_short_bps)
    churn_score, churn_value = _churn_score(context)

    score = _clamp(
        spread_score * 0.18
        + flow_score * 0.22
        + microprice_score * 0.18
        + depth_score * 0.12
        + activity_score * 0.14
        + range_score * 0.10
        + churn_score * 0.06
    )
    missing_flags = []
    if microprice_missing:
        missing_flags.append("microprice")
    if depth_missing:
        missing_flags.append("depth")

    return {
        "microstructure_shadow_active": True,
        "microstructure_shadow_version": "p115_v1",
        "microstructure_shadow_side": str(side).lower() or "long",
        "microstructure_shadow_score": round(score, 4),
        "microstructure_shadow_bucket": _score_bucket(score),
        "microstructure_shadow_spread_score": round(spread_score, 4),
        "microstructure_shadow_flow_score": round(flow_score, 4),
        "microstructure_shadow_microprice_score": round(microprice_score, 4),
        "microstructure_shadow_depth_score": round(depth_score, 4),
        "microstructure_shadow_activity_score": round(activity_score, 4),
        "microstructure_shadow_range_score": round(range_score, 4),
        "microstructure_shadow_churn_score": round(churn_score, 4),
        "microstructure_shadow_flow": round(flow_value, 4),
        "microstructure_shadow_microprice_bps": round(microprice_value, 4),
        "microstructure_shadow_depth_ratio": round(depth_ratio or 0.0, 4),
        "microstructure_shadow_bucket_notional_usd": round(bucket_notional_usd, 4),
        "microstructure_shadow_churn": round(churn_value, 4),
        "microstructure_shadow_missing_flags": ",".join(missing_flags),
    }


def _score_bucket(score: float) -> str:
    if score >= 0.72:
        return "strong"
    if score >= 0.56:
        return "ok"
    if score >= 0.42:
        return "weak"
    return "poor"


def _depth_ratio(context: AnchorTrendContext, *, direction: float) -> float | None:
    bid_depth = max(context.bid_depth_10bps, context.best_bid_size, 0.0)
    ask_depth = max(context.ask_depth_10bps, context.best_ask_size, 0.0)
    total = bid_depth + ask_depth
    if total <= 0.0:
        return None
    bid_ratio = bid_depth / total
    return bid_ratio if direction > 0.0 else 1.0 - bid_ratio


def _range_score(bucket_range_bps: float, realized_vol_short_bps: float) -> float:
    effective_range = max(bucket_range_bps, realized_vol_short_bps, 0.0)
    if effective_range <= 0.0:
        return 0.5
    return _declining_score(effective_range, good=15.0, bad=110.0)


def _churn_score(context: AnchorTrendContext) -> tuple[float, float]:
    depth_velocity = (
        abs(context.bid_depth_velocity)
        + abs(context.ask_depth_velocity)
        + abs(context.best_bid_size_velocity)
        + abs(context.best_ask_size_velocity)
    )
    flow_flip = abs(context.delta_trade_flow_bias) + abs(context.delta_book_imbalance)
    spread_instability = abs(context.delta_spread_bps) / 2.0
    range_floor = max(context.bucket_range_bps, 1.0)
    spread_range_ratio = max(context.spread_bps, 0.0) / range_floor
    churn = (
        _clamp(depth_velocity / 3.0) * 0.35
        + _clamp(flow_flip / 0.8) * 0.25
        + _clamp(spread_instability) * 0.20
        + _clamp(spread_range_ratio / 0.25) * 0.20
    )
    return 1.0 - _clamp(churn), churn


def _rising_score(value: float, *, bad: float, good: float) -> float:
    if good <= bad:
        return 0.0
    return _clamp((value - bad) / (good - bad))


def _declining_score(value: float, *, good: float, bad: float) -> float:
    if bad <= good:
        return 0.0
    return 1.0 - _clamp((value - good) / (bad - good))


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))
