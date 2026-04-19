from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.research.hyperliquid_top30_research import (
    DEFAULT_HOLD_BARS,
    FUNDING_ZSCORE_PERIODS,
    HyperliquidTop30Analyzer,
    HyperliquidTop30DatasetBuilder,
)


PATTERN_FAMILY = {
    "trend_breakout": "trend",
    "trend_pullback": "trend",
    "ichimoku_continuation": "trend",
    "vwap_reclaim": "trend",
    "squeeze_breakout": "breakout",
    "ttm_squeeze_release": "breakout",
    "range_mean_reversion": "mean_reversion",
    "funding_reversion": "mean_reversion",
    "stoch_cci_reversion": "mean_reversion",
}


def _returns_by_timestamp(candles: list[object]) -> dict[int, float]:
    result: dict[int, float] = {}
    for index in range(1, len(candles)):
        previous = candles[index - 1].close
        current = candles[index].close
        if previous > 0:
            result[candles[index].end_time] = (current - previous) / previous
    return result


def _pearson(x_values: list[float], y_values: list[float]) -> float | None:
    size = min(len(x_values), len(y_values))
    if size < 3:
        return None
    mean_x = sum(x_values) / size
    mean_y = sum(y_values) / size
    covariance = 0.0
    variance_x = 0.0
    variance_y = 0.0
    for x_value, y_value in zip(x_values, y_values, strict=False):
        dx = x_value - mean_x
        dy = y_value - mean_y
        covariance += dx * dy
        variance_x += dx * dx
        variance_y += dy * dy
    if variance_x <= 0 or variance_y <= 0:
        return None
    return covariance / math.sqrt(variance_x * variance_y)


def _impulse_pullback_expectancy(
    candles: list[object],
    *,
    hold_bars: int = 4,
    round_trip_cost_bps: float = 8.0,
) -> tuple[float | None, int]:
    returns: list[float] = []
    for index in range(5, len(candles) - hold_bars):
        start_price = candles[index - 4].close
        impulse_price = candles[index - 1].close
        pullback_price = candles[index].close
        exit_price = candles[index + hold_bars].close
        if min(start_price, impulse_price, pullback_price) <= 0:
            continue
        impulse_bps = (impulse_price - start_price) / start_price * 10_000.0
        pullback_bps = (pullback_price - impulse_price) / impulse_price * 10_000.0
        if impulse_bps >= 450.0 and -180.0 <= pullback_bps <= -20.0:
            returns.append(
                (exit_price - pullback_price) / pullback_price * 10_000.0
                - round_trip_cost_bps
            )
    if not returns:
        return None, 0
    return round(sum(returns) / len(returns), 4), len(returns)


def _similarity_family(row: dict[str, object]) -> str:
    best_2h = str(row.get("2h_best_pattern") or "")
    trend_pullback_2h = row.get("2h_trend_pullback_bps")
    trend_pullback_4h = row.get("4h_trend_pullback_bps")
    if (
        best_2h in {"trend_pullback", "trend_breakout", "ichimoku_continuation", "vwap_reclaim"}
        and isinstance(trend_pullback_2h, (int, float))
        and isinstance(trend_pullback_4h, (int, float))
        and trend_pullback_2h > 20.0
        and trend_pullback_4h > 0.0
    ):
        return "tao_like_trend"
    if best_2h in {"stoch_cci_reversion", "range_mean_reversion", "funding_reversion"}:
        return "mean_reverting_or_fade"
    if best_2h in {"ttm_squeeze_release", "squeeze_breakout"}:
        return "breakout"
    return "mixed"


