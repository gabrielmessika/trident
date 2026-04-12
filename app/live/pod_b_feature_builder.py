from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class _FeatureBookState:
    time_ms: int
    mid: float
    spread_bps: float
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float
    bid_depth_10bps: float
    ask_depth_10bps: float


@dataclass(slots=True)
class _WindowMetrics:
    close_mid: float
    trade_count: int
    notional_volume_usd: float
    flow_bias: float
    book_imbalance: float
    microprice_dislocation_bps: float
    range_bps: float
    spread_bps: float
    realized_move_bps: float


@dataclass(slots=True)
class _FeatureBucket:
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    first_mid: float = 0.0
    last_mid: float = 0.0
    high_mid: float = 0.0
    low_mid: float = 0.0
    last_spread_bps: float = 0.0
    last_book_imbalance: float = 0.0
    last_microprice_dislocation_bps: float = 0.0
    max_abs_microprice_dislocation_bps: float = 0.0
    max_abs_book_imbalance: float = 0.0

    @property
    def trade_count(self) -> int:
        return self.buy_count + self.sell_count

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def flow_bias(self) -> float:
        total_volume = self.total_volume
        if total_volume <= 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / total_volume

    def update_book(
        self,
        *,
        mid: float,
        spread_bps: float,
        book_imbalance: float,
        microprice_dislocation_bps: float,
    ) -> None:
        if mid <= 0:
            return
        if self.first_mid <= 0:
            self.first_mid = mid
            self.high_mid = mid
            self.low_mid = mid
        self.last_mid = mid
        self.high_mid = max(self.high_mid, mid)
        self.low_mid = min(self.low_mid, mid) if self.low_mid > 0 else mid
        self.last_spread_bps = spread_bps
        self.last_book_imbalance = book_imbalance
        self.last_microprice_dislocation_bps = microprice_dislocation_bps
        self.max_abs_microprice_dislocation_bps = max(
            self.max_abs_microprice_dislocation_bps,
            abs(microprice_dislocation_bps),
        )
        self.max_abs_book_imbalance = max(
            self.max_abs_book_imbalance,
            abs(book_imbalance),
        )


