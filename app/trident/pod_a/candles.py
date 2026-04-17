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


def _clamp(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _highest_high(candles: list[Candle], *, window: int) -> float | None:
    if len(candles) < window:
        return None
    sample = candles[-window:]
    return max(candle.high for candle in sample)


def _lowest_low(candles: list[Candle], *, window: int) -> float | None:
    if len(candles) < window:
        return None
    sample = candles[-window:]
    return min(candle.low for candle in sample)


def _rsi_series(closes: list[float], *, period: int = 14) -> list[float]:
    if len(closes) <= period:
        return []
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(closes, closes[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _to_rsi(gain: float, loss: float) -> float:
        if loss <= 1e-9:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    values = [_to_rsi(avg_gain, avg_loss)]
    for index in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
        values.append(_to_rsi(avg_gain, avg_loss))
    return values


def _stoch_rsi(candles: list[Candle], *, rsi_period: int = 14, stoch_period: int = 14) -> float | None:
    closes = [candle.close for candle in candles if candle.close > 0]
    rsi_values = _rsi_series(closes, period=rsi_period)
    if len(rsi_values) < stoch_period:
        return None
    sample = rsi_values[-stoch_period:]
    highest = max(sample)
    lowest = min(sample)
    if highest - lowest <= 1e-9:
        return 0.5
    return (rsi_values[-1] - lowest) / (highest - lowest)


def _cci(candles: list[Candle], *, period: int = 20) -> float | None:
    if len(candles) < period:
        return None
    sample = candles[-period:]
    typical_prices = [
        (candle.high + candle.low + candle.close) / 3.0
        for candle in sample
    ]
    sma = sum(typical_prices) / len(typical_prices)
    mean_deviation = sum(abs(value - sma) for value in typical_prices) / len(typical_prices)
    if mean_deviation <= 1e-9:
        return 0.0
    return (typical_prices[-1] - sma) / (0.015 * mean_deviation)


def _ichimoku_bias(candles: list[Candle]) -> float | None:
    if len(candles) < 26:
        return None
    high_9 = _highest_high(candles, window=9)
    low_9 = _lowest_low(candles, window=9)
    high_26 = _highest_high(candles, window=26)
    low_26 = _lowest_low(candles, window=26)
    if None in {high_9, low_9, high_26, low_26}:
        return None
    tenkan = (float(high_9) + float(low_9)) / 2.0
    kijun = (float(high_26) + float(low_26)) / 2.0
    span_a = (tenkan + kijun) / 2.0
    if len(candles) >= 52:
        high_52 = _highest_high(candles, window=52)
        low_52 = _lowest_low(candles, window=52)
        span_b = (
            (float(high_52) + float(low_52)) / 2.0
            if high_52 is not None and low_52 is not None
            else kijun
        )
    else:
        span_b = kijun
    price = candles[-1].close
    cloud_top = max(span_a, span_b)
    cloud_bottom = min(span_a, span_b)
    score = 0.0
    if price > cloud_top:
        score += 0.45
    elif price < cloud_bottom:
        score -= 0.45
    if tenkan > kijun:
        score += 0.25
    elif tenkan < kijun:
        score -= 0.25
    if price > kijun:
        score += 0.20
    elif price < kijun:
        score -= 0.20
    if price > (span_a + span_b) / 2.0 and span_a >= span_b:
        score += 0.10
    elif price < (span_a + span_b) / 2.0 and span_a <= span_b:
        score -= 0.10
    return _clamp(score)


def _supertrend_direction(
    candles: list[Candle],
    *,
    period: int = 10,
    multiplier: float = 2.0,
) -> int:
    if len(candles) < period + 1:
        return 0
    true_ranges: list[float] = []
    for index, candle in enumerate(candles):
        prev_close = candles[index - 1].close if index > 0 else candle.close
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        )

    final_upper = 0.0
    final_lower = 0.0
    direction = 0
    for index, candle in enumerate(candles):
        if index < period - 1:
            continue
        atr_sample = true_ranges[index - period + 1 : index + 1]
        atr = sum(atr_sample) / len(atr_sample)
        hl2 = (candle.high + candle.low) / 2.0
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr
        if direction == 0:
            final_upper = basic_upper
            final_lower = basic_lower
            direction = 1 if candle.close >= hl2 else -1
            continue
        prev_close = candles[index - 1].close
        prev_final_upper = final_upper
        prev_final_lower = final_lower
        final_upper = (
            basic_upper
            if basic_upper < prev_final_upper or prev_close > prev_final_upper
            else prev_final_upper
        )
        final_lower = (
            basic_lower
            if basic_lower > prev_final_lower or prev_close < prev_final_lower
            else prev_final_lower
        )
        if candle.close > prev_final_upper:
            direction = 1
        elif candle.close < prev_final_lower:
            direction = -1
    return direction


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
                "ichimoku_bias_score": 0.0,
                "supertrend_direction": 0,
                "stoch_rsi_k": 0.5,
                "cci20": 0.0,
            }

        candles_15m = buffers["15m"].candles()
        candles_1h = buffers["1h"].candles()
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
        ichimoku_bias_score = _ichimoku_bias(candles_1h) or 0.0
        supertrend_direction = _supertrend_direction(candles_15m)
        stoch_rsi_k = _stoch_rsi(candles_15m) or 0.5
        cci20 = _cci(candles_15m) or 0.0
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
            "ichimoku_bias_score": round(float(ichimoku_bias_score), 4),
            "supertrend_direction": int(supertrend_direction),
            "stoch_rsi_k": round(float(stoch_rsi_k), 4),
            "cci20": round(float(cci20), 4),
        }
