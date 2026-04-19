from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_report import PodABacktestReport
from app.backtest.routing_replay import RoutingReplayRunner
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
from app.trident.types import (
    PodAllocation,
    PodName,
    RegimeSnapshot,
    SignalPreview,
    SymbolAllocation,
    SymbolMarketSnapshot,
)


def _merge_blocked_symbols(config: AppConfig, reserved_symbols: list[str]) -> AppConfig:
    existing = {str(symbol).strip().upper() for symbol in config.pod_a.blocked_symbols}
    merged = list(existing)
    for symbol in reserved_symbols:
        normalized = str(symbol).strip().upper()
        if normalized and normalized not in existing:
            existing.add(normalized)
            merged.append(normalized)
    return replace(config, pod_a=replace(config.pod_a, blocked_symbols=merged))


def _sum_by_date(*mappings: dict[str, float]) -> dict[str, float]:
    dates = sorted({date for mapping in mappings for date in mapping})
    return {
        date: round(sum(float(mapping.get(date, 0.0) or 0.0) for mapping in mappings), 2)
        for date in dates
    }


@dataclass(slots=True)
class FullBotSpecialReplacementResult:
    input_path: str
    dedupe_by_timestamp: bool
    records_processed: int
    duplicate_timestamps_skipped: int
    first_timestamp: str | None
    last_timestamp: str | None
    dates_covered: list[str]
    pod_a: dict[str, object]
    pod_special: dict[str, object]
    pod_c: dict[str, object]
    routing: dict[str, object]
    total_realized_pnl_usd: float
    directional_fees_usd: float
    total_activity_count: int
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FullBotSpecialReplacementRunner(FullBotBacktestRunner):
    """Integrated replay: Pod A + Pod C + the special-symbols pod in the old Pod B slot."""

    def __init__(
        self,
        main_config: AppConfig,
        special_config: AppConfig,
        *,
        reserved_symbols: list[str],
    ) -> None:
        blocked_config = _merge_blocked_symbols(main_config, reserved_symbols)
        blocked_config = replace(blocked_config, pod_b=replace(blocked_config.pod_b, enabled=False))
        super().__init__(blocked_config, force_enable_all_pods=False)
        runtime_config, selection = build_special_symbols_runtime_config(
            special_config,
            tradable_symbols=reserved_symbols,
        )
        self.slot_source_config = main_config
        self.special_runtime_config = runtime_config
        self.special_selection = selection
        self.special_context_service = MarketContextService(runtime_config)
        self.special_service = AnchorTrendService(runtime_config)
        self.special_planner = AnchorTrendPlanner(runtime_config)
        self.special_risk_gate = PodARiskGate(runtime_config)
        self.special_executor = DirectionalExecutor(runtime_config)
        self.loader = SnapshotLoader()

    def run_jsonl(
        self,
        input_path: str | Path,
        *,
        dedupe_by_timestamp: bool = True,
    ) -> FullBotSpecialReplacementResult:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-full-bot-special-replacement",
            mode="dry-run",
        )
        pod_a_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        pod_c_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        special_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        seen_timestamps: set[str] = set()
        latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        latest_special_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
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
            self._process_special_slot(
                supervisor=supervisor,
                report=special_report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
            latest_special_snapshots_by_symbol.update(
                {
                    snapshot.symbol: snapshot
                    for snapshot in snapshots
                    if snapshot.symbol.upper() in {
                        symbol.upper() for symbol in self.special_selection.tradable_symbols
                    }
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
            report=special_report,
            executor=self.special_executor,
            latest_snapshots=list(latest_special_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
            closed_trade_recorder=self._record_special_closed_trade,
        )

        pod_a = pod_a_report.to_dict()
        pod_c = pod_c_report.to_dict()
        pod_special = special_report.to_dict()
        routing = RoutingReplayRunner(self.config).run_jsonl(
            input_path=input_path,
            dedupe_by_timestamp=dedupe_by_timestamp,
        ).to_dict()
        return FullBotSpecialReplacementResult(
            input_path=str(input_path),
            dedupe_by_timestamp=dedupe_by_timestamp,
            records_processed=records_processed,
            duplicate_timestamps_skipped=duplicate_timestamps_skipped,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            dates_covered=sorted(dates_covered),
            pod_a=pod_a,
            pod_special=pod_special,
            pod_c=pod_c,
            routing=routing,
            total_realized_pnl_usd=round(
                float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
                + float(pod_special.get("realized_pnl_usd", 0.0) or 0.0)
                + float(pod_c.get("realized_pnl_usd", 0.0) or 0.0),
                4,
            ),
            directional_fees_usd=round(
                float(pod_a.get("fees_usd", 0.0) or 0.0)
                + float(pod_special.get("fees_usd", 0.0) or 0.0)
                + float(pod_c.get("fees_usd", 0.0) or 0.0),
                6,
            ),
            total_activity_count=(
                int(pod_a.get("closed_trade_count", 0) or 0)
                + int(pod_special.get("closed_trade_count", 0) or 0)
                + int(pod_c.get("closed_trade_count", 0) or 0)
            ),
            notes=[
                "Pod A is replayed with reserved symbols blocked.",
                "The special pod uses the Pod B capital slot allocations.",
                "Pod C is replayed in the same integrated run.",
                "Pod B is removed from routing to reflect a true replacement.",
            ],
        )

    def _process_special_slot(
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
        universe_set = {
            symbol.upper() for symbol in self.special_selection.observation_universe
        }
        tradable_set = {
            symbol.upper() for symbol in self.special_selection.tradable_symbols
        }
        special_universe_snapshots = [
            snapshot for snapshot in snapshots if snapshot.symbol.upper() in universe_set
        ]
        contexts = self.special_context_service.build_contexts(
            supervisor.state.regime,
            special_universe_snapshots,
            timestamp=timestamp,
        )
        tradable_contexts = [
            context for context in contexts if context.symbol.upper() in tradable_set
        ]
        signals = self.special_service.evaluate_many(tradable_contexts)
        previews = [
            SignalPreview(
                symbol=signal.symbol,
                side=signal.side,
                setup=signal.setup,
                confidence=signal.confidence,
            )
            for signal in signals
        ]
        pod_allocation = self._special_slot_allocation(supervisor.state.regime.value, signals)
        trade_plans = [
            plan
            for signal in signals
            if (plan := self.special_planner.build_trade_plan(signal, pod_allocation)) is not None
        ]
        date_key = self._date_key(timestamp, source_file)
        for plan in trade_plans:
            plan.setup_details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
        risk_decisions = self.special_risk_gate.evaluate_many(trade_plans)
        tradable_snapshots = [
            snapshot for snapshot in special_universe_snapshots if snapshot.symbol.upper() in tradable_set
        ]
        execution = self.special_executor.process_record(
            snapshots=tradable_snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
        )
        self._record_directional_tick(
            report=report,
            config=self.special_runtime_config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=risk_decisions,
            execution=execution,
            executor=self.special_executor,
            closed_trade_recorder=self._record_special_closed_trade,
        )

    def _slot_target_pct(self, regime_name: str) -> float:
        allocations = self.slot_source_config.trident.allocations
        if regime_name == "TrendExpansion":
            return float(allocations.trend_expansion.pod_b)
        if regime_name == "RangeAuction":
            return float(allocations.range_auction.pod_b)
        if regime_name == "PanicSqueeze":
            return float(allocations.panic_squeeze.pod_b)
        return float(allocations.dead_zone.pod_b)

    def _special_slot_allocation(
        self,
        regime_name: str,
        signals: list[object],
    ) -> PodAllocation:
        target_pct = max(self._slot_target_pct(regime_name), 0.0)
        total_equity = max(self.slot_source_config.trident.capital.reference_equity_usd, 1e-9)
        target_usd = round(target_pct * total_equity, 2)
        if not signals or target_usd <= 0:
            return PodAllocation(pod=PodName.POD_B, target_pct=target_pct, target_usd=target_usd)
        signal_symbols = list(dict.fromkeys(str(signal.symbol) for signal in signals))
        per_symbol_usd = min(
            target_usd / len(signal_symbols),
            self.slot_source_config.trident.capital.max_allocation_per_symbol_pct * total_equity,
        )
        if per_symbol_usd <= 0:
            return PodAllocation(pod=PodName.POD_B, target_pct=0.0, target_usd=0.0)
        allocated_usd = round(per_symbol_usd * len(signal_symbols), 2)
        return PodAllocation(
            pod=PodName.POD_B,
            target_pct=round(allocated_usd / total_equity, 6),
            target_usd=allocated_usd,
            symbols=[
                SymbolAllocation(
                    symbol=symbol,
                    target_pct=round(per_symbol_usd / total_equity, 6),
                    target_usd=round(per_symbol_usd, 2),
                )
                for symbol in signal_symbols
            ],
        )

    def _record_special_closed_trade(self, trade: object) -> None:
        closed_at = getattr(trade, "closed_at", None)
        self.special_risk_gate.record_closed_trade(
            symbol=str(getattr(trade, "symbol", "")),
            setup=getattr(trade, "setup", None),
            pnl_usd=getattr(trade, "pnl_usd", None),
            date_key=(closed_at.isoformat()[:10] if closed_at is not None else None),
        )


@dataclass(slots=True)
class FullBotIntegratedCompareResult:
    input_path: str
    compare_config: str
    special_config: str
    reserved_symbols: list[str]
    old_pod_b_full_bot: dict[str, object]
    special_replacement_full_bot: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _render_markdown(payload: FullBotIntegratedCompareResult) -> str:
    old_run = payload.old_pod_b_full_bot
    special = payload.special_replacement_full_bot
    lines = [
        "# Old Pod B Vs Special Replacement",
        "",
        f"- Input: `{payload.input_path}`",
        f"- Compare config: `{payload.compare_config}`",
        f"- Special config: `{payload.special_config}`",
        f"- Reserved symbols: `{', '.join(payload.reserved_symbols)}`",
        "",
        "| Scenario | Total PnL USD | Fees USD | Closed trades |",
        "|---|---:|---:|---:|",
        f"| Old Pod B full bot | {float(old_run.get('total_realized_pnl_usd', 0.0) or 0.0):.2f} | {float(old_run.get('directional_fees_usd', 0.0) or 0.0):.2f} | {int(old_run.get('total_activity_count', 0) or 0)} |",
        f"| Special replacement full bot | {float(special.get('total_realized_pnl_usd', 0.0) or 0.0):.2f} | {float(special.get('directional_fees_usd', 0.0) or 0.0):.2f} | {int(special.get('total_activity_count', 0) or 0)} |",
        "",
        "## Daily PnL",
        "",
        "| Date | Old Pod B Full Bot | Special Replacement | Delta |",
        "|---|---:|---:|---:|",
    ]
    old_total_by_date = _sum_by_date(
        {str(k): float(v or 0.0) for k, v in (old_run.get("pod_a", {}).get("pnl_by_date", {}) or {}).items()},
        {str(k): float(v or 0.0) for k, v in (old_run.get("pod_b", {}).get("pnl_by_date", {}) or {}).items()},
        {str(k): float(v or 0.0) for k, v in (old_run.get("pod_c", {}).get("pnl_by_date", {}) or {}).items()},
    )
    special_total_by_date = _sum_by_date(
        {str(k): float(v or 0.0) for k, v in (special.get("pod_a", {}).get("pnl_by_date", {}) or {}).items()},
        {str(k): float(v or 0.0) for k, v in (special.get("pod_special", {}).get("pnl_by_date", {}) or {}).items()},
        {str(k): float(v or 0.0) for k, v in (special.get("pod_c", {}).get("pnl_by_date", {}) or {}).items()},
    )
    for date in sorted(set(old_total_by_date) | set(special_total_by_date)):
        old_value = float(old_total_by_date.get(date, 0.0) or 0.0)
        special_value = float(special_total_by_date.get(date, 0.0) or 0.0)
        lines.append(f"| {date} | {old_value:.2f} | {special_value:.2f} | {(special_value - old_value):.2f} |")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare the legacy Pod B full bot against the integrated TAO/XPL replacement."
    )
    parser.add_argument("--compare-config", default="config/trident_compare_pod_b_slot.toml")
    parser.add_argument("--special-config", default="config/trident_special_symbols_taoxpl_shadow.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--reserved-symbols", default="TAO,XPL")
    parser.add_argument("--json-output")
    parser.add_argument("--md-output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    reserved_symbols = [item.strip().upper() for item in args.reserved_symbols.split(",") if item.strip()]
    compare_config = load_config(args.compare_config)
    special_config = load_config(args.special_config)
    old_pod_b = FullBotBacktestRunner(
        compare_config,
        force_enable_all_pods=False,
    ).run_jsonl(args.input).to_dict()
    special_replacement = FullBotSpecialReplacementRunner(
        compare_config,
        special_config,
        reserved_symbols=reserved_symbols,
    ).run_jsonl(args.input).to_dict()
    result = FullBotIntegratedCompareResult(
        input_path=str(args.input),
        compare_config=args.compare_config,
        special_config=args.special_config,
        reserved_symbols=reserved_symbols,
        old_pod_b_full_bot=old_pod_b,
        special_replacement_full_bot=special_replacement,
    )
    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    if args.md_output:
        md_path = Path(args.md_output)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_render_markdown(result), encoding="utf-8")
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()
