#!/usr/bin/env python3
"""Replay the top 3 chart-pattern candidates as a synthetic research sleeve.

Research-only: reads local chart-pattern scan outputs and OHLCV candles, then
simulates a combined TP/SL overlay. It does not modify live config, deploy,
fetch, or trading behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import run_cup_handle_pattern_scan as cup


DEFAULT_CASES_CSV = (
    "server-data/replay_reports/chart_pattern_skill_replay_20260706T000000Z/"
    "chart_pattern_cases.csv"
)
DEFAULT_BASELINE_JSON = "tmp/full_bot_baseline_current_20260519.json"
DEFAULT_OFFICIAL_BASELINE_JSON = (
    "server-data/replay_reports/official_baseline_current_cli_20260513.json"
)
DEFAULT_OUTPUT_DIR = "server-data/replay_reports/chart_pattern_top3_overlay_20260706T000000Z"


@dataclass(frozen=True, slots=True)
class PatternConfig:
    pattern: str
    label: str
    filter_name: str
    target_fraction_pct: float
    stop_loss_pct: float
    priority: int


@dataclass(slots=True)
class PatternCase:
    pattern: str
    symbol: str
    timeframe: str
    validation_time: str
    entry_price: float
    target_pct_from_entry: float
    horizon_bars: int
    structure_depth_pct: float
    breakout_margin_pct: float
    volume_ratio20: float | None
    score: float
    atr14_pct: float | None


@dataclass(slots=True)
class TradeReplay:
    pattern: str
    label: str
    filter_name: str
    symbol: str
    timeframe: str
    validation_time: str
    entry_price: float
    target_fraction_pct: float
    stop_loss_pct: float
    target_pct: float
    outcome: str
    exit_time: str
    exit_return_pct: float
    pnl_usd: float
    hold_bars: int
    hold_hours: float
    skipped: bool
    skip_reason: str
    split: str


@dataclass(slots=True)
class SegmentSummary:
    segment: str
    start_time: str | None
    end_time: str | None
    candidate_count: int
    accepted_trades: int
    skipped_trades: int
    target_count: int
    stop_count: int
    timeout_count: int
    win_rate_pct: float | None
    stop_rate_pct: float | None
    avg_return_pct: float | None
    median_return_pct: float | None
    total_return_pct_on_notional: float
    pnl_usd: float
    profit_factor: float | None
    max_drawdown_usd: float
    max_open_positions: int
    by_pattern_pnl_usd: dict[str, float]
    by_pattern_trades: dict[str, int]
    by_pattern_avg_return_pct: dict[str, float | None]
    by_pattern_win_rate_pct: dict[str, float | None]
    baseline_current_pnl_usd: float | None
    baseline_official_pnl_usd: float | None
    total_with_current_baseline_usd: float | None
    delta_vs_current_baseline_usd: float | None
    total_with_official_baseline_usd: float | None
    delta_vs_official_baseline_usd: float | None


def top3_configs() -> list[PatternConfig]:
    return [
        PatternConfig(
            pattern="double_bottom",
            label="double_bottom_4h_target100_sl6",
            filter_name="4h_target_low_q50_score_high_q50_volume_high_q50",
            target_fraction_pct=100.0,
            stop_loss_pct=6.0,
            priority=1,
        ),
        PatternConfig(
            pattern="triangle_breakout",
            label="triangle_breakout_4h_target75_sl15",
            filter_name="4h_target_low_q50_score_high_q50_volume_high_q50",
            target_fraction_pct=75.0,
            stop_loss_pct=15.0,
            priority=2,
        ),
        PatternConfig(
            pattern="cup_handle",
            label="cup_handle_4h_target33_sl8",
            filter_name="4h_target_low_q33_depth_low_q33_breakout_high_q66_volume_high_q66",
            target_fraction_pct=33.0,
            stop_loss_pct=8.0,
            priority=3,
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-csv", default=DEFAULT_CASES_CSV)
    parser.add_argument("--input-root", action="append", default=None)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--baseline-json", default=DEFAULT_BASELINE_JSON)
    parser.add_argument("--official-baseline-json", default=DEFAULT_OFFICIAL_BASELINE_JSON)
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--max-open-positions", type=int, default=4)
    parser.add_argument("--max-new-positions-per-bar", type=int, default=2)
    parser.add_argument("--skip-same-symbol-overlap", action="store_true", default=True)
    parser.add_argument("--exclude-symbol", action="append", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    configs = top3_configs()
    input_roots = [Path(item) for item in (args.input_root or ["data", "server-data"])]
    series, _source_files = cup.load_all_series(cup.discover_candle_files(input_roots))
    cases = load_cases(Path(args.cases_csv))
    excluded_symbols = {str(item).upper() for item in (args.exclude_symbol or []) if str(item).strip()}
    if excluded_symbols:
        cases = [case for case in cases if case.symbol.upper() not in excluded_symbols]
    thresholds = compute_thresholds(cases)
    candidates = select_candidates(cases, configs, thresholds)
    trades = replay_portfolio(
        candidates=candidates,
        configs={config.pattern: config for config in configs},
        series=series,
        notional_usd=float(args.notional_usd),
        max_open_positions=int(args.max_open_positions),
        max_new_positions_per_bar=int(args.max_new_positions_per_bar),
        skip_same_symbol_overlap=bool(args.skip_same_symbol_overlap),
    )
    current_baseline = load_baseline(Path(args.baseline_json))
    official_baseline = load_baseline(Path(args.official_baseline_json))
    baseline_start = current_baseline.get("first_timestamp")
    baseline_end = current_baseline.get("last_timestamp")
    summaries = [
        summarize_segment(
            "all_ohlcv_history",
            trades,
            start_time=None,
            end_time=None,
            current_baseline=None,
            official_baseline=None,
        ),
        summarize_segment(
            "official_baseline_window",
            trades,
            start_time=baseline_start,
            end_time=baseline_end,
            current_baseline=current_baseline,
            official_baseline=official_baseline,
        ),
    ]
    payload = {
        "kind": "chart_pattern_top3_overlay_replay",
        "generated_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "status": "research_only_no_live_change",
        "method": {
            "scope": "Synthetic chart-pattern sleeve on local OHLCV candles; official full-bot baseline is used for the comparable baseline window.",
            "top3": [asdict(config) for config in configs],
            "notional_usd_per_trade": float(args.notional_usd),
            "max_open_positions": int(args.max_open_positions),
            "max_new_positions_per_bar": int(args.max_new_positions_per_bar),
            "same_symbol_overlap_guard": bool(args.skip_same_symbol_overlap),
            "excluded_symbols": sorted(excluded_symbols),
            "intrabar_ordering": "Conservative: stop wins if TP and SL are both touched in the same candle.",
            "limits": [
                "No fees/slippage/liquidity model.",
                "No full-bot routing or margin interaction outside the official baseline additive comparison.",
                "Baseline full-bot is only available on its snapshot replay window.",
            ],
        },
        "inputs": {
            "cases_csv": str(args.cases_csv),
            "input_roots": [str(item) for item in input_roots],
            "baseline_json": str(args.baseline_json),
            "official_baseline_json": str(args.official_baseline_json),
        },
        "thresholds": threshold_payload(thresholds),
        "candidate_counts": dict(Counter(case.pattern for case in candidates)),
        "summaries": [asdict(summary) for summary in summaries],
        "trades": [asdict(trade) for trade in trades],
    }
    write_csv(output_dir / "top3_overlay_trades.csv", trades)
    write_csv(output_dir / "top3_overlay_summary.csv", summaries)
    (output_dir / "top3_overlay_replay.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "top3_overlay_replay.md", payload)
    print(output_dir)


def load_cases(path: Path) -> list[PatternCase]:
    rows: list[PatternCase] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            if raw.get("timeframe") != "4h":
                continue
            pattern = str(raw.get("pattern", ""))
            if pattern not in {config.pattern for config in top3_configs()}:
                continue
            rows.append(
                PatternCase(
                    pattern=pattern,
                    symbol=str(raw.get("symbol", "")),
                    timeframe=str(raw.get("timeframe", "")),
                    validation_time=str(raw.get("validation_time", "")),
                    entry_price=parse_float(raw.get("entry_price")),
                    target_pct_from_entry=parse_float(raw.get("target_pct_from_entry")),
                    horizon_bars=parse_int(raw.get("horizon_bars")),
                    structure_depth_pct=parse_float(raw.get("structure_depth_pct")),
                    breakout_margin_pct=parse_float(raw.get("breakout_margin_pct")),
                    volume_ratio20=parse_optional_float(raw.get("volume_ratio20")),
                    score=parse_float(raw.get("score")),
                    atr14_pct=parse_optional_float(raw.get("atr14_pct")),
                )
            )
    return rows


def compute_thresholds(cases: list[PatternCase]) -> dict[tuple[str, str, int], float]:
    thresholds: dict[tuple[str, str, int], float] = {}
    fields = [
        "target_pct_from_entry",
        "structure_depth_pct",
        "breakout_margin_pct",
        "volume_ratio20",
        "score",
    ]
    for pattern in sorted({case.pattern for case in cases}):
        scoped = [case for case in cases if case.pattern == pattern and case.timeframe == "4h"]
        for field in fields:
            values = [getattr(case, field) for case in scoped if getattr(case, field) is not None]
            for pct_value in (33, 50, 66):
                value = cup.percentile(values, float(pct_value))
                if value is not None:
                    thresholds[(pattern, field, pct_value)] = value
    return thresholds


def select_candidates(
    cases: list[PatternCase],
    configs: list[PatternConfig],
    thresholds: dict[tuple[str, str, int], float],
) -> list[PatternCase]:
    selected: list[PatternCase] = []
    by_pattern = {config.pattern: config for config in configs}
    for case in cases:
        config = by_pattern.get(case.pattern)
        if config is None or case.timeframe != "4h":
            continue
        if case.pattern == "cup_handle":
            if not (
                le(case, thresholds, "target_pct_from_entry", 33)
                and le(case, thresholds, "structure_depth_pct", 33)
                and ge(case, thresholds, "breakout_margin_pct", 66)
                and ge(case, thresholds, "volume_ratio20", 66)
            ):
                continue
        elif case.pattern in {"double_bottom", "triangle_breakout"}:
            if not (
                le(case, thresholds, "target_pct_from_entry", 50)
                and ge(case, thresholds, "score", 50)
                and ge(case, thresholds, "volume_ratio20", 50)
            ):
                continue
        selected.append(case)
    selected.sort(key=lambda item: (cup.iso_ms(item.validation_time), by_pattern[item.pattern].priority, item.symbol))
    return selected


def replay_portfolio(
    *,
    candidates: list[PatternCase],
    configs: dict[str, PatternConfig],
    series: dict[tuple[str, str], list[cup.Candle]],
    notional_usd: float,
    max_open_positions: int,
    max_new_positions_per_bar: int,
    skip_same_symbol_overlap: bool,
) -> list[TradeReplay]:
    index_maps = cup.build_start_time_index(series)
    rows: list[TradeReplay] = []
    open_positions: list[TradeReplay] = []
    opened_by_bar: Counter[int] = Counter()
    for case in candidates:
        now_ms = cup.iso_ms(case.validation_time)
        open_positions = [
            trade
            for trade in open_positions
            if cup.iso_ms(trade.exit_time) > now_ms
        ]
        config = configs[case.pattern]
        split = "train" if now_ms < cup.iso_ms("2026-01-01T00:00:00Z") else "test"
        skip_reason = ""
        if opened_by_bar[now_ms] >= max_new_positions_per_bar:
            skip_reason = "max_new_positions_per_bar"
        elif len(open_positions) >= max_open_positions:
            skip_reason = "max_open_positions"
        elif skip_same_symbol_overlap and any(trade.symbol == case.symbol for trade in open_positions):
            skip_reason = "same_symbol_overlap"
        if skip_reason:
            rows.append(skipped_trade(case, config, notional_usd, split, skip_reason))
            continue
        replay = simulate_trade(case, config, series, index_maps, notional_usd, split)
        rows.append(replay)
        if not replay.skipped:
            open_positions.append(replay)
            opened_by_bar[now_ms] += 1
    return rows


def simulate_trade(
    case: PatternCase,
    config: PatternConfig,
    series: dict[tuple[str, str], list[cup.Candle]],
    index_maps: dict[tuple[str, str], dict[int, int]],
    notional_usd: float,
    split: str,
) -> TradeReplay:
    key = (case.symbol, case.timeframe)
    candles = series.get(key, [])
    breakout_index = index_maps.get(key, {}).get(cup.iso_ms(case.validation_time))
    if breakout_index is None:
        return skipped_trade(case, config, notional_usd, split, "missing_candle_index")
    target_pct = case.target_pct_from_entry * config.target_fraction_pct / 100.0
    future = cup.contiguous_future(candles, breakout_index, case.horizon_bars, case.timeframe)
    if not future or case.entry_price <= 0:
        return skipped_trade(case, config, notional_usd, split, "missing_future")
    target_price = case.entry_price * (1.0 + target_pct / 100.0)
    stop_price = case.entry_price * (1.0 - config.stop_loss_pct / 100.0)
    for offset, candle in enumerate(future, start=1):
        hit_stop = candle.low <= stop_price
        hit_target = candle.high >= target_price
        if hit_stop:
            return trade_row(case, config, notional_usd, split, target_pct, "stop", candle, -config.stop_loss_pct, offset)
        if hit_target:
            return trade_row(case, config, notional_usd, split, target_pct, "target", candle, target_pct, offset)
    end_return = cup.pct((future[-1].close - case.entry_price) / case.entry_price)
    return trade_row(case, config, notional_usd, split, target_pct, "timeout", future[-1], end_return, len(future))


def trade_row(
    case: PatternCase,
    config: PatternConfig,
    notional_usd: float,
    split: str,
    target_pct: float,
    outcome: str,
    exit_candle: cup.Candle,
    exit_return_pct: float,
    hold_bars: int,
) -> TradeReplay:
    return TradeReplay(
        pattern=case.pattern,
        label=config.label,
        filter_name=config.filter_name,
        symbol=case.symbol,
        timeframe=case.timeframe,
        validation_time=case.validation_time,
        entry_price=case.entry_price,
        target_fraction_pct=config.target_fraction_pct,
        stop_loss_pct=config.stop_loss_pct,
        target_pct=cup.round_float(target_pct),
        outcome=outcome,
        exit_time=cup.ms_iso(exit_candle.start_time),
        exit_return_pct=cup.round_float(exit_return_pct),
        pnl_usd=cup.round_float(notional_usd * exit_return_pct / 100.0),
        hold_bars=hold_bars,
        hold_hours=hold_bars * 4.0,
        skipped=False,
        skip_reason="",
        split=split,
    )


def skipped_trade(
    case: PatternCase,
    config: PatternConfig,
    notional_usd: float,
    split: str,
    reason: str,
) -> TradeReplay:
    del notional_usd
    return TradeReplay(
        pattern=case.pattern,
        label=config.label,
        filter_name=config.filter_name,
        symbol=case.symbol,
        timeframe=case.timeframe,
        validation_time=case.validation_time,
        entry_price=case.entry_price,
        target_fraction_pct=config.target_fraction_pct,
        stop_loss_pct=config.stop_loss_pct,
        target_pct=cup.round_float(case.target_pct_from_entry * config.target_fraction_pct / 100.0),
        outcome="skipped",
        exit_time=case.validation_time,
        exit_return_pct=0.0,
        pnl_usd=0.0,
        hold_bars=0,
        hold_hours=0.0,
        skipped=True,
        skip_reason=reason,
        split=split,
    )


def summarize_segment(
    segment: str,
    trades: list[TradeReplay],
    *,
    start_time: str | None,
    end_time: str | None,
    current_baseline: dict[str, Any] | None,
    official_baseline: dict[str, Any] | None,
) -> SegmentSummary:
    scoped = [
        trade
        for trade in trades
        if in_window(trade.validation_time, start_time, end_time)
    ]
    accepted = [trade for trade in scoped if not trade.skipped]
    returns = [trade.exit_return_pct for trade in accepted]
    pnl = sum(trade.pnl_usd for trade in accepted)
    target_count = sum(1 for trade in accepted if trade.outcome == "target")
    stop_count = sum(1 for trade in accepted if trade.outcome == "stop")
    timeout_count = sum(1 for trade in accepted if trade.outcome == "timeout")
    baseline_current_pnl = float(current_baseline["total_realized_pnl_usd"]) if current_baseline else None
    baseline_official_pnl = float(official_baseline["total_realized_pnl_usd"]) if official_baseline else None
    by_pattern = defaultdict(list)
    for trade in accepted:
        by_pattern[trade.pattern].append(trade)
    by_pattern_pnl = {
        pattern: cup.round_float(sum(trade.pnl_usd for trade in rows))
        for pattern, rows in sorted(by_pattern.items())
    }
    by_pattern_trades = {pattern: len(rows) for pattern, rows in sorted(by_pattern.items())}
    by_pattern_avg_return = {
        pattern: cup.round_optional(cup.mean(trade.exit_return_pct for trade in rows))
        for pattern, rows in sorted(by_pattern.items())
    }
    by_pattern_win_rate = {
        pattern: cup.round_optional(100.0 * sum(1 for trade in rows if trade.exit_return_pct > 0) / len(rows))
        for pattern, rows in sorted(by_pattern.items())
    }
    return SegmentSummary(
        segment=segment,
        start_time=start_time,
        end_time=end_time,
        candidate_count=len(scoped),
        accepted_trades=len(accepted),
        skipped_trades=sum(1 for trade in scoped if trade.skipped),
        target_count=target_count,
        stop_count=stop_count,
        timeout_count=timeout_count,
        win_rate_pct=cup.round_optional(100.0 * sum(1 for value in returns if value > 0) / len(returns)) if returns else None,
        stop_rate_pct=cup.round_optional(100.0 * stop_count / len(accepted)) if accepted else None,
        avg_return_pct=cup.round_optional(cup.mean(returns)),
        median_return_pct=cup.round_optional(cup.median(returns)),
        total_return_pct_on_notional=cup.round_float(sum(returns)),
        pnl_usd=cup.round_float(pnl),
        profit_factor=cup.round_optional(profit_factor(returns)),
        max_drawdown_usd=cup.round_float(max_drawdown([trade.pnl_usd for trade in accepted])),
        max_open_positions=max_concurrent_positions(accepted),
        by_pattern_pnl_usd=by_pattern_pnl,
        by_pattern_trades=by_pattern_trades,
        by_pattern_avg_return_pct=by_pattern_avg_return,
        by_pattern_win_rate_pct=by_pattern_win_rate,
        baseline_current_pnl_usd=cup.round_optional(baseline_current_pnl),
        baseline_official_pnl_usd=cup.round_optional(baseline_official_pnl),
        total_with_current_baseline_usd=cup.round_optional(baseline_current_pnl + pnl) if baseline_current_pnl is not None else None,
        delta_vs_current_baseline_usd=cup.round_optional(pnl) if baseline_current_pnl is not None else None,
        total_with_official_baseline_usd=cup.round_optional(baseline_official_pnl + pnl) if baseline_official_pnl is not None else None,
        delta_vs_official_baseline_usd=cup.round_optional(pnl) if baseline_official_pnl is not None else None,
    )


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summaries = payload["summaries"]
    lines = [
        "# Chart pattern top 3 overlay replay",
        "",
        "Statut: `research_only_no_live_change`.",
        "",
        "## Methode",
        "",
        f"- Notional fixe par trade: `{payload['method']['notional_usd_per_trade']:.2f} USD`.",
        f"- Max open positions overlay: `{payload['method']['max_open_positions']}`.",
        f"- Max nouvelles positions par bougie: `{payload['method']['max_new_positions_per_bar']}`.",
        "- Intrabar conservateur: le stop gagne si TP et SL touchent dans la meme bougie.",
        "- Comparaison full-bot seulement sur la fenetre officielle de baseline.",
        "",
        "## Top 3 integres",
        "",
        "| Rang | Pattern | Filtre | Target | SL |",
        "| ---: | --- | --- | ---: | ---: |",
    ]
    for config in payload["method"]["top3"]:
        lines.append(
            f"| {config['priority']} | {config['pattern']} | `{config['filter_name']}` | {config['target_fraction_pct']:.0f}% | {config['stop_loss_pct']:.2f}% |"
        )
    lines.extend([
        "",
        "## Synthese",
        "",
        "| Segment | Candidates | Trades | Skips | TP | Stop | Timeout | Win | Avg ret | Med ret | PnL | PF | Max DD | Max open | Baseline current | Total current+overlay | Delta |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in summaries:
        lines.append(
            "| {segment} | {candidate_count} | {accepted_trades} | {skipped_trades} | {target_count} | {stop_count} | {timeout_count} | {win} | {avg} | {median} | {pnl} | {pf} | {dd} | {max_open} | {baseline} | {total} | {delta} |".format(
                segment=row["segment"],
                candidate_count=row["candidate_count"],
                accepted_trades=row["accepted_trades"],
                skipped_trades=row["skipped_trades"],
                target_count=row["target_count"],
                stop_count=row["stop_count"],
                timeout_count=row["timeout_count"],
                win=fmt_pct(row["win_rate_pct"]),
                avg=fmt_pct(row["avg_return_pct"]),
                median=fmt_pct(row["median_return_pct"]),
                pnl=fmt_usd(row["pnl_usd"]),
                pf=fmt_num(row["profit_factor"]),
                dd=fmt_usd(row["max_drawdown_usd"]),
                max_open=row["max_open_positions"],
                baseline=fmt_usd(row["baseline_current_pnl_usd"]),
                total=fmt_usd(row["total_with_current_baseline_usd"]),
                delta=fmt_usd(row["delta_vs_current_baseline_usd"]),
            )
        )
    lines.extend(["", "## Breakdown par figure", ""])
    for row in summaries:
        lines.extend([
            f"### {row['segment']}",
            "",
            "| Pattern | Trades | PnL | Avg ret | Win rate |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        patterns = sorted(row["by_pattern_trades"])
        for pattern in patterns:
            lines.append(
                f"| {pattern} | {row['by_pattern_trades'][pattern]} | {fmt_usd(row['by_pattern_pnl_usd'].get(pattern))} | {fmt_pct(row['by_pattern_avg_return_pct'].get(pattern))} | {fmt_pct(row['by_pattern_win_rate_pct'].get(pattern))} |"
            )
        lines.append("")
    lines.extend([
        "## Garde-fous",
        "",
        "- Ce replay est un sleeve synthetique sur bougies 4h, pas une activation TRIDENT.",
        "- Le delta baseline est additif et ne modelise pas la marge, la correlation avec les positions Pod A/C, ni les fees/slippage.",
        "- Un candidat positif ici doit encore etre rejoue dans un full-bot/paper harness avant toute promotion.",
        "",
        "## Fichiers",
        "",
        "- `top3_overlay_trades.csv`",
        "- `top3_overlay_summary.csv`",
        "- `top3_overlay_replay.json`",
        "- `top3_overlay_replay.md`",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def le(case: PatternCase, thresholds: dict[tuple[str, str, int], float], field: str, pct_value: int) -> bool:
    value = getattr(case, field)
    threshold = thresholds.get((case.pattern, field, pct_value))
    return value is not None and threshold is not None and value <= threshold


def ge(case: PatternCase, thresholds: dict[tuple[str, str, int], float], field: str, pct_value: int) -> bool:
    value = getattr(case, field)
    threshold = thresholds.get((case.pattern, field, pct_value))
    return value is not None and threshold is not None and value >= threshold


def threshold_payload(thresholds: dict[tuple[str, str, int], float]) -> dict[str, dict[str, dict[str, float]]]:
    payload: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for (pattern, field, pct_value), value in thresholds.items():
        payload[pattern][field][str(pct_value)] = cup.round_float(value)
    return {pattern: dict(fields) for pattern, fields in payload.items()}


def in_window(timestamp: str, start: str | None, end: str | None) -> bool:
    value = cup.iso_ms(timestamp)
    if start is not None and value < cup.iso_ms(start):
        return False
    if end is not None and value > cup.iso_ms(end):
        return False
    return True


def max_concurrent_positions(trades: list[TradeReplay]) -> int:
    events: list[tuple[int, int]] = []
    for trade in trades:
        events.append((cup.iso_ms(trade.validation_time), 1))
        events.append((cup.iso_ms(trade.exit_time), -1))
    current = 0
    max_seen = 0
    for _time, delta in sorted(events, key=lambda item: (item[0], item[1])):
        current += delta
        max_seen = max(max_seen, current)
    return max_seen


def max_drawdown(pnls: Iterable[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def profit_factor(returns: Iterable[float]) -> float | None:
    gains = sum(value for value in returns if value > 0)
    losses = -sum(value for value in returns if value < 0)
    if losses <= 0:
        return None
    return gains / losses


def write_csv(path: Path, rows: list[Any]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


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


def fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def fmt_usd(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    numeric = float(value)
    if not math.isfinite(numeric):
        return "-"
    return f"{numeric:.3f}"


if __name__ == "__main__":
    main()
