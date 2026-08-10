#!/usr/bin/env python3
"""Research-only chart-pattern scan and filtered replay.

This is the executable companion for the local chart-pattern-edge-analysis
skill. It reads local OHLCV candles, detects validated long breakout patterns,
measures theoretical and partial targets, tests simple filters, and writes
standalone reports. It does not change live config, deploy scripts, fetch
scripts, or trading behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import run_cup_handle_pattern_scan as cup


DEFAULT_PATTERNS = [
    "cup_handle",
    "rectangle_breakout",
    "flag_pennant",
    "triangle_breakout",
    "double_bottom",
]
DEFAULT_TIMEFRAMES = ["1h", "4h"]
DEFAULT_TARGET_FRACTIONS = [25.0, 33.0, 50.0, 66.0, 75.0, 100.0]
DEFAULT_STOP_LOSSES = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0]
SPLIT_TIME = "2026-01-01T00:00:00Z"

LENGTHS = {
    "rectangle_breakout": {
        "1h": (48, 96),
        "4h": (24, 48),
    },
    "triangle_breakout": {
        "1h": (72, 144),
        "4h": (24, 48),
    },
    "flag_pennant": {
        "1h": (24, 48),
        "4h": (12, 24),
    },
    "double_bottom": {
        "1h": (72, 144),
        "4h": (24, 48),
    },
}


@dataclass(slots=True)
class PatternCase:
    pattern: str
    side: str
    symbol: str
    timeframe: str
    validation_time: str
    status: str
    target_hit_horizon: bool
    target_hit_ever: bool
    target_progress_horizon_pct: float
    target_progress_ever_pct: float
    adverse_before_target_or_peak_pct: float
    adverse_before_target_or_horizon_pct: float
    max_favorable_horizon_pct: float
    max_up_if_no_target_pct: float | None
    end_return_horizon_pct: float
    bars_to_target_horizon: int | None
    time_to_target_horizon: str
    horizon_bars: int
    available_future_bars: int
    pattern_bars: int
    setup_bars: int | None
    consolidation_bars: int | None
    entry_price: float
    target_price: float
    target_pct_from_entry: float
    structure_height_pct: float
    structure_depth_pct: float
    breakout_margin_pct: float
    compression_pct: float | None
    prior_move_pct: float | None
    pullback_pct: float | None
    touch_count: int | None
    upper_slope_pct_per_bar: float | None
    lower_slope_pct_per_bar: float | None
    score: float
    rsi14: float | None
    volume_ratio20: float | None
    volume_zscore20: float | None
    atr14_pct: float | None
    sma20_distance_pct: float | None
    prior_24h_return_pct: float | None


@dataclass(slots=True)
class TradeCaseRow:
    pattern: str
    symbol: str
    timeframe: str
    validation_time: str
    target_theorique_atteinte: str
    temps_apres_validation: str
    hausse_max_si_target_non_atteinte_pct: float | None
    baisse_max_avant_target_ou_point_haut_pct: float
    target_theorique_pct: float
    entry_price: float
    target_theorique_price: float
    target_33_pct_atteinte: str
    target_33_pct_temps: str
    target_33_pct_baisse_avant_pct: float
    target_50_pct_atteinte: str
    target_50_pct_temps: str
    target_50_pct_baisse_avant_pct: float
    rsi14: float | None
    volume_ratio20: float | None
    atr14_pct: float | None
    sma20_distance_pct: float | None
    prior_24h_return_pct: float | None
    structure_height_pct: float
    structure_depth_pct: float
    breakout_margin_pct: float
    score: float


@dataclass(slots=True)
class TargetLevelCase:
    pattern: str
    symbol: str
    timeframe: str
    validation_time: str
    target_fraction_pct: float
    target_hit: str
    time_to_target: str
    bars_to_target: int | None
    target_pct_from_entry: float
    target_price: float
    max_up_if_not_hit_pct: float | None
    adverse_before_target_or_peak_pct: float


@dataclass(slots=True)
class TargetLevelSummary:
    pattern: str
    timeframe: str
    target_fraction_pct: float
    case_count: int
    hit_count: int
    hit_rate_pct: float
    median_target_pct_from_entry: float | None
    median_bars_to_target: float | None
    median_adverse_before_target_or_peak_pct: float | None
    stop_needed_for_75pct_hits_pct: float | None
    stop_needed_for_90pct_hits_pct: float | None
    median_max_up_if_not_hit_pct: float | None


@dataclass(slots=True)
class StopGridSummary:
    pattern: str
    timeframe: str
    target_fraction_pct: float
    stop_loss_pct: float
    trade_count: int
    target_count: int
    stop_count: int
    timeout_count: int
    target_rate_pct: float
    stop_rate_pct: float
    avg_exit_return_pct: float
    median_exit_return_pct: float | None
    profit_factor: float | None


@dataclass(slots=True)
class IndicatorCorrelationRow:
    pattern: str
    timeframe: str
    indicator: str
    sample_count: int
    corr_target_hit: float | None
    corr_target_progress: float | None
    corr_max_favorable: float | None
    hit_rate_bottom_tercile_pct: float | None
    hit_rate_top_tercile_pct: float | None
    median_progress_bottom_tercile_pct: float | None
    median_progress_top_tercile_pct: float | None


@dataclass(slots=True)
class FilterReplayRow:
    pattern: str
    filter_name: str
    target_fraction_pct: float
    stop_loss_pct: float
    split: str
    trade_count: int
    target_count: int
    stop_count: int
    timeout_count: int
    target_rate_pct: float
    stop_rate_pct: float
    avg_exit_return_pct: float
    median_exit_return_pct: float | None
    profit_factor: float | None
    robust_positive: bool
    selected: bool


@dataclass(slots=True)
class SelectedTradeRow:
    pattern: str
    filter_name: str
    target_fraction_pct: float
    stop_loss_pct: float
    symbol: str
    timeframe: str
    validation_time: str
    split: str
    outcome: str
    exit_return_pct: float
    target_pct_from_entry: float
    adverse_before_target_or_peak_pct: float
    volume_ratio20: float | None
    atr14_pct: float | None
    breakout_margin_pct: float
    structure_depth_pct: float
    score: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", action="append", default=None)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--patterns", default=",".join(DEFAULT_PATTERNS))
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    parser.add_argument("--target-fractions", default=",".join(str(item) for item in DEFAULT_TARGET_FRACTIONS))
    parser.add_argument("--stop-loss-pcts", default=",".join(str(item) for item in DEFAULT_STOP_LOSSES))
    parser.add_argument("--split-time", default=SPLIT_TIME)
    parser.add_argument("--max-horizon-days", type=float, default=14.0)
    parser.add_argument("--cup-source-dir", default="server-data/replay_reports/cup_handle_pattern_scan_20260705T204125Z")
    parser.add_argument("--top-rows", type=int, default=40)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_roots = [Path(item) for item in (args.input_root or ["data", "server-data"])]
    patterns = [item.strip() for item in args.patterns.split(",") if item.strip()]
    timeframes = [item.strip() for item in args.timeframes.split(",") if item.strip()]
    target_fractions = cup.parse_percent_list(args.target_fractions, default=DEFAULT_TARGET_FRACTIONS)
    stop_losses = cup.parse_percent_list(args.stop_loss_pcts, default=DEFAULT_STOP_LOSSES)
    stamp = cup.utc_stamp()
    output_dir = Path(args.output_dir or f"server-data/replay_reports/chart_pattern_skill_replay_{stamp}")
    output_dir.mkdir(parents=True, exist_ok=True)

    candle_files = cup.discover_candle_files(input_roots)
    series_all, source_files = cup.load_all_series(candle_files)
    series = {
        key: candles
        for key, candles in series_all.items()
        if key[1] in set(timeframes)
    }
    coverage_rows = [
        row
        for row in cup.build_coverage_rows(series, source_files)
        if row.timeframe in set(timeframes)
    ]

    raw_cases: list[PatternCase] = []
    if "cup_handle" in patterns:
        raw_cases.extend(
            detect_cup_handle(
                series,
                source_files,
                max_horizon_days=float(args.max_horizon_days),
                source_dir=Path(args.cup_source_dir),
            )
        )
        print(f"cup_handle cases loaded: {sum(1 for item in raw_cases if item.pattern == 'cup_handle')}", file=sys.stderr, flush=True)
    for (symbol, timeframe), candles in sorted(series.items()):
        if "rectangle_breakout" in patterns:
            raw_cases.extend(detect_rectangle_breakouts(symbol, timeframe, candles, max_horizon_days=float(args.max_horizon_days)))
        if "triangle_breakout" in patterns:
            raw_cases.extend(detect_triangle_breakouts(symbol, timeframe, candles, max_horizon_days=float(args.max_horizon_days)))
        if "flag_pennant" in patterns:
            raw_cases.extend(detect_flag_pennants(symbol, timeframe, candles, max_horizon_days=float(args.max_horizon_days)))
        if "double_bottom" in patterns:
            raw_cases.extend(detect_double_bottoms(symbol, timeframe, candles, max_horizon_days=float(args.max_horizon_days)))
        print(f"scanned {symbol} {timeframe}: raw cases {len(raw_cases)}", file=sys.stderr, flush=True)

    cases = dedupe_cases(raw_cases)
    cases.sort(key=lambda item: (item.pattern, item.validation_time, item.symbol, item.timeframe))
    target_level_cases = build_target_level_cases(cases, series, target_fractions)
    target_level_summaries = summarize_target_level_cases(target_level_cases)
    stop_grid_summaries = build_stop_grid_summaries(cases, series, target_fractions, stop_losses)
    indicator_correlations = build_indicator_correlations(cases)
    trade_rows = build_trade_case_rows(cases, target_level_cases)
    filter_rows, selected_trades, selected_configs = run_filtered_replay(
        cases=cases,
        series=series,
        target_fractions=target_fractions,
        stop_losses=stop_losses,
        split_time=args.split_time,
    )

    payload = {
        "kind": "chart_pattern_skill_replay",
        "generated_at": stamp,
        "decision": "research_only_no_live_change",
        "parameters": {
            "patterns": patterns,
            "timeframes": timeframes,
            "target_fractions_pct": target_fractions,
            "stop_loss_pcts": stop_losses,
            "split_time": args.split_time,
            "max_horizon_days": float(args.max_horizon_days),
        },
        "inputs": {
            "input_roots": [str(item) for item in input_roots],
            "candle_file_count": len(candle_files),
        },
        "coverage": [asdict(row) for row in coverage_rows],
        "summary": build_summary(cases, coverage_rows),
        "target_level_summary": [asdict(row) for row in target_level_summaries],
        "stop_grid_summary": [asdict(row) for row in stop_grid_summaries],
        "indicator_correlations": [asdict(row) for row in indicator_correlations],
        "filtered_replay": [asdict(row) for row in filter_rows],
        "selected_configs": selected_configs,
        "cases": [asdict(row) for row in cases],
    }

    write_csv(output_dir / "chart_pattern_cases.csv", cases)
    write_csv(output_dir / "chart_pattern_trade_cases.csv", trade_rows)
    write_csv(output_dir / "chart_pattern_target_level_cases.csv", target_level_cases)
    write_csv(output_dir / "chart_pattern_target_level_summary.csv", target_level_summaries)
    write_csv(output_dir / "chart_pattern_stop_grid_summary.csv", stop_grid_summaries)
    write_csv(output_dir / "chart_pattern_indicator_correlations.csv", indicator_correlations)
    write_csv(output_dir / "filtered_replay_grid.csv", filter_rows)
    write_csv(output_dir / "selected_filter_trades.csv", selected_trades)
    write_json(output_dir / "chart_pattern_report.json", payload)
    write_markdown_report(
        output_dir / "chart_pattern_report.md",
        payload=payload,
        cases=cases,
        target_level_summaries=target_level_summaries,
        filter_rows=filter_rows,
        selected_configs=selected_configs,
        top_rows=int(args.top_rows),
    )
    print(output_dir)


def detect_cup_handle(
    series: dict[tuple[str, str], list[cup.Candle]],
    source_files: dict[tuple[str, str], set[str]],
    *,
    max_horizon_days: float,
    source_dir: Path,
) -> list[PatternCase]:
    csv_path = source_dir / "cup_handle_cases.csv"
    if csv_path.exists():
        return load_cup_cases_from_csv(csv_path, set(series))
    raw: list[cup.PatternCase] = []
    for (symbol, timeframe), candles in sorted(series.items()):
        if timeframe not in cup.DEFAULT_LENGTHS:
            continue
        if len(candles) < min(cup.DEFAULT_LENGTHS[timeframe]):
            continue
        raw.extend(
            cup.detect_cases(
                symbol=symbol,
                interval=timeframe,
                candles=candles,
                source_file_count=len(source_files.get((symbol, timeframe), set())),
                min_cup_depth_pct=3.0,
                max_cup_depth_pct=45.0,
                min_handle_depth_pct=0.25,
                max_rim_mismatch_pct=8.0,
                breakout_buffer_pct=0.15,
                max_horizon_days=max_horizon_days,
                min_prior_trend_pct=5.0,
            )
        )
    return [convert_cup_case(row) for row in cup.dedupe_cases(raw)]


def load_cup_cases_from_csv(path: Path, allowed_keys: set[tuple[str, str]]) -> list[PatternCase]:
    rows: list[PatternCase] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            symbol = str(raw.get("symbol", ""))
            timeframe = str(raw.get("timeframe", ""))
            if (symbol, timeframe) not in allowed_keys:
                continue
            rows.append(
                PatternCase(
                    pattern="cup_handle",
                    side="long",
                    symbol=symbol,
                    timeframe=timeframe,
                    validation_time=str(raw.get("breakout_time", "")),
                    status=str(raw.get("status", "")),
                    target_hit_horizon=parse_bool(raw.get("target_hit_horizon")),
                    target_hit_ever=parse_bool(raw.get("target_hit_ever")),
                    target_progress_horizon_pct=parse_float(raw.get("target_progress_horizon_pct")),
                    target_progress_ever_pct=parse_float(raw.get("target_progress_ever_pct")),
                    adverse_before_target_or_peak_pct=parse_float(raw.get("adverse_before_target_or_peak_pct")),
                    adverse_before_target_or_horizon_pct=parse_float(raw.get("adverse_before_target_or_horizon_pct")),
                    max_favorable_horizon_pct=parse_float(raw.get("max_favorable_horizon_pct")),
                    max_up_if_no_target_pct=parse_optional_float(raw.get("max_up_if_no_target_pct")),
                    end_return_horizon_pct=parse_float(raw.get("end_return_horizon_pct")),
                    bars_to_target_horizon=parse_optional_int(raw.get("bars_to_target_horizon")),
                    time_to_target_horizon=str(raw.get("time_to_target_horizon", "")),
                    horizon_bars=parse_int(raw.get("horizon_bars")),
                    available_future_bars=parse_int(raw.get("available_future_bars")),
                    pattern_bars=parse_int(raw.get("pattern_bars")),
                    setup_bars=parse_optional_int(raw.get("cup_bars")),
                    consolidation_bars=parse_optional_int(raw.get("handle_bars")),
                    entry_price=parse_float(raw.get("breakout_close")),
                    target_price=parse_float(raw.get("target_price")),
                    target_pct_from_entry=parse_float(raw.get("target_pct_from_breakout")),
                    structure_height_pct=parse_float(raw.get("cup_depth_pct")),
                    structure_depth_pct=parse_float(raw.get("cup_depth_pct")),
                    breakout_margin_pct=parse_float(raw.get("breakout_margin_pct")),
                    compression_pct=None,
                    prior_move_pct=parse_optional_float(raw.get("prior_trend_pct")),
                    pullback_pct=parse_optional_float(raw.get("handle_depth_pct")),
                    touch_count=None,
                    upper_slope_pct_per_bar=None,
                    lower_slope_pct_per_bar=None,
                    score=parse_float(raw.get("score")),
                    rsi14=parse_optional_float(raw.get("rsi14")),
                    volume_ratio20=parse_optional_float(raw.get("volume_ratio20")),
                    volume_zscore20=parse_optional_float(raw.get("volume_zscore20")),
                    atr14_pct=parse_optional_float(raw.get("atr14_pct")),
                    sma20_distance_pct=parse_optional_float(raw.get("sma20_distance_pct")),
                    prior_24h_return_pct=parse_optional_float(raw.get("prior_24h_return_pct")),
                )
            )
    return rows


def convert_cup_case(row: cup.PatternCase) -> PatternCase:
    return PatternCase(
        pattern="cup_handle",
        side="long",
        symbol=row.symbol,
        timeframe=row.timeframe,
        validation_time=row.breakout_time,
        status=row.status,
        target_hit_horizon=row.target_hit_horizon,
        target_hit_ever=row.target_hit_ever,
        target_progress_horizon_pct=row.target_progress_horizon_pct,
        target_progress_ever_pct=row.target_progress_ever_pct,
        adverse_before_target_or_peak_pct=row.adverse_before_target_or_peak_pct,
        adverse_before_target_or_horizon_pct=row.adverse_before_target_or_horizon_pct,
        max_favorable_horizon_pct=row.max_favorable_horizon_pct,
        max_up_if_no_target_pct=row.max_up_if_no_target_pct,
        end_return_horizon_pct=row.end_return_horizon_pct,
        bars_to_target_horizon=row.bars_to_target_horizon,
        time_to_target_horizon=row.time_to_target_horizon,
        horizon_bars=row.horizon_bars,
        available_future_bars=row.available_future_bars,
        pattern_bars=row.pattern_bars,
        setup_bars=row.cup_bars,
        consolidation_bars=row.handle_bars,
        entry_price=row.breakout_close,
        target_price=row.target_price,
        target_pct_from_entry=row.target_pct_from_breakout,
        structure_height_pct=row.cup_depth_pct,
        structure_depth_pct=row.cup_depth_pct,
        breakout_margin_pct=row.breakout_margin_pct,
        compression_pct=None,
        prior_move_pct=row.prior_trend_pct,
        pullback_pct=row.handle_depth_pct,
        touch_count=None,
        upper_slope_pct_per_bar=None,
        lower_slope_pct_per_bar=None,
        score=row.score,
        rsi14=row.rsi14,
        volume_ratio20=row.volume_ratio20,
        volume_zscore20=row.volume_zscore20,
        atr14_pct=row.atr14_pct,
        sma20_distance_pct=row.sma20_distance_pct,
        prior_24h_return_pct=row.prior_24h_return_pct,
    )


def detect_rectangle_breakouts(
    symbol: str,
    timeframe: str,
    candles: list[cup.Candle],
    *,
    max_horizon_days: float,
) -> list[PatternCase]:
    rows: list[PatternCase] = []
    lengths = LENGTHS["rectangle_breakout"].get(timeframe, ())
    max_horizon_bars = max_horizon(timeframe, max_horizon_days)
    for breakout_index in range(max(lengths, default=0), len(candles)):
        for pattern_bars in lengths:
            start = breakout_index - pattern_bars
            if start < 0 or not cup.is_contiguous(candles, start, breakout_index, timeframe):
                continue
            base = candles[start:breakout_index]
            if len(base) < pattern_bars:
                continue
            range_high = max(candle.high for candle in base)
            range_low = min(candle.low for candle in base)
            if range_high <= 0 or range_low <= 0 or range_high <= range_low:
                continue
            height = range_high - range_low
            height_pct = cup.pct(height / range_high)
            if height_pct < 2.0 or height_pct > 22.0:
                continue
            tolerance = height * 0.18
            upper_touches = sum(1 for candle in base if candle.high >= range_high - tolerance)
            lower_touches = sum(1 for candle in base if candle.low <= range_low + tolerance)
            if upper_touches < 2 or lower_touches < 2:
                continue
            breakout = candles[breakout_index]
            previous = candles[breakout_index - 1]
            if breakout.close <= range_high * 1.0015 or previous.close > range_high * 1.00075:
                continue
            entry = breakout.close
            target = range_high + height
            if target <= entry * 1.001:
                continue
            breakout_margin = cup.pct((entry - range_high) / range_high)
            score = rectangle_score(height_pct, breakout_margin, upper_touches, lower_touches, pattern_bars)
            rows.append(
                build_case(
                    pattern="rectangle_breakout",
                    symbol=symbol,
                    timeframe=timeframe,
                    candles=candles,
                    breakout_index=breakout_index,
                    entry=entry,
                    target=target,
                    horizon_bars=min(max_horizon_bars, max(pattern_bars * 2, 24 if timeframe == "1h" else 12)),
                    pattern_bars=pattern_bars,
                    setup_bars=None,
                    consolidation_bars=pattern_bars,
                    structure_height_pct=height_pct,
                    structure_depth_pct=height_pct,
                    breakout_margin_pct=breakout_margin,
                    compression_pct=None,
                    prior_move_pct=None,
                    pullback_pct=None,
                    touch_count=upper_touches + lower_touches,
                    upper_slope_pct_per_bar=None,
                    lower_slope_pct_per_bar=None,
                    score=score,
                )
            )
    return rows


def detect_triangle_breakouts(
    symbol: str,
    timeframe: str,
    candles: list[cup.Candle],
    *,
    max_horizon_days: float,
) -> list[PatternCase]:
    rows: list[PatternCase] = []
    lengths = LENGTHS["triangle_breakout"].get(timeframe, ())
    max_horizon_bars = max_horizon(timeframe, max_horizon_days)
    for breakout_index in range(max(lengths, default=0), len(candles)):
        for pattern_bars in lengths:
            start = breakout_index - pattern_bars
            if start < 0 or not cup.is_contiguous(candles, start, breakout_index, timeframe):
                continue
            base = candles[start:breakout_index]
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
            compression_pct = cup.pct((start_height - end_height) / start_height)
            height_pct = cup.pct(widest_height / max(highs))
            upper_slope_pct = cup.pct(high_slope / max(upper_start, 1e-12))
            lower_slope_pct = cup.pct(low_slope / max(lower_start, 1e-12))
            if compression_pct < 18.0 or height_pct < 2.5 or height_pct > 35.0:
                continue
            if high_slope >= 0 or low_slope <= 0:
                continue
            breakout = candles[breakout_index]
            previous = candles[breakout_index - 1]
            previous_upper = high_intercept + high_slope * (len(highs) - 1)
            if breakout.close <= upper_now * 1.0015 or previous.close > previous_upper * 1.00075:
                continue
            entry = breakout.close
            target = upper_now + widest_height
            if target <= entry * 1.001:
                continue
            breakout_margin = cup.pct((entry - upper_now) / upper_now)
            score = triangle_score(compression_pct, height_pct, breakout_margin, pattern_bars)
            rows.append(
                build_case(
                    pattern="triangle_breakout",
                    symbol=symbol,
                    timeframe=timeframe,
                    candles=candles,
                    breakout_index=breakout_index,
                    entry=entry,
                    target=target,
                    horizon_bars=min(max_horizon_bars, max(pattern_bars * 2, 24 if timeframe == "1h" else 12)),
                    pattern_bars=pattern_bars,
                    setup_bars=None,
                    consolidation_bars=pattern_bars,
                    structure_height_pct=height_pct,
                    structure_depth_pct=height_pct,
                    breakout_margin_pct=breakout_margin,
                    compression_pct=compression_pct,
                    prior_move_pct=None,
                    pullback_pct=None,
                    touch_count=None,
                    upper_slope_pct_per_bar=upper_slope_pct,
                    lower_slope_pct_per_bar=lower_slope_pct,
                    score=score,
                )
            )
    return rows


def detect_flag_pennants(
    symbol: str,
    timeframe: str,
    candles: list[cup.Candle],
    *,
    max_horizon_days: float,
) -> list[PatternCase]:
    rows: list[PatternCase] = []
    lengths = LENGTHS["flag_pennant"].get(timeframe, ())
    max_horizon_bars = max_horizon(timeframe, max_horizon_days)
    for breakout_index in range(max(lengths, default=0) * 3, len(candles)):
        for consolidation_bars in lengths:
            impulse_bars = max(6 if timeframe == "1h" else 4, int(consolidation_bars * 0.75))
            start = breakout_index - consolidation_bars - impulse_bars
            cons_start = breakout_index - consolidation_bars
            if start < 0 or cons_start <= start:
                continue
            if not cup.is_contiguous(candles, start, breakout_index, timeframe):
                continue
            impulse = candles[start:cons_start]
            consolidation = candles[cons_start:breakout_index]
            if len(impulse) < impulse_bars or len(consolidation) < consolidation_bars:
                continue
            impulse_low = min(candle.low for candle in impulse)
            impulse_high = max(candle.high for candle in impulse)
            impulse_start_close = impulse[0].close
            impulse_end_close = impulse[-1].close
            if impulse_start_close <= 0 or impulse_low <= 0:
                continue
            impulse_return_pct = cup.pct((impulse_end_close - impulse_start_close) / impulse_start_close)
            impulse_height_pct = cup.pct((impulse_high - impulse_low) / impulse_low)
            min_impulse = 4.5 if timeframe == "1h" else 6.0
            if impulse_return_pct < min_impulse or impulse_height_pct < min_impulse:
                continue
            cons_high = max(candle.high for candle in consolidation)
            cons_low = min(candle.low for candle in consolidation)
            cons_height = cons_high - cons_low
            impulse_height = impulse_high - impulse_low
            if cons_high <= 0 or cons_low <= 0 or cons_height <= 0 or impulse_height <= 0:
                continue
            pullback_pct = cup.pct((impulse_high - cons_low) / max(impulse_high, 1e-12))
            if pullback_pct > min(18.0, impulse_height_pct * 0.65):
                continue
            if cons_height > impulse_height * 0.55:
                continue
            slope, intercept = linear_regression([candle.close for candle in consolidation])
            slope_pct = cup.pct(slope / max(intercept, 1e-12))
            if slope_pct > 0.35:
                continue
            breakout = candles[breakout_index]
            previous = candles[breakout_index - 1]
            if breakout.close <= cons_high * 1.0015 or previous.close > cons_high * 1.00075:
                continue
            entry = breakout.close
            target = entry + impulse_height
            if target <= entry * 1.001:
                continue
            breakout_margin = cup.pct((entry - cons_high) / cons_high)
            cons_height_pct = cup.pct(cons_height / cons_high)
            score = flag_score(impulse_return_pct, pullback_pct, cons_height / impulse_height, breakout_margin)
            rows.append(
                build_case(
                    pattern="flag_pennant",
                    symbol=symbol,
                    timeframe=timeframe,
                    candles=candles,
                    breakout_index=breakout_index,
                    entry=entry,
                    target=target,
                    horizon_bars=min(max_horizon_bars, max((consolidation_bars + impulse_bars) * 2, 24 if timeframe == "1h" else 12)),
                    pattern_bars=consolidation_bars + impulse_bars,
                    setup_bars=impulse_bars,
                    consolidation_bars=consolidation_bars,
                    structure_height_pct=impulse_height_pct,
                    structure_depth_pct=cons_height_pct,
                    breakout_margin_pct=breakout_margin,
                    compression_pct=100.0 * (1.0 - cons_height / impulse_height),
                    prior_move_pct=impulse_return_pct,
                    pullback_pct=pullback_pct,
                    touch_count=None,
                    upper_slope_pct_per_bar=slope_pct,
                    lower_slope_pct_per_bar=None,
                    score=score,
                )
            )
    return rows


def detect_double_bottoms(
    symbol: str,
    timeframe: str,
    candles: list[cup.Candle],
    *,
    max_horizon_days: float,
) -> list[PatternCase]:
    rows: list[PatternCase] = []
    lengths = LENGTHS["double_bottom"].get(timeframe, ())
    max_horizon_bars = max_horizon(timeframe, max_horizon_days)
    for breakout_index in range(max(lengths, default=0), len(candles)):
        for pattern_bars in lengths:
            start = breakout_index - pattern_bars
            if start < 0 or not cup.is_contiguous(candles, start, breakout_index, timeframe):
                continue
            base = candles[start:breakout_index]
            if len(base) < pattern_bars:
                continue
            first_zone_end = max(1, int(pattern_bars * 0.45))
            second_zone_start = max(first_zone_end + 2, int(pattern_bars * 0.45))
            second_zone_end = max(second_zone_start + 2, int(pattern_bars * 0.90))
            if second_zone_end > len(base):
                continue
            first_rel = min(range(0, first_zone_end), key=lambda index: base[index].low)
            second_rel = min(range(second_zone_start, second_zone_end), key=lambda index: base[index].low)
            if second_rel <= first_rel + max(4, pattern_bars // 6):
                continue
            first_low = base[first_rel].low
            second_low = base[second_rel].low
            avg_low = (first_low + second_low) / 2.0
            if avg_low <= 0:
                continue
            low_mismatch_pct = cup.pct(abs(first_low - second_low) / avg_low)
            if low_mismatch_pct > 5.5:
                continue
            neckline_slice = base[first_rel:second_rel + 1]
            neckline = max(candle.high for candle in neckline_slice)
            if neckline <= avg_low * 1.025:
                continue
            post_second_high = max(candle.high for candle in base[second_rel:])
            if post_second_high < neckline * 0.985:
                continue
            breakout = candles[breakout_index]
            previous = candles[breakout_index - 1]
            if breakout.close <= neckline * 1.0015 or previous.close > neckline * 1.00075:
                continue
            entry = breakout.close
            target = neckline + (neckline - avg_low)
            if target <= entry * 1.001:
                continue
            height_pct = cup.pct((neckline - avg_low) / neckline)
            if height_pct < 2.5 or height_pct > 35.0:
                continue
            breakout_margin = cup.pct((entry - neckline) / neckline)
            score = double_bottom_score(height_pct, low_mismatch_pct, breakout_margin, pattern_bars)
            rows.append(
                build_case(
                    pattern="double_bottom",
                    symbol=symbol,
                    timeframe=timeframe,
                    candles=candles,
                    breakout_index=breakout_index,
                    entry=entry,
                    target=target,
                    horizon_bars=min(max_horizon_bars, max(pattern_bars * 2, 24 if timeframe == "1h" else 12)),
                    pattern_bars=pattern_bars,
                    setup_bars=None,
                    consolidation_bars=None,
                    structure_height_pct=height_pct,
                    structure_depth_pct=height_pct,
                    breakout_margin_pct=breakout_margin,
                    compression_pct=None,
                    prior_move_pct=None,
                    pullback_pct=low_mismatch_pct,
                    touch_count=2,
                    upper_slope_pct_per_bar=None,
                    lower_slope_pct_per_bar=None,
                    score=score,
                )
            )
    return rows


def build_case(
    *,
    pattern: str,
    symbol: str,
    timeframe: str,
    candles: list[cup.Candle],
    breakout_index: int,
    entry: float,
    target: float,
    horizon_bars: int,
    pattern_bars: int,
    setup_bars: int | None,
    consolidation_bars: int | None,
    structure_height_pct: float,
    structure_depth_pct: float,
    breakout_margin_pct: float,
    compression_pct: float | None,
    prior_move_pct: float | None,
    pullback_pct: float | None,
    touch_count: int | None,
    upper_slope_pct_per_bar: float | None,
    lower_slope_pct_per_bar: float | None,
    score: float,
) -> PatternCase:
    outcome = cup.evaluate_outcome(
        candles=candles,
        breakout_index=breakout_index,
        entry_price=entry,
        target_price=target,
        horizon_bars=horizon_bars,
        interval=timeframe,
    )
    indicators = cup.build_indicator_snapshot(candles, breakout_index, timeframe)
    return PatternCase(
        pattern=pattern,
        side="long",
        symbol=symbol,
        timeframe=timeframe,
        validation_time=cup.ms_iso(candles[breakout_index].start_time),
        status=str(outcome["status"]),
        target_hit_horizon=bool(outcome["target_hit_horizon"]),
        target_hit_ever=bool(outcome["target_hit_ever"]),
        target_progress_horizon_pct=cup.round_float(outcome["target_progress_horizon_pct"]),
        target_progress_ever_pct=cup.round_float(outcome["target_progress_ever_pct"]),
        adverse_before_target_or_peak_pct=cup.round_float(outcome["adverse_before_target_or_peak_pct"]),
        adverse_before_target_or_horizon_pct=cup.round_float(outcome["adverse_before_target_or_horizon_pct"]),
        max_favorable_horizon_pct=cup.round_float(outcome["max_favorable_horizon_pct"]),
        max_up_if_no_target_pct=cup.round_optional(outcome["max_up_if_no_target_pct"]),
        end_return_horizon_pct=cup.round_float(outcome["end_return_horizon_pct"]),
        bars_to_target_horizon=outcome["bars_to_target_horizon"],
        time_to_target_horizon=cup.bars_to_duration_label(outcome["bars_to_target_horizon"], timeframe),
        horizon_bars=int(outcome["horizon_bars"]),
        available_future_bars=int(outcome["available_future_bars"]),
        pattern_bars=pattern_bars,
        setup_bars=setup_bars,
        consolidation_bars=consolidation_bars,
        entry_price=cup.round_price(entry),
        target_price=cup.round_price(target),
        target_pct_from_entry=cup.round_float(cup.pct((target - entry) / entry)),
        structure_height_pct=cup.round_float(structure_height_pct),
        structure_depth_pct=cup.round_float(structure_depth_pct),
        breakout_margin_pct=cup.round_float(breakout_margin_pct),
        compression_pct=cup.round_optional(compression_pct),
        prior_move_pct=cup.round_optional(prior_move_pct),
        pullback_pct=cup.round_optional(pullback_pct),
        touch_count=touch_count,
        upper_slope_pct_per_bar=cup.round_optional(upper_slope_pct_per_bar),
        lower_slope_pct_per_bar=cup.round_optional(lower_slope_pct_per_bar),
        score=cup.round_float(score),
        rsi14=cup.round_optional(indicators["rsi14"]),
        volume_ratio20=cup.round_optional(indicators["volume_ratio20"]),
        volume_zscore20=cup.round_optional(indicators["volume_zscore20"]),
        atr14_pct=cup.round_optional(indicators["atr14_pct"]),
        sma20_distance_pct=cup.round_optional(indicators["sma20_distance_pct"]),
        prior_24h_return_pct=cup.round_optional(indicators["prior_24h_return_pct"]),
    )


def dedupe_cases(rows: list[PatternCase]) -> list[PatternCase]:
    grouped: dict[tuple[str, str, str], list[PatternCase]] = defaultdict(list)
    for row in rows:
        grouped[(row.pattern, row.symbol, row.timeframe)].append(row)
    selected: list[PatternCase] = []
    for (_pattern, _symbol, timeframe), bucket in grouped.items():
        bucket.sort(key=lambda item: item.validation_time)
        cluster: list[PatternCase] = []
        last_time: int | None = None
        cooldown_bars = 12 if timeframe == "1h" else 6
        cooldown_ms = cooldown_bars * cup.INTERVAL_MS[timeframe]
        for row in bucket:
            current = cup.iso_ms(row.validation_time)
            if last_time is None or current - last_time > cooldown_ms:
                if cluster:
                    selected.append(best_case(cluster))
                cluster = [row]
                last_time = current
                continue
            cluster.append(row)
            last_time = current
        if cluster:
            selected.append(best_case(cluster))
    return selected


def best_case(rows: list[PatternCase]) -> PatternCase:
    return sorted(
        rows,
        key=lambda item: (item.score, item.pattern_bars, -item.target_pct_from_entry),
        reverse=True,
    )[0]


def build_target_level_cases(
    cases: list[PatternCase],
    series: dict[tuple[str, str], list[cup.Candle]],
    target_fractions: list[float],
) -> list[TargetLevelCase]:
    index_maps = cup.build_start_time_index(series)
    rows: list[TargetLevelCase] = []
    for case in cases:
        key = (case.symbol, case.timeframe)
        candles = series.get(key, [])
        breakout_index = index_maps.get(key, {}).get(cup.iso_ms(case.validation_time))
        if breakout_index is None:
            continue
        for fraction in target_fractions:
            target_pct = case.target_pct_from_entry * fraction / 100.0
            target_price = case.entry_price * (1.0 + target_pct / 100.0)
            outcome = cup.evaluate_outcome(
                candles=candles,
                breakout_index=breakout_index,
                entry_price=case.entry_price,
                target_price=target_price,
                horizon_bars=case.horizon_bars,
                interval=case.timeframe,
            )
            rows.append(
                TargetLevelCase(
                    pattern=case.pattern,
                    symbol=case.symbol,
                    timeframe=case.timeframe,
                    validation_time=case.validation_time,
                    target_fraction_pct=cup.round_float(fraction),
                    target_hit=cup.yes_no(bool(outcome["target_hit_horizon"])),
                    time_to_target=cup.bars_to_duration_label(outcome["bars_to_target_horizon"], case.timeframe),
                    bars_to_target=outcome["bars_to_target_horizon"],
                    target_pct_from_entry=cup.round_float(target_pct),
                    target_price=cup.round_price(target_price),
                    max_up_if_not_hit_pct=cup.round_optional(outcome["max_up_if_no_target_pct"]),
                    adverse_before_target_or_peak_pct=cup.round_float(outcome["adverse_before_target_or_peak_pct"]),
                )
            )
    return rows


def summarize_target_level_cases(rows: list[TargetLevelCase]) -> list[TargetLevelSummary]:
    grouped: dict[tuple[str, str, float], list[TargetLevelCase]] = defaultdict(list)
    for row in rows:
        grouped[(row.pattern, "all", row.target_fraction_pct)].append(row)
        grouped[(row.pattern, row.timeframe, row.target_fraction_pct)].append(row)
    summaries: list[TargetLevelSummary] = []
    for (pattern, timeframe, fraction), bucket in sorted(grouped.items()):
        hits = [row for row in bucket if row.target_hit == "yes"]
        misses = [row for row in bucket if row.target_hit != "yes"]
        hit_drawdowns = [max(0.0, -row.adverse_before_target_or_peak_pct) for row in hits]
        summaries.append(
            TargetLevelSummary(
                pattern=pattern,
                timeframe=timeframe,
                target_fraction_pct=fraction,
                case_count=len(bucket),
                hit_count=len(hits),
                hit_rate_pct=cup.round_float(100.0 * len(hits) / len(bucket)) if bucket else 0.0,
                median_target_pct_from_entry=cup.round_optional(cup.median(row.target_pct_from_entry for row in bucket)),
                median_bars_to_target=cup.round_optional(cup.median(row.bars_to_target for row in hits if row.bars_to_target is not None)),
                median_adverse_before_target_or_peak_pct=cup.round_optional(
                    cup.median(row.adverse_before_target_or_peak_pct for row in bucket)
                ),
                stop_needed_for_75pct_hits_pct=cup.round_optional(cup.percentile(hit_drawdowns, 75.0)),
                stop_needed_for_90pct_hits_pct=cup.round_optional(cup.percentile(hit_drawdowns, 90.0)),
                median_max_up_if_not_hit_pct=cup.round_optional(
                    cup.median(row.max_up_if_not_hit_pct for row in misses if row.max_up_if_not_hit_pct is not None)
                ),
            )
        )
    return summaries


def build_stop_grid_summaries(
    cases: list[PatternCase],
    series: dict[tuple[str, str], list[cup.Candle]],
    target_fractions: list[float],
    stop_losses: list[float],
) -> list[StopGridSummary]:
    index_maps = cup.build_start_time_index(series)
    grouped: dict[tuple[str, str, float, float], list[tuple[str, float]]] = defaultdict(list)
    for case in cases:
        key = (case.symbol, case.timeframe)
        breakout_index = index_maps.get(key, {}).get(cup.iso_ms(case.validation_time))
        if breakout_index is None:
            continue
        candles = series[key]
        for fraction in target_fractions:
            target_pct = case.target_pct_from_entry * fraction / 100.0
            for stop in stop_losses:
                outcome, exit_return = cup.simulate_target_stop(
                    candles=candles,
                    breakout_index=breakout_index,
                    entry_price=case.entry_price,
                    target_pct=target_pct,
                    stop_loss_pct=stop,
                    horizon_bars=case.horizon_bars,
                    interval=case.timeframe,
                )
                grouped[(case.pattern, case.timeframe, fraction, stop)].append((outcome, exit_return))
                grouped[(case.pattern, "all", fraction, stop)].append((outcome, exit_return))
    summaries: list[StopGridSummary] = []
    for (pattern, timeframe, fraction, stop), outcomes in sorted(grouped.items()):
        summaries.append(make_stop_summary(pattern, timeframe, fraction, stop, outcomes))
    return summaries


def build_trade_case_rows(cases: list[PatternCase], target_rows: list[TargetLevelCase]) -> list[TradeCaseRow]:
    level_map = {
        (row.pattern, row.symbol, row.timeframe, row.validation_time, row.target_fraction_pct): row
        for row in target_rows
    }
    rows: list[TradeCaseRow] = []
    for case in cases:
        t33 = level_map.get((case.pattern, case.symbol, case.timeframe, case.validation_time, 33.0))
        t50 = level_map.get((case.pattern, case.symbol, case.timeframe, case.validation_time, 50.0))
        rows.append(
            TradeCaseRow(
                pattern=case.pattern,
                symbol=case.symbol,
                timeframe=case.timeframe,
                validation_time=case.validation_time,
                target_theorique_atteinte=cup.oui_non(case.target_hit_horizon),
                temps_apres_validation=case.time_to_target_horizon if case.target_hit_horizon else "",
                hausse_max_si_target_non_atteinte_pct=case.max_up_if_no_target_pct if not case.target_hit_horizon else None,
                baisse_max_avant_target_ou_point_haut_pct=case.adverse_before_target_or_peak_pct,
                target_theorique_pct=case.target_pct_from_entry,
                entry_price=case.entry_price,
                target_theorique_price=case.target_price,
                target_33_pct_atteinte="O" if t33 and t33.target_hit == "yes" else "N",
                target_33_pct_temps=t33.time_to_target if t33 else "",
                target_33_pct_baisse_avant_pct=t33.adverse_before_target_or_peak_pct if t33 else 0.0,
                target_50_pct_atteinte="O" if t50 and t50.target_hit == "yes" else "N",
                target_50_pct_temps=t50.time_to_target if t50 else "",
                target_50_pct_baisse_avant_pct=t50.adverse_before_target_or_peak_pct if t50 else 0.0,
                rsi14=case.rsi14,
                volume_ratio20=case.volume_ratio20,
                atr14_pct=case.atr14_pct,
                sma20_distance_pct=case.sma20_distance_pct,
                prior_24h_return_pct=case.prior_24h_return_pct,
                structure_height_pct=case.structure_height_pct,
                structure_depth_pct=case.structure_depth_pct,
                breakout_margin_pct=case.breakout_margin_pct,
                score=case.score,
            )
        )
    return rows


def build_indicator_correlations(cases: list[PatternCase]) -> list[IndicatorCorrelationRow]:
    indicators = [
        "rsi14",
        "volume_ratio20",
        "volume_zscore20",
        "atr14_pct",
        "sma20_distance_pct",
        "prior_24h_return_pct",
        "target_pct_from_entry",
        "structure_height_pct",
        "structure_depth_pct",
        "breakout_margin_pct",
        "compression_pct",
        "prior_move_pct",
        "pullback_pct",
        "score",
    ]
    rows: list[IndicatorCorrelationRow] = []
    patterns = sorted({case.pattern for case in cases})
    for pattern in patterns:
        pattern_cases = [case for case in cases if case.pattern == pattern]
        for timeframe in ("all", "1h", "4h"):
            scoped = pattern_cases if timeframe == "all" else [case for case in pattern_cases if case.timeframe == timeframe]
            for indicator in indicators:
                pairs: list[tuple[float, PatternCase]] = []
                for case in scoped:
                    value = getattr(case, indicator)
                    if value is None:
                        continue
                    numeric = float(value)
                    if math.isfinite(numeric):
                        pairs.append((numeric, case))
                if len(pairs) < 20:
                    continue
                values = [value for value, _case in pairs]
                hit_values = [1.0 if case.target_hit_horizon else 0.0 for _value, case in pairs]
                progress_values = [case.target_progress_horizon_pct for _value, case in pairs]
                mfe_values = [case.max_favorable_horizon_pct for _value, case in pairs]
                bottom, top = cup.terciles(pairs)
                rows.append(
                    IndicatorCorrelationRow(
                        pattern=pattern,
                        timeframe=timeframe,
                        indicator=indicator,
                        sample_count=len(pairs),
                        corr_target_hit=cup.round_optional(cup.pearson(values, hit_values)),
                        corr_target_progress=cup.round_optional(cup.pearson(values, progress_values)),
                        corr_max_favorable=cup.round_optional(cup.pearson(values, mfe_values)),
                        hit_rate_bottom_tercile_pct=cup.round_optional(hit_rate(bottom)),
                        hit_rate_top_tercile_pct=cup.round_optional(hit_rate(top)),
                        median_progress_bottom_tercile_pct=cup.round_optional(
                            cup.median(case.target_progress_horizon_pct for _value, case in bottom)
                        ),
                        median_progress_top_tercile_pct=cup.round_optional(
                            cup.median(case.target_progress_horizon_pct for _value, case in top)
                        ),
                    )
                )
    return rows


def run_filtered_replay(
    *,
    cases: list[PatternCase],
    series: dict[tuple[str, str], list[cup.Candle]],
    target_fractions: list[float],
    stop_losses: list[float],
    split_time: str,
) -> tuple[list[FilterReplayRow], list[SelectedTradeRow], list[dict[str, Any]]]:
    split_ms = cup.iso_ms(split_time)
    index_maps = cup.build_start_time_index(series)
    all_rows: list[FilterReplayRow] = []
    selected_trades: list[SelectedTradeRow] = []
    selected_configs: list[dict[str, Any]] = []
    patterns = sorted({case.pattern for case in cases})
    for pattern in patterns:
        pattern_cases = [case for case in cases if case.pattern == pattern]
        candidate_sets = build_filter_candidates(pattern_cases)
        config_rows: list[tuple[dict[str, Any], list[tuple[PatternCase, str, float]]]] = []
        for filter_name, filtered in candidate_sets:
            if len(filtered) < 20:
                continue
            for fraction in target_fractions:
                for stop in stop_losses:
                    trade_outcomes = simulate_cases(filtered, series, index_maps, fraction, stop, split_ms)
                    split_summaries = {
                        split: summarize_outcomes(pattern, filter_name, fraction, stop, split, values)
                        for split, values in split_trade_groups(trade_outcomes, split_ms).items()
                    }
                    if "all" not in split_summaries:
                        continue
                    robust = is_robust_positive(split_summaries)
                    for split, summary in split_summaries.items():
                        all_rows.append(
                            FilterReplayRow(
                                pattern=pattern,
                                filter_name=filter_name,
                                target_fraction_pct=fraction,
                                stop_loss_pct=stop,
                                split=split,
                                trade_count=summary.trade_count,
                                target_count=summary.target_count,
                                stop_count=summary.stop_count,
                                timeout_count=summary.timeout_count,
                                target_rate_pct=summary.target_rate_pct,
                                stop_rate_pct=summary.stop_rate_pct,
                                avg_exit_return_pct=summary.avg_exit_return_pct,
                                median_exit_return_pct=summary.median_exit_return_pct,
                                profit_factor=summary.profit_factor,
                                robust_positive=robust,
                                selected=False,
                            )
                        )
                    all_summary = split_summaries["all"]
                    train_summary = split_summaries.get("train")
                    test_summary = split_summaries.get("test")
                    config_rows.append(
                        (
                            {
                                "pattern": pattern,
                                "filter_name": filter_name,
                                "target_fraction_pct": fraction,
                                "stop_loss_pct": stop,
                                "robust_positive": robust,
                                "all_trades": all_summary.trade_count,
                                "all_avg_exit_return_pct": all_summary.avg_exit_return_pct,
                                "all_target_rate_pct": all_summary.target_rate_pct,
                                "all_stop_rate_pct": all_summary.stop_rate_pct,
                                "all_profit_factor": all_summary.profit_factor,
                                "train_trades": train_summary.trade_count if train_summary else 0,
                                "train_avg_exit_return_pct": train_summary.avg_exit_return_pct if train_summary else None,
                                "test_trades": test_summary.trade_count if test_summary else 0,
                                "test_avg_exit_return_pct": test_summary.avg_exit_return_pct if test_summary else None,
                                "test_target_rate_pct": test_summary.target_rate_pct if test_summary else None,
                                "test_stop_rate_pct": test_summary.stop_rate_pct if test_summary else None,
                            },
                            trade_outcomes,
                        )
                    )
        selected = select_config(config_rows)
        if selected is None:
            continue
        selected_config, trade_outcomes = selected
        selected_configs.append(selected_config)
        mark_selected(all_rows, selected_config)
        selected_trades.extend(
            build_selected_trade_rows(selected_config, trade_outcomes, split_ms)
        )
    return all_rows, selected_trades, selected_configs


def build_filter_candidates(cases: list[PatternCase]) -> list[tuple[str, list[PatternCase]]]:
    rows: list[tuple[str, list[PatternCase]]] = [("all", cases)]
    for timeframe in ("1h", "4h"):
        scoped = [case for case in cases if case.timeframe == timeframe]
        if len(scoped) < 20:
            continue
        q = quantiles(scoped)
        rows.append((f"{timeframe}_target_low_q33", [case for case in scoped if le(case.target_pct_from_entry, q, "target_pct_from_entry", 33)]))
        rows.append((f"{timeframe}_target_low_q50", [case for case in scoped if le(case.target_pct_from_entry, q, "target_pct_from_entry", 50)]))
        rows.append((f"{timeframe}_target_low_q33_breakout_high_q66", [
            case for case in scoped
            if le(case.target_pct_from_entry, q, "target_pct_from_entry", 33)
            and ge(case.breakout_margin_pct, q, "breakout_margin_pct", 66)
        ]))
        rows.append((f"{timeframe}_target_low_q33_depth_low_q33_breakout_high_q66_volume_high_q66", [
            case for case in scoped
            if le(case.target_pct_from_entry, q, "target_pct_from_entry", 33)
            and le(case.structure_depth_pct, q, "structure_depth_pct", 33)
            and ge(case.breakout_margin_pct, q, "breakout_margin_pct", 66)
            and ge(case.volume_ratio20, q, "volume_ratio20", 66)
        ]))
        rows.append((f"{timeframe}_target_low_q50_depth_low_q50_breakout_high_q50", [
            case for case in scoped
            if le(case.target_pct_from_entry, q, "target_pct_from_entry", 50)
            and le(case.structure_depth_pct, q, "structure_depth_pct", 50)
            and ge(case.breakout_margin_pct, q, "breakout_margin_pct", 50)
        ]))
        rows.append((f"{timeframe}_score_high_q66_volume_high_q50", [
            case for case in scoped
            if ge(case.score, q, "score", 66)
            and ge(case.volume_ratio20, q, "volume_ratio20", 50)
        ]))
        rows.append((f"{timeframe}_target_low_q50_score_high_q50_volume_high_q50", [
            case for case in scoped
            if le(case.target_pct_from_entry, q, "target_pct_from_entry", 50)
            and ge(case.score, q, "score", 50)
            and ge(case.volume_ratio20, q, "volume_ratio20", 50)
        ]))
    deduped: list[tuple[str, list[PatternCase]]] = []
    seen: set[str] = set()
    for name, bucket in rows:
        if name in seen:
            continue
        seen.add(name)
        deduped.append((name, bucket))
    return deduped


def quantiles(cases: list[PatternCase]) -> dict[tuple[str, int], float]:
    fields = [
        "target_pct_from_entry",
        "structure_depth_pct",
        "breakout_margin_pct",
        "volume_ratio20",
        "atr14_pct",
        "score",
    ]
    result: dict[tuple[str, int], float] = {}
    for field in fields:
        values = [getattr(case, field) for case in cases if getattr(case, field) is not None]
        for value in (33, 50, 66):
            q = cup.percentile(values, float(value))
            if q is not None:
                result[(field, value)] = q
    return result


def le(value: float | None, q: dict[tuple[str, int], float], field: str, pct_value: int) -> bool:
    threshold = q.get((field, pct_value))
    return value is not None and threshold is not None and value <= threshold


def ge(value: float | None, q: dict[tuple[str, int], float], field: str, pct_value: int) -> bool:
    threshold = q.get((field, pct_value))
    return value is not None and threshold is not None and value >= threshold


def simulate_cases(
    cases: list[PatternCase],
    series: dict[tuple[str, str], list[cup.Candle]],
    index_maps: dict[tuple[str, str], dict[int, int]],
    fraction: float,
    stop: float,
    split_ms: int,
) -> list[tuple[PatternCase, str, float]]:
    rows: list[tuple[PatternCase, str, float]] = []
    for case in cases:
        key = (case.symbol, case.timeframe)
        candles = series.get(key)
        breakout_index = index_maps.get(key, {}).get(cup.iso_ms(case.validation_time))
        if candles is None or breakout_index is None:
            continue
        outcome, exit_return = cup.simulate_target_stop(
            candles=candles,
            breakout_index=breakout_index,
            entry_price=case.entry_price,
            target_pct=case.target_pct_from_entry * fraction / 100.0,
            stop_loss_pct=stop,
            horizon_bars=case.horizon_bars,
            interval=case.timeframe,
        )
        rows.append((case, outcome, exit_return))
    return rows


def split_trade_groups(
    rows: list[tuple[PatternCase, str, float]],
    split_ms: int,
) -> dict[str, list[tuple[PatternCase, str, float]]]:
    grouped = {"all": rows, "train": [], "test": []}
    for row in rows:
        case = row[0]
        if cup.iso_ms(case.validation_time) < split_ms:
            grouped["train"].append(row)
        else:
            grouped["test"].append(row)
    return grouped


def summarize_outcomes(
    pattern: str,
    filter_name: str,
    fraction: float,
    stop: float,
    split: str,
    rows: list[tuple[PatternCase, str, float]],
) -> StopGridSummary:
    return make_stop_summary(
        pattern=pattern,
        timeframe=split,
        fraction=fraction,
        stop=stop,
        outcomes=[(outcome, exit_return) for _case, outcome, exit_return in rows],
    )


def make_stop_summary(
    pattern: str,
    timeframe: str,
    fraction: float,
    stop: float,
    outcomes: list[tuple[str, float]],
) -> StopGridSummary:
    if not outcomes:
        return StopGridSummary(
            pattern=pattern,
            timeframe=timeframe,
            target_fraction_pct=fraction,
            stop_loss_pct=stop,
            trade_count=0,
            target_count=0,
            stop_count=0,
            timeout_count=0,
            target_rate_pct=0.0,
            stop_rate_pct=0.0,
            avg_exit_return_pct=0.0,
            median_exit_return_pct=None,
            profit_factor=None,
        )
    target_count = sum(1 for outcome, _return in outcomes if outcome == "target")
    stop_count = sum(1 for outcome, _return in outcomes if outcome == "stop")
    timeout_count = sum(1 for outcome, _return in outcomes if outcome == "timeout")
    returns = [exit_return for _outcome, exit_return in outcomes]
    return StopGridSummary(
        pattern=pattern,
        timeframe=timeframe,
        target_fraction_pct=fraction,
        stop_loss_pct=stop,
        trade_count=len(outcomes),
        target_count=target_count,
        stop_count=stop_count,
        timeout_count=timeout_count,
        target_rate_pct=cup.round_float(100.0 * target_count / len(outcomes)),
        stop_rate_pct=cup.round_float(100.0 * stop_count / len(outcomes)),
        avg_exit_return_pct=cup.round_float(sum(returns) / len(returns)),
        median_exit_return_pct=cup.round_optional(cup.median(returns)),
        profit_factor=cup.round_optional(profit_factor(returns)),
    )


def is_robust_positive(summaries: dict[str, StopGridSummary]) -> bool:
    all_summary = summaries.get("all")
    train = summaries.get("train")
    test = summaries.get("test")
    if all_summary is None or train is None or test is None:
        return False
    return (
        all_summary.trade_count >= 30
        and train.trade_count >= 10
        and test.trade_count >= 10
        and all_summary.avg_exit_return_pct > 0
        and train.avg_exit_return_pct > 0
        and test.avg_exit_return_pct > 0
    )


def select_config(
    rows: list[tuple[dict[str, Any], list[tuple[PatternCase, str, float]]]]
) -> tuple[dict[str, Any], list[tuple[PatternCase, str, float]]] | None:
    if not rows:
        return None
    robust = [row for row in rows if row[0]["robust_positive"]]
    pool = robust if robust else rows
    return sorted(
        pool,
        key=lambda row: (
            bool(row[0]["robust_positive"]),
            row[0]["all_avg_exit_return_pct"],
            row[0]["test_avg_exit_return_pct"] if row[0]["test_avg_exit_return_pct"] is not None else -999.0,
            row[0]["all_trades"],
        ),
        reverse=True,
    )[0]


def mark_selected(rows: list[FilterReplayRow], selected: dict[str, Any]) -> None:
    for row in rows:
        if (
            row.pattern == selected["pattern"]
            and row.filter_name == selected["filter_name"]
            and row.target_fraction_pct == selected["target_fraction_pct"]
            and row.stop_loss_pct == selected["stop_loss_pct"]
        ):
            row.selected = True


def build_selected_trade_rows(
    selected: dict[str, Any],
    trade_outcomes: list[tuple[PatternCase, str, float]],
    split_ms: int,
) -> list[SelectedTradeRow]:
    rows: list[SelectedTradeRow] = []
    for case, outcome, exit_return in trade_outcomes:
        rows.append(
            SelectedTradeRow(
                pattern=case.pattern,
                filter_name=selected["filter_name"],
                target_fraction_pct=selected["target_fraction_pct"],
                stop_loss_pct=selected["stop_loss_pct"],
                symbol=case.symbol,
                timeframe=case.timeframe,
                validation_time=case.validation_time,
                split="train" if cup.iso_ms(case.validation_time) < split_ms else "test",
                outcome=outcome,
                exit_return_pct=cup.round_float(exit_return),
                target_pct_from_entry=cup.round_float(case.target_pct_from_entry * selected["target_fraction_pct"] / 100.0),
                adverse_before_target_or_peak_pct=case.adverse_before_target_or_peak_pct,
                volume_ratio20=case.volume_ratio20,
                atr14_pct=case.atr14_pct,
                breakout_margin_pct=case.breakout_margin_pct,
                structure_depth_pct=case.structure_depth_pct,
                score=case.score,
            )
        )
    return rows


def build_summary(cases: list[PatternCase], coverage_rows: list[cup.CoverageRow]) -> dict[str, Any]:
    summary = {
        "series_count": len(coverage_rows),
        "symbol_count": len({row.symbol for row in coverage_rows}),
        "total_candles": sum(row.candle_count for row in coverage_rows),
        "case_count": len(cases),
        "by_pattern_timeframe": [],
    }
    grouped: dict[tuple[str, str], list[PatternCase]] = defaultdict(list)
    for case in cases:
        grouped[(case.pattern, "all")].append(case)
        grouped[(case.pattern, case.timeframe)].append(case)
    for (pattern, timeframe), bucket in sorted(grouped.items()):
        hits = sum(1 for case in bucket if case.target_hit_horizon)
        summary["by_pattern_timeframe"].append(
            {
                "pattern": pattern,
                "timeframe": timeframe,
                "case_count": len(bucket),
                "target_hit_rate_pct": cup.round_float(100.0 * hits / len(bucket)) if bucket else 0.0,
                "target_ever_rate_pct": cup.round_float(
                    100.0 * sum(1 for case in bucket if case.target_hit_ever) / len(bucket)
                ) if bucket else 0.0,
                "median_target_pct": cup.round_optional(cup.median(case.target_pct_from_entry for case in bucket)),
                "median_mfe_pct": cup.round_optional(cup.median(case.max_favorable_horizon_pct for case in bucket)),
                "median_adverse_pct": cup.round_optional(cup.median(case.adverse_before_target_or_peak_pct for case in bucket)),
                "median_end_return_pct": cup.round_optional(cup.median(case.end_return_horizon_pct for case in bucket)),
            }
        )
    return summary


def write_markdown_report(
    output_path: Path,
    *,
    payload: dict[str, Any],
    cases: list[PatternCase],
    target_level_summaries: list[TargetLevelSummary],
    filter_rows: list[FilterReplayRow],
    selected_configs: list[dict[str, Any]],
    top_rows: int,
) -> None:
    lines: list[str] = [
        "# Chart pattern skill replay",
        "",
        "Statut: `research_only_no_live_change`.",
        "",
        "## Assumptions",
        "",
        "- Long breakout only for all patterns in this run.",
        "- Timeframes: `1h`, `4h` unless overridden.",
        "- Theoretical targets use textbook measured-move rules; practical replay also tests partial targets.",
        "- Replay is candle-level and conservative: if TP and SL touch in the same candle, SL is counted first.",
        "- No fees, slippage, sizing, TRIDENT gates, order book liquidity or full-bot interaction are included.",
        "",
        "## Coverage",
        "",
        f"- Series: `{payload['summary']['series_count']}`.",
        f"- Symbols: `{payload['summary']['symbol_count']}`.",
        f"- Unique candles: `{payload['summary']['total_candles']}`.",
        f"- Validated cases after dedupe: `{payload['summary']['case_count']}`.",
        "",
        "## Raw pattern summary",
        "",
        "| Pattern | TF | Cases | Target hit | Target ever | Med target | Med MFE | Med adverse | Med end |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in payload["summary"]["by_pattern_timeframe"]:
        lines.append(
            "| {pattern} | {timeframe} | {case_count} | {target_hit_rate_pct:.2f}% | {target_ever_rate_pct:.2f}% | {target} | {mfe} | {adverse} | {end} |".format(
                pattern=row["pattern"],
                timeframe=row["timeframe"],
                case_count=row["case_count"],
                target_hit_rate_pct=row["target_hit_rate_pct"],
                target_ever_rate_pct=row["target_ever_rate_pct"],
                target=fmt_pct(row["median_target_pct"]),
                mfe=fmt_pct(row["median_mfe_pct"]),
                adverse=fmt_pct(row["median_adverse_pct"]),
                end=fmt_pct(row["median_end_return_pct"]),
            )
        )
    lines.extend(["", "## Partial targets", ""])
    lines.extend([
        "| Pattern | TF | Target | Cases | Hit rate | Med target | Med bars | Med adverse | SL 75% hits | SL 90% hits |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in sorted(target_level_summaries, key=lambda item: (item.pattern, item.timeframe, item.target_fraction_pct)):
        if row.timeframe != "all":
            continue
        lines.append(
            f"| {row.pattern} | {row.timeframe} | {row.target_fraction_pct:.0f}% | {row.case_count} | {row.hit_rate_pct:.2f}% | {fmt_pct(row.median_target_pct_from_entry)} | {fmt_num(row.median_bars_to_target)} | {fmt_pct(row.median_adverse_before_target_or_peak_pct)} | {fmt_pct(row.stop_needed_for_75pct_hits_pct)} | {fmt_pct(row.stop_needed_for_90pct_hits_pct)} |"
        )
    lines.extend(["", "## Selected filtered replays", ""])
    lines.extend([
        "| Pattern | Filter | Target | SL | Robust | All trades | All avg | All TP | Train avg | Test trades | Test avg | Test TP | Test stop |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for cfg in selected_configs:
        lines.append(
            "| {pattern} | `{filter_name}` | {target:.0f}% | {stop:.2f}% | {robust} | {all_trades} | {all_avg} | {all_tp} | {train_avg} | {test_trades} | {test_avg} | {test_tp} | {test_stop} |".format(
                pattern=cfg["pattern"],
                filter_name=cfg["filter_name"],
                target=cfg["target_fraction_pct"],
                stop=cfg["stop_loss_pct"],
                robust="yes" if cfg["robust_positive"] else "no",
                all_trades=cfg["all_trades"],
                all_avg=fmt_pct(cfg["all_avg_exit_return_pct"]),
                all_tp=fmt_pct(cfg["all_target_rate_pct"]),
                train_avg=fmt_pct(cfg["train_avg_exit_return_pct"]),
                test_trades=cfg["test_trades"],
                test_avg=fmt_pct(cfg["test_avg_exit_return_pct"]),
                test_tp=fmt_pct(cfg["test_target_rate_pct"]),
                test_stop=fmt_pct(cfg["test_stop_rate_pct"]),
            )
        )
    lines.extend(["", "## Top filtered configs", ""])
    lines.extend([
        "| Pattern | Filter | Target | SL | Split | Trades | TP | Stop | Avg exit | PF | Robust | Selected |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ])
    ranked = sorted(
        [row for row in filter_rows if row.split == "all"],
        key=lambda item: (item.selected, item.robust_positive, item.avg_exit_return_pct, item.trade_count),
        reverse=True,
    )
    for row in ranked[:top_rows]:
        lines.append(
            f"| {row.pattern} | `{row.filter_name}` | {row.target_fraction_pct:.0f}% | {row.stop_loss_pct:.2f}% | {row.split} | {row.trade_count} | {row.target_rate_pct:.2f}% | {row.stop_rate_pct:.2f}% | {fmt_pct(row.avg_exit_return_pct)} | {fmt_num(row.profit_factor)} | {'yes' if row.robust_positive else 'no'} | {'yes' if row.selected else 'no'} |"
        )
    lines.extend([
        "",
        "## Files",
        "",
        "- `chart_pattern_cases.csv`: all validated cases.",
        "- `chart_pattern_trade_cases.csv`: per-case table with requested target/adverse/indicator fields.",
        "- `chart_pattern_target_level_cases.csv`: per-case partial target outcomes.",
        "- `chart_pattern_target_level_summary.csv`: partial target summary.",
        "- `chart_pattern_stop_grid_summary.csv`: raw TP/SL grid.",
        "- `chart_pattern_indicator_correlations.csv`: correlation scan.",
        "- `filtered_replay_grid.csv`: filtered replay grid.",
        "- `selected_filter_trades.csv`: per-trade replay rows for selected configs.",
        "- `chart_pattern_report.json`: full machine-readable payload.",
    ])
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def max_horizon(timeframe: str, max_horizon_days: float) -> int:
    return max(1, int(max_horizon_days * 86_400_000 / cup.INTERVAL_MS[timeframe]))


def rectangle_score(height_pct: float, breakout_margin_pct: float, upper_touches: int, lower_touches: int, pattern_bars: int) -> float:
    height_score = 1.0 - min(abs(height_pct - 8.0) / 16.0, 1.0)
    touch_score = min((upper_touches + lower_touches) / 8.0, 1.0)
    breakout_score = min(max(breakout_margin_pct / 2.0, 0.0), 1.0)
    length_score = min(math.log(max(pattern_bars, 2), 2) / 8.0, 1.0) * 0.25
    return height_score + touch_score + breakout_score + length_score


def triangle_score(compression_pct: float, height_pct: float, breakout_margin_pct: float, pattern_bars: int) -> float:
    compression_score = min(max(compression_pct / 55.0, 0.0), 1.0)
    height_score = 1.0 - min(abs(height_pct - 10.0) / 20.0, 1.0)
    breakout_score = min(max(breakout_margin_pct / 2.0, 0.0), 1.0)
    length_score = min(math.log(max(pattern_bars, 2), 2) / 8.0, 1.0) * 0.25
    return compression_score + height_score + breakout_score + length_score


def flag_score(impulse_return_pct: float, pullback_pct: float, cons_to_impulse: float, breakout_margin_pct: float) -> float:
    impulse_score = min(max(impulse_return_pct / 18.0, 0.0), 1.0)
    pullback_score = 1.0 - min(pullback_pct / 18.0, 1.0)
    tightness_score = 1.0 - min(cons_to_impulse / 0.55, 1.0)
    breakout_score = min(max(breakout_margin_pct / 2.0, 0.0), 1.0)
    return impulse_score + pullback_score + tightness_score + breakout_score


def double_bottom_score(height_pct: float, low_mismatch_pct: float, breakout_margin_pct: float, pattern_bars: int) -> float:
    height_score = 1.0 - min(abs(height_pct - 9.0) / 18.0, 1.0)
    symmetry_score = 1.0 - min(low_mismatch_pct / 5.5, 1.0)
    breakout_score = min(max(breakout_margin_pct / 2.0, 0.0), 1.0)
    length_score = min(math.log(max(pattern_bars, 2), 2) / 8.0, 1.0) * 0.25
    return height_score + symmetry_score + breakout_score + length_score


def hit_rate(rows: list[tuple[float, PatternCase]]) -> float | None:
    if not rows:
        return None
    return 100.0 * sum(1 for _value, case in rows if case.target_hit_horizon) / len(rows)


def profit_factor(returns: Iterable[float]) -> float | None:
    gains = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    if gains <= 0 and losses <= 0:
        return None
    if losses <= 0:
        return None
    return gains / losses


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "o"}


def parse_float(value: Any) -> float:
    parsed = cup.safe_float(value)
    return 0.0 if parsed is None else parsed


def parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return cup.safe_float(value)


def parse_int(value: Any) -> int:
    parsed = cup.safe_int(value)
    return 0 if parsed is None else parsed


def parse_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return cup.safe_int(value)


def fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(numeric):
        return "-"
    return f"{numeric:.2f}%"


def fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(numeric):
        return "-"
    if abs(numeric) >= 100:
        return f"{numeric:.1f}"
    if abs(numeric) >= 10:
        return f"{numeric:.2f}"
    return f"{numeric:.3f}"


def write_csv(path: Path, rows: list[Any]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
