from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path

from app.research.hyperliquid_top30_research import CandleRecord, FundingRecord, HyperliquidTop30Analyzer
from app.trident.regime_snapshot_v2 import enrich_regime_snapshot


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
    result.sort(key=lambda item: item.end_time)
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
    result.sort(key=lambda item: item.time)
    return result


def _iso_from_ms(value: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _safe_value(series: list[float | None], index: int, default: float = 0.0) -> float:
    if index < 0 or index >= len(series):
        return default
    value = series[index]
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return default
    return float(value)


def _rolling_mean(values: list[float], index: int, window: int) -> float:
    start = max(0, index - window + 1)
    sample = values[start : index + 1]
    return sum(sample) / max(len(sample), 1)


class HyperliquidDatasetToTridentReplayConverter:
    """Builds a synthetic TRIDENT replay input from direct HL candle/funding datasets."""

    def __init__(self) -> None:
        self._analyzer = HyperliquidTop30Analyzer()

    def convert(
        self,
        *,
        dataset_dir: str | Path,
        output_path: str | Path,
        interval: str = "15m",
        leader_symbol: str = "ETH",
    ) -> dict[str, object]:
        dataset_path = Path(dataset_dir)
        manifest = json.loads((dataset_path / "manifest.json").read_text(encoding="utf-8"))
        symbols = [str(item).upper() for item in manifest.get("symbols", [])]
        if leader_symbol.upper() not in symbols:
            raise ValueError(f"leader symbol {leader_symbol} not in dataset symbols {symbols}")

        candles_by_symbol: dict[str, list[CandleRecord]] = {}
        funding_by_symbol: dict[str, list[FundingRecord]] = {}
        features_by_symbol: dict[str, dict[str, list[float | None] | list[float] | list[int]]] = {}
        candle_index_by_symbol: dict[str, dict[int, int]] = {}

        for symbol in symbols:
            candles = _read_candles(dataset_path / "raw" / "candles" / interval / f"{symbol}.json.gz")
            funding = _read_funding(dataset_path / "raw" / "funding" / f"{symbol}.json.gz")
            if not candles:
                continue
            candles_by_symbol[symbol] = candles
            funding_by_symbol[symbol] = funding
            features_by_symbol[symbol] = self._analyzer._build_features(
                interval=interval,
                candles=candles,
                funding=funding,
            )
            candle_index_by_symbol[symbol] = {
                candle.end_time: index for index, candle in enumerate(candles)
            }

        common_symbols = sorted(candles_by_symbol)
        if leader_symbol.upper() not in candles_by_symbol:
            raise ValueError(f"leader symbol {leader_symbol} has no candles in dataset")

        all_timestamps = sorted(
            {
                candle.end_time
                for candles in candles_by_symbol.values()
                for candle in candles
            }
        )
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        records_written = 0
        first_timestamp: str | None = None
        last_timestamp: str | None = None

        with output.open("w", encoding="utf-8") as handle:
            for timestamp_ms in all_timestamps:
                symbols_payload: list[dict[str, object]] = []
                leader_direction = 0.0
                leader_index = candle_index_by_symbol[leader_symbol.upper()].get(timestamp_ms)
                if leader_index is not None:
                    leader_features = features_by_symbol[leader_symbol.upper()]
                    leader_direction = _safe_value(leader_features["recent_return_8"], leader_index)

                for symbol in common_symbols:
                    index = candle_index_by_symbol[symbol].get(timestamp_ms)
                    if index is None:
                        continue
                    candles = candles_by_symbol[symbol]
                    candle = candles[index]
                    features = features_by_symbol[symbol]
                    close = candle.close
                    if close <= 0:
                        continue
                    ema_fast = _safe_value(features["ema20"], index, close)
                    ema_slow = _safe_value(features["ema50"], index, close)
                    vwap_distance_bps = _safe_value(features["vwap_distance_bps_20"], index, 0.0)
                    supertrend_direction = _safe_value(features["supertrend_direction"], index, 0.0)
                    one_bar_return_bps = _safe_value(features["one_bar_return_bps"], index, 0.0)
                    recent_return_8 = _safe_value(features["recent_return_8"], index, 0.0)
                    trend_component = (
                        ((ema_fast - ema_slow) / close) * 120.0 if close > 0 else 0.0
                    )
                    structure_score = _clamp(
                        trend_component
                        + _clamp(recent_return_8 / 180.0, -0.35, 0.35)
                        + _clamp(supertrend_direction * 0.18, -0.18, 0.18)
                    )
                    bar_range_bps = ((candle.high - candle.low) / close * 10_000.0) if close > 0 else 0.0
                    directional_bar_bias = (
                        ((candle.close - candle.open) / close) * 10_000.0 if close > 0 else 0.0
                    )
                    trade_flow_bias = _clamp(directional_bar_bias / max(bar_range_bps, 25.0), -1.0, 1.0)
                    book_imbalance = _clamp(
                        trade_flow_bias * 0.6 + _clamp(vwap_distance_bps / 80.0, -0.4, 0.4),
                        -1.0,
                        1.0,
                    )
                    volume_ratio = _safe_value(features["volume_ratio_20"], index, 1.0)
                    average_trade_count = _rolling_mean(
                        [float(item.trade_count) for item in candles],
                        index,
                        20,
                    )
                    trade_count_ratio = (
                        float(candle.trade_count) / average_trade_count if average_trade_count > 0 else 1.0
                    )
                    realized_vol_short_bps = _rolling_mean(
                        [abs(_safe_value(features["one_bar_return_bps"], i, 0.0)) for i in range(len(candles))],
                        index,
                        6,
                    )
                    realized_vol_long_bps = _rolling_mean(
                        [abs(_safe_value(features["one_bar_return_bps"], i, 0.0)) for i in range(len(candles))],
                        index,
                        24,
                    )
                    bb_width_pct = _safe_value(features["bb_width_pct"], index, 50.0)
                    squeeze_on = _safe_value(features["squeeze_on"], index, 0.0)
                    compression_score = _clamp(
                        (1.0 - min(bb_width_pct / 100.0, 1.0)) * 0.7 + squeeze_on * 0.3,
                        0.0,
                        1.0,
                    )
                    funding_rate = _safe_value(features["funding_rate"], index, 0.0)
                    leader_aligned = True
                    if symbol != leader_symbol.upper():
                        leader_aligned = (
                            one_bar_return_bps == 0.0
                            or leader_direction == 0.0
                            or (one_bar_return_bps > 0) == (leader_direction > 0)
                        )
                    spread_bps = round(
                        max(1.0, min(12.0, bar_range_bps * 0.06 + (0.5 if symbol == leader_symbol.upper() else 1.0))),
                        4,
                    )
                    symbols_payload.append(
                        {
                            "symbol": symbol,
                            "price": round(close, 8),
                            "ema_fast": round(ema_fast, 8),
                            "ema_slow": round(ema_slow, 8),
                            "vwap_distance_bps": round(vwap_distance_bps, 4),
                            "structure_score": round(structure_score, 4),
                            "funding_rate": round(funding_rate, 8),
                            "spread_bps": spread_bps,
                            "btc_aligned": leader_aligned,
                            "book_imbalance": round(book_imbalance, 4),
                            "trade_flow_bias": round(trade_flow_bias, 4),
                            "bucket_volume": round(candle.volume, 6),
                            "bucket_trade_count": int(candle.trade_count),
                            "bucket_range_bps": round(bar_range_bps, 4),
                            "bucket_notional_usd": round(candle.volume * close, 4),
                            "delta_book_imbalance": 0.0,
                            "delta_trade_flow_bias": 0.0,
                            "volume_ratio": round(volume_ratio, 4),
                            "trade_count_ratio": round(trade_count_ratio, 4),
                            "realized_vol_short_bps": round(realized_vol_short_bps, 4),
                            "realized_vol_long_bps": round(realized_vol_long_bps, 4),
                            "compression_score": round(compression_score, 4),
                            "microprice_dislocation_bps": 0.0,
                            "source": "hl_candles_synth",
                        }
                    )
                if not symbols_payload:
                    continue

                leader_snapshot = next(
                    (item for item in symbols_payload if str(item["symbol"]).upper() == leader_symbol.upper()),
                    symbols_payload[0],
                )
                regime_snapshot = enrich_regime_snapshot(
                    {
                        "ready": True,
                        "adx": round(max(8.0, min(55.0, abs(float(leader_snapshot["structure_score"])) * 65.0)), 2),
                        "atr_ratio": round(max(0.1, float(leader_snapshot["bucket_range_bps"]) / 40.0), 4),
                        "range_width_bps": round(max(float(leader_snapshot["bucket_range_bps"]), 10.0), 4),
                        "structure_score": float(leader_snapshot["structure_score"]),
                        "btc_impulse": abs(leader_direction) >= 30.0,
                    },
                    symbols_payload,
                    leader_candidates=[leader_symbol.upper()],
                    market_cluster="crypto",
                )
                timestamp = _iso_from_ms(timestamp_ms)
                handle.write(
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "regime_snapshot": regime_snapshot,
                            "symbols": symbols_payload,
                        }
                    )
                    + "\n"
                )
                records_written += 1
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp

        return {
            "dataset_dir": str(dataset_path),
            "output_path": str(output),
            "interval": interval,
            "leader_symbol": leader_symbol.upper(),
            "symbols": common_symbols,
            "records_written": records_written,
            "first_timestamp": first_timestamp,
            "last_timestamp": last_timestamp,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a direct HL dataset into a synthetic TRIDENT replay input."
    )
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--leader-symbol", default="ETH")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    summary = HyperliquidDatasetToTridentReplayConverter().convert(
        dataset_dir=args.dataset_dir,
        output_path=args.output,
        interval=args.interval,
        leader_symbol=args.leader_symbol,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
