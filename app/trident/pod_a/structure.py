from __future__ import annotations


def long_invalidation_price(*, price: float, ema_slow: float, bucket_range_bps: float) -> float:
    range_buffer = max(bucket_range_bps, 25.0) / 10_000.0
    return round(min(ema_slow, price) * (1.0 - range_buffer * 0.35), 8)


def short_invalidation_price(*, price: float, ema_slow: float, bucket_range_bps: float) -> float:
    range_buffer = max(bucket_range_bps, 25.0) / 10_000.0
    return round(max(ema_slow, price) * (1.0 + range_buffer * 0.35), 8)


def stop_bps_from_invalidation(
    *,
    entry_price: float,
    invalidation_price: float | None,
    side: str,
    fallback_bps: float,
    min_stop_bps: float = 45.0,
    max_stop_bps: float = 160.0,
) -> float:
    if invalidation_price is None or entry_price <= 0:
        return round(fallback_bps, 4)
    if side == "long":
        raw_bps = (entry_price - invalidation_price) / entry_price * 10_000.0
    else:
        raw_bps = (invalidation_price - entry_price) / entry_price * 10_000.0
    if raw_bps <= 0:
        return round(fallback_bps, 4)
    return round(max(min_stop_bps, min(raw_bps, max_stop_bps)), 4)
