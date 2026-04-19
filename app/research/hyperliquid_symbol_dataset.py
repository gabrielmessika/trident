from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.research.hyperliquid_top30_research import (
    HyperliquidTop30DatasetBuilder,
    INTERVAL_TO_MS,
    RankedSymbol,
    _dt_to_ms,
    _iso_from_ms,
    _safe_float,
)

EXTRA_INTERVAL_TO_MS = {
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


def _coverage_dict(
    builder: HyperliquidTop30DatasetBuilder,
    candles: list[object],
    interval: str,
    *,
    requested_start_ms: int,
    requested_end_ms: int,
) -> dict[str, object]:
    if interval in INTERVAL_TO_MS:
        return builder._coverage_dict(
            candles,
            interval,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
        )
    interval_ms = EXTRA_INTERVAL_TO_MS.get(interval)
    if not candles or interval_ms is None:
        return {
            "available": False,
            "bar_count": 0,
            "interval": interval,
        }
    actual_start_ms = candles[0].start_time
    actual_end_ms = candles[-1].end_time
    expected_bars = max(1, (requested_end_ms - requested_start_ms) // interval_ms)
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


def _selected_symbol_meta(
    builder: HyperliquidTop30DatasetBuilder,
    *,
    symbols: list[str],
) -> list[RankedSymbol]:
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
        key=lambda item: (item.day_ntl_vlm, item.open_interest_usd, item.max_leverage),
        reverse=True,
    )
    for rank, item in enumerate(selected, start=1):
        item.rank = rank
    missing = sorted(requested - {item.symbol for item in selected})
    if missing:
        raise RuntimeError(f"Symbols not found in current HL perp universe: {', '.join(missing)}")
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect arbitrary Hyperliquid perp symbol history into a reusable dataset.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbol list")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--intervals", default="2h,4h,1d")
    parser.add_argument(
        "--output-dir",
        default="server-data/research/hyperliquid_symbols/latest",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    intervals = [item.strip() for item in args.intervals.split(",") if item.strip()]
    if not symbols:
        raise SystemExit("No symbols provided")
    if not intervals:
        raise SystemExit("No intervals provided")

    builder = HyperliquidTop30DatasetBuilder(config_path=args.config)
    ranked_symbols = _selected_symbol_meta(builder, symbols=symbols)

    requested_end = datetime.now(tz=UTC)
    requested_start = requested_end - timedelta(days=max(args.days, 1))
    start_ms = _dt_to_ms(requested_start)
    end_ms = _dt_to_ms(requested_end)

    output_path = Path(args.output_dir)
    raw_dir = output_path / "raw"
    candles_dir = raw_dir / "candles"
    funding_dir = raw_dir / "funding"
    candles_dir.mkdir(parents=True, exist_ok=True)
    funding_dir.mkdir(parents=True, exist_ok=True)

    availability: dict[str, dict[str, object]] = {}
    for interval in intervals:
        availability[interval] = {
            "requested_start": _iso_from_ms(start_ms),
            "requested_end": _iso_from_ms(end_ms),
            "symbols": {},
        }

    total_candle_jobs = len(ranked_symbols) * len(intervals)
    candle_job_index = 0
    for ranked_symbol in ranked_symbols:
        for interval in intervals:
            candle_job_index += 1
            print(f"[candles {candle_job_index}/{total_candle_jobs}] {ranked_symbol.symbol} {interval}", flush=True)
            candles = builder._fetch_candles(
                symbol=ranked_symbol.symbol,
                interval=interval,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            interval_dir = candles_dir / interval
            interval_dir.mkdir(parents=True, exist_ok=True)
            builder._write_gzip_json(
                interval_dir / f"{ranked_symbol.symbol}.json.gz",
                [item.to_dict() for item in candles],
            )
            availability[interval]["symbols"][ranked_symbol.symbol] = _coverage_dict(
                builder,
                candles,
                interval,
                requested_start_ms=start_ms,
                requested_end_ms=end_ms,
            )

    for index, ranked_symbol in enumerate(ranked_symbols, start=1):
        print(f"[funding {index}/{len(ranked_symbols)}] {ranked_symbol.symbol}", flush=True)
        funding = builder._fetch_funding_history(
            symbol=ranked_symbol.symbol,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        builder._write_gzip_json(
            funding_dir / f"{ranked_symbol.symbol}.json.gz",
            [item.to_dict() for item in funding],
        )

    manifest = {
        "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "config_path": args.config,
        "dataset_dir": str(output_path),
        "requested_start": requested_start.isoformat().replace("+00:00", "Z"),
        "requested_end": requested_end.isoformat().replace("+00:00", "Z"),
        "symbol_count": len(ranked_symbols),
        "intervals": intervals,
        "symbols": [item.symbol for item in ranked_symbols],
        "ranking": [item.to_dict() for item in ranked_symbols],
        "availability": availability,
        "notes": [
            "Dataset built from current Hyperliquid perp universe for an arbitrary symbol subset.",
            "candleSnapshot only exposes the most recent 5000 candles per interval.",
            "fundingHistory is paginated and collected across the requested window.",
        ],
    }
    (output_path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(output_path / "manifest.json")


if __name__ == "__main__":
    main()
