#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.live_cap import apply_live_notional_cap
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.pod_a.regime_shadow import build_regime_shadow_features
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


@dataclass(frozen=True, slots=True)
class WindowSpec:
    name: str
    input_path: Path
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True, slots=True)
class GateSpec:
    name: str
    description: str
    allow_longs: bool = True
    allow_shorts: bool = False
    long_min_bull: int = 0
    long_max_bear: int = 99
    short_min_bear: int = 99
    short_max_bull: int = 0
    block_longs_when_bear_ge: int | None = None
    long_regime_gates: tuple[str, ...] = ()
    short_regime_gates: tuple[str, ...] = ()


@dataclass(slots=True)
class GateFeatures:
    timestamp: str
    symbol: str
    bull_score: int
    bear_score: int
    regime_gate: str
    btc_ret_60m_bps: float | None
    btc_ret_240m_bps: float | None
    btc_ret_1440m_bps: float | None
    symbol_ret_60m_bps: float | None
    symbol_ret_240m_bps: float | None
    btc_above_ema_slow: bool
    btc_fast_above_slow: bool
    symbol_above_ema_slow: bool
    symbol_fast_above_slow: bool
    structure_score: float
    breadth_pct: float | None
    alt_participation_pct: float | None
    leader_trend_score: float | None
    coherence_score: float | None
    dispersion_pct: float | None


@dataclass(slots=True)
class FeatureHistory:
    timestamps: list[datetime] = field(default_factory=list)
    prices: list[float] = field(default_factory=list)

    def append(self, timestamp: datetime, price: float) -> None:
        if price <= 0:
            return
        if self.timestamps and timestamp < self.timestamps[-1]:
            return
        self.timestamps.append(timestamp)
        self.prices.append(price)

    def return_bps(self, timestamp: datetime, minutes: int, current_price: float) -> float | None:
        cutoff = timestamp - timedelta(minutes=minutes)
        index = bisect_right(self.timestamps, cutoff) - 1
        if index < 0:
            return None
        previous_price = self.prices[index]
        if previous_price <= 0 or current_price <= 0:
            return None
        return (current_price / previous_price - 1.0) * 10000.0

    def future_return_bps(self, index: int, minutes: int) -> float | None:
        if index < 0 or index >= len(self.timestamps):
            return None
        target = self.timestamps[index] + timedelta(minutes=minutes)
        future_index = bisect_right(self.timestamps, target) - 1
        if future_index <= index:
            return None
        if self.timestamps[future_index] < target - timedelta(minutes=5):
            return None
        price = self.prices[index]
        future_price = self.prices[future_index]
        if price <= 0 or future_price <= 0:
            return None
        return (future_price / price - 1.0) * 10000.0


@dataclass(slots=True)
class ScenarioState:
    spec: GateSpec
    risk_gate: PodARiskGate
    executor: PodAExecutor
    report: PodABacktestReport
    gate_rejections: Counter[str] = field(default_factory=Counter)
    gate_decisions: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class ScenarioSummary:
    window: str
    scenario: str
    description: str
    records_processed: int
    signal_count: int
    accepted_count: int
    rejected_count: int
    gate_rejections: dict[str, int]
    gate_decisions: dict[str, int]
    opened_count: int
    skipped_open_count: int
    closed_trade_count: int
    realized_pnl_usd: float
    fees_usd: float
    max_drawdown_usd: float
    win_rate: float | None
    profit_factor: float | None
    trades_by_setup: dict[str, int]
    pnl_by_setup: dict[str, float]
    trades_by_side: dict[str, int]
    pnl_by_side: dict[str, float]
    close_reasons: dict[str, int]
    avg_bull_score: float | None
    avg_bear_score: float | None


@dataclass(slots=True)
class ScenarioTradeRow:
    window: str
    scenario: str
    symbol: str
    side: str
    setup: str | None
    opened_at: str | None
    closed_at: str | None
    close_reason: str
    pnl_usd: float
    fees_usd: float
    hold_hours: float | None
    confidence: float | None
    bull_score: float | None
    bear_score: float | None
    regime_gate: str
    btc_ret_60m_bps: float | None
    btc_ret_240m_bps: float | None
    btc_ret_1440m_bps: float | None
    symbol_ret_60m_bps: float | None
    symbol_ret_240m_bps: float | None


@dataclass(slots=True)
class RegimeLabelRow:
    window: str
    timestamp: str
    label_6h: str
    label_24h: str
    bull_score: int
    bear_score: int
    btc_ret_60m_bps: float | None
    btc_ret_240m_bps: float | None
    btc_ret_1440m_bps: float | None
    btc_fwd_360m_bps: float | None
    btc_fwd_1440m_bps: float | None
    breadth_pct: float | None
    leader_trend_score: float | None
    structure_score: float


