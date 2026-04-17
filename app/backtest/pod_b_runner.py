from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path

from app.backtest.pod_report import PodABacktestReport
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.directional_executor import DirectionalExecutor
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.pod_b import (
    BreakoutContext,
    BreakoutPlanner,
    BreakoutService,
    PodBRiskGate,
    ReplayFeatureEnricher,
)
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodAllocation, PodName, RegimeSnapshot, SignalPreview, SymbolAllocation, SymbolMarketSnapshot


@dataclass(slots=True)
class PodBBacktestResult:
    backtest: dict[str, object]
    output_path: str | None = None


class PodBBacktestRunner:
    """Replay-only runner for the official directional Pod B strategy."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.loader = SnapshotLoader()
        self.service = BreakoutService(config)
        self.planner = BreakoutPlanner(config)
        self.risk_gate = PodBRiskGate(config)
        self.executor = DirectionalExecutor(config)
        self.replay_enricher = ReplayFeatureEnricher()

    def run_jsonl(
        self,
        input_path: str | Path,
        output_path: str | Path | None = None,
    ) -> PodBBacktestResult:
        supervisor_config = replace(
            self.config,
            pod_a=replace(self.config.pod_a, enabled=False),
            pod_b=replace(self.config.pod_b, enabled=True),
            pod_c=replace(self.config.pod_c, enabled=False),
        )
        supervisor = TridentSupervisor(
            config=supervisor_config,
            profile="trident-pod-b-bis-backtest",
            mode="observation",
        )
        report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        last_snapshot_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        last_timestamp: str | None = None

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
            snapshots = [SymbolMarketSnapshot(**item) for item in record.symbols]
            snapshots = self.replay_enricher.enrich_many(snapshots)
            supervisor.refresh_symbol_routing(snapshots)
            opening_symbols = supervisor.opening_symbols_for(PodName.POD_B)
            managed_symbols = supervisor.managed_symbols_for(
                PodName.POD_B,
                active_symbols=self.executor.portfolio.open_positions.keys(),
            )
            contexts = [
                BreakoutContext(
                    symbol=snapshot.symbol,
                    regime=current_regime,
                    price=snapshot.price,
                    ema_fast=snapshot.ema_fast,
                    ema_slow=snapshot.ema_slow,
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
                    delta_book_imbalance=snapshot.delta_book_imbalance,
                    delta_trade_flow_bias=snapshot.delta_trade_flow_bias,
                    volume_ratio=snapshot.volume_ratio,
                    trade_count_ratio=snapshot.trade_count_ratio,
                    realized_vol_short_bps=snapshot.realized_vol_short_bps,
                    realized_vol_long_bps=snapshot.realized_vol_long_bps,
                    compression_score=snapshot.compression_score,
                    microprice_dislocation_bps=snapshot.microprice_dislocation_bps,
                )
                for snapshot in snapshots
                if snapshot.symbol in opening_symbols
            ]
            signals = self.service.evaluate_many(contexts)
            previews = [
                SignalPreview(
                    symbol=signal.symbol,
                    side=signal.side,
                    setup=signal.setup,
                    confidence=signal.confidence,
                )
                for signal in signals
            ]
            allocation = self._planning_allocation(
                supervisor.capital_plan.pod_allocations[PodName.POD_B],
                signals,
            )
            trade_plans = [
                plan
                for signal in signals
                if (plan := self.planner.build_trade_plan(signal, allocation)) is not None
            ]
            current_open_positions = list(self.executor.portfolio.open_positions.values())
            risk_decisions = self.risk_gate.evaluate_many(
                trade_plans,
                current_open_expected_loss_usd=sum(
                    max(float(getattr(position, "expected_loss_usd", 0.0)), 0.0)
                    for position in current_open_positions
                ),
                current_open_position_count=len(current_open_positions),
            )
            execution = self.executor.process_record(
                snapshots=snapshots,
                risk_decisions=risk_decisions,
                signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
                timestamp=record.timestamp,
                entry_allowed_symbols=opening_symbols,
                managed_symbols=managed_symbols,
            )
            decisions_by_symbol = {decision.trade_plan.symbol: decision for decision in risk_decisions}
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
            )

        payload = report.to_dict()
        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return PodBBacktestResult(
            backtest=payload,
            output_path=str(output_path) if output_path is not None else None,
        )

    def _planning_allocation(
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
    parser = argparse.ArgumentParser(description="Replay the directional Pod B strategy on snapshot JSONL data")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = PodBBacktestRunner(load_config(args.config)).run_jsonl(
        input_path=args.input,
        output_path=args.output,
    )
    payload = result.backtest
    print(f"records_processed={payload.get('records_processed', 0)}")
    print(f"signal_count={payload.get('signal_count', 0)}")
    print(f"accepted_count={payload.get('accepted_count', 0)}")
    print(f"closed_trade_count={payload.get('closed_trade_count', 0)}")
    print(f"realized_pnl_usd={payload.get('realized_pnl_usd', 0.0)}")
    if result.output_path:
        print(f"output_path={result.output_path}")


if __name__ == "__main__":
    main()
