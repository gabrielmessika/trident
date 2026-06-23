from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.backtest.snapshot_loader import SnapshotLoader
from app.trident.market_clusters import cluster_for_symbol
from app.persistence.journal import (
    JsonlJournal,
    build_signal_journal_record,
    build_signal_review_journal_record,
    build_trade_journal_record,
)
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, RiskDecision, SymbolMarketSnapshot, symbol_market_snapshot_from_mapping


@dataclass(slots=True)
class PodABacktestResult:
    reference_equity_usd: float
    records_processed: int
    signal_count: int
    accepted_count: int
    rejected_count: int
    opened_count: int
    skipped_open_count: int
    closed_trade_count: int
    win_count: int
    loss_count: int
    realized_pnl_usd: float
    gross_pnl_usd: float
    fees_usd: float
    max_drawdown_usd: float
    average_hold_hours: float
    records_by_regime: dict[str, int]
    records_by_date: dict[str, int]
    signals_by_symbol: dict[str, int]
    signals_by_cluster: dict[str, int]
    signals_by_side: dict[str, int]
    signals_by_setup: dict[str, int]
    signals_by_regime: dict[str, int]
    signals_by_date: dict[str, int]
    accepted_by_date: dict[str, int]
    rejected_by_date: dict[str, int]
    rejections_by_reason: dict[str, int]
    accepted_by_setup: dict[str, int]
    rejected_by_setup: dict[str, int]
    regime_transition_count: int
    regime_transitions: dict[str, int]
    regime_transitions_by_date: dict[str, dict[str, int]]
    close_reasons: dict[str, int]
    opened_by_setup: dict[str, int]
    skipped_open_by_setup: dict[str, int]
    trades_by_symbol: dict[str, int]
    trades_by_cluster: dict[str, int]
    trades_by_regime: dict[str, int]
    trades_by_setup: dict[str, int]
    pnl_by_symbol: dict[str, float]
    pnl_by_cluster: dict[str, float]
    pnl_by_regime: dict[str, float]
    pnl_by_setup: dict[str, float]
    pnl_by_date: dict[str, float]
    max_open_positions: int
    max_open_margin_usd: float
    max_open_notional_usd: float
    max_open_expected_loss_usd: float
    closed_trade_log: list[dict[str, object]]
    average_confidence: float
    output_path: str | None = None