def default_gate_specs() -> list[GateSpec]:
    return [
        GateSpec(
            name="current_long_only",
            description="Current Pod A direction policy: trend_pullback_long only, no extra regime gate.",
            allow_longs=True,
            allow_shorts=False,
        ),
        GateSpec(
            name="long_not_bear",
            description="Longs only, but block entries when pre-entry bear_score >= 4.",
            allow_longs=True,
            allow_shorts=False,
            block_longs_when_bear_ge=4,
        ),
        GateSpec(
            name="global_long_short",
            description="Research control: allow all trend_pullback_long and trend_pullback_short plans.",
            allow_longs=True,
            allow_shorts=True,
            short_min_bear=0,
            short_max_bull=99,
        ),
        GateSpec(
            name="short_only_global",
            description="Research control: trend_pullback_short only, no regime gate.",
            allow_longs=False,
            allow_shorts=True,
            short_min_bear=0,
            short_max_bull=99,
        ),
        GateSpec(
            name="defensive_short_only",
            description="Research candidate: shorts only in defensive transition, not full bearish.",
            allow_longs=False,
            allow_shorts=True,
            short_min_bear=0,
            short_max_bull=99,
            short_regime_gates=("defensive",),
        ),
        GateSpec(
            name="long_not_bear_defensive_short",
            description="Candidate: current longs blocked when bear_score >= 4, plus shorts only in defensive transition.",
            allow_longs=True,
            allow_shorts=True,
            short_min_bear=0,
            short_max_bull=99,
            block_longs_when_bear_ge=4,
            short_regime_gates=("defensive",),
        ),
        GateSpec(
            name="bull3_long_only",
            description="Longs only when bull_score >= 3 and bear_score <= 2.",
            allow_longs=True,
            allow_shorts=False,
            long_min_bull=3,
            long_max_bear=2,
        ),
        GateSpec(
            name="bull3_bear3_long_short",
            description="Long bull_score >= 3 / bear_score <= 2; short bear_score >= 3 / bull_score <= 2.",
            allow_longs=True,
            allow_shorts=True,
            long_min_bull=3,
            long_max_bear=2,
            short_min_bear=3,
            short_max_bull=2,
        ),
        GateSpec(
            name="bear3_short_only",
            description="Research control: shorts only when bear_score >= 3 and bull_score <= 2.",
            allow_longs=False,
            allow_shorts=True,
            short_min_bear=3,
            short_max_bull=2,
        ),
        GateSpec(
            name="bull3_bear4_long_short",
            description="Long bull_score >= 3 / bear_score <= 2; stricter short bear_score >= 4 / bull_score <= 2.",
            allow_longs=True,
            allow_shorts=True,
            long_min_bull=3,
            long_max_bear=2,
            short_min_bear=4,
            short_max_bull=2,
        ),
        GateSpec(
            name="bear4_short_only",
            description="Research control: shorts only when bear_score >= 4 and bull_score <= 2.",
            allow_longs=False,
            allow_shorts=True,
            short_min_bear=4,
            short_max_bull=2,
        ),
        GateSpec(
            name="strict_bull4_bear4",
            description="Strict directional gate: long bull_score >= 4 / bear_score <= 2; short bear_score >= 4 / bull_score <= 1.",
            allow_longs=True,
            allow_shorts=True,
            long_min_bull=4,
            long_max_bear=2,
            short_min_bear=4,
            short_max_bull=1,
        ),
    ]


