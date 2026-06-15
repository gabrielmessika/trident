from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping

from app.trident.pod_a.candles import Candle
from app.trident.pod_a.regime_shadow import RegimeShadowFeatures
from app.trident.types import SymbolMarketSnapshot


@dataclass(slots=True)
class OrderBlockShadowEvent:
    pattern: str
    side: str
    timeframe: str
    opened_at: str
    triggered_at: str


@dataclass(slots=True)
class OrderBlockShadowFeatures:
    timestamp: str
    symbol: str
    bullish_order_blocks_1h4h: list[str]
    bearish_order_blocks_1h4h: list[str]


@dataclass(slots=True)
class ActiveOrderBlock:
    event: OrderBlockShadowEvent
    expires_at: datetime


@dataclass(slots=True)
class OrderBlockTimeframeState:
    timeframe: str
    duration: timedelta
    current_bucket: datetime | None = None
    current: Candle | None = None
    completed: list[Candle] = field(default_factory=list)
    active: list[ActiveOrderBlock] = field(default_factory=list)
    emitted_keys: set[str] = field(default_factory=set)
    bullish_order_blocks: list[tuple[float, float, datetime]] = field(default_factory=list)
    bearish_order_blocks: list[tuple[float, float, datetime]] = field(default_factory=list)

    def observe(self, *, timestamp: datetime, symbol: str, price: float) -> None:
        bucket = bucket_start(timestamp, self.duration)
        if self.current_bucket is None:
            self.current_bucket = bucket
            self.current = Candle(opened_at=bucket, open=price, high=price, low=price, close=price)
            return
        if bucket != self.current_bucket:
            assert self.current is not None
            self.completed.append(self.current)
            self.completed = self.completed[-96:]
            self._detect_order_blocks(symbol=symbol, timestamp=bucket)
            self.current_bucket = bucket
            self.current = Candle(opened_at=bucket, open=price, high=price, low=price, close=price)
            self._expire(timestamp)
            return
        assert self.current is not None
        self.current.update(price)
        self._expire(timestamp)

    def active_events(self, *, side: str | None = None) -> list[OrderBlockShadowEvent]:
        if side is None:
            return [item.event for item in self.active]
        return [item.event for item in self.active if item.event.side == side]

    def _expire(self, timestamp: datetime) -> None:
        self.active = [item for item in self.active if item.expires_at >= timestamp]

    def _detect_order_blocks(self, *, symbol: str, timestamp: datetime) -> None:
        if len(self.completed) < 2:
            return
        latest = self.completed[-1]
        previous = self.completed[-2]
        body_bps = candle_body_bps(latest)
        range_bps = candle_range_bps(latest)
        if latest.close > latest.open and body_bps >= 40.0 and range_bps >= 55.0 and previous.close < previous.open:
            low, high = sorted((previous.open, previous.close))
            self.bullish_order_blocks.append((low, high, previous.opened_at))
            self.bullish_order_blocks = self.bullish_order_blocks[-8:]
        if latest.close < latest.open and body_bps >= 40.0 and range_bps >= 55.0 and previous.close > previous.open:
            low, high = sorted((previous.open, previous.close))
            self.bearish_order_blocks.append((low, high, previous.opened_at))
            self.bearish_order_blocks = self.bearish_order_blocks[-8:]
        for _low, high, opened_at in self.bullish_order_blocks:
            if latest.low <= high and latest.close >= high and latest.close > latest.open:
                self._add_event(
                    symbol=symbol,
                    timestamp=timestamp,
                    opened_at=opened_at,
                    pattern="order_block_bull_retest",
                    side="long",
                    key_suffix=f"{isoformat(opened_at)}:{isoformat(latest.opened_at)}",
                )
        for low, _high, opened_at in self.bearish_order_blocks:
            if latest.high >= low and latest.close <= low and latest.close < latest.open:
                self._add_event(
                    symbol=symbol,
                    timestamp=timestamp,
                    opened_at=opened_at,
                    pattern="order_block_bear_retest",
                    side="short",
                    key_suffix=f"{isoformat(opened_at)}:{isoformat(latest.opened_at)}",
                )

    def _add_event(
        self,
        *,
        symbol: str,
        timestamp: datetime,
        opened_at: datetime,
        pattern: str,
        side: str,
        key_suffix: str,
    ) -> None:
        key = f"{symbol}:{pattern}:{side}:{self.timeframe}:{key_suffix}"
        if key in self.emitted_keys:
            return
        self.emitted_keys.add(key)
        self.active.append(
            ActiveOrderBlock(
                event=OrderBlockShadowEvent(
                    pattern=pattern,
                    side=side,
                    timeframe=self.timeframe,
                    opened_at=isoformat(opened_at),
                    triggered_at=isoformat(timestamp),
                ),
                expires_at=timestamp + self.duration * 2,
            )
        )


