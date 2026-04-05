from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


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


@dataclass(slots=True)
class BookState:
    time_ms: int
    price: float
    spread_bps: float
    bid_depth_10bps: float
    ask_depth_10bps: float


class LiveSnapshotBuilder:
    """Builds TRIDENT snapshots from live Hyperliquid l2Book and trades streams."""

    def __init__(self, coins: list[str], bucket_ms: int = 60_000) -> None:
        self.coins = [coin.upper() for coin in coins]
        self.bucket_ms = bucket_ms
        self.current_bucket: int | None = None
        self.latest_book_by_symbol: dict[str, BookState] = {}
        self.trade_buckets: dict[int, dict[str, TradeBucket]] = {}
        self.state_by_symbol: dict[str, dict[str, float]] = {}

    def ingest_ws_message(self, message: dict[str, object]) -> list[dict[str, object]]:
        channel = str(message.get("channel", ""))
        if channel == "l2Book":
            data = message.get("data")
            if isinstance(data, dict):
                return self.ingest_book(data)
            return []
        if channel == "trades":
            data = message.get("data")
            if isinstance(data, list):
                records: list[dict[str, object]] = []
                for trade in data:
                    if isinstance(trade, dict):
                        records.extend(self.ingest_trade(trade))
                return records
        return []

    def ingest_book(self, book: dict[str, object]) -> list[dict[str, object]]:
        coin = str(book.get("coin", "")).upper()
        if not coin or coin not in self.coins:
            return []
        event_time = int(book.get("time", 0))
        bucket = event_time // self.bucket_ms
        records = self._flush_before(bucket)
        levels = book.get("levels", [[], []])
        bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else []
        asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
        best_bid = float(bids[0]["px"]) if bids else 0.0
        best_ask = float(asks[0]["px"]) if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else max(best_bid, best_ask)
        spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid > 0 and best_ask >= best_bid else 0.0
        self.latest_book_by_symbol[coin] = BookState(
            time_ms=event_time,
            price=mid,
            spread_bps=round(spread_bps, 4),
            bid_depth_10bps=round(self._depth_within_bps(bids, mid, "bid", 10.0), 6),
            ask_depth_10bps=round(self._depth_within_bps(asks, mid, "ask", 10.0), 6),
        )
        return records

    def ingest_trade(self, trade: dict[str, object]) -> list[dict[str, object]]:
        coin = str(trade.get("coin", "")).upper()
        if not coin or coin not in self.coins:
            return []
        event_time = int(trade.get("time", 0))
        bucket = event_time // self.bucket_ms
        records = self._flush_before(bucket)
        trade_bucket = self.trade_buckets.setdefault(bucket, {}).setdefault(coin, TradeBucket())
        size = float(trade.get("sz", 0.0))
        price = float(trade.get("px", 0.0))
        side = str(trade.get("side", "")).lower()
        is_buy = side in {"b", "buy", "bid", "long"}
        trade_bucket.vwap_numerator += price * size
        trade_bucket.total_volume += size
        if is_buy:
            trade_bucket.buy_volume += size
            trade_bucket.buy_count += 1
        else:
            trade_bucket.sell_volume += size
            trade_bucket.sell_count += 1
        return records

    def finalize(self) -> list[dict[str, object]]:
        if self.current_bucket is None:
            return []
        records = self._build_bucket_snapshot(self.current_bucket)
        self.trade_buckets.pop(self.current_bucket, None)
        self.current_bucket = None
        return records

    def _flush_before(self, new_bucket: int) -> list[dict[str, object]]:
        if self.current_bucket is None:
            self.current_bucket = new_bucket
            return []
        records: list[dict[str, object]] = []
        while self.current_bucket is not None and new_bucket > self.current_bucket:
            records.extend(self._build_bucket_snapshot(self.current_bucket))
            self.trade_buckets.pop(self.current_bucket, None)
            self.current_bucket += 1
        return records

    def _build_bucket_snapshot(self, bucket: int) -> list[dict[str, object]]:
        symbols: list[dict[str, object]] = []
        momentum_by_symbol: dict[str, float] = {}
        trades_for_bucket = self.trade_buckets.get(bucket, {})

        for coin in self.coins:
            row = self.latest_book_by_symbol.get(coin)
            if row is None or row.price <= 0:
                continue
            trades = trades_for_bucket.get(coin, TradeBucket())
            state = self.state_by_symbol.setdefault(
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
            momentum_bps = ((row.price - last_price) / last_price * 10_000) if last_price else 0.0
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
                    "btc_aligned": True,
                    "book_imbalance": round(book_imbalance, 4),
                    "trade_flow_bias": round(trades.flow_bias, 4),
                    "bucket_volume": round(trades.total_volume, 6),
                    "bucket_trade_count": trades.trade_count,
                    "bucket_range_bps": round(range_width_bps, 4),
                    "source": "hyperliquid_live_collector",
                }
            )

        if not symbols:
            return []
        btc_mom = momentum_by_symbol.get("BTC")
        if btc_mom is not None:
            for symbol in symbols:
                sym_mom = momentum_by_symbol.get(symbol["symbol"], 0.0)
                symbol["btc_aligned"] = sym_mom == 0 or btc_mom == 0 or (sym_mom > 0) == (btc_mom > 0)

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
        return [
            {
                "timestamp": self._timestamp_to_iso(bucket * self.bucket_ms),
                "regime_snapshot": regime_snapshot,
                "symbols": symbols,
            }
        ]

    def _depth_within_bps(
        self,
        levels: object,
        mid: float,
        side: str,
        limit_bps: float,
    ) -> float:
        if not isinstance(levels, list) or mid <= 0:
            return 0.0
        total = 0.0
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = float(level.get("px", 0.0))
            size = float(level.get("sz", 0.0))
            if price <= 0 or size <= 0:
                continue
            bps = abs(price - mid) / mid * 10_000
            if bps > limit_bps:
                continue
            if side == "bid" and price <= mid:
                total += size
            elif side == "ask" and price >= mid:
                total += size
        return total

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
        return (
            datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
