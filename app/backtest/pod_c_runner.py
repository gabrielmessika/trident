from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from pathlib import Path

from app.backtest.pod_report import PodABacktestReport
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.directional_executor import DirectionalExecutor
from app.persistence.journal import (
    JsonlJournal,
    build_signal_journal_record,
    build_trade_journal_record,
)
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.supervisor import TridentSupervisor
from app.trident.types import RegimeSnapshot, RiskDecision, SymbolMarketSnapshot


@dataclass(slots=True)
class PodCBacktestResult:
    backtest: dict[str, object]
    output_path: str | None = None


class PodCBacktestRunner:
    """Replays market snapshots through Pod C using the shared directional rules."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.loader = SnapshotLoader()
        self.risk_gate = PodCRiskGate(config)
        self.executor = DirectionalExecutor(config)

    def run_jsonl(
        self,
        input_path: str | Path,
        output_path: str | Path | None = None,
    ) -> PodCBacktestResult:
        supervisor_config = replace(
            self.config,
            pod_a=replace(self.config.pod_a, enabled=False),
            pod_b=replace(self.config.pod_b, enabled=False),
            pod_c=replace(self.config.pod_c, enabled=True),
        )
        supervisor = TridentSupervisor(
            config=supervisor_config,
            profile="trident-pod-c-backtest",
            mode="observation",
        )
        output_journal = JsonlJournal(output_path) if output_path is not None else None
        report = PodABacktestReport()

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
            report.add_record_regime(current_regime)

            snapshots = [SymbolMarketSnapshot(**item) for item in record.symbols]
            previews = supervisor.preview_pod_c_signals(snapshots)
            trade_plans = supervisor.build_pod_c_trade_plans(snapshots)
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

            if output_journal is not None:
                fills_by_symbol: dict[str, list[dict[str, object]]] = {}
                for fill in execution.fills:
                    fills_by_symbol.setdefault(str(fill["symbol"]), []).append(fill)
                output_journal.append_many(
                    build_signal_journal_record(
                        timestamp=record.timestamp,
                        record_index=record.record_index,
                        regime=current_regime,
                        regime_snapshot=record.regime_snapshot,
                        symbol_snapshot=snapshot_by_symbol.get(preview.symbol),
                        source="pod_c_backtest",
                        signal={
                            "symbol": preview.symbol,
                            "side": preview.side,
                            "setup": preview.setup,
                            "confidence": preview.confidence,
                            "confidence_components": (
                                decisions_by_symbol[preview.symbol].trade_plan.confidence_components
                                if preview.symbol in decisions_by_symbol
                                else {}
                            ),
                            "risk": {
                                "accepted": decisions_by_symbol.get(preview.symbol).accepted
                                if preview.symbol in decisions_by_symbol
                                else False,
                                "reason": decisions_by_symbol.get(preview.symbol).reason
                                if preview.symbol in decisions_by_symbol
                                else "missing_trade_plan",
                            },
                            "execution": {
                                "opened": preview.symbol in execution.opened_symbols,
                                "skipped_open": preview.symbol in execution.skipped_open_symbols,
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
            decisions_by_symbol = {decision.trade_plan.symbol: decision for decision in risk_decisions}
            for symbol in execution.opened_symbols:
                decision = decisions_by_symbol.get(symbol)
                if decision is not None:
                    report.add_opened_setup(decision.trade_plan.setup)
            for symbol in execution.skipped_open_symbols:
                decision = decisions_by_symbol.get(symbol)
                if decision is not None:
                    report.add_skipped_open_setup(decision.trade_plan.setup)
            for trade in execution.closed_trades:
                if output_journal is not None:
                    output_journal.append(
                        build_trade_journal_record(
                            timestamp=record.timestamp,
                            record_index=record.record_index,
                            trade=self._trade_to_record(trade),
                            source="pod_c_backtest_trade",
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
                    stop_bps=getattr(trade, "stop_bps", None),
                    time_stop_hours=getattr(trade, "time_stop_hours", None),
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
        for trade in final_trades:
            if output_journal is not None:
                output_journal.append(
                    build_trade_journal_record(
                        timestamp=last_timestamp,
                        record_index=report.records_processed,
                        trade=self._trade_to_record(trade),
                        source="pod_c_backtest_trade",
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
                stop_bps=getattr(trade, "stop_bps", None),
                time_stop_hours=getattr(trade, "time_stop_hours", None),
                pnl_usd=trade.pnl_usd,
                gross_pnl_usd=trade.gross_pnl_usd,
                fees_usd=trade.fees_usd,
                close_reason=trade.close_reason,
                hold_hours=self._hold_hours(trade),
                opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
            )

        backtest = report.to_dict()
        if output_path is not None:
            backtest["output_path"] = str(output_path)
        return PodCBacktestResult(backtest=backtest, output_path=str(output_path) if output_path else None)

    def _date_key(self, timestamp: str | None, source_file: str) -> str:
        if timestamp:
            return timestamp[:10]
        return source_file[:10]

    def _hold_hours(self, trade: object) -> float | None:
        opened_at = getattr(trade, "opened_at", None)
        closed_at = getattr(trade, "closed_at", None)
        if opened_at is None or closed_at is None:
            return None
        return (closed_at - opened_at).total_seconds() / 3600.0

    def _trade_to_record(self, trade: object) -> dict[str, object]:
        return {
            "symbol": getattr(trade, "symbol"),
            "side": getattr(trade, "side"),
            "entry_price": getattr(trade, "entry_price"),
            "exit_price": getattr(trade, "exit_price"),
            "target_notional_usd": getattr(trade, "target_notional_usd"),
            "gross_pnl_usd": getattr(trade, "gross_pnl_usd"),
            "fees_usd": getattr(trade, "fees_usd"),
            "pnl_usd": getattr(trade, "pnl_usd"),
            "close_reason": getattr(trade, "close_reason"),
            "opened_at": getattr(trade, "opened_at").isoformat() if getattr(trade, "opened_at") else None,
            "closed_at": getattr(trade, "closed_at").isoformat() if getattr(trade, "closed_at") else None,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay snapshots through Pod C")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True, help="Snapshot JSONL file or directory")
    parser.add_argument("--output", help="Optional JSONL journal output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    config.pod_c.enabled = True
    result = PodCBacktestRunner(config).run_jsonl(args.input, output_path=args.output)
    for key in (
        "records_processed",
        "signal_count",
        "accepted_count",
        "rejected_count",
        "opened_count",
        "skipped_open_count",
        "closed_trade_count",
        "realized_pnl_usd",
        "signals_by_setup",
    ):
        print(f"{key}={result.backtest[key]}")
    if result.output_path:
        print(f"output_path={result.output_path}")


if __name__ == "__main__":
    main()
