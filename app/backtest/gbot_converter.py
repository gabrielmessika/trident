from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from app.trident.regime_snapshot_v2 import enrich_regime_snapshot


@dataclass(slots=True)
class BucketRow:
    timestamp: int
    symbol: str
    price: float
    spread_bps: float
    bid_depth_10bps: float
    ask_depth_10bps: float


@dataclass(slots=True)
class TradeBucket:
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    vwap_numerator: float = 0.0
    total_volume: float = 0.0

    @property
    def trade_count(self) -> int:
        return self.buy_count + self.sell_count

    @property
    def vwap(self) -> float | None:
        if self.total_volume <= 0:
            return None
        return self.vwap_numerator / self.total_volume

    @property
    def flow_bias(self) -> float:
        if self.total_volume <= 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / self.total_volume


class GbotL2ToTridentConverter:
    """Converts gbot L2 JSONL files into TRIDENT snapshot JSONL records."""

    def __init__(self, bucket_ms: int = 60_000) -> None:
        self.bucket_ms = bucket_ms

    def convert(
        self,
        *,
        data_dir: str | Path,
        date: str,
        coins: list[str],
        output_path: str | Path,
    ) -> int:
        data_root = Path(data_dir)
        rows_by_coin = {coin: self._load_coin_rows(data_root, coin, date) for coin in coins}
        trades_by_coin = {
            coin: self._load_trade_buckets(data_root, coin, date) for coin in coins
        }
        all_buckets = sorted(
            {bucket for rows in rows_by_coin.values() for bucket in rows.keys()}
        )
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        state_by_symbol: dict[str, dict[str, float]] = {}
        written = 0
        with output.open("w", encoding="utf-8") as handle:
            for bucket in all_buckets:
                symbols = []
                momentum_by_symbol: dict[str, float] = {}

                for coin in coins:
                    row = rows_by_coin.get(coin, {}).get(bucket)
                    if row is None:
                        continue
                    trades = trades_by_coin.get(coin, {}).get(bucket, TradeBucket())
                    state = state_by_symbol.setdefault(
                        coin,
                        {
                            "ema_fast": row.price,
                            "ema_slow": row.price,
                            "last_price": row.price,
                            "recent_high": row.price,
                            "recent_low": row.price,
                        },
                    )
                    state["ema_fast"] = self._ema(state["ema_fast"], row.price, alpha=0.35)
                    state["ema_slow"] = self._ema(state["ema_slow"], row.price, alpha=0.12)
                    last_price = state["last_price"]
                    momentum_bps = (
                        (row.price - last_price) / last_price * 10_000 if last_price else 0.0
                    )
                    state["last_price"] = row.price
                    state["recent_high"] = max(self._ema(state["recent_high"], row.price, 0.1), row.price)
                    state["recent_low"] = min(self._ema(state["recent_low"], row.price, 0.1), row.price)
                    momentum_by_symbol[coin] = momentum_bps

                    book_imbalance = self._book_imbalance(row.bid_depth_10bps, row.ask_depth_10bps)
                    trend_score = self._clamp(
                        ((state["ema_fast"] - state["ema_slow"]) / row.price) * 100.0,
                        -1.0,
                        1.0,
                    )
                    structure_score = self._clamp(
                        trend_score * 0.55 + trades.flow_bias * 0.25 + book_imbalance * 0.20,
                        -1.0,
                        1.0,
                    )
                    vwap_anchor = trades.vwap if trades.vwap is not None else state["ema_fast"]
                    range_width_bps = self._range_width_bps(
                        state["recent_low"], state["recent_high"], row.price
                    )

                    symbols.append(
                        {
                            "symbol": coin,
                            "price": round(row.price, 8),
                            "ema_fast": round(state["ema_fast"], 8),
                            "ema_slow": round(state["ema_slow"], 8),
                            "vwap_distance_bps": round(
                                ((row.price - vwap_anchor) / row.price * 10_000) if row.price else 0.0,
                                4,
                            ),
                            "structure_score": round(structure_score, 4),
                            "funding_rate": 0.0,
                            "spread_bps": round(row.spread_bps, 4),
                            "btc_aligned": True,  # updated below when BTC exists
                            "book_imbalance": round(book_imbalance, 4),
                            "trade_flow_bias": round(trades.flow_bias, 4),
                            "bucket_volume": round(trades.total_volume, 6),
                            "bucket_trade_count": trades.trade_count,
                            "bucket_range_bps": round(range_width_bps, 4),
                            "source": "gbot_l2_trades_converter",
                        }
                    )

                if not symbols:
                    continue

                btc_mom = momentum_by_symbol.get("BTC")
                if btc_mom is not None:
                    for symbol in symbols:
                        sym_mom = momentum_by_symbol.get(symbol["symbol"], 0.0)
                        symbol["btc_aligned"] = sym_mom == 0 or btc_mom == 0 or (sym_mom > 0) == (btc_mom > 0)

                leader = next((s for s in symbols if s["symbol"] == "BTC"), symbols[0])
                leader_range = float(leader.get("bucket_range_bps", 10.0))
                regime_snapshot = enrich_regime_snapshot(
                    {
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
                    },
                    symbols,
                )

                payload = {
                    "timestamp": self._timestamp_to_iso(bucket * self.bucket_ms),
                    "regime_snapshot": regime_snapshot,
                    "symbols": symbols,
                }
                handle.write(json.dumps(payload) + "\n")
                written += 1

        return written

    def _load_coin_rows(
        self,
        data_dir: Path,
        coin: str,
        date: str,
    ) -> dict[int, BucketRow]:
        path = data_dir / "l2" / coin / f"{date}.jsonl"
        buckets: dict[int, BucketRow] = {}
        if not path.exists():
            return buckets
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                bucket = int(payload["timestamp"]) // self.bucket_ms
                buckets[bucket] = BucketRow(
                    timestamp=int(payload["timestamp"]),
                    symbol=coin,
                    price=float(payload["mid"]),
                    spread_bps=float(payload["spread_bps"]),
                    bid_depth_10bps=float(payload.get("bid_depth_10bps", 0.0)),
                    ask_depth_10bps=float(payload.get("ask_depth_10bps", 0.0)),
                )
        return buckets

    def _load_trade_buckets(
        self,
        data_dir: Path,
        coin: str,
        date: str,
    ) -> dict[int, TradeBucket]:
        path = data_dir / "trades" / coin / f"{date}.jsonl"
        buckets: dict[int, TradeBucket] = {}
        if not path.exists():
            return buckets
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                bucket = int(payload["timestamp"]) // self.bucket_ms
                trade = buckets.setdefault(bucket, TradeBucket())
                size = float(payload.get("size", 0.0))
                price = float(payload.get("price", 0.0))
                trade.vwap_numerator += price * size
                trade.total_volume += size
                if bool(payload.get("is_buy", False)):
                    trade.buy_volume += size
                    trade.buy_count += 1
                else:
                    trade.sell_volume += size
                    trade.sell_count += 1
        return buckets

    def _ema(self, prev: float, value: float, alpha: float) -> float:
        return prev * (1 - alpha) + value * alpha

    def _book_imbalance(self, bid_depth: float, ask_depth: float) -> float:
        total = bid_depth + ask_depth
        if total <= 0:
            return 0.0
        return (bid_depth - ask_depth) / total

    def _range_width_bps(self, recent_low: float, recent_high: float, price: float) -> float:
        if price <= 0 or recent_low <= 0 or recent_high <= 0:
            return 0.0
        return (recent_high - recent_low) / price * 10_000

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))

    def _timestamp_to_iso(self, timestamp_ms: int) -> str:
        from datetime import datetime, timezone

        return (
            datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert gbot L2 JSONL into TRIDENT snapshots")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--coins", required=True, help="Comma-separated list, e.g. BTC,ETH,SOL")
    parser.add_argument("--output", required=True)
    parser.add_argument("--bucket-ms", type=int, default=60_000)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    converter = GbotL2ToTridentConverter(bucket_ms=args.bucket_ms)
    count = converter.convert(
        data_dir=args.data_dir,
        date=args.date,
        coins=[coin.strip().upper() for coin in args.coins.split(",") if coin.strip()],
        output_path=args.output,
    )
    print(f"records_written={count}")


if __name__ == "__main__":
    main()
