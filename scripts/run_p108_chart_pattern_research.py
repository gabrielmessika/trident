#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.live_cap import apply_live_notional_cap
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, load_config
from app.trident.pod_a.candles import Candle
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SymbolMarketSnapshot,
    TradePlan,
    symbol_market_snapshot_from_mapping,
)


DEFAULT_BASELINE_INPUT = (
    "server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl"
)
DEFAULT_LIVE_INPUT = "server-data/live_snapshots"
DEFAULT_SYMBOLS = "BTC,ETH,SOL,HYPE,DOGE,SUI,ENA,ZEC,BIO"


@dataclass(frozen=True, slots=True)
class WindowSpec:
    name: str
    input_path: Path
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True, slots=True)
class ReplayScenario:
    name: str
    description: str
    require_bullish: tuple[str, ...] = ()
    veto_bearish: tuple[str, ...] = ()


@dataclass(slots=True)
class ReplayState:
    scenario: ReplayScenario
    risk_gate: PodARiskGate
    executor: PodAExecutor
    rejected_by_filter: Counter[str] = field(default_factory=Counter)
    decisions: int = 0
    accepted: int = 0
    opened: int = 0
    skipped_open: int = 0


@dataclass(slots=True)
class ReplaySummary:
    window: str
    scenario: str
    description: str
    records_processed: int
    trade_plan_count: int
    accepted_count: int
    opened_count: int
    skipped_open_count: int
    closed_trade_count: int
    pnl_usd: float
    win_rate: float | None
    profit_factor: float | None
    max_drawdown_usd: float
    rejected_by_filter: dict[str, int]
    pnl_by_symbol: dict[str, float]
    pnl_by_setup: dict[str, float]


@dataclass(slots=True)
class PatternEvent:
    window: str
    timestamp: str
    symbol: str
    timeframe: str
    pattern: str
    side: str
    price: float


@dataclass(slots=True)
class PatternProbeTrade:
    window: str
    timestamp: str
    symbol: str
    timeframe: str
    pattern: str
    side: str
    entry_price: float
    exit_price: float
    closed_at: str
    close_reason: str
    gross_bps: float
    net_bps: float
    pnl_usd: float
    mfe_bps: float
    mae_bps: float


@dataclass(slots=True)
class PatternProbeSummary:
    window: str
    pattern: str
    side: str
    timeframe: str
    event_count: int
    trade_count: int
    pnl_usd: float
    avg_net_bps: float | None
    win_rate: float | None
    profit_factor: float | None
    avg_mfe_bps: float | None
    avg_mae_bps: float | None


@dataclass(slots=True)
class CompletedCandle:
    symbol: str
    timeframe: str
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class ActivePattern:
    event: PatternEvent
    expires_at: datetime


