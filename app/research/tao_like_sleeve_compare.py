from __future__ import annotations

import argparse
import gzip
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.research.hyperliquid_top30_research import (
    CandleRecord,
    DEFAULT_HOLD_BARS,
    FUNDING_ZSCORE_PERIODS,
    FundingRecord,
    HyperliquidTop30Analyzer,
)


BASELINE_PATTERNS = ("trend_pullback",)
SLEEVE_PATTERNS = ("trend_pullback", "trend_breakout", "ichimoku_continuation")


@dataclass(slots=True)
class ProfilePick:
    symbol: str
    profile: str
    interval: str | None
    pattern: str | None
    expectancy_net_bps: float
    sample_count: int
    hit_rate: float
    total_net_bps: float

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


def _best_pick(results: list[object], *, symbol: str, patterns: tuple[str, ...], profile: str) -> ProfilePick:
    eligible = [
        item
        for item in results
        if item.pattern in patterns and item.sample_count >= 1
    ]
    if not eligible:
        return ProfilePick(
            symbol=symbol,
            profile=profile,
            interval=None,
            pattern=None,
            expectancy_net_bps=0.0,
            sample_count=0,
            hit_rate=0.0,
            total_net_bps=0.0,
        )
    eligible.sort(
        key=lambda item: (item.expectancy_net_bps, item.total_net_bps, item.sample_count),
        reverse=True,
    )
    best = eligible[0]
    return ProfilePick(
        symbol=symbol,
        profile=profile,
        interval=best.interval,
        pattern=best.pattern,
        expectancy_net_bps=best.expectancy_net_bps,
        sample_count=best.sample_count,
        hit_rate=best.hit_rate,
        total_net_bps=best.total_net_bps,
    )


def _render_markdown(
    *,
    dataset_dir: Path,
    baseline_picks: list[ProfilePick],
    sleeve_picks: list[ProfilePick],
) -> str:
    baseline_total = sum(item.total_net_bps for item in baseline_picks)
    sleeve_total = sum(item.total_net_bps for item in sleeve_picks)
    baseline_samples = sum(item.sample_count for item in baseline_picks)
    sleeve_samples = sum(item.sample_count for item in sleeve_picks)
    lines = [
        "# TAO-Like Sleeve Compare",
        "",
        f"- Dataset: `{dataset_dir}`",
        "",
        "## Summary",
        "",
        f"- Baseline proxy (`trend_pullback` only): `{baseline_total:.2f} bps` across `{baseline_samples}` trades",
        f"- Special sleeve proxy (`trend_pullback / trend_breakout / ichimoku_continuation`): `{sleeve_total:.2f} bps` across `{sleeve_samples}` trades",
        f"- Delta sleeve - baseline: `{(sleeve_total - baseline_total):.2f} bps`",
        "",
        "Important: this is a research proxy on the same raw HL history, not a full snapshot replay with supervisor/routing.",
        "",
        "## Per Symbol",
        "",
        "| Symbol | Baseline interval | Baseline pattern | Baseline exp bps | Baseline total bps | Sleeve interval | Sleeve pattern | Sleeve exp bps | Sleeve total bps | Delta total bps |",
        "|---|---|---|---:|---:|---|---|---:|---:|---:|",
    ]
    sleeve_by_symbol = {item.symbol: item for item in sleeve_picks}
    for baseline in baseline_picks:
        sleeve = sleeve_by_symbol[baseline.symbol]
        lines.append(
            "| "
            + " | ".join(
                [
                    baseline.symbol,
                    baseline.interval or "-",
                    baseline.pattern or "-",
                    f"{baseline.expectancy_net_bps:.2f}",
                    f"{baseline.total_net_bps:.2f}",
                    sleeve.interval or "-",
                    sleeve.pattern or "-",
                    f"{sleeve.expectancy_net_bps:.2f}",
                    f"{sleeve.total_net_bps:.2f}",
                    f"{(sleeve.total_net_bps - baseline.total_net_bps):.2f}",
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare a TAO-like dedicated sleeve proxy against the baseline Pod A proxy.",
    )
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument(
        "--json-output",
        default="server-data/replay_reports/tao_like_sleeve_compare_latest.json",
    )
    parser.add_argument(
        "--md-output",
        default="server-data/replay_reports/tao_like_sleeve_compare_latest.md",
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

    results_by_symbol: dict[str, list[object]] = {}
    for symbol in symbols:
        funding = _read_funding(dataset_dir / "raw" / "funding" / f"{symbol}.json.gz")
        symbol_results: list[object] = []
        for interval in intervals:
            candles = _read_candles(dataset_dir / "raw" / "candles" / interval / f"{symbol}.json.gz")
            if len(candles) < 150:
                continue
            symbol_results.extend(
                analyzer._analyze_symbol_interval(
                    symbol=symbol,
                    interval=interval,
                    candles=candles,
                    funding=funding,
                )
            )
        results_by_symbol[symbol] = symbol_results

    baseline_picks = [
        _best_pick(results_by_symbol.get(symbol, []), symbol=symbol, patterns=BASELINE_PATTERNS, profile="baseline")
        for symbol in symbols
    ]
    sleeve_picks = [
        _best_pick(results_by_symbol.get(symbol, []), symbol=symbol, patterns=SLEEVE_PATTERNS, profile="sleeve")
        for symbol in symbols
    ]

    payload = {
        "dataset_dir": str(dataset_dir),
        "symbols": symbols,
        "intervals": intervals,
        "baseline_proxy_patterns": list(BASELINE_PATTERNS),
        "sleeve_proxy_patterns": list(SLEEVE_PATTERNS),
        "baseline_picks": [item.to_dict() for item in baseline_picks],
        "sleeve_picks": [item.to_dict() for item in sleeve_picks],
        "baseline_total_net_bps": round(sum(item.total_net_bps for item in baseline_picks), 4),
        "sleeve_total_net_bps": round(sum(item.total_net_bps for item in sleeve_picks), 4),
        "delta_total_net_bps": round(
            sum(item.total_net_bps for item in sleeve_picks) - sum(item.total_net_bps for item in baseline_picks),
            4,
        ),
    }

    json_output = Path(args.json_output)
    md_output = Path(args.md_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    md_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_output.write_text(
        _render_markdown(
            dataset_dir=dataset_dir,
            baseline_picks=baseline_picks,
            sleeve_picks=sleeve_picks,
        ),
        encoding="utf-8",
    )
    print(json_output)
    print(md_output)


if __name__ == "__main__":
    main()