class PodBFeatureBuilder:
    """Builds intraminute sidecar features for Pod B breakout research."""

    def __init__(
        self,
        coins: list[str],
        bucket_ms: int = 10_000,
        ws_to_name: dict[str, str] | None = None,
    ) -> None:
        self.coins = [coin.upper() for coin in coins]
        self.bucket_ms = bucket_ms
        self._ws_to_name = ws_to_name or {}
        self.current_bucket: int | None = None
        self.latest_book_by_symbol: dict[str, _FeatureBookState] = {}
        self.feature_buckets: dict[int, dict[str, _FeatureBucket]] = {}
        self.history_by_symbol: dict[str, deque[_WindowMetrics]] = {}
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
        coin = self._resolve_coin(str(book.get("coin", "")))
        if coin is None:
            return []
        event_time = int(book.get("time", 0))
        bucket = event_time // self.bucket_ms
        records = self._flush_before(bucket)
        levels = book.get("levels", [[], []])
        bids = levels[0] if isinstance(levels, list) and len(levels) > 0 else []
        asks = levels[1] if isinstance(levels, list) and len(levels) > 1 else []
        best_bid = float(bids[0]["px"]) if bids else 0.0
        best_ask = float(asks[0]["px"]) if asks else 0.0
        best_bid_size = float(bids[0]["sz"]) if bids else 0.0
        best_ask_size = float(asks[0]["sz"]) if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else max(best_bid, best_ask)
        spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid > 0 and best_ask >= best_bid else 0.0
        bid_depth_10bps = round(self._depth_within_bps(bids, mid, "bid", 10.0), 6)
        ask_depth_10bps = round(self._depth_within_bps(asks, mid, "ask", 10.0), 6)
        self.latest_book_by_symbol[coin] = _FeatureBookState(
            time_ms=event_time,
            mid=mid,
            spread_bps=round(spread_bps, 4),
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            bid_depth_10bps=bid_depth_10bps,
            ask_depth_10bps=ask_depth_10bps,
        )
        feature_bucket = self.feature_buckets.setdefault(bucket, {}).setdefault(coin, _FeatureBucket())
        feature_bucket.update_book(
            mid=mid,
            spread_bps=round(spread_bps, 4),
            book_imbalance=self._book_imbalance(bid_depth_10bps, ask_depth_10bps),
            microprice_dislocation_bps=self._microprice_dislocation_bps(
                best_bid=best_bid,
                best_ask=best_ask,
                best_bid_size=best_bid_size,
                best_ask_size=best_ask_size,
                mid=mid,
            ),
        )
        return records

    def ingest_trade(self, trade: dict[str, object]) -> list[dict[str, object]]:
        coin = self._resolve_coin(str(trade.get("coin", "")))
        if coin is None:
            return []
        event_time = int(trade.get("time", 0))
        bucket = event_time // self.bucket_ms
        records = self._flush_before(bucket)
        feature_bucket = self.feature_buckets.setdefault(bucket, {}).setdefault(coin, _FeatureBucket())
        size = float(trade.get("sz", 0.0))
        side = str(trade.get("side", "")).lower()
        if side in {"b", "buy", "bid", "long"}:
            feature_bucket.buy_volume += size
            feature_bucket.buy_count += 1
        else:
            feature_bucket.sell_volume += size
            feature_bucket.sell_count += 1
        row = self.latest_book_by_symbol.get(coin)
        if row is not None:
            feature_bucket.update_book(
                mid=row.mid,
                spread_bps=row.spread_bps,
                book_imbalance=self._book_imbalance(row.bid_depth_10bps, row.ask_depth_10bps),
                microprice_dislocation_bps=self._microprice_dislocation_bps(
                    best_bid=row.best_bid,
                    best_ask=row.best_ask,
                    best_bid_size=row.best_bid_size,
                    best_ask_size=row.best_ask_size,
                    mid=row.mid,
                ),
            )
        return records

    def finalize(self) -> list[dict[str, object]]:
        if self.current_bucket is None:
            return []
        records = self._build_bucket_features(self.current_bucket)
        self.feature_buckets.pop(self.current_bucket, None)
        self.current_bucket = None
        return records

    def _flush_before(self, new_bucket: int) -> list[dict[str, object]]:
        if self.current_bucket is None:
            self.current_bucket = new_bucket
            return []
        records: list[dict[str, object]] = []
        while self.current_bucket is not None and new_bucket > self.current_bucket:
            records.extend(self._build_bucket_features(self.current_bucket))
            self.feature_buckets.pop(self.current_bucket, None)
            self.current_bucket += 1
        return records

    def _build_bucket_features(self, bucket: int) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        bucket_rows = self.feature_buckets.get(bucket, {})
        for coin in self.coins:
            book = self.latest_book_by_symbol.get(coin)
            if book is None or book.mid <= 0:
                continue
            current = bucket_rows.get(coin, _FeatureBucket())
            history = self.history_by_symbol.setdefault(coin, deque(maxlen=12))
            prior_windows = list(history)[-2:]
            window_30 = prior_windows + [self._window_metrics_for_bucket(current, book, history[-1] if history else None)]
            trade_count_30s = sum(item.trade_count for item in window_30)
            notional_volume_30s_usd = sum(item.notional_volume_usd for item in window_30)
            range_width_30s_bps = self._range_width_from_windows(window_30)
            realized_vol_short_bps = self._average(
                [item.realized_move_bps for item in window_30 if item.realized_move_bps > 0]
            )
            state = self.state_by_symbol.setdefault(
                coin,
                {
                    "avg_trade_count_30s": 0.0,
                    "avg_notional_volume_30s_usd": 0.0,
                    "avg_realized_vol_short_bps": 0.0,
                },
            )
            trade_count_ratio = self._ratio_against_baseline(
                float(trade_count_30s),
                state["avg_trade_count_30s"],
            )
            volume_ratio = self._ratio_against_baseline(
                notional_volume_30s_usd,
                state["avg_notional_volume_30s_usd"],
            )
            realized_vol_long_bps = self._update_baseline(
                state["avg_realized_vol_short_bps"],
                realized_vol_short_bps,
                alpha=0.12,
            )
            prev_close_mid = history[-1].close_mid if history else book.mid
            delta_mid_10s_bps = self._delta_bps(prev_close_mid, book.mid)
            anchor_30 = window_30[0].close_mid if window_30 else book.mid
            delta_mid_30s_bps = self._delta_bps(anchor_30, book.mid)
            microprice_dislocation_bps = self._microprice_dislocation_bps(
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                best_bid_size=book.best_bid_size,
                best_ask_size=book.best_ask_size,
                mid=book.mid,
            )
            book_imbalance = self._book_imbalance(book.bid_depth_10bps, book.ask_depth_10bps)
            activity_score = self._activity_score(
                trade_count_ratio=trade_count_ratio,
                volume_ratio=volume_ratio,
                delta_mid_10s_bps=delta_mid_10s_bps,
                delta_mid_30s_bps=delta_mid_30s_bps,
                microprice_dislocation_bps=microprice_dislocation_bps,
                flow_bias=current.flow_bias,
                book_imbalance=book_imbalance,
            )
            sweep_signature_score = self._sweep_signature_score(
                delta_mid_10s_bps=delta_mid_10s_bps,
                flow_bias=current.flow_bias,
                book_imbalance=book_imbalance,
                microprice_dislocation_bps=microprice_dislocation_bps,
                trade_count_ratio=trade_count_ratio,
            )
            compression_score = self._compression_score(
                range_width_bps=range_width_30s_bps,
                spread_bps=book.spread_bps,
                realized_vol_short_bps=realized_vol_short_bps,
                realized_vol_long_bps=realized_vol_long_bps,
                flow_bias=current.flow_bias,
            )
            records.append(
                {
                    "timestamp": self._timestamp_to_iso(bucket * self.bucket_ms),
                    "symbol": coin,
                    "midprice": round(book.mid, 8),
                    "spread_bps": round(book.spread_bps, 4),
                    "best_bid": round(book.best_bid, 8),
                    "best_ask": round(book.best_ask, 8),
                    "best_bid_size": round(book.best_bid_size, 6),
                    "best_ask_size": round(book.best_ask_size, 6),
                    "bid_depth_10bps": round(book.bid_depth_10bps, 6),
                    "ask_depth_10bps": round(book.ask_depth_10bps, 6),
                    "book_imbalance": round(book_imbalance, 4),
                    "microprice_dislocation_bps": round(microprice_dislocation_bps, 4),
                    "trade_count_10s": current.trade_count,
                    "trade_count_30s": trade_count_30s,
                    "trade_count_ratio": round(trade_count_ratio, 4),
                    "notional_volume_10s_usd": round(current.total_volume * book.mid, 4),
                    "notional_volume_30s_usd": round(notional_volume_30s_usd, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "delta_mid_10s_bps": round(delta_mid_10s_bps, 4),
                    "delta_mid_30s_bps": round(delta_mid_30s_bps, 4),
                    "range_width_10s_bps": round(self._range_width(current, book.mid), 4),
                    "range_width_30s_bps": round(range_width_30s_bps, 4),
                    "realized_vol_short_bps": round(realized_vol_short_bps, 4),
                    "realized_vol_long_bps": round(realized_vol_long_bps, 4),
                    "flow_bias_10s": round(current.flow_bias, 4),
                    "signed_trade_delta_10s": round(current.buy_volume - current.sell_volume, 6),
                    "sweep_signature_score": round(sweep_signature_score, 4),
                    "activity_score": round(activity_score, 4),
                    "compression_score": round(compression_score, 4),
                    "source": "hyperliquid_live_collector_pod_b_sidecar",
                }
            )
            state["avg_trade_count_30s"] = self._update_baseline(
                state["avg_trade_count_30s"],
                float(trade_count_30s),
                alpha=0.15,
            )
            state["avg_notional_volume_30s_usd"] = self._update_baseline(
                state["avg_notional_volume_30s_usd"],
                notional_volume_30s_usd,
                alpha=0.15,
            )
            state["avg_realized_vol_short_bps"] = realized_vol_long_bps
            history.append(
                _WindowMetrics(
                    close_mid=book.mid,
                    trade_count=current.trade_count,
                    notional_volume_usd=current.total_volume * book.mid,
                    flow_bias=current.flow_bias,
                    book_imbalance=book_imbalance,
                    microprice_dislocation_bps=microprice_dislocation_bps,
                    range_bps=self._range_width(current, book.mid),
                    spread_bps=book.spread_bps,
                    realized_move_bps=abs(delta_mid_10s_bps),
                )
            )
        return records


    def _resolve_coin(self, raw_coin: str) -> str | None:
        coin = raw_coin.upper()
        if coin in self.coins:
            return coin
        resolved = self._ws_to_name.get(raw_coin) or self._ws_to_name.get(coin)
        if resolved and resolved in self.coins:
            return resolved
        return None

    def _window_metrics_for_bucket(
        self,
        bucket: _FeatureBucket,
        book: _FeatureBookState,
        previous: _WindowMetrics | None,
    ) -> _WindowMetrics:
        prev_close = previous.close_mid if previous is not None else book.mid
        mid = bucket.last_mid if bucket.last_mid > 0 else book.mid
        return _WindowMetrics(
            close_mid=mid,
            trade_count=bucket.trade_count,
            notional_volume_usd=bucket.total_volume * book.mid,
            flow_bias=bucket.flow_bias,
            book_imbalance=bucket.last_book_imbalance,
            microprice_dislocation_bps=bucket.last_microprice_dislocation_bps,
            range_bps=self._range_width(bucket, book.mid),
            spread_bps=bucket.last_spread_bps or book.spread_bps,
            realized_move_bps=abs(self._delta_bps(prev_close, mid)),
        )

    def _range_width(self, bucket: _FeatureBucket, fallback_mid: float) -> float:
        mid = bucket.last_mid if bucket.last_mid > 0 else fallback_mid
        low = bucket.low_mid if bucket.low_mid > 0 else mid
        high = bucket.high_mid if bucket.high_mid > 0 else mid
        if mid <= 0 or low <= 0 or high <= 0:
            return 0.0
        return (high - low) / mid * 10_000

    def _range_width_from_windows(self, windows: list[_WindowMetrics]) -> float:
        if not windows:
            return 0.0
        mids = [window.close_mid for window in windows if window.close_mid > 0]
        if not mids:
            return 0.0
        anchor = mids[-1]
        if anchor <= 0:
            return 0.0
        return (max(mids) - min(mids)) / anchor * 10_000

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

    def _book_imbalance(self, bid_depth: float, ask_depth: float) -> float:
        total = bid_depth + ask_depth
        if total <= 0:
            return 0.0
        return (bid_depth - ask_depth) / total

    def _microprice_dislocation_bps(
        self,
        *,
        best_bid: float,
        best_ask: float,
        best_bid_size: float,
        best_ask_size: float,
        mid: float,
    ) -> float:
        if mid <= 0:
            return 0.0
        total_size = best_bid_size + best_ask_size
        if best_bid <= 0 or best_ask <= 0 or total_size <= 0:
            return 0.0
        microprice = (best_ask * best_bid_size + best_bid * best_ask_size) / total_size
        return (microprice - mid) / mid * 10_000

    def _ratio_against_baseline(self, current: float, baseline: float) -> float:
        if baseline > 0:
            return current / baseline
        return 2.0 if current > 0 else 1.0

    def _update_baseline(self, baseline: float, current: float, alpha: float) -> float:
        if baseline <= 0:
            return current
        return baseline * (1 - alpha) + current * alpha

    def _delta_bps(self, anchor: float, current: float) -> float:
        if anchor <= 0 or current <= 0:
            return 0.0
        return (current - anchor) / anchor * 10_000

    def _average(self, values: list[float]) -> float:
        if not values:
            return 0.0
        return sum(values) / len(values)

    def _activity_score(
        self,
        *,
        trade_count_ratio: float,
        volume_ratio: float,
        delta_mid_10s_bps: float,
        delta_mid_30s_bps: float,
        microprice_dislocation_bps: float,
        flow_bias: float,
        book_imbalance: float,
    ) -> float:
        return min(
            1.0,
            min(trade_count_ratio / 3.0, 1.0) * 0.25
            + min(volume_ratio / 3.0, 1.0) * 0.25
            + min(max(abs(delta_mid_10s_bps), abs(delta_mid_30s_bps)) / 12.0, 1.0) * 0.20
            + min(abs(microprice_dislocation_bps) / 2.0, 1.0) * 0.15
            + min(abs(flow_bias) * 0.6 + abs(book_imbalance) * 0.4, 1.0) * 0.15,
        )

    def _sweep_signature_score(
        self,
        *,
        delta_mid_10s_bps: float,
        flow_bias: float,
        book_imbalance: float,
        microprice_dislocation_bps: float,
        trade_count_ratio: float,
    ) -> float:
        if abs(delta_mid_10s_bps) <= 0.01:
            return 0.0
        reference_sign = 1 if delta_mid_10s_bps > 0 else -1
        aligned = 0
        comparisons = 0
        for value in (flow_bias, book_imbalance, microprice_dislocation_bps):
            if abs(value) <= 1e-9:
                continue
            comparisons += 1
            if (value > 0) == (reference_sign > 0):
                aligned += 1
        alignment_factor = 0.5 if comparisons == 0 else 0.5 + 0.5 * (aligned / comparisons)
        magnitude = (
            min(abs(delta_mid_10s_bps) / 8.0, 1.0) * 0.25
            + min(abs(flow_bias), 1.0) * 0.25
            + min(abs(book_imbalance), 1.0) * 0.20
            + min(abs(microprice_dislocation_bps) / 2.0, 1.0) * 0.15
            + min(trade_count_ratio / 3.0, 1.0) * 0.15
        )
        return min(1.0, magnitude * alignment_factor)

    def _compression_score(
        self,
        *,
        range_width_bps: float,
        spread_bps: float,
        realized_vol_short_bps: float,
        realized_vol_long_bps: float,
        flow_bias: float,
    ) -> float:
        range_component = 1.0 - self._clamp(range_width_bps / 20.0, 0.0, 1.0)
        spread_component = 1.0 - self._clamp(spread_bps / 8.0, 0.0, 1.0)
        vol_component = 1.0 - self._clamp(realized_vol_short_bps / 10.0, 0.0, 1.0)
        relative_vol_component = 1.0
        if realized_vol_long_bps > 0:
            relative_vol_component = 1.0 - self._clamp(
                realized_vol_short_bps / max(realized_vol_long_bps * 1.5, 1e-9),
                0.0,
                1.0,
            )
        flow_component = 1.0 - self._clamp(abs(flow_bias), 0.0, 1.0)
        return (
            range_component * 0.30
            + spread_component * 0.20
            + vol_component * 0.20
            + relative_vol_component * 0.15
            + flow_component * 0.15
        )

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))

    def _timestamp_to_iso(self, timestamp_ms: int) -> str:
        return (
            datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
