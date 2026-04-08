"""Converts historical candle + funding JSONL files into TRIDENT backtest snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(slots=True)
class CandleRow:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int


@dataclass(slots=True)
class FundingRow:
    timestamp_ms: int
    coin: str
    funding_rate: float
    premium: float


class CandleToSnapshotConverter:
    """Builds TRIDENT-compatible snapshot JSONL from historical candles and funding rates.

    This trades L2 microstructure granularity (spread, book depth, trade flow)
    for much deeper historical coverage (weeks/months instead of days).
    """

    def convert(
        self,
        *,
        candle_dir: str | Path,
        funding_dir: str | Path | None = None,
        date: str,
        coins: list[str],
        interval: str = "1h",
        output_path: str | Path,
    ) -> int:
        candle_root = Path(candle_dir)
        funding_root = Path(funding_dir) if funding_dir else None

        candles_by_coin = {
            coin: self._load_candles(candle_root / interval / coin, date) for coin in coins
        }
        funding_by_coin: dict[str, dict[int, FundingRow]] = {}
        if funding_root is not None:
            funding_by_coin = {
                coin: self._load_funding(funding_root / coin, date) for coin in coins
            }

        all_timestamps = sorted(
            {ts for rows in candles_by_coin.values() for ts in rows.keys()}
        )

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        state_by_symbol: dict[str, dict[str, float]] = {}
        written = 0

        with output.open("w", encoding="utf-8") as handle:
            for ts in all_timestamps:
                symbols = []
                momentum_by_symbol: dict[str, float] = {}

                for coin in coins:
                    candle = candles_by_coin.get(coin, {}).get(ts)
                    if candle is None:
                        continue

                    price = candle.close
                    state = state_by_symbol.setdefault(
                        coin,
                        {
                            "ema_fast": price,
                            "ema_slow": price,
                            "last_price": price,
                            "recent_high": price,
                            "recent_low": price,
                        },
                    )
                    state["ema_fast"] = _ema(state["ema_fast"], price, alpha=0.35)
                    state["ema_slow"] = _ema(state["ema_slow"], price, alpha=0.12)
                    last_price = state["last_price"]
                    momentum_bps = (
                        (price - last_price) / last_price * 10_000 if last_price else 0.0
                    )
                    state["last_price"] = price
                    state["recent_high"] = max(
                        _ema(state["recent_high"], candle.high, 0.1), candle.high
                    )
                    state["recent_low"] = min(
                        _ema(state["recent_low"], candle.low, 0.1), candle.low
                    )
                    momentum_by_symbol[coin] = momentum_bps

                    # Estimate spread from candle range (rough proxy)
                    candle_range_bps = (
                        (candle.high - candle.low) / price * 10_000 if price else 0.0
                    )
                    estimated_spread_bps = max(candle_range_bps * 0.02, 0.5)

                    # VWAP proxy from candle OHLC
                    vwap_proxy = (candle.open + candle.high + candle.low + candle.close) / 4.0

                    trend_score = _clamp(
                        ((state["ema_fast"] - state["ema_slow"]) / price) * 100.0,
                        -1.0,
                        1.0,
                    )

                    # Flow bias from candle direction
                    candle_body = candle.close - candle.open
                    candle_full = candle.high - candle.low
                    flow_bias = (candle_body / candle_full) if candle_full > 0 else 0.0
                    flow_bias = _clamp(flow_bias, -1.0, 1.0)

                    structure_score = _clamp(
                        trend_score * 0.60 + flow_bias * 0.40,
                        -1.0,
                        1.0,
                    )

                    range_width_bps = _range_width_bps(
                        state["recent_low"], state["recent_high"], price
                    )

                    # Lookup funding for this coin at this hour
                    funding_rate = 0.0
                    premium = 0.0
                    funding_rows = funding_by_coin.get(coin, {})
                    funding_row = _nearest_funding(funding_rows, ts)
                    if funding_row is not None:
                        funding_rate = funding_row.funding_rate
                        premium = funding_row.premium

                    symbols.append(
                        {
                            "symbol": coin,
                            "price": round(price, 8),
                            "ema_fast": round(state["ema_fast"], 8),
                            "ema_slow": round(state["ema_slow"], 8),
                            "vwap_distance_bps": round(
                                ((price - vwap_proxy) / price * 10_000) if price else 0.0,
                                4,
                            ),
                            "structure_score": round(structure_score, 4),
                            "funding_rate": round(funding_rate, 8),
                            "spread_bps": round(estimated_spread_bps, 4),
                            "btc_aligned": True,
                            "book_imbalance": round(flow_bias * 0.5, 4),
                            "trade_flow_bias": round(flow_bias, 4),
                            "bucket_volume": round(candle.volume, 6),
                            "bucket_trade_count": candle.trade_count,
                            "bucket_range_bps": round(candle_range_bps, 4),
                            "source": "candle_funding_converter",
                        }
                    )

                if not symbols:
                    continue

                # BTC alignment
                btc_mom = momentum_by_symbol.get("BTC")
                if btc_mom is not None:
                    for symbol in symbols:
                        sym_mom = momentum_by_symbol.get(symbol["symbol"], 0.0)
                        symbol["btc_aligned"] = (
                            sym_mom == 0 or btc_mom == 0 or (sym_mom > 0) == (btc_mom > 0)
                        )

                leader = next((s for s in symbols if s["symbol"] == "BTC"), symbols[0])
                leader_range = float(leader.get("bucket_range_bps", 10.0))
                regime_snapshot = {
                    "ready": True,
                    "adx": round(
                        min(
                            55.0,
                            abs(float(leader["structure_score"])) * 70
                            + abs(momentum_by_symbol.get(leader["symbol"], 0.0)) / 3.0,
                        ),
                        2,
                    ),
                    "atr_ratio": round(max(leader_range / 30.0, 0.1), 4),
                    "range_width_bps": round(max(leader_range, 10.0), 4),
                    "structure_score": leader["structure_score"],
                    "btc_impulse": abs(momentum_by_symbol.get("BTC", 0.0)) >= 10.0,
                }

                payload = {
                    "timestamp": _ms_to_iso(ts),
                    "regime_snapshot": regime_snapshot,
                    "symbols": symbols,
                }
                handle.write(json.dumps(payload) + "\n")
                written += 1

        return written

    def _load_candles(self, coin_dir: Path, date: str) -> dict[int, CandleRow]:
        path = coin_dir / f"{date}.jsonl"
        rows: dict[int, CandleRow] = {}
        if not path.exists():
            return rows
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                ts = int(data["t"])
                rows[ts] = CandleRow(
                    timestamp_ms=ts,
                    open=float(data["o"]),
                    high=float(data["h"]),
                    low=float(data["l"]),
                    close=float(data["c"]),
                    volume=float(data["v"]),
                    trade_count=int(data.get("n", 0)),
                )
        return rows

    def _load_funding(self, coin_dir: Path, date: str) -> dict[int, FundingRow]:
        path = coin_dir / f"{date}.jsonl"
        rows: dict[int, FundingRow] = {}
        if not path.exists():
            return rows
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                ts = int(data.get("time", 0))
                rows[ts] = FundingRow(
                    timestamp_ms=ts,
                    coin=str(data.get("coin", "")),
                    funding_rate=float(data.get("fundingRate", 0.0)),
                    premium=float(data.get("premium", 0.0)),
                )
        return rows


def _nearest_funding(
    rows: dict[int, FundingRow],
    target_ms: int,
) -> FundingRow | None:
    if not rows:
        return None
    # Funding is hourly; find closest timestamp <= target
    best_ts = None
    for ts in rows:
        if ts <= target_ms:
            if best_ts is None or ts > best_ts:
                best_ts = ts
    return rows.get(best_ts) if best_ts is not None else None


def _ema(prev: float, value: float, alpha: float) -> float:
    return prev * (1 - alpha) + value * alpha


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _range_width_bps(recent_low: float, recent_high: float, price: float) -> float:
    if price <= 0 or recent_low <= 0 or recent_high <= 0:
        return 0.0
    return (recent_high - recent_low) / price * 10_000


def _ms_to_iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