def run_window(
    *,
    config: AppConfig,
    window: WindowSpec,
    gate_specs: list[GateSpec],
    apply_live_caps: bool,
    sample_label_minutes: int,
) -> tuple[list[ScenarioSummary], list[RegimeLabelRow], list[ScenarioTradeRow], dict[str, Any]]:
    runtime_config = _short_enabled_config(config)
    supervisor = TridentSupervisor(
        config=runtime_config,
        profile=f"p106-regime-gate-{window.name}",
        mode="observation",
    )
    loader = SnapshotLoader()
    states = [
        ScenarioState(
            spec=spec,
            risk_gate=PodARiskGate(runtime_config),
            executor=PodAExecutor(runtime_config),
            report=PodABacktestReport(
                reference_equity_usd=runtime_config.trident.capital.reference_equity_usd,
            ),
        )
        for spec in gate_specs
    ]
    histories: dict[str, FeatureHistory] = defaultdict(FeatureHistory)
    last_snapshot_by_symbol: dict[str, SymbolMarketSnapshot] = {}
    last_timestamp: str | None = None
    first_timestamp: str | None = None
    last_label_sample: datetime | None = None
    btc_label_features: list[tuple[datetime, GateFeatures]] = []
    processed_records = 0
    skipped_records = 0
    started = time.perf_counter()

    for record in iter_window_records(loader, window):
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
        if first_timestamp is None:
            first_timestamp = isoformat(timestamp)
        last_timestamp = isoformat(timestamp)
        processed_records += 1
        date_key = timestamp.date().isoformat()
        previous_regime = supervisor.state.regime.value
        cluster_regime_snapshots = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        }
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(**record.regime_snapshot),
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        current_regime = supervisor.state.regime.value
        regime = _crypto_regime(record)
        snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
        snapshot_by_symbol = {snapshot.symbol.upper(): snapshot for snapshot in snapshots}
        btc_snapshot = snapshot_by_symbol.get("BTC")
        features_by_symbol: dict[str, GateFeatures] = {}
        if btc_snapshot is not None:
            btc_features = build_features(
                timestamp=timestamp,
                symbol="BTC",
                snapshot=btc_snapshot,
                btc_snapshot=btc_snapshot,
                histories=histories,
                regime=regime,
            )
            features_by_symbol["BTC"] = btc_features
            if (
                last_label_sample is None
                or timestamp - last_label_sample >= timedelta(minutes=sample_label_minutes)
            ):
                btc_label_features.append((timestamp, btc_features))
                last_label_sample = timestamp

        for snapshot in snapshots:
            if snapshot.symbol.upper() == "BTC":
                continue
            if btc_snapshot is None:
                continue
            features_by_symbol[snapshot.symbol.upper()] = build_features(
                timestamp=timestamp,
                symbol=snapshot.symbol.upper(),
                snapshot=snapshot,
                btc_snapshot=btc_snapshot,
                histories=histories,
                regime=regime,
            )

        trade_plans = supervisor.build_pod_a_trade_plans(snapshots, timestamp=record.timestamp)
        enriched_plans: list[TradePlan] = []
        for plan in trade_plans:
            features = features_by_symbol.get(plan.symbol.upper())
            details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
            if features is not None:
                details.update(_feature_details(features))
            enriched = replace(plan, setup_details=details)
            if apply_live_caps:
                enriched = apply_live_notional_cap(
                    enriched,
                    runtime_config.trident.execution.live_max_order_notional_usd,
                    max_leverage=runtime_config.pod_a.max_leverage,
                )
            enriched_plans.append(enriched)

        for state in states:
            _process_scenario_record(
                state=state,
                config=runtime_config,
                snapshots=snapshots,
                plans=enriched_plans,
                features_by_symbol=features_by_symbol,
                timestamp=record.timestamp,
                date_key=date_key,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )

        for snapshot in snapshots:
            histories[snapshot.symbol.upper()].append(timestamp, float(snapshot.price or 0.0))
        last_snapshot_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})

    final_snapshots = list(last_snapshot_by_symbol.values())
    for state in states:
        _finalize_scenario(
            state=state,
            config=runtime_config,
            snapshots=final_snapshots,
            timestamp=last_timestamp,
            close_regime=supervisor.state.regime.value,
        )
    supervisor.flush_compact_logs()
    labels = _build_regime_labels(window.name, histories.get("BTC", FeatureHistory()), btc_label_features)
    summaries = [_summarize_state(window.name, state) for state in states]
    trades = [
        trade
        for state in states
        for trade in _trade_rows(window.name, state)
    ]
    meta = {
        "window": window.name,
        "input_path": str(window.input_path),
        "start": isoformat(window.start),
        "end": isoformat(window.end),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "records_processed": processed_records,
        "records_skipped": skipped_records,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "apply_live_caps": apply_live_caps,
        "live_max_order_notional_usd": runtime_config.trident.execution.live_max_order_notional_usd,
    }
    return summaries, labels, trades, meta


