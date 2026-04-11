from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.pod_report import PodABacktestReport
from app.backtest.routing_replay import RoutingReplayRunner
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.directional_executor import DirectionalExecutor
from app.reporting.pod_b import PodBReport
from app.risk.pod_a_gate import PodARiskGate
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.pod_b.paper_engine import PodBPaperEngine
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, RiskDecision, SymbolMarketSnapshot


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
    report_path: str | None = None
    summary_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FullBotBacktestRunner:
    """Runs Pod A, Pod B, and Pod C together on one snapshot stream."""

    def __init__(self, config: AppConfig, *, force_enable_all_pods: bool = True) -> None:
        self.config = self._runtime_config(config, force_enable_all_pods=force_enable_all_pods)
        self.loader = SnapshotLoader()
        self.pod_a_risk_gate = PodARiskGate(self.config)
        self.pod_c_risk_gate = PodCRiskGate(self.config)
        self.pod_a_executor = DirectionalExecutor(self.config)
        self.pod_c_executor = DirectionalExecutor(self.config)

    def _runtime_config(self, config: AppConfig, *, force_enable_all_pods: bool) -> AppConfig:
        if not force_enable_all_pods:
            return config
        return replace(
            config,
            pod_a=replace(config.pod_a, enabled=True),
            pod_b=replace(config.pod_b, enabled=True),
            pod_c=replace(config.pod_c, enabled=True),
        )

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
        pod_b_report = PodBReport()
        pod_b_engine = PodBPaperEngine(
            managed_symbols=[],
            target_usd=0.0,
            config=self.config.pod_b,
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

            snapshots = [SymbolMarketSnapshot(**item) for item in record.symbols]
            latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
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
                engine=pod_b_engine,
                snapshots=snapshots,
                timestamp=timestamp,
                regime_snapshot=record.regime_snapshot,
            )
            records_processed += 1

        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_a_report,
            executor=self.pod_a_executor,
            latest_snapshots=list(latest_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
        )
        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_c_report,
            executor=self.pod_c_executor,
            latest_snapshots=list(latest_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
        )

        routing = RoutingReplayRunner(self.config).run_jsonl(
            input_path=input_path,
            dedupe_by_timestamp=dedupe_by_timestamp,
        ).to_dict()
        pod_a = pod_a_report.to_dict()
        pod_b = pod_b_report.to_dict()
        pod_c = pod_c_report.to_dict()
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
                + float(pod_b.get("realized_pnl_usd", 0.0))
                + float(pod_c.get("realized_pnl_usd", 0.0)),
                4,
            ),
            directional_fees_usd=round(
                float(pod_a.get("fees_usd", 0.0))
                + float(pod_c.get("fees_usd", 0.0)),
                6,
            ),
            total_activity_count=(
                int(pod_a.get("closed_trade_count", 0))
                + int(pod_b.get("total_fill_count", 0))
                + int(pod_c.get("closed_trade_count", 0))
            ),
            notes=[
                "directional_fees_usd ne couvre que Pod A et Pod C; les frais Pod B ne sont pas exposes separement dans PodBReport.",
                "total_activity_count additionne les trades clotures de Pod A/Pod C et les fills Pod B; c'est un indicateur d'activite, pas un nombre homogene de trades.",
            ],
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
        risk_decisions = self.pod_a_risk_gate.evaluate_many(trade_plans)
        execution = self.pod_a_executor.process_record(
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            allowed_symbols=supervisor.allowed_symbols_for(PodName.POD_A),
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=risk_decisions,
            execution=execution,
            executor=self.pod_a_executor,
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
        risk_decisions = self.pod_c_risk_gate.evaluate_many(trade_plans)
        execution = self.pod_c_executor.process_record(
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            allowed_symbols=supervisor.allowed_symbols_for(PodName.POD_C),
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=risk_decisions,
            execution=execution,
            executor=self.pod_c_executor,
        )

    def _process_pod_b(
        self,
        *,
        supervisor: TridentSupervisor,
        report: PodBReport,
        engine: PodBPaperEngine,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
        regime_snapshot: dict[str, object],
    ) -> None:
        pod_b_allocation = supervisor.capital_plan.pod_allocations[PodName.POD_B]
        pod_b_owned = supervisor.registry.symbols_for(PodName.POD_B)
        engine.update_allocation(
            managed_symbols=pod_b_owned,
            target_usd=pod_b_allocation.target_usd,
        )
        pod_b_snapshots = [snapshot for snapshot in snapshots if snapshot.symbol in pod_b_owned]
        status, fills = engine.process_record(
            timestamp=timestamp,
            snapshots=pod_b_snapshots,
            status_meta={"config_path": "", "status_path": ""},
            regime_snapshot=regime_snapshot,
            last_sync_reason="full_bot_backtest_tick",
        )
        report.add_tick(timestamp=timestamp, status=status, fills=fills)

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
                pnl_usd=trade.pnl_usd,
                gross_pnl_usd=trade.gross_pnl_usd,
                fees_usd=trade.fees_usd,
                close_reason=trade.close_reason,
                hold_hours=self._hold_hours(trade),
                opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
            )

    def _finalize_directional_report(
        self,
        *,
        supervisor: TridentSupervisor,
        report: PodABacktestReport,
        executor: DirectionalExecutor,
        latest_snapshots: list[SymbolMarketSnapshot],
        last_timestamp: str | None,
    ) -> None:
        final_trades, _ = executor.finalize(
            snapshots=latest_snapshots,
            timestamp=last_timestamp,
        )
        for trade in final_trades:
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
                pnl_usd=trade.pnl_usd,
                gross_pnl_usd=trade.gross_pnl_usd,
                fees_usd=trade.fees_usd,
                close_reason=trade.close_reason,
                hold_hours=self._hold_hours(trade),
                opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
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

    def _comparison_entry(self, result: FullBotBacktestResult) -> dict[str, object]:
        pod_a = result.pod_a
        pod_b = result.pod_b
        pod_c = result.pod_c
        routing = result.routing
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
            "pod_b_total_fill_count": pod_b.get("total_fill_count", 0),
            "pod_c_closed_trade_count": pod_c.get("closed_trade_count", 0),
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
        return (
            "# TRIDENT full-bot backtest\n\n"
            f"- input: `{result.input_path}`\n"
            f"- dates: `{', '.join(result.dates_covered)}`\n"
            f"- records_processed: `{result.records_processed}`\n"
            f"- duplicate_timestamps_skipped: `{result.duplicate_timestamps_skipped}`\n"
            f"- total_realized_pnl_usd: `{result.total_realized_pnl_usd}`\n"
            f"- directional_fees_usd: `{result.directional_fees_usd}`\n"
            "\n"
            "## PnL par pod\n\n"
            f"- Pod A realized_pnl_usd: `{pod_a.get('realized_pnl_usd', 0.0)}`\n"
            f"- Pod B realized_pnl_usd: `{pod_b.get('realized_pnl_usd', 0.0)}`\n"
            f"- Pod C realized_pnl_usd: `{pod_c.get('realized_pnl_usd', 0.0)}`\n"
            "\n"
            "## Activite\n\n"
            f"- Pod A closed_trade_count: `{pod_a.get('closed_trade_count', 0)}`\n"
            f"- Pod B total_fill_count: `{pod_b.get('total_fill_count', 0)}`\n"
            f"- Pod C closed_trade_count: `{pod_c.get('closed_trade_count', 0)}`\n"
            f"- total_activity_count: `{result.total_activity_count}`\n"
            f"- routing reassignment_event_count: `{routing.get('reassignment_event_count', 0)}`\n"
            f"- routing max_ownership_conflict_count: `{routing.get('max_ownership_conflict_count', 0)}`\n"
            "\n"
            "## Notes\n\n"
            + "".join(f"- {note}\n" for note in result.notes)
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
        help="Do not force-enable all pods for this backtest.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = FullBotBacktestRunner(
        load_config(args.config),
        force_enable_all_pods=not args.respect_config_enabled,
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
