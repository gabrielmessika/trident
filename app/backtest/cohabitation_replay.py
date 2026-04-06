from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.backtest.snapshot_loader import SnapshotLoader
from app.reporting.multi_pod import build_cohabitation_summary
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, load_config
from app.trident.pod_b.paper_engine import PodBPaperEngine
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, SymbolMarketSnapshot


@dataclass(slots=True)
class CohabitationReplayResult:
    records_processed: int
    ownership_conflict_count: int
    ownership_conflicts: list[dict[str, str]]
    pod_a_owned_symbols: list[str]
    pod_b_owned_symbols: list[str]
    no_symbol_overlap: bool
    pod_a_signal_count: int
    pod_a_accepted_count: int
    pod_a_opened_count: int
    pod_a_closed_trade_count: int
    pod_a_realized_pnl_usd: float
    pod_b_total_fill_count: int
    pod_b_recent_fill_count: int
    pod_b_total_open_order_count: int
    pod_b_total_position_count: int
    pod_b_realized_pnl_usd: float
    pod_b_total_unrealized_pnl_usd: float
    output_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["summary"] = build_cohabitation_summary(self)
        return payload


class CohabitationReplayRunner:
    """Replays Pod A and Pod B together to validate ownership separation."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.loader = SnapshotLoader()
        self.risk_gate = PodARiskGate(config)
        self.executor = PodAExecutor(config)

    def run_jsonl(
        self,
        input_path: str | Path,
        output_path: str | Path | None = None,
    ) -> CohabitationReplayResult:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-cohabitation",
            mode="observation",
        )
        pod_a_report = PodABacktestReport()
        pod_b_engine = PodBPaperEngine(
            managed_symbols=supervisor.registry.symbols_for(PodName.POD_B),
            target_usd=supervisor.capital_plan.pod_allocations[PodName.POD_B].target_usd,
            config=self.config.pod_b,
        )
        pod_b_status = pod_b_engine.build_status(
            process_state="stopped",
            last_sync_reason="cohabitation_init",
            status_meta={"config_path": "", "status_path": ""},
        )

        last_snapshot_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        last_timestamp: str | None = None

        for record in self.loader.iter_jsonl(input_path):
            pod_a_report.records_processed += 1
            supervisor.apply_regime_snapshot(RegimeSnapshot(**record.regime_snapshot))
            snapshots = [SymbolMarketSnapshot(**item) for item in record.symbols]
            snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}

            pod_a_owned = set(supervisor.registry.symbols_for(PodName.POD_A))
            pod_b_owned = supervisor.registry.symbols_for(PodName.POD_B)
            pod_a_snapshots = [snapshot for snapshot in snapshots if snapshot.symbol in pod_a_owned]
            pod_b_snapshots = [snapshot for snapshot in snapshots if snapshot.symbol in pod_b_owned]

            previews = supervisor.preview_pod_a_signals(pod_a_snapshots)
            trade_plans = supervisor.build_pod_a_trade_plans(pod_a_snapshots)
            risk_decisions = self.risk_gate.evaluate_many(trade_plans)
            execution = self.executor.process_record(
                snapshots=pod_a_snapshots,
                risk_decisions=risk_decisions,
                signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
                timestamp=record.timestamp,
            )

            date_key = (record.timestamp or record.source_file)[:10]
            for preview in previews:
                pod_a_report.add_signal(
                    date_key=date_key,
                    symbol=preview.symbol,
                    side=preview.side,
                    setup=preview.setup,
                    regime=supervisor.state.regime.value,
                    confidence=preview.confidence,
                )
            for decision in risk_decisions:
                pod_a_report.add_decision(
                    date_key=date_key,
                    accepted=decision.accepted,
                    reason=decision.reason,
                )
            pod_a_report.add_execution_batch(
                opened_symbols=execution.opened_symbols,
                skipped_open_symbols=execution.skipped_open_symbols,
            )
            for trade in execution.closed_trades:
                pod_a_report.add_closed_trade(
                    date_key=date_key,
                    symbol=trade.symbol,
                    side=trade.side,
                    setup=getattr(trade, "setup", None),
                    confidence=getattr(trade, "confidence", None),
                    entry_price=getattr(trade, "entry_price", None),
                    exit_price=getattr(trade, "exit_price", None),
                    target_notional_usd=getattr(trade, "target_notional_usd", None),
                    stop_bps=getattr(trade, "stop_bps", None),
                    time_stop_hours=getattr(trade, "time_stop_hours", None),
                    pnl_usd=trade.pnl_usd,
                    gross_pnl_usd=trade.gross_pnl_usd,
                    fees_usd=trade.fees_usd,
                    close_reason=trade.close_reason,
                    hold_hours=None,
                    opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                    closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
                )

            pod_b_engine.update_allocation(
                managed_symbols=pod_b_owned,
                target_usd=supervisor.capital_plan.pod_allocations[PodName.POD_B].target_usd,
            )
            if pod_b_snapshots or pod_b_owned:
                pod_b_status, _ = pod_b_engine.process_record(
                    timestamp=record.timestamp,
                    snapshots=pod_b_snapshots,
                    status_meta={"config_path": "", "status_path": ""},
                    last_sync_reason="cohabitation_tick",
                )

            last_snapshot_by_symbol.update(snapshot_by_symbol)
            last_timestamp = record.timestamp

        final_trades, _ = self.executor.finalize(
            snapshots=list(last_snapshot_by_symbol.values()),
            timestamp=last_timestamp,
        )
        for trade in final_trades:
            pod_a_report.add_closed_trade(
                date_key=(last_timestamp or "finalize")[:10],
                symbol=trade.symbol,
                side=trade.side,
                setup=getattr(trade, "setup", None),
                confidence=getattr(trade, "confidence", None),
                entry_price=getattr(trade, "entry_price", None),
                exit_price=getattr(trade, "exit_price", None),
                target_notional_usd=getattr(trade, "target_notional_usd", None),
                stop_bps=getattr(trade, "stop_bps", None),
                time_stop_hours=getattr(trade, "time_stop_hours", None),
                pnl_usd=trade.pnl_usd,
                gross_pnl_usd=trade.gross_pnl_usd,
                fees_usd=trade.fees_usd,
                close_reason=trade.close_reason,
                hold_hours=None,
                opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
            )

        pod_b_status = pod_b_engine.build_status(
            process_state="stopped",
            last_sync_reason="cohabitation_completed",
            status_meta={"config_path": "", "status_path": ""},
        )

        ownership_conflicts = [
            {
                "symbol": conflict.symbol,
                "requested_by": conflict.requested_by.value,
                "owner": conflict.owner.value,
            }
            for conflict in supervisor.state.ownership_conflicts
        ]
        result = CohabitationReplayResult(
            records_processed=pod_a_report.records_processed,
            ownership_conflict_count=len(ownership_conflicts),
            ownership_conflicts=ownership_conflicts,
            pod_a_owned_symbols=supervisor.registry.symbols_for(PodName.POD_A),
            pod_b_owned_symbols=supervisor.registry.symbols_for(PodName.POD_B),
            no_symbol_overlap=set(supervisor.registry.symbols_for(PodName.POD_A)).isdisjoint(
                supervisor.registry.symbols_for(PodName.POD_B)
            ),
            pod_a_signal_count=pod_a_report.signal_count,
            pod_a_accepted_count=pod_a_report.accepted_count,
            pod_a_opened_count=pod_a_report.opened_count,
            pod_a_closed_trade_count=pod_a_report.closed_trade_count,
            pod_a_realized_pnl_usd=pod_a_report.realized_pnl_usd,
            pod_b_total_fill_count=pod_b_status.total_fill_count,
            pod_b_recent_fill_count=len(pod_b_status.recent_fills),
            pod_b_total_open_order_count=pod_b_status.total_open_order_count,
            pod_b_total_position_count=pod_b_status.total_position_count,
            pod_b_realized_pnl_usd=pod_b_status.realized_pnl_usd,
            pod_b_total_unrealized_pnl_usd=pod_b_status.total_unrealized_pnl_usd,
            output_path=str(output_path) if output_path is not None else None,
        )
        if output_path is not None:
            report_path = Path(output_path)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay Pod A and Pod B together on one snapshot stream")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = CohabitationReplayRunner(load_config(args.config)).run_jsonl(
        input_path=args.input,
        output_path=args.output,
    )
    print(f"records_processed={result.records_processed}")
    print(f"ownership_conflict_count={result.ownership_conflict_count}")
    print(f"pod_a_owned_symbols={result.pod_a_owned_symbols}")
    print(f"pod_b_owned_symbols={result.pod_b_owned_symbols}")
    print(f"no_symbol_overlap={result.no_symbol_overlap}")
    print(f"pod_a_signal_count={result.pod_a_signal_count}")
    print(f"pod_a_realized_pnl_usd={result.pod_a_realized_pnl_usd}")
    print(f"pod_b_total_fill_count={result.pod_b_total_fill_count}")
    print(f"pod_b_realized_pnl_usd={result.pod_b_realized_pnl_usd}")
    print(f"pod_b_total_unrealized_pnl_usd={result.pod_b_total_unrealized_pnl_usd}")


if __name__ == "__main__":
    main()
