from __future__ import annotations

import math
from dataclasses import dataclass

from app.trident.pod_a.candles import Candle


DOUBLE_BOTTOM_LENGTHS = (24, 48)
TRIANGLE_LENGTHS = (24, 48)


@dataclass(slots=True)
class ChartPatternCandidate:
    pattern: str
    validation_time: str
    entry_price: float
    target_price: float
    theoretical_target_pct: float
    structure_height_pct: float
    structure_depth_pct: float
    breakout_margin_pct: float
    compression_pct: float | None
    low_mismatch_pct: float | None
    upper_slope_pct_per_bar: float | None
    lower_slope_pct_per_bar: float | None
    pattern_bars: int
    score: float


def best_chart_pattern_candidate(candles_4h: list[Candle]) -> ChartPatternCandidate | None:
    """Return the best validated long 4h chart-pattern candidate on completed candles."""
    if len(candles_4h) < 25:
        return None
    candidates = [
        *detect_double_bottoms(candles_4h),
        *detect_triangle_breakouts(candles_4h),
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            item.score,
            item.breakout_margin_pct,
            -item.theoretical_target_pct,
        ),
        reverse=True,
    )[0]


def detect_double_bottoms(candles_4h: list[Candle]) -> list[ChartPatternCandidate]:
    rows: list[ChartPatternCandidate] = []
    breakout_index = len(candles_4h) - 1
    for pattern_bars in DOUBLE_BOTTOM_LENGTHS:
        start = breakout_index - pattern_bars
        if start < 0:
            continue
        base = candles_4h[start:breakout_index]
        if len(base) < pattern_bars:
            continue
        first_zone_end = max(1, int(pattern_bars * 0.45))
        second_zone_start = max(first_zone_end + 2, int(pattern_bars * 0.45))
        second_zone_end = max(second_zone_start + 2, int(pattern_bars * 0.90))
        if second_zone_end > len(base):
            continue
        first_rel = min(range(0, first_zone_end), key=lambda index: base[index].low)
        second_rel = min(
            range(second_zone_start, second_zone_end),
            key=lambda index: base[index].low,
        )
        if second_rel <= first_rel + max(4, pattern_bars // 6):
            continue
        first_low = base[first_rel].low
        second_low = base[second_rel].low
        avg_low = (first_low + second_low) / 2.0
        if avg_low <= 0:
            continue
        low_mismatch_pct = pct(abs(first_low - second_low) / avg_low)
        if low_mismatch_pct > 5.5:
            continue
        neckline_slice = base[first_rel : second_rel + 1]
        neckline = max(candle.high for candle in neckline_slice)
        if neckline <= avg_low * 1.025:
            continue
        post_second_high = max(candle.high for candle in base[second_rel:])
        if post_second_high < neckline * 0.985:
            continue
        breakout = candles_4h[breakout_index]
        previous = candles_4h[breakout_index - 1]
        if breakout.close <= neckline * 1.0015 or previous.close > neckline * 1.00075:
            continue
        entry = breakout.close
        target = neckline + (neckline - avg_low)
        if target <= entry * 1.001:
            continue
        height_pct = pct((neckline - avg_low) / neckline)
        if height_pct < 2.5 or height_pct > 35.0:
            continue
        breakout_margin = pct((entry - neckline) / neckline)
        rows.append(
            ChartPatternCandidate(
                pattern="double_bottom",
                validation_time=iso_z(breakout.opened_at),
                entry_price=entry,
                target_price=target,
                theoretical_target_pct=pct((target - entry) / entry),
                structure_height_pct=height_pct,
                structure_depth_pct=height_pct,
                breakout_margin_pct=breakout_margin,
                compression_pct=None,
                low_mismatch_pct=low_mismatch_pct,
                upper_slope_pct_per_bar=None,
                lower_slope_pct_per_bar=None,
                pattern_bars=pattern_bars,
                score=double_bottom_score(height_pct, low_mismatch_pct, breakout_margin, pattern_bars),
            )
        )
    return rows


def detect_triangle_breakouts(candles_4h: list[Candle]) -> list[ChartPatternCandidate]:
    rows: list[ChartPatternCandidate] = []
    breakout_index = len(candles_4h) - 1
    for pattern_bars in TRIANGLE_LENGTHS:
        start = breakout_index - pattern_bars
        if start < 0:
            continue
        base = candles_4h[start:breakout_index]
        if len(base) < pattern_bars:
            continue
        highs = [candle.high for candle in base]
        lows = [candle.low for candle in base]
        if min(lows) <= 0:
            continue
        high_slope, high_intercept = linear_regression(highs)
        low_slope, low_intercept = linear_regression(lows)
        upper_now = high_intercept + high_slope * len(highs)
        lower_now = low_intercept + low_slope * len(lows)
        upper_start = high_intercept
        lower_start = low_intercept
        if upper_now <= 0 or lower_now <= 0:
            continue
        start_height = upper_start - lower_start
        end_height = upper_now - lower_now
        widest_height = max(highs) - min(lows)
        if start_height <= 0 or end_height <= 0 or widest_height <= 0:
            continue
        compression_pct = pct((start_height - end_height) / start_height)
        height_pct = pct(widest_height / max(highs))
        upper_slope_pct = pct(high_slope / max(upper_start, 1e-12))
        lower_slope_pct = pct(low_slope / max(lower_start, 1e-12))
        if compression_pct < 18.0 or height_pct < 2.5 or height_pct > 35.0:
            continue
        if high_slope >= 0 or low_slope <= 0:
            continue
        breakout = candles_4h[breakout_index]
        previous = candles_4h[breakout_index - 1]
        previous_upper = high_intercept + high_slope * (len(highs) - 1)
        if breakout.close <= upper_now * 1.0015 or previous.close > previous_upper * 1.00075:
            continue
        entry = breakout.close
        target = upper_now + widest_height
        if target <= entry * 1.001:
            continue
        breakout_margin = pct((entry - upper_now) / upper_now)
        rows.append(
            ChartPatternCandidate(
                pattern="triangle_breakout",
                validation_time=iso_z(breakout.opened_at),
                entry_price=entry,
                target_price=target,
                theoretical_target_pct=pct((target - entry) / entry),
                structure_height_pct=height_pct,
                structure_depth_pct=height_pct,
                breakout_margin_pct=breakout_margin,
                compression_pct=compression_pct,
                low_mismatch_pct=None,
                upper_slope_pct_per_bar=upper_slope_pct,
                lower_slope_pct_per_bar=lower_slope_pct,
                pattern_bars=pattern_bars,
                score=triangle_score(compression_pct, height_pct, breakout_margin, pattern_bars),
            )
        )
    return rows


def linear_regression(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    sum_x = n * (n - 1) / 2.0
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6.0
    sum_y = sum(values)
    sum_xy = sum(index * value for index, value in enumerate(values))
    denom = n * sum_x2 - sum_x * sum_x
    if denom <= 0:
        return 0.0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def double_bottom_score(
    height_pct: float,
    low_mismatch_pct: float,
    breakout_margin_pct: float,
    pattern_bars: int,
) -> float:
    height_score = 1.0 - min(abs(height_pct - 9.0) / 18.0, 1.0)
    symmetry_score = 1.0 - min(low_mismatch_pct / 5.5, 1.0)
    breakout_score = min(max(breakout_margin_pct / 2.0, 0.0), 1.0)
    length_score = min(math.log(max(pattern_bars, 2), 2) / 8.0, 1.0) * 0.25
    return height_score + symmetry_score + breakout_score + length_score


def triangle_score(
    compression_pct: float,
    height_pct: float,
    breakout_margin_pct: float,
    pattern_bars: int,
) -> float:
    compression_score = min(max(compression_pct / 55.0, 0.0), 1.0)
    height_score = 1.0 - min(abs(height_pct - 10.0) / 20.0, 1.0)
    breakout_score = min(max(breakout_margin_pct / 2.0, 0.0), 1.0)
    length_score = min(math.log(max(pattern_bars, 2), 2) / 8.0, 1.0) * 0.25
    return compression_score + height_score + breakout_score + length_score


def pct(value: float) -> float:
    return value * 100.0


def iso_z(value: object) -> str:
    return str(value.isoformat()).replace("+00:00", "Z")
