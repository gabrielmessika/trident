from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_report import PodABacktestReport
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.directional_executor import DirectionalExecutor
from app.hyperliquid.info_client import HyperliquidInfoClient
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.pod_b.hyperps import (
    HyperpReversionContext,
    HyperpReversionPlanner,
    HyperpReversionProfile,
    HyperpReversionService,
    HyperpRiskGate,
    HyperpThresholds,
    HyperpLifecyclePolicy,
    HyperpLifecycleState,
    HyperpUniverseRegistry,
)
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodAllocation,
    PodName,
    RegimeSnapshot,
    SignalPreview,
    SymbolAllocation,
    SymbolMarketSnapshot,
    symbol_market_snapshot_from_mapping,
)


DEFAULT_HYPERP_SYMBOLS = ("TAO", "XPL", "BIO", "PENGU")


@dataclass(slots=True)
class HyperpsPodBBacktestResult:
    pod: str
    slot: str
    profile: dict[str, object]
    symbols: list[str]
    universe: dict[str, object]
    thresholds: dict[str, dict[str, object]]
    input_path: str
    start_date: str | None
    end_date: str | None
    backtest: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class HyperpsSweepRow:
    idea: str
    family: str
    profile: dict[str, object]
    train_pnl_usd: float
    validation_pnl_usd: float
    full_pnl_usd: float
    train_trades: int
    validation_trades: int
    full_trades: int
    full_fees_usd: float
    full_max_drawdown_usd: float
    full_pnl_by_symbol: dict[str, float]
    full_trades_by_symbol: dict[str, int]


@dataclass(slots=True)
class HyperpsSweepResult:
    input_path: str
    config_path: str
    symbols: list[str]
    universe: dict[str, object]
    train_end_date: str
    validation_start_date: str
    rows: list[HyperpsSweepRow]
    selected_idea: str | None
    selected_full: dict[str, object] | None
    integrated_replacement: dict[str, object] | None = None
    markdown_path: str | None = None
    json_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "config_path": self.config_path,
            "symbols": self.symbols,
            "universe": self.universe,
            "train_end_date": self.train_end_date,
            "validation_start_date": self.validation_start_date,
            "rows": [asdict(row) for row in self.rows],
            "selected_idea": self.selected_idea,
            "selected_full": self.selected_full,
            "integrated_replacement": self.integrated_replacement,
            "markdown_path": self.markdown_path,
            "json_path": self.json_path,
        }


@dataclass(slots=True)
class FullBotHyperpsReplacementResult:
    input_path: str
    reserved_symbols: list[str]
    profile: dict[str, object]
    thresholds: dict[str, dict[str, object]]
    baseline_full_bot: dict[str, object]
    replacement_full_bot: dict[str, object]
    delta_total_pnl_usd: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class HyperpThresholdBuilder:
    def __init__(self) -> None:
        self.loader = SnapshotLoader()

    def build_jsonl(
        self,
        input_path: str | Path,
        *,
        symbols: Iterable[str],
        profile: HyperpReversionProfile,
        end_date: str | None = None,
    ) -> dict[str, HyperpThresholds]:
        symbol_set = {symbol.upper() for symbol in symbols}
        funding_positive: dict[str, list[float]] = defaultdict(list)
        funding_negative: dict[str, list[float]] = defaultdict(list)
        deviations: dict[str, list[float]] = defaultdict(list)
        ranges: dict[str, list[float]] = defaultdict(list)
        volume_ratios: dict[str, list[float]] = defaultdict(list)
        for record in self.loader.iter_merged_jsonl(input_path):
            if end_date and record.timestamp and record.timestamp[:10] > end_date:
                continue
            for item in record.symbols:
                symbol = str(item.get("symbol", "")).upper()
                if symbol not in symbol_set:
                    continue
                funding = _float(item.get("funding_rate"))
                if funding > 0.0:
                    funding_positive[symbol].append(funding)
                elif funding < 0.0:
                    funding_negative[symbol].append(funding)
                price = _float(item.get("price"))
                ema_fast = _float(item.get("ema_fast"))
                if price > 0.0 and ema_fast > 0.0:
                    deviations[symbol].append(abs((price / ema_fast - 1.0) * 10_000.0))
                ranges[symbol].append(max(_float(item.get("bucket_range_bps")), 0.0))
                volume_ratios[symbol].append(max(_float(item.get("volume_ratio")), 0.0))

        thresholds: dict[str, HyperpThresholds] = {}
        for symbol in sorted(symbol_set):
            pos = _percentile(
                funding_positive.get(symbol, []),
                profile.funding_percentile,
                default=profile.min_abs_funding_rate,
            )
            neg = _percentile(
                funding_negative.get(symbol, []),
                1.0 - profile.funding_percentile,
                default=-profile.min_abs_funding_rate,
            )
            thresholds[symbol] = HyperpThresholds(
                symbol=symbol,
                positive_funding_extreme=max(pos, profile.min_abs_funding_rate),
                negative_funding_extreme=min(neg, -profile.min_abs_funding_rate),
                abs_deviation_extreme_bps=max(
                    _percentile(
                        deviations.get(symbol, []),
                        profile.deviation_percentile,
                        default=profile.min_deviation_bps,
                    ),
                    profile.min_deviation_bps,
                ),
                event_range_bps=_percentile(ranges.get(symbol, []), 0.95, default=0.0),
                event_volume_ratio=_percentile(volume_ratios.get(symbol, []), 0.98, default=profile.max_event_volume_ratio),
            )
        return thresholds