def _process_scenario_record(
    *,
    state: ScenarioState,
    config: AppConfig,
    snapshots: list[SymbolMarketSnapshot],
    plans: list[TradePlan],
    features_by_symbol: dict[str, GateFeatures],
    timestamp: str | None,
    date_key: str,
    previous_regime: str,
    current_regime: str,
) -> None:
    report = state.report
    report.records_processed += 1
    report.add_record_date(date_key)
    report.add_record_regime(current_regime)
    if current_regime != previous_regime:
        report.add_regime_transition(
            date_key=date_key,
            previous_regime=previous_regime,
            new_regime=current_regime,
        )
    filtered_plans: list[TradePlan] = []
    for plan in plans:
        features = features_by_symbol.get(plan.symbol.upper())
        if _gate_allows(state.spec, plan, features):
            filtered_plans.append(plan)
            if features is not None:
                state.gate_decisions[f"{plan.side}:{features.regime_gate}"] += 1
            continue
        reason = _gate_reject_reason(state.spec, plan, features)
        state.gate_rejections[reason] += 1
        report.add_decision(date_key=date_key, setup=plan.setup, accepted=False, reason=reason)

    risk_decisions = state.risk_gate.evaluate_many(filtered_plans)
    execution = state.executor.process_record(
        snapshots=snapshots,
        risk_decisions=risk_decisions,
        signal_sides_by_symbol={decision.trade_plan.symbol: decision.trade_plan.side for decision in risk_decisions},
        timestamp=timestamp,
    )
    decisions_by_symbol = {decision.trade_plan.symbol: decision for decision in risk_decisions}
    for decision in risk_decisions:
        plan = decision.trade_plan
        report.add_signal(
            date_key=date_key,
            symbol=plan.symbol,
            side=plan.side,
            setup=plan.setup,
            regime=current_regime,
            confidence=plan.confidence,
            market_cluster=cluster_for_symbol(config, plan.symbol),
        )
        report.add_decision(
            date_key=date_key,
            setup=plan.setup,
            accepted=decision.accepted,
            reason=decision.reason,
        )
    report.add_execution_batch(
        opened_symbols=execution.opened_symbols,
        skipped_open_symbols=execution.skipped_open_symbols,
    )
    for symbol in execution.opened_symbols:
        decision = decisions_by_symbol.get(symbol)
        if decision is not None:
            report.add_opened_setup(decision.trade_plan.setup)
    for symbol in execution.skipped_open_symbols:
        decision = decisions_by_symbol.get(symbol)
        if decision is not None:
            report.add_skipped_open_setup(decision.trade_plan.setup)
    report.observe_open_exposure(list(state.executor.portfolio.open_positions.values()))
    for trade in execution.closed_trades:
        _record_closed_trade(
            state=state,
            config=config,
            trade=trade,
            date_key=(
                parse_timestamp(trade.closed_at.isoformat()).date().isoformat()
                if getattr(trade, "closed_at", None) is not None
                else date_key
            ),
            close_regime=current_regime,
        )


def _finalize_scenario(
    *,
    state: ScenarioState,
    config: AppConfig,
    snapshots: list[SymbolMarketSnapshot],
    timestamp: str | None,
    close_regime: str,
) -> None:
    final_trades, _ = state.executor.finalize(snapshots=snapshots, timestamp=timestamp)
    for trade in final_trades:
        _record_closed_trade(
            state=state,
            config=config,
            trade=trade,
            date_key=(
                parse_timestamp(trade.closed_at.isoformat()).date().isoformat()
                if getattr(trade, "closed_at", None) is not None
                else "finalize"
            ),
            close_regime=close_regime,
        )


def _record_closed_trade(
    *,
    state: ScenarioState,
    config: AppConfig,
    trade: Any,
    date_key: str,
    close_regime: str,
) -> None:
    state.risk_gate.record_closed_trade(
        symbol=trade.symbol,
        setup=getattr(trade, "setup", None),
        pnl_usd=trade.pnl_usd,
        date_key=date_key,
    )
    opened_at = getattr(trade, "opened_at", None)
    closed_at = getattr(trade, "closed_at", None)
    hold_hours = None
    if opened_at is not None and closed_at is not None:
        hold_hours = round((closed_at - opened_at).total_seconds() / 3600.0, 4)
    state.report.add_closed_trade(
        date_key=date_key,
        symbol=trade.symbol,
        side=trade.side,
        setup=getattr(trade, "setup", None),
        confidence=getattr(trade, "confidence", None),
        market_cluster=cluster_for_symbol(config, trade.symbol),
        close_regime=close_regime,
        entry_price=getattr(trade, "entry_price", None),
        exit_price=getattr(trade, "exit_price", None),
        target_notional_usd=getattr(trade, "target_notional_usd", None),
        margin_usd=getattr(trade, "margin_usd", None),
        effective_leverage=getattr(trade, "effective_leverage", None),
        risk_budget_usd=getattr(trade, "risk_budget_usd", None),
        expected_loss_usd=getattr(trade, "expected_loss_usd", None),
        invalidation_price=getattr(trade, "invalidation_price", None),
        stop_bps=getattr(trade, "stop_bps", None),
        time_stop_hours=getattr(trade, "time_stop_hours", None),
        take_profit_bps=getattr(trade, "take_profit_bps", None),
        break_even_trigger_bps=getattr(trade, "break_even_trigger_bps", None),
        trailing_activation_bps=getattr(trade, "trailing_activation_bps", None),
        trailing_distance_bps=getattr(trade, "trailing_distance_bps", None),
        pnl_usd=trade.pnl_usd,
        gross_pnl_usd=trade.gross_pnl_usd,
        fees_usd=trade.fees_usd,
        close_reason=trade.close_reason,
        hold_hours=hold_hours,
        opened_at=opened_at.isoformat() if opened_at else None,
        closed_at=closed_at.isoformat() if closed_at else None,
        setup_details=getattr(trade, "setup_details", None),
    )