@dataclass(slots=True)
class TimeframeState:
    timeframe: str
    duration: timedelta
    current_bucket: datetime | None = None
    current: Candle | None = None
    completed: list[Candle] = field(default_factory=list)
    active: list[ActivePattern] = field(default_factory=list)
    emitted_keys: set[str] = field(default_factory=set)
    bullish_order_blocks: list[tuple[float, float, datetime]] = field(default_factory=list)
    bearish_order_blocks: list[tuple[float, float, datetime]] = field(default_factory=list)

    def observe(
        self,
        *,
        timestamp: datetime,
        symbol: str,
        price: float,
        window: str,
    ) -> list[PatternEvent]:
        bucket = bucket_start(timestamp, self.duration)
        events: list[PatternEvent] = []
        if self.current_bucket is None:
            self.current_bucket = bucket
            self.current = Candle(opened_at=bucket, open=price, high=price, low=price, close=price)
            return events
        if bucket != self.current_bucket:
            assert self.current is not None
            completed = self.current
            self.completed.append(completed)
            self.completed = self.completed[-96:]
            events.extend(self._detect_events(window=window, symbol=symbol, timestamp=bucket, price=price))
            self.current_bucket = bucket
            self.current = Candle(opened_at=bucket, open=price, high=price, low=price, close=price)
            self._expire(timestamp)
            return events
        assert self.current is not None
        self.current.update(price)
        self._expire(timestamp)
        return events

    def active_events(self, *, side: str | None = None) -> list[PatternEvent]:
        if side is None:
            return [item.event for item in self.active]
        return [item.event for item in self.active if item.event.side == side]

    def _expire(self, timestamp: datetime) -> None:
        self.active = [item for item in self.active if item.expires_at >= timestamp]

    def _add_event(
        self,
        *,
        events: list[PatternEvent],
        window: str,
        symbol: str,
        timestamp: datetime,
        pattern: str,
        side: str,
        price: float,
        key_suffix: str,
    ) -> None:
        key = f"{pattern}:{side}:{key_suffix}"
        if key in self.emitted_keys:
            return
        self.emitted_keys.add(key)
        event = PatternEvent(
            window=window,
            timestamp=isoformat(timestamp),
            symbol=symbol,
            timeframe=self.timeframe,
            pattern=pattern,
            side=side,
            price=round(price, 10),
        )
        events.append(event)
        self.active.append(
            ActivePattern(
                event=event,
                expires_at=timestamp + self.duration * 2,
            )
        )

    def _detect_events(
        self,
        *,
        window: str,
        symbol: str,
        timestamp: datetime,
        price: float,
    ) -> list[PatternEvent]:
        events: list[PatternEvent] = []
        candles = self.completed
        if len(candles) < 3:
            return events
        self._detect_ema_cross(events, window=window, symbol=symbol, timestamp=timestamp, price=price)
        self._detect_order_block(events, window=window, symbol=symbol, timestamp=timestamp, price=price)
        self._detect_head_shoulders(events, window=window, symbol=symbol, timestamp=timestamp, price=price)
        self._detect_cup_handle(events, window=window, symbol=symbol, timestamp=timestamp, price=price)
        return events

    def _detect_ema_cross(
        self,
        events: list[PatternEvent],
        *,
        window: str,
        symbol: str,
        timestamp: datetime,
        price: float,
    ) -> None:
        closes = [c.close for c in self.completed]
        fast = ema_series(closes, period=9)
        slow = ema_series(closes, period=21)
        if len(fast) < 2 or len(slow) < 2:
            return
        offset = len(fast) - len(slow)
        prev_fast = fast[-2]
        curr_fast = fast[-1]
        prev_slow = slow[-2]
        curr_slow = slow[-1]
        if offset > 0:
            prev_fast = fast[-2]
            curr_fast = fast[-1]
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            self._add_event(
                events=events,
                window=window,
                symbol=symbol,
                timestamp=timestamp,
                pattern="ema_cross_bull",
                side="long",
                price=price,
                key_suffix=isoformat(self.completed[-1].opened_at),
            )
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            self._add_event(
                events=events,
                window=window,
                symbol=symbol,
                timestamp=timestamp,
                pattern="ema_cross_bear",
                side="short",
                price=price,
                key_suffix=isoformat(self.completed[-1].opened_at),
            )

    def _detect_order_block(
        self,
        events: list[PatternEvent],
        *,
        window: str,
        symbol: str,
        timestamp: datetime,
        price: float,
    ) -> None:
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
        for low, high, opened_at in self.bullish_order_blocks:
            if latest.low <= high and latest.close >= high and latest.close > latest.open:
                self._add_event(
                    events=events,
                    window=window,
                    symbol=symbol,
                    timestamp=timestamp,
                    pattern="order_block_bull_retest",
                    side="long",
                    price=price,
                    key_suffix=f"{isoformat(opened_at)}:{isoformat(latest.opened_at)}",
                )
        for low, high, opened_at in self.bearish_order_blocks:
            if latest.high >= low and latest.close <= low and latest.close < latest.open:
                self._add_event(
                    events=events,
                    window=window,
                    symbol=symbol,
                    timestamp=timestamp,
                    pattern="order_block_bear_retest",
                    side="short",
                    price=price,
                    key_suffix=f"{isoformat(opened_at)}:{isoformat(latest.opened_at)}",
                )

    def _detect_head_shoulders(
        self,
        events: list[PatternEvent],
        *,
        window: str,
        symbol: str,
        timestamp: datetime,
        price: float,
    ) -> None:
        candles = self.completed[-36:]
        if len(candles) < 15:
            return
        highs = pivot_highs(candles)
        lows = pivot_lows(candles)
        if len(highs) >= 3 and len(lows) >= 2:
            h1, h2, h3 = highs[-3:]
            left = candles[h1].high
            head = candles[h2].high
            right = candles[h3].high
            shoulder_avg = (left + right) / 2.0
            shoulder_match = abs(left - right) / max(shoulder_avg, 1e-9) <= 0.035
            head_clear = head >= shoulder_avg * 1.008
            between_lows = [
                candles[index].low
                for index in lows
                if h1 < index < h3
            ]
            neckline = min(between_lows) if between_lows else 0.0
            if shoulder_match and head_clear and neckline > 0 and candles[-1].close < neckline:
                self._add_event(
                    events=events,
                    window=window,
                    symbol=symbol,
                    timestamp=timestamp,
                    pattern="head_shoulders_breakdown",
                    side="short",
                    price=price,
                    key_suffix=f"{isoformat(candles[h2].opened_at)}:{isoformat(candles[-1].opened_at)}",
                )
        if len(lows) >= 3 and len(highs) >= 2:
            l1, l2, l3 = lows[-3:]
            left = candles[l1].low
            head = candles[l2].low
            right = candles[l3].low
            shoulder_avg = (left + right) / 2.0
            shoulder_match = abs(left - right) / max(shoulder_avg, 1e-9) <= 0.035
            head_clear = head <= shoulder_avg * 0.992
            between_highs = [
                candles[index].high
                for index in highs
                if l1 < index < l3
            ]
            neckline = max(between_highs) if between_highs else 0.0
            if shoulder_match and head_clear and neckline > 0 and candles[-1].close > neckline:
                self._add_event(
                    events=events,
                    window=window,
                    symbol=symbol,
                    timestamp=timestamp,
                    pattern="inverse_head_shoulders_breakout",
                    side="long",
                    price=price,
                    key_suffix=f"{isoformat(candles[l2].opened_at)}:{isoformat(candles[-1].opened_at)}",
                )

    def _detect_cup_handle(
        self,
        events: list[PatternEvent],
        *,
        window: str,
        symbol: str,
        timestamp: datetime,
        price: float,
    ) -> None:
        candles = self.completed
        for length in (24, 36, 48):
            if len(candles) < length:
                continue
            sample = candles[-length:]
            left = sample[: max(5, length // 5)]
            middle = sample[length // 5 : int(length * 0.72)]
            handle = sample[int(length * 0.72) :]
            left_high = max(c.high for c in left)
            mid_low = min(c.low for c in middle)
            right_high = max(c.high for c in middle[-max(4, length // 6) :])
            rim = min(left_high, right_high)
            cup_depth = (rim - mid_low) / max(rim, 1e-9)
            handle_low = min(c.low for c in handle)
            handle_depth = (rim - handle_low) / max(rim, 1e-9)
            if (
                0.012 <= cup_depth <= 0.18
                and 0.002 <= handle_depth <= cup_depth * 0.65
                and abs(left_high - right_high) / max(rim, 1e-9) <= 0.045
                and sample[-1].close > rim * 1.002
            ):
                self._add_event(
                    events=events,
                    window=window,
                    symbol=symbol,
                    timestamp=timestamp,
                    pattern="cup_handle_breakout",
                    side="long",
                    price=price,
                    key_suffix=f"{length}:{isoformat(sample[-1].opened_at)}",
                )
            left_low = min(c.low for c in left)
            mid_high = max(c.high for c in middle)
            right_low = min(c.low for c in middle[-max(4, length // 6) :])
            rim_low = max(left_low, right_low)
            inv_depth = (mid_high - rim_low) / max(rim_low, 1e-9)
            handle_high = max(c.high for c in handle)
            inv_handle_depth = (handle_high - rim_low) / max(rim_low, 1e-9)
            if (
                0.012 <= inv_depth <= 0.18
                and 0.002 <= inv_handle_depth <= inv_depth * 0.65
                and abs(left_low - right_low) / max(rim_low, 1e-9) <= 0.045
                and sample[-1].close < rim_low * 0.998
            ):
                self._add_event(
                    events=events,
                    window=window,
                    symbol=symbol,
                    timestamp=timestamp,
                    pattern="inverse_cup_handle_breakdown",
                    side="short",
                    price=price,
                    key_suffix=f"{length}:{isoformat(sample[-1].opened_at)}",
                )


class ChartPatternEngine:
    def __init__(self, *, timeframes: dict[str, timedelta]) -> None:
        self.timeframes = timeframes
        self._states: dict[tuple[str, str], TimeframeState] = {}

    def observe(
        self,
        *,
        timestamp: datetime,
        window: str,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[PatternEvent]:
        events: list[PatternEvent] = []
        for snapshot in snapshots:
            if snapshot.price <= 0:
                continue
            symbol = snapshot.symbol.upper()
            if symbol.startswith("XYZ:"):
                continue
            for timeframe, duration in self.timeframes.items():
                state = self._states.setdefault(
                    (symbol, timeframe),
                    TimeframeState(timeframe=timeframe, duration=duration),
                )
                events.extend(
                    state.observe(
                        timestamp=timestamp,
                        symbol=symbol,
                        price=float(snapshot.price),
                        window=window,
                    )
                )
        return events

    def active_events(self, symbol: str, *, side: str | None = None) -> list[PatternEvent]:
        rows: list[PatternEvent] = []
        for (state_symbol, _timeframe), state in self._states.items():
            if state_symbol != symbol.upper():
                continue
            rows.extend(state.active_events(side=side))
        return rows

    def active_names(self, symbol: str, *, side: str | None = None) -> set[str]:
        return {event.pattern for event in self.active_events(symbol, side=side)}


def replay_scenarios() -> list[ReplayScenario]:
    return [
        ReplayScenario("current", "Pod A current config, no extra chart-pattern gate."),
        ReplayScenario(
            "veto_bearish_classic_any_tf",
            "Drop long plans when bearish EMA/order-block/H&S/cup-like pattern is active on any timeframe.",
            veto_bearish=(
                "ema_cross_bear",
                "order_block_bear_retest",
                "head_shoulders_breakdown",
                "inverse_cup_handle_breakdown",
            ),
        ),
        ReplayScenario(
            "veto_bearish_ema_any_tf",
            "Drop long plans when bearish EMA cross is active on any timeframe.",
            veto_bearish=("ema_cross_bear",),
        ),
        ReplayScenario(
            "veto_bearish_order_block_any_tf",
            "Drop long plans when bearish order-block retest is active on any timeframe.",
            veto_bearish=("order_block_bear_retest",),
        ),
        ReplayScenario(
            "require_bullish_classic_any_tf",
            "Keep long plans only when bullish EMA/order-block/inverse-H&S/cup pattern is active on any timeframe.",
            require_bullish=(
                "ema_cross_bull",
                "order_block_bull_retest",
                "inverse_head_shoulders_breakout",
                "cup_handle_breakout",
            ),
        ),
        ReplayScenario(
            "require_bullish_ema_any_tf",
            "Keep long plans only when bullish EMA cross is active on any timeframe.",
            require_bullish=("ema_cross_bull",),
        ),
        ReplayScenario(
            "require_bullish_order_block_any_tf",
            "Keep long plans only when bullish order-block retest is active on any timeframe.",
            require_bullish=("order_block_bull_retest",),
        ),
        ReplayScenario(
            "require_inverse_hs_or_cup_any_tf",
            "Keep long plans only when inverse H&S or cup-and-handle breakout is active.",
            require_bullish=("inverse_head_shoulders_breakout", "cup_handle_breakout"),
        ),
    ]


def parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def bucket_start(value: datetime, duration: timedelta) -> datetime:
    seconds = int(duration.total_seconds())
    epoch = int(value.timestamp())
    return datetime.fromtimestamp((epoch // seconds) * seconds, tz=timezone.utc)


def ema_series(values: list[float], *, period: int) -> list[float]:
    if len(values) < period or period <= 0:
        return []
    alpha = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    series = [ema]
    for value in values[period:]:
        ema = value * alpha + ema * (1.0 - alpha)
        series.append(ema)
    return series


def candle_body_bps(candle: Candle) -> float:
    if candle.open <= 0:
        return 0.0
    return abs(candle.close - candle.open) / candle.open * 10000.0


def candle_range_bps(candle: Candle) -> float:
    if candle.open <= 0:
        return 0.0
    return abs(candle.high - candle.low) / candle.open * 10000.0


def pivot_highs(candles: list[Candle]) -> list[int]:
    return [
        index
        for index in range(1, len(candles) - 1)
        if candles[index].high > candles[index - 1].high
        and candles[index].high >= candles[index + 1].high
    ]


def pivot_lows(candles: list[Candle]) -> list[int]:
    return [
        index
        for index in range(1, len(candles) - 1)
        if candles[index].low < candles[index - 1].low
        and candles[index].low <= candles[index + 1].low
    ]


def run_window(
    *,
    config: AppConfig,
    window: WindowSpec,
    scenarios: list[ReplayScenario],
    symbols: set[str],
    apply_live_caps: bool,
    timeframes: dict[str, timedelta],
    standalone_notional_usd: float,
    standalone_cost_bps: float,
    standalone_stop_bps: float,
    standalone_take_profit_bps: float,
) -> tuple[list[ReplaySummary], list[PatternEvent], list[PatternProbeTrade], list[PatternProbeSummary], dict[str, Any]]:
    loader = SnapshotLoader()
    supervisor = TridentSupervisor(
        config=config,
        profile=f"p108-chart-pattern-{window.name}",
        mode="observation",
    )
    states = [
        ReplayState(
            scenario=scenario,
            risk_gate=PodARiskGate(config),
            executor=PodAExecutor(config),
        )
        for scenario in scenarios
    ]
    engine = ChartPatternEngine(timeframes=timeframes)
    all_events: list[PatternEvent] = []
    point_index: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    last_snapshots: list[SymbolMarketSnapshot] = []
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    records_processed = 0
    skipped_records = 0
    started = time.perf_counter()

    for record in loader.iter_merged_jsonl(window.input_path):
        timestamp = parse_timestamp(record.timestamp)
        if timestamp is None:
            skipped_records += 1
            continue
        if window.start is not None and timestamp < window.start:
            skipped_records += 1
            continue
        if window.end is not None and timestamp > window.end:
            skipped_records += 1
            continue
        snapshots = [
            symbol_market_snapshot_from_mapping(item)
            for item in record.symbols
            if isinstance(item, dict)
            and str(item.get("symbol", "")).strip().upper() in symbols
        ]
        if not snapshots:
            skipped_records += 1
            continue
        records_processed += 1
        first_timestamp = first_timestamp or isoformat(timestamp)
        last_timestamp = isoformat(timestamp)
        regime_raw = record.regime_snapshot or {}
        cluster_regimes = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        }
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(**regime_raw),
            cluster_regime_snapshots=cluster_regimes,
        )
        current_events = engine.observe(timestamp=timestamp, window=window.name, snapshots=snapshots)
        all_events.extend(current_events)
        for snapshot in snapshots:
            point_index[snapshot.symbol.upper()].append((timestamp, float(snapshot.price)))

        trade_plans = supervisor.build_pod_a_trade_plans(snapshots, timestamp=record.timestamp)
        if apply_live_caps:
            trade_plans = [
                apply_live_notional_cap(
                    plan,
                    config.trident.execution.live_max_order_notional_usd,
                    max_leverage=config.pod_a.max_leverage,
                )
                for plan in trade_plans
            ]
        for state in states:
            entry_allowed_symbols = supervisor.opening_symbols_for(PodName.POD_A)
            managed_symbols = supervisor.managed_symbols_for(
                PodName.POD_A,
                {str(symbol).upper() for symbol in state.executor.portfolio.open_positions},
            )
            filtered_plans = _filter_plans(
                state=state,
                engine=engine,
                plans=trade_plans,
                timestamp=record.timestamp,
            )
            risk_decisions = state.risk_gate.evaluate_many(filtered_plans)
            state.decisions += len(filtered_plans)
            state.accepted += sum(1 for decision in risk_decisions if decision.accepted)
            execution = state.executor.process_record(
                snapshots=snapshots,
                risk_decisions=risk_decisions,
                signal_sides_by_symbol={
                    decision.trade_plan.symbol: decision.trade_plan.side
                    for decision in risk_decisions
                },
                timestamp=record.timestamp,
                entry_allowed_symbols=entry_allowed_symbols,
                managed_symbols=managed_symbols,
            )
            state.opened += len(execution.opened_symbols)
            state.skipped_open += len(execution.skipped_open_symbols)
            for trade in execution.closed_trades:
                state.risk_gate.record_closed_trade(
                    symbol=trade.symbol,
                    setup=getattr(trade, "setup", None),
                    pnl_usd=trade.pnl_usd,
                    date_key=trade.closed_at.date().isoformat() if trade.closed_at else "unknown",
                )
        last_snapshots = snapshots

    for state in states:
        final_trades, _ = state.executor.finalize(
            snapshots=last_snapshots,
            timestamp=last_timestamp,
        )
        for trade in final_trades:
            state.risk_gate.record_closed_trade(
                symbol=trade.symbol,
                setup=getattr(trade, "setup", None),
                pnl_usd=trade.pnl_usd,
                date_key=trade.closed_at.date().isoformat() if trade.closed_at else "finalize",
            )
    probe_trades = simulate_pattern_probe_trades(
        events=all_events,
        point_index=point_index,
        notional_usd=standalone_notional_usd,
        cost_bps=standalone_cost_bps,
        stop_bps=standalone_stop_bps,
        take_profit_bps=standalone_take_profit_bps,
    )
    meta = {
        "window": window.name,
        "input_path": str(window.input_path),
        "start": isoformat(window.start),
        "end": isoformat(window.end),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "records_processed": records_processed,
        "records_skipped": skipped_records,
        "symbols": sorted(symbols),
        "apply_live_caps": apply_live_caps,
        "standalone_notional_usd": standalone_notional_usd,
        "standalone_cost_bps": standalone_cost_bps,
        "standalone_stop_bps": standalone_stop_bps,
        "standalone_take_profit_bps": standalone_take_profit_bps,
        "runtime_seconds": round(time.perf_counter() - started, 3),
    }
    return (
        [_summarize_replay_state(window.name, records_processed, state) for state in states],
        all_events,
        probe_trades,
        summarize_probe_trades(probe_trades, all_events),
        meta,
    )


def _filter_plans(
    *,
    state: ReplayState,
    engine: ChartPatternEngine,
    plans: list[TradePlan],
    timestamp: str | None,
) -> list[TradePlan]:
    scenario = state.scenario
    filtered: list[TradePlan] = []
    for plan in plans:
        if plan.side != "long":
            filtered.append(plan)
            continue
        bullish = engine.active_names(plan.symbol, side="long")
        bearish = engine.active_names(plan.symbol, side="short")
        required = set(scenario.require_bullish)
        vetoed = set(scenario.veto_bearish)
        if required and not (bullish & required):
            state.rejected_by_filter["missing_bullish_pattern"] += 1
            continue
        hit_veto = bearish & vetoed
        if hit_veto:
            for name in sorted(hit_veto):
                state.rejected_by_filter[f"bearish_{name}"] += 1
            continue
        details = dict(plan.setup_details or {})
        if bullish:
            details["chart_bullish_patterns"] = ",".join(sorted(bullish))
        if bearish:
            details["chart_bearish_patterns"] = ",".join(sorted(bearish))
        details["chart_pattern_scenario"] = scenario.name
        filtered.append(replace(plan, setup_details=details))
    return filtered


def _summarize_replay_state(window: str, records_processed: int, state: ReplayState) -> ReplaySummary:
    trades = list(state.executor.portfolio.closed_trades)
    pnl = round(sum(float(trade.pnl_usd or 0.0) for trade in trades), 6)
    wins = [float(trade.pnl_usd or 0.0) for trade in trades if float(trade.pnl_usd or 0.0) > 0]
    losses = [float(trade.pnl_usd or 0.0) for trade in trades if float(trade.pnl_usd or 0.0) < 0]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        equity += float(trade.pnl_usd or 0.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    pnl_by_symbol: dict[str, float] = defaultdict(float)
    pnl_by_setup: dict[str, float] = defaultdict(float)
    for trade in trades:
        pnl_by_symbol[str(trade.symbol)] += float(trade.pnl_usd or 0.0)
        pnl_by_setup[str(getattr(trade, "setup", "") or "")] += float(trade.pnl_usd or 0.0)
    return ReplaySummary(
        window=window,
        scenario=state.scenario.name,
        description=state.scenario.description,
        records_processed=records_processed,
        trade_plan_count=state.decisions,
        accepted_count=state.accepted,
        opened_count=state.opened,
        skipped_open_count=state.skipped_open,
        closed_trade_count=len(trades),
        pnl_usd=round(pnl, 6),
        win_rate=round(len(wins) / len(trades), 6) if trades else None,
        profit_factor=profit_factor(wins, losses),
        max_drawdown_usd=round(abs(max_dd), 6),
        rejected_by_filter=dict(state.rejected_by_filter),
        pnl_by_symbol={key: round(value, 6) for key, value in sorted(pnl_by_symbol.items())},
        pnl_by_setup={key: round(value, 6) for key, value in sorted(pnl_by_setup.items())},
    )


def simulate_pattern_probe_trades(
    *,
    events: list[PatternEvent],
    point_index: dict[str, list[tuple[datetime, float]]],
    notional_usd: float,
    cost_bps: float,
    stop_bps: float,
    take_profit_bps: float,
) -> list[PatternProbeTrade]:
    trades: list[PatternProbeTrade] = []
    cooldown: dict[tuple[str, str, str], datetime] = {}
    indexed_points = {
        symbol: ([timestamp for timestamp, _price in rows], [price for _timestamp, price in rows])
        for symbol, rows in point_index.items()
    }
    for event in events:
        opened_at = parse_timestamp(event.timestamp)
        if opened_at is None:
            continue
        cooldown_key = (event.symbol, event.timeframe, event.pattern)
        if cooldown.get(cooldown_key, datetime.min.replace(tzinfo=timezone.utc)) > opened_at:
            continue
        timestamps, prices = indexed_points.get(event.symbol, ([], []))
        trade = simulate_event_trade(
            event=event,
            timestamps=timestamps,
            prices=prices,
            opened_at=opened_at,
            notional_usd=notional_usd,
            cost_bps=cost_bps,
            stop_bps=stop_bps,
            take_profit_bps=take_profit_bps,
        )
        if trade is None:
            continue
        trades.append(trade)
        cooldown[cooldown_key] = opened_at + timeframe_duration(event.timeframe) * 2
    return trades


def simulate_event_trade(
    *,
    event: PatternEvent,
    timestamps: list[datetime],
    prices: list[float],
    opened_at: datetime,
    notional_usd: float,
    cost_bps: float,
    stop_bps: float,
    take_profit_bps: float,
) -> PatternProbeTrade | None:
    horizon = timeframe_duration(event.timeframe) * 8
    deadline = opened_at + horizon
    entry_price = event.price
    if entry_price <= 0:
        return None
    close_time: datetime | None = None
    close_price = 0.0
    close_reason = "time_stop"
    gross_bps = 0.0
    mfe = -math.inf
    mae = math.inf
    started = False
    start_index = bisect_left(timestamps, opened_at)
    for index in range(start_index, len(timestamps)):
        timestamp = timestamps[index]
        if timestamp > deadline:
            break
        price = prices[index]
        started = True
        gross_bps = side_return_bps(entry_price, price, event.side)
        mfe = max(mfe, gross_bps)
        mae = min(mae, gross_bps)
        close_time = timestamp
        close_price = price
        if gross_bps <= -stop_bps:
            close_reason = "stop"
            break
        if gross_bps >= take_profit_bps:
            close_reason = "take_profit"
            break
    if not started or close_time is None:
        return None
    net_bps = gross_bps - cost_bps
    return PatternProbeTrade(
        window=event.window,
        timestamp=event.timestamp,
        symbol=event.symbol,
        timeframe=event.timeframe,
        pattern=event.pattern,
        side=event.side,
        entry_price=round(entry_price, 10),
        exit_price=round(close_price, 10),
        closed_at=isoformat(close_time),
        close_reason=close_reason,
        gross_bps=round(gross_bps, 6),
        net_bps=round(net_bps, 6),
        pnl_usd=round(notional_usd * net_bps / 10000.0, 6),
        mfe_bps=round(mfe if mfe != -math.inf else gross_bps, 6),
        mae_bps=round(mae if mae != math.inf else gross_bps, 6),
    )


def summarize_probe_trades(
    trades: list[PatternProbeTrade],
    events: list[PatternEvent],
) -> list[PatternProbeSummary]:
    event_counts = Counter((event.window, event.pattern, event.side, event.timeframe) for event in events)
    grouped: dict[tuple[str, str, str, str], list[PatternProbeTrade]] = defaultdict(list)
    for trade in trades:
        grouped[(trade.window, trade.pattern, trade.side, trade.timeframe)].append(trade)
    keys = sorted(set(event_counts) | set(grouped))
    summaries: list[PatternProbeSummary] = []
    for window, pattern, side, timeframe in keys:
        rows = grouped.get((window, pattern, side, timeframe), [])
        wins = [row.pnl_usd for row in rows if row.pnl_usd > 0]
        losses = [row.pnl_usd for row in rows if row.pnl_usd < 0]
        summaries.append(
            PatternProbeSummary(
                window=window,
                pattern=pattern,
                side=side,
                timeframe=timeframe,
                event_count=event_counts.get((window, pattern, side, timeframe), 0),
                trade_count=len(rows),
                pnl_usd=round(sum(row.pnl_usd for row in rows), 6),
                avg_net_bps=round(sum(row.net_bps for row in rows) / len(rows), 6) if rows else None,
                win_rate=round(len(wins) / len(rows), 6) if rows else None,
                profit_factor=profit_factor(wins, losses),
                avg_mfe_bps=round(sum(row.mfe_bps for row in rows) / len(rows), 6) if rows else None,
                avg_mae_bps=round(sum(row.mae_bps for row in rows) / len(rows), 6) if rows else None,
            )
        )
    return summaries


def timeframe_duration(timeframe: str) -> timedelta:
    if timeframe.endswith("m"):
        return timedelta(minutes=int(timeframe[:-1]))
    if timeframe.endswith("h"):
        return timedelta(hours=int(timeframe[:-1]))
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def side_return_bps(entry: float, exit_price: float, side: str) -> float:
    if entry <= 0 or exit_price <= 0:
        return 0.0
    raw = (exit_price / entry - 1.0) * 10000.0
    return raw if side == "long" else -raw


def profit_factor(wins: Iterable[float], losses: Iterable[float]) -> float | None:
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_win <= 0 and gross_loss <= 0:
        return None
    if gross_loss <= 0:
        return None
    return round(gross_win / gross_loss, 6)


def write_csv(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(asdict(rows[0]).keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_report(
    *,
    output_dir: Path,
    replay_summaries: list[ReplaySummary],
    probe_summaries: list[PatternProbeSummary],
    metas: list[dict[str, Any]],
) -> None:
    lines = [
        "# P1-08 chart pattern research",
        "",
        "Statut: `research_only`.",
        "",
        "Scope: Pod A crypto snapshots only. Les patterns sont detectes sur OHLC reconstruits depuis les snapshots minute, pas depuis des chandeliers exchange natifs.",
        "",
        "## Donnees",
        "",
    ]
    for meta in metas:
        lines.append(
            f"- `{meta['window']}`: `{meta['first_timestamp']}` -> `{meta['last_timestamp']}`, "
            f"{meta['records_processed']} records, symbols={meta['symbols']}"
        )
    lines.extend(["", "## Replay Pod A avec filtres patterns", ""])
    for summary in replay_summaries:
        if summary.scenario != "current":
            baseline = next(
                (
                    item
                    for item in replay_summaries
                    if item.window == summary.window and item.scenario == "current"
                ),
                None,
            )
            delta = summary.pnl_usd - baseline.pnl_usd if baseline is not None else 0.0
        else:
            delta = 0.0
        lines.append(
            f"- `{summary.window}` / `{summary.scenario}`: pnl `{summary.pnl_usd:.2f}` USD "
            f"(delta `{delta:+.2f}`), trades `{summary.closed_trade_count}`, "
            f"PF `{format_optional(summary.profit_factor)}`, DD `{summary.max_drawdown_usd:.2f}`."
        )
    lines.extend(["", "## Standalone pattern probes", ""])
    top_rows = sorted(probe_summaries, key=lambda item: item.pnl_usd, reverse=True)[:20]
    bottom_rows = sorted(probe_summaries, key=lambda item: item.pnl_usd)[:20]
    lines.append("Top 20 PnL simule:")
    for item in top_rows:
        lines.append(
            f"- `{item.window}` `{item.pattern}` `{item.timeframe}` `{item.side}`: "
            f"pnl `{item.pnl_usd:.2f}`, n `{item.trade_count}`, avg `{format_optional(item.avg_net_bps)}` bps, "
            f"PF `{format_optional(item.profit_factor)}`."
        )
    lines.append("")
    lines.append("Bottom 20 PnL simule:")
    for item in bottom_rows:
        lines.append(
            f"- `{item.window}` `{item.pattern}` `{item.timeframe}` `{item.side}`: "
            f"pnl `{item.pnl_usd:.2f}`, n `{item.trade_count}`, avg `{format_optional(item.avg_net_bps)}` bps, "
            f"PF `{format_optional(item.profit_factor)}`."
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- Un filtre est interessant seulement s'il ameliore la fenetre recente sans detruire la baseline avril/mai.",
            "- Un pattern standalone est seulement une piste si son edge survit aux deux regimes et avec un nombre de trades raisonnable.",
            "- Aucun resultat de ce rapport ne doit etre promu sans replay full-bot dedie et shadow live.",
            "",
        ]
    )
    (output_dir / "p108_chart_pattern_research.md").write_text("\n".join(lines), encoding="utf-8")


def format_optional(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def parse_window(value: str) -> datetime | None:
    if not value:
        return None
    return parse_timestamp(value)


def parse_symbols(value: str) -> set[str]:
    return {item.strip().upper() for item in value.split(",") if item.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--live-input", default=DEFAULT_LIVE_INPUT)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--windows",
        default="baseline_apr_may,live_post_baseline",
        help="Comma-separated windows: baseline_apr_may,live_post_baseline,all_available",
    )
    parser.add_argument("--apply-live-caps", action="store_true", default=True)
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--cost-bps", type=float, default=16.0)
    parser.add_argument("--stop-bps", type=float, default=160.0)
    parser.add_argument("--take-profit-bps", type=float, default=260.0)
    args = parser.parse_args()

    config = load_config(args.config)
    symbols = parse_symbols(args.symbols)
    window_by_name = {
        "baseline_apr_may": WindowSpec(
            name="baseline_apr_may",
            input_path=Path(args.baseline_input),
            start=parse_window("2026-04-05T00:00:00Z"),
            end=parse_window("2026-05-13T23:59:59Z"),
        ),
        "live_post_baseline": WindowSpec(
            name="live_post_baseline",
            input_path=Path(args.live_input),
            start=parse_window("2026-05-14T00:00:00Z"),
            end=parse_window("2026-06-12T23:59:59Z"),
        ),
        "all_available": WindowSpec(
            name="all_available",
            input_path=Path(args.live_input),
            start=parse_window("2026-04-05T00:00:00Z"),
            end=parse_window("2026-06-12T23:59:59Z"),
        ),
    }
    requested_windows = [item.strip() for item in args.windows.split(",") if item.strip()]
    windows = [window_by_name[name] for name in requested_windows]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p108_chart_patterns_{stamp}")
    output_dir.mkdir(parents=True, exist_ok=True)
    scenarios = replay_scenarios()
    timeframes = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
    }
    all_replay_summaries: list[ReplaySummary] = []
    all_events: list[PatternEvent] = []
    all_probe_trades: list[PatternProbeTrade] = []
    all_probe_summaries: list[PatternProbeSummary] = []
    metas: list[dict[str, Any]] = []
    for window in windows:
        replay_summaries, events, probe_trades, probe_summaries, meta = run_window(
            config=config,
            window=window,
            scenarios=scenarios,
            symbols=symbols,
            apply_live_caps=args.apply_live_caps,
            timeframes=timeframes,
            standalone_notional_usd=args.notional_usd,
            standalone_cost_bps=args.cost_bps,
            standalone_stop_bps=args.stop_bps,
            standalone_take_profit_bps=args.take_profit_bps,
        )
        all_replay_summaries.extend(replay_summaries)
        all_events.extend(events)
        all_probe_trades.extend(probe_trades)
        all_probe_summaries.extend(probe_summaries)
        metas.append(meta)

    write_csv(output_dir / "replay_summaries.csv", all_replay_summaries)
    write_csv(output_dir / "pattern_events.csv", all_events)
    write_csv(output_dir / "pattern_probe_trades.csv", all_probe_trades)
    write_csv(output_dir / "pattern_probe_summaries.csv", all_probe_summaries)
    (output_dir / "p108_chart_pattern_research.json").write_text(
        json.dumps(
            {
                "meta": metas,
                "replay_summaries": [asdict(item) for item in all_replay_summaries],
                "pattern_probe_summaries": [asdict(item) for item in all_probe_summaries],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    write_report(
        output_dir=output_dir,
        replay_summaries=all_replay_summaries,
        probe_summaries=all_probe_summaries,
        metas=metas,
    )
    print(json.dumps({"output_dir": str(output_dir), "meta": metas}, indent=2))


if __name__ == "__main__":
    main()
