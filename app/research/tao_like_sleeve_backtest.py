from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.research.hyperliquid_top30_research import (
    CandleRecord,
    DEFAULT_HOLD_BARS,
    FUNDING_ZSCORE_PERIODS,
    FundingRecord,
    HyperliquidTop30Analyzer,
    PATTERN_TO_ARCHETYPE,
    StrategyResult,
    _iso_from_ms,
)


BASELINE_PATTERNS = ("trend_pullback",)
SLEEVE_PATTERNS = ("trend_pullback", "trend_breakout", "ichimoku_continuation")


@dataclass(slots=True)
class TradeEvent:
    symbol: str
    profile: str
    interval: str
    pattern: str
    side: str
    entry_time: str
    exit_time: str
    net_bps: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_gzip_json(path: Path) -> object:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _read_candles(path: Path) -> list[CandleRecord]:
    payload = _read_gzip_json(path)
    if not isinstance(payload, list):
        return []
    result: list[CandleRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        result.append(
            CandleRecord(
                start_time=int(item.get("start_time", 0)),
                end_time=int(item.get("end_time", 0)),
                interval=str(item.get("interval", "")),
                symbol=str(item.get("symbol", "")),
                open=float(item.get("open", 0.0)),
                high=float(item.get("high", 0.0)),
                low=float(item.get("low", 0.0)),
                close=float(item.get("close", 0.0)),
                volume=float(item.get("volume", 0.0)),
                trade_count=int(item.get("trade_count", 0)),
            )
        )
    return result


def _read_funding(path: Path) -> list[FundingRecord]:
    payload = _read_gzip_json(path)
    if not isinstance(payload, list):
        return []
    result: list[FundingRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        result.append(
            FundingRecord(
                symbol=str(item.get("symbol", "")),
                time=int(item.get("time", 0)),
                funding_rate=float(item.get("funding_rate", 0.0)),
                premium=float(item.get("premium", 0.0)),
            )
        )
    return result


def _signal_map(analyzer: HyperliquidTop30Analyzer):
    return {
        "trend_breakout": analyzer._trend_breakout_signal,
        "trend_pullback": analyzer._trend_pullback_signal,
        "ichimoku_continuation": analyzer._ichimoku_continuation_signal,
        "vwap_reclaim": analyzer._vwap_reclaim_signal,
        "squeeze_breakout": analyzer._squeeze_breakout_signal,
        "ttm_squeeze_release": analyzer._ttm_squeeze_release_signal,
        "range_mean_reversion": analyzer._range_mean_reversion_signal,
        "funding_reversion": analyzer._funding_reversion_signal,
        "stoch_cci_reversion": analyzer._stoch_cci_reversion_signal,
    }


def _trade_log(
    analyzer: HyperliquidTop30Analyzer,
    *,
    symbol: str,
    profile: str,
    interval: str,
    candles: list[CandleRecord],
    features: dict[str, list[float | None] | list[float] | list[int]],
    pattern: str,
) -> tuple[StrategyResult, list[TradeEvent]] | None:
    signal_fn = _signal_map(analyzer)[pattern]
    hold_bars = DEFAULT_HOLD_BARS[interval]
    trade_returns: list[float] = []
    winners: list[float] = []
    losers: list[float] = []
    next_allowed_index = 0
    long_count = 0
    short_count = 0
    first_signal_time: str | None = None
    last_signal_time: str | None = None
    events: list[TradeEvent] = []

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
        net_return = aligned_return - analyzer.round_trip_cost_bps
        trade_returns.append(net_return)
        if net_return >= 0:
            winners.append(net_return)
        else:
            losers.append(net_return)
        timestamp = timestamps[index]
        exit_timestamp = timestamps[index + hold_bars]
        if isinstance(timestamp, int):
            iso_time = _iso_from_ms(timestamp)
            if first_signal_time is None:
                first_signal_time = iso_time
            last_signal_time = iso_time
            events.append(
                TradeEvent(
                    symbol=symbol,
                    profile=profile,
                    interval=interval,
                    pattern=pattern,
                    side=side,
                    entry_time=iso_time,
                    exit_time=_iso_from_ms(exit_timestamp) if isinstance(exit_timestamp, int) else iso_time,
                    net_bps=round(net_return, 4),
                )
            )
        next_allowed_index = index + hold_bars

    if not trade_returns:
        return (
            StrategyResult(
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
            ),
            [],
        )

    gross_expectancy = sum(value + analyzer.round_trip_cost_bps for value in trade_returns) / len(trade_returns)
    gross_profit = sum(value for value in winners if value > 0)
    gross_loss = abs(sum(value for value in losers if value < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    result = StrategyResult(
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
        profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else 999.0,
        avg_winner_bps=round(sum(winners) / len(winners), 4) if winners else 0.0,
        avg_loser_bps=round(sum(losers) / len(losers), 4) if losers else 0.0,
        median_net_bps=round(median(trade_returns), 4),
        total_net_bps=round(sum(trade_returns), 4),
        first_signal_time=first_signal_time,
        last_signal_time=last_signal_time,
    )
    return result, events


def _best_candidate(
    candidates: list[tuple[StrategyResult, list[TradeEvent]]],
    *,
    allowed_patterns: tuple[str, ...],
) -> tuple[StrategyResult | None, list[TradeEvent]]:
    eligible = [
        item for item in candidates
        if item[0].pattern in allowed_patterns and item[0].sample_count >= 1
    ]
    if not eligible:
        return None, []
    eligible.sort(
        key=lambda item: (item[0].expectancy_net_bps, item[0].total_net_bps, item[0].sample_count),
        reverse=True,
    )
    return eligible[0]


def _render_markdown(
    *,
    dataset_dir: Path,
    baseline_total: float,
    sleeve_total: float,
    baseline_daily: dict[str, float],
    sleeve_daily: dict[str, float],
    baseline_selected: list[StrategyResult],
    sleeve_selected: list[StrategyResult],
) -> str:
    all_days = sorted(set(baseline_daily) | set(sleeve_daily))
    lines = [
        "# TAO-Like Sleeve Backtest",
        "",
        f"- Dataset: `{dataset_dir}`",
        "",
        "## Summary",
        "",
        f"- Baseline total: `{baseline_total:.2f} bps`",
        f"- Sleeve total: `{sleeve_total:.2f} bps`",
        f"- Delta: `{(sleeve_total - baseline_total):.2f} bps`",
        "",
        "## Daily",
        "",
        "| Date | Baseline bps | Sleeve bps | Delta bps |",
        "|---|---:|---:|---:|",
    ]
    for day in all_days:
        baseline_value = baseline_daily.get(day, 0.0)
        sleeve_value = sleeve_daily.get(day, 0.0)
        lines.append(f"| {day} | {baseline_value:.2f} | {sleeve_value:.2f} | {(sleeve_value - baseline_value):.2f} |")

    lines.extend(
        [
            "",
            "## Selected Baseline Patterns",
            "",
            "| Symbol | Interval | Pattern | Trades | Exp bps | Total bps |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for item in baseline_selected:
        lines.append(
            f"| {item.symbol} | {item.interval} | {item.pattern} | {item.sample_count} | {item.expectancy_net_bps:.2f} | {item.total_net_bps:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Selected Sleeve Patterns",
            "",
            "| Symbol | Interval | Pattern | Trades | Exp bps | Total bps |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for item in sleeve_selected:
        lines.append(
            f"| {item.symbol} | {item.interval} | {item.pattern} | {item.sample_count} | {item.expectancy_net_bps:.2f} | {item.total_net_bps:.2f} |"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a research backtest for the TAO-like sleeve dataset.")
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument(
        "--json-output",
        default="server-data/replay_reports/tao_like_sleeve_backtest_latest.json",
    )
    parser.add_argument(
        "--md-output",
        default="server-data/replay_reports/tao_like_sleeve_backtest_latest.md",
    )
    parser.add_argument("--round-trip-cost-bps", type=float, default=8.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    dataset_dir = Path(args.dataset_dir)
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    symbols = [str(item).upper() for item in manifest.get("symbols", [])]
    intervals = [str(item) for item in manifest.get("intervals", [])]
    for interval in intervals:
        DEFAULT_HOLD_BARS.setdefault(interval, 4)
    FUNDING_ZSCORE_PERIODS.setdefault("4h", 36)
    FUNDING_ZSCORE_PERIODS.setdefault("1d", 20)
    analyzer = HyperliquidTop30Analyzer(round_trip_cost_bps=args.round_trip_cost_bps)

    baseline_selected: list[StrategyResult] = []
    sleeve_selected: list[StrategyResult] = []
    baseline_events: list[TradeEvent] = []
    sleeve_events: list[TradeEvent] = []

    for symbol in symbols:
        funding = _read_funding(dataset_dir / "raw" / "funding" / f"{symbol}.json.gz")
        candidates_baseline: list[tuple[StrategyResult, list[TradeEvent]]] = []
        candidates_sleeve: list[tuple[StrategyResult, list[TradeEvent]]] = []
        for interval in intervals:
            candles = _read_candles(dataset_dir / "raw" / "candles" / interval / f"{symbol}.json.gz")
            if len(candles) < 150:
                continue
            features = analyzer._build_features(interval=interval, candles=candles, funding=funding)
            for pattern in set(BASELINE_PATTERNS + SLEEVE_PATTERNS):
                evaluated = _trade_log(
                    analyzer,
                    symbol=symbol,
                    profile="candidate",
                    interval=interval,
                    candles=candles,
                    features=features,
                    pattern=pattern,
                )
                if evaluated is None:
                    continue
                if pattern in BASELINE_PATTERNS:
                    candidates_baseline.append(evaluated)
                if pattern in SLEEVE_PATTERNS:
                    candidates_sleeve.append(evaluated)

        baseline_pick, baseline_trade_log = _best_candidate(candidates_baseline, allowed_patterns=BASELINE_PATTERNS)
        sleeve_pick, sleeve_trade_log = _best_candidate(candidates_sleeve, allowed_patterns=SLEEVE_PATTERNS)
        if baseline_pick is not None:
            baseline_selected.append(baseline_pick)
            for item in baseline_trade_log:
                item.profile = "baseline"
            baseline_events.extend(baseline_trade_log)
        if sleeve_pick is not None:
            sleeve_selected.append(sleeve_pick)
            for item in sleeve_trade_log:
                item.profile = "sleeve"
            sleeve_events.extend(sleeve_trade_log)

    baseline_daily: dict[str, float] = defaultdict(float)
    sleeve_daily: dict[str, float] = defaultdict(float)
    for item in baseline_events:
        baseline_daily[item.exit_time[:10]] += item.net_bps
    for item in sleeve_events:
        sleeve_daily[item.exit_time[:10]] += item.net_bps

    baseline_total = round(sum(item.net_bps for item in baseline_events), 4)
    sleeve_total = round(sum(item.net_bps for item in sleeve_events), 4)

    payload = {
        "dataset_dir": str(dataset_dir),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "symbols": symbols,
        "intervals": intervals,
        "baseline_patterns": list(BASELINE_PATTERNS),
        "sleeve_patterns": list(SLEEVE_PATTERNS),
        "baseline_total_net_bps": baseline_total,
        "sleeve_total_net_bps": sleeve_total,
        "delta_total_net_bps": round(sleeve_total - baseline_total, 4),
        "baseline_daily_net_bps": {key: round(value, 4) for key, value in sorted(baseline_daily.items())},
        "sleeve_daily_net_bps": {key: round(value, 4) for key, value in sorted(sleeve_daily.items())},
        "baseline_selected": [asdict(item) for item in baseline_selected],
        "sleeve_selected": [asdict(item) for item in sleeve_selected],
        "baseline_events": [item.to_dict() for item in baseline_events],
        "sleeve_events": [item.to_dict() for item in sleeve_events],
    }

    json_output = Path(args.json_output)
    md_output = Path(args.md_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    md_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_output.write_text(
        _render_markdown(
            dataset_dir=dataset_dir,
            baseline_total=baseline_total,
            sleeve_total=sleeve_total,
            baseline_daily=dict(sorted(baseline_daily.items())),
            sleeve_daily=dict(sorted(sleeve_daily.items())),
            baseline_selected=baseline_selected,
            sleeve_selected=sleeve_selected,
        ),
        encoding="utf-8",
    )
    print(json_output)
    print(md_output)


if __name__ == "__main__":
    main()