def _gate_allows(spec: GateSpec, plan: TradePlan, features: GateFeatures | None) -> bool:
    if plan.side == "long":
        if not spec.allow_longs:
            return False
        if spec.long_regime_gates and features is not None:
            if features.regime_gate not in set(spec.long_regime_gates):
                return False
        if spec.block_longs_when_bear_ge is not None and features is not None:
            if features.bear_score >= spec.block_longs_when_bear_ge:
                return False
        if spec.long_min_bull > 0 or spec.long_max_bear < 99:
            if features is None:
                return False
            return features.bull_score >= spec.long_min_bull and features.bear_score <= spec.long_max_bear
        return True
    if plan.side == "short":
        if not spec.allow_shorts:
            return False
        if features is None:
            return False
        if spec.short_regime_gates and features.regime_gate not in set(spec.short_regime_gates):
            return False
        return features.bear_score >= spec.short_min_bear and features.bull_score <= spec.short_max_bull
    return False


def _gate_reject_reason(spec: GateSpec, plan: TradePlan, features: GateFeatures | None) -> str:
    if features is None:
        return "regime_gate_missing_features"
    if plan.side == "long" and not spec.allow_longs:
        return "regime_gate_long_disabled"
    if plan.side == "short" and not spec.allow_shorts:
        return "regime_gate_short_disabled"
    return f"regime_gate_{plan.side}_{features.regime_gate}_filtered"


def build_features(
    *,
    timestamp: datetime,
    symbol: str,
    snapshot: SymbolMarketSnapshot,
    btc_snapshot: SymbolMarketSnapshot,
    histories: dict[str, FeatureHistory],
    regime: dict[str, Any],
) -> GateFeatures:
    shadow = build_regime_shadow_features(
        timestamp=timestamp,
        symbol=symbol,
        snapshot=snapshot,
        btc_snapshot=btc_snapshot,
        histories=histories,
        regime=regime,
    )
    return GateFeatures(
        timestamp=shadow.timestamp,
        symbol=shadow.symbol,
        bull_score=shadow.bull_regime_score,
        bear_score=shadow.bear_regime_score,
        regime_gate=shadow.regime_gate_decision,
        btc_ret_60m_bps=shadow.btc_ret_60m_bps,
        btc_ret_240m_bps=shadow.btc_ret_240m_bps,
        btc_ret_1440m_bps=shadow.btc_ret_1440m_bps,
        symbol_ret_60m_bps=shadow.symbol_ret_60m_bps,
        symbol_ret_240m_bps=shadow.symbol_ret_240m_bps,
        btc_above_ema_slow=shadow.btc_above_ema_slow,
        btc_fast_above_slow=shadow.btc_fast_above_slow,
        symbol_above_ema_slow=shadow.symbol_above_ema_slow,
        symbol_fast_above_slow=shadow.symbol_fast_above_slow,
        structure_score=shadow.structure_score,
        breadth_pct=shadow.breadth_pct,
        alt_participation_pct=shadow.alt_participation_pct,
        leader_trend_score=shadow.leader_trend_score,
        coherence_score=shadow.coherence_score,
        dispersion_pct=shadow.dispersion_pct,
    )


