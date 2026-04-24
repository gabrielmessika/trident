from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.research.hyperliquid_top30_research import (
    DEFAULT_HOLD_BARS,
    INTERVAL_TO_MS,
    CandleRecord,
    FundingRecord,
    HyperliquidTop30Analyzer,
    HyperliquidTop30DatasetBuilder,
    RankedSymbol,
    StrategyResult,
    _dt_to_ms,
    _iso_from_ms,
    _safe_float,
)
from app.settings import load_config


def _read_gzip_json(path: Path) -> object:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _write_gzip_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_candles(path: Path) -> list[CandleRecord]:
    if not path.exists():
        return []
    payload = _read_gzip_json(path)
    if not isinstance(payload, list):
        return []
    records: list[CandleRecord] = []
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


def _read_funding(path: Path) -> list[FundingRecord]:
    if not path.exists():
        return []
    payload = _read_gzip_json(path)
    if not isinstance(payload, list):
        return []
    records: list[FundingRecord] = []
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


def _bot_crypto_symbols(config_path: str | Path) -> list[str]:
    config = load_config(config_path)
    return [
        symbol.upper()
        for symbol in (config.hyperliquid.observation_universe or [])
        if ":" not in symbol and symbol.upper() == symbol
    ]


def _effective_days(requested_days: int, intervals: list[str], *, min_daily_bars: int = 180) -> int:
    days = max(requested_days, 1)
    if "1d" in intervals and days < min_daily_bars:
        days = min_daily_bars
    if "4h" in intervals and days < 45:
        days = 45
    return days


