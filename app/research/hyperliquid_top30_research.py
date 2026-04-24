from __future__ import annotations

import argparse
import gzip
import json
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Callable

from app.hyperliquid.info_client import HyperliquidInfoClient
from app.live.errors import HyperliquidAPIError
from app.settings import load_config


INTERVAL_TO_MS = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

DEFAULT_HOLD_BARS = {
    "15m": 4,
    "30m": 4,
    "1h": 4,
    "2h": 4,
    "4h": 3,
    "1d": 3,
}

FUNDING_ZSCORE_PERIODS = {
    "15m": 288,
    "30m": 144,
    "1h": 72,
    "2h": 36,
    "4h": 18,
    "1d": 14,
}

PATTERN_TO_ARCHETYPE = {
    "trend_breakout": "trend",
    "trend_pullback": "trend",
    "ichimoku_continuation": "trend",
    "vwap_reclaim": "trend",
    "squeeze_breakout": "breakout",
    "ttm_squeeze_release": "breakout",
    "range_mean_reversion": "mean_reversion",
    "funding_reversion": "mean_reversion",
    "stoch_cci_reversion": "mean_reversion",
    "ema50_overextension_reversion": "mean_reversion",
}


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def _iso_from_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC).isoformat().replace("+00:00", "Z")


def _dt_to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _round_or_none(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _pearson(x_values: list[float], y_values: list[float]) -> float | None:
    size = min(len(x_values), len(y_values))
    if size < 3:
        return None
    mean_x = sum(x_values) / size
    mean_y = sum(y_values) / size
    cov = 0.0
    var_x = 0.0
    var_y = 0.0
    for x_value, y_value in zip(x_values, y_values, strict=False):
        dx = x_value - mean_x
        dy = y_value - mean_y
        cov += dx * dy
        var_x += dx * dx
        var_y += dy * dy
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _rolling_mean(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0:
        return result
    running_sum = 0.0
    window: deque[float] = deque()
    for index, value in enumerate(values):
        window.append(value)
        running_sum += value
        if len(window) > period:
            running_sum -= window.popleft()
        if len(window) == period:
            result[index] = running_sum / period
    return result


def _rolling_std(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 1:
        return result
    running_sum = 0.0
    running_sq_sum = 0.0
    window: deque[float] = deque()
    for index, value in enumerate(values):
        window.append(value)
        running_sum += value
        running_sq_sum += value * value
        if len(window) > period:
            removed = window.popleft()
            running_sum -= removed
            running_sq_sum -= removed * removed
        if len(window) == period:
            mean_value = running_sum / period
            variance = max(running_sq_sum / period - mean_value * mean_value, 0.0)
            result[index] = math.sqrt(variance)
    return result


def _rolling_zscore(values: list[float], period: int) -> list[float | None]:
    means = _rolling_mean(values, period)
    stds = _rolling_std(values, period)
    result: list[float | None] = [None] * len(values)
    for index, value in enumerate(values):
        mean_value = means[index]
        std_value = stds[index]
        if mean_value is None or std_value is None or std_value <= 0:
            continue
        result[index] = (value - mean_value) / std_value
    return result


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    multiplier = 2.0 / (period + 1.0)
    previous = seed
    for index in range(period, len(values)):
        previous = (values[index] - previous) * multiplier + previous
        result[index] = previous
    return result


def _rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return result
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        delta = values[index] - values[index - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs_value = avg_gain / avg_loss
        result[period] = 100.0 - (100.0 / (1.0 + rs_value))
    for index in range(period + 1, len(values)):
        delta = values[index] - values[index - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        if avg_loss == 0:
            result[index] = 100.0
            continue
        rs_value = avg_gain / avg_loss
        result[index] = 100.0 - (100.0 / (1.0 + rs_value))
    return result


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) <= period:
        return result
    true_ranges: list[float] = [highs[0] - lows[0]]
    for index in range(1, len(closes)):
        true_range = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        true_ranges.append(true_range)
    atr_seed = sum(true_ranges[1 : period + 1]) / period
    result[period] = atr_seed
    previous = atr_seed
    for index in range(period + 1, len(closes)):
        previous = ((previous * (period - 1)) + true_ranges[index]) / period
        result[index] = previous
    return result


def _adx(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) <= (period * 2):
        return result

    true_ranges: list[float] = [0.0]
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for index in range(1, len(closes)):
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )

    tr_sum = sum(true_ranges[1 : period + 1])
    plus_dm_sum = sum(plus_dm[1 : period + 1])
    minus_dm_sum = sum(minus_dm[1 : period + 1])
    dx_values: list[float | None] = [None] * len(closes)

    for index in range(period, len(closes)):
        if index > period:
            tr_sum = tr_sum - (tr_sum / period) + true_ranges[index]
            plus_dm_sum = plus_dm_sum - (plus_dm_sum / period) + plus_dm[index]
            minus_dm_sum = minus_dm_sum - (minus_dm_sum / period) + minus_dm[index]
        if tr_sum <= 0:
            continue
        plus_di = 100.0 * plus_dm_sum / tr_sum
        minus_di = 100.0 * minus_dm_sum / tr_sum
        denominator = plus_di + minus_di
        if denominator <= 0:
            dx_values[index] = 0.0
            continue
        dx_values[index] = 100.0 * abs(plus_di - minus_di) / denominator

    adx_seed_values = [value for value in dx_values[period : (period * 2)] if value is not None]
    if len(adx_seed_values) < period:
        return result
    adx_value = sum(adx_seed_values) / period
    seed_index = (period * 2) - 1
    result[seed_index] = adx_value
    for index in range(seed_index + 1, len(closes)):
        dx_value = dx_values[index]
        if dx_value is None:
            continue
        adx_value = ((adx_value * (period - 1)) + dx_value) / period
        result[index] = adx_value
    return result


def _bollinger(values: list[float], period: int, std_multiplier: float = 2.0) -> tuple[list[float | None], list[float | None], list[float | None], list[float | None]]:
    middle = _rolling_mean(values, period)
    stds = _rolling_std(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    width: list[float | None] = [None] * len(values)
    for index in range(len(values)):
        middle_value = middle[index]
        std_value = stds[index]
        if middle_value is None or std_value is None:
            continue
        upper[index] = middle_value + std_multiplier * std_value
        lower[index] = middle_value - std_multiplier * std_value
        if middle_value != 0:
            width[index] = (upper[index] - lower[index]) / middle_value
    return upper, middle, lower, width


def _rolling_percentile(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = [value for value in values[index - period + 1 : index + 1] if value is not None]
        current = values[index]
        if len(window) < period or current is None:
            continue
        count = sum(1 for value in window if value <= current)
        result[index] = count / len(window)
    return result


def _donchian_previous(highs: list[float], lows: list[float], period: int) -> tuple[list[float | None], list[float | None]]:
    high_result: list[float | None] = [None] * len(highs)
    low_result: list[float | None] = [None] * len(lows)
    for index in range(period, len(highs)):
        high_result[index] = max(highs[index - period : index])
        low_result[index] = min(lows[index - period : index])
    return high_result, low_result


def _linreg_slope(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 1:
        return result
    x_mean = (period - 1) / 2.0
    denom = sum((index - x_mean) ** 2 for index in range(period))
    if denom <= 0:
        return result
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        y_mean = sum(window) / period
        numerator = 0.0
        for x_index, y_value in enumerate(window):
            numerator += (x_index - x_mean) * (y_value - y_mean)
        result[index] = numerator / denom
    return result


def _rolling_max(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0:
        return result
    for index in range(period - 1, len(values)):
        result[index] = max(values[index - period + 1 : index + 1])
    return result


def _rolling_min(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0:
        return result
    for index in range(period - 1, len(values)):
        result[index] = min(values[index - period + 1 : index + 1])
    return result


def _stoch_rsi(rsi_values: list[float | None], period: int = 14, smooth_k: int = 3, smooth_d: int = 3) -> tuple[list[float | None], list[float | None]]:
    raw: list[float | None] = [None] * len(rsi_values)
    for index in range(period - 1, len(rsi_values)):
        window = [value for value in rsi_values[index - period + 1 : index + 1] if value is not None]
        current = rsi_values[index]
        if len(window) < period or current is None:
            continue
        low_value = min(window)
        high_value = max(window)
        if high_value <= low_value:
            raw[index] = 0.5
            continue
        raw[index] = (current - low_value) / (high_value - low_value)

    smooth_source = [value if value is not None else 0.0 for value in raw]
    smooth_k_values = _rolling_mean(smooth_source, smooth_k)
    smooth_d_values = _rolling_mean(
        [value if value is not None else 0.0 for value in smooth_k_values],
        smooth_d,
    )

    k_values: list[float | None] = [None] * len(rsi_values)
    d_values: list[float | None] = [None] * len(rsi_values)
    for index in range(len(rsi_values)):
        if raw[index] is not None and smooth_k_values[index] is not None:
            k_values[index] = smooth_k_values[index]
        if k_values[index] is not None and smooth_d_values[index] is not None:
            d_values[index] = smooth_d_values[index]
    return k_values, d_values


def _rolling_vwap(highs: list[float], lows: list[float], closes: list[float], volumes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if period <= 0:
        return result
    pv_window: deque[float] = deque()
    volume_window: deque[float] = deque()
    pv_sum = 0.0
    volume_sum = 0.0
    for index in range(len(closes)):
        typical_price = (highs[index] + lows[index] + closes[index]) / 3.0
        pv = typical_price * volumes[index]
        pv_window.append(pv)
        volume_window.append(volumes[index])
        pv_sum += pv
        volume_sum += volumes[index]
        if len(pv_window) > period:
            pv_sum -= pv_window.popleft()
            volume_sum -= volume_window.popleft()
        if len(pv_window) == period and volume_sum > 0:
            result[index] = pv_sum / volume_sum
    return result


def _cci(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    typical_prices = [(highs[index] + lows[index] + closes[index]) / 3.0 for index in range(len(closes))]
    sma = _rolling_mean(typical_prices, period)
    result: list[float | None] = [None] * len(closes)
    if period <= 0:
        return result
    for index in range(period - 1, len(closes)):
        mean_value = sma[index]
        if mean_value is None:
            continue
        window = typical_prices[index - period + 1 : index + 1]
        mean_deviation = sum(abs(value - mean_value) for value in window) / period
        if mean_deviation <= 0:
            continue
        result[index] = (typical_prices[index] - mean_value) / (0.015 * mean_deviation)
    return result


def _keltner_channels(highs: list[float], lows: list[float], closes: list[float], period: int = 20, atr_multiplier: float = 1.5) -> tuple[list[float | None], list[float | None], list[float | None]]:
    basis = _ema(closes, period)
    atr_values = _atr(highs, lows, closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for index in range(len(closes)):
        basis_value = basis[index]
        atr_value = atr_values[index]
        if basis_value is None or atr_value is None:
            continue
        upper[index] = basis_value + atr_value * atr_multiplier
        lower[index] = basis_value - atr_value * atr_multiplier
    return upper, basis, lower


def _obv(closes: list[float], volumes: list[float]) -> list[float]:
    result: list[float] = [0.0] * len(closes)
    for index in range(1, len(closes)):
        if closes[index] > closes[index - 1]:
            result[index] = result[index - 1] + volumes[index]
        elif closes[index] < closes[index - 1]:
            result[index] = result[index - 1] - volumes[index]
        else:
            result[index] = result[index - 1]
    return result


def _mfi(highs: list[float], lows: list[float], closes: list[float], volumes: list[float], period: int = 14) -> list[float | None]:
    typical_prices = [(highs[index] + lows[index] + closes[index]) / 3.0 for index in range(len(closes))]
    positive_flow = [0.0] * len(closes)
    negative_flow = [0.0] * len(closes)
    for index in range(1, len(closes)):
        money_flow = typical_prices[index] * volumes[index]
        if typical_prices[index] > typical_prices[index - 1]:
            positive_flow[index] = money_flow
        elif typical_prices[index] < typical_prices[index - 1]:
            negative_flow[index] = money_flow
    result: list[float | None] = [None] * len(closes)
    for index in range(period, len(closes)):
        positive_sum = sum(positive_flow[index - period + 1 : index + 1])
        negative_sum = sum(negative_flow[index - period + 1 : index + 1])
        if negative_sum <= 0:
            result[index] = 100.0 if positive_sum > 0 else 50.0
            continue
        money_ratio = positive_sum / negative_sum
        result[index] = 100.0 - (100.0 / (1.0 + money_ratio))
    return result


def _ichimoku(highs: list[float], lows: list[float], closes: list[float]) -> dict[str, list[float | None]]:
    tenkan_high = _rolling_max(highs, 9)
    tenkan_low = _rolling_min(lows, 9)
    kijun_high = _rolling_max(highs, 26)
    kijun_low = _rolling_min(lows, 26)
    span_b_high = _rolling_max(highs, 52)
    span_b_low = _rolling_min(lows, 52)
    tenkan: list[float | None] = [None] * len(closes)
    kijun: list[float | None] = [None] * len(closes)
    span_a: list[float | None] = [None] * len(closes)
    span_b: list[float | None] = [None] * len(closes)
    cloud_top: list[float | None] = [None] * len(closes)
    cloud_bottom: list[float | None] = [None] * len(closes)
    chikou_delta_26: list[float | None] = [None] * len(closes)
    for index in range(len(closes)):
        if tenkan_high[index] is not None and tenkan_low[index] is not None:
            tenkan[index] = (tenkan_high[index] + tenkan_low[index]) / 2.0
        if kijun_high[index] is not None and kijun_low[index] is not None:
            kijun[index] = (kijun_high[index] + kijun_low[index]) / 2.0
        if tenkan[index] is not None and kijun[index] is not None:
            span_a[index] = (tenkan[index] + kijun[index]) / 2.0
        if span_b_high[index] is not None and span_b_low[index] is not None:
            span_b[index] = (span_b_high[index] + span_b_low[index]) / 2.0
        if span_a[index] is not None and span_b[index] is not None:
            cloud_top[index] = max(span_a[index], span_b[index])
            cloud_bottom[index] = min(span_a[index], span_b[index])
        if index >= 26:
            chikou_delta_26[index] = closes[index] - closes[index - 26]
    return {
        "ichimoku_tenkan": tenkan,
        "ichimoku_kijun": kijun,
        "ichimoku_span_a": span_a,
        "ichimoku_span_b": span_b,
        "ichimoku_cloud_top": cloud_top,
        "ichimoku_cloud_bottom": cloud_bottom,
        "ichimoku_chikou_delta_26": chikou_delta_26,
    }


def _supertrend(highs: list[float], lows: list[float], closes: list[float], period: int = 10, multiplier: float = 3.0) -> tuple[list[float | None], list[float | None]]:
    atr_values = _atr(highs, lows, closes, period)
    final_upper: list[float | None] = [None] * len(closes)
    final_lower: list[float | None] = [None] * len(closes)
    supertrend: list[float | None] = [None] * len(closes)
    direction: list[float | None] = [None] * len(closes)

    for index in range(len(closes)):
        atr_value = atr_values[index]
        if atr_value is None:
            continue
        hl2 = (highs[index] + lows[index]) / 2.0
        basic_upper = hl2 + multiplier * atr_value
        basic_lower = hl2 - multiplier * atr_value
        if index == 0 or final_upper[index - 1] is None or final_lower[index - 1] is None:
            final_upper[index] = basic_upper
            final_lower[index] = basic_lower
            supertrend[index] = basic_lower if closes[index] >= basic_lower else basic_upper
            direction[index] = 1.0 if closes[index] >= supertrend[index] else -1.0
            continue
        previous_upper = final_upper[index - 1]
        previous_lower = final_lower[index - 1]
        previous_close = closes[index - 1]
        previous_supertrend = supertrend[index - 1]

        if basic_upper < previous_upper or previous_close > previous_upper:
            final_upper[index] = basic_upper
        else:
            final_upper[index] = previous_upper

        if basic_lower > previous_lower or previous_close < previous_lower:
            final_lower[index] = basic_lower
        else:
            final_lower[index] = previous_lower

        if previous_supertrend == previous_upper:
            supertrend[index] = final_upper[index] if closes[index] <= final_upper[index] else final_lower[index]
        else:
            supertrend[index] = final_lower[index] if closes[index] >= final_lower[index] else final_upper[index]
        direction[index] = 1.0 if closes[index] > supertrend[index] else -1.0

    return supertrend, direction


@dataclass(slots=True)
class RankedSymbol:
    rank: int
    symbol: str
    day_ntl_vlm: float
    open_interest: float
    open_interest_usd: float
    mark_px: float
    mid_px: float
    premium: float
    funding: float
    max_leverage: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class CandleRecord:
    start_time: int
    end_time: int
    interval: str
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class FundingRecord:
    symbol: str
    time: int
    funding_rate: float
    premium: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class StrategyResult:
    symbol: str
    interval: str
    pattern: str
    archetype: str
    sample_count: int
    long_count: int
    short_count: int
    hit_rate: float
    expectancy_gross_bps: float
    expectancy_net_bps: float
    profit_factor: float
    avg_winner_bps: float
    avg_loser_bps: float
    median_net_bps: float
    total_net_bps: float
    first_signal_time: str | None
    last_signal_time: str | None
    side_breakdown: dict[str, dict[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _trade_return_summary(values: list[float]) -> dict[str, object]:
    if not values:
        return {
            "sample_count": 0,
            "hit_rate": 0.0,
            "expectancy_net_bps": 0.0,
            "profit_factor": 0.0,
            "avg_winner_bps": 0.0,
            "avg_loser_bps": 0.0,
            "median_net_bps": 0.0,
            "total_net_bps": 0.0,
        }
    winners = [value for value in values if value >= 0]
    losers = [value for value in values if value < 0]
    gross_profit = sum(value for value in winners if value > 0)
    gross_loss = abs(sum(value for value in losers if value < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    return {
        "sample_count": len(values),
        "hit_rate": round(sum(1 for value in values if value > 0) / len(values), 4),
        "expectancy_net_bps": round(sum(values) / len(values), 4),
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else 999.0,
        "avg_winner_bps": round(sum(winners) / len(winners), 4) if winners else 0.0,
        "avg_loser_bps": round(sum(losers) / len(losers), 4) if losers else 0.0,
        "median_net_bps": round(median(values), 4),
        "total_net_bps": round(sum(values), 4),
    }


@dataclass(slots=True)
class AggregatePatternResult:
    interval: str
    pattern: str
    archetype: str
    sample_count: int
    active_symbols: int
    positive_symbols: int
    positive_symbol_fraction: float
    hit_rate: float
    expectancy_net_bps: float
    profit_factor: float
    recommendation: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class CorrelationPair:
    interval: str
    left_symbol: str
    right_symbol: str
    sample_count: int
    correlation: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class LeadLagPair:
    interval: str
    leader: str
    follower: str
    lag_bars: int
    sample_count: int
    lagged_correlation: float
    same_time_correlation: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class SymbolReport:
    symbol: str
    rank: int
    day_ntl_vlm: float
    open_interest_usd: float
    best_pattern: str | None
    best_interval: str | None
    best_expectancy_net_bps: float
    best_sample_count: int
    archetype: str | None
    recommended_owner: str
    btc_correlation_1h: float | None
    beta_to_btc_1h: float | None
    total_return_1h_bps: float | None
    total_return_2h_bps: float | None
    coverage_days: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ResearchResult:
    dataset_dir: str
    requested_start: str
    requested_end: str
    symbols: list[str]
    intervals: list[str]
    data_gaps: dict[str, dict[str, object]]
    aggregate_patterns: list[AggregatePatternResult]
    top_correlations: list[CorrelationPair]
    lead_lag_pairs: list[LeadLagPair]
    correlation_clusters: list[list[str]]
    symbol_reports: list[SymbolReport]
    final_recommendation: str
    recommendation_rationale: str

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_dir": self.dataset_dir,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "symbols": self.symbols,
            "intervals": self.intervals,
            "data_gaps": self.data_gaps,
            "aggregate_patterns": [item.to_dict() for item in self.aggregate_patterns],
            "top_correlations": [item.to_dict() for item in self.top_correlations],
            "lead_lag_pairs": [item.to_dict() for item in self.lead_lag_pairs],
            "correlation_clusters": self.correlation_clusters,
            "symbol_reports": [item.to_dict() for item in self.symbol_reports],
            "final_recommendation": self.final_recommendation,
            "recommendation_rationale": self.recommendation_rationale,
        }


class HyperliquidTop30DatasetBuilder:
    def __init__(
        self,
        *,
        config_path: str | Path = "config/trident.toml",
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        config = load_config(config_path)
        self.config_path = str(config_path)
        self.client = HyperliquidInfoClient(config.hyperliquid, sleep_fn=sleep_fn)

    def collect(
        self,
        *,
        output_dir: str | Path,
        days: int = 180,
        top_n: int = 30,
        intervals: list[str] | None = None,
    ) -> dict[str, object]:
        selected_intervals = intervals or ["15m", "30m", "1h", "2h"]
        requested_end = datetime.now(tz=UTC)
        requested_start = requested_end - timedelta(days=days)
        start_ms = _dt_to_ms(requested_start)
        end_ms = _dt_to_ms(requested_end)

        output_path = Path(output_dir)
        raw_dir = output_path / "raw"
        candles_dir = raw_dir / "candles"
        funding_dir = raw_dir / "funding"
        candles_dir.mkdir(parents=True, exist_ok=True)
        funding_dir.mkdir(parents=True, exist_ok=True)

        ranked_symbols = self._fetch_top_symbols(top_n=top_n)
        print(
            f"Selected {len(ranked_symbols)} symbols from current HL perp universe "
            f"using 24h notional volume ranking."
        )

        availability: dict[str, dict[str, object]] = {}
        for interval in selected_intervals:
            availability[interval] = {
                "requested_start": _iso_from_ms(start_ms),
                "requested_end": _iso_from_ms(end_ms),
                "symbols": {},
            }

        total_candle_jobs = len(ranked_symbols) * len(selected_intervals)
        candle_job_index = 0
        for ranked_symbol in ranked_symbols:
            for interval in selected_intervals:
                candle_job_index += 1
                print(f"[candles {candle_job_index}/{total_candle_jobs}] {ranked_symbol.symbol} {interval}")
                candles = self._fetch_candles(
                    symbol=ranked_symbol.symbol,
                    interval=interval,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
                interval_dir = candles_dir / interval
                interval_dir.mkdir(parents=True, exist_ok=True)
                self._write_gzip_json(
                    interval_dir / f"{ranked_symbol.symbol}.json.gz",
                    [item.to_dict() for item in candles],
                )
                coverage = self._coverage_dict(candles, interval, requested_start_ms=start_ms, requested_end_ms=end_ms)
                availability[interval]["symbols"][ranked_symbol.symbol] = coverage

        for index, ranked_symbol in enumerate(ranked_symbols, start=1):
            print(f"[funding {index}/{len(ranked_symbols)}] {ranked_symbol.symbol}")
            funding = self._fetch_funding_history(
                symbol=ranked_symbol.symbol,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            self._write_gzip_json(
                funding_dir / f"{ranked_symbol.symbol}.json.gz",
                [item.to_dict() for item in funding],
            )

        manifest = {
            "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "config_path": self.config_path,
            "dataset_dir": str(output_path),
            "requested_start": requested_start.isoformat().replace("+00:00", "Z"),
            "requested_end": requested_end.isoformat().replace("+00:00", "Z"),
            "top_n": top_n,
            "intervals": selected_intervals,
            "symbols": [item.symbol for item in ranked_symbols],
            "ranking": [item.to_dict() for item in ranked_symbols],
            "availability": availability,
            "notes": [
                "Ranking uses current 24h notional volume on the main Hyperliquid perp dex.",
                "candleSnapshot only exposes the most recent 5000 candles per interval.",
                "fundingHistory is paginated and collected across the requested window.",
            ],
        }
        self._write_json(output_path / "manifest.json", manifest)
        return manifest

    def _fetch_top_symbols(self, *, top_n: int) -> list[RankedSymbol]:
        payload = self.client.post_info({"type": "metaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) < 2:
            raise RuntimeError("Unexpected metaAndAssetCtxs payload")
        meta = payload[0]
        ctxs = payload[1]
        if not isinstance(meta, dict) or not isinstance(ctxs, list):
            raise RuntimeError("Unexpected metaAndAssetCtxs payload")
        universe = meta.get("universe", [])
        if not isinstance(universe, list):
            raise RuntimeError("Unexpected universe payload")

        ranked: list[RankedSymbol] = []
        for item, ctx in zip(universe, ctxs, strict=False):
            if not isinstance(item, dict) or not isinstance(ctx, dict):
                continue
            if bool(item.get("isDelisted", False)):
                continue
            symbol = str(item.get("name", "")).strip()
            if not symbol or ":" in symbol:
                continue
            mark_px = _safe_float(ctx.get("markPx"))
            day_ntl_vlm = _safe_float(ctx.get("dayNtlVlm"))
            open_interest = _safe_float(ctx.get("openInterest"))
            if mark_px <= 0 or day_ntl_vlm <= 0:
                continue
            ranked.append(
                RankedSymbol(
                    rank=0,
                    symbol=symbol,
                    day_ntl_vlm=day_ntl_vlm,
                    open_interest=open_interest,
                    open_interest_usd=open_interest * mark_px,
                    mark_px=mark_px,
                    mid_px=_safe_float(ctx.get("midPx"), mark_px),
                    premium=_safe_float(ctx.get("premium")),
                    funding=_safe_float(ctx.get("funding")),
                    max_leverage=_safe_float(item.get("maxLeverage")),
                )
            )
        ranked.sort(
            key=lambda item: (item.day_ntl_vlm, item.open_interest_usd, item.max_leverage),
            reverse=True,
        )
        selected = ranked[:top_n]
        for rank, item in enumerate(selected, start=1):
            item.rank = rank
        return selected

    def _fetch_candles(
        self,
        *,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[CandleRecord]:
        payload = self._post_info_resilient(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            },
            context=f"candles:{symbol}:{interval}",
            fallback=[],
        )
        if not isinstance(payload, list):
            return []
        candles: list[CandleRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            candles.append(
                CandleRecord(
                    start_time=int(item.get("t", 0)),
                    end_time=int(item.get("T", 0)),
                    interval=str(item.get("i", interval)),
                    symbol=str(item.get("s", symbol)),
                    open=_safe_float(item.get("o")),
                    high=_safe_float(item.get("h")),
                    low=_safe_float(item.get("l")),
                    close=_safe_float(item.get("c")),
                    volume=_safe_float(item.get("v")),
                    trade_count=int(_safe_float(item.get("n"))),
                )
            )
        candles.sort(key=lambda item: item.start_time)
        return candles

    def _fetch_funding_history(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> list[FundingRecord]:
        records: list[FundingRecord] = []
        cursor = start_ms
        seen_times: set[int] = set()
        while cursor <= end_ms:
            payload = self._post_info_resilient(
                {
                    "type": "fundingHistory",
                    "coin": symbol,
                    "startTime": cursor,
                    "endTime": end_ms,
                },
                context=f"funding:{symbol}",
                fallback=[],
            )
            if not isinstance(payload, list) or not payload:
                break
            batch_max_time = cursor
            for item in payload:
                if not isinstance(item, dict):
                    continue
                time_value = int(_safe_float(item.get("time")))
                if time_value <= 0 or time_value in seen_times:
                    continue
                seen_times.add(time_value)
                batch_max_time = max(batch_max_time, time_value)
                records.append(
                    FundingRecord(
                        symbol=str(item.get("coin", symbol)),
                        time=time_value,
                        funding_rate=_safe_float(item.get("fundingRate")),
                        premium=_safe_float(item.get("premium")),
                    )
                )
            if batch_max_time <= cursor:
                break
            cursor = batch_max_time + 1
            if len(payload) < 500:
                break
        records.sort(key=lambda item: item.time)
        return records

    def _coverage_dict(
        self,
        candles: list[CandleRecord],
        interval: str,
        *,
        requested_start_ms: int,
        requested_end_ms: int,
    ) -> dict[str, object]:
        if not candles:
            return {
                "available": False,
                "bar_count": 0,
                "interval": interval,
            }
        actual_start_ms = candles[0].start_time
        actual_end_ms = candles[-1].end_time
        interval_ms = INTERVAL_TO_MS[interval]
        expected_bars = max(1, math.floor((requested_end_ms - requested_start_ms) / interval_ms))
        actual_bars = len(candles)
        return {
            "available": True,
            "bar_count": actual_bars,
            "interval": interval,
            "actual_start": _iso_from_ms(actual_start_ms),
            "actual_end": _iso_from_ms(actual_end_ms),
            "coverage_days": round((actual_end_ms - actual_start_ms) / 86_400_000.0, 2),
            "coverage_ratio_vs_request": round(min(actual_bars / expected_bars, 1.0), 4),
            "full_requested_window": actual_start_ms <= requested_start_ms + interval_ms,
        }

    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _write_gzip_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def _post_info_resilient(
        self,
        payload: dict[str, object],
        *,
        context: str,
        fallback: object,
        outer_attempts: int = 3,
    ) -> object:
        for attempt in range(1, outer_attempts + 1):
            try:
                return self.client.post_info(payload, max_attempts=5, timeout=30.0)
            except HyperliquidAPIError as exc:
                if attempt >= outer_attempts:
                    print(f"warning: {context} failed after {outer_attempts} attempts: {exc}")
                    return fallback
                time.sleep(float(attempt))
        return fallback


class HyperliquidTop30Analyzer:
    def __init__(self, *, round_trip_cost_bps: float = 8.0) -> None:
        self.round_trip_cost_bps = round_trip_cost_bps

    def analyze(self, dataset_dir: str | Path) -> ResearchResult:
        dataset_path = Path(dataset_dir)
        manifest = json.loads((dataset_path / "manifest.json").read_text(encoding="utf-8"))
        symbols = [str(item) for item in manifest.get("symbols", [])]
        intervals = [str(item) for item in manifest.get("intervals", [])]
        ranking = {
            str(item["symbol"]): item
            for item in manifest.get("ranking", [])
            if isinstance(item, dict) and item.get("symbol")
        }

        candle_data: dict[str, dict[str, list[CandleRecord]]] = {}
        funding_data: dict[str, list[FundingRecord]] = {}
        for symbol in symbols:
            funding_data[symbol] = self._read_funding(dataset_path / "raw" / "funding" / f"{symbol}.json.gz")
        for interval in intervals:
            candle_data[interval] = {}
            for symbol in symbols:
                candle_data[interval][symbol] = self._read_candles(
                    dataset_path / "raw" / "candles" / interval / f"{symbol}.json.gz"
                )

        per_symbol_results: dict[str, list[StrategyResult]] = {symbol: [] for symbol in symbols}
        aggregate_results: list[AggregatePatternResult] = []
        for interval in intervals:
            interval_results: dict[str, list[StrategyResult]] = {}
            for symbol in symbols:
                symbol_results = self._analyze_symbol_interval(
                    symbol=symbol,
                    interval=interval,
                    candles=candle_data[interval][symbol],
                    funding=funding_data[symbol],
                )
                per_symbol_results[symbol].extend(symbol_results)
                interval_results[symbol] = symbol_results
            aggregate_results.extend(self._aggregate_interval_results(interval_results, interval))

        top_correlations = self._build_correlations(candle_data, symbols=symbols)
        lead_lag_pairs = self._build_lead_lag_pairs(candle_data, symbols=symbols)
        correlation_clusters = self._build_correlation_clusters(candle_data.get("1h", {}), symbols=symbols)
        symbol_reports = self._build_symbol_reports(
            ranking=ranking,
            symbols=symbols,
            candle_data=candle_data,
            per_symbol_results=per_symbol_results,
        )
        final_recommendation, rationale = self._final_recommendation(
            aggregate_results=aggregate_results,
            symbol_reports=symbol_reports,
        )
        return ResearchResult(
            dataset_dir=str(dataset_path),
            requested_start=str(manifest.get("requested_start", "")),
            requested_end=str(manifest.get("requested_end", "")),
            symbols=symbols,
            intervals=intervals,
            data_gaps=manifest.get("availability", {}),
            aggregate_patterns=aggregate_results,
            top_correlations=top_correlations,
            lead_lag_pairs=lead_lag_pairs,
            correlation_clusters=correlation_clusters,
            symbol_reports=symbol_reports,
            final_recommendation=final_recommendation,
            recommendation_rationale=rationale,
        )

    def _read_candles(self, path: Path) -> list[CandleRecord]:
        if not path.exists():
            return []
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        records: list[CandleRecord] = []
        if not isinstance(payload, list):
            return records
        for item in payload:
            if not isinstance(item, dict):
                continue
            records.append(
                CandleRecord(
                    start_time=int(item.get("start_time", 0)),
                    end_time=int(item.get("end_time", 0)),
                    interval=str(item.get("interval", "")),
                    symbol=str(item.get("symbol", "")),
                    open=_safe_float(item.get("open")),
                    high=_safe_float(item.get("high")),
                    low=_safe_float(item.get("low")),
                    close=_safe_float(item.get("close")),
                    volume=_safe_float(item.get("volume")),
                    trade_count=int(_safe_float(item.get("trade_count"))),
                )
            )
        records.sort(key=lambda item: item.start_time)
        return records

    def _read_funding(self, path: Path) -> list[FundingRecord]:
        if not path.exists():
            return []
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        records: list[FundingRecord] = []
        if not isinstance(payload, list):
            return records
        for item in payload:
            if not isinstance(item, dict):
                continue
            records.append(
                FundingRecord(
                    symbol=str(item.get("symbol", "")),
                    time=int(item.get("time", 0)),
                    funding_rate=_safe_float(item.get("funding_rate")),
                    premium=_safe_float(item.get("premium")),
                )
            )
        records.sort(key=lambda item: item.time)
        return records

    def _analyze_symbol_interval(
        self,
        *,
        symbol: str,
        interval: str,
        candles: list[CandleRecord],
        funding: list[FundingRecord],
    ) -> list[StrategyResult]:
        if len(candles) < 150:
            return []
        features = self._build_features(interval=interval, candles=candles, funding=funding)
        pattern_results = [
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="trend_breakout",
                signal_fn=self._trend_breakout_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="trend_pullback",
                signal_fn=self._trend_pullback_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="ichimoku_continuation",
                signal_fn=self._ichimoku_continuation_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="vwap_reclaim",
                signal_fn=self._vwap_reclaim_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="squeeze_breakout",
                signal_fn=self._squeeze_breakout_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="ttm_squeeze_release",
                signal_fn=self._ttm_squeeze_release_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="range_mean_reversion",
                signal_fn=self._range_mean_reversion_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="funding_reversion",
                signal_fn=self._funding_reversion_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="stoch_cci_reversion",
                signal_fn=self._stoch_cci_reversion_signal,
            ),
            self._evaluate_pattern(
                symbol=symbol,
                interval=interval,
                candles=candles,
                features=features,
                pattern="ema50_overextension_reversion",
                signal_fn=self._ema50_overextension_reversion_signal,
            ),
        ]
        return [item for item in pattern_results if item is not None]

    def _build_features(
        self,
        *,
        interval: str,
        candles: list[CandleRecord],
        funding: list[FundingRecord],
    ) -> dict[str, list[float | None] | list[float] | list[int]]:
        closes = [item.close for item in candles]
        highs = [item.high for item in candles]
        lows = [item.low for item in candles]
        opens = [item.open for item in candles]
        volumes = [item.volume for item in candles]
        timestamps = [item.end_time for item in candles]

        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)
        ema100 = _ema(closes, 100)
        rsi14 = _rsi(closes, 14)
        rsi21 = _rsi(closes, 21)
        macd_fast = _ema(closes, 12)
        macd_slow = _ema(closes, 26)
        macd_line: list[float | None] = [None] * len(closes)
        for index in range(len(closes)):
            fast = macd_fast[index]
            slow = macd_slow[index]
            if fast is None or slow is None:
                continue
            macd_line[index] = fast - slow
        macd_signal = _ema([value if value is not None else 0.0 for value in macd_line], 9)
        macd_hist: list[float | None] = [None] * len(closes)
        for index in range(len(closes)):
            line = macd_line[index]
            signal = macd_signal[index]
            if line is None or signal is None:
                continue
            macd_hist[index] = line - signal

        atr14 = _atr(highs, lows, closes, 14)
        adx14 = _adx(highs, lows, closes, 14)
        bb_upper, bb_middle, bb_lower, bb_width = _bollinger(closes, 20)
        bb_position: list[float | None] = [None] * len(closes)
        for index, close in enumerate(closes):
            upper_value = bb_upper[index]
            lower_value = bb_lower[index]
            if upper_value is None or lower_value is None or upper_value <= lower_value:
                continue
            bb_position[index] = (close - lower_value) / (upper_value - lower_value)
        bb_width_pct = _rolling_percentile(bb_width, 100)
        keltner_upper, keltner_basis, keltner_lower = _keltner_channels(highs, lows, closes, 20, 1.5)
        squeeze_on: list[float | None] = [None] * len(closes)
        for index in range(len(closes)):
            if (
                bb_upper[index] is None
                or bb_lower[index] is None
                or keltner_upper[index] is None
                or keltner_lower[index] is None
            ):
                continue
            squeeze_on[index] = 1.0 if bb_upper[index] < keltner_upper[index] and bb_lower[index] > keltner_lower[index] else 0.0
        donchian_high_20, donchian_low_20 = _donchian_previous(highs, lows, 20)
        volume_mean_20 = _rolling_mean(volumes, 20)
        volume_ratio_20: list[float | None] = [None] * len(volumes)
        for index, volume in enumerate(volumes):
            average = volume_mean_20[index]
            if average is None or average <= 0:
                continue
            volume_ratio_20[index] = volume / average
        rolling_vwap_20 = _rolling_vwap(highs, lows, closes, volumes, 20)
        vwap_distance_bps_20: list[float | None] = [None] * len(closes)
        for index, close in enumerate(closes):
            vwap_value = rolling_vwap_20[index]
            if vwap_value is None or vwap_value <= 0:
                continue
            vwap_distance_bps_20[index] = (close - vwap_value) / vwap_value * 10_000.0
        cci20 = _cci(highs, lows, closes, 20)
        stoch_rsi_k, stoch_rsi_d = _stoch_rsi(rsi14, 14, 3, 3)
        supertrend, supertrend_direction = _supertrend(highs, lows, closes, 10, 3.0)
        obv = _obv(closes, volumes)
        obv_slope_5 = _linreg_slope(obv, 5)
        mfi14 = _mfi(highs, lows, closes, volumes, 14)
        ichimoku = _ichimoku(highs, lows, closes)
        price_zscore_20 = _rolling_zscore(closes, 20)
        trend_slope_20 = _linreg_slope(closes, 20)
        recent_return_8: list[float | None] = [None] * len(closes)
        one_bar_return_bps: list[float | None] = [None] * len(closes)
        for index in range(1, len(closes)):
            if closes[index - 1] > 0:
                one_bar_return_bps[index] = (closes[index] - closes[index - 1]) / closes[index - 1] * 10_000.0
        for index in range(8, len(closes)):
            if closes[index - 8] > 0:
                recent_return_8[index] = (closes[index] - closes[index - 8]) / closes[index - 8] * 10_000.0
        ema50_distance_pct: list[float | None] = [None] * len(closes)
        ema50_distance_atr: list[float | None] = [None] * len(closes)
        for index, close in enumerate(closes):
            ema50_value = ema50[index]
            atr_value = atr14[index]
            if ema50_value is not None and ema50_value > 0:
                ema50_distance_pct[index] = (close - ema50_value) / ema50_value * 100.0
            if ema50_value is not None and atr_value is not None and atr_value > 0:
                ema50_distance_atr[index] = (close - ema50_value) / atr_value

        aligned_funding = self._align_funding(timestamps=timestamps, funding=funding, field="funding_rate")
        funding_zscore = _rolling_zscore(
            [value if value is not None else 0.0 for value in aligned_funding],
            FUNDING_ZSCORE_PERIODS.get(interval, 72),
        )

        return {
            "open": opens,
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": volumes,
            "timestamp": timestamps,
            "ema20": ema20,
            "ema50": ema50,
            "ema100": ema100,
            "rsi14": rsi14,
            "rsi21": rsi21,
            "macd_hist": macd_hist,
            "atr14": atr14,
            "adx14": adx14,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "bb_position": bb_position,
            "bb_width_pct": bb_width_pct,
            "keltner_upper": keltner_upper,
            "keltner_basis": keltner_basis,
            "keltner_lower": keltner_lower,
            "squeeze_on": squeeze_on,
            "donchian_high_20": donchian_high_20,
            "donchian_low_20": donchian_low_20,
            "volume_ratio_20": volume_ratio_20,
            "rolling_vwap_20": rolling_vwap_20,
            "vwap_distance_bps_20": vwap_distance_bps_20,
            "cci20": cci20,
            "stoch_rsi_k": stoch_rsi_k,
            "stoch_rsi_d": stoch_rsi_d,
            "supertrend": supertrend,
            "supertrend_direction": supertrend_direction,
            "obv": obv,
            "obv_slope_5": obv_slope_5,
            "mfi14": mfi14,
            "price_zscore_20": price_zscore_20,
            "trend_slope_20": trend_slope_20,
            "recent_return_8": recent_return_8,
            "one_bar_return_bps": one_bar_return_bps,
            "ema50_distance_pct": ema50_distance_pct,
            "ema50_distance_atr": ema50_distance_atr,
            "funding_rate": aligned_funding,
            "funding_zscore": funding_zscore,
            **ichimoku,
        }

    def _align_funding(
        self,
        *,
        timestamps: list[int],
        funding: list[FundingRecord],
        field: str,
    ) -> list[float | None]:
        if not funding:
            return [None] * len(timestamps)
        values: list[float | None] = [None] * len(timestamps)
        funding_index = 0
        current_value: float | None = None
        for index, timestamp in enumerate(timestamps):
            while funding_index < len(funding) and funding[funding_index].time <= timestamp:
                current_value = getattr(funding[funding_index], field)
                funding_index += 1
            values[index] = current_value
        return values

    def _evaluate_pattern(
        self,
        *,
        symbol: str,
        interval: str,
        candles: list[CandleRecord],
        features: dict[str, list[float | None] | list[float] | list[int]],
        pattern: str,
        signal_fn: Callable[[int, dict[str, list[float | None] | list[float] | list[int]]], str | None],
    ) -> StrategyResult | None:
        hold_bars = DEFAULT_HOLD_BARS[interval]
        trade_returns: list[float] = []
        long_returns: list[float] = []
        short_returns: list[float] = []
        winners: list[float] = []
        losers: list[float] = []
        next_allowed_index = 0
        long_count = 0
        short_count = 0
        first_signal_time: str | None = None
        last_signal_time: str | None = None

        closes = features["close"]
        timestamps = features["timestamp"]
        if not isinstance(closes, list) or not isinstance(timestamps, list):
            return None

        for index in range(120, len(candles) - hold_bars):
            if index < next_allowed_index:
                continue
            side = signal_fn(index, features)
            if side is None:
                continue
            entry_px = closes[index]
            exit_px = closes[index + hold_bars]
            if not isinstance(entry_px, float) or not isinstance(exit_px, float) or entry_px <= 0:
                continue
            aligned_return = (exit_px - entry_px) / entry_px * 10_000.0
            if side == "short":
                aligned_return = -aligned_return
                short_count += 1
            else:
                long_count += 1
            net_return = aligned_return - self.round_trip_cost_bps
            trade_returns.append(net_return)
            if side == "short":
                short_returns.append(net_return)
            else:
                long_returns.append(net_return)
            if net_return >= 0:
                winners.append(net_return)
            else:
                losers.append(net_return)
            timestamp = timestamps[index]
            if isinstance(timestamp, int):
                iso_time = _iso_from_ms(timestamp)
                if first_signal_time is None:
                    first_signal_time = iso_time
                last_signal_time = iso_time
            next_allowed_index = index + hold_bars

        if not trade_returns:
            return StrategyResult(
                symbol=symbol,
                interval=interval,
                pattern=pattern,
                archetype=PATTERN_TO_ARCHETYPE[pattern],
                sample_count=0,
                long_count=0,
                short_count=0,
                hit_rate=0.0,
                expectancy_gross_bps=0.0,
                expectancy_net_bps=0.0,
                profit_factor=0.0,
                avg_winner_bps=0.0,
                avg_loser_bps=0.0,
                median_net_bps=0.0,
                total_net_bps=0.0,
                first_signal_time=None,
                last_signal_time=None,
                side_breakdown={
                    "long": _trade_return_summary([]),
                    "short": _trade_return_summary([]),
                },
            )

        gross_expectancy = sum(value + self.round_trip_cost_bps for value in trade_returns) / len(trade_returns)
        gross_profit = sum(value for value in winners if value > 0)
        gross_loss = abs(sum(value for value in losers if value < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        return StrategyResult(
            symbol=symbol,
            interval=interval,
            pattern=pattern,
            archetype=PATTERN_TO_ARCHETYPE[pattern],
            sample_count=len(trade_returns),
            long_count=long_count,
            short_count=short_count,
            hit_rate=round(sum(1 for value in trade_returns if value > 0) / len(trade_returns), 4),
            expectancy_gross_bps=round(gross_expectancy, 4),
            expectancy_net_bps=round(sum(trade_returns) / len(trade_returns), 4),
            profit_factor=round(profit_factor, 4) if math.isfinite(profit_factor) else 999.0,
            avg_winner_bps=round(sum(winners) / len(winners), 4) if winners else 0.0,
            avg_loser_bps=round(sum(losers) / len(losers), 4) if losers else 0.0,
            median_net_bps=round(median(trade_returns), 4),
            total_net_bps=round(sum(trade_returns), 4),
            first_signal_time=first_signal_time,
            last_signal_time=last_signal_time,
            side_breakdown={
                "long": _trade_return_summary(long_returns),
                "short": _trade_return_summary(short_returns),
            },
        )

    def _trend_breakout_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        ema20 = self._value(features, "ema20", index)
        ema50 = self._value(features, "ema50", index)
        ema100 = self._value(features, "ema100", index)
        rsi14 = self._value(features, "rsi14", index)
        macd_hist = self._value(features, "macd_hist", index)
        macd_prev = self._value(features, "macd_hist", index - 1)
        adx14 = self._value(features, "adx14", index)
        volume_ratio = self._value(features, "volume_ratio_20", index)
        don_high = self._value(features, "donchian_high_20", index)
        don_low = self._value(features, "donchian_low_20", index)
        atr14 = self._value(features, "atr14", index)
        vwap_distance = self._value(features, "vwap_distance_bps_20", index)
        supertrend_direction = self._value(features, "supertrend_direction", index)
        cloud_top = self._value(features, "ichimoku_cloud_top", index)
        cloud_bottom = self._value(features, "ichimoku_cloud_bottom", index)
        stoch_k = self._value(features, "stoch_rsi_k", index)
        stoch_d = self._value(features, "stoch_rsi_d", index)
        if None in (
            close,
            ema20,
            ema50,
            ema100,
            rsi14,
            macd_hist,
            macd_prev,
            adx14,
            volume_ratio,
            atr14,
            vwap_distance,
            supertrend_direction,
            stoch_k,
            stoch_d,
        ):
            return None
        if atr14 <= 0 or close <= 0:
            return None
        extension = abs(close - ema20) / atr14
        if extension > 1.8:
            return None
        if (
            don_high is not None
            and close > don_high
            and close > ema20 > ema50 > ema100
            and (cloud_top is None or close > cloud_top)
            and 54 <= rsi14 <= 75
            and macd_hist > 0
            and macd_hist >= macd_prev
            and adx14 >= 20
            and volume_ratio >= 1.15
            and vwap_distance >= 0
            and supertrend_direction > 0
            and stoch_k >= max(stoch_d, 0.45)
            and stoch_k <= 0.95
        ):
            return "long"
        if (
            don_low is not None
            and close < don_low
            and close < ema20 < ema50 < ema100
            and (cloud_bottom is None or close < cloud_bottom)
            and 25 <= rsi14 <= 46
            and macd_hist < 0
            and macd_hist <= macd_prev
            and adx14 >= 20
            and volume_ratio >= 1.15
            and vwap_distance <= 0
            and supertrend_direction < 0
            and stoch_k <= min(stoch_d, 0.55)
            and stoch_k >= 0.05
        ):
            return "short"
        return None

    def _trend_pullback_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        low = self._value(features, "low", index)
        high = self._value(features, "high", index)
        ema20 = self._value(features, "ema20", index)
        ema50 = self._value(features, "ema50", index)
        ema100 = self._value(features, "ema100", index)
        rsi14 = self._value(features, "rsi14", index)
        macd_hist = self._value(features, "macd_hist", index)
        macd_prev = self._value(features, "macd_hist", index - 1)
        adx14 = self._value(features, "adx14", index)
        atr14 = self._value(features, "atr14", index)
        recent_return = self._value(features, "recent_return_8", index)
        vwap = self._value(features, "rolling_vwap_20", index)
        vwap_distance = self._value(features, "vwap_distance_bps_20", index)
        cci20 = self._value(features, "cci20", index)
        stoch_k = self._value(features, "stoch_rsi_k", index)
        stoch_d = self._value(features, "stoch_rsi_d", index)
        supertrend_direction = self._value(features, "supertrend_direction", index)
        if None in (
            close,
            low,
            high,
            ema20,
            ema50,
            ema100,
            rsi14,
            macd_hist,
            macd_prev,
            adx14,
            atr14,
            recent_return,
            vwap,
            vwap_distance,
            cci20,
            stoch_k,
            stoch_d,
            supertrend_direction,
        ):
            return None
        if atr14 <= 0:
            return None
        if (
            close > ema20 > ema50 > ema100
            and low <= ema20 * 1.003
            and close >= vwap
            and recent_return > 0
            and 42 <= rsi14 <= 62
            and macd_hist >= macd_prev
            and adx14 >= 18
            and abs(close - ema20) / atr14 <= 0.75
            and -35 <= cci20 <= 120
            and stoch_k >= stoch_d
            and vwap_distance >= -40
            and supertrend_direction > 0
        ):
            return "long"
        if (
            close < ema20 < ema50 < ema100
            and high >= ema20 * 0.997
            and close <= vwap
            and recent_return < 0
            and 38 <= rsi14 <= 58
            and macd_hist <= macd_prev
            and adx14 >= 18
            and abs(close - ema20) / atr14 <= 0.75
            and -120 <= cci20 <= 35
            and stoch_k <= stoch_d
            and vwap_distance <= 40
            and supertrend_direction < 0
        ):
            return "short"
        return None

    def _ichimoku_continuation_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        tenkan = self._value(features, "ichimoku_tenkan", index)
        kijun = self._value(features, "ichimoku_kijun", index)
        cloud_top = self._value(features, "ichimoku_cloud_top", index)
        cloud_bottom = self._value(features, "ichimoku_cloud_bottom", index)
        chikou_delta = self._value(features, "ichimoku_chikou_delta_26", index)
        supertrend_direction = self._value(features, "supertrend_direction", index)
        adx14 = self._value(features, "adx14", index)
        stoch_k = self._value(features, "stoch_rsi_k", index)
        stoch_d = self._value(features, "stoch_rsi_d", index)
        mfi14 = self._value(features, "mfi14", index)
        vwap_distance = self._value(features, "vwap_distance_bps_20", index)
        if None in (
            close,
            tenkan,
            kijun,
            cloud_top,
            cloud_bottom,
            chikou_delta,
            supertrend_direction,
            adx14,
            stoch_k,
            stoch_d,
            mfi14,
            vwap_distance,
        ):
            return None
        if (
            close > cloud_top
            and tenkan > kijun
            and chikou_delta > 0
            and supertrend_direction > 0
            and adx14 >= 18
            and 45 <= mfi14 <= 82
            and stoch_k >= stoch_d
            and 0.35 <= stoch_k <= 0.95
            and vwap_distance >= -20
        ):
            return "long"
        if (
            close < cloud_bottom
            and tenkan < kijun
            and chikou_delta < 0
            and supertrend_direction < 0
            and adx14 >= 18
            and 18 <= mfi14 <= 55
            and stoch_k <= stoch_d
            and 0.05 <= stoch_k <= 0.65
            and vwap_distance <= 20
        ):
            return "short"
        return None

    def _vwap_reclaim_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        previous_close = self._value(features, "close", index - 1)
        vwap = self._value(features, "rolling_vwap_20", index)
        previous_vwap = self._value(features, "rolling_vwap_20", index - 1)
        ema20 = self._value(features, "ema20", index)
        ema50 = self._value(features, "ema50", index)
        cci20 = self._value(features, "cci20", index)
        obv_slope = self._value(features, "obv_slope_5", index)
        supertrend_direction = self._value(features, "supertrend_direction", index)
        mfi14 = self._value(features, "mfi14", index)
        if None in (
            close,
            previous_close,
            vwap,
            previous_vwap,
            ema20,
            ema50,
            cci20,
            obv_slope,
            supertrend_direction,
            mfi14,
        ):
            return None
        if (
            previous_close < previous_vwap
            and close > vwap
            and close > ema20 > ema50
            and cci20 > 0
            and obv_slope > 0
            and supertrend_direction > 0
            and 45 <= mfi14 <= 80
        ):
            return "long"
        if (
            previous_close > previous_vwap
            and close < vwap
            and close < ema20 < ema50
            and cci20 < 0
            and obv_slope < 0
            and supertrend_direction < 0
            and 20 <= mfi14 <= 55
        ):
            return "short"
        return None

    def _squeeze_breakout_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        bb_upper = self._value(features, "bb_upper", index)
        bb_lower = self._value(features, "bb_lower", index)
        bb_width_pct = self._value(features, "bb_width_pct", index)
        adx14 = self._value(features, "adx14", index)
        adx_prev = self._value(features, "adx14", index - 1)
        volume_ratio = self._value(features, "volume_ratio_20", index)
        don_high = self._value(features, "donchian_high_20", index)
        don_low = self._value(features, "donchian_low_20", index)
        macd_hist = self._value(features, "macd_hist", index)
        supertrend_direction = self._value(features, "supertrend_direction", index)
        obv_slope = self._value(features, "obv_slope_5", index)
        mfi14 = self._value(features, "mfi14", index)
        previous_squeeze = self._value(features, "squeeze_on", index - 1)
        if None in (
            close,
            bb_upper,
            bb_lower,
            bb_width_pct,
            adx14,
            adx_prev,
            volume_ratio,
            macd_hist,
            supertrend_direction,
            obv_slope,
            mfi14,
            previous_squeeze,
        ):
            return None
        if (
            bb_width_pct <= 0.25
            and previous_squeeze >= 0.5
            and close > bb_upper
            and (don_high is None or close > don_high)
            and volume_ratio >= 1.4
            and macd_hist > 0
            and adx14 >= max(adx_prev, 16)
            and supertrend_direction > 0
            and obv_slope > 0
            and mfi14 >= 52
        ):
            return "long"
        if (
            bb_width_pct <= 0.25
            and previous_squeeze >= 0.5
            and close < bb_lower
            and (don_low is None or close < don_low)
            and volume_ratio >= 1.4
            and macd_hist < 0
            and adx14 >= max(adx_prev, 16)
            and supertrend_direction < 0
            and obv_slope < 0
            and mfi14 <= 48
        ):
            return "short"
        return None

    def _ttm_squeeze_release_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        previous_close = self._value(features, "close", index - 1)
        squeeze_now = self._value(features, "squeeze_on", index)
        squeeze_prev1 = self._value(features, "squeeze_on", index - 1)
        squeeze_prev2 = self._value(features, "squeeze_on", index - 2)
        keltner_upper = self._value(features, "keltner_upper", index)
        keltner_lower = self._value(features, "keltner_lower", index)
        macd_hist = self._value(features, "macd_hist", index)
        obv_slope = self._value(features, "obv_slope_5", index)
        mfi14 = self._value(features, "mfi14", index)
        adx14 = self._value(features, "adx14", index)
        if None in (
            close,
            previous_close,
            squeeze_now,
            squeeze_prev1,
            squeeze_prev2,
            keltner_upper,
            keltner_lower,
            macd_hist,
            obv_slope,
            mfi14,
            adx14,
        ):
            return None
        squeeze_loaded = squeeze_prev1 >= 0.5 and squeeze_prev2 >= 0.5 and squeeze_now < 0.5
        if (
            squeeze_loaded
            and close > keltner_upper
            and close > previous_close
            and macd_hist > 0
            and obv_slope > 0
            and mfi14 >= 55
            and adx14 >= 16
        ):
            return "long"
        if (
            squeeze_loaded
            and close < keltner_lower
            and close < previous_close
            and macd_hist < 0
            and obv_slope < 0
            and mfi14 <= 45
            and adx14 >= 16
        ):
            return "short"
        return None

    def _range_mean_reversion_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        previous_close = self._value(features, "close", index - 1)
        bb_upper = self._value(features, "bb_upper", index)
        bb_lower = self._value(features, "bb_lower", index)
        previous_upper = self._value(features, "bb_upper", index - 1)
        previous_lower = self._value(features, "bb_lower", index - 1)
        rsi14 = self._value(features, "rsi14", index)
        adx14 = self._value(features, "adx14", index)
        zscore = self._value(features, "price_zscore_20", index)
        stoch_k = self._value(features, "stoch_rsi_k", index)
        stoch_d = self._value(features, "stoch_rsi_d", index)
        cci20 = self._value(features, "cci20", index)
        vwap = self._value(features, "rolling_vwap_20", index)
        if None in (
            close,
            previous_close,
            bb_upper,
            bb_lower,
            previous_upper,
            previous_lower,
            rsi14,
            adx14,
            zscore,
            stoch_k,
            stoch_d,
            cci20,
            vwap,
        ):
            return None
        if adx14 > 18:
            return None
        if (
            previous_close < previous_lower
            and close >= bb_lower
            and close <= vwap * 1.01
            and zscore <= -1.2
            and rsi14 <= 45
            and cci20 <= -90
            and stoch_k >= stoch_d
            and stoch_k <= 0.35
        ):
            return "long"
        if (
            previous_close > previous_upper
            and close <= bb_upper
            and close >= vwap * 0.99
            and zscore >= 1.2
            and rsi14 >= 55
            and cci20 >= 90
            and stoch_k <= stoch_d
            and stoch_k >= 0.65
        ):
            return "short"
        return None

    def _funding_reversion_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        funding_z = self._value(features, "funding_zscore", index)
        close = self._value(features, "close", index)
        bb_upper = self._value(features, "bb_upper", index)
        bb_lower = self._value(features, "bb_lower", index)
        rsi14 = self._value(features, "rsi14", index)
        adx14 = self._value(features, "adx14", index)
        stoch_k = self._value(features, "stoch_rsi_k", index)
        cci20 = self._value(features, "cci20", index)
        mfi14 = self._value(features, "mfi14", index)
        if None in (funding_z, close, bb_upper, bb_lower, rsi14, adx14, stoch_k, cci20, mfi14):
            return None
        if abs(funding_z) < 1.5 or adx14 > 28:
            return None
        if (
            funding_z <= -1.5
            and close <= bb_lower
            and rsi14 <= 42
            and stoch_k <= 0.25
            and cci20 <= -90
            and mfi14 <= 42
        ):
            return "long"
        if (
            funding_z >= 1.5
            and close >= bb_upper
            and rsi14 >= 58
            and stoch_k >= 0.75
            and cci20 >= 90
            and mfi14 >= 58
        ):
            return "short"
        return None

    def _stoch_cci_reversion_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        bb_upper = self._value(features, "bb_upper", index)
        bb_lower = self._value(features, "bb_lower", index)
        stoch_k = self._value(features, "stoch_rsi_k", index)
        stoch_d = self._value(features, "stoch_rsi_d", index)
        cci20 = self._value(features, "cci20", index)
        mfi14 = self._value(features, "mfi14", index)
        adx14 = self._value(features, "adx14", index)
        vwap = self._value(features, "rolling_vwap_20", index)
        if None in (close, bb_upper, bb_lower, stoch_k, stoch_d, cci20, mfi14, adx14, vwap):
            return None
        if adx14 > 20:
            return None
        if (
            close <= bb_lower
            and close <= vwap
            and stoch_k >= stoch_d
            and stoch_k <= 0.25
            and cci20 <= -100
            and mfi14 <= 35
        ):
            return "long"
        if (
            close >= bb_upper
            and close >= vwap
            and stoch_k <= stoch_d
            and stoch_k >= 0.75
            and cci20 >= 100
            and mfi14 >= 65
        ):
            return "short"
        return None

    def _ema50_overextension_reversion_signal(
        self,
        index: int,
        features: dict[str, list[float | None] | list[float] | list[int]],
    ) -> str | None:
        close = self._value(features, "close", index)
        ema50_distance_pct = self._value(features, "ema50_distance_pct", index)
        ema50_distance_atr = self._value(features, "ema50_distance_atr", index)
        rsi21 = self._value(features, "rsi21", index)
        macd_hist = self._value(features, "macd_hist", index)
        macd_prev = self._value(features, "macd_hist", index - 1)
        bb_position = self._value(features, "bb_position", index)
        stoch_k = self._value(features, "stoch_rsi_k", index)
        cci20 = self._value(features, "cci20", index)
        if None in (
            close,
            ema50_distance_pct,
            ema50_distance_atr,
            rsi21,
            macd_hist,
            macd_prev,
            bb_position,
            stoch_k,
            cci20,
        ):
            return None
        if close <= 0:
            return None
        if (
            ema50_distance_pct >= 4.0
            and ema50_distance_atr >= 2.0
            and rsi21 >= 65.0
            and (macd_hist <= macd_prev or bb_position >= 0.90)
            and (stoch_k >= 0.75 or cci20 >= 100.0)
        ):
            return "short"
        if (
            ema50_distance_pct <= -4.0
            and ema50_distance_atr <= -2.0
            and rsi21 <= 35.0
            and (macd_hist >= macd_prev or bb_position <= 0.10)
            and (stoch_k <= 0.25 or cci20 <= -100.0)
        ):
            return "long"
        return None

    def _value(
        self,
        features: dict[str, list[float | None] | list[float] | list[int]],
        key: str,
        index: int,
    ) -> float | None:
        series = features.get(key)
        if not isinstance(series, list) or index < 0 or index >= len(series):
            return None
        value = series[index]
        if isinstance(value, int):
            return float(value)
        return value

    def _aggregate_interval_results(
        self,
        interval_results: dict[str, list[StrategyResult]],
        interval: str,
    ) -> list[AggregatePatternResult]:
        by_pattern: dict[str, list[StrategyResult]] = {}
        for results in interval_results.values():
            for result in results:
                by_pattern.setdefault(result.pattern, []).append(result)
        aggregates: list[AggregatePatternResult] = []
        for pattern, results in by_pattern.items():
            active = [item for item in results if item.sample_count >= 5]
            if not active:
                aggregates.append(
                    AggregatePatternResult(
                        interval=interval,
                        pattern=pattern,
                        archetype=PATTERN_TO_ARCHETYPE[pattern],
                        sample_count=0,
                        active_symbols=0,
                        positive_symbols=0,
                        positive_symbol_fraction=0.0,
                        hit_rate=0.0,
                        expectancy_net_bps=0.0,
                        profit_factor=0.0,
                        recommendation="kill",
                    )
                )
                continue
            trade_sample_count = sum(item.sample_count for item in active)
            positive = sum(1 for item in active if item.expectancy_net_bps > 0)
            weighted_hit_numerator = sum(item.hit_rate * item.sample_count for item in active)
            weighted_expectancy_numerator = sum(item.expectancy_net_bps * item.sample_count for item in active)
            gross_profit = sum(
                max(item.avg_winner_bps, 0.0) * max(item.hit_rate, 0.0) * item.sample_count
                for item in active
            )
            gross_loss = sum(
                abs(min(item.avg_loser_bps, 0.0)) * max(1.0 - item.hit_rate, 0.0) * item.sample_count
                for item in active
            )
            positive_fraction = positive / len(active)
            expectancy = weighted_expectancy_numerator / trade_sample_count if trade_sample_count else 0.0
            hit_rate = weighted_hit_numerator / trade_sample_count if trade_sample_count else 0.0
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
            recommendation = "kill"
            if trade_sample_count < 40 or len(active) < 5:
                recommendation = "park"
            elif expectancy > 0 and positive_fraction >= 0.55 and hit_rate >= 0.48:
                recommendation = "go"
            elif expectancy >= 0 and positive_fraction >= 0.45:
                recommendation = "park"
            aggregates.append(
                AggregatePatternResult(
                    interval=interval,
                    pattern=pattern,
                    archetype=PATTERN_TO_ARCHETYPE[pattern],
                    sample_count=trade_sample_count,
                    active_symbols=len(active),
                    positive_symbols=positive,
                    positive_symbol_fraction=round(positive_fraction, 4),
                    hit_rate=round(hit_rate, 4),
                    expectancy_net_bps=round(expectancy, 4),
                    profit_factor=round(profit_factor, 4) if math.isfinite(profit_factor) else 999.0,
                    recommendation=recommendation,
                )
            )
        aggregates.sort(
            key=lambda item: (item.recommendation == "go", item.expectancy_net_bps, item.sample_count),
            reverse=True,
        )
        return aggregates

    def _build_correlations(
        self,
        candle_data: dict[str, dict[str, list[CandleRecord]]],
        *,
        symbols: list[str],
    ) -> list[CorrelationPair]:
        pairs: list[CorrelationPair] = []
        for interval in ("30m", "1h", "2h"):
            interval_data = candle_data.get(interval, {})
            return_maps = {
                symbol: self._returns_by_timestamp(interval_data.get(symbol, []))
                for symbol in symbols
            }
            for left_index, left_symbol in enumerate(symbols):
                for right_symbol in symbols[left_index + 1 :]:
                    left_returns = return_maps[left_symbol]
                    right_returns = return_maps[right_symbol]
                    common_times = sorted(set(left_returns) & set(right_returns))
                    if len(common_times) < 100:
                        continue
                    x_values = [left_returns[item] for item in common_times]
                    y_values = [right_returns[item] for item in common_times]
                    correlation = _pearson(x_values, y_values)
                    if correlation is None:
                        continue
                    pairs.append(
                        CorrelationPair(
                            interval=interval,
                            left_symbol=left_symbol,
                            right_symbol=right_symbol,
                            sample_count=len(common_times),
                            correlation=round(correlation, 4),
                        )
                    )
        pairs.sort(key=lambda item: abs(item.correlation), reverse=True)
        return pairs[:20]

    def _build_lead_lag_pairs(
        self,
        candle_data: dict[str, dict[str, list[CandleRecord]]],
        *,
        symbols: list[str],
    ) -> list[LeadLagPair]:
        if not symbols:
            return []
        leaders = symbols[: min(5, len(symbols))]
        pairs: list[LeadLagPair] = []
        for interval in ("30m", "1h"):
            interval_ms = INTERVAL_TO_MS.get(interval)
            if interval_ms is None:
                continue
            interval_data = candle_data.get(interval, {})
            return_maps = {
                symbol: self._returns_by_timestamp(interval_data.get(symbol, []))
                for symbol in symbols
            }
            for leader in leaders:
                for follower in symbols:
                    if follower == leader:
                        continue
                    same_time_corr = self._pair_correlation(return_maps[leader], return_maps[follower], lag_ms=0)
                    if same_time_corr is None:
                        continue
                    for lag_bars in range(1, 5):
                        lagged = self._pair_correlation(
                            return_maps[leader],
                            return_maps[follower],
                            lag_ms=lag_bars * interval_ms,
                        )
                        if lagged is None:
                            continue
                        if lagged["sample_count"] < 100:
                            continue
                        if lagged["correlation"] >= 0.2 and lagged["correlation"] > same_time_corr["correlation"] + 0.03:
                            pairs.append(
                                LeadLagPair(
                                    interval=interval,
                                    leader=leader,
                                    follower=follower,
                                    lag_bars=lag_bars,
                                    sample_count=lagged["sample_count"],
                                    lagged_correlation=round(lagged["correlation"], 4),
                                    same_time_correlation=round(same_time_corr["correlation"], 4),
                                )
                            )
        pairs.sort(key=lambda item: item.lagged_correlation - item.same_time_correlation, reverse=True)
        return pairs[:20]

    def _build_correlation_clusters(
        self,
        candles_by_symbol: dict[str, list[CandleRecord]],
        *,
        symbols: list[str],
    ) -> list[list[str]]:
        adjacency: dict[str, set[str]] = {symbol: set() for symbol in symbols}
        return_maps = {symbol: self._returns_by_timestamp(candles_by_symbol.get(symbol, [])) for symbol in symbols}
        for left_index, left_symbol in enumerate(symbols):
            for right_symbol in symbols[left_index + 1 :]:
                result = self._pair_correlation(return_maps[left_symbol], return_maps[right_symbol], lag_ms=0)
                if result is None or result["sample_count"] < 100:
                    continue
                if result["correlation"] >= 0.65:
                    adjacency[left_symbol].add(right_symbol)
                    adjacency[right_symbol].add(left_symbol)
        visited: set[str] = set()
        clusters: list[list[str]] = []
        for symbol in symbols:
            if symbol in visited:
                continue
            stack = [symbol]
            cluster: list[str] = []
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                cluster.append(current)
                stack.extend(neighbor for neighbor in adjacency[current] if neighbor not in visited)
            if len(cluster) >= 3:
                clusters.append(sorted(cluster))
        clusters.sort(key=len, reverse=True)
        return clusters[:10]

    def _returns_by_timestamp(self, candles: list[CandleRecord]) -> dict[int, float]:
        result: dict[int, float] = {}
        for index in range(1, len(candles)):
            previous = candles[index - 1].close
            current = candles[index].close
            if previous <= 0:
                continue
            result[candles[index].end_time] = (current - previous) / previous
        return result

    def _pair_correlation(
        self,
        left_returns: dict[int, float],
        right_returns: dict[int, float],
        *,
        lag_ms: int,
    ) -> dict[str, float | int] | None:
        left_values: list[float] = []
        right_values: list[float] = []
        if lag_ms == 0:
            common_times = sorted(set(left_returns) & set(right_returns))
            for timestamp in common_times:
                left_values.append(left_returns[timestamp])
                right_values.append(right_returns[timestamp])
        else:
            for timestamp, value in left_returns.items():
                follower_timestamp = timestamp + lag_ms
                if follower_timestamp not in right_returns:
                    continue
                left_values.append(value)
                right_values.append(right_returns[follower_timestamp])
        if len(left_values) < 3:
            return None
        correlation = _pearson(left_values, right_values)
        if correlation is None:
            return None
        return {
            "sample_count": len(left_values),
            "correlation": correlation,
        }

    def _build_symbol_reports(
        self,
        *,
        ranking: dict[str, dict[str, object]],
        symbols: list[str],
        candle_data: dict[str, dict[str, list[CandleRecord]]],
        per_symbol_results: dict[str, list[StrategyResult]],
    ) -> list[SymbolReport]:
        reports: list[SymbolReport] = []
        btc_returns = self._returns_by_timestamp(candle_data.get("1h", {}).get("BTC", []))
        btc_values = list(btc_returns.values())
        btc_std = self._std(btc_values)
        for symbol in symbols:
            ranking_row = ranking.get(symbol, {})
            symbol_results = [item for item in per_symbol_results.get(symbol, []) if item.sample_count >= 5]
            best_result = self._pick_best_result(symbol_results)
            one_hour_returns = self._returns_by_timestamp(candle_data.get("1h", {}).get(symbol, []))
            corr_to_btc = None
            beta_to_btc = None
            if symbol != "BTC" and btc_returns and one_hour_returns:
                common_times = sorted(set(btc_returns) & set(one_hour_returns))
                if len(common_times) >= 100:
                    btc_series = [btc_returns[item] for item in common_times]
                    symbol_series = [one_hour_returns[item] for item in common_times]
                    correlation = _pearson(btc_series, symbol_series)
                    if correlation is not None:
                        corr_to_btc = round(correlation, 4)
                        symbol_std = self._std(symbol_series)
                        if btc_std > 0:
                            beta_to_btc = round(correlation * (symbol_std / btc_std), 4)
            coverage_days = {
                interval: round(
                    (candles[-1].end_time - candles[0].start_time) / 86_400_000.0,
                    2,
                )
                if len(candles) >= 2
                else 0.0
                for interval, candles in (
                    (interval_name, candle_data.get(interval_name, {}).get(symbol, []))
                    for interval_name in candle_data
                )
            }
            reports.append(
                SymbolReport(
                    symbol=symbol,
                    rank=int(_safe_float(ranking_row.get("rank"))),
                    day_ntl_vlm=_safe_float(ranking_row.get("day_ntl_vlm")),
                    open_interest_usd=_safe_float(ranking_row.get("open_interest_usd")),
                    best_pattern=best_result.pattern if best_result is not None else None,
                    best_interval=best_result.interval if best_result is not None else None,
                    best_expectancy_net_bps=best_result.expectancy_net_bps if best_result is not None else 0.0,
                    best_sample_count=best_result.sample_count if best_result is not None else 0,
                    archetype=best_result.archetype if best_result is not None else None,
                    recommended_owner=self._recommended_owner(best_result),
                    btc_correlation_1h=corr_to_btc,
                    beta_to_btc_1h=beta_to_btc,
                    total_return_1h_bps=self._total_return_bps(candle_data.get("1h", {}).get(symbol, [])),
                    total_return_2h_bps=self._total_return_bps(candle_data.get("2h", {}).get(symbol, [])),
                    coverage_days=coverage_days,
                )
            )
        reports.sort(key=lambda item: item.rank)
        return reports

    def _pick_best_result(self, results: list[StrategyResult]) -> StrategyResult | None:
        if not results:
            return None
        scored = [
            (
                result.expectancy_net_bps * math.sqrt(result.sample_count) * max(result.profit_factor, 0.25),
                result,
            )
            for result in results
            if result.sample_count >= 5
        ]
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def _recommended_owner(self, result: StrategyResult | None) -> str:
        if result is None or result.expectancy_net_bps <= 0:
            return "observe_only"
        if result.archetype == "trend":
            return "pod_a"
        if result.archetype == "breakout":
            return "pod_b"
        return "new_pod_candidate"

    def _final_recommendation(
        self,
        *,
        aggregate_results: list[AggregatePatternResult],
        symbol_reports: list[SymbolReport],
    ) -> tuple[str, str]:
        trend_go = [
            item
            for item in aggregate_results
            if item.archetype == "trend" and item.recommendation == "go"
        ]
        breakout_go = [
            item
            for item in aggregate_results
            if item.archetype == "breakout" and item.recommendation == "go"
        ]
        mean_rev_go = [
            item
            for item in aggregate_results
            if item.archetype == "mean_reversion" and item.recommendation == "go"
        ]
        owner_counts = {
            "pod_a": sum(1 for item in symbol_reports if item.recommended_owner == "pod_a"),
            "pod_b": sum(1 for item in symbol_reports if item.recommended_owner == "pod_b"),
            "new_pod_candidate": sum(
                1 for item in symbol_reports if item.recommended_owner == "new_pod_candidate"
            ),
        }
        if trend_go or breakout_go:
            rationale_parts = []
            if trend_go:
                best_trend = max(trend_go, key=lambda item: item.expectancy_net_bps)
                rationale_parts.append(
                    f"trend family strongest on {best_trend.interval} with {best_trend.pattern} "
                    f"(net {best_trend.expectancy_net_bps} bps, {best_trend.positive_symbol_fraction:.0%} positive symbols)"
                )
            if breakout_go:
                best_breakout = max(breakout_go, key=lambda item: item.expectancy_net_bps)
                rationale_parts.append(
                    f"breakout family strongest on {best_breakout.interval} with {best_breakout.pattern} "
                    f"(net {best_breakout.expectancy_net_bps} bps)"
                )
            if owner_counts["new_pod_candidate"] <= max(owner_counts["pod_a"], owner_counts["pod_b"]):
                return (
                    "upgrade_existing_pods",
                    "; ".join(rationale_parts)
                    + ". Les signaux robustes restent majoritairement dans les familles déjà couvertes par Pod A/B.",
                )
        if mean_rev_go and owner_counts["new_pod_candidate"] >= 6:
            best_mean_rev = max(mean_rev_go, key=lambda item: item.expectancy_net_bps)
            return (
                "create_new_pod",
                f"mean reversion family shows repeatable edge on {best_mean_rev.interval} via "
                f"{best_mean_rev.pattern} (net {best_mean_rev.expectancy_net_bps} bps) "
                "sur un nombre significatif de symbols hors du scope naturel Pod A/B.",
            )
        return (
            "park_research_only",
            "Aucun pattern transversal n'est assez robuste pour justifier un remplacement immédiat. "
            "Le meilleur chemin est de conserver la collecte, renforcer les pods existants si besoin, "
            "et exiger un replay/backtest dédié avant tout nouveau sleeve live.",
        )

    def _total_return_bps(self, candles: list[CandleRecord]) -> float | None:
        if len(candles) < 2 or candles[0].close <= 0:
            return None
        return round((candles[-1].close - candles[0].close) / candles[0].close * 10_000.0, 4)

    def _std(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean_value = sum(values) / len(values)
        variance = sum((value - mean_value) ** 2 for value in values) / len(values)
        return math.sqrt(variance)


class HyperliquidTop30ResearchRunner:
    def __init__(
        self,
        *,
        config_path: str | Path = "config/trident.toml",
        round_trip_cost_bps: float = 8.0,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.collector = HyperliquidTop30DatasetBuilder(config_path=config_path, sleep_fn=sleep_fn)
        self.analyzer = HyperliquidTop30Analyzer(round_trip_cost_bps=round_trip_cost_bps)

    def collect(
        self,
        *,
        output_dir: str | Path,
        days: int = 180,
        top_n: int = 30,
        intervals: list[str] | None = None,
    ) -> dict[str, object]:
        return self.collector.collect(
            output_dir=output_dir,
            days=days,
            top_n=top_n,
            intervals=intervals,
        )

    def analyze(
        self,
        *,
        dataset_dir: str | Path,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
    ) -> ResearchResult:
        result = self.analyzer.analyze(dataset_dir)
        if output_json is not None:
            path = Path(output_json)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        if output_md is not None:
            path = Path(output_md)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._render_markdown(result), encoding="utf-8")
        return result

    def run(
        self,
        *,
        output_dir: str | Path,
        days: int = 180,
        top_n: int = 30,
        intervals: list[str] | None = None,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
        refresh: bool = False,
    ) -> ResearchResult:
        manifest_path = Path(output_dir) / "manifest.json"
        if refresh or not manifest_path.exists():
            self.collect(
                output_dir=output_dir,
                days=days,
                top_n=top_n,
                intervals=intervals,
            )
        return self.analyze(
            dataset_dir=output_dir,
            output_json=output_json,
            output_md=output_md,
        )

    def _render_markdown(self, result: ResearchResult) -> str:
        lines = [
            "# Hyperliquid Top 30 Research",
            "",
            f"- Dataset: `{result.dataset_dir}`",
            f"- Requested window: `{result.requested_start}` -> `{result.requested_end}`",
            f"- Final recommendation: **{result.final_recommendation}**",
            f"- Rationale: {result.recommendation_rationale}",
            "",
            "## Data Coverage",
            "",
            "| Interval | Full requested window | Median coverage ratio | Notes |",
            "|----------|------------------------|------------------------|-------|",
        ]
        for interval in result.intervals:
            interval_gap = result.data_gaps.get(interval, {})
            symbol_rows = [
                row
                for row in (interval_gap.get("symbols", {}) or {}).values()
                if isinstance(row, dict) and row.get("available")
            ]
            full_count = sum(1 for row in symbol_rows if row.get("full_requested_window"))
            ratios = [
                _safe_float(row.get("coverage_ratio_vs_request"))
                for row in symbol_rows
                if row.get("coverage_ratio_vs_request") is not None
            ]
            note = "Official HL candle API limit hits this interval." if full_count < len(symbol_rows) else "Full request covered."
            median_ratio = round(median(ratios), 4) if ratios else 0.0
            lines.append(
                f"| {interval} | {full_count}/{len(symbol_rows)} | {median_ratio} | {note} |"
            )
        lines.extend(
            [
                "",
                "## Strongest Pattern Families",
                "",
                "| Interval | Pattern | Archetype | Samples | Positive symbols | Hit rate | Net expectancy (bps) | Recommendation |",
                "|----------|---------|-----------|---------|------------------|----------|----------------------|----------------|",
            ]
        )
        for item in result.aggregate_patterns[:12]:
            lines.append(
                f"| {item.interval} | {item.pattern} | {item.archetype} | {item.sample_count} | "
                f"{item.positive_symbols}/{item.active_symbols} | {item.hit_rate} | "
                f"{item.expectancy_net_bps} | {item.recommendation} |"
            )
        lines.extend(
            [
                "",
                "## Symbol Recommendations",
                "",
                "| Rank | Symbol | 24h volume ($M) | OI ($M) | Best pattern | Best TF | Net expectancy (bps) | Suggested owner | Corr BTC 1h |",
                "|------|--------|-----------------|---------|--------------|---------|----------------------|-----------------|-------------|",
            ]
        )
        for item in result.symbol_reports:
            lines.append(
                f"| {item.rank} | {item.symbol} | {round(item.day_ntl_vlm / 1_000_000.0, 2)} | "
                f"{round(item.open_interest_usd / 1_000_000.0, 2)} | {item.best_pattern or '-'} | "
                f"{item.best_interval or '-'} | {item.best_expectancy_net_bps} | {item.recommended_owner} | "
                f"{item.btc_correlation_1h if item.btc_correlation_1h is not None else '-'} |"
            )
        lines.extend(
            [
                "",
                "## Strongest Correlations",
                "",
                "| Interval | Left | Right | Samples | Corr |",
                "|----------|------|-------|---------|------|",
            ]
        )
        for item in result.top_correlations[:10]:
            lines.append(
                f"| {item.interval} | {item.left_symbol} | {item.right_symbol} | "
                f"{item.sample_count} | {item.correlation} |"
            )
        lines.extend(
            [
                "",
                "## Lead-Lag Candidates",
                "",
                "| Interval | Leader | Follower | Lag bars | Lagged corr | Same-time corr |",
                "|----------|--------|----------|----------|-------------|----------------|",
            ]
        )
        for item in result.lead_lag_pairs[:10]:
            lines.append(
                f"| {item.interval} | {item.leader} | {item.follower} | {item.lag_bars} | "
                f"{item.lagged_correlation} | {item.same_time_correlation} |"
            )
        if result.correlation_clusters:
            lines.extend(["", "## Correlation Clusters", ""])
            for cluster in result.correlation_clusters[:10]:
                lines.append(f"- {', '.join(cluster)}")
        lines.append("")
        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and analyze top 30 Hyperliquid crypto data.")
    parser.add_argument("--mode", choices=["run", "collect", "analyze"], default="run")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--dataset-dir", default="data/research/hyperliquid_top30/current")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--timeframes", default="15m,30m,1h,2h")
    parser.add_argument("--round-trip-cost-bps", type=float, default=8.0)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    parser.add_argument("--refresh", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    intervals = [item.strip() for item in args.timeframes.split(",") if item.strip()]
    runner = HyperliquidTop30ResearchRunner(
        config_path=args.config,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    if args.mode == "collect":
        manifest = runner.collect(
            output_dir=args.dataset_dir,
            days=args.days,
            top_n=args.top_n,
            intervals=intervals,
        )
        print(f"dataset_dir={manifest['dataset_dir']}")
        print(f"symbols={len(manifest['symbols'])}")
        return
    if args.mode == "analyze":
        result = runner.analyze(
            dataset_dir=args.dataset_dir,
            output_json=args.output_json,
            output_md=args.output_md,
        )
    else:
        result = runner.run(
            output_dir=args.dataset_dir,
            days=args.days,
            top_n=args.top_n,
            intervals=intervals,
            output_json=args.output_json,
            output_md=args.output_md,
            refresh=args.refresh,
        )
    print(f"final_recommendation={result.final_recommendation}")
    print(f"symbols={len(result.symbols)}")


if __name__ == "__main__":
    main()