def _feature_details(features: GateFeatures) -> dict[str, float | str | bool]:
    return {
        "bull_regime_score": features.bull_score,
        "bear_regime_score": features.bear_score,
        "regime_gate_decision": features.regime_gate,
        "btc_ret_60m_bps": features.btc_ret_60m_bps or 0.0,
        "btc_ret_240m_bps": features.btc_ret_240m_bps or 0.0,
        "btc_ret_1440m_bps": features.btc_ret_1440m_bps or 0.0,
        "symbol_ret_60m_bps": features.symbol_ret_60m_bps or 0.0,
        "symbol_ret_240m_bps": features.symbol_ret_240m_bps or 0.0,
        "btc_above_ema_slow": features.btc_above_ema_slow,
        "btc_fast_above_slow": features.btc_fast_above_slow,
        "breadth_pct": features.breadth_pct or 0.0,
        "leader_trend_score": features.leader_trend_score or 0.0,
    }


def _build_regime_labels(
    window: str,
    btc_history: FeatureHistory,
    sampled_features: list[tuple[datetime, GateFeatures]],
) -> list[RegimeLabelRow]:
    labels: list[RegimeLabelRow] = []
    timestamp_to_index = {timestamp: index for index, timestamp in enumerate(btc_history.timestamps)}
    for timestamp, features in sampled_features:
        index = timestamp_to_index.get(timestamp)
        if index is None:
            continue
        fwd_6h = btc_history.future_return_bps(index, 360)
        fwd_24h = btc_history.future_return_bps(index, 1440)
        labels.append(
            RegimeLabelRow(
                window=window,
                timestamp=features.timestamp,
                label_6h=_label_forward_return(fwd_6h, threshold_bps=100.0),
                label_24h=_label_forward_return(fwd_24h, threshold_bps=180.0),
                bull_score=features.bull_score,
                bear_score=features.bear_score,
                btc_ret_60m_bps=features.btc_ret_60m_bps,
                btc_ret_240m_bps=features.btc_ret_240m_bps,
                btc_ret_1440m_bps=features.btc_ret_1440m_bps,
                btc_fwd_360m_bps=_round_optional(fwd_6h),
                btc_fwd_1440m_bps=_round_optional(fwd_24h),
                breadth_pct=features.breadth_pct,
                leader_trend_score=features.leader_trend_score,
                structure_score=features.structure_score,
            )
        )
    return labels


def _label_forward_return(value: float | None, *, threshold_bps: float) -> str:
    if value is None:
        return "unknown"
    if value >= threshold_bps:
        return "bullish"
    if value <= -threshold_bps:
        return "bearish"
    return "neutral"


def _summarize_state(window: str, state: ScenarioState) -> ScenarioSummary:
    report = state.report
    pnl_by_side: dict[str, float] = {}
    trades_by_side: dict[str, int] = {}
    bull_scores: list[float] = []
    bear_scores: list[float] = []
    gains = 0.0
    losses = 0.0
    for row in report.closed_trade_log:
        side = str(row.get("side") or "unknown")
        pnl = _float(row.get("pnl_usd"))
        trades_by_side[side] = trades_by_side.get(side, 0) + 1
        pnl_by_side[side] = round(pnl_by_side.get(side, 0.0) + pnl, 6)
        if pnl > 0:
            gains += pnl
        elif pnl < 0:
            losses += -pnl
        details = row.get("setup_details")
        if isinstance(details, dict):
            bull_scores.append(_float(details.get("bull_regime_score")))
            bear_scores.append(_float(details.get("bear_regime_score")))
    closed_count = report.win_count + report.loss_count
    return ScenarioSummary(
        window=window,
        scenario=state.spec.name,
        description=state.spec.description,
        records_processed=report.records_processed,
        signal_count=report.signal_count,
        accepted_count=report.accepted_count,
        rejected_count=report.rejected_count,
        gate_rejections=dict(state.gate_rejections),
        gate_decisions=dict(state.gate_decisions),
        opened_count=report.opened_count,
        skipped_open_count=report.skipped_open_count,
        closed_trade_count=report.closed_trade_count,
        realized_pnl_usd=round(report.realized_pnl_usd, 6),
        fees_usd=round(report.fees_usd, 6),
        max_drawdown_usd=round(report.max_drawdown_usd, 6),
        win_rate=(report.win_count / closed_count if closed_count else None),
        profit_factor=(gains / losses if losses > 0 else None),
        trades_by_setup=dict(report.trades_by_setup),
        pnl_by_setup={key: round(value, 6) for key, value in report.pnl_by_setup.items()},
        trades_by_side=trades_by_side,
        pnl_by_side=pnl_by_side,
        close_reasons=dict(report.close_reasons),
        avg_bull_score=(
            round(sum(bull_scores) / len(bull_scores), 4) if bull_scores else None
        ),
        avg_bear_score=(
            round(sum(bear_scores) / len(bear_scores), 4) if bear_scores else None
        ),
    )