def _fmt_markdown(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan Hyperliquid symbols for TAO-like structural profiles.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--target-symbol", default="TAO")
    parser.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbol list, for example TAO,XPL,BIO,PENGU,DOGE,HYPE",
    )
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--timeframes", default="2h,4h,1d")
    parser.add_argument("--round-trip-cost-bps", type=float, default=8.0)
    parser.add_argument(
        "--json-output",
        default="server-data/replay_reports/tao_like_profile_scan_latest.json",
    )
    parser.add_argument(
        "--md-output",
        default="server-data/replay_reports/tao_like_profile_scan_latest.md",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    intervals = [item.strip() for item in args.timeframes.split(",") if item.strip()]
    target_symbol = args.target_symbol.strip().upper()
    if target_symbol not in symbols:
        raise SystemExit(f"target symbol {target_symbol} must be present in --symbols")

    for interval in intervals:
        DEFAULT_HOLD_BARS.setdefault(interval, 4)
    FUNDING_ZSCORE_PERIODS.setdefault("4h", 36)
    FUNDING_ZSCORE_PERIODS.setdefault("1d", 20)

    builder = HyperliquidTop30DatasetBuilder(config_path=args.config)
    analyzer = HyperliquidTop30Analyzer(round_trip_cost_bps=args.round_trip_cost_bps)

    end = datetime.now(UTC)
    start = end - timedelta(days=max(args.days, 1))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    raw_by_symbol: dict[str, dict[str, object]] = {}

    for index, symbol in enumerate(symbols, start=1):
        print(f"[{index}/{len(symbols)}] {symbol}", flush=True)
        funding = builder._fetch_funding_history(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        symbol_payload: dict[str, object] = {"funding_count": len(funding), "intervals": {}}
        for interval in intervals:
            candles = builder._fetch_candles(
                symbol=symbol,
                interval=interval,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            interval_payload: dict[str, object] = {
                "bars": len(candles),
                "coverage_days": round(
                    (candles[-1].end_time - candles[0].start_time) / 86_400_000.0,
                    2,
                )
                if len(candles) >= 2
                else 0.0,
                "patterns": {},
                "median_atr_bps": None,
                "median_adx14": None,
                "impulse_pullback_long_expectancy_bps": None,
                "impulse_pullback_sample_count": 0,
                "returns_by_timestamp": _returns_by_timestamp(candles),
            }
            if len(candles) >= 150:
                pattern_results = analyzer._analyze_symbol_interval(
                    symbol=symbol,
                    interval=interval,
                    candles=candles,
                    funding=funding,
                )
                features = analyzer._build_features(
                    interval=interval,
                    candles=candles,
                    funding=funding,
                )
                atr_values: list[float] = []
                for atr_value, close_value in zip(
                    features["atr14"],
                    features["close"],
                    strict=False,
                ):
                    if atr_value is None or not isinstance(close_value, float) or close_value <= 0:
                        continue
                    atr_values.append(atr_value / close_value * 10_000.0)
                adx_values = [
                    float(value)
                    for value in features["adx14"]
                    if value is not None
                ]
                impulse_expectancy, impulse_sample_count = _impulse_pullback_expectancy(
                    candles,
                    hold_bars=DEFAULT_HOLD_BARS.get(interval, 4),
                    round_trip_cost_bps=args.round_trip_cost_bps,
                )
                interval_payload.update(
                    {
                        "patterns": {
                            item.pattern: item.to_dict()
                            for item in pattern_results
                        },
                        "median_atr_bps": round(statistics.median(atr_values), 4)
                        if atr_values
                        else None,
                        "median_adx14": round(statistics.median(adx_values), 4)
                        if adx_values
                        else None,
                        "impulse_pullback_long_expectancy_bps": impulse_expectancy,
                        "impulse_pullback_sample_count": impulse_sample_count,
                    }
                )
            symbol_payload["intervals"][interval] = interval_payload
        raw_by_symbol[symbol] = symbol_payload

    btc_returns = raw_by_symbol.get("BTC", {}).get("intervals", {}).get("2h", {}).get(
        "returns_by_timestamp",
        {},
    )
    rows: list[dict[str, object]] = []

    for symbol in symbols:
        row: dict[str, object] = {"symbol": symbol}
        symbol_returns = raw_by_symbol[symbol]["intervals"].get("2h", {}).get(
            "returns_by_timestamp",
            {},
        )
        if isinstance(btc_returns, dict) and isinstance(symbol_returns, dict):
            common = sorted(set(btc_returns) & set(symbol_returns))
            if len(common) >= 10:
                corr = _pearson(
                    [btc_returns[item] for item in common],
                    [symbol_returns[item] for item in common],
                )
                row["corr_btc_2h"] = round(corr, 4) if corr is not None else None
            else:
                row["corr_btc_2h"] = None
        else:
            row["corr_btc_2h"] = None

        for interval in intervals:
            entry = raw_by_symbol[symbol]["intervals"][interval]
            patterns = entry["patterns"]
            best_pattern = None
            best_expectancy = None
            best_trend = None
            best_breakout = None
            best_mean_reversion = None
            for name, payload in patterns.items():
                expectancy = payload.get("expectancy_net_bps")
                if expectancy is None:
                    continue
                if best_pattern is None or expectancy > best_expectancy:
                    best_pattern = name
                    best_expectancy = expectancy
                family = PATTERN_FAMILY.get(name)
                if family == "trend" and (best_trend is None or expectancy > best_trend):
                    best_trend = expectancy
                elif family == "breakout" and (
                    best_breakout is None or expectancy > best_breakout
                ):
                    best_breakout = expectancy
                elif family == "mean_reversion" and (
                    best_mean_reversion is None or expectancy > best_mean_reversion
                ):
                    best_mean_reversion = expectancy
            row[f"{interval}_best_pattern"] = best_pattern
            row[f"{interval}_trend_pullback_bps"] = patterns.get("trend_pullback", {}).get(
                "expectancy_net_bps"
            )
            row[f"{interval}_best_trend_bps"] = best_trend
            row[f"{interval}_best_breakout_bps"] = best_breakout
            row[f"{interval}_best_meanrev_bps"] = best_mean_reversion
            row[f"{interval}_median_atr_bps"] = entry.get("median_atr_bps")
            row[f"{interval}_median_adx14"] = entry.get("median_adx14")
            row[f"{interval}_impulse_pullback_bps"] = entry.get(
                "impulse_pullback_long_expectancy_bps"
            )
            row[f"{interval}_impulse_pullback_n"] = entry.get("impulse_pullback_sample_count")
            row[f"{interval}_coverage_days"] = entry.get("coverage_days")
        rows.append(row)

    similarity_keys = [
        "2h_trend_pullback_bps",
        "4h_trend_pullback_bps",
        "1d_trend_pullback_bps",
        "2h_impulse_pullback_bps",
        "4h_impulse_pullback_bps",
        "2h_median_atr_bps",
        "corr_btc_2h",
    ]
    means: dict[str, float] = {}
    stds: dict[str, float] = {}
    for key in similarity_keys:
        values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
        means[key] = sum(values) / len(values) if values else 0.0
        if len(values) >= 2:
            std = math.sqrt(sum((value - means[key]) ** 2 for value in values) / len(values))
            stds[key] = std or 1.0
        else:
            stds[key] = 1.0

    target = next(row for row in rows if row["symbol"] == target_symbol)
    ranked_rows: list[dict[str, object]] = []
    for row in rows:
        weighted_distance = 0.0
        total_weight = 0.0
        for key in similarity_keys:
            target_value = target.get(key)
            row_value = row.get(key)
            if not isinstance(target_value, (int, float)) or not isinstance(row_value, (int, float)):
                continue
            weight = 1.5 if "trend_pullback" in key or "impulse_pullback" in key else 1.0
            weighted_distance += weight * abs((target_value - row_value) / stds[key])
            total_weight += weight
        enriched = dict(row)
        enriched["tao_distance"] = (
            round(weighted_distance / total_weight, 4) if total_weight > 0 else None
        )
        enriched["family"] = _similarity_family(enriched)
        ranked_rows.append(enriched)
    ranked_rows.sort(
        key=lambda item: (
            999.0 if item["tao_distance"] is None else item["tao_distance"],
            str(item["symbol"]),
        )
    )

    payload = {
        "config": args.config,
        "target_symbol": target_symbol,
        "symbols": symbols,
        "intervals": intervals,
        "requested_start": start.isoformat().replace("+00:00", "Z"),
        "requested_end": end.isoformat().replace("+00:00", "Z"),
        "rows": ranked_rows,
    }

    json_path = Path(args.json_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    markdown_lines = [
        "# TAO-Like Profile Scan",
        "",
        f"- Config: `{args.config}`",
        f"- Target symbol: `{target_symbol}`",
        f"- Window: `{start.date()} -> {end.date()}`",
        f"- Timeframes: `{', '.join(intervals)}`",
        "",
        "## Closest Candidates",
        "",
        "| Symbol | Family | Dist | 2h best | 4h best | 1d best | 2h TP | 4h TP | 1d TP | 2h meanrev | 2h breakout | Corr BTC 2h |",
        "|---|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in ranked_rows:
        markdown_lines.append(
            "| "
            + " | ".join(
                [
                    str(item["symbol"]),
                    str(item["family"]),
                    _fmt_markdown(item["tao_distance"]),
                    str(item.get("2h_best_pattern") or "-"),
                    str(item.get("4h_best_pattern") or "-"),
                    str(item.get("1d_best_pattern") or "-"),
                    _fmt_markdown(item.get("2h_trend_pullback_bps")),
                    _fmt_markdown(item.get("4h_trend_pullback_bps")),
                    _fmt_markdown(item.get("1d_trend_pullback_bps")),
                    _fmt_markdown(item.get("2h_best_meanrev_bps")),
                    _fmt_markdown(item.get("2h_best_breakout_bps")),
                    _fmt_markdown(item.get("corr_btc_2h")),
                ]
            )
            + " |"
        )
    markdown_lines.extend(
        [
            "",
            "## Read",
            "",
            "- `tao_like_trend` means: trend-led on 2h, positive pullback continuation on both 2h and 4h, and not primarily a mean-reversion coin.",
            "- Coins dominated by `stoch_cci_reversion`, `funding_reversion`, or `range_mean_reversion` should not share the TAO config.",
        ]
    )

    md_path = Path(args.md_output)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")
    print(md_path)
    print(json_path)


if __name__ == "__main__":
    main()