class PodABacktestRunner:
    """Replays market snapshots through Pod A signal generation."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.loader = SnapshotLoader()
        self.risk_gate = PodARiskGate(config)
        self.executor = PodAExecutor(config)

    def run_jsonl(
        self,
        input_path: str | Path,
        output_path: str | Path | None = None,
        *,
        include_signal_reviews: bool = True,
    ) -> PodABacktestResult:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-backtest",
            mode="observation",
        )
        output_journal = JsonlJournal(output_path) if output_path is not None else None
        report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )

        last_snapshot_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        last_timestamp: str | None = None

        for record in self.loader.iter_jsonl(input_path):
            report.records_processed += 1
            date_key = self._date_key(record.timestamp, record.source_file)
            report.add_record_date(date_key)
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
            if current_regime != previous_regime:
                report.add_regime_transition(
                    date_key=date_key,
                    previous_regime=previous_regime,
                    new_regime=current_regime,
                )
            report.add_record_regime(supervisor.state.regime.value)
            snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
            previews = supervisor.preview_pod_a_signals(snapshots, timestamp=record.timestamp)
            trade_plans = supervisor.build_pod_a_trade_plans(snapshots, timestamp=record.timestamp)
            for plan in trade_plans:
                plan.setup_details = {
                    **dict(plan.setup_details or {}),
                    "current_date_key": date_key,
                }
            risk_decisions = self.risk_gate.evaluate_many(trade_plans)
            execution = self.executor.process_record(
                snapshots=snapshots,
                risk_decisions=risk_decisions,
                signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
                timestamp=record.timestamp,
            )

            snapshot_by_symbol = {item["symbol"]: item for item in record.symbols}
            decisions_by_symbol: dict[str, RiskDecision] = {
                decision.trade_plan.symbol: decision for decision in risk_decisions
            }
            pod_allocation = supervisor.capital_plan.pod_allocations[PodName.POD_A]
            allocation_by_symbol = {
                item.symbol: item
                for item in pod_allocation.symbols
            }

            if output_journal is not None:
                fills_by_symbol: dict[str, list[dict[str, object]]] = {}
                for fill in execution.fills:
                    fills_by_symbol.setdefault(str(fill["symbol"]), []).append(fill)
                output_journal.append_many(
                    build_signal_journal_record(
                        timestamp=record.timestamp,
                        record_index=record.record_index,
                        regime=supervisor.state.regime.value,
                        regime_snapshot=record.regime_snapshot,
                        symbol_snapshot=snapshot_by_symbol.get(preview.symbol),
                        signal={
                            "symbol": preview.symbol,
                            "side": preview.side,
                            "setup": preview.setup,
                            "confidence": preview.confidence,
                            "reason_summary": preview.reason_summary,
                            "setup_details": dict(preview.setup_details),
                            "confidence_components": (
                                decisions_by_symbol[preview.symbol].trade_plan.confidence_components
                                if preview.symbol in decisions_by_symbol
                                else {}
                            ),
                            "allocation": {
                                "pod_target_pct": pod_allocation.target_pct,
                                "pod_target_usd": pod_allocation.target_usd,
                                "symbol_target_pct": allocation_by_symbol.get(preview.symbol).target_pct
                                if preview.symbol in allocation_by_symbol
                                else 0.0,
                                "symbol_target_usd": allocation_by_symbol.get(preview.symbol).target_usd
                                if preview.symbol in allocation_by_symbol
                                else 0.0,
                                "reason_summary": allocation_by_symbol.get(preview.symbol).reason_summary
                                if preview.symbol in allocation_by_symbol
                                else "",
                                "correlation_group": allocation_by_symbol.get(preview.symbol).correlation_group
                                if preview.symbol in allocation_by_symbol
                                else "",
                                "correlation_density_factor": allocation_by_symbol.get(preview.symbol).correlation_density_factor
                                if preview.symbol in allocation_by_symbol
                                else 1.0,
                                "capped_by_correlation": allocation_by_symbol.get(preview.symbol).capped_by_correlation
                                if preview.symbol in allocation_by_symbol
                                else False,
                            },
                            "source_file": record.source_file,
                            "risk": {
                                "accepted": decisions_by_symbol.get(preview.symbol).accepted
                                if preview.symbol in decisions_by_symbol
                                else False,
                                "reason": decisions_by_symbol.get(preview.symbol).reason
                                if preview.symbol in decisions_by_symbol
                                else "missing_trade_plan",
                                "target_notional_usd": (
                                    decisions_by_symbol[preview.symbol].trade_plan.target_notional_usd
                                    if preview.symbol in decisions_by_symbol
                                    else 0.0
                                ),
                                "margin_usd": (
                                    decisions_by_symbol[preview.symbol].trade_plan.margin_usd
                                    if preview.symbol in decisions_by_symbol
                                    else 0.0
                                ),
                                "effective_leverage": (
                                    decisions_by_symbol[preview.symbol].trade_plan.effective_leverage
                                    if preview.symbol in decisions_by_symbol
                                    else 1.0
                                ),
                                "risk_budget_usd": (
                                    decisions_by_symbol[preview.symbol].trade_plan.risk_budget_usd
                                    if preview.symbol in decisions_by_symbol
                                    else 0.0
                                ),
                                "expected_loss_usd": (
                                    decisions_by_symbol[preview.symbol].trade_plan.expected_loss_usd
                                    if preview.symbol in decisions_by_symbol
                                    else 0.0
                                ),
                                "invalidation_price": (
                                    decisions_by_symbol[preview.symbol].trade_plan.invalidation_price
                                    if preview.symbol in decisions_by_symbol
                                    else None
                                ),
                                "stop_bps": (
                                    decisions_by_symbol[preview.symbol].trade_plan.stop_bps
                                    if preview.symbol in decisions_by_symbol
                                    else 0.0
                                ),
                            },
                            "execution": {
                                "had_open_position_before": execution.had_open_position_before.get(
                                    preview.symbol,
                                    False,
                                ),
                                "has_open_position_after": execution.has_open_position_after.get(
                                    preview.symbol,
                                    False,
                                ),
                                "opened": preview.symbol in execution.opened_symbols,
                                "skipped_open": preview.symbol
                                in execution.skipped_open_symbols,
                                "skip_reason": execution.skip_reasons_by_symbol.get(
                                    preview.symbol
                                ),
                                "close_reason": execution.close_reasons_by_symbol.get(
                                    preview.symbol
                                ),
                                "open_fills": [
                                    fill
                                    for fill in fills_by_symbol.get(preview.symbol, [])
                                    if fill.get("action") == "open"
                                ],
                                "close_fills": [
                                    fill
                                    for fill in fills_by_symbol.get(preview.symbol, [])
                                    if fill.get("action") == "close"
                                ],
                            },
                        },
                    )
                    for preview in previews
                )
                if include_signal_reviews:
                    output_journal.append_many(
                        build_signal_review_journal_record(
                            timestamp=record.timestamp,
                            record_index=record.record_index,
                            regime=supervisor.state.regime.value,
                            regime_snapshot=record.regime_snapshot,
                            symbol_snapshot=snapshot_by_symbol.get(str(review.get("symbol", ""))),
                            source="pod_a_backtest_filtered",
                            review=review,
                        )
                        for review in supervisor.state.pod_a_signal_review
                        if str(review.get("status")) == "filtered"
                    )

            for preview in previews:
                report.add_signal(
                    date_key=date_key,
                    symbol=preview.symbol,
                    side=preview.side,
                    setup=preview.setup,
                    regime=supervisor.state.regime.value,
                    confidence=preview.confidence,
                    market_cluster=cluster_for_symbol(self.config, preview.symbol),
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
            report.observe_open_exposure(
                list(self.executor.portfolio.open_positions.values())
            )
            for trade in execution.closed_trades:
                self.risk_gate.record_closed_trade(
                    symbol=trade.symbol,
                    setup=getattr(trade, "setup", None),
                    pnl_usd=trade.pnl_usd,
                    date_key=self._date_key(
                        trade.closed_at.isoformat() if trade.closed_at is not None else record.timestamp,
                        record.source_file,
                    ),
                )
                if output_journal is not None:
                    output_journal.append(
                        build_trade_journal_record(
                            timestamp=record.timestamp,
                            record_index=record.record_index,
                            trade=self._trade_to_record(trade),
                        )
                    )
                report.add_closed_trade(
                    date_key=self._date_key(
                        trade.closed_at.isoformat() if trade.closed_at is not None else record.timestamp,
                        record.source_file,
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
                    hold_hours=self._hold_hours(trade),
                    opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                    closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
                    setup_details=getattr(trade, "setup_details", None),
                )

            last_snapshot_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
            last_timestamp = record.timestamp

        final_trades, _ = self.executor.finalize(
            snapshots=list(last_snapshot_by_symbol.values()),
            timestamp=last_timestamp,
        )
        supervisor.flush_compact_logs()
        for trade in final_trades:
            self.risk_gate.record_closed_trade(
                symbol=trade.symbol,
                setup=getattr(trade, "setup", None),
                pnl_usd=trade.pnl_usd,
                date_key=self._date_key(
                    trade.closed_at.isoformat() if trade.closed_at is not None else last_timestamp,
                    "finalize",
                ),
            )
            if output_journal is not None:
                output_journal.append(
                    build_trade_journal_record(
                        timestamp=last_timestamp,
                        record_index=report.records_processed,
                        trade=self._trade_to_record(trade),
                    )
                )
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

        return PodABacktestResult(
            reference_equity_usd=report.reference_equity_usd,
            records_processed=report.records_processed,
            signal_count=report.signal_count,
            accepted_count=report.accepted_count,
            rejected_count=report.rejected_count,
            opened_count=report.opened_count,
            skipped_open_count=report.skipped_open_count,
            closed_trade_count=report.closed_trade_count,
            win_count=report.win_count,
            loss_count=report.loss_count,
            realized_pnl_usd=report.realized_pnl_usd,
            gross_pnl_usd=report.gross_pnl_usd,
            fees_usd=report.fees_usd,
            max_drawdown_usd=report.max_drawdown_usd,
            average_hold_hours=report.average_hold_hours,
            records_by_regime=report.records_by_regime,
            records_by_date=report.records_by_date,
            signals_by_symbol=report.signals_by_symbol,
            signals_by_cluster=report.signals_by_cluster,
            signals_by_side=report.signals_by_side,
            signals_by_setup=report.signals_by_setup,
            signals_by_regime=report.signals_by_regime,
            signals_by_date=report.signals_by_date,
            accepted_by_date=report.accepted_by_date,
            rejected_by_date=report.rejected_by_date,
            rejections_by_reason=report.rejections_by_reason,
            accepted_by_setup=report.accepted_by_setup,
            rejected_by_setup=report.rejected_by_setup,
            regime_transition_count=report.regime_transition_count,
            regime_transitions=report.regime_transitions,
            regime_transitions_by_date=report.regime_transitions_by_date,
            close_reasons=report.close_reasons,
            opened_by_setup=report.opened_by_setup,
            skipped_open_by_setup=report.skipped_open_by_setup,
            trades_by_symbol=report.trades_by_symbol,
            trades_by_cluster=report.trades_by_cluster,
            trades_by_regime=report.trades_by_regime,
            trades_by_setup=report.trades_by_setup,
            pnl_by_symbol=report.pnl_by_symbol,
            pnl_by_cluster=report.pnl_by_cluster,
            pnl_by_regime=report.pnl_by_regime,
            pnl_by_setup=report.pnl_by_setup,
            pnl_by_date=report.pnl_by_date,
            max_open_positions=report.max_open_positions,
            max_open_margin_usd=report.max_open_margin_usd,
            max_open_notional_usd=report.max_open_notional_usd,
            max_open_expected_loss_usd=report.max_open_expected_loss_usd,
            closed_trade_log=report.closed_trade_log,
            average_confidence=report.average_confidence,
            output_path=str(output_path) if output_path is not None else None,
        )

    def _hold_hours(self, trade: object) -> float | None:
        opened_at = getattr(trade, "opened_at", None)
        closed_at = getattr(trade, "closed_at", None)
        if opened_at is None or closed_at is None:
            return None
        return round((closed_at - opened_at).total_seconds() / 3600.0, 4)

    def _trade_to_record(self, trade: object) -> dict[str, object]:
        return {
            "symbol": trade.symbol,
            "side": trade.side,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "target_notional_usd": trade.target_notional_usd,
            "margin_usd": getattr(trade, "margin_usd", None),
            "leverage": getattr(trade, "effective_leverage", None),
            "effective_leverage": getattr(trade, "effective_leverage", None),
            "risk_budget_usd": getattr(trade, "risk_budget_usd", None),
            "expected_loss_usd": getattr(trade, "expected_loss_usd", None),
            "invalidation_price": getattr(trade, "invalidation_price", None),
            "gross_pnl_usd": trade.gross_pnl_usd,
            "fees_usd": trade.fees_usd,
            "pnl_usd": trade.pnl_usd,
            "close_reason": trade.close_reason,
            "hold_hours": self._hold_hours(trade),
            "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
            "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
            "setup_details": dict(getattr(trade, "setup_details", {}) or {}),
        }

    def _date_key(self, timestamp: str | None, fallback_source_file: str) -> str:
        if timestamp:
            return timestamp[:10]
        if fallback_source_file.endswith(".jsonl"):
            return fallback_source_file.removesuffix(".jsonl")
        return fallback_source_file