def _trade_rows(window: str, state: ScenarioState) -> list[ScenarioTradeRow]:
    rows: list[ScenarioTradeRow] = []
    for row in state.report.closed_trade_log:
        details = row.get("setup_details")
        details_dict = details if isinstance(details, dict) else {}
        rows.append(
            ScenarioTradeRow(
                window=window,
                scenario=state.spec.name,
                symbol=str(row.get("symbol") or ""),
                side=str(row.get("side") or ""),
                setup=str(row.get("setup") or "") or None,
                opened_at=str(row.get("opened_at") or "") or None,
                closed_at=str(row.get("closed_at") or "") or None,
                close_reason=str(row.get("close_reason") or ""),
                pnl_usd=round(_float(row.get("pnl_usd")), 6),
                fees_usd=round(_float(row.get("fees_usd")), 6),
                hold_hours=_round_optional(_maybe(row.get("hold_hours"))),
                confidence=_round_optional(_maybe(row.get("confidence"))),
                bull_score=_round_optional(_maybe(details_dict.get("bull_regime_score"))),
                bear_score=_round_optional(_maybe(details_dict.get("bear_regime_score"))),
                regime_gate=str(details_dict.get("regime_gate_decision") or ""),
                btc_ret_60m_bps=_round_optional(_maybe(details_dict.get("btc_ret_60m_bps"))),
                btc_ret_240m_bps=_round_optional(_maybe(details_dict.get("btc_ret_240m_bps"))),
                btc_ret_1440m_bps=_round_optional(_maybe(details_dict.get("btc_ret_1440m_bps"))),
                symbol_ret_60m_bps=_round_optional(_maybe(details_dict.get("symbol_ret_60m_bps"))),
                symbol_ret_240m_bps=_round_optional(_maybe(details_dict.get("symbol_ret_240m_bps"))),
            )
        )
    return rows


def _short_enabled_config(config: AppConfig) -> AppConfig:
    allowed = list(dict.fromkeys(list(config.pod_a.allowed_setups) + ["trend_pullback_short"]))
    disabled = [item for item in config.pod_a.disabled_setups if item != "trend_pullback_short"]
    return replace(
        config,
        pod_a=replace(config.pod_a, allowed_setups=allowed, disabled_setups=disabled),
    )


def iter_window_records(loader: SnapshotLoader, window: WindowSpec) -> Iterable[Any]:
    path = window.input_path
    if not path.is_dir():
        yield from loader.iter_merged_jsonl(path)
        return
    start_date = window.start.date() if window.start is not None else None
    end_date = window.end.date() if window.end is not None else None
    for file_path in sorted(path.glob("*.jsonl")):
        file_date = _date_from_stem(file_path.stem)
        if file_date is not None:
            if start_date is not None and file_date < start_date:
                continue
            if end_date is not None and file_date > end_date:
                continue
        yield from loader.iter_merged_jsonl(file_path)


def _date_from_stem(stem: str) -> date | None:
    try:
        return datetime.strptime(stem[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _crypto_regime(record: Any) -> dict[str, Any]:
    if isinstance(record.cluster_regime_snapshots, dict):
        crypto = record.cluster_regime_snapshots.get("crypto")
        if isinstance(crypto, dict):
            return dict(crypto)
    return dict(record.regime_snapshot or {})


def write_csv(path: Path, rows: Iterable[Any]) -> None:
    row_dicts = [asdict(row) for row in rows]
    if not row_dicts:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row_dicts[0]))
        writer.writeheader()
        writer.writerows(row_dicts)


