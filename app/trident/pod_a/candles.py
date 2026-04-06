from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.trident.types import SymbolMarketSnapshot


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(UTC)


def _bucket_start(value: datetime, timeframe: timedelta) -> datetime:
    seconds = int(timeframe.total_seconds())
    epoch = int(value.timestamp())
    return datetime.fromtimestamp((epoch // seconds) * seconds, tz=UTC)


@dataclass(slots=True)
class Candle:
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float
    sample_count: int = 1

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.sample_count += 1


class TimeframeBuffer:
    def __init__(self, *, timeframe: timedelta, max_candles: int = 64) -> None:
        self.timeframe = timeframe
        self.max_candles = max_candles
        self._history: deque[Candle] = deque(maxlen=max_candles)
        self._current: Candle | None = None
        self._current_bucket: datetime | None = None

    def update(self, timestamp: datetime, price: float) -> None:
        bucket = _bucket_start(timestamp, self.timeframe)
        if self._current_bucket != bucket:
            if self._current is not None:
                self._history.append(self._current)
            self._current_bucket = bucket
            self._current = Candle(
                opened_at=bucket,
                open=price,
                high=price,
                low=price,
                close=price,
            )
            return
        assert self._current is not None
        self._current.update(price)

    def candles(self) -> list[Candle]:
        candles = list(self._history)
        if self._current is not None:
            candles.append(self._current)
        return candles

    def completed_candles(self) -> list[Candle]:
        return list(self._history)

    def trend_bps(self, *, window: int) -> float:
        candles = self.candles()
        if len(candles) < 2:
            return 0.0
        sample = candles[-window:] if len(candles) >= window else candles
        first_open = sample[0].open
        last_close = sample[-1].close
        if first_open <= 0 or last_close <= 0:
            return 0.0
        return round(((last_close - first_open) / last_close) * 10_000.0, 4)

    def ready(self, *, min_candles: int) -> bool:
        return len(self.candles()) >= min_candles

    def recent_range_high(self, *, window: int) -> float | None:
        completed = self.completed_candles()
        if not completed:
            return None
        sample = completed[-window:] if len(completed) >= window else completed
        return round(max(candle.high for candle in sample), 8)

    def recent_range_low(self, *, window: int) -> float | None:
        completed = self.completed_candles()
        if not completed:
            return None
        sample = completed[-window:] if len(completed) >= window else completed
        return round(min(candle.low for candle in sample), 8)

    def last_swing_high(self) -> float | None:
        completed = self.completed_candles()
        if len(completed) < 3:
            return None
        for index in range(len(completed) - 2, 0, -1):
            previous = completed[index - 1]
            current = completed[index]
            following = completed[index + 1]
            if current.high > previous.high and current.high >= following.high:
                return round(current.high, 8)
        return None

    def last_swing_low(self) -> float | None:
        completed = self.completed_candles()
        if len(completed) < 3:
            return None
        for index in range(len(completed) - 2, 0, -1):
            previous = completed[index - 1]
            current = completed[index]
            following = completed[index + 1]
            if current.low < previous.low and current.low <= following.low:
                return round(current.low, 8)
        return None

    def current_close(self) -> float | None:
        if self._current is None:
            return None
        return self._current.close


class CandleService:
    """Maintains lightweight multi-timeframe state from timestamped snapshots."""

    def __init__(self) -> None:
        self._buffers_by_symbol: dict[str, dict[str, TimeframeBuffer]] = {}
        self._last_seen_timestamp_by_symbol: dict[str, str] = {}

    def observe(
        self,
        *,
        timestamp: str | None,
        snapshots: list[SymbolMarketSnapshot],
    ) -> None:
        parsed_timestamp = _parse_timestamp(timestamp)
        if parsed_timestamp is None:
            return

        for snapshot in snapshots:
            if self._last_seen_timestamp_by_symbol.get(snapshot.symbol) == timestamp:
                continue
            buffers = self._buffers_by_symbol.setdefault(
                snapshot.symbol,
                {
                    "15m": TimeframeBuffer(timeframe=timedelta(minutes=15)),
                    "1h": TimeframeBuffer(timeframe=timedelta(hours=1)),
                    "4h": TimeframeBuffer(timeframe=timedelta(hours=4)),
                },
            )
            for buffer in buffers.values():
                buffer.update(parsed_timestamp, snapshot.price)
            self._last_seen_timestamp_by_symbol[snapshot.symbol] = timestamp

    def features_for(self, symbol: str) -> dict[str, float | bool]:
        buffers = self._buffers_by_symbol.get(symbol)
        if buffers is None:
            return {
                "trend_15m_bps": 0.0,
                "trend_1h_bps": 0.0,
                "trend_4h_bps": 0.0,
                "mtf_bias_score": 0.0,
                "candles_ready": False,
                "structure_ready": False,
                "range_high_1h": 0.0,
                "range_low_1h": 0.0,
                "swing_high_1h": 0.0,
                "swing_low_1h": 0.0,
                "bos_long_confirmed": False,
                "bos_short_confirmed": False,
            }

        trend_15m_bps = buffers["15m"].trend_bps(window=4)
        trend_1h_bps = buffers["1h"].trend_bps(window=4)
        trend_4h_bps = buffers["4h"].trend_bps(window=3)
        range_high_1h = buffers["1h"].recent_range_high(window=4)
        range_low_1h = buffers["1h"].recent_range_low(window=4)
        swing_high_1h = buffers["1h"].last_swing_high()
        swing_low_1h = buffers["1h"].last_swing_low()
        current_close_1h = buffers["1h"].current_close()
        structure_ready = (
            range_high_1h is not None
            and range_low_1h is not None
            and swing_high_1h is not None
            and swing_low_1h is not None
            and current_close_1h is not None
        )
        bos_long_confirmed = bool(
            structure_ready
            and current_close_1h is not None
            and swing_high_1h is not None
            and current_close_1h > swing_high_1h
        )
        bos_short_confirmed = bool(
            structure_ready
            and current_close_1h is not None
            and swing_low_1h is not None
            and current_close_1h < swing_low_1h
        )
        candles_ready = (
            buffers["15m"].ready(min_candles=4)
            and buffers["1h"].ready(min_candles=3)
            and buffers["4h"].ready(min_candles=2)
        )
        mtf_bias_score = round(
            trend_15m_bps * 0.20 + trend_1h_bps * 0.35 + trend_4h_bps * 0.45,
            4,
        )
        return {
            "trend_15m_bps": trend_15m_bps,
            "trend_1h_bps": trend_1h_bps,
            "trend_4h_bps": trend_4h_bps,
            "mtf_bias_score": mtf_bias_score,
            "candles_ready": candles_ready,
            "structure_ready": structure_ready,
            "range_high_1h": float(range_high_1h or 0.0),
            "range_low_1h": float(range_low_1h or 0.0),
            "swing_high_1h": float(swing_high_1h or 0.0),
            "swing_low_1h": float(swing_low_1h or 0.0),
            "bos_long_confirmed": bos_long_confirmed,
            "bos_short_confirmed": bos_short_confirmed,
        }
