from __future__ import annotations

import math
import statistics
from typing import Any

from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import OutcomeMarket, ShortHorizonFeatures


def update_price_history_payload(
    payload: dict[str, Any],
    prices: dict[str, float],
    *,
    now_ts: int,
    max_age_seconds: int,
    sample_limit: int,
    payload_key: str = "short_expiry_price_history",
    min_sample_interval_seconds: int = 0,
) -> dict[str, list[dict[str, float]]]:
    raw_history = payload.get(payload_key, {})
    if not isinstance(raw_history, dict):
        raw_history = {}
    parsed: dict[str, list[dict[str, float]]] = {}
    cutoff = now_ts - max(int(max_age_seconds), 1)
    limit = max(int(sample_limit), 2)
    for underlying, raw_samples in raw_history.items():
        samples = _parse_samples(raw_samples)
        kept = [sample for sample in samples if int(sample["ts"]) >= cutoff]
        if kept:
            parsed[str(underlying).upper()] = kept[-limit:]

    for underlying, price in prices.items():
        if price <= 0:
            continue
        key = underlying.upper()
        samples = parsed.get(key, [])
        min_interval = max(int(min_sample_interval_seconds), 0)
        if not samples:
            samples.append({"ts": float(now_ts), "price": float(price)})
        elif int(samples[-1]["ts"]) == int(now_ts):
            samples[-1] = {"ts": float(now_ts), "price": float(price)}
        elif int(now_ts - samples[-1]["ts"]) >= min_interval:
            samples.append({"ts": float(now_ts), "price": float(price)})
        parsed[key] = [sample for sample in samples if int(sample["ts"]) >= cutoff][-limit:]

    payload[payload_key] = parsed
    return parsed


class ShortHorizonFeatureBuilder:
    def __init__(self, config: Hip4OutcomeConfig) -> None:
        self.config = config

    def build(
        self,
        *,
        market: OutcomeMarket,
        reference_price: float,
        now_ts: int,
        history: dict[str, list[dict[str, float]]],
    ) -> ShortHorizonFeatures:
        samples = _parse_samples(history.get(market.underlying.upper(), []))
        samples = [sample for sample in samples if int(sample["ts"]) <= now_ts and sample["price"] > 0]
        if not samples or int(samples[-1]["ts"]) != now_ts:
            samples.append({"ts": float(now_ts), "price": float(reference_price)})
        samples.sort(key=lambda item: item["ts"])
        span = int(max(samples[-1]["ts"] - samples[0]["ts"], 0.0)) if samples else 0
        windows = self.config.short_expiry_momentum_windows_seconds or [30, 60, 180]
        momentum = {
            int(window): _momentum_bps(
                samples=samples,
                current_price=reference_price,
                now_ts=now_ts,
                window_seconds=int(window),
            )
            for window in windows
            if int(window) > 0
        }
        distance = 0.0
        if market.strike > 0:
            distance = (reference_price / market.strike - 1.0) * 10_000.0
        return ShortHorizonFeatures(
            underlying=market.underlying,
            reference_price=reference_price,
            strike=market.strike,
            seconds_left=max(market.expiry_ts - now_ts, 0),
            sample_count=len(samples),
            history_span_seconds=span,
            distance_to_strike_bps=round(distance, 6),
            momentum_bps_by_window=momentum,
            realized_vol_bps_60s=_realized_vol_bps(samples=samples, now_ts=now_ts, window_seconds=60),
            velocity_bps_per_minute=_velocity_bps_per_minute(
                samples=samples,
                current_price=reference_price,
            ),
            has_min_history=span >= max(int(self.config.short_expiry_min_history_seconds), 0),
        )


def _parse_samples(raw_samples: object) -> list[dict[str, float]]:
    if not isinstance(raw_samples, list):
        return []
    samples: list[dict[str, float]] = []
    for raw in raw_samples:
        if not isinstance(raw, dict):
            continue
        try:
            ts = float(raw.get("ts"))
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            continue
        if ts <= 0 or price <= 0:
            continue
        samples.append({"ts": ts, "price": price})
    samples.sort(key=lambda item: item["ts"])
    return samples


def _momentum_bps(
    *,
    samples: list[dict[str, float]],
    current_price: float,
    now_ts: int,
    window_seconds: int,
) -> float | None:
    if current_price <= 0:
        return None
    cutoff = now_ts - max(int(window_seconds), 1)
    candidates = [sample for sample in samples if int(sample["ts"]) <= cutoff]
    if not candidates:
        return None
    base = candidates[-1]
    base_price = base["price"]
    if base_price <= 0:
        return None
    return round((current_price / base_price - 1.0) * 10_000.0, 6)


def _realized_vol_bps(
    *,
    samples: list[dict[str, float]],
    now_ts: int,
    window_seconds: int,
) -> float | None:
    cutoff = now_ts - max(int(window_seconds), 1)
    window = [sample for sample in samples if int(sample["ts"]) >= cutoff]
    if len(window) < 3:
        return None
    returns: list[float] = []
    for previous, current in zip(window, window[1:]):
        if previous["price"] <= 0 or current["price"] <= 0:
            continue
        returns.append(math.log(current["price"] / previous["price"]))
    if len(returns) < 2:
        return None
    return round(statistics.pstdev(returns) * 10_000.0, 6)


def _velocity_bps_per_minute(
    *,
    samples: list[dict[str, float]],
    current_price: float,
) -> float | None:
    if len(samples) < 2 or current_price <= 0:
        return None
    first = samples[0]
    span_seconds = max(samples[-1]["ts"] - first["ts"], 0.0)
    if span_seconds <= 0 or first["price"] <= 0:
        return None
    bps = (current_price / first["price"] - 1.0) * 10_000.0
    return round(bps / (span_seconds / 60.0), 6)