def write_report(
    path: Path,
    *,
    generated_at: str,
    summaries: list[ScenarioSummary],
    labels: list[RegimeLabelRow],
    metas: list[dict[str, Any]],
) -> None:
    lines = [
        "# P1-06 Regime Gate Replay",
        "",
        f"- Generated at: `{generated_at}`",
        "- Status: `research_only_no_live_change`",
        "- Scope: Pod A gated replay, with live notional caps when enabled.",
        "",
        "## Windows",
        "",
    ]
    for meta in metas:
        lines.append(
            f"- `{meta['window']}`: `{meta['first_timestamp']}` -> `{meta['last_timestamp']}`, "
            f"records `{meta['records_processed']}`, runtime `{meta['runtime_seconds']}`s, "
            f"live caps `{meta['apply_live_caps']}`"
        )
    lines.extend(
        [
            "",
            "## Scenario Summary",
            "",
            "| Window | Scenario | Trades | PnL | Max DD | WR | PF | Signals | Gate rejects | Side PnL | Setup PnL |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|---|---|",
        ]
    )
    for row in summaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.window}`",
                    f"`{row.scenario}`",
                    str(row.closed_trade_count),
                    f"{row.realized_pnl_usd:.2f}",
                    f"{row.max_drawdown_usd:.2f}",
                    _fmt_pct(row.win_rate),
                    _fmt_float(row.profit_factor),
                    str(row.signal_count),
                    _fmt_dict(row.gate_rejections),
                    _fmt_dict(row.pnl_by_side),
                    _fmt_dict(row.pnl_by_setup),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Label Mix", ""])
    label_counts: dict[tuple[str, str, str], int] = Counter(
        (row.window, "6h", row.label_6h) for row in labels
    )
    label_counts.update(Counter((row.window, "24h", row.label_24h) for row in labels))
    lines.append("| Window | Horizon | Label | Count |")
    lines.append("|---|---|---|---:|")
    for (window, horizon, label), count in sorted(label_counts.items()):
        lines.append(f"| `{window}` | `{horizon}` | `{label}` | {count} |")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This is not a live change. Shorts remain research/shadow only.",
            "- The gate uses only pre-entry features; forward labels are used only for evaluation.",
            "- Pod C and full capital routing are not changed by this Pod A replay.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_windows(args: argparse.Namespace) -> list[WindowSpec]:
    if args.windows_json:
        payload = json.loads(Path(args.windows_json).read_text(encoding="utf-8"))
        return [
            WindowSpec(
                name=str(item["name"]),
                input_path=Path(item["input_path"]),
                start=parse_timestamp(item.get("start")),
                end=parse_timestamp(item.get("end")),
            )
            for item in payload
        ]
    return [
        WindowSpec(
            name="baseline_apr_may",
            input_path=Path(args.baseline_input),
            start=parse_timestamp(args.baseline_start),
            end=parse_timestamp(args.baseline_end),
        ),
        WindowSpec(
            name="live_post_baseline",
            input_path=Path(args.live_input),
            start=parse_timestamp(args.live_start),
            end=parse_timestamp(args.live_end),
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--live-input", default=DEFAULT_LIVE_INPUT)
    parser.add_argument("--baseline-start", default="2026-04-05T00:00:00Z")
    parser.add_argument("--baseline-end", default="2026-05-13T23:59:59Z")
    parser.add_argument("--live-start", default="2026-05-14T00:00:00Z")
    parser.add_argument("--live-end", default="2026-06-12T23:59:59Z")
    parser.add_argument("--windows-json", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-live-caps", action="store_true")
    parser.add_argument("--sample-label-minutes", type=int, default=15)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated_at = utc_stamp()
    output_dir = Path(args.output_dir) if args.output_dir else Path(
        f"server-data/replay_reports/p106_regime_gate_replay_{generated_at}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    gate_specs = default_gate_specs()
    all_summaries: list[ScenarioSummary] = []
    all_labels: list[RegimeLabelRow] = []
    all_trades: list[ScenarioTradeRow] = []
    metas: list[dict[str, Any]] = []
    for window in parse_windows(args):
        print(f"window={window.name} status=running input={window.input_path}", flush=True)
        summaries, labels, trades, meta = run_window(
            config=config,
            window=window,
            gate_specs=gate_specs,
            apply_live_caps=not args.no_live_caps,
            sample_label_minutes=args.sample_label_minutes,
        )
        all_summaries.extend(summaries)
        all_labels.extend(labels)
        all_trades.extend(trades)
        metas.append(meta)
        print(
            f"window={window.name} status=done records={meta['records_processed']} "
            f"seconds={meta['runtime_seconds']}",
            flush=True,
        )
    write_csv(output_dir / "scenario_summary.csv", all_summaries)
    write_csv(output_dir / "regime_labels.csv", all_labels)
    write_csv(output_dir / "closed_trades.csv", all_trades)
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "meta": metas,
        "scenario_summary": [asdict(row) for row in all_summaries],
        "gate_specs": [asdict(row) for row in gate_specs],
    }
    (output_dir / "p106_regime_gate_replay.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_report(
        output_dir / "p106_regime_gate_replay.md",
        generated_at=generated_at,
        summaries=all_summaries,
        labels=all_labels,
        metas=metas,
    )
    print(output_dir)


def _float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _maybe(value: object) -> float | None:
    parsed = _float(value, default=math.nan)
    if not math.isfinite(parsed):
        return None
    return parsed


def _round_optional(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def _fmt_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _fmt_dict(values: dict[str, object]) -> str:
    if not values:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in values.items())


if __name__ == "__main__":
    main()
