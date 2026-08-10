#!/usr/bin/env python3
"""Scan native Hyperliquid candles for validated cup-and-handle breakouts.

Research-only: this script reads local candle datasets and writes a standalone
report. It does not change runtime config, deploy files, or fetch scripts.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}

DEFAULT_LENGTHS = {
    "1h": (24, 36, 48, 72, 96, 144, 192),
    "4h": (12, 18, 24, 36, 48, 72, 96),
}


@dataclass(slots=True)
class Candle:
    symbol: str
    interval: str
    start_time: int
    end_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    trade_count: float | None = None
    sources: set[str] = field(default_factory=set)


@dataclass(slots=True)
class CoverageRow:
    symbol: str
    timeframe: str
    candle_count: int
    first_candle: str
    last_candle: str
    coverage_days: float
    source_file_count: int
    gap_count: int
    max_gap_hours: float


@dataclass(slots=True)
class PatternCase:
    symbol: str
    timeframe: str
    breakout_time: str
    status: str
    target_hit_horizon: bool
    target_hit_ever: bool
    target_progress_horizon_pct: float
    target_progress_ever_pct: float
    adverse_before_target_or_horizon_pct: float
    adverse_before_target_ever_pct: float | None
    max_favorable_horizon_pct: float
    end_return_horizon_pct: float
    max_up_if_no_target_pct: float | None
    adverse_before_target_or_peak_pct: float
    peak_bars_horizon: int | None
    peak_time_horizon: str | None
    bars_to_target_horizon: int | None
    bars_to_target_ever: int | None
    time_to_target_horizon: str
    horizon_bars: int
    available_future_bars: int
    pattern_bars: int
    cup_bars: int
    handle_bars: int
    left_rim_time: str
    cup_low_time: str
    right_rim_time: str
    handle_low_time: str
    rim_price: float
    cup_low_price: float
    handle_low_price: float
    breakout_close: float
    target_price: float
    cup_depth_pct: float
    handle_depth_pct: float
    handle_depth_of_cup_pct: float
    breakout_margin_pct: float
    target_pct_from_breakout: float
    prior_trend_pct: float | None
    prior_trend_ok: bool
    rsi14: float | None
    volume_ratio20: float | None
    volume_zscore20: float | None
    atr14_pct: float | None
    sma20_distance_pct: float | None
    prior_24h_return_pct: float | None
    rim_mismatch_pct: float
    source_file_count: int
    score: float


@dataclass(slots=True)
class TradeCaseRow:
    symbol: str
    timeframe: str
    breakout_time: str
    target_theorique_atteinte: str
    temps_apres_validation: str
    hausse_max_si_target_non_atteinte_pct: float | None
    baisse_max_avant_target_ou_point_haut_pct: float
    target_theorique_pct: float
    breakout_close: float
    target_theorique_price: float
    target_50_pct_atteinte: str
    target_50_pct_temps: str
    target_50_pct_baisse_avant_pct: float
    target_75_pct_atteinte: str
    target_75_pct_temps: str
    target_75_pct_baisse_avant_pct: float
    rsi14: float | None
    volume_ratio20: float | None
    volume_zscore20: float | None
    atr14_pct: float | None
    sma20_distance_pct: float | None
    prior_24h_return_pct: float | None
    cup_depth_pct: float
    handle_depth_pct: float
    breakout_margin_pct: float
    rim_mismatch_pct: float
    score: float


@dataclass(slots=True)
class TargetLevelCase:
    symbol: str
    timeframe: str
    breakout_time: str
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


@dataclass(slots=True)
class IndicatorCorrelationRow:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-root",
        action="append",
        default=None,
        help="Root to scan for candle json.gz files. Can be repeated.",
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--min-cup-depth-pct", type=float, default=3.0)
    parser.add_argument("--max-cup-depth-pct", type=float, default=45.0)
    parser.add_argument("--min-handle-depth-pct", type=float, default=0.4)
    parser.add_argument("--max-rim-mismatch-pct", type=float, default=8.0)
    parser.add_argument("--breakout-buffer-pct", type=float, default=0.15)
    parser.add_argument("--max-horizon-days", type=float, default=14.0)
    parser.add_argument("--min-prior-trend-pct", type=float, default=3.0)
    parser.add_argument("--top-table-rows", type=int, default=80)
    parser.add_argument("--target-fractions", default="25,33,50,66,75,100")
    parser.add_argument("--stop-loss-pcts", default="1,2,3,4,5,6,8,10,12,15")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_roots = [Path(item) for item in (args.input_root or ["data", "server-data"])]
    target_fractions = parse_percent_list(str(args.target_fractions), default=[25.0, 33.0, 50.0, 66.0, 75.0, 100.0])
    stop_loss_pcts = parse_percent_list(str(args.stop_loss_pcts), default=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0])
    stamp = utc_stamp()
    output_dir = Path(args.output_dir or f"server-data/replay_reports/cup_handle_pattern_scan_{stamp}")
    output_dir.mkdir(parents=True, exist_ok=True)

    candle_files = discover_candle_files(input_roots)
    series, source_files = load_all_series(candle_files)
    coverage_rows = build_coverage_rows(series, source_files)

    raw_cases: list[PatternCase] = []
    for (symbol, interval), candles in sorted(series.items()):
        if len(candles) < min(DEFAULT_LENGTHS.get(interval, (999,))):
            continue
        raw_cases.extend(
            detect_cases(
                symbol=symbol,
                interval=interval,
                candles=candles,
                source_file_count=len(source_files[(symbol, interval)]),
                min_cup_depth_pct=float(args.min_cup_depth_pct),
                max_cup_depth_pct=float(args.max_cup_depth_pct),
                min_handle_depth_pct=float(args.min_handle_depth_pct),
                max_rim_mismatch_pct=float(args.max_rim_mismatch_pct),
                breakout_buffer_pct=float(args.breakout_buffer_pct),
                max_horizon_days=float(args.max_horizon_days),
                min_prior_trend_pct=float(args.min_prior_trend_pct),
            )
        )
    cases = dedupe_cases(raw_cases)
    cases.sort(key=lambda item: (item.breakout_time, item.symbol, item.timeframe))
    target_level_cases = build_target_level_cases(cases, series, target_fractions=target_fractions)
    target_level_summaries = summarize_target_level_cases(target_level_cases)
    stop_grid_summaries = build_stop_grid_summaries(
        cases,
        series,
        target_fractions=target_fractions,
        stop_loss_pcts=stop_loss_pcts,
    )
    indicator_correlations = build_indicator_correlations(cases)
    trade_case_rows = build_trade_case_rows(target_level_cases=target_level_cases, cases=cases)

    payload = {
        "kind": "cup_handle_pattern_scan",
        "generated_at": stamp,
        "decision": "research_only_no_live_change",
        "inputs": {
            "input_roots": [str(item) for item in input_roots],
            "candle_file_count": len(candle_files),
        },
        "parameters": {
            "timeframes": ["1h", "4h"],
            "lengths": DEFAULT_LENGTHS,
            "min_cup_depth_pct": float(args.min_cup_depth_pct),
            "max_cup_depth_pct": float(args.max_cup_depth_pct),
            "min_handle_depth_pct": float(args.min_handle_depth_pct),
            "max_rim_mismatch_pct": float(args.max_rim_mismatch_pct),
            "breakout_buffer_pct": float(args.breakout_buffer_pct),
            "max_horizon_days": float(args.max_horizon_days),
            "min_prior_trend_pct": float(args.min_prior_trend_pct),
            "target_fractions_pct": target_fractions,
            "stop_loss_pcts": stop_loss_pcts,
            "target_rule": "target_price = rim_price + (rim_price - cup_low_price)",
            "validation_rule": "close breakout above rim after a shallow upper-half handle",
        },
        "coverage": [asdict(item) for item in coverage_rows],
        "summary": build_summary(cases, coverage_rows),
        "target_level_summary": [asdict(item) for item in target_level_summaries],
        "stop_grid_summary": [asdict(item) for item in stop_grid_summaries],
        "indicator_correlations": [asdict(item) for item in indicator_correlations],
        "cases": [asdict(item) for item in cases],
    }

    write_csv(output_dir / "cup_handle_cases.csv", cases)
    write_csv(output_dir / "cup_handle_trade_cases.csv", trade_case_rows)
    write_csv(output_dir / "cup_handle_target_level_cases.csv", target_level_cases)
    write_csv(output_dir / "cup_handle_target_level_summary.csv", target_level_summaries)
    write_csv(output_dir / "cup_handle_stop_grid_summary.csv", stop_grid_summaries)
    write_csv(output_dir / "cup_handle_indicator_correlations.csv", indicator_correlations)
    write_csv(output_dir / "coverage.csv", coverage_rows)
    write_json(output_dir / "cup_handle_pattern_scan.json", payload)
    write_markdown(
        output_dir / "cup_handle_pattern_scan.md",
        payload,
        cases,
        target_level_summaries,
        stop_grid_summaries,
        indicator_correlations,
        int(args.top_table_rows),
    )
    write_cases_markdown(output_dir / "cup_handle_cases.md", cases)
    write_trade_cases_markdown(output_dir / "cup_handle_trade_cases.md", trade_case_rows)
    write_target_level_summary_markdown(output_dir / "cup_handle_target_level_summary.md", target_level_summaries)
    write_stop_grid_markdown(output_dir / "cup_handle_stop_grid_summary.md", stop_grid_summaries)
    write_indicator_correlations_markdown(output_dir / "cup_handle_indicator_correlations.md", indicator_correlations)
    print(output_dir)


def discover_candle_files(input_roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in input_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.json.gz"):
            match = candle_path_meta(path)
            if match is not None:
                files.append(path)
    return sorted(files)


def candle_path_meta(path: Path) -> tuple[str, str] | None:
    parts = path.parts
    for marker in ("candles", "api_candles"):
        if marker not in parts:
            continue
        index = parts.index(marker)
        if index + 2 >= len(parts):
            continue
        interval = parts[index + 1]
        if interval not in INTERVAL_MS:
            continue
        symbol = path.stem.removesuffix(".json").upper()
        return symbol, interval
    return None


def load_all_series(paths: list[Path]) -> tuple[dict[tuple[str, str], list[Candle]], dict[tuple[str, str], set[str]]]:
    by_key_time: dict[tuple[str, str], dict[int, Candle]] = defaultdict(dict)
    source_files: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in paths:
        meta = candle_path_meta(path)
        if meta is None:
            continue
        path_symbol, path_interval = meta
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                rows = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            candle = parse_candle(raw, fallback_symbol=path_symbol, fallback_interval=path_interval)
            if candle is None:
                continue
            key = (candle.symbol, candle.interval)
            candle.sources.add(str(path))
            existing = by_key_time[key].get(candle.start_time)
            if existing is None:
                by_key_time[key][candle.start_time] = candle
            else:
                existing.sources.add(str(path))
                if existing.volume in (None, 0.0) and candle.volume not in (None, 0.0):
                    existing.open = candle.open
                    existing.high = candle.high
                    existing.low = candle.low
                    existing.close = candle.close
                    existing.volume = candle.volume
                    existing.trade_count = candle.trade_count
            source_files[key].add(str(path))
    series = {
        key: sorted(rows.values(), key=lambda item: item.start_time)
        for key, rows in by_key_time.items()
    }
    return series, source_files


def parse_candle(raw: dict[str, Any], *, fallback_symbol: str, fallback_interval: str) -> Candle | None:
    symbol = str(raw.get("symbol") or fallback_symbol).strip().upper()
    interval = str(raw.get("interval") or fallback_interval).strip()
    if not symbol or interval not in INTERVAL_MS:
        return None
    start_time = safe_int(raw.get("start_time"))
    end_time = safe_int(raw.get("end_time"))
    open_px = safe_float(raw.get("open"))
    high = safe_float(raw.get("high"))
    low = safe_float(raw.get("low"))
    close = safe_float(raw.get("close"))
    if start_time is None or end_time is None:
        return None
    if open_px is None or high is None or low is None or close is None:
        return None
    if min(open_px, high, low, close) <= 0:
        return None
    return Candle(
        symbol=symbol,
        interval=interval,
        start_time=start_time,
        end_time=end_time,
        open=open_px,
        high=high,
        low=low,
        close=close,
        volume=safe_float(raw.get("volume")),
        trade_count=safe_float(raw.get("trade_count")),
    )


def build_coverage_rows(
    series: dict[tuple[str, str], list[Candle]],
    source_files: dict[tuple[str, str], set[str]],
) -> list[CoverageRow]:
    rows: list[CoverageRow] = []
    for (symbol, interval), candles in sorted(series.items()):
        gaps = gap_durations(candles, interval)
        rows.append(
            CoverageRow(
                symbol=symbol,
                timeframe=interval,
                candle_count=len(candles),
                first_candle=ms_iso(candles[0].start_time) if candles else "",
                last_candle=ms_iso(candles[-1].start_time) if candles else "",
                coverage_days=round((candles[-1].start_time - candles[0].start_time) / 86_400_000.0, 3)
                if len(candles) >= 2
                else 0.0,
                source_file_count=len(source_files[(symbol, interval)]),
                gap_count=len(gaps),
                max_gap_hours=round(max(gaps) / 3_600_000.0, 3) if gaps else 0.0,
            )
        )
    return rows


def detect_cases(
    *,
    symbol: str,
    interval: str,
    candles: list[Candle],
    source_file_count: int,
    min_cup_depth_pct: float,
    max_cup_depth_pct: float,
    min_handle_depth_pct: float,
    max_rim_mismatch_pct: float,
    breakout_buffer_pct: float,
    max_horizon_days: float,
    min_prior_trend_pct: float,
) -> list[PatternCase]:
    cases: list[PatternCase] = []
    lengths = DEFAULT_LENGTHS[interval]
    interval_ms = INTERVAL_MS[interval]
    max_horizon_bars = max(1, int(max_horizon_days * 86_400_000 / interval_ms))
    for breakout_index in range(max(lengths), len(candles)):
        for pattern_bars in lengths:
            start = breakout_index - pattern_bars + 1
            if start < 0:
                continue
            if not is_contiguous(candles, start, breakout_index, interval):
                continue
            detected = detect_window(
                candles=candles,
                symbol=symbol,
                interval=interval,
                start=start,
                breakout_index=breakout_index,
                pattern_bars=pattern_bars,
                source_file_count=source_file_count,
                min_cup_depth_pct=min_cup_depth_pct,
                max_cup_depth_pct=max_cup_depth_pct,
                min_handle_depth_pct=min_handle_depth_pct,
                max_rim_mismatch_pct=max_rim_mismatch_pct,
                breakout_buffer_pct=breakout_buffer_pct,
                max_horizon_bars=max_horizon_bars,
                min_prior_trend_pct=min_prior_trend_pct,
            )
            if detected is not None:
                cases.append(detected)
    return cases


def detect_window(
    *,
    candles: list[Candle],
    symbol: str,
    interval: str,
    start: int,
    breakout_index: int,
    pattern_bars: int,
    source_file_count: int,
    min_cup_depth_pct: float,
    max_cup_depth_pct: float,
    min_handle_depth_pct: float,
    max_rim_mismatch_pct: float,
    breakout_buffer_pct: float,
    max_horizon_bars: int,
    min_prior_trend_pct: float,
) -> PatternCase | None:
    sample = candles[start : breakout_index + 1]
    left_end = max(3, int(pattern_bars * 0.20))
    handle_start = max(left_end + 4, int(pattern_bars * 0.72))
    if handle_start >= pattern_bars - 2:
        return None
    left = sample[:left_end]
    cup_middle = sample[left_end:handle_start]
    handle = sample[handle_start:]
    if len(cup_middle) < 5 or len(handle) < 3:
        return None

    left_rel = max(range(len(left)), key=lambda idx: left[idx].high)
    left_index = start + left_rel
    cup_low_rel = min(range(left_end, handle_start), key=lambda idx: sample[idx].low)
    cup_low_index = start + cup_low_rel
    right_search_start = max(left_end, handle_start - max(4, pattern_bars // 6))
    right_rel = max(range(right_search_start, handle_start), key=lambda idx: sample[idx].high)
    right_index = start + right_rel
    handle_low_rel = min(range(handle_start, pattern_bars), key=lambda idx: sample[idx].low)
    handle_low_index = start + handle_low_rel

    left_high = candles[left_index].high
    right_high = candles[right_index].high
    rim = min(left_high, right_high)
    cup_low = candles[cup_low_index].low
    handle_low = candles[handle_low_index].low
    breakout = candles[breakout_index]
    previous = candles[breakout_index - 1] if breakout_index > 0 else None
    if rim <= 0 or cup_low <= 0 or handle_low <= 0:
        return None

    cup_depth_pct = pct((rim - cup_low) / rim)
    if cup_depth_pct < min_cup_depth_pct or cup_depth_pct > max_cup_depth_pct:
        return None
    rim_mismatch_pct = pct(abs(left_high - right_high) / rim)
    if rim_mismatch_pct > max_rim_mismatch_pct:
        return None
    if right_high < left_high * (1.0 - max_rim_mismatch_pct / 100.0):
        return None

    handle_depth_pct = pct((rim - handle_low) / rim)
    if handle_depth_pct < min_handle_depth_pct:
        return None
    if handle_depth_pct > cup_depth_pct * 0.65:
        return None
    handle_depth_of_cup_pct = 100.0 * handle_depth_pct / cup_depth_pct if cup_depth_pct else 0.0
    cup_midpoint = cup_low + (rim - cup_low) * 0.50
    if handle_low < cup_midpoint:
        return None
    if handle_low_index <= right_index:
        return None

    breakout_threshold = rim * (1.0 + breakout_buffer_pct / 100.0)
    if breakout.close <= breakout_threshold:
        return None
    if previous is not None and previous.close > rim * (1.0 + breakout_buffer_pct / 200.0):
        return None

    bottom_zone = cup_low + (rim - cup_low) * 0.25
    bottom_bars = sum(1 for candle in cup_middle if candle.low <= bottom_zone)
    if bottom_bars < max(2, pattern_bars // 24):
        return None

    prior_trend_pct = prior_trend(candles, start, left_index)
    prior_trend_ok = prior_trend_pct is not None and prior_trend_pct >= min_prior_trend_pct
    target = rim + (rim - cup_low)
    if target <= breakout.close * 1.001:
        return None
    horizon_bars = max(
        1,
        min(
            max_horizon_bars,
            max(12 if interval == "4h" else 24, pattern_bars * 2),
        ),
    )
    outcome = evaluate_outcome(
        candles=candles,
        breakout_index=breakout_index,
        entry_price=breakout.close,
        target_price=target,
        horizon_bars=horizon_bars,
        interval=interval,
    )
    indicators = build_indicator_snapshot(candles, breakout_index, interval)
    breakout_margin_pct = pct((breakout.close - rim) / rim)
    score = pattern_score(
        cup_depth_pct=cup_depth_pct,
        handle_depth_of_cup_pct=handle_depth_of_cup_pct,
        rim_mismatch_pct=rim_mismatch_pct,
        breakout_margin_pct=breakout_margin_pct,
        prior_trend_ok=prior_trend_ok,
        pattern_bars=pattern_bars,
    )
    return PatternCase(
        symbol=symbol,
        timeframe=interval,
        breakout_time=ms_iso(breakout.start_time),
        status=outcome["status"],
        target_hit_horizon=bool(outcome["target_hit_horizon"]),
        target_hit_ever=bool(outcome["target_hit_ever"]),
        target_progress_horizon_pct=round_float(outcome["target_progress_horizon_pct"]),
        target_progress_ever_pct=round_float(outcome["target_progress_ever_pct"]),
        adverse_before_target_or_horizon_pct=round_float(outcome["adverse_before_target_or_horizon_pct"]),
        adverse_before_target_ever_pct=round_optional(outcome["adverse_before_target_ever_pct"]),
        max_favorable_horizon_pct=round_float(outcome["max_favorable_horizon_pct"]),
        end_return_horizon_pct=round_float(outcome["end_return_horizon_pct"]),
        max_up_if_no_target_pct=round_optional(outcome["max_up_if_no_target_pct"]),
        adverse_before_target_or_peak_pct=round_float(outcome["adverse_before_target_or_peak_pct"]),
        peak_bars_horizon=outcome["peak_bars_horizon"],
        peak_time_horizon=outcome["peak_time_horizon"],
        bars_to_target_horizon=outcome["bars_to_target_horizon"],
        bars_to_target_ever=outcome["bars_to_target_ever"],
        time_to_target_horizon=bars_to_duration_label(outcome["bars_to_target_horizon"], interval),
        horizon_bars=int(outcome["horizon_bars"]),
        available_future_bars=int(outcome["available_future_bars"]),
        pattern_bars=pattern_bars,
        cup_bars=handle_start,
        handle_bars=pattern_bars - handle_start,
        left_rim_time=ms_iso(candles[left_index].start_time),
        cup_low_time=ms_iso(candles[cup_low_index].start_time),
        right_rim_time=ms_iso(candles[right_index].start_time),
        handle_low_time=ms_iso(candles[handle_low_index].start_time),
        rim_price=round_price(rim),
        cup_low_price=round_price(cup_low),
        handle_low_price=round_price(handle_low),
        breakout_close=round_price(breakout.close),
        target_price=round_price(target),
        cup_depth_pct=round_float(cup_depth_pct),
        handle_depth_pct=round_float(handle_depth_pct),
        handle_depth_of_cup_pct=round_float(handle_depth_of_cup_pct),
        breakout_margin_pct=round_float(breakout_margin_pct),
        target_pct_from_breakout=round_float(pct((target - breakout.close) / breakout.close)),
        prior_trend_pct=round_optional(prior_trend_pct),
        prior_trend_ok=prior_trend_ok,
        rsi14=round_optional(indicators["rsi14"]),
        volume_ratio20=round_optional(indicators["volume_ratio20"]),
        volume_zscore20=round_optional(indicators["volume_zscore20"]),
        atr14_pct=round_optional(indicators["atr14_pct"]),
        sma20_distance_pct=round_optional(indicators["sma20_distance_pct"]),
        prior_24h_return_pct=round_optional(indicators["prior_24h_return_pct"]),
        rim_mismatch_pct=round_float(rim_mismatch_pct),
        source_file_count=source_file_count,
        score=round_float(score),
    )


def evaluate_outcome(
    *,
    candles: list[Candle],
    breakout_index: int,
    entry_price: float,
    target_price: float,
    horizon_bars: int,
    interval: str,
) -> dict[str, Any]:
    first_future = breakout_index + 1
    contiguous_end = breakout_index
    interval_ms = INTERVAL_MS[interval]
    max_gap = int(interval_ms * 1.5)
    while (
        contiguous_end + 1 < len(candles)
        and candles[contiguous_end + 1].start_time - candles[contiguous_end].start_time <= max_gap
    ):
        contiguous_end += 1
    available_future_bars = max(0, contiguous_end - breakout_index)
    horizon_end = min(contiguous_end, breakout_index + horizon_bars)
    horizon = candles[first_future : horizon_end + 1] if first_future <= horizon_end else []
    ever = candles[first_future : contiguous_end + 1] if first_future <= contiguous_end else []

    if entry_price <= 0 or target_price <= entry_price:
        return {
            "status": "target_already_reached_at_breakout",
            "target_hit_horizon": True,
            "target_hit_ever": True,
            "target_progress_horizon_pct": 100.0,
            "target_progress_ever_pct": 100.0,
            "adverse_before_target_or_horizon_pct": 0.0,
            "adverse_before_target_ever_pct": 0.0,
            "max_favorable_horizon_pct": 0.0,
            "end_return_horizon_pct": 0.0,
            "max_up_if_no_target_pct": None,
            "adverse_before_target_or_peak_pct": 0.0,
            "peak_bars_horizon": 0,
            "peak_time_horizon": ms_iso(candles[breakout_index].start_time),
            "bars_to_target_horizon": 0,
            "bars_to_target_ever": 0,
            "horizon_bars": horizon_bars,
            "available_future_bars": available_future_bars,
        }
    if not horizon:
        return {
            "status": "insufficient_forward_data",
            "target_hit_horizon": False,
            "target_hit_ever": False,
            "target_progress_horizon_pct": 0.0,
            "target_progress_ever_pct": 0.0,
            "adverse_before_target_or_horizon_pct": 0.0,
            "adverse_before_target_ever_pct": None,
            "max_favorable_horizon_pct": 0.0,
            "end_return_horizon_pct": 0.0,
            "max_up_if_no_target_pct": 0.0,
            "adverse_before_target_or_peak_pct": 0.0,
            "peak_bars_horizon": None,
            "peak_time_horizon": None,
            "bars_to_target_horizon": None,
            "bars_to_target_ever": None,
            "horizon_bars": horizon_bars,
            "available_future_bars": available_future_bars,
        }

    horizon_target_index = first_target_index(horizon, target_price)
    ever_target_index = first_target_index(ever, target_price)
    max_high_horizon = max(candle.high for candle in horizon)
    min_low_horizon = min(candle.low for candle in horizon)
    max_high_ever = max((candle.high for candle in ever), default=max_high_horizon)
    target_distance = target_price - entry_price
    peak_index = max(range(len(horizon)), key=lambda index: horizon[index].high)

    if horizon_target_index is None:
        adverse_horizon = min_low_horizon
        endpoint_index = peak_index
        status = "target_not_reached_horizon"
    else:
        target_slice = horizon[: horizon_target_index + 1]
        adverse_horizon = min(candle.low for candle in target_slice)
        endpoint_index = horizon_target_index
        status = "target_hit_horizon"
    endpoint_slice = horizon[: endpoint_index + 1]
    adverse_before_target_or_peak = min(candle.low for candle in endpoint_slice)

    adverse_ever_pct: float | None = None
    if ever_target_index is not None:
        target_slice = ever[: ever_target_index + 1]
        adverse_ever_pct = pct((min(candle.low for candle in target_slice) - entry_price) / entry_price)

    return {
        "status": status,
        "target_hit_horizon": horizon_target_index is not None,
        "target_hit_ever": ever_target_index is not None,
        "target_progress_horizon_pct": clamp(100.0 * (max_high_horizon - entry_price) / target_distance, 0.0, 100.0),
        "target_progress_ever_pct": clamp(100.0 * (max_high_ever - entry_price) / target_distance, 0.0, 100.0),
        "adverse_before_target_or_horizon_pct": pct((adverse_horizon - entry_price) / entry_price),
        "adverse_before_target_ever_pct": adverse_ever_pct,
        "max_favorable_horizon_pct": pct((max_high_horizon - entry_price) / entry_price),
        "end_return_horizon_pct": pct((horizon[-1].close - entry_price) / entry_price),
        "max_up_if_no_target_pct": None
        if horizon_target_index is not None
        else max(0.0, pct((max_high_horizon - entry_price) / entry_price)),
        "adverse_before_target_or_peak_pct": min(
            0.0,
            pct((adverse_before_target_or_peak - entry_price) / entry_price),
        ),
        "peak_bars_horizon": peak_index + 1,
        "peak_time_horizon": ms_iso(horizon[peak_index].start_time),
        "bars_to_target_horizon": horizon_target_index + 1 if horizon_target_index is not None else None,
        "bars_to_target_ever": ever_target_index + 1 if ever_target_index is not None else None,
        "horizon_bars": horizon_bars,
        "available_future_bars": available_future_bars,
    }


def first_target_index(candles: list[Candle], target_price: float) -> int | None:
    for index, candle in enumerate(candles):
        if candle.high >= target_price:
            return index
    return None


def build_indicator_snapshot(candles: list[Candle], index: int, interval: str) -> dict[str, float | None]:
    close = candles[index].close
    return {
        "rsi14": rsi(candles, index, periods=14),
        "volume_ratio20": volume_ratio(candles, index, periods=20),
        "volume_zscore20": volume_zscore(candles, index, periods=20),
        "atr14_pct": atr_pct(candles, index, periods=14),
        "sma20_distance_pct": sma_distance_pct(candles, index, periods=20),
        "prior_24h_return_pct": prior_return_pct(candles, index, bars=24 if interval == "1h" else 6, close=close),
    }


def rsi(candles: list[Candle], index: int, *, periods: int) -> float | None:
    if index < periods:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for pos in range(index - periods + 1, index + 1):
        change = candles[pos].close - candles[pos - 1].close
        if change >= 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs_value = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs_value))


def volume_ratio(candles: list[Candle], index: int, *, periods: int) -> float | None:
    current = candles[index].volume
    if current is None or current <= 0 or index < 1:
        return None
    history = [
        candle.volume
        for candle in candles[max(0, index - periods) : index]
        if candle.volume is not None and candle.volume > 0
    ]
    avg = mean(history)
    if avg is None or avg <= 0:
        return None
    return current / avg


def volume_zscore(candles: list[Candle], index: int, *, periods: int) -> float | None:
    current = candles[index].volume
    if current is None or current <= 0 or index < 1:
        return None
    history = [
        candle.volume
        for candle in candles[max(0, index - periods) : index]
        if candle.volume is not None and candle.volume > 0
    ]
    avg = mean(history)
    std = stdev(history)
    if avg is None or std is None or std <= 0:
        return None
    return (current - avg) / std


def atr_pct(candles: list[Candle], index: int, *, periods: int) -> float | None:
    if index < periods:
        return None
    true_ranges: list[float] = []
    for pos in range(index - periods + 1, index + 1):
        previous_close = candles[pos - 1].close
        candle = candles[pos]
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    avg = mean(true_ranges)
    close = candles[index].close
    if avg is None or close <= 0:
        return None
    return pct(avg / close)


def sma_distance_pct(candles: list[Candle], index: int, *, periods: int) -> float | None:
    if index + 1 < periods:
        return None
    closes = [candle.close for candle in candles[index - periods + 1 : index + 1]]
    avg = mean(closes)
    close = candles[index].close
    if avg is None or avg <= 0:
        return None
    return pct((close - avg) / avg)


def prior_return_pct(candles: list[Candle], index: int, *, bars: int, close: float) -> float | None:
    if index < bars:
        return None
    prior = candles[index - bars].close
    if prior <= 0:
        return None
    return pct((close - prior) / prior)


def dedupe_cases(cases: list[PatternCase]) -> list[PatternCase]:
    grouped: dict[tuple[str, str], list[PatternCase]] = defaultdict(list)
    for case in cases:
        grouped[(case.symbol, case.timeframe)].append(case)
    selected: list[PatternCase] = []
    for (symbol, timeframe), rows in grouped.items():
        rows.sort(key=lambda item: item.breakout_time)
        cluster: list[PatternCase] = []
        last_index_time: int | None = None
        interval_ms = INTERVAL_MS[timeframe]
        cooldown_bars = 12 if timeframe == "1h" else 6
        for row in rows:
            row_time = iso_ms(row.breakout_time)
            if last_index_time is None or (row_time - last_index_time) > cooldown_bars * interval_ms:
                if cluster:
                    selected.append(best_case(cluster))
                cluster = [row]
                last_index_time = row_time
                continue
            cluster.append(row)
            last_index_time = row_time
        if cluster:
            selected.append(best_case(cluster))
    return selected


def best_case(rows: list[PatternCase]) -> PatternCase:
    return sorted(
        rows,
        key=lambda item: (
            item.score,
            item.pattern_bars,
            item.target_progress_horizon_pct,
        ),
        reverse=True,
    )[0]


def build_summary(cases: list[PatternCase], coverage_rows: list[CoverageRow]) -> dict[str, Any]:
    by_tf: dict[str, dict[str, Any]] = {}
    for timeframe in ("1h", "4h"):
        rows = [case for case in cases if case.timeframe == timeframe]
        by_tf[timeframe] = summarize_case_rows(rows)
    prior_rows = [case for case in cases if case.prior_trend_ok]
    no_prior_rows = [case for case in cases if not case.prior_trend_ok]
    return {
        "series_count": len(coverage_rows),
        "symbols": sorted({row.symbol for row in coverage_rows}),
        "total_candles": sum(row.candle_count for row in coverage_rows),
        "case_count": len(cases),
        "by_timeframe": by_tf,
        "prior_trend_ok": summarize_case_rows(prior_rows),
        "prior_trend_not_ok": summarize_case_rows(no_prior_rows),
        "top_symbols_by_case_count": dict(Counter(case.symbol for case in cases).most_common(20)),
    }


def summarize_case_rows(rows: list[PatternCase]) -> dict[str, Any]:
    if not rows:
        return {
            "case_count": 0,
            "target_hit_horizon_count": 0,
            "target_hit_horizon_rate_pct": None,
            "target_hit_ever_count": 0,
            "target_hit_ever_rate_pct": None,
            "median_target_progress_horizon_pct": None,
            "median_adverse_pct": None,
            "median_bars_to_target_horizon": None,
        }
    hit_horizon = [row for row in rows if row.target_hit_horizon]
    hit_ever = [row for row in rows if row.target_hit_ever]
    bars_to_target = [row.bars_to_target_horizon for row in hit_horizon if row.bars_to_target_horizon is not None]
    return {
        "case_count": len(rows),
        "target_hit_horizon_count": len(hit_horizon),
        "target_hit_horizon_rate_pct": round_float(100.0 * len(hit_horizon) / len(rows)),
        "target_hit_ever_count": len(hit_ever),
        "target_hit_ever_rate_pct": round_float(100.0 * len(hit_ever) / len(rows)),
        "median_target_progress_horizon_pct": round_optional(median(row.target_progress_horizon_pct for row in rows)),
        "median_adverse_pct": round_optional(median(row.adverse_before_target_or_peak_pct for row in rows)),
        "median_max_favorable_horizon_pct": round_optional(median(row.max_favorable_horizon_pct for row in rows)),
        "median_end_return_horizon_pct": round_optional(median(row.end_return_horizon_pct for row in rows)),
        "median_target_pct_from_breakout": round_optional(median(row.target_pct_from_breakout for row in rows)),
        "median_bars_to_target_horizon": round_optional(median(bars_to_target)) if bars_to_target else None,
    }


def build_target_level_cases(
    cases: list[PatternCase],
    series: dict[tuple[str, str], list[Candle]],
    *,
    target_fractions: list[float],
) -> list[TargetLevelCase]:
    index_maps = build_start_time_index(series)
    rows: list[TargetLevelCase] = []
    for case in cases:
        key = (case.symbol, case.timeframe)
        candles = series.get(key, [])
        breakout_index = index_maps.get(key, {}).get(iso_ms(case.breakout_time))
        if breakout_index is None:
            continue
        entry = case.breakout_close
        target_distance_pct = case.target_pct_from_breakout
        for fraction_pct in target_fractions:
            target_pct = target_distance_pct * fraction_pct / 100.0
            target_price = entry * (1.0 + target_pct / 100.0)
            outcome = evaluate_outcome(
                candles=candles,
                breakout_index=breakout_index,
                entry_price=entry,
                target_price=target_price,
                horizon_bars=case.horizon_bars,
                interval=case.timeframe,
            )
            rows.append(
                TargetLevelCase(
                    symbol=case.symbol,
                    timeframe=case.timeframe,
                    breakout_time=case.breakout_time,
                    target_fraction_pct=round_float(fraction_pct),
                    target_hit=yes_no(bool(outcome["target_hit_horizon"])),
                    time_to_target=bars_to_duration_label(outcome["bars_to_target_horizon"], case.timeframe),
                    bars_to_target=outcome["bars_to_target_horizon"],
                    target_pct_from_entry=round_float(target_pct),
                    target_price=round_price(target_price),
                    max_up_if_not_hit_pct=round_optional(outcome["max_up_if_no_target_pct"]),
                    adverse_before_target_or_peak_pct=round_float(outcome["adverse_before_target_or_peak_pct"]),
                )
            )
    return rows


def summarize_target_level_cases(rows: list[TargetLevelCase]) -> list[TargetLevelSummary]:
    grouped: dict[tuple[str, float], list[TargetLevelCase]] = defaultdict(list)
    for row in rows:
        grouped[("all", row.target_fraction_pct)].append(row)
        grouped[(row.timeframe, row.target_fraction_pct)].append(row)
    summaries: list[TargetLevelSummary] = []
    for (timeframe, fraction_pct), bucket in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        hits = [row for row in bucket if row.target_hit == "yes"]
        not_hits = [row for row in bucket if row.target_hit != "yes"]
        hit_drawdowns = [max(0.0, -row.adverse_before_target_or_peak_pct) for row in hits]
        summaries.append(
            TargetLevelSummary(
                timeframe=timeframe,
                target_fraction_pct=fraction_pct,
                case_count=len(bucket),
                hit_count=len(hits),
                hit_rate_pct=round_float(100.0 * len(hits) / len(bucket)) if bucket else 0.0,
                median_target_pct_from_entry=round_optional(median(row.target_pct_from_entry for row in bucket)),
                median_bars_to_target=round_optional(median(row.bars_to_target for row in hits if row.bars_to_target is not None)),
                median_adverse_before_target_or_peak_pct=round_optional(
                    median(row.adverse_before_target_or_peak_pct for row in bucket)
                ),
                stop_needed_for_75pct_hits_pct=round_optional(percentile(hit_drawdowns, 75.0)),
                stop_needed_for_90pct_hits_pct=round_optional(percentile(hit_drawdowns, 90.0)),
                median_max_up_if_not_hit_pct=round_optional(
                    median(row.max_up_if_not_hit_pct for row in not_hits if row.max_up_if_not_hit_pct is not None)
                ),
            )
        )
    return summaries


def build_stop_grid_summaries(
    cases: list[PatternCase],
    series: dict[tuple[str, str], list[Candle]],
    *,
    target_fractions: list[float],
    stop_loss_pcts: list[float],
) -> list[StopGridSummary]:
    index_maps = build_start_time_index(series)
    grouped: dict[tuple[str, float, float], list[float | str]] = defaultdict(list)
    for case in cases:
        key = (case.symbol, case.timeframe)
        candles = series.get(key, [])
        breakout_index = index_maps.get(key, {}).get(iso_ms(case.breakout_time))
        if breakout_index is None:
            continue
        for fraction_pct in target_fractions:
            target_pct = case.target_pct_from_breakout * fraction_pct / 100.0
            for stop_pct in stop_loss_pcts:
                outcome, exit_return = simulate_target_stop(
                    candles=candles,
                    breakout_index=breakout_index,
                    entry_price=case.breakout_close,
                    target_pct=target_pct,
                    stop_loss_pct=stop_pct,
                    horizon_bars=case.horizon_bars,
                    interval=case.timeframe,
                )
                for timeframe in ("all", case.timeframe):
                    grouped[(timeframe, round_float(fraction_pct), round_float(stop_pct))].append(outcome)
                    grouped[(timeframe, round_float(fraction_pct), -round_float(stop_pct))].append(exit_return)
    summaries: list[StopGridSummary] = []
    for (timeframe, fraction_pct, stop_key), values in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])):
        if stop_key < 0:
            continue
        outcomes = [value for value in values if isinstance(value, str)]
        returns = [
            value
            for value in grouped.get((timeframe, fraction_pct, -stop_key), [])
            if isinstance(value, (int, float))
        ]
        if not outcomes:
            continue
        target_count = sum(1 for value in outcomes if value == "target")
        stop_count = sum(1 for value in outcomes if value == "stop")
        timeout_count = sum(1 for value in outcomes if value == "timeout")
        summaries.append(
            StopGridSummary(
                timeframe=timeframe,
                target_fraction_pct=fraction_pct,
                stop_loss_pct=stop_key,
                trade_count=len(outcomes),
                target_count=target_count,
                stop_count=stop_count,
                timeout_count=timeout_count,
                target_rate_pct=round_float(100.0 * target_count / len(outcomes)),
                stop_rate_pct=round_float(100.0 * stop_count / len(outcomes)),
                avg_exit_return_pct=round_float(sum(float(value) for value in returns) / len(returns)) if returns else 0.0,
                median_exit_return_pct=round_optional(median(float(value) for value in returns)),
            )
        )
    return summaries


def simulate_target_stop(
    *,
    candles: list[Candle],
    breakout_index: int,
    entry_price: float,
    target_pct: float,
    stop_loss_pct: float,
    horizon_bars: int,
    interval: str,
) -> tuple[str, float]:
    future = contiguous_future(candles, breakout_index, horizon_bars, interval)
    if not future or entry_price <= 0:
        return "timeout", 0.0
    target_price = entry_price * (1.0 + target_pct / 100.0)
    stop_price = entry_price * (1.0 - stop_loss_pct / 100.0)
    for candle in future:
        hit_stop = candle.low <= stop_price
        hit_target = candle.high >= target_price
        if hit_stop:
            return "stop", -stop_loss_pct
        if hit_target:
            return "target", target_pct
    return "timeout", pct((future[-1].close - entry_price) / entry_price)


def build_trade_case_rows(
    *,
    target_level_cases: list[TargetLevelCase],
    cases: list[PatternCase],
) -> list[TradeCaseRow]:
    level_map: dict[tuple[str, str, str, float], TargetLevelCase] = {
        (row.symbol, row.timeframe, row.breakout_time, row.target_fraction_pct): row
        for row in target_level_cases
    }
    rows: list[TradeCaseRow] = []
    for case in cases:
        target_50 = level_map.get((case.symbol, case.timeframe, case.breakout_time, 50.0))
        target_75 = level_map.get((case.symbol, case.timeframe, case.breakout_time, 75.0))
        rows.append(
            TradeCaseRow(
                symbol=case.symbol,
                timeframe=case.timeframe,
                breakout_time=case.breakout_time,
                target_theorique_atteinte="O" if case.target_hit_horizon else "N",
                temps_apres_validation=case.time_to_target_horizon if case.target_hit_horizon else "",
                hausse_max_si_target_non_atteinte_pct=case.max_up_if_no_target_pct if not case.target_hit_horizon else None,
                baisse_max_avant_target_ou_point_haut_pct=case.adverse_before_target_or_peak_pct,
                target_theorique_pct=case.target_pct_from_breakout,
                breakout_close=case.breakout_close,
                target_theorique_price=case.target_price,
                target_50_pct_atteinte=oui_non(target_50.target_hit == "yes") if target_50 else "",
                target_50_pct_temps=target_50.time_to_target if target_50 and target_50.target_hit == "yes" else "",
                target_50_pct_baisse_avant_pct=target_50.adverse_before_target_or_peak_pct if target_50 else 0.0,
                target_75_pct_atteinte=oui_non(target_75.target_hit == "yes") if target_75 else "",
                target_75_pct_temps=target_75.time_to_target if target_75 and target_75.target_hit == "yes" else "",
                target_75_pct_baisse_avant_pct=target_75.adverse_before_target_or_peak_pct if target_75 else 0.0,
                rsi14=case.rsi14,
                volume_ratio20=case.volume_ratio20,
                volume_zscore20=case.volume_zscore20,
                atr14_pct=case.atr14_pct,
                sma20_distance_pct=case.sma20_distance_pct,
                prior_24h_return_pct=case.prior_24h_return_pct,
                cup_depth_pct=case.cup_depth_pct,
                handle_depth_pct=case.handle_depth_pct,
                breakout_margin_pct=case.breakout_margin_pct,
                rim_mismatch_pct=case.rim_mismatch_pct,
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
        "cup_depth_pct",
        "handle_depth_pct",
        "handle_depth_of_cup_pct",
        "breakout_margin_pct",
        "rim_mismatch_pct",
        "target_pct_from_breakout",
        "score",
    ]
    rows: list[IndicatorCorrelationRow] = []
    for timeframe in ("all", "1h", "4h"):
        scoped = cases if timeframe == "all" else [case for case in cases if case.timeframe == timeframe]
        for indicator in indicators:
            pairs: list[tuple[float, PatternCase]] = []
            for case in scoped:
                value = getattr(case, indicator)
                if value is None:
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(numeric):
                    pairs.append((numeric, case))
            if len(pairs) < 20:
                continue
            values = [value for value, _case in pairs]
            hit_values = [1.0 if case.target_hit_horizon else 0.0 for _value, case in pairs]
            progress_values = [case.target_progress_horizon_pct for _value, case in pairs]
            mfe_values = [case.max_favorable_horizon_pct for _value, case in pairs]
            bottom, top = terciles(pairs)
            rows.append(
                IndicatorCorrelationRow(
                    timeframe=timeframe,
                    indicator=indicator,
                    sample_count=len(pairs),
                    corr_target_hit=round_optional(pearson(values, hit_values)),
                    corr_target_progress=round_optional(pearson(values, progress_values)),
                    corr_max_favorable=round_optional(pearson(values, mfe_values)),
                    hit_rate_bottom_tercile_pct=round_optional(hit_rate(bottom)),
                    hit_rate_top_tercile_pct=round_optional(hit_rate(top)),
                    median_progress_bottom_tercile_pct=round_optional(
                        median(case.target_progress_horizon_pct for _value, case in bottom)
                    ),
                    median_progress_top_tercile_pct=round_optional(
                        median(case.target_progress_horizon_pct for _value, case in top)
                    ),
                )
            )
    return rows


def build_start_time_index(series: dict[tuple[str, str], list[Candle]]) -> dict[tuple[str, str], dict[int, int]]:
    return {
        key: {candle.start_time: index for index, candle in enumerate(candles)}
        for key, candles in series.items()
    }


def contiguous_future(candles: list[Candle], breakout_index: int, horizon_bars: int, interval: str) -> list[Candle]:
    if breakout_index + 1 >= len(candles):
        return []
    max_gap = int(INTERVAL_MS[interval] * 1.5)
    rows: list[Candle] = []
    current_index = breakout_index
    while current_index + 1 < len(candles) and len(rows) < horizon_bars:
        if candles[current_index + 1].start_time - candles[current_index].start_time > max_gap:
            break
        rows.append(candles[current_index + 1])
        current_index += 1
    return rows


def terciles(pairs: list[tuple[float, PatternCase]]) -> tuple[list[tuple[float, PatternCase]], list[tuple[float, PatternCase]]]:
    ordered = sorted(pairs, key=lambda item: item[0])
    size = max(1, len(ordered) // 3)
    return ordered[:size], ordered[-size:]


def hit_rate(rows: list[tuple[float, PatternCase]]) -> float | None:
    if not rows:
        return None
    return 100.0 * sum(1 for _value, case in rows if case.target_hit_horizon) / len(rows)


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_var = sum((x - left_mean) ** 2 for x in left)
    right_var = sum((y - right_mean) ** 2 for y in right)
    if left_var <= 0 or right_var <= 0:
        return None
    return numerator / math.sqrt(left_var * right_var)


def write_markdown(
    output_path: Path,
    payload: dict[str, Any],
    cases: list[PatternCase],
    target_level_summaries: list[TargetLevelSummary],
    stop_grid_summaries: list[StopGridSummary],
    indicator_correlations: list[IndicatorCorrelationRow],
    top_table_rows: int,
) -> None:
    summary = payload["summary"]
    params = payload["parameters"]
    lines = [
        "# Cup-and-handle pattern scan",
        "",
        "Statut: `research_only_no_live_change`.",
        "",
        "## Definition utilisee",
        "",
        "- Donnees: bougies exchange natives Hyperliquid locales en `1h` et `4h`, fusionnees sans doublons par `symbol/timeframe/start_time`.",
        "- Pattern valide: tasse long uniquement, avec breakout de cloture au-dessus du rim apres une anse peu profonde situee dans la moitie haute de la tasse.",
        f"- Profondeur tasse: `{params['min_cup_depth_pct']:.2f}%` -> `{params['max_cup_depth_pct']:.2f}%`; mismatch rims max `{params['max_rim_mismatch_pct']:.2f}%`; breakout buffer `{params['breakout_buffer_pct']:.2f}%`.",
        f"- Target classique: `{params['target_rule']}`; horizon fixe: max `{params['max_horizon_days']:.1f}` jours ou `2x` la longueur du pattern.",
        "- `target_progress_horizon_pct` mesure le plus haut atteint entre l'entree breakout et la target theorique.",
        "- La baisse adverse orientee trade est mesuree avant la target; si la target n'est pas atteinte, elle est mesuree avant le point haut observe.",
        "",
        "## Couverture",
        "",
        f"- Series scannees: `{summary['series_count']}`.",
        f"- Symbols: `{len(summary['symbols'])}`.",
        f"- Bougies uniques: `{summary['total_candles']}`.",
        f"- Patterns valides apres dedupe: `{summary['case_count']}`.",
        "",
        "## Synthese edge",
        "",
        "| Segment | Cas | Target horizon | Hit rate | Target ever | Ever rate | Med progress | Med adverse | Med MFE | Med end |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, item in [
        ("1h", summary["by_timeframe"].get("1h", {})),
        ("4h", summary["by_timeframe"].get("4h", {})),
        ("prior trend OK", summary.get("prior_trend_ok", {})),
        ("prior trend not OK", summary.get("prior_trend_not_ok", {})),
    ]:
        lines.append(summary_row(label, item))
    lines.extend(
        [
            "",
            "Lecture rapide:",
            "",
            "- `Target horizon` est la mesure exploitable pour un edge systematique; `Target ever` indique seulement si la target finit par etre vue plus tard dans les donnees disponibles.",
            "- Une baisse adverse mediane importante avant target implique un besoin de stop plus large, une target plus basse, ou un meilleur filtre d'entree.",
            "- Les cas proches de la fin des donnees peuvent etre `insufficient_forward_data` et ne doivent pas etre surinterpretes.",
            "",
            "## Targets partielles",
            "",
            "| TF | Target partielle | Cas | Hit rate | Target mediane | Bars median | Baisse mediane | SL 75% hits | SL 90% hits | MFE median si non-hit |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in target_level_summaries:
        if row.timeframe not in {"all", "1h", "4h"}:
            continue
        lines.append(
            f"| {row.timeframe} | {row.target_fraction_pct:.0f}% | {row.case_count} | "
            f"{row.hit_rate_pct:.2f}% | {fmt_pct(row.median_target_pct_from_entry)} | "
            f"{fmt_number(row.median_bars_to_target)} | "
            f"{fmt_pct(row.median_adverse_before_target_or_peak_pct)} | "
            f"{fmt_pct(row.stop_needed_for_75pct_hits_pct)} | "
            f"{fmt_pct(row.stop_needed_for_90pct_hits_pct)} | "
            f"{fmt_pct(row.median_max_up_if_not_hit_pct)} |"
        )
    lines.extend(
        [
            "",
            "## Meilleures combinaisons TP/SL brutes",
            "",
            "Simulation conservative sur bougies: si TP et SL touchent dans la meme bougie, le SL gagne.",
            "",
            "| TF | Target partielle | SL | Trades | TP rate | Stop rate | Avg exit | Med exit |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in best_stop_grid_rows(stop_grid_summaries)[:24]:
        lines.append(
            f"| {row.timeframe} | {row.target_fraction_pct:.0f}% | {row.stop_loss_pct:.2f}% | "
            f"{row.trade_count} | {row.target_rate_pct:.2f}% | {row.stop_rate_pct:.2f}% | "
            f"{fmt_pct(row.avg_exit_return_pct)} | {fmt_pct(row.median_exit_return_pct)} |"
        )
    lines.extend(
        [
            "",
            "## Correlations indicateurs",
            "",
            "| TF | Indicateur | N | Corr hit | Corr progress | Corr MFE | Hit bottom tercile | Hit top tercile |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in best_correlation_rows(indicator_correlations)[:24]:
        lines.append(
            f"| {row.timeframe} | {row.indicator} | {row.sample_count} | "
            f"{fmt_number(row.corr_target_hit)} | {fmt_number(row.corr_target_progress)} | "
            f"{fmt_number(row.corr_max_favorable)} | {fmt_pct(row.hit_rate_bottom_tercile_pct)} | "
            f"{fmt_pct(row.hit_rate_top_tercile_pct)} |"
        )
    lines.extend(
        [
            "",
            f"## Cas valides ({min(len(cases), top_table_rows)} premiers sur {len(cases)})",
            "",
            "| Time | Sym | TF | Resultat | Target % | Progress | Adverse | MFE | End | Bars target | Cup | Handle | Rim | Target |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    sorted_cases = sorted(
        cases,
        key=lambda item: (
            item.target_hit_horizon,
            item.target_progress_horizon_pct,
            item.max_favorable_horizon_pct,
        ),
        reverse=True,
    )
    for case in sorted_cases[:top_table_rows]:
        bars = str(case.bars_to_target_horizon) if case.bars_to_target_horizon is not None else "-"
        result = "target_hit" if case.target_hit_horizon else f"{case.target_progress_horizon_pct:.1f}%"
        lines.append(
            f"| {case.breakout_time} | {case.symbol} | {case.timeframe} | {result} | "
            f"{case.target_pct_from_breakout:.2f}% | {case.target_progress_horizon_pct:.1f}% | "
            f"{case.adverse_before_target_or_peak_pct:.2f}% | {case.max_favorable_horizon_pct:.2f}% | "
            f"{case.end_return_horizon_pct:.2f}% | {bars} | {case.cup_depth_pct:.2f}% | "
            f"{case.handle_depth_pct:.2f}% | {case.rim_price:g} | {case.target_price:g} |"
        )
    lines.extend(
        [
            "",
            "## Fichiers",
            "",
            "- `cup_handle_cases.csv`: tous les cas valides dedupes avec metrics completes.",
            "- `cup_handle_cases.md`: table Markdown complete de tous les cas valides.",
            "- `cup_handle_trade_cases.csv/md`: table orientee trade avec les colonnes demandees.",
            "- `cup_handle_target_level_cases.csv`: chaque cas decline en targets partielles.",
            "- `cup_handle_target_level_summary.csv/md`: hit rates par target partielle.",
            "- `cup_handle_stop_grid_summary.csv/md`: grille target/SL conservative.",
            "- `cup_handle_indicator_correlations.csv/md`: correlations simples avec les indicateurs au breakout.",
            "- `coverage.csv`: couverture par symbole/timeframe apres fusion.",
            "- `cup_handle_pattern_scan.json`: payload complet machine-readable.",
            "",
            "## Garde-fous",
            "",
            "- Ce scan n'est pas un replay full-bot et ne tient pas compte des frais, slippage, sizing, gates TRIDENT, liquidite intrabar ou fills.",
            "- Aucun changement live/config/deploy/fetch n'est implique par ce rapport.",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_cases_markdown(output_path: Path, cases: list[PatternCase]) -> None:
    lines = [
        "# Cup-and-handle validated cases",
        "",
        "Table complete des patterns valides dedupes. `Progress` est la part de target atteinte dans l'horizon; `Adverse` est la baisse max avant target, ou avant le point haut si target non atteinte.",
        "",
        "| Time | Sym | TF | Status | Target hit | Ever | Target % | Progress | Adverse | MFE | End | Bars target | Cup | Handle | Breakout | Target |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for case in sorted(cases, key=lambda item: (item.breakout_time, item.symbol, item.timeframe)):
        bars = str(case.bars_to_target_horizon) if case.bars_to_target_horizon is not None else "-"
        lines.append(
            f"| {case.breakout_time} | {case.symbol} | {case.timeframe} | {case.status} | "
            f"{yes_no(case.target_hit_horizon)} | {yes_no(case.target_hit_ever)} | "
            f"{case.target_pct_from_breakout:.2f}% | {case.target_progress_horizon_pct:.1f}% | "
            f"{case.adverse_before_target_or_peak_pct:.2f}% | {case.max_favorable_horizon_pct:.2f}% | "
            f"{case.end_return_horizon_pct:.2f}% | {bars} | {case.cup_depth_pct:.2f}% | "
            f"{case.handle_depth_pct:.2f}% | {case.breakout_close:g} | {case.target_price:g} |"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_trade_cases_markdown(output_path: Path, rows: list[TradeCaseRow]) -> None:
    lines = [
        "# Cup-and-handle trade table",
        "",
        "Table orientee decision. `Baisse` = baisse max avant target theorique, ou avant point haut si la target theorique n'est pas atteinte.",
        "",
        "| Time | Sym | TF | Target theorique | Temps | MFE si N | Baisse | Target % | T50 | T50 temps | T50 baisse | T75 | T75 temps | T75 baisse | RSI14 | Vol ratio | ATR14 | SMA20 dist | Ret 24h |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.breakout_time} | {row.symbol} | {row.timeframe} | {row.target_theorique_atteinte} | "
            f"{row.temps_apres_validation or '-'} | {fmt_pct(row.hausse_max_si_target_non_atteinte_pct)} | "
            f"{fmt_pct(row.baisse_max_avant_target_ou_point_haut_pct)} | {fmt_pct(row.target_theorique_pct)} | "
            f"{row.target_50_pct_atteinte} | {row.target_50_pct_temps or '-'} | "
            f"{fmt_pct(row.target_50_pct_baisse_avant_pct)} | {row.target_75_pct_atteinte} | "
            f"{row.target_75_pct_temps or '-'} | {fmt_pct(row.target_75_pct_baisse_avant_pct)} | "
            f"{fmt_number(row.rsi14)} | {fmt_number(row.volume_ratio20)} | {fmt_pct(row.atr14_pct)} | "
            f"{fmt_pct(row.sma20_distance_pct)} | {fmt_pct(row.prior_24h_return_pct)} |"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_target_level_summary_markdown(output_path: Path, rows: list[TargetLevelSummary]) -> None:
    lines = [
        "# Cup-and-handle target-level summary",
        "",
        "| TF | Target partielle | Cas | Hits | Hit rate | Target mediane | Bars median | Baisse mediane | SL 75% hits | SL 90% hits | MFE median si non-hit |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.timeframe} | {row.target_fraction_pct:.0f}% | {row.case_count} | {row.hit_count} | "
            f"{fmt_pct(row.hit_rate_pct)} | {fmt_pct(row.median_target_pct_from_entry)} | "
            f"{fmt_number(row.median_bars_to_target)} | "
            f"{fmt_pct(row.median_adverse_before_target_or_peak_pct)} | "
            f"{fmt_pct(row.stop_needed_for_75pct_hits_pct)} | "
            f"{fmt_pct(row.stop_needed_for_90pct_hits_pct)} | "
            f"{fmt_pct(row.median_max_up_if_not_hit_pct)} |"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_stop_grid_markdown(output_path: Path, rows: list[StopGridSummary]) -> None:
    lines = [
        "# Cup-and-handle TP/SL grid",
        "",
        "Simulation conservative sur bougies: si TP et SL touchent dans la meme bougie, le SL gagne.",
        "",
        "| TF | Target partielle | SL | Trades | TP | Stop | Timeout | TP rate | Stop rate | Avg exit | Med exit |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.timeframe} | {row.target_fraction_pct:.0f}% | {row.stop_loss_pct:.2f}% | "
            f"{row.trade_count} | {row.target_count} | {row.stop_count} | {row.timeout_count} | "
            f"{fmt_pct(row.target_rate_pct)} | {fmt_pct(row.stop_rate_pct)} | "
            f"{fmt_pct(row.avg_exit_return_pct)} | {fmt_pct(row.median_exit_return_pct)} |"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_indicator_correlations_markdown(output_path: Path, rows: list[IndicatorCorrelationRow]) -> None:
    lines = [
        "# Cup-and-handle indicator correlations",
        "",
        "Correlations simples au breakout. Elles indiquent des filtres candidats, pas une causalite.",
        "",
        "| TF | Indicateur | N | Corr hit | Corr progress | Corr MFE | Hit bottom tercile | Hit top tercile | Progress bottom | Progress top |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.timeframe} | {row.indicator} | {row.sample_count} | "
            f"{fmt_number(row.corr_target_hit)} | {fmt_number(row.corr_target_progress)} | "
            f"{fmt_number(row.corr_max_favorable)} | {fmt_pct(row.hit_rate_bottom_tercile_pct)} | "
            f"{fmt_pct(row.hit_rate_top_tercile_pct)} | "
            f"{fmt_pct(row.median_progress_bottom_tercile_pct)} | "
            f"{fmt_pct(row.median_progress_top_tercile_pct)} |"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def best_stop_grid_rows(rows: list[StopGridSummary]) -> list[StopGridSummary]:
    return sorted(
        [row for row in rows if row.trade_count >= 50],
        key=lambda row: (row.avg_exit_return_pct, row.target_rate_pct, -row.stop_rate_pct),
        reverse=True,
    )


def best_correlation_rows(rows: list[IndicatorCorrelationRow]) -> list[IndicatorCorrelationRow]:
    return sorted(
        rows,
        key=lambda row: max(
            abs(row.corr_target_hit or 0.0),
            abs(row.corr_target_progress or 0.0),
            abs(row.corr_max_favorable or 0.0),
        ),
        reverse=True,
    )


def summary_row(label: str, item: dict[str, Any]) -> str:
    return (
        f"| {label} | {item.get('case_count', 0)} | {item.get('target_hit_horizon_count', 0)} | "
        f"{fmt_pct(item.get('target_hit_horizon_rate_pct'))} | {item.get('target_hit_ever_count', 0)} | "
        f"{fmt_pct(item.get('target_hit_ever_rate_pct'))} | {fmt_pct(item.get('median_target_progress_horizon_pct'))} | "
        f"{fmt_pct(item.get('median_adverse_pct'))} | {fmt_pct(item.get('median_max_favorable_horizon_pct'))} | "
        f"{fmt_pct(item.get('median_end_return_horizon_pct'))} |"
    )


def pattern_score(
    *,
    cup_depth_pct: float,
    handle_depth_of_cup_pct: float,
    rim_mismatch_pct: float,
    breakout_margin_pct: float,
    prior_trend_ok: bool,
    pattern_bars: int,
) -> float:
    depth_score = 1.0 - min(abs(cup_depth_pct - 12.0) / 24.0, 1.0)
    handle_score = 1.0 - min(abs(handle_depth_of_cup_pct - 30.0) / 45.0, 1.0)
    rim_score = 1.0 - min(rim_mismatch_pct / 8.0, 1.0)
    breakout_score = min(max(breakout_margin_pct / 2.0, 0.0), 1.0)
    trend_score = 0.25 if prior_trend_ok else 0.0
    length_score = min(math.log(max(pattern_bars, 2), 2) / 8.0, 1.0) * 0.25
    return depth_score + handle_score + rim_score + breakout_score + trend_score + length_score


def prior_trend(candles: list[Candle], start: int, left_index: int) -> float | None:
    lookback = max(8, (left_index - start + 1) * 2)
    prior_index = start - lookback
    if prior_index < 0:
        return None
    prior_close = candles[prior_index].close
    if prior_close <= 0:
        return None
    return pct((candles[left_index].high - prior_close) / prior_close)


def is_contiguous(candles: list[Candle], start: int, end: int, interval: str) -> bool:
    if start >= end:
        return True
    interval_ms = INTERVAL_MS[interval]
    max_gap = int(interval_ms * 1.5)
    for index in range(start + 1, end + 1):
        if candles[index].start_time - candles[index - 1].start_time > max_gap:
            return False
    return True


def gap_durations(candles: list[Candle], interval: str) -> list[int]:
    if len(candles) < 2:
        return []
    interval_ms = INTERVAL_MS[interval]
    max_gap = int(interval_ms * 1.5)
    return [
        candles[index].start_time - candles[index - 1].start_time
        for index in range(1, len(candles))
        if candles[index].start_time - candles[index - 1].start_time > max_gap
    ]


def write_csv(path: Path, rows: list[Any]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(asdict(rows[0]).keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_percent_list(text: str, *, default: list[float]) -> list[float]:
    values: list[float] = []
    for item in text.split(","):
        value = safe_float(item.strip())
        if value is not None and value > 0:
            values.append(round_float(value))
    return sorted(set(values)) if values else default


def median(values: Iterable[float | int]) -> float | None:
    rows = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not rows:
        return None
    mid = len(rows) // 2
    if len(rows) % 2:
        return rows[mid]
    return (rows[mid - 1] + rows[mid]) / 2.0


def percentile(values: Iterable[float | int | None], pct_value: float) -> float | None:
    rows = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    rank = (len(rows) - 1) * clamp(pct_value, 0.0, 100.0) / 100.0
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return rows[lower]
    weight = rank - lower
    return rows[lower] * (1.0 - weight) + rows[upper] * weight


def mean(values: Iterable[float | int | None]) -> float | None:
    rows = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not rows:
        return None
    return sum(rows) / len(rows)


def stdev(values: Iterable[float | int | None]) -> float | None:
    rows = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if len(rows) < 2:
        return None
    avg = sum(rows) / len(rows)
    variance = sum((value - avg) ** 2 for value in rows) / (len(rows) - 1)
    return math.sqrt(variance)


def safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pct(value: float) -> float:
    return value * 100.0


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def round_price(value: float) -> float:
    return round(value, 10)


def round_float(value: float) -> float:
    return round(float(value), 6)


def round_optional(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round_float(float(value))


def fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "-"


def fmt_number(value: Any) -> str:
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


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def oui_non(value: bool) -> str:
    return "O" if value else "N"


def bars_to_duration_label(bars: int | None, interval: str) -> str:
    if bars is None:
        return ""
    total_hours = bars * (1 if interval == "1h" else 4)
    if total_hours < 24:
        return f"{total_hours}h"
    days = total_hours // 24
    hours = total_hours % 24
    if hours == 0:
        return f"{days}d"
    return f"{days}d {hours}h"


def ms_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def iso_ms(value: str) -> int:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return int(parsed.timestamp() * 1000)


def utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
