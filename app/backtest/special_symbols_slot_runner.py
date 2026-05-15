from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.backtest.pod_report import PodABacktestReport
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.directional_executor import DirectionalExecutor
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, load_config
from app.special_symbols_runtime import build_special_symbols_runtime_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.pod_a.context import MarketContextService
from app.trident.pod_a.planner import AnchorTrendPlanner
from app.trident.pod_a.service import AnchorTrendService
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodAllocation, PodName, RegimeSnapshot, SymbolAllocation, SymbolMarketSnapshot, symbol_market_snapshot_from_mapping


@dataclass(slots=True)
class SpecialSymbolsSlotBacktestResult:
    pod: str
    slot: str
    tradable_symbols: list[str]
    observe_only_symbols: list[str]
    observation_universe: list[str]
    backtest: dict[str, object]
    output_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SpecialSymbolsSlotBacktestRunner:
    """Backtests the new special-symbols pod using the Pod B capital slot."""

    def __init__(
        self,
        main_config: AppConfig,
        special_config: AppConfig,
        *,
        tradable_symbols: list[str] | None = None,
        observe_only_symbols: list[str] | None = None,
    ) -> None:
        runtime_config, selection = build_special_symbols_runtime_config(
            special_config,
            tradable_symbols=tradable_symbols,
            observe_only_symbols=observe_only_symbols,
        )
        self.main_config = main_config
        self.special_config = runtime_config
        self.selection = selection
        self.loader = SnapshotLoader()
        self.context_service = MarketContextService(runtime_config)
        self.service = AnchorTrendService(runtime_config)
        self.planner = AnchorTrendPlanner(runtime_config)
        self.risk_gate = PodARiskGate(runtime_config)
        self.executor = DirectionalExecutor(runtime_config)

    def run_jsonl(self, input_path: str | Path) -> SpecialSymbolsSlotBacktestResult:
        supervisor = TridentSupervisor(
            config=self.main_config,
            profile="trident-special-symbols-slot-backtest",
            mode="observation",
        )
        report = PodABacktestReport(
            reference_equity_usd=self.main_config.trident.capital.reference_equity_usd,
        )
        last_snapshot_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        last_timestamp: str | None = None
        tradable_set = {symbol.upper() for symbol in self.selection.tradable_symbols}
        universe_set = {symbol.upper() for symbol in self.selection.observation_universe}

        for record in self.loader.iter_merged_jsonl(input_path):
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
            report.add_record_regime(current_regime)

            snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
            special_universe_snapshots = [
                snapshot for snapshot in snapshots if snapshot.symbol.upper() in universe_set
            ]
            if special_universe_snapshots:
                contexts = self.context_service.build_contexts(
                    supervisor.state.regime,
                    special_universe_snapshots,
                    timestamp=record.timestamp,
                )
            else:
                contexts = []
            tradable_contexts = [
                context for context in contexts if context.symbol.upper() in tradable_set
            ]
            signals = self.service.evaluate_many(tradable_contexts)
            pod_allocation = self._planning_allocation(
                supervisor.capital_plan.pod_allocations[PodName.POD_B],
                signals,
            )
            trade_plans = [
                plan
                for signal in signals
                if (plan := self.planner.build_trade_plan(signal, pod_allocation)) is not None
            ]
            for plan in trade_plans:
                plan.setup_details = {
                    **dict(plan.setup_details or {}),
                    "current_date_key": date_key,
                }
            risk_decisions = self.risk_gate.evaluate_many(trade_plans)
            tradable_snapshots = [
                snapshot for snapshot in special_universe_snapshots if snapshot.symbol.upper() in tradable_set
            ]
            execution = self.executor.process_record(
                snapshots=tradable_snapshots,
                risk_decisions=risk_decisions,
                signal_sides_by_symbol={signal.symbol: signal.side for signal in signals},
                timestamp=record.timestamp,
            )
            decisions_by_symbol = {decision.trade_plan.symbol: decision for decision in risk_decisions}
            for signal in signals:
                report.add_signal(
                    date_key=date_key,
                    symbol=signal.symbol,
                    side=signal.side,
                    setup=signal.setup,
                    regime=current_regime,
                    confidence=signal.confidence,
                    market_cluster=cluster_for_symbol(self.special_config, signal.symbol),
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
            report.observe_open_exposure(list(self.executor.portfolio.open_positions.values()))
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
                report.add_closed_trade(
                    date_key=self._date_key(
                        trade.closed_at.isoformat() if trade.closed_at is not None else record.timestamp,
                        record.source_file,
                    ),
                    symbol=trade.symbol,
                    side=trade.side,
                    setup=getattr(trade, "setup", None),
                    confidence=getattr(trade, "confidence", None),
                    market_cluster=cluster_for_symbol(self.special_config, trade.symbol),
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
            last_snapshot_by_symbol.update({snapshot.symbol: snapshot for snapshot in tradable_snapshots})
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
            report.add_closed_trade(
                date_key=self._date_key(
                    trade.closed_at.isoformat() if trade.closed_at is not None else last_timestamp,
                    "finalize",
                ),
                symbol=trade.symbol,
                side=trade.side,
                setup=getattr(trade, "setup", None),
                confidence=getattr(trade, "confidence", None),
                market_cluster=cluster_for_symbol(self.special_config, trade.symbol),
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

        return SpecialSymbolsSlotBacktestResult(
            pod="special_symbols",
            slot="pod_b",
            tradable_symbols=list(self.selection.tradable_symbols),
            observe_only_symbols=list(self.selection.observe_only_symbols),
            observation_universe=list(self.selection.observation_universe),
            backtest=report.to_dict(),
        )

    def _planning_allocation(
        self,
        base: PodAllocation,
        signals: list[object],
    ) -> PodAllocation:
        if not signals:
            return PodAllocation(pod=PodName.POD_B, target_pct=base.target_pct, target_usd=base.target_usd)
        signal_symbols = list(dict.fromkeys(str(signal.symbol) for signal in signals))
        total_equity = max(self.main_config.trident.capital.reference_equity_usd, 1e-9)
        target_usd = round(max(base.target_usd, 0.0), 2)
        if target_usd <= 0:
            return PodAllocation(pod=PodName.POD_B, target_pct=0.0, target_usd=0.0)
        max_symbol_usd = (
            self.main_config.trident.capital.max_allocation_per_symbol_pct * total_equity
        )
        per_symbol_usd = min(target_usd / len(signal_symbols), max_symbol_usd)
        if per_symbol_usd <= 0:
            return PodAllocation(pod=PodName.POD_B, target_pct=0.0, target_usd=0.0)
        symbols = [
            SymbolAllocation(
                symbol=symbol,
                target_pct=round(per_symbol_usd / total_equity, 6),
                target_usd=round(per_symbol_usd, 2),
            )
            for symbol in signal_symbols
        ]
        allocated_usd = round(sum(item.target_usd for item in symbols), 2)
        return PodAllocation(
            pod=PodName.POD_B,
            target_pct=round(allocated_usd / total_equity, 6),
            target_usd=allocated_usd,
            symbols=symbols,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtest the special-symbols pod using the Pod B capital slot."
    )
    parser.add_argument("--main-config", default="config/trident.toml")
    parser.add_argument("--special-config", default="config/trident_special_symbols_taoxpl_shadow.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--tradable-symbols")
    parser.add_argument("--observe-only-symbols")
    return parser


def _parse_symbol_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main() -> None:
    args = build_parser().parse_args()
    result = SpecialSymbolsSlotBacktestRunner(
        load_config(args.main_config),
        load_config(args.special_config),
        tradable_symbols=_parse_symbol_list(args.tradable_symbols),
        observe_only_symbols=_parse_symbol_list(args.observe_only_symbols),
    ).run_jsonl(args.input)
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()