class HyperpsPodBBacktestRunner:
    """Backtests Hyperps as a dedicated Pod B slot."""

    def __init__(
        self,
        config: AppConfig,
        *,
        profile: HyperpReversionProfile,
        symbols: Iterable[str] = DEFAULT_HYPERP_SYMBOLS,
        thresholds: dict[str, HyperpThresholds] | None = None,
        lifecycle_registry: HyperpUniverseRegistry | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.lifecycle_registry = lifecycle_registry
        requested_symbols = [symbol.upper() for symbol in symbols]
        if lifecycle_registry is not None:
            for symbol in lifecycle_registry.known_symbols():
                if symbol not in requested_symbols:
                    requested_symbols.append(symbol)
        self.symbols = requested_symbols
        self.symbol_set = set(self.symbols)
        self.thresholds = thresholds
        self.loader = SnapshotLoader()
        self.service = HyperpReversionService(profile)
        self.planner = HyperpReversionPlanner(config, profile)
        self.risk_gate = HyperpRiskGate(config, profile)
        self.executor = DirectionalExecutor(config)
        self._price_history: dict[str, deque[float]] = {
            symbol: deque(maxlen=32) for symbol in self.symbols
        }

    def run_jsonl(
        self,
        input_path: str | Path,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        dedupe_by_timestamp: bool = True,
    ) -> HyperpsPodBBacktestResult:
        thresholds = self.thresholds or HyperpThresholdBuilder().build_jsonl(
            input_path,
            symbols=self.symbols,
            profile=self.profile,
            end_date=start_date,
        )
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-hyperps-pod-b-backtest",
            mode="dry-run",
        )
        report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        seen_timestamps: set[str] = set()
        latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        last_timestamp: str | None = None

        for record in self.loader.iter_merged_jsonl(input_path):
            if _outside_date_window(record.timestamp, start_date=start_date, end_date=end_date):
                continue
            if dedupe_by_timestamp and record.timestamp:
                if record.timestamp in seen_timestamps:
                    continue
                seen_timestamps.add(record.timestamp)
            previous_regime = supervisor.state.regime.value
            _apply_regime(supervisor, record.regime_snapshot, record.cluster_regime_snapshots)
            current_regime = supervisor.state.regime.value
            self._add_regime_record(
                report=report,
                timestamp=record.timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )

            snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
            hyperp_snapshots = [
                snapshot for snapshot in snapshots if snapshot.symbol.upper() in self.symbol_set
            ]
            contexts = self._build_contexts(
                snapshots=hyperp_snapshots,
                regime=current_regime,
                thresholds=thresholds,
                timestamp=record.timestamp,
            )
            signals = self.service.evaluate_many(contexts)
            allocation = self._slot_allocation(current_regime, signals)
            plans = [
                plan
                for signal in signals
                if (plan := self.planner.build_trade_plan(signal, allocation)) is not None
            ]
            for plan in plans:
                plan.setup_details = {
                    **dict(plan.setup_details or {}),
                    "current_date_key": _date_key(record.timestamp, record.source_file),
                }
            decisions = self.risk_gate.evaluate_many(plans, self.executor.portfolio.open_positions)
            execution = self.executor.process_record(
                snapshots=hyperp_snapshots,
                risk_decisions=decisions,
                signal_sides_by_symbol={signal.symbol: signal.side for signal in signals},
                timestamp=record.timestamp,
                entry_allowed_symbols=self.symbol_set,
                managed_symbols=self.symbol_set,
            )
            self._record_tick(
                report=report,
                current_regime=current_regime,
                timestamp=record.timestamp,
                source_file=record.source_file,
                previews=signals,
                decisions=decisions,
                execution=execution,
            )
            latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in hyperp_snapshots})
            last_timestamp = record.timestamp

        self._finalize_report(report, list(latest_snapshots_by_symbol.values()), last_timestamp)
        return HyperpsPodBBacktestResult(
            pod="hyperps",
            slot="pod_b",
            profile=asdict(self.profile),
            symbols=self.symbols,
            universe=self._universe_summary(),
            thresholds={symbol: asdict(value) for symbol, value in sorted(thresholds.items())},
            input_path=str(input_path),
            start_date=start_date,
            end_date=end_date,
            backtest=report.to_dict(),
        )

    def _build_contexts(
        self,
        *,
        snapshots: list[SymbolMarketSnapshot],
        regime: str,
        thresholds: dict[str, HyperpThresholds],
        timestamp: str | None,
    ) -> list[HyperpReversionContext]:
        contexts: list[HyperpReversionContext] = []
        for snapshot in snapshots:
            symbol = snapshot.symbol.upper()
            threshold = thresholds.get(symbol)
            if threshold is None:
                continue
            lifecycle_state = self._lifecycle_state(symbol, timestamp)
            if not lifecycle_state.tradable:
                continue
            history = self._price_history.setdefault(symbol, deque(maxlen=32))
            previous_price = history[-1] if history else float(snapshot.price)
            history.append(float(snapshot.price))
            price_move_bps = 0.0
            if previous_price > 0.0 and snapshot.price > 0.0:
                price_move_bps = ((float(snapshot.price) / previous_price) - 1.0) * 10_000.0
            contexts.append(
                HyperpReversionContext(
                    snapshot=snapshot,
                    regime=regime,
                    thresholds=threshold,
                    rsi14=_rsi(list(history), 14),
                    price_move_bps=price_move_bps,
                    timestamp=timestamp,
                    lifecycle_phase=lifecycle_state.phase,
                    lifecycle_weight=lifecycle_state.weight,
                    lifecycle_days_since_active=lifecycle_state.days_since_active,
                    lifecycle_strictness=lifecycle_state.strictness_multiplier,
                )
            )
        return contexts

    def _lifecycle_state(self, symbol: str, timestamp: str | None) -> HyperpLifecycleState:
        if self.lifecycle_registry is None:
            return HyperpLifecyclePolicy().state_from_dates(
                symbol=symbol,
                as_of=_parse_datetime(timestamp),
                first_seen=_parse_datetime(timestamp),
                last_seen=_parse_datetime(timestamp),
                active_now=True,
            )
        return self.lifecycle_registry.state_for(symbol, timestamp)

    def _universe_summary(self) -> dict[str, object]:
        if self.lifecycle_registry is None:
            return {
                "mode": "static",
                "symbols": self.symbols,
            }
        return {
            "mode": "snapshot_lifecycle",
            "symbols": self.symbols,
            "snapshot_count": len(self.lifecycle_registry.snapshots),
            "policy": asdict(self.lifecycle_registry.policy),
        }

    def _slot_allocation(
        self,
        regime_name: str,
        signals: list[SignalPreview],
    ) -> PodAllocation:
        target_pct = max(_pod_b_target_pct(self.config, regime_name), 0.0)
        total_equity = max(float(self.config.trident.capital.reference_equity_usd), 1e-9)
        target_usd = round(target_pct * total_equity, 2)
        if not signals or target_usd <= 0.0:
            return PodAllocation(pod=PodName.POD_B, target_pct=target_pct, target_usd=target_usd)
        signal_symbols = list(dict.fromkeys(signal.symbol for signal in signals))
        max_symbol_usd = self.config.trident.capital.max_allocation_per_symbol_pct * total_equity
        base_per_symbol_usd = min(target_usd / len(signal_symbols), max_symbol_usd)
        allocations: list[SymbolAllocation] = []
        for symbol in signal_symbols:
            signal = next(item for item in signals if item.symbol == symbol)
            lifecycle_weight = _clamp01(
                _float(dict(signal.setup_details or {}).get("lifecycle_weight", 1.0))
            )
            symbol_usd = round(base_per_symbol_usd * lifecycle_weight, 2)
            if symbol_usd <= 0.0:
                continue
            allocations.append(
                SymbolAllocation(
                    symbol=symbol,
                    target_pct=round(symbol_usd / total_equity, 6),
                    target_usd=symbol_usd,
                )
            )
        allocated_usd = round(sum(item.target_usd for item in allocations), 2)
        return PodAllocation(
            pod=PodName.POD_B,
            target_pct=round(allocated_usd / total_equity, 6),
            target_usd=allocated_usd,
            symbols=allocations,
        )

    def _add_regime_record(
        self,
        *,
        report: PodABacktestReport,
        timestamp: str | None,
        source_file: str,
        previous_regime: str,
        current_regime: str,
    ) -> None:
        report.records_processed += 1
        date_key = _date_key(timestamp, source_file)
        report.add_record_date(date_key)
        if current_regime != previous_regime:
            report.add_regime_transition(
                date_key=date_key,
                previous_regime=previous_regime,
                new_regime=current_regime,
            )
        report.add_record_regime(current_regime)

    def _record_tick(
        self,
        *,
        report: PodABacktestReport,
        current_regime: str,
        timestamp: str | None,
        source_file: str,
        previews: list[SignalPreview],
        decisions: list[object],
        execution: object,
    ) -> None:
        date_key = _date_key(timestamp, source_file)
        decisions_by_symbol = {
            decision.trade_plan.symbol: decision for decision in decisions if hasattr(decision, "trade_plan")
        }
        for preview in previews:
            report.add_signal(
                date_key=date_key,
                symbol=preview.symbol,
                side=preview.side,
                setup=preview.setup,
                regime=current_regime,
                confidence=preview.confidence,
                market_cluster=cluster_for_symbol(self.config, preview.symbol),
            )
        for decision in decisions:
            report.add_decision(
                date_key=date_key,
                setup=decision.trade_plan.setup,
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
        report.observe_open_exposure(list(self.executor.portfolio.open_positions.values()))
        self._record_closed_trades(report, execution.closed_trades, current_regime, timestamp, source_file)

    def _finalize_report(
        self,
        report: PodABacktestReport,
        latest_snapshots: list[SymbolMarketSnapshot],
        last_timestamp: str | None,
    ) -> None:
        final_trades, _ = self.executor.finalize(
            snapshots=latest_snapshots,
            timestamp=last_timestamp,
        )
        self._record_closed_trades(report, final_trades, "finalize", last_timestamp, "finalize")

    def _record_closed_trades(
        self,
        report: PodABacktestReport,
        trades: list[object],
        current_regime: str,
        timestamp: str | None,
        source_file: str,
    ) -> None:
        for trade in trades:
            self.risk_gate.record_closed_trade(
                symbol=str(getattr(trade, "symbol", "")),
                pnl_usd=getattr(trade, "pnl_usd", None),
            )
            report.add_closed_trade(
                date_key=_date_key(
                    trade.closed_at.isoformat() if trade.closed_at is not None else timestamp,
                    source_file,
                ),
                symbol=trade.symbol,
                side=trade.side,
                setup=getattr(trade, "setup", None),
                confidence=getattr(trade, "confidence", None),
                market_cluster=cluster_for_symbol(self.config, trade.symbol),
                close_regime=current_regime,
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
                hold_hours=_hold_hours(trade),
                opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
                setup_details=getattr(trade, "setup_details", None),
            )


class FullBotHyperpsReplacementRunner(FullBotBacktestRunner):
    """Integrated replay: Pod A + Pod C + Hyperps in the Pod B capital slot."""

    def __init__(
        self,
        config: AppConfig,
        *,
        profile: HyperpReversionProfile,
        symbols: Iterable[str] = DEFAULT_HYPERP_SYMBOLS,
        thresholds: dict[str, HyperpThresholds],
        lifecycle_registry: HyperpUniverseRegistry | None = None,
    ) -> None:
        requested_symbols = [symbol.upper() for symbol in symbols]
        if lifecycle_registry is not None:
            for symbol in lifecycle_registry.known_symbols():
                if symbol not in requested_symbols:
                    requested_symbols.append(symbol)
        self.hyperp_symbols = requested_symbols
        self.hyperp_symbol_set = set(self.hyperp_symbols)
        blocked_config = _config_with_reserved_hyperps(config, self.hyperp_symbols)
        super().__init__(blocked_config, force_enable_all_pods=False)
        self.slot_source_config = config
        self.hyperp_profile = profile
        self.hyperp_thresholds = thresholds
        self.hyperp_lifecycle_registry = lifecycle_registry
        self.hyperp_service = HyperpReversionService(profile)
        self.hyperp_planner = HyperpReversionPlanner(config, profile)
        self.hyperp_risk_gate = HyperpRiskGate(config, profile)
        self.hyperp_executor = DirectionalExecutor(config)
        self._hyperp_price_history: dict[str, deque[float]] = {
            symbol: deque(maxlen=32) for symbol in self.hyperp_symbols
        }

    def run_jsonl(
        self,
        input_path: str | Path,
        *,
        dedupe_by_timestamp: bool = True,
    ) -> dict[str, object]:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-full-bot-hyperps-replacement",
            mode="dry-run",
        )
        pod_a_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        pod_c_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        hyperps_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        seen_timestamps: set[str] = set()
        latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        latest_hyperp_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        dates_covered: set[str] = set()
        records_processed = 0
        duplicate_timestamps_skipped = 0

        for record in self.loader.iter_merged_jsonl(input_path):
            timestamp = record.timestamp
            if dedupe_by_timestamp and timestamp:
                if timestamp in seen_timestamps:
                    duplicate_timestamps_skipped += 1
                    continue
                seen_timestamps.add(timestamp)
            if first_timestamp is None:
                first_timestamp = timestamp
            last_timestamp = timestamp
            if timestamp:
                dates_covered.add(timestamp[:10])

            snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
            latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
            previous_regime = supervisor.state.regime.value
            _apply_regime(supervisor, record.regime_snapshot, record.cluster_regime_snapshots)
            current_regime = supervisor.state.regime.value

            self._process_pod_a(
                supervisor=supervisor,
                report=pod_a_report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
            self._process_pod_c(
                supervisor=supervisor,
                report=pod_c_report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
            self._process_hyperps_slot(
                report=hyperps_report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
            latest_hyperp_snapshots_by_symbol.update(
                {
                    snapshot.symbol: snapshot
                    for snapshot in snapshots
                    if snapshot.symbol.upper() in self.hyperp_symbol_set
                }
            )
            records_processed += 1

        supervisor.flush_compact_logs()
        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_a_report,
            executor=self.pod_a_executor,
            latest_snapshots=list(latest_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
            closed_trade_recorder=self._record_pod_a_closed_trade,
        )
        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_c_report,
            executor=self.pod_c_executor,
            latest_snapshots=list(latest_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
        )
        self._finalize_directional_report(
            supervisor=supervisor,
            report=hyperps_report,
            executor=self.hyperp_executor,
            latest_snapshots=list(latest_hyperp_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
            closed_trade_recorder=self._record_hyperp_closed_trade,
        )
        pod_a = pod_a_report.to_dict()
        pod_c = pod_c_report.to_dict()
        pod_hyperps = hyperps_report.to_dict()
        return {
            "input_path": str(input_path),
            "dedupe_by_timestamp": dedupe_by_timestamp,
            "records_processed": records_processed,
            "duplicate_timestamps_skipped": duplicate_timestamps_skipped,
            "first_timestamp": first_timestamp,
            "last_timestamp": last_timestamp,
            "dates_covered": sorted(dates_covered),
            "pod_a": pod_a,
            "pod_hyperps": pod_hyperps,
            "pod_c": pod_c,
            "total_realized_pnl_usd": round(
                float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
                + float(pod_hyperps.get("realized_pnl_usd", 0.0) or 0.0)
                + float(pod_c.get("realized_pnl_usd", 0.0) or 0.0),
                4,
            ),
            "directional_fees_usd": round(
                float(pod_a.get("fees_usd", 0.0) or 0.0)
                + float(pod_hyperps.get("fees_usd", 0.0) or 0.0)
                + float(pod_c.get("fees_usd", 0.0) or 0.0),
                6,
            ),
            "total_activity_count": (
                int(pod_a.get("closed_trade_count", 0) or 0)
                + int(pod_hyperps.get("closed_trade_count", 0) or 0)
                + int(pod_c.get("closed_trade_count", 0) or 0)
            ),
            "notes": [
                "Pod A is replayed with Hyperps reserved out of its symbol pool.",
                "The Hyperps sleeve uses the Pod B capital slot allocations.",
                "Legacy Pod B breakout is disabled in the replacement run.",
            ],
        }

    def _process_hyperps_slot(
        self,
        *,
        report: PodABacktestReport,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
        source_file: str,
        previous_regime: str,
        current_regime: str,
    ) -> None:
        self._add_regime_record(
            report=report,
            timestamp=timestamp,
            source_file=source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        hyperp_snapshots = [
            snapshot for snapshot in snapshots if snapshot.symbol.upper() in self.hyperp_symbol_set
        ]
        contexts = self._hyperp_contexts(hyperp_snapshots, current_regime, timestamp)
        signals = self.hyperp_service.evaluate_many(contexts)
        allocation = self._hyperp_slot_allocation(current_regime, signals)
        trade_plans = [
            plan
            for signal in signals
            if (plan := self.hyperp_planner.build_trade_plan(signal, allocation)) is not None
        ]
        date_key = self._date_key(timestamp, source_file)
        for plan in trade_plans:
            plan.setup_details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
        risk_decisions = self.hyperp_risk_gate.evaluate_many(
            trade_plans,
            self.hyperp_executor.portfolio.open_positions,
        )
        execution = self.hyperp_executor.process_record(
            snapshots=hyperp_snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={signal.symbol: signal.side for signal in signals},
            timestamp=timestamp,
            entry_allowed_symbols=self.hyperp_symbol_set,
            managed_symbols=self.hyperp_symbol_set,
        )
        self._record_directional_tick(
            report=report,
            config=self.slot_source_config,
            current_regime=current_regime,
            timestamp=timestamp,
            source_file=source_file,
            previews=signals,
            risk_decisions=risk_decisions,
            execution=execution,
            executor=self.hyperp_executor,
            closed_trade_recorder=self._record_hyperp_closed_trade,
        )

    def _hyperp_contexts(
        self,
        snapshots: list[SymbolMarketSnapshot],
        regime: str,
        timestamp: str | None,
    ) -> list[HyperpReversionContext]:
        contexts: list[HyperpReversionContext] = []
        for snapshot in snapshots:
            symbol = snapshot.symbol.upper()
            threshold = self.hyperp_thresholds.get(symbol)
            if threshold is None:
                continue
            lifecycle_state = self._hyperp_lifecycle_state(symbol, timestamp)
            if not lifecycle_state.tradable:
                continue
            history = self._hyperp_price_history.setdefault(symbol, deque(maxlen=32))
            previous_price = history[-1] if history else float(snapshot.price)
            history.append(float(snapshot.price))
            price_move_bps = 0.0
            if previous_price > 0.0 and snapshot.price > 0.0:
                price_move_bps = ((float(snapshot.price) / previous_price) - 1.0) * 10_000.0
            contexts.append(
                HyperpReversionContext(
                    snapshot=snapshot,
                    regime=regime,
                    thresholds=threshold,
                    rsi14=_rsi(list(history), 14),
                    price_move_bps=price_move_bps,
                    timestamp=timestamp,
                    lifecycle_phase=lifecycle_state.phase,
                    lifecycle_weight=lifecycle_state.weight,
                    lifecycle_days_since_active=lifecycle_state.days_since_active,
                    lifecycle_strictness=lifecycle_state.strictness_multiplier,
                )
            )
        return contexts

    def _hyperp_lifecycle_state(self, symbol: str, timestamp: str | None) -> HyperpLifecycleState:
        if self.hyperp_lifecycle_registry is None:
            return HyperpLifecyclePolicy().state_from_dates(
                symbol=symbol,
                as_of=_parse_datetime(timestamp),
                first_seen=_parse_datetime(timestamp),
                last_seen=_parse_datetime(timestamp),
                active_now=True,
            )
        return self.hyperp_lifecycle_registry.state_for(symbol, timestamp)

    def _hyperp_slot_allocation(
        self,
        regime_name: str,
        signals: list[SignalPreview],
    ) -> PodAllocation:
        target_pct = max(_pod_b_target_pct(self.slot_source_config, regime_name), 0.0)
        total_equity = max(float(self.slot_source_config.trident.capital.reference_equity_usd), 1e-9)
        target_usd = round(target_pct * total_equity, 2)
        if not signals or target_usd <= 0.0:
            return PodAllocation(pod=PodName.POD_B, target_pct=target_pct, target_usd=target_usd)
        signal_symbols = list(dict.fromkeys(signal.symbol for signal in signals))
        max_symbol_usd = self.slot_source_config.trident.capital.max_allocation_per_symbol_pct * total_equity
        base_per_symbol_usd = min(target_usd / len(signal_symbols), max_symbol_usd)
        allocations: list[SymbolAllocation] = []
        for symbol in signal_symbols:
            signal = next(item for item in signals if item.symbol == symbol)
            lifecycle_weight = _clamp01(
                _float(dict(signal.setup_details or {}).get("lifecycle_weight", 1.0))
            )
            symbol_usd = round(base_per_symbol_usd * lifecycle_weight, 2)
            if symbol_usd <= 0.0:
                continue
            allocations.append(
                SymbolAllocation(
                    symbol=symbol,
                    target_pct=round(symbol_usd / total_equity, 6),
                    target_usd=symbol_usd,
                )
            )
        allocated_usd = round(sum(item.target_usd for item in allocations), 2)
        return PodAllocation(
            pod=PodName.POD_B,
            target_pct=round(allocated_usd / total_equity, 6),
            target_usd=allocated_usd,
            symbols=allocations,
        )

    def _record_hyperp_closed_trade(self, trade: object) -> None:
        self.hyperp_risk_gate.record_closed_trade(
            symbol=str(getattr(trade, "symbol", "")),
            pnl_usd=getattr(trade, "pnl_usd", None),
        )


def candidate_profiles() -> list[tuple[str, str, HyperpReversionProfile]]:
    base = HyperpReversionProfile()
    return [
        ("draft_strict", "single", base),
        (
            "relaxed_percentiles",
            "single",
            replace(
                base,
                name="relaxed_percentiles",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                long_rsi_max=45.0,
                short_rsi_min=55.0,
            ),
        ),
        (
            "no_rsi_keep_rejection",
            "single",
            replace(base, name="no_rsi_keep_rejection", require_rsi=False, min_score=4),
        ),
        (
            "rsi_no_rejection",
            "single",
            replace(base, name="rsi_no_rejection", require_rejection=False, min_score=4),
        ),
        (
            "flow_confirmed",
            "single",
            replace(
                base,
                name="flow_confirmed",
                require_rsi=False,
                require_flow_confirmation=True,
                min_score=4,
            ),
        ),
        (
            "long_capitulation_only",
            "single",
            replace(base, name="long_capitulation_only", allow_shorts=False, long_rsi_max=46.0),
        ),
        (
            "short_euphoria_only",
            "single",
            replace(base, name="short_euphoria_only", allow_longs=False, short_rsi_min=56.0),
        ),
        (
            "no_event_safety",
            "single",
            replace(base, name="no_event_safety", block_event_spikes=False),
        ),
        (
            "combo_relaxed_quick_tp",
            "combined",
            replace(
                base,
                name="combo_relaxed_quick_tp",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                long_rsi_max=45.0,
                short_rsi_min=55.0,
                take_profit_extension_fraction=0.42,
                take_profit_stop_ratio=0.70,
                time_stop_hours=2,
                reentry_cooldown_minutes=240,
            ),
        ),
        (
            "combo_flow_quick_lowrisk",
            "combined",
            replace(
                base,
                name="combo_flow_quick_lowrisk",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                require_rsi=False,
                require_flow_confirmation=True,
                risk_per_trade_pct=0.0025,
                size_multiplier=0.50,
                take_profit_extension_fraction=0.42,
                take_profit_stop_ratio=0.70,
                time_stop_hours=2,
            ),
        ),
        (
            "combo_relaxed_wider_stop",
            "combined",
            replace(
                base,
                name="combo_relaxed_wider_stop",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                long_rsi_max=45.0,
                short_rsi_min=55.0,
                stop_vol_multiplier=1.80,
                stop_ceiling_bps=240.0,
                time_stop_hours=6,
            ),
        ),
        (
            "flow_veto_top5_strict",
            "single",
            replace(
                base,
                name="flow_veto_top5_strict",
                trigger_mode="funding_veto_flow",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                max_spread_bps=3.0,
                min_bucket_notional_usd=50.0,
                min_interest_score=0.55,
                top_n=5,
                block_event_spikes=False,
                take_profit_extension_fraction=0.45,
                take_profit_stop_ratio=0.75,
                time_stop_hours=1,
            ),
        ),
        (
            "flow_veto_top5_relaxed_spread",
            "single",
            replace(
                base,
                name="flow_veto_top5_relaxed_spread",
                trigger_mode="funding_veto_flow",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                max_spread_bps=5.0,
                min_bucket_notional_usd=50.0,
                min_interest_score=0.52,
                top_n=5,
                block_event_spikes=False,
                take_profit_extension_fraction=0.45,
                take_profit_stop_ratio=0.75,
                time_stop_hours=1,
            ),
        ),
        (
            "combo_flow_veto_lowrisk",
            "combined",
            replace(
                base,
                name="combo_flow_veto_lowrisk",
                trigger_mode="funding_veto_flow",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                max_spread_bps=5.0,
                min_bucket_notional_usd=50.0,
                min_interest_score=0.52,
                top_n=5,
                short_rsi_min=45.0,
                block_event_spikes=False,
                risk_per_trade_pct=0.0025,
                size_multiplier=0.50,
                take_profit_extension_fraction=0.40,
                take_profit_stop_ratio=0.70,
                time_stop_hours=1,
                reentry_cooldown_minutes=120,
            ),
        ),
        (
            "fade_flow_exhaustion",
            "single",
            replace(
                base,
                name="fade_flow_exhaustion",
                trigger_mode="flow_exhaustion_fade",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                max_spread_bps=5.0,
                min_bucket_notional_usd=50.0,
                min_interest_score=0.52,
                top_n=5,
                block_event_spikes=False,
                risk_per_trade_pct=0.0025,
                size_multiplier=0.50,
                take_profit_extension_fraction=0.45,
                take_profit_stop_ratio=0.70,
                time_stop_hours=1,
                reentry_cooldown_minutes=120,
            ),
        ),
        (
            "combo_fade_flow_wider",
            "combined",
            replace(
                base,
                name="combo_fade_flow_wider",
                trigger_mode="flow_exhaustion_fade",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=30.0,
                max_spread_bps=6.0,
                min_bucket_notional_usd=50.0,
                min_interest_score=0.50,
                top_n=5,
                short_rsi_min=45.0,
                block_event_spikes=False,
                risk_per_trade_pct=0.0025,
                size_multiplier=0.50,
                stop_vol_multiplier=1.45,
                take_profit_extension_fraction=0.45,
                take_profit_stop_ratio=0.75,
                time_stop_hours=2,
                reentry_cooldown_minutes=120,
            ),
        ),
        (
            "combo_fade_rsi45_standard_size",
            "combined",
            replace(
                base,
                name="combo_fade_rsi45_standard_size",
                trigger_mode="flow_exhaustion_fade",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                max_spread_bps=5.0,
                min_bucket_notional_usd=50.0,
                min_interest_score=0.52,
                top_n=5,
                short_rsi_min=45.0,
                block_event_spikes=False,
                risk_per_trade_pct=0.0035,
                size_multiplier=0.60,
                take_profit_extension_fraction=0.45,
                take_profit_stop_ratio=0.70,
                time_stop_hours=1,
                reentry_cooldown_minutes=120,
            ),
        ),
        (
            "combo_fade_rsi50_lowrisk",
            "combined",
            replace(
                base,
                name="combo_fade_rsi50_lowrisk",
                trigger_mode="flow_exhaustion_fade",
                funding_percentile=0.85,
                deviation_percentile=0.70,
                min_abs_funding_rate=0.000013,
                min_deviation_bps=35.0,
                max_spread_bps=5.0,
                min_bucket_notional_usd=50.0,
                min_interest_score=0.52,
                top_n=5,
                short_rsi_min=50.0,
                block_event_spikes=False,
                risk_per_trade_pct=0.0025,
                size_multiplier=0.50,
                take_profit_extension_fraction=0.45,
                take_profit_stop_ratio=0.70,
                time_stop_hours=1,
                reentry_cooldown_minutes=120,
            ),
        ),
    ]


def run_sweep(
    *,
    input_path: str | Path,
    config_path: str,
    symbols: list[str],
    train_end_date: str,
    validation_start_date: str,
    lifecycle_registry: HyperpUniverseRegistry | None = None,
) -> HyperpsSweepResult:
    config = load_config(config_path)
    effective_symbols = list(dict.fromkeys(symbol.upper() for symbol in symbols))
    if lifecycle_registry is not None:
        for symbol in lifecycle_registry.known_symbols():
            if symbol not in effective_symbols:
                effective_symbols.append(symbol)
    rows: list[HyperpsSweepRow] = []
    selected_full: dict[str, object] | None = None
    selected_idea: str | None = None
    selected_profile: HyperpReversionProfile | None = None
    selected_thresholds: dict[str, HyperpThresholds] | None = None
    builder = HyperpThresholdBuilder()

    for idea, family, profile in candidate_profiles():
        thresholds = builder.build_jsonl(
            input_path,
            symbols=effective_symbols,
            profile=profile,
            end_date=train_end_date,
        )
        train = HyperpsPodBBacktestRunner(
            config,
            profile=profile,
            symbols=effective_symbols,
            thresholds=thresholds,
            lifecycle_registry=lifecycle_registry,
        ).run_jsonl(input_path, end_date=train_end_date).backtest
        validation = HyperpsPodBBacktestRunner(
            config,
            profile=profile,
            symbols=effective_symbols,
            thresholds=thresholds,
            lifecycle_registry=lifecycle_registry,
        ).run_jsonl(input_path, start_date=validation_start_date).backtest
        full = HyperpsPodBBacktestRunner(
            config,
            profile=profile,
            symbols=effective_symbols,
            thresholds=thresholds,
            lifecycle_registry=lifecycle_registry,
        ).run_jsonl(input_path).backtest
        row = HyperpsSweepRow(
            idea=idea,
            family=family,
            profile=asdict(profile),
            train_pnl_usd=float(train.get("realized_pnl_usd", 0.0) or 0.0),
            validation_pnl_usd=float(validation.get("realized_pnl_usd", 0.0) or 0.0),
            full_pnl_usd=float(full.get("realized_pnl_usd", 0.0) or 0.0),
            train_trades=int(train.get("closed_trade_count", 0) or 0),
            validation_trades=int(validation.get("closed_trade_count", 0) or 0),
            full_trades=int(full.get("closed_trade_count", 0) or 0),
            full_fees_usd=float(full.get("fees_usd", 0.0) or 0.0),
            full_max_drawdown_usd=float(full.get("max_drawdown_usd", 0.0) or 0.0),
            full_pnl_by_symbol={
                str(symbol): float(value or 0.0)
                for symbol, value in (full.get("pnl_by_symbol", {}) or {}).items()
            },
            full_trades_by_symbol={
                str(symbol): int(value or 0)
                for symbol, value in (full.get("trades_by_symbol", {}) or {}).items()
            },
        )
        rows.append(row)
        if row.validation_pnl_usd > 0 and row.full_pnl_usd > 0:
            if selected_full is None or row.validation_pnl_usd > float(selected_full.get("validation_pnl_usd", -1e9)):
                selected_idea = idea
                selected_profile = profile
                selected_thresholds = thresholds
                selected_full = {
                    "idea": idea,
                    "validation_pnl_usd": row.validation_pnl_usd,
                    "full_pnl_usd": row.full_pnl_usd,
                    "full_trades": row.full_trades,
                    "full_pnl_by_symbol": row.full_pnl_by_symbol,
                }

    integrated: dict[str, object] | None = None
    if selected_profile is not None and selected_thresholds is not None:
        baseline = FullBotBacktestRunner(config, force_enable_all_pods=False).run_jsonl(input_path).to_dict()
        replacement = FullBotHyperpsReplacementRunner(
            config,
            profile=selected_profile,
            symbols=effective_symbols,
            thresholds=selected_thresholds,
            lifecycle_registry=lifecycle_registry,
        ).run_jsonl(input_path)
        integrated = FullBotHyperpsReplacementResult(
            input_path=str(input_path),
            reserved_symbols=effective_symbols,
            profile=asdict(selected_profile),
            thresholds={symbol: asdict(value) for symbol, value in sorted(selected_thresholds.items())},
            baseline_full_bot=baseline,
            replacement_full_bot=replacement,
            delta_total_pnl_usd=round(
                float(replacement.get("total_realized_pnl_usd", 0.0) or 0.0)
                - float(baseline.get("total_realized_pnl_usd", 0.0) or 0.0),
                4,
            ),
        ).to_dict()

    return HyperpsSweepResult(
        input_path=str(input_path),
        config_path=config_path,
        symbols=effective_symbols,
        universe=_universe_summary(effective_symbols, lifecycle_registry),
        train_end_date=train_end_date,
        validation_start_date=validation_start_date,
        rows=rows,
        selected_idea=selected_idea,
        selected_full=selected_full,
        integrated_replacement=integrated,
    )


def render_markdown(result: HyperpsSweepResult) -> str:
    rows = sorted(result.rows, key=lambda row: row.validation_pnl_usd, reverse=True)
    lines = [
        "# Hyperps Pod B Research",
        "",
        f"- Input: `{result.input_path}`",
        f"- Config: `{result.config_path}`",
        f"- Symbols: `{', '.join(result.symbols)}`",
        f"- Universe mode: `{result.universe.get('mode', 'static')}`",
        f"- Train: through `{result.train_end_date}`",
        f"- Validation: from `{result.validation_start_date}`",
        "",
        "## Critique du draft",
        "",
        "- Les bonnes idees du draft sont funding extreme + distance EMA + liquidite + taille reduite.",
        "- La limite principale est que les snapshots disponibles n'ont pas de vraie bougie OHLC ni d'open interest fiable partout; le rejet est donc approxime via variation 15m, flow et imbalance.",
        "- Le take-profit partiel n'existe pas dans l'executeur directionnel actuel; le replay l'approxime par TP rapide, break-even et trailing stop.",
        "- Les seuils fixes seraient fragiles: les tests ci-dessous calibrent funding/deviation par coin sur le train puis valident sur le holdout.",
        "",
        "## Resultats des idees",
        "",
        "| Idee | Famille | Train PnL | Validation PnL | Full PnL | Full trades | Fees | Max DD | PnL par coin |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        pnl_by_symbol = ", ".join(
            f"{symbol}:{pnl:.2f}" for symbol, pnl in sorted(row.full_pnl_by_symbol.items())
        ) or "-"
        lines.append(
            f"| {row.idea} | {row.family} | {row.train_pnl_usd:.2f} | "
            f"{row.validation_pnl_usd:.2f} | {row.full_pnl_usd:.2f} | "
            f"{row.full_trades} | {row.full_fees_usd:.2f} | "
            f"{row.full_max_drawdown_usd:.2f} | {pnl_by_symbol} |"
        )
    lines.extend(["", "## Selection", ""])
    if result.selected_full:
        lines.append(
            f"- Selection: `{result.selected_idea}` avec validation "
            f"{float(result.selected_full.get('validation_pnl_usd', 0.0)):.2f}$ "
            f"et full {float(result.selected_full.get('full_pnl_usd', 0.0)):.2f}$."
        )
    else:
        lines.append("- Aucune idee n'a passe le filtre validation > 0 et full > 0.")
    if result.integrated_replacement:
        baseline = result.integrated_replacement["baseline_full_bot"]
        replacement = result.integrated_replacement["replacement_full_bot"]
        lines.extend(
            [
                "",
                "## Backtest global avec Pod B Hyperps",
                "",
                "| Scenario | Total PnL | Fees | Trades |",
                "|---|---:|---:|---:|",
                f"| Baseline | {float(baseline.get('total_realized_pnl_usd', 0.0) or 0.0):.2f} | {float(baseline.get('directional_fees_usd', 0.0) or 0.0):.2f} | {int(baseline.get('total_activity_count', 0) or 0)} |",
                f"| Pod B Hyperps | {float(replacement.get('total_realized_pnl_usd', 0.0) or 0.0):.2f} | {float(replacement.get('directional_fees_usd', 0.0) or 0.0):.2f} | {int(replacement.get('total_activity_count', 0) or 0)} |",
                f"| Delta | {float(result.integrated_replacement.get('delta_total_pnl_usd', 0.0) or 0.0):.2f} | | |",
                "",
                "### Pod B Hyperps seul dans le replay global",
                "",
                f"- PnL: {float(replacement.get('pod_hyperps', {}).get('realized_pnl_usd', 0.0) or 0.0):.2f}$",
                f"- Trades: {int(replacement.get('pod_hyperps', {}).get('closed_trade_count', 0) or 0)}",
                f"- Par coin: `{replacement.get('pod_hyperps', {}).get('pnl_by_symbol', {})}`",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research and replay a Hyperps-only Pod B sleeve.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", default=",".join(DEFAULT_HYPERP_SYMBOLS), help="Comma-separated symbols, or 'auto' with --hyperp-snapshots.")
    parser.add_argument("--hyperp-snapshots", help="JSONL snapshots of the active Hyperps universe.")
    parser.add_argument("--refresh-hyperp-snapshot", action="store_true", help="Fetch the live HL Hyperps list and append it to --hyperp-snapshots before running.")
    parser.add_argument("--lifecycle-half-life-days", type=float, default=30.0)
    parser.add_argument("--lifecycle-cooling-days", type=int, default=30)
    parser.add_argument("--lifecycle-retired-after-days", type=int, default=120)
    parser.add_argument("--lifecycle-min-trade-weight", type=float, default=0.15)
    parser.add_argument("--train-end-date", default="2026-04-12")
    parser.add_argument("--validation-start-date", default="2026-04-13")
    parser.add_argument("--json-output")
    parser.add_argument("--md-output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    policy = HyperpLifecyclePolicy(
        half_life_days=args.lifecycle_half_life_days,
        cooling_off_days=args.lifecycle_cooling_days,
        retired_after_days=args.lifecycle_retired_after_days,
        min_trade_weight=args.lifecycle_min_trade_weight,
    )
    lifecycle_registry: HyperpUniverseRegistry | None = None
    if args.hyperp_snapshots:
        lifecycle_registry = HyperpUniverseRegistry.from_jsonl(args.hyperp_snapshots, policy=policy)
        if args.refresh_hyperp_snapshot:
            snapshot = lifecycle_registry.fetch_snapshot(HyperliquidInfoClient(config.hyperliquid))
            lifecycle_registry.append_jsonl(args.hyperp_snapshots, snapshot)
    elif args.refresh_hyperp_snapshot:
        lifecycle_registry = HyperpUniverseRegistry(policy=policy)
        snapshot = lifecycle_registry.fetch_snapshot(HyperliquidInfoClient(config.hyperliquid))
        lifecycle_registry.snapshots.append(snapshot)
        lifecycle_registry.snapshots.sort(key=lambda item: item.as_datetime)
    if args.symbols.strip().lower() == "auto":
        symbols = lifecycle_registry.known_symbols() if lifecycle_registry is not None else list(DEFAULT_HYPERP_SYMBOLS)
    else:
        symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    result = run_sweep(
        input_path=args.input,
        config_path=args.config,
        symbols=symbols,
        train_end_date=args.train_end_date,
        validation_start_date=args.validation_start_date,
        lifecycle_registry=lifecycle_registry,
    )
    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        result.json_path = str(json_path)
        json_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    if args.md_output:
        md_path = Path(args.md_output)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        result.markdown_path = str(md_path)
        md_path.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps(result.to_dict(), indent=2))


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _clamp01(value: float) -> float:
    return max(0.0, min(value, 1.0))


def _percentile(values: list[float], pct: float, *, default: float) -> float:
    clean = sorted(float(value) for value in values if value == value)
    if not clean:
        return default
    pct = min(max(pct, 0.0), 1.0)
    index = int(round((len(clean) - 1) * pct))
    return clean[index]


def _rsi(prices: list[float], period: int) -> float | None:
    if len(prices) <= period:
        return None
    window = prices[-(period + 1) :]
    gains = 0.0
    losses = 0.0
    for previous, current in zip(window, window[1:]):
        delta = current - previous
        if delta >= 0.0:
            gains += delta
        else:
            losses -= delta
    if gains <= 0.0 and losses <= 0.0:
        return 50.0
    if losses <= 0.0:
        return 100.0
    rs = gains / losses
    return round(100.0 - (100.0 / (1.0 + rs)), 4)


def _apply_regime(
    supervisor: TridentSupervisor,
    regime_snapshot: dict[str, object],
    cluster_regime_snapshots: dict[str, dict[str, object]] | None,
) -> None:
    cluster_snapshots = {
        cluster: RegimeSnapshot(**snapshot)
        for cluster, snapshot in (cluster_regime_snapshots or {}).items()
        if isinstance(snapshot, dict)
    }
    supervisor.apply_regime_snapshot(
        RegimeSnapshot(**regime_snapshot),
        cluster_regime_snapshots=cluster_snapshots,
    )


def _outside_date_window(
    timestamp: str | None,
    *,
    start_date: str | None,
    end_date: str | None,
) -> bool:
    if timestamp is None:
        return False
    date_key = timestamp[:10]
    return bool((start_date and date_key < start_date) or (end_date and date_key > end_date))


def _date_key(timestamp: str | None, fallback_source_file: str) -> str:
    if timestamp:
        return timestamp[:10]
    if fallback_source_file.endswith(".jsonl"):
        return fallback_source_file.removesuffix(".jsonl")
    return fallback_source_file


def _parse_datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return datetime.now(timezone.utc)
    if len(text) == 10:
        text = f"{text}T00:00:00Z"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hold_hours(trade: object) -> float | None:
    opened_at = getattr(trade, "opened_at", None)
    closed_at = getattr(trade, "closed_at", None)
    if opened_at is None or closed_at is None:
        return None
    return round((closed_at - opened_at).total_seconds() / 3600.0, 4)


def _pod_b_target_pct(config: AppConfig, regime_name: str) -> float:
    allocations = config.trident.allocations
    if regime_name == "TrendExpansion":
        return float(allocations.trend_expansion.pod_b)
    if regime_name == "RangeAuction":
        return float(allocations.range_auction.pod_b)
    if regime_name == "PanicSqueeze":
        return float(allocations.panic_squeeze.pod_b)
    return float(allocations.dead_zone.pod_b)


def _universe_summary(
    symbols: list[str],
    lifecycle_registry: HyperpUniverseRegistry | None,
) -> dict[str, object]:
    if lifecycle_registry is None:
        return {
            "mode": "static",
            "symbols": symbols,
        }
    return {
        "mode": "snapshot_lifecycle",
        "symbols": symbols,
        "snapshot_count": len(lifecycle_registry.snapshots),
        "policy": asdict(lifecycle_registry.policy),
    }


def _config_with_reserved_hyperps(config: AppConfig, symbols: list[str]) -> AppConfig:
    blocked = {str(symbol).upper() for symbol in config.pod_a.blocked_symbols}
    merged_blocked = list(config.pod_a.blocked_symbols)
    for symbol in symbols:
        if symbol not in blocked:
            blocked.add(symbol)
            merged_blocked.append(symbol)
    return replace(
        config,
        pod_a=replace(config.pod_a, blocked_symbols=merged_blocked),
        pod_b=replace(config.pod_b, enabled=False),
    )


if __name__ == "__main__":
    main()