class PodAOrderBlockShadowTracker:
    """Observation-only order-block tracker for P1-07b."""

    def __init__(self) -> None:
        self._timeframes = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}
        self._states: dict[tuple[str, str], OrderBlockTimeframeState] = {}

    def observe(
        self,
        *,
        timestamp: str | datetime,
        snapshots: list[SymbolMarketSnapshot],
    ) -> dict[str, OrderBlockShadowFeatures]:
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            return {}
        for snapshot in snapshots:
            if snapshot.price <= 0:
                continue
            symbol = snapshot.symbol.upper()
            if symbol.startswith("XYZ:"):
                continue
            for timeframe, duration in self._timeframes.items():
                state = self._states.setdefault(
                    (symbol, timeframe),
                    OrderBlockTimeframeState(timeframe=timeframe, duration=duration),
                )
                state.observe(timestamp=parsed, symbol=symbol, price=float(snapshot.price))
        return {
            snapshot.symbol.upper(): self.features_for(
                timestamp=parsed,
                symbol=snapshot.symbol,
            )
            for snapshot in snapshots
            if snapshot.price > 0
        }

    def features_for(
        self,
        *,
        timestamp: datetime,
        symbol: str,
    ) -> OrderBlockShadowFeatures:
        normalized = symbol.upper()
        bullish: list[str] = []
        bearish: list[str] = []
        for (state_symbol, _timeframe), state in self._states.items():
            if state_symbol != normalized:
                continue
            bullish.extend(
                f"{event.pattern}:{event.timeframe}"
                for event in state.active_events(side="long")
                if event.pattern == "order_block_bull_retest"
            )
            bearish.extend(
                f"{event.pattern}:{event.timeframe}"
                for event in state.active_events(side="short")
                if event.pattern == "order_block_bear_retest"
            )
        return OrderBlockShadowFeatures(
            timestamp=isoformat(timestamp),
            symbol=normalized,
            bullish_order_blocks_1h4h=sorted(set(bullish)),
            bearish_order_blocks_1h4h=sorted(set(bearish)),
        )


def signal_order_block_shadow_details(
    features: OrderBlockShadowFeatures | None,
    regime_features: RegimeShadowFeatures | None,
    *,
    side: str,
    setup: str,
) -> dict[str, object]:
    short_candidate = str(setup) == "trend_pullback_short" or str(side) == "short"
    return order_block_shadow_details(
        features,
        regime_features,
        side=side,
        short_candidate=short_candidate,
    )


def review_order_block_shadow_details(
    features: OrderBlockShadowFeatures | None,
    regime_features: RegimeShadowFeatures | None,
    review: Mapping[str, object],
) -> dict[str, object]:
    candidates = review.get("candidate_setups", [])
    if not isinstance(candidates, list):
        candidates = []
    return order_block_shadow_details(
        features,
        regime_features,
        side=str(review.get("preferred_side", "")),
        short_candidate="trend_pullback_short" in {str(item) for item in candidates},
    )


def order_block_shadow_details(
    features: OrderBlockShadowFeatures | None,
    regime_features: RegimeShadowFeatures | None,
    *,
    side: str,
    short_candidate: bool = False,
) -> dict[str, object]:
    bullish = list(features.bullish_order_blocks_1h4h) if features is not None else []
    bearish = list(features.bearish_order_blocks_1h4h) if features is not None else []
    regime_gate = (
        regime_features.regime_gate_decision
        if regime_features is not None
        else "missing_features"
    )
    side_normalized = str(side).strip().lower()
    has_bearish = bool(bearish)
    would_block_long = (
        side_normalized == "long"
        and regime_gate in {"defensive", "bearish"}
        and has_bearish
    )
    would_open_defensive_short = (
        bool(short_candidate)
        and side_normalized == "short"
        and regime_gate == "defensive"
        and has_bearish
    )
    return {
        "order_block_shadow_mode": "observation_only",
        "bullish_order_blocks_1h4h": ",".join(bullish),
        "bearish_order_blocks_1h4h": ",".join(bearish),
        "has_bullish_order_block_1h4h": bool(bullish),
        "has_bearish_order_block_1h4h": has_bearish,
        "regime_gate_decision": regime_gate,
        "would_block_long_order_block_shadow": would_block_long,
        "would_open_defensive_short_order_block_shadow": would_open_defensive_short,
        "live_action_unchanged": True,
    }


def order_block_shadow_setup_details(details: Mapping[str, object]) -> dict[str, float | str | bool]:
    return {
        key: _setup_detail_value(value)
        for key, value in details.items()
        if key
        in {
            "order_block_shadow_mode",
            "bullish_order_blocks_1h4h",
            "bearish_order_blocks_1h4h",
            "has_bullish_order_block_1h4h",
            "has_bearish_order_block_1h4h",
            "would_block_long_order_block_shadow",
            "would_open_defensive_short_order_block_shadow",
            "live_action_unchanged",
        }
    }


def parse_timestamp(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bucket_start(value: datetime, duration: timedelta) -> datetime:
    seconds = int(duration.total_seconds())
    epoch = int(value.timestamp())
    return datetime.fromtimestamp((epoch // seconds) * seconds, tz=timezone.utc)


def candle_body_bps(candle: Candle) -> float:
    if candle.open <= 0:
        return 0.0
    return abs(candle.close - candle.open) / candle.open * 10000.0


def candle_range_bps(candle: Candle) -> float:
    if candle.open <= 0:
        return 0.0
    return abs(candle.high - candle.low) / candle.open * 10000.0


def _setup_detail_value(value: object) -> float | str | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return str(value)