def _selected_symbol_meta(
    builder: HyperliquidTop30DatasetBuilder,
    *,
    symbols: list[str],
) -> tuple[list[RankedSymbol], list[str]]:
    payload = builder.client.post_info({"type": "metaAndAssetCtxs"})
    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError("Unexpected metaAndAssetCtxs payload")
    meta = payload[0]
    ctxs = payload[1]
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        raise RuntimeError("Unexpected metaAndAssetCtxs payload")
    universe = meta.get("universe", [])
    if not isinstance(universe, list):
        raise RuntimeError("Unexpected universe payload")

    requested = {symbol.upper() for symbol in symbols}
    selected: list[RankedSymbol] = []
    for item, ctx in zip(universe, ctxs, strict=False):
        if not isinstance(item, dict) or not isinstance(ctx, dict):
            continue
        if bool(item.get("isDelisted", False)):
            continue
        symbol = str(item.get("name", "")).strip().upper()
        if symbol not in requested:
            continue
        mark_px = _safe_float(ctx.get("markPx"))
        day_ntl_vlm = _safe_float(ctx.get("dayNtlVlm"))
        open_interest = _safe_float(ctx.get("openInterest"))
        selected.append(
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
    selected.sort(
        key=lambda item: (symbols.index(item.symbol), -item.day_ntl_vlm)
        if item.symbol in symbols
        else (9999, -item.day_ntl_vlm)
    )
    for rank, item in enumerate(selected, start=1):
        item.rank = rank
    missing = sorted(requested - {item.symbol for item in selected})
    return selected, missing


def _dedupe_candles(records: list[CandleRecord], *, start_ms: int, end_ms: int) -> list[CandleRecord]:
    by_start: dict[int, CandleRecord] = {}
    for record in records:
        if record.end_time < start_ms or record.start_time > end_ms:
            continue
        if record.start_time <= 0:
            continue
        by_start[record.start_time] = record
    return [by_start[key] for key in sorted(by_start)]


def _dedupe_funding(records: list[FundingRecord], *, start_ms: int, end_ms: int) -> list[FundingRecord]:
    by_time: dict[int, FundingRecord] = {}
    for record in records:
        if record.time < start_ms or record.time > end_ms:
            continue
        if record.time <= 0:
            continue
        by_time[record.time] = record
    return [by_time[key] for key in sorted(by_time)]


def _coverage_dict(
    candles: list[CandleRecord],
    interval: str,
    *,
    requested_start_ms: int,
    requested_end_ms: int,
) -> dict[str, object]:
    interval_ms = INTERVAL_TO_MS[interval]
    if not candles:
        return {
            "available": False,
            "bar_count": 0,
            "interval": interval,
        }
    actual_start_ms = candles[0].start_time
    actual_end_ms = candles[-1].end_time
    expected_bars = max(1, math.floor((requested_end_ms - requested_start_ms) / interval_ms))
    return {
        "available": True,
        "bar_count": len(candles),
        "interval": interval,
        "actual_start": _iso_from_ms(actual_start_ms),
        "actual_end": _iso_from_ms(actual_end_ms),
        "coverage_days": round((actual_end_ms - actual_start_ms) / 86_400_000.0, 2),
        "coverage_ratio_vs_request": round(min(len(candles) / expected_bars, 1.0), 4),
        "full_requested_window": actual_start_ms <= requested_start_ms + interval_ms,
    }


def _source_records(
    output_path: Path,
    seed_paths: list[Path],
    *,
    kind: str,
) -> list[CandleRecord] | list[FundingRecord]:
    paths = [output_path, *seed_paths]
    records: list[CandleRecord] | list[FundingRecord] = []
    for path in paths:
        if kind == "candles":
            records.extend(_read_candles(path))  # type: ignore[arg-type]
        else:
            records.extend(_read_funding(path))  # type: ignore[arg-type]
    return records


def _collect_incremental(
    *,
    config_path: str | Path,
    output_dir: Path,
    seed_dataset_dirs: list[Path],
    symbols: list[str],
    intervals: list[str],
    requested_days: int,
) -> dict[str, object]:
    effective_days = _effective_days(requested_days, intervals)
    requested_end = datetime.now(tz=UTC)
    requested_start = requested_end - timedelta(days=effective_days)
    start_ms = _dt_to_ms(requested_start)
    end_ms = _dt_to_ms(requested_end)
    builder = HyperliquidTop30DatasetBuilder(config_path=config_path)
    ranked_symbols, missing_symbols = _selected_symbol_meta(builder, symbols=symbols)

    raw_dir = output_dir / "raw"
    candles_dir = raw_dir / "candles"
    funding_dir = raw_dir / "funding"
    candles_dir.mkdir(parents=True, exist_ok=True)
    funding_dir.mkdir(parents=True, exist_ok=True)

    availability: dict[str, dict[str, object]] = {}
    fetch_summary = {
        "candle_fetches": 0,
        "funding_fetches": 0,
        "seed_dataset_dirs": [str(path) for path in seed_dataset_dirs],
        "requested_days": requested_days,
        "effective_days": effective_days,
    }
    for interval in intervals:
        availability[interval] = {
            "requested_start": _iso_from_ms(start_ms),
            "requested_end": _iso_from_ms(end_ms),
            "symbols": {},
        }

    for ranked_symbol in ranked_symbols:
        for interval in intervals:
            interval_ms = INTERVAL_TO_MS[interval]
            output_path = candles_dir / interval / f"{ranked_symbol.symbol}.json.gz"
            seed_paths = [
                seed_dir / "raw" / "candles" / interval / f"{ranked_symbol.symbol}.json.gz"
                for seed_dir in seed_dataset_dirs
            ]
            candles = _dedupe_candles(
                _source_records(output_path, seed_paths, kind="candles"),  # type: ignore[arg-type]
                start_ms=start_ms,
                end_ms=end_ms,
            )
            fetched_batches: list[CandleRecord] = []
            if not candles:
                print(f"[candles full] {ranked_symbol.symbol} {interval}", flush=True)
                fetched_batches.extend(
                    builder._fetch_candles(
                        symbol=ranked_symbol.symbol,
                        interval=interval,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    )
                )
                fetch_summary["candle_fetches"] += 1
            else:
                latest_end = candles[-1].end_time
                earliest_start = candles[0].start_time
                expected_bars = max(1, math.floor((end_ms - start_ms) / interval_ms))
                can_backfill_full_window = expected_bars <= 5000
                if can_backfill_full_window and earliest_start > start_ms + interval_ms:
                    print(f"[candles backfill] {ranked_symbol.symbol} {interval}", flush=True)
                    fetched_batches.extend(
                        builder._fetch_candles(
                            symbol=ranked_symbol.symbol,
                            interval=interval,
                            start_ms=start_ms,
                            end_ms=earliest_start - 1,
                        )
                    )
                    fetch_summary["candle_fetches"] += 1
                if latest_end < end_ms - interval_ms:
                    print(f"[candles tail] {ranked_symbol.symbol} {interval}", flush=True)
                    fetched_batches.extend(
                        builder._fetch_candles(
                            symbol=ranked_symbol.symbol,
                            interval=interval,
                            start_ms=latest_end + 1,
                            end_ms=end_ms,
                        )
                    )
                    fetch_summary["candle_fetches"] += 1
            candles = _dedupe_candles(
                [*candles, *fetched_batches],
                start_ms=start_ms,
                end_ms=end_ms,
            )
            _write_gzip_json(output_path, [item.to_dict() for item in candles])
            availability[interval]["symbols"][ranked_symbol.symbol] = _coverage_dict(
                candles,
                interval,
                requested_start_ms=start_ms,
                requested_end_ms=end_ms,
            )

        output_path = funding_dir / f"{ranked_symbol.symbol}.json.gz"
        seed_paths = [
            seed_dir / "raw" / "funding" / f"{ranked_symbol.symbol}.json.gz"
            for seed_dir in seed_dataset_dirs
        ]
        funding = _dedupe_funding(
            _source_records(output_path, seed_paths, kind="funding"),  # type: ignore[arg-type]
            start_ms=start_ms,
            end_ms=end_ms,
        )
        fetched_funding: list[FundingRecord] = []
        if not funding:
            print(f"[funding full] {ranked_symbol.symbol}", flush=True)
            fetched_funding.extend(
                builder._fetch_funding_history(
                    symbol=ranked_symbol.symbol,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
            fetch_summary["funding_fetches"] += 1
        else:
            if funding[0].time > start_ms + 60 * 60 * 1000:
                print(f"[funding backfill] {ranked_symbol.symbol}", flush=True)
                fetched_funding.extend(
                    builder._fetch_funding_history(
                        symbol=ranked_symbol.symbol,
                        start_ms=start_ms,
                        end_ms=funding[0].time - 1,
                    )
                )
                fetch_summary["funding_fetches"] += 1
            if funding[-1].time < end_ms - 60 * 60 * 1000:
                print(f"[funding tail] {ranked_symbol.symbol}", flush=True)
                fetched_funding.extend(
                    builder._fetch_funding_history(
                        symbol=ranked_symbol.symbol,
                        start_ms=funding[-1].time + 1,
                        end_ms=end_ms,
                    )
                )
                fetch_summary["funding_fetches"] += 1
        funding = _dedupe_funding(
            [*funding, *fetched_funding],
            start_ms=start_ms,
            end_ms=end_ms,
        )
        _write_gzip_json(output_path, [item.to_dict() for item in funding])

    manifest = {
        "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "config_path": str(config_path),
        "dataset_dir": str(output_dir),
        "requested_start": requested_start.isoformat().replace("+00:00", "Z"),
        "requested_end": requested_end.isoformat().replace("+00:00", "Z"),
        "requested_days": requested_days,
        "effective_days": effective_days,
        "symbol_count": len(ranked_symbols),
        "symbols": [item.symbol for item in ranked_symbols],
        "missing_symbols": missing_symbols,
        "intervals": intervals,
        "ranking": [item.to_dict() for item in ranked_symbols],
        "availability": availability,
        "fetch_summary": fetch_summary,
        "notes": [
            "Dataset targets the current crypto observation universe from config/trident.toml.",
            "Existing local gzip candles/funding are reused and only missing heads/tails/timeframes are fetched.",
            "Requested 30d windows are widened when needed for 4h/1d pattern warmup and sample size.",
        ],
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def _score_result(result: StrategyResult) -> float:
    if result.sample_count <= 0:
        return -1_000_000.0
    profit_factor = min(max(result.profit_factor, 0.25), 5.0)
    sample_factor = math.sqrt(result.sample_count)
    hit_factor = max(result.hit_rate, 0.25)
    return result.expectancy_net_bps * sample_factor * profit_factor * hit_factor


def _confidence_label(result: StrategyResult, *, min_samples: int) -> str:
    if result.sample_count < min_samples:
        return "thin"
    if result.expectancy_net_bps >= 15.0 and result.hit_rate >= 0.52 and result.sample_count >= 20:
        return "strong"
    if result.expectancy_net_bps > 0.0:
        return "promising"
    return "weak"


def _owner_for_result(result: StrategyResult) -> str:
    if result.expectancy_net_bps <= 0:
        return "observe_only"
    if result.archetype == "trend":
        return "Pod A"
    if result.archetype == "breakout":
        return "Pod B"
    if result.pattern == "ema50_overextension_reversion":
        return "watch/veto"
    return "research_only"


def _returns_by_timestamp(candles: list[CandleRecord]) -> dict[int, float]:
    returns: dict[int, float] = {}
    for index in range(1, len(candles)):
        previous = candles[index - 1].close
        if previous <= 0:
            continue
        returns[candles[index].end_time] = (candles[index].close - previous) / previous
    return returns


def _pearson(left: list[float], right: list[float]) -> float | None:
    size = min(len(left), len(right))
    if size < 3:
        return None
    left = left[:size]
    right = right[:size]
    mean_left = sum(left) / size
    mean_right = sum(right) / size
    covariance = 0.0
    left_variance = 0.0
    right_variance = 0.0
    for left_value, right_value in zip(left, right, strict=False):
        left_delta = left_value - mean_left
        right_delta = right_value - mean_right
        covariance += left_delta * right_delta
        left_variance += left_delta * left_delta
        right_variance += right_delta * right_delta
    if left_variance <= 0.0 or right_variance <= 0.0:
        return None
    return covariance / math.sqrt(left_variance * right_variance)


def _analyze_dataset(
    *,
    dataset_dir: Path,
    output_json: Path,
    output_md: Path,
    hold_bars_options: list[int],
    min_samples: int,
) -> dict[str, object]:
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    symbols = [str(item) for item in manifest.get("symbols", [])]
    intervals = [str(item) for item in manifest.get("intervals", [])]
    ranking = {
        str(item.get("symbol")): item
        for item in manifest.get("ranking", [])
        if isinstance(item, dict) and item.get("symbol")
    }
    analyzer = HyperliquidTop30Analyzer(round_trip_cost_bps=8.0)
    all_results: list[dict[str, object]] = []
    per_symbol_results: dict[str, list[dict[str, object]]] = {symbol: [] for symbol in symbols}
    candle_data: dict[str, dict[str, list[CandleRecord]]] = {interval: {} for interval in intervals}
    funding_data: dict[str, list[FundingRecord]] = {}

    for symbol in symbols:
        funding_data[symbol] = _read_funding(dataset_dir / "raw" / "funding" / f"{symbol}.json.gz")
    for interval in intervals:
        for symbol in symbols:
            candle_data[interval][symbol] = _read_candles(
                dataset_dir / "raw" / "candles" / interval / f"{symbol}.json.gz"
            )

    for interval in intervals:
        original_hold_bars = DEFAULT_HOLD_BARS.get(interval, 4)
        for hold_bars in hold_bars_options:
            DEFAULT_HOLD_BARS[interval] = hold_bars
            for symbol in symbols:
                results = analyzer._analyze_symbol_interval(
                    symbol=symbol,
                    interval=interval,
                    candles=candle_data[interval][symbol],
                    funding=funding_data[symbol],
                )
                for result in results:
                    result_dict = result.to_dict()
                    result_dict["hold_bars"] = hold_bars
                    result_dict["score"] = round(_score_result(result), 4)
                    result_dict["confidence"] = _confidence_label(result, min_samples=min_samples)
                    result_dict["suggested_owner"] = _owner_for_result(result)
                    all_results.append(result_dict)
                    per_symbol_results[symbol].append(result_dict)
        DEFAULT_HOLD_BARS[interval] = original_hold_bars

    btc_returns = _returns_by_timestamp(candle_data.get("1h", {}).get("BTC", []))
    symbol_rows: list[dict[str, object]] = []
    for symbol in symbols:
        symbol_results = per_symbol_results[symbol]
        positive = [
            item
            for item in symbol_results
            if int(item.get("sample_count", 0)) >= min_samples
            and float(item.get("expectancy_net_bps", 0.0)) > 0.0
        ]
        if not positive:
            positive = [
                item for item in symbol_results if float(item.get("expectancy_net_bps", 0.0)) > 0.0
            ]
        positive.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        top_patterns = positive[:3]
        coverage = {
            interval: _coverage_dict(
                candle_data[interval].get(symbol, []),
                interval,
                requested_start_ms=_dt_to_ms(
                    datetime.fromisoformat(str(manifest["requested_start"]).replace("Z", "+00:00"))
                ),
                requested_end_ms=_dt_to_ms(
                    datetime.fromisoformat(str(manifest["requested_end"]).replace("Z", "+00:00"))
                ),
            )
            for interval in intervals
        }
        symbol_returns = _returns_by_timestamp(candle_data.get("1h", {}).get(symbol, []))
        btc_correlation = None
        if symbol != "BTC" and btc_returns and symbol_returns:
            common = sorted(set(btc_returns) & set(symbol_returns))
            if len(common) >= 100:
                correlation = _pearson(
                    [btc_returns[item] for item in common],
                    [symbol_returns[item] for item in common],
                )
                if correlation is not None:
                    btc_correlation = round(correlation, 4)
        row = {
            "symbol": symbol,
            "ranking": ranking.get(symbol, {}),
            "btc_correlation_1h": btc_correlation,
            "coverage": coverage,
            "top_patterns": top_patterns,
            "decision": "observe_only" if not top_patterns else str(top_patterns[0]["suggested_owner"]),
        }
        symbol_rows.append(row)

    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "dataset_dir": str(dataset_dir),
        "manifest": manifest,
        "hold_bars_options": hold_bars_options,
        "min_samples": min_samples,
        "symbols": symbol_rows,
        "all_results": all_results,
    }
    _write_json(output_json, payload)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_render_markdown(payload), encoding="utf-8")
    return payload


def _pattern_label(item: dict[str, object]) -> str:
    side_text = _side_breakdown_label(item.get("side_breakdown"))
    suffix = f"; {side_text}" if side_text else ""
    return (
        f"{item.get('pattern')} {item.get('interval')} h{item.get('hold_bars')} "
        f"{item.get('expectancy_net_bps')}bps/{item.get('sample_count')}n{suffix}"
    )


def _side_breakdown_label(raw_breakdown: object) -> str:
    if not isinstance(raw_breakdown, dict):
        return ""
    pieces: list[str] = []
    for side, label in (("long", "L"), ("short", "S")):
        stats = raw_breakdown.get(side)
        if not isinstance(stats, dict):
            continue
        sample_count = int(stats.get("sample_count") or 0)
        if sample_count <= 0:
            continue
        expectancy = float(stats.get("expectancy_net_bps") or 0.0)
        hit_rate = float(stats.get("hit_rate") or 0.0)
        pieces.append(f"{label} {expectancy:.2f}bps/{sample_count}n/{hit_rate:.0%}")
    return "; ".join(pieces)


def _render_markdown(payload: dict[str, object]) -> str:
    manifest = payload["manifest"]
    lines = [
        "# Bot Coin Pattern Matrix",
        "",
        f"- Dataset: `{payload['dataset_dir']}`",
        f"- Window: `{manifest['requested_start']}` -> `{manifest['requested_end']}`",
        f"- Requested days: `{manifest.get('requested_days')}`, effective days: `{manifest.get('effective_days')}`",
        f"- Intervals: `{', '.join(manifest['intervals'])}`",
        f"- Hold bars tested: `{', '.join(str(item) for item in payload['hold_bars_options'])}`",
        "- Pattern labels include overall expectancy, then side splits as `L/S expectancy/sample/hit-rate`.",
        "",
        "## Best Patterns By Coin",
        "",
        "| Coin | Decision | BTC corr 1h | Best patterns | Notes |",
        "|------|----------|-------------|---------------|-------|",
    ]
    for row in payload["symbols"]:
        top_patterns = row["top_patterns"]
        if top_patterns:
            pattern_text = "<br>".join(_pattern_label(item) for item in top_patterns)
            notes = ", ".join(
                sorted({str(item.get("confidence")) for item in top_patterns if item.get("confidence")})
            )
        else:
            pattern_text = "-"
            notes = "no positive pattern"
        corr = row["btc_correlation_1h"]
        lines.append(
            f"| {row['symbol']} | {row['decision']} | {corr if corr is not None else '-'} | "
            f"{pattern_text} | {notes} |"
        )
    lines.extend(
        [
            "",
            "## Data Collection",
            "",
            f"- Candle fetches: `{manifest['fetch_summary']['candle_fetches']}`",
            f"- Funding fetches: `{manifest['fetch_summary']['funding_fetches']}`",
            f"- Seed datasets: `{', '.join(manifest['fetch_summary']['seed_dataset_dirs'])}`",
            f"- Missing symbols: `{', '.join(manifest.get('missing_symbols') or []) or '-'}`",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Incrementally collect and analyze configured TRIDENT crypto coins.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--intervals", default="15m,30m,1h,2h,4h,1d")
    parser.add_argument("--hold-bars", default="1,2,3")
    parser.add_argument("--min-samples", type=int, default=8)
    parser.add_argument("--dataset-dir", default="data/research/hyperliquid_bot_coins/current")
    parser.add_argument(
        "--seed-dataset-dir",
        action="append",
        default=["data/research/hyperliquid_top30/current"],
    )
    parser.add_argument("--output-json", default="server-data/replay_reports/bot_coin_pattern_matrix_20260424.json")
    parser.add_argument("--output-md", default="server-data/replay_reports/bot_coin_pattern_matrix_20260424.md")
    parser.add_argument("--analyze-only", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = (
        [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        if args.symbols.strip()
        else _bot_crypto_symbols(args.config)
    )
    intervals = [item.strip() for item in args.intervals.split(",") if item.strip()]
    hold_bars = [int(item.strip()) for item in args.hold_bars.split(",") if item.strip()]
    dataset_dir = Path(args.dataset_dir)
    if not args.analyze_only:
        manifest = _collect_incremental(
            config_path=args.config,
            output_dir=dataset_dir,
            seed_dataset_dirs=[Path(item) for item in args.seed_dataset_dir],
            symbols=symbols,
            intervals=intervals,
            requested_days=args.days,
        )
        print(f"dataset_dir={manifest['dataset_dir']}")
        print(f"symbols={len(manifest['symbols'])}")
        print(f"effective_days={manifest['effective_days']}")
    payload = _analyze_dataset(
        dataset_dir=dataset_dir,
        output_json=Path(args.output_json),
        output_md=Path(args.output_md),
        hold_bars_options=hold_bars,
        min_samples=args.min_samples,
    )
    print(f"output_json={args.output_json}")
    print(f"output_md={args.output_md}")
    print(f"symbols_analyzed={len(payload['symbols'])}")


if __name__ == "__main__":
    main()
