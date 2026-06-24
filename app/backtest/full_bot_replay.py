from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.backtest.external_reference_policy import ExternalReferenceDecisionPolicy
from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.backtest.routing_replay import RoutingReplayRunner
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.live_cap import apply_live_notional_cap
from app.execution.directional_executor import DirectionalExecutor, ExecutionBatch
from app.risk.pod_a_gate import PodARiskGate
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.pod_b import (
    BreakoutContext,
    BreakoutPlanner,
    BreakoutService,
    PodBRiskGate,
    ReplayFeatureEnricher,
)
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodAllocation,
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SignalPreview,
    SymbolAllocation,
    SymbolMarketSnapshot,
    TradePlan,
    symbol_market_snapshot_from_mapping,
)


@dataclass(slots=True)
class FullBotBacktestResult:
    input_path: str
    dedupe_by_timestamp: bool
    records_processed: int
    duplicate_timestamps_skipped: int
    first_timestamp: str | None
    last_timestamp: str | None
    dates_covered: list[str]
    pod_a: dict[str, object]
    pod_b: dict[str, object]
    pod_c: dict[str, object]
    routing: dict[str, object]
    total_realized_pnl_usd: float
    directional_fees_usd: float
    total_activity_count: int
    notes: list[str]
    experiments: dict[str, object] = field(default_factory=dict)
    report_path: str | None = None
    summary_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FullBotBacktestRunner:
    """Runs Pod A, Pod B, and Pod C together on one snapshot stream."""

    def __init__(
        self,
        config: AppConfig,
        *,
        force_enable_all_pods: bool = True,
        external_reference_policy: ExternalReferenceDecisionPolicy | None = None,
        apply_live_notional_caps: bool = False,
        enable_loss_reaction_trades: bool = False,
        loss_reaction_direction: str = "opposite",
        loss_reaction_parent_close_reasons: set[str] | None = None,
        loss_reaction_allowed_pods: set[str] | None = None,
        loss_reaction_validation: str = "none",
        loss_reaction_max_parent_hold_hours: float | None = None,
        loss_reaction_size_multiplier: float = 1.0,
        loss_reaction_ignore_entry_guards: bool = False,
    ) -> None:
        self.config = self._runtime_config(config, force_enable_all_pods=force_enable_all_pods)
        self.external_reference_policy = external_reference_policy
        self.apply_live_notional_caps = bool(apply_live_notional_caps)
        self.enable_loss_reaction_trades = bool(enable_loss_reaction_trades)
        self.loss_reaction_direction = self._normalize_loss_reaction_direction(
            loss_reaction_direction
        )
        self.loss_reaction_parent_close_reasons = {
            item.strip()
            for item in (loss_reaction_parent_close_reasons or set())
            if item.strip()
        }
        self.loss_reaction_allowed_pods = {
            item.strip()
            for item in (loss_reaction_allowed_pods or set())
            if item.strip()
        }
        self.loss_reaction_validation = self._normalize_loss_reaction_validation(
            loss_reaction_validation
        )
        self.loss_reaction_max_parent_hold_hours = (
            float(loss_reaction_max_parent_hold_hours)
            if loss_reaction_max_parent_hold_hours is not None
            else None
        )
        self.loss_reaction_size_multiplier = max(float(loss_reaction_size_multiplier), 0.0)
        self.loss_reaction_ignore_entry_guards = bool(loss_reaction_ignore_entry_guards)
        self.loader = SnapshotLoader()
        self.pod_a_risk_gate = PodARiskGate(self.config)
        self.pod_c_risk_gate = PodCRiskGate(self.config)
        self.pod_a_executor = PodAExecutor(self.config)
        self.pod_c_executor = DirectionalExecutor(self.config)
        self.pod_b_service = BreakoutService(self.config)
        self.pod_b_planner = BreakoutPlanner(self.config)
        self.pod_b_risk_gate = PodBRiskGate(self.config)
        self.pod_b_executor = DirectionalExecutor(self.config)
        self.pod_b_replay_enricher = ReplayFeatureEnricher()

    def _runtime_config(self, config: AppConfig, *, force_enable_all_pods: bool) -> AppConfig:
        if not force_enable_all_pods:
            return config
        return replace(
            config,
            pod_a=replace(config.pod_a, enabled=True),
            pod_c=replace(config.pod_c, enabled=True),
        )

    def _normalize_loss_reaction_direction(self, value: str) -> str:
        normalized = str(value or "opposite").strip().lower()
        if normalized in {"opposite", "same"}:
            return normalized
        raise ValueError("loss_reaction_direction must be 'opposite' or 'same'")

    def _normalize_loss_reaction_validation(self, value: str) -> str:
        normalized = str(value or "none").strip().lower()
        allowed = {
            "none",
            "trend",
            "orderflow",
            "trend_orderflow",
            "micro_momentum",
        }
        if normalized in allowed:
            return normalized
        raise ValueError(
            "loss_reaction_validation must be one of: "
            + ", ".join(sorted(allowed))
        )

    def _loss_reaction_policy_summary(self) -> dict[str, object]:
        return {
            "direction": self.loss_reaction_direction,
            "parent_close_reasons": sorted(self.loss_reaction_parent_close_reasons),
            "allowed_pods": sorted(self.loss_reaction_allowed_pods),
            "validation": self.loss_reaction_validation,
            "max_parent_hold_hours": self.loss_reaction_max_parent_hold_hours,
            "size_multiplier": self.loss_reaction_size_multiplier,
            "ignore_entry_guards": self.loss_reaction_ignore_entry_guards,
        }

    def run_jsonl(
        self,
        input_path: str | Path,
        *,
        dedupe_by_timestamp: bool = True,
        report_output: str | Path | None = None,
        summary_output: str | Path | None = None,
        comparison_output: str | Path | None = None,
    ) -> FullBotBacktestResult:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-full-bot-backtest",
            mode="dry-run",
        )
        pod_a_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        pod_c_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        pod_b_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        seen_timestamps: set[str] = set()
        latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
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
            previous_snapshots_by_symbol = dict(latest_snapshots_by_symbol)
            latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
            if record.capture_reason == "maintenance_refresh":
                self._process_maintenance_record(
                    supervisor=supervisor,
                    pod_a_report=pod_a_report,
                    pod_b_report=pod_b_report,
                    pod_c_report=pod_c_report,
                    snapshots=snapshots,
                    timestamp=timestamp,
                    source_file=record.source_file,
                    stream_source=record.stream_source,
                )
                continue
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
            self._process_pod_b(
                supervisor=supervisor,
                report=pod_b_report,
                snapshots=snapshots,
                previous_snapshots_by_symbol=previous_snapshots_by_symbol,
                timestamp=timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
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
            report=pod_b_report,
            executor=self.pod_b_executor,
            latest_snapshots=list(latest_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
            closed_trade_recorder=self._record_pod_b_closed_trade,
        )

        routing = RoutingReplayRunner(self.config).run_jsonl(
            input_path=input_path,
            dedupe_by_timestamp=dedupe_by_timestamp,
        ).to_dict()
        pod_a = pod_a_report.to_dict()
        pod_b = pod_b_report.to_dict()
        pod_c = pod_c_report.to_dict()
        experiments = self._experiment_summary(
            pod_a=pod_a,
            pod_b=pod_b,
            pod_c=pod_c,
        )
        pod_b_realized = float(pod_b.get("realized_pnl_usd", 0.0))
        pod_b_fees = float(pod_b.get("fees_usd", 0.0))
        pod_b_activity = int(pod_b.get("closed_trade_count", 0))
        result = FullBotBacktestResult(
            input_path=str(input_path),
            dedupe_by_timestamp=dedupe_by_timestamp,
            records_processed=records_processed,
            duplicate_timestamps_skipped=duplicate_timestamps_skipped,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            dates_covered=sorted(dates_covered),
            pod_a=pod_a,
            pod_b=pod_b,
            pod_c=pod_c,
            routing=routing,
            total_realized_pnl_usd=round(
                float(pod_a.get("realized_pnl_usd", 0.0))
                + pod_b_realized
                + float(pod_c.get("realized_pnl_usd", 0.0)),
                4,
            ),
            directional_fees_usd=round(
                float(pod_a.get("fees_usd", 0.0))
                + pod_b_fees
                + float(pod_c.get("fees_usd", 0.0)),
                6,
            ),
            total_activity_count=(
                int(pod_a.get("closed_trade_count", 0))
                + pod_b_activity
                + int(pod_c.get("closed_trade_count", 0))
            ),
            notes=[
                "directional_fees_usd couvre Pod A, Pod B et Pod C.",
                "total_activity_count additionne les trades clotures des trois pods directionnels.",
                (
                    "loss_reaction_trades est actif en replay research-only."
                    if self.enable_loss_reaction_trades
                    else "loss_reaction_trades est inactif."
                ),
            ],
            experiments=experiments,
            report_path=str(report_output) if report_output is not None else None,
            summary_path=str(summary_output) if summary_output is not None else None,
        )
        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        if summary_output is not None:
            summary_path = Path(summary_output)
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(self._render_summary(result), encoding="utf-8")
        if comparison_output is not None:
            comparison_path = Path(comparison_output)
            comparison_path.parent.mkdir(parents=True, exist_ok=True)
            with comparison_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self._comparison_entry(result)) + "\n")
        return result

    def _process_pod_a(
        self,
        *,
        supervisor: TridentSupervisor,
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
        previews = supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp)
        trade_plans = supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp)
        date_key = self._date_key(timestamp, source_file)
        for plan in trade_plans:
            plan.setup_details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
        trade_plans = self._apply_live_notional_caps(PodName.POD_A, trade_plans)
        if self.external_reference_policy is not None:
            self.external_reference_policy.adjust_plans(PodName.POD_A, trade_plans)
        risk_decisions = self.pod_a_risk_gate.evaluate_many(trade_plans)
        if self.external_reference_policy is not None:
            risk_decisions = self.external_reference_policy.apply_decisions(
                PodName.POD_A,
                risk_decisions,
            )
        opening_symbols = supervisor.opening_symbols_for(PodName.POD_A)
        managed_symbols = supervisor.managed_symbols_for(
            PodName.POD_A,
            active_symbols=self.pod_a_executor.portfolio.open_positions.keys(),
        )
        execution, report_risk_decisions = self._execute_directional_record(
            pod_name=PodName.POD_A,
            executor=self.pod_a_executor,
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            entry_allowed_symbols=opening_symbols,
            managed_symbols=managed_symbols,
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=report_risk_decisions,
            execution=execution,
            executor=self.pod_a_executor,
            closed_trade_recorder=self._record_pod_a_closed_trade,
        )

    def _process_pod_c(
        self,
        *,
        supervisor: TridentSupervisor,
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
        previews = supervisor.preview_pod_c_signals(snapshots)
        trade_plans = supervisor.build_pod_c_trade_plans(snapshots)
        date_key = self._date_key(timestamp, source_file)
        for plan in trade_plans:
            plan.setup_details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
        trade_plans = self._apply_live_notional_caps(PodName.POD_C, trade_plans)
        if self.external_reference_policy is not None:
            self.external_reference_policy.adjust_plans(PodName.POD_C, trade_plans)
        risk_decisions = self.pod_c_risk_gate.evaluate_many(trade_plans)
        if self.external_reference_policy is not None:
            risk_decisions = self.external_reference_policy.apply_decisions(
                PodName.POD_C,
                risk_decisions,
            )
        opening_symbols = supervisor.opening_symbols_for(PodName.POD_C)
        managed_symbols = supervisor.managed_symbols_for(
            PodName.POD_C,
            active_symbols=self.pod_c_executor.portfolio.open_positions.keys(),
        )
        execution, report_risk_decisions = self._execute_directional_record(
            pod_name=PodName.POD_C,
            executor=self.pod_c_executor,
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            entry_allowed_symbols=opening_symbols,
            managed_symbols=managed_symbols,
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=report_risk_decisions,
            execution=execution,
            executor=self.pod_c_executor,
        )

    def _apply_live_notional_caps(
        self,
        pod_name: PodName,
        trade_plans: list[object],
    ) -> list[object]:
        if not self.apply_live_notional_caps:
            return trade_plans
        if pod_name == PodName.POD_A:
            leverage_policy = LeveragePolicy(self.config.pod_a)
        elif pod_name == PodName.POD_C:
            leverage_policy = LeveragePolicy(self.config.pod_c)
        else:
            return trade_plans
        return [
            apply_live_notional_cap(
                plan,  # type: ignore[arg-type]
                self.config.trident.execution.live_max_order_notional_usd,
                max_leverage=leverage_policy.max_allowed(getattr(plan, "symbol", None)),
            )
            for plan in trade_plans
        ]

    def _process_pod_b(
        self,
        *,
        supervisor: TridentSupervisor,
        report: PodABacktestReport,
        snapshots: list[SymbolMarketSnapshot],
        previous_snapshots_by_symbol: dict[str, SymbolMarketSnapshot],
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
        snapshots = self.pod_b_replay_enricher.enrich_many(snapshots)
        supervisor.refresh_symbol_routing(snapshots)
        opening_symbols = supervisor.opening_symbols_for(PodName.POD_B)
        managed_symbols = supervisor.managed_symbols_for(
            PodName.POD_B,
            active_symbols=self.pod_b_executor.portfolio.open_positions.keys(),
        )
        contexts = [
            BreakoutContext(
                symbol=snapshot.symbol,
                regime=supervisor.state.regime.value,
                price=snapshot.price,
                ema_fast=snapshot.ema_fast,
                ema_slow=snapshot.ema_slow,
                price_move_bps=(
                    round(
                        (snapshot.price - previous_snapshot.price)
                        / previous_snapshot.price
                        * 10_000.0,
                        4,
                    )
                    if (
                        (previous_snapshot := previous_snapshots_by_symbol.get(snapshot.symbol)) is not None
                        and previous_snapshot.price > 0
                        and snapshot.price > 0
                    )
                    else 0.0
                ),
                vwap_distance_bps=snapshot.vwap_distance_bps,
                structure_score=snapshot.structure_score,
                funding_rate=snapshot.funding_rate,
                spread_bps=snapshot.spread_bps,
                btc_aligned=snapshot.btc_aligned,
                market_cluster=snapshot.market_cluster,
                cluster_leader=snapshot.cluster_leader,
                book_imbalance=snapshot.book_imbalance,
                trade_flow_bias=snapshot.trade_flow_bias,
                bucket_trade_count=snapshot.bucket_trade_count,
                bucket_notional_usd=(
                    snapshot.bucket_notional_usd
                    if snapshot.bucket_notional_usd > 0
                    else snapshot.bucket_volume * snapshot.price
                ),
                bucket_range_bps=snapshot.bucket_range_bps,
                delta_spread_bps=snapshot.delta_spread_bps,
                delta_book_imbalance=snapshot.delta_book_imbalance,
                delta_trade_flow_bias=snapshot.delta_trade_flow_bias,
                volume_ratio=snapshot.volume_ratio,
                trade_count_ratio=snapshot.trade_count_ratio,
                realized_vol_short_bps=snapshot.realized_vol_short_bps,
                realized_vol_long_bps=snapshot.realized_vol_long_bps,
                compression_score=snapshot.compression_score,
                best_bid_size=snapshot.best_bid_size,
                best_ask_size=snapshot.best_ask_size,
                bid_depth_10bps=snapshot.bid_depth_10bps,
                ask_depth_10bps=snapshot.ask_depth_10bps,
                bid_depth_velocity=snapshot.bid_depth_velocity,
                ask_depth_velocity=snapshot.ask_depth_velocity,
                best_bid_size_velocity=snapshot.best_bid_size_velocity,
                best_ask_size_velocity=snapshot.best_ask_size_velocity,
                microprice_dislocation_bps=snapshot.microprice_dislocation_bps,
            )
            for snapshot in snapshots
            if snapshot.symbol in opening_symbols
        ]
        signals = self.pod_b_service.evaluate_many(contexts)
        previews = [
            SignalPreview(
                symbol=signal.symbol,
                side=signal.side,
                setup=signal.setup,
                confidence=signal.confidence,
            )
            for signal in signals
        ]
        pod_allocation = self._pod_b_planning_allocation(
            supervisor.capital_plan.pod_allocations[PodName.POD_B],
            signals,
        )
        trade_plans = [
            plan
            for signal in signals
            if (plan := self.pod_b_planner.build_trade_plan(signal, pod_allocation)) is not None
        ]
        current_open_positions = list(self.pod_b_executor.portfolio.open_positions.values())
        risk_decisions = self.pod_b_risk_gate.evaluate_many(
            trade_plans,
            current_open_expected_loss_usd=sum(
                max(float(getattr(position, "expected_loss_usd", 0.0)), 0.0)
                for position in current_open_positions
            ),
            current_open_position_count=len(current_open_positions),
        )
        execution, report_risk_decisions = self._execute_directional_record(
            pod_name=PodName.POD_B,
            executor=self.pod_b_executor,
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            entry_allowed_symbols=opening_symbols,
            managed_symbols=managed_symbols,
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=report_risk_decisions,
            execution=execution,
            executor=self.pod_b_executor,
            closed_trade_recorder=self._record_pod_b_closed_trade,
        )

    def _process_maintenance_record(
        self,
        *,
        supervisor: TridentSupervisor,
        pod_a_report: PodABacktestReport,
        pod_b_report: PodABacktestReport,
        pod_c_report: PodABacktestReport,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
        source_file: str,
        stream_source: str | None,
    ) -> None:
        for pod_name, report, executor, closed_trade_recorder in self._maintenance_targets(
            stream_source=stream_source,
            pod_a_report=pod_a_report,
            pod_b_report=pod_b_report,
            pod_c_report=pod_c_report,
        ):
            managed_symbols = supervisor.managed_symbols_for(
                pod_name,
                active_symbols=executor.portfolio.open_positions.keys(),
            )
            execution = executor.process_record(
                snapshots=snapshots,
                risk_decisions=[],
                signal_sides_by_symbol={},
                timestamp=timestamp,
                entry_allowed_symbols=supervisor.opening_symbols_for(pod_name),
                managed_symbols=managed_symbols,
            )
            self._record_directional_tick(
                report=report,
                config=self.config,
                current_regime=supervisor.state.regime.value,
                timestamp=timestamp,
                source_file=source_file,
                previews=[],
                risk_decisions=[],
                execution=execution,
                executor=executor,
                closed_trade_recorder=closed_trade_recorder,
            )

    def _maintenance_targets(
        self,
        *,
        stream_source: str | None,
        pod_a_report: PodABacktestReport,
        pod_b_report: PodABacktestReport,
        pod_c_report: PodABacktestReport,
    ) -> list[
        tuple[
            PodName,
            PodABacktestReport,
            DirectionalExecutor,
            Callable[[object], None] | None,
        ]
    ]:
        source = str(stream_source or "").strip().lower()
        targets = {
            PodName.POD_A: (
                PodName.POD_A,
                pod_a_report,
                self.pod_a_executor,
                self._record_pod_a_closed_trade,
            ),
            PodName.POD_B: (
                PodName.POD_B,
                pod_b_report,
                self.pod_b_executor,
                self._record_pod_b_closed_trade,
            ),
            PodName.POD_C: (
                PodName.POD_C,
                pod_c_report,
                self.pod_c_executor,
                None,
            ),
        }
        if source.startswith("pod_a"):
            return [targets[PodName.POD_A]]
        if source.startswith("pod_b"):
            return [targets[PodName.POD_B]]
        if source.startswith("pod_c"):
            return [targets[PodName.POD_C]]
        return list(targets.values())

    def _execute_directional_record(
        self,
        *,
        pod_name: PodName,
        executor: DirectionalExecutor,
        snapshots: list[SymbolMarketSnapshot],
        risk_decisions: list[RiskDecision],
        signal_sides_by_symbol: dict[str, str],
        timestamp: str | None,
        entry_allowed_symbols: set[str] | None,
        managed_symbols: set[str] | None,
    ) -> tuple[ExecutionBatch, list[RiskDecision]]:
        if not self.enable_loss_reaction_trades:
            return (
                executor.process_record(
                    snapshots=snapshots,
                    risk_decisions=risk_decisions,
                    signal_sides_by_symbol=signal_sides_by_symbol,
                    timestamp=timestamp,
                    entry_allowed_symbols=entry_allowed_symbols,
                    managed_symbols=managed_symbols,
                ),
                risk_decisions,
            )

        close_execution = executor.process_record(
            snapshots=snapshots,
            risk_decisions=[],
            signal_sides_by_symbol=signal_sides_by_symbol,
            timestamp=timestamp,
            entry_allowed_symbols=entry_allowed_symbols,
            managed_symbols=managed_symbols,
        )
        reaction_decisions = self._loss_reaction_decisions_from_closed_trades(
            pod_name=pod_name,
            closed_trades=close_execution.closed_trades,
            snapshots=snapshots,
        )
        reaction_symbols = {decision.trade_plan.symbol for decision in reaction_decisions}
        normal_decisions = [
            decision
            for decision in risk_decisions
            if decision.trade_plan.symbol not in reaction_symbols
        ]
        open_entry_allowed_symbols = entry_allowed_symbols
        open_managed_symbols = managed_symbols
        if self.loss_reaction_ignore_entry_guards and reaction_symbols:
            open_entry_allowed_symbols = (
                None
                if entry_allowed_symbols is None
                else {*entry_allowed_symbols, *reaction_symbols}
            )
            open_managed_symbols = (
                None
                if managed_symbols is None
                else {*managed_symbols, *reaction_symbols}
            )
        open_execution = executor.process_record(
            snapshots=snapshots,
            risk_decisions=[*reaction_decisions, *normal_decisions],
            signal_sides_by_symbol={},
            timestamp=timestamp,
            entry_allowed_symbols=open_entry_allowed_symbols,
            managed_symbols=open_managed_symbols,
        )
        return (
            self._combine_execution_batches(close_execution, open_execution),
            [*reaction_decisions, *normal_decisions],
        )

    def _loss_reaction_decisions_from_closed_trades(
        self,
        *,
        pod_name: PodName,
        closed_trades: list[object],
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[RiskDecision]:
        decisions: list[RiskDecision] = []
        seen_symbols: set[str] = set()
        snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
        for trade in closed_trades:
            plan = self._loss_reaction_plan_from_trade(
                pod_name=pod_name,
                trade=trade,
                snapshot=snapshot_by_symbol.get(str(getattr(trade, "symbol", ""))),
            )
            if plan is None or plan.symbol in seen_symbols:
                continue
            decisions.append(RiskDecision(accepted=True, reason="loss_reaction", trade_plan=plan))
            seen_symbols.add(plan.symbol)
        return decisions

    def _loss_reaction_plan_from_trade(
        self,
        *,
        pod_name: PodName,
        trade: object,
        snapshot: SymbolMarketSnapshot | None = None,
    ) -> TradePlan | None:
        if self.loss_reaction_allowed_pods and pod_name.value not in self.loss_reaction_allowed_pods:
            return None
        pnl_usd = float(getattr(trade, "pnl_usd", 0.0) or 0.0)
        if pnl_usd >= 0.0:
            return None
        setup_details = getattr(trade, "setup_details", None)
        if isinstance(setup_details, dict) and bool(setup_details.get("loss_reaction_trade")):
            return None
        if str(getattr(trade, "setup", "")) == "loss_reaction":
            return None
        parent_close_reason = str(getattr(trade, "close_reason", "")).strip()
        if (
            self.loss_reaction_parent_close_reasons
            and parent_close_reason not in self.loss_reaction_parent_close_reasons
        ):
            return None
        parent_hold_hours = self._hold_hours(trade)
        if (
            self.loss_reaction_max_parent_hold_hours is not None
            and (
                parent_hold_hours is None
                or parent_hold_hours > self.loss_reaction_max_parent_hold_hours
            )
        ):
            return None
        side = str(getattr(trade, "side", ""))
        if self.loss_reaction_direction == "same":
            reaction_side = side
        elif side == "long":
            reaction_side = "short"
        elif side == "short":
            reaction_side = "long"
        else:
            return None
        symbol = str(getattr(trade, "symbol", "")).strip()
        target_notional_usd = float(getattr(trade, "target_notional_usd", 0.0) or 0.0)
        stop_bps = float(getattr(trade, "stop_bps", 0.0) or 0.0)
        if not symbol or target_notional_usd <= 0.0 or stop_bps <= 0.0:
            return None
        validation_details = self._loss_reaction_validation_details(
            side=reaction_side,
            snapshot=snapshot,
        )
        if validation_details is None:
            return None
        parent_details = setup_details if isinstance(setup_details, dict) else {}
        opened_at = getattr(trade, "opened_at", None)
        closed_at = getattr(trade, "closed_at", None)
        reaction_details: dict[str, float | str | bool] = {
            "loss_reaction_trade": True,
            "loss_reaction_pod": pod_name.value,
            "loss_reaction_direction": self.loss_reaction_direction,
            "loss_reaction_validation": self.loss_reaction_validation,
            "loss_reaction_parent_setup": str(getattr(trade, "setup", "")),
            "loss_reaction_parent_side": side,
            "loss_reaction_parent_close_reason": parent_close_reason,
            "loss_reaction_parent_pnl_usd": round(pnl_usd, 6),
            "loss_reaction_parent_hold_hours": (
                round(parent_hold_hours, 6) if parent_hold_hours is not None else -1.0
            ),
            "loss_reaction_parent_opened_at": (
                opened_at.isoformat() if opened_at is not None else ""
            ),
            "loss_reaction_parent_closed_at": (
                closed_at.isoformat() if closed_at is not None else ""
            ),
        }
        reaction_details.update(validation_details)
        for key in ("market_cluster", "cluster_leader", "current_date_key"):
            value = parent_details.get(key)
            if isinstance(value, (str, int, float, bool)):
                reaction_details[key] = value
        base_plan = self._trade_plan_template_from_closed_trade(trade)
        size_multiplier = self.loss_reaction_size_multiplier
        return replace(
            base_plan,
            side=reaction_side,
            setup="loss_reaction",
            target_notional_usd=round(base_plan.target_notional_usd * size_multiplier, 6),
            margin_usd=round(base_plan.margin_usd * size_multiplier, 6),
            risk_budget_usd=round(base_plan.risk_budget_usd * size_multiplier, 6),
            expected_loss_usd=round(base_plan.expected_loss_usd * size_multiplier, 6),
            reentry_cooldown_minutes=0,
            invalidation_price=None,
            setup_details=reaction_details,
        )

    def _loss_reaction_validation_details(
        self,
        *,
        side: str,
        snapshot: SymbolMarketSnapshot | None,
    ) -> dict[str, float | str | bool] | None:
        if self.loss_reaction_validation == "none":
            return {"loss_reaction_validation_passed": True}
        if snapshot is None:
            return None
        trend_score, trend_components = self._reaction_trend_score(side, snapshot)
        orderflow_score, orderflow_components = self._reaction_orderflow_score(side, snapshot)
        micro_score, micro_components = self._reaction_micro_score(side, snapshot)
        passed = False
        if self.loss_reaction_validation == "trend":
            passed = trend_score >= 3
        elif self.loss_reaction_validation == "orderflow":
            passed = orderflow_score >= 2
        elif self.loss_reaction_validation == "trend_orderflow":
            passed = trend_score >= 3 and orderflow_score >= 2
        elif self.loss_reaction_validation == "micro_momentum":
            passed = micro_score >= 3
        if not passed:
            return None
        return {
            "loss_reaction_validation_passed": True,
            "loss_reaction_validation_trend_score": trend_score,
            "loss_reaction_validation_orderflow_score": orderflow_score,
            "loss_reaction_validation_micro_score": micro_score,
            **trend_components,
            **orderflow_components,
            **micro_components,
        }

    def _reaction_trend_score(
        self,
        side: str,
        snapshot: SymbolMarketSnapshot,
    ) -> tuple[int, dict[str, float | bool]]:
        direction = 1.0 if side == "long" else -1.0
        price = float(snapshot.price)
        ema_fast = float(snapshot.ema_fast)
        ema_slow = float(snapshot.ema_slow)
        checks = {
            "price_vs_ema_fast_aligned": direction * (price - ema_fast) > 0.0,
            "price_vs_ema_slow_aligned": direction * (price - ema_slow) > 0.0,
            "vwap_aligned": direction * float(snapshot.vwap_distance_bps) > 0.0,
            "structure_aligned": direction * float(snapshot.structure_score) > 0.0,
        }
        details: dict[str, float | bool] = {
            "loss_reaction_price_vs_ema_fast_bps": (
                round(((price - ema_fast) / ema_fast) * 10_000.0, 6)
                if ema_fast > 0
                else 0.0
            ),
            "loss_reaction_price_vs_ema_slow_bps": (
                round(((price - ema_slow) / ema_slow) * 10_000.0, 6)
                if ema_slow > 0
                else 0.0
            ),
            "loss_reaction_vwap_distance_bps": round(float(snapshot.vwap_distance_bps), 6),
            "loss_reaction_structure_score": round(float(snapshot.structure_score), 6),
            **{f"loss_reaction_{key}": value for key, value in checks.items()},
        }
        return sum(1 for value in checks.values() if value), details

    def _reaction_orderflow_score(
        self,
        side: str,
        snapshot: SymbolMarketSnapshot,
    ) -> tuple[int, dict[str, float | bool]]:
        direction = 1.0 if side == "long" else -1.0
        book = float(snapshot.book_imbalance)
        flow = float(snapshot.trade_flow_bias)
        checks = {
            "book_imbalance_aligned": direction * book >= 0.20,
            "trade_flow_bias_aligned": direction * flow >= 0.50,
        }
        return (
            sum(1 for value in checks.values() if value),
            {
                "loss_reaction_book_imbalance": round(book, 6),
                "loss_reaction_trade_flow_bias": round(flow, 6),
                **{f"loss_reaction_{key}": value for key, value in checks.items()},
            },
        )

    def _reaction_micro_score(
        self,
        side: str,
        snapshot: SymbolMarketSnapshot,
    ) -> tuple[int, dict[str, float | bool]]:
        direction = 1.0 if side == "long" else -1.0
        micro = float(snapshot.microprice_dislocation_bps)
        flow = float(snapshot.trade_flow_bias)
        book = float(snapshot.book_imbalance)
        vwap = float(snapshot.vwap_distance_bps)
        checks = {
            "microprice_aligned": direction * micro > 0.0,
            "trade_flow_bias_soft_aligned": direction * flow > 0.0,
            "book_imbalance_soft_aligned": direction * book > 0.0,
            "vwap_soft_aligned": direction * vwap > 0.0,
        }
        return (
            sum(1 for value in checks.values() if value),
            {
                "loss_reaction_microprice_dislocation_bps": round(micro, 6),
                **{f"loss_reaction_{key}": value for key, value in checks.items()},
            },
        )

    def _trade_plan_template_from_closed_trade(self, trade: object) -> TradePlan:
        effective_leverage = float(getattr(trade, "effective_leverage", 1.0) or 1.0)
        return TradePlan(
            symbol=str(getattr(trade, "symbol", "")),
            side=str(getattr(trade, "side", "")),
            setup=str(getattr(trade, "setup", "")),
            confidence=float(getattr(trade, "confidence", 0.0) or 0.0),
            target_notional_usd=float(getattr(trade, "target_notional_usd", 0.0) or 0.0),
            stop_bps=float(getattr(trade, "stop_bps", 0.0) or 0.0),
            time_stop_hours=int(getattr(trade, "time_stop_hours", 0) or 0),
            take_profit_bps=float(getattr(trade, "take_profit_bps", 0.0) or 0.0),
            break_even_trigger_bps=float(
                getattr(trade, "break_even_trigger_bps", 0.0) or 0.0
            ),
            trailing_activation_bps=float(
                getattr(trade, "trailing_activation_bps", 0.0) or 0.0
            ),
            trailing_distance_bps=float(getattr(trade, "trailing_distance_bps", 0.0) or 0.0),
            margin_usd=float(getattr(trade, "margin_usd", 0.0) or 0.0),
            requested_leverage=effective_leverage,
            effective_leverage=effective_leverage,
            risk_budget_usd=float(getattr(trade, "risk_budget_usd", 0.0) or 0.0),
            expected_loss_usd=float(getattr(trade, "expected_loss_usd", 0.0) or 0.0),
            isolated=bool(getattr(trade, "isolated", True)),
        )

    def _combine_execution_batches(
        self,
        first: ExecutionBatch,
        second: ExecutionBatch,
    ) -> ExecutionBatch:
        return ExecutionBatch(
            opened_symbols=[*first.opened_symbols, *second.opened_symbols],
            skipped_open_symbols=[*first.skipped_open_symbols, *second.skipped_open_symbols],
            skip_reasons_by_symbol={
                **first.skip_reasons_by_symbol,
                **second.skip_reasons_by_symbol,
            },
            closed_trades=[*first.closed_trades, *second.closed_trades],
            fills=[*first.fills, *second.fills],
            had_open_position_before={
                **second.had_open_position_before,
                **first.had_open_position_before,
            },
            has_open_position_after={
                **first.has_open_position_after,
                **second.has_open_position_after,
            },
            close_reasons_by_symbol={
                **first.close_reasons_by_symbol,
                **second.close_reasons_by_symbol,
            },
        )

    def _pod_b_planning_allocation(
        self,
        base: PodAllocation,
        signals: list[object],
    ) -> PodAllocation:
        if not signals:
            return base
        signal_symbols = list(dict.fromkeys(str(signal.symbol) for signal in signals))
        total_equity = max(self.config.trident.capital.reference_equity_usd, 1e-9)
        target_usd = min(base.target_usd, base.target_pct * total_equity)
        if target_usd <= 0:
            return base
        per_symbol_usd = min(
            target_usd / len(signal_symbols),
            self.config.trident.capital.max_allocation_per_symbol_pct * total_equity,
        )
        if per_symbol_usd <= 0:
            return base
        allocated_usd = round(per_symbol_usd * len(signal_symbols), 2)
        return PodAllocation(
            pod=base.pod,
            target_pct=round(allocated_usd / total_equity, 6),
            target_usd=allocated_usd,
            capped_by_pod_limit=base.capped_by_pod_limit,
            symbols=[
                SymbolAllocation(
                    symbol=symbol,
                    target_pct=round(per_symbol_usd / total_equity, 6),
                    target_usd=round(per_symbol_usd, 2),
                )
                for symbol in signal_symbols
            ],
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
        date_key = self._date_key(timestamp, source_file)
        report.add_record_date(date_key)
        if current_regime != previous_regime:
            report.add_regime_transition(
                date_key=date_key,
                previous_regime=previous_regime,
                new_regime=current_regime,
            )
        report.add_record_regime(current_regime)

    def _record_directional_tick(
        self,
        *,
        report: PodABacktestReport,
        config: AppConfig,
        current_regime: str,
        timestamp: str | None,
        source_file: str,
        previews: list[object],
        risk_decisions: list[RiskDecision],
        execution: object,
        executor: DirectionalExecutor,
        closed_trade_recorder: Callable[[object], None] | None = None,
    ) -> None:
        date_key = self._date_key(timestamp, source_file)
        decisions_by_symbol = {decision.trade_plan.symbol: decision for decision in risk_decisions}
        for preview in previews:
            report.add_signal(
                date_key=date_key,
                symbol=preview.symbol,
                side=preview.side,
                setup=preview.setup,
                regime=current_regime,
                confidence=preview.confidence,
                market_cluster=cluster_for_symbol(config, preview.symbol),
            )
        for decision in risk_decisions:
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
        report.observe_open_exposure(list(executor.portfolio.open_positions.values()))
        for trade in execution.closed_trades:
            if closed_trade_recorder is not None:
                closed_trade_recorder(trade)
            report.add_closed_trade(
                date_key=self._date_key(
                    trade.closed_at.isoformat() if trade.closed_at is not None else timestamp,
                    source_file,
                ),
                symbol=trade.symbol,
                side=trade.side,
                setup=getattr(trade, "setup", None),
                confidence=getattr(trade, "confidence", None),
                market_cluster=cluster_for_symbol(config, trade.symbol),
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
                hold_hours=self._hold_hours(trade),
                opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
                setup_details=getattr(trade, "setup_details", None),
            )

    def _finalize_directional_report(
        self,
        *,
        supervisor: TridentSupervisor,
        report: PodABacktestReport,
        executor: DirectionalExecutor,
        latest_snapshots: list[SymbolMarketSnapshot],
        last_timestamp: str | None,
        closed_trade_recorder: Callable[[object], None] | None = None,
    ) -> None:
        final_trades, _ = executor.finalize(
            snapshots=latest_snapshots,
            timestamp=last_timestamp,
        )
        for trade in final_trades:
            if closed_trade_recorder is not None:
                closed_trade_recorder(trade)
            report.add_closed_trade(
                date_key=self._date_key(
                    trade.closed_at.isoformat() if trade.closed_at is not None else last_timestamp,
                    "finalize",
                ),
                symbol=trade.symbol,
                side=trade.side,
                setup=getattr(trade, "setup", None),
                confidence=getattr(trade, "confidence", None),
                market_cluster=cluster_for_symbol(self.config, trade.symbol),
                close_regime=supervisor.state.regime.value,
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
                hold_hours=self._hold_hours(trade),
                opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
                setup_details=getattr(trade, "setup_details", None),
            )

    def _record_pod_b_closed_trade(self, trade: object) -> None:
        self.pod_b_risk_gate.record_closed_trade(
            symbol=str(getattr(trade, "symbol", "")),
            setup=getattr(trade, "setup", None),
            pnl_usd=getattr(trade, "pnl_usd", None),
        )

    def _record_pod_a_closed_trade(self, trade: object) -> None:
        closed_at = getattr(trade, "closed_at", None)
        self.pod_a_risk_gate.record_closed_trade(
            symbol=str(getattr(trade, "symbol", "")),
            setup=getattr(trade, "setup", None),
            pnl_usd=getattr(trade, "pnl_usd", None),
            date_key=(closed_at.isoformat()[:10] if closed_at is not None else None),
        )

    def _date_key(self, timestamp: str | None, fallback_source_file: str) -> str:
        if timestamp:
            return timestamp[:10]
        if fallback_source_file.endswith(".jsonl"):
            return fallback_source_file.removesuffix(".jsonl")
        return fallback_source_file

    def _hold_hours(self, trade: object) -> float | None:
        opened_at = getattr(trade, "opened_at", None)
        closed_at = getattr(trade, "closed_at", None)
        if opened_at is None or closed_at is None:
            return None
        return round((closed_at - opened_at).total_seconds() / 3600.0, 4)

    def _experiment_summary(
        self,
        *,
        pod_a: dict[str, object],
        pod_b: dict[str, object],
        pod_c: dict[str, object],
    ) -> dict[str, object]:
        if not self.enable_loss_reaction_trades:
            return {
                "loss_reaction_trades_enabled": False,
                "loss_reaction_policy": self._loss_reaction_policy_summary(),
            }
        pods: dict[str, dict[str, object]] = {}
        total_opened = 0
        total_closed = 0
        total_pnl = 0.0
        for pod_name, pod in (("pod_a", pod_a), ("pod_b", pod_b), ("pod_c", pod_c)):
            opened_by_setup = pod.get("opened_by_setup", {})
            trades_by_setup = pod.get("trades_by_setup", {})
            pnl_by_setup = pod.get("pnl_by_setup", {})
            log = pod.get("closed_trade_log", [])
            reaction_trades = [
                trade
                for trade in log
                if isinstance(trade, dict)
                and (
                    trade.get("setup") == "loss_reaction"
                    or bool((trade.get("setup_details") or {}).get("loss_reaction_trade"))
                )
            ]
            close_reasons: dict[str, int] = {}
            parent_setups: dict[str, int] = {}
            for trade in reaction_trades:
                reason = str(trade.get("close_reason", ""))
                if reason:
                    close_reasons[reason] = close_reasons.get(reason, 0) + 1
                setup_details = trade.get("setup_details") or {}
                if isinstance(setup_details, dict):
                    parent_setup = str(setup_details.get("loss_reaction_parent_setup", ""))
                    if parent_setup:
                        parent_setups[parent_setup] = parent_setups.get(parent_setup, 0) + 1
            opened = int(
                opened_by_setup.get("loss_reaction", 0) if isinstance(opened_by_setup, dict) else 0
            )
            closed = int(
                trades_by_setup.get("loss_reaction", 0) if isinstance(trades_by_setup, dict) else 0
            )
            pnl = float(
                pnl_by_setup.get("loss_reaction", 0.0) if isinstance(pnl_by_setup, dict) else 0.0
            )
            wins = sum(1 for trade in reaction_trades if float(trade.get("pnl_usd", 0.0) or 0.0) >= 0.0)
            losses = len(reaction_trades) - wins
            pods[pod_name] = {
                "opened_count": opened,
                "closed_trade_count": closed,
                "realized_pnl_usd": round(pnl, 2),
                "win_count": wins,
                "loss_count": losses,
                "win_rate": round(wins / len(reaction_trades), 4) if reaction_trades else None,
                "close_reasons": close_reasons,
                "parent_setups": parent_setups,
            }
            total_opened += opened
            total_closed += closed
            total_pnl += pnl
        return {
            "loss_reaction_trades_enabled": True,
            "loss_reaction_policy": self._loss_reaction_policy_summary(),
            "loss_reaction_trades": {
                "setup": "loss_reaction",
                "opened_count": total_opened,
                "closed_trade_count": total_closed,
                "realized_pnl_usd": round(total_pnl, 2),
                "pods": pods,
            },
        }

    def _comparison_entry(self, result: FullBotBacktestResult) -> dict[str, object]:
        pod_a = result.pod_a
        pod_b = result.pod_b
        pod_c = result.pod_c
        routing = result.routing
        loss_reaction = result.experiments.get("loss_reaction_trades", {})
        return {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "input_path": result.input_path,
            "dates_covered": result.dates_covered,
            "dedupe_by_timestamp": result.dedupe_by_timestamp,
            "records_processed": result.records_processed,
            "duplicate_timestamps_skipped": result.duplicate_timestamps_skipped,
            "total_realized_pnl_usd": result.total_realized_pnl_usd,
            "directional_fees_usd": result.directional_fees_usd,
            "pod_a_realized_pnl_usd": pod_a.get("realized_pnl_usd", 0.0),
            "pod_b_realized_pnl_usd": pod_b.get("realized_pnl_usd", 0.0),
            "pod_c_realized_pnl_usd": pod_c.get("realized_pnl_usd", 0.0),
            "pod_a_closed_trade_count": pod_a.get("closed_trade_count", 0),
            "pod_b_closed_trade_count": pod_b.get("closed_trade_count", 0),
            "pod_c_closed_trade_count": pod_c.get("closed_trade_count", 0),
            "loss_reaction_trades_enabled": result.experiments.get(
                "loss_reaction_trades_enabled",
                False,
            ),
            "loss_reaction_closed_trade_count": (
                loss_reaction.get("closed_trade_count", 0)
                if isinstance(loss_reaction, dict)
                else 0
            ),
            "loss_reaction_realized_pnl_usd": (
                loss_reaction.get("realized_pnl_usd", 0.0)
                if isinstance(loss_reaction, dict)
                else 0.0
            ),
            "routing_reassignment_event_count": routing.get("reassignment_event_count", 0),
            "routing_max_ownership_conflict_count": routing.get(
                "max_ownership_conflict_count",
                0,
            ),
            "report_path": result.report_path,
            "summary_path": result.summary_path,
        }

    def _render_summary(self, result: FullBotBacktestResult) -> str:
        pod_a = result.pod_a
        pod_b = result.pod_b
        pod_c = result.pod_c
        routing = result.routing
        loss_reaction = result.experiments.get("loss_reaction_trades", {})
        experiment_lines: list[str] = []
        if isinstance(loss_reaction, dict):
            experiment_lines = [
                "\n",
                "## Experience loss_reaction_trades\n\n",
                f"- enabled: `{result.experiments.get('loss_reaction_trades_enabled')}`\n",
                f"- policy: `{result.experiments.get('loss_reaction_policy', {})}`\n",
                f"- opened_count: `{loss_reaction.get('opened_count', 0)}`\n",
                f"- closed_trade_count: `{loss_reaction.get('closed_trade_count', 0)}`\n",
                f"- realized_pnl_usd: `{loss_reaction.get('realized_pnl_usd', 0.0)}`\n",
            ]
        return "".join(
            [
                "# TRIDENT full-bot backtest\n\n",
                f"- input: `{result.input_path}`\n",
                f"- dates: `{', '.join(result.dates_covered)}`\n",
                f"- records_processed: `{result.records_processed}`\n",
                f"- duplicate_timestamps_skipped: `{result.duplicate_timestamps_skipped}`\n",
                f"- total_realized_pnl_usd: `{result.total_realized_pnl_usd}`\n",
                f"- directional_fees_usd: `{result.directional_fees_usd}`\n",
                "\n",
                "## PnL par pod\n\n",
                f"- Pod A realized_pnl_usd: `{pod_a.get('realized_pnl_usd', 0.0)}`\n",
                f"- Pod B realized_pnl_usd: `{pod_b.get('realized_pnl_usd', 0.0)}`\n",
                f"- Pod C realized_pnl_usd: `{pod_c.get('realized_pnl_usd', 0.0)}`\n",
                "\n",
                "## Activite\n\n",
                f"- Pod A closed_trade_count: `{pod_a.get('closed_trade_count', 0)}`\n",
                f"- Pod B closed_trade_count: `{pod_b.get('closed_trade_count', 0)}`\n",
                f"- Pod C closed_trade_count: `{pod_c.get('closed_trade_count', 0)}`\n",
                f"- total_activity_count: `{result.total_activity_count}`\n",
                f"- routing reassignment_event_count: `{routing.get('reassignment_event_count', 0)}`\n",
                f"- routing max_ownership_conflict_count: `{routing.get('max_ownership_conflict_count', 0)}`\n",
                "\n",
                "## Notes\n\n",
                "".join(f"- {note}\n" for note in result.notes),
                *experiment_lines,
            ]
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay Pod A, Pod B, and Pod C together on one snapshot stream",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--report-output")
    parser.add_argument("--summary-output")
    parser.add_argument("--comparison-output")
    parser.add_argument(
        "--no-dedupe-timestamps",
        action="store_true",
        help="Do not skip duplicate timestamps in the input stream.",
    )
    parser.add_argument(
        "--respect-config-enabled",
        action="store_true",
        help="Do not force-enable Pod A / Pod C for this backtest.",
    )
    parser.add_argument(
        "--apply-live-notional-caps",
        action="store_true",
        help="Apply the A/C live notional cap before dry-run replay execution.",
    )
    parser.add_argument(
        "--enable-loss-reaction-trades",
        action="store_true",
        help="Open one opposite-side research-only reaction trade after a losing close.",
    )
    parser.add_argument(
        "--loss-reaction-direction",
        choices=("opposite", "same"),
        default="opposite",
        help="Reaction direction relative to the losing parent trade.",
    )
    parser.add_argument(
        "--loss-reaction-parent-close-reasons",
        default="",
        help="Comma-separated parent close reasons allowed to trigger a reaction.",
    )
    parser.add_argument(
        "--loss-reaction-pods",
        default="",
        help="Comma-separated pod values allowed to trigger reactions, e.g. pod_a,pod_c.",
    )
    parser.add_argument(
        "--loss-reaction-validation",
        choices=("none", "trend", "orderflow", "trend_orderflow", "micro_momentum"),
        default="none",
        help="Snapshot validation required before opening a reaction trade.",
    )
    parser.add_argument(
        "--loss-reaction-max-parent-hold-hours",
        type=float,
        default=None,
        help="Only react to parent trades held at most this many hours.",
    )
    parser.add_argument(
        "--loss-reaction-size-multiplier",
        type=float,
        default=1.0,
        help="Scale cloned reaction notional/risk/margin by this multiplier.",
    )
    parser.add_argument(
        "--loss-reaction-ignore-entry-guards",
        action="store_true",
        help="Allow reaction trades to bypass entry routing/managed-symbol guards.",
    )
    return parser


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def main() -> None:
    args = build_parser().parse_args()
    result = FullBotBacktestRunner(
        load_config(args.config),
        force_enable_all_pods=not args.respect_config_enabled,
        apply_live_notional_caps=args.apply_live_notional_caps,
        enable_loss_reaction_trades=args.enable_loss_reaction_trades,
        loss_reaction_direction=args.loss_reaction_direction,
        loss_reaction_parent_close_reasons=_csv_set(args.loss_reaction_parent_close_reasons),
        loss_reaction_allowed_pods=_csv_set(args.loss_reaction_pods),
        loss_reaction_validation=args.loss_reaction_validation,
        loss_reaction_max_parent_hold_hours=args.loss_reaction_max_parent_hold_hours,
        loss_reaction_size_multiplier=args.loss_reaction_size_multiplier,
        loss_reaction_ignore_entry_guards=args.loss_reaction_ignore_entry_guards,
    ).run_jsonl(
        input_path=args.input,
        dedupe_by_timestamp=not args.no_dedupe_timestamps,
        report_output=args.report_output,
        summary_output=args.summary_output,
        comparison_output=args.comparison_output,
    )
    print(f"dates_covered={result.dates_covered}")
    print(f"records_processed={result.records_processed}")
    print(f"duplicate_timestamps_skipped={result.duplicate_timestamps_skipped}")
    print(f"total_realized_pnl_usd={result.total_realized_pnl_usd}")
    print(f"pod_a_realized_pnl_usd={result.pod_a.get('realized_pnl_usd', 0.0)}")
    print(f"pod_b_realized_pnl_usd={result.pod_b.get('realized_pnl_usd', 0.0)}")
    print(f"pod_c_realized_pnl_usd={result.pod_c.get('realized_pnl_usd', 0.0)}")
    print(f"routing_reassignment_event_count={result.routing.get('reassignment_event_count', 0)}")
    if result.report_path:
        print(f"report_path={result.report_path}")
    if result.summary_path:
        print(f"summary_path={result.summary_path}")


if __name__ == "__main__":
    main()
