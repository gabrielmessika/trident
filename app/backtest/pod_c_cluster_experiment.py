from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from app.backtest.pod_c_runner import PodCBacktestRunner
from app.backtest.pod_report import PodABacktestReport
from app.backtest.snapshot_loader import SnapshotLoader
from app.execution.directional_executor import DirectionalExecutor
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.pod_c import TradfiTrendContextService, TradfiTrendPlanner, TradfiTrendService
from app.trident.pod_c.signals import TradfiTrendContext, TradfiTrendSignal
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, SymbolMarketSnapshot


@dataclass(slots=True)
class ScenarioResult:
    name: str
    summary: dict[str, object]
    backtest: dict[str, object]


class ClusterAwareTradfiService(TradfiTrendService):
    """Backtest-only Pod C service for cluster-specific pattern experiments."""

    def __init__(self, config, *, scenario: str) -> None:
        super().__init__(config)
        self._scenario = scenario

    def evaluate(self, context: TradfiTrendContext) -> TradfiTrendSignal | None:
        signal = super().evaluate(context)
        if signal is None:
            return None
        cluster = str(context.market_cluster).strip().lower()
        if self._scenario == "oil_only_v1":
            return signal if self._allow_oil_only(context, signal, cluster) else None
        if self._scenario == "oil_silver_v1":
            return signal if self._allow_oil_silver(context, signal, cluster) else None
        if self._scenario == "oil_silver_index_v1":
            return signal if self._allow_oil_silver_index(context, signal, cluster) else None
        return signal

    def _allow_oil_only(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
        cluster: str,
    ) -> bool:
        if cluster != "oil":
            return False
        return self._is_oil_long_pullback(context, signal) or self._is_oil_short_breakdown(context, signal)

    def _allow_oil_silver(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
        cluster: str,
    ) -> bool:
        if cluster == "oil":
            return self._is_oil_long_pullback(context, signal) or self._is_oil_short_breakdown(context, signal)
        if cluster == "silver":
            return self._is_silver_breakout_long(context, signal)
        return False

    def _allow_oil_silver_index(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
        cluster: str,
    ) -> bool:
        if self._allow_oil_silver(context, signal, cluster):
            return True
        if cluster == "index":
            return self._is_index_breakout_long(context, signal)
        return False

    def _is_oil_long_pullback(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        return (
            signal.setup == "tradfi_continuation_long"
            and signal.side == "long"
            and context.trend_bps >= 8.0
            and context.structure_score >= 0.18
            and context.trade_flow_bias >= 0.02
            and context.vwap_distance_bps <= -0.5
            and context.vwap_distance_bps >= -4.0
            and context.bucket_range_bps >= 18.0
            and context.spread_bps <= 3.0
        )

    def _is_oil_short_breakdown(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        return (
            signal.setup == "tradfi_continuation_short"
            and signal.side == "short"
            and context.trend_bps <= -10.0
            and context.structure_score <= -0.20
            and context.trade_flow_bias <= -0.05
            and context.vwap_distance_bps <= -1.0
            and context.bucket_range_bps >= 20.0
            and context.spread_bps <= 3.0
        )

    def _is_silver_breakout_long(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        return (
            signal.setup == "tradfi_continuation_long"
            and signal.side == "long"
            and context.trend_bps >= 10.0
            and context.structure_score >= 0.20
            and context.trade_flow_bias >= 0.03
            and context.vwap_distance_bps >= 1.0
            and context.vwap_distance_bps <= 6.0
            and context.bucket_range_bps >= 18.0
            and context.spread_bps <= 2.0
        )

    def _is_index_breakout_long(
        self,
        context: TradfiTrendContext,
        signal: TradfiTrendSignal,
    ) -> bool:
        return (
            signal.setup == "tradfi_continuation_long"
            and signal.side == "long"
            and context.trend_bps >= 8.0
            and context.structure_score >= 0.18
            and context.trade_flow_bias >= 0.02
            and context.vwap_distance_bps >= 1.0
            and context.vwap_distance_bps <= 6.0
            and context.bucket_range_bps >= 16.0
            and context.spread_bps <= 2.5
        )


class ExperimentalPodCBacktestRunner:
    """Minimal Pod C runner that swaps in a custom Tradfi service."""

    def __init__(self, config: AppConfig, service: TradfiTrendService) -> None:
        self.config = config
        self.service = service
        self.loader = SnapshotLoader()
        self.risk_gate = PodCRiskGate(config)
        self.executor = DirectionalExecutor(config)

    def run_jsonl(self, input_path: str | Path) -> dict[str, object]:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-pod-c-cluster-experiment",
            mode="observation",
        )
        supervisor.pod_c_service = self.service
        supervisor.pod_c_context_service = TradfiTrendContextService(self.config, self.service)
        supervisor.pod_c_planner = TradfiTrendPlanner(self.config)

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
        return report.to_dict()

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


def summarize_backtest(payload: dict[str, object]) -> dict[str, object]:
    closed_trade_count = int(payload.get("closed_trade_count", 0) or 0)
    win_count = int(payload.get("win_count", 0) or 0)
    return {
        "signal_count": int(payload.get("signal_count", 0) or 0),
        "accepted_count": int(payload.get("accepted_count", 0) or 0),
        "closed_trade_count": closed_trade_count,
        "win_rate": round(win_count / closed_trade_count, 4) if closed_trade_count else 0.0,
        "realized_pnl_usd": round(float(payload.get("realized_pnl_usd", 0.0) or 0.0), 4),
        "gross_pnl_usd": round(float(payload.get("gross_pnl_usd", 0.0) or 0.0), 4),
        "fees_usd": round(float(payload.get("fees_usd", 0.0) or 0.0), 4),
        "max_drawdown_usd": round(float(payload.get("max_drawdown_usd", 0.0) or 0.0), 4),
        "pnl_by_symbol": dict(payload.get("pnl_by_symbol", {}) or {}),
        "trades_by_symbol": dict(payload.get("trades_by_symbol", {}) or {}),
        "pnl_by_setup": dict(payload.get("pnl_by_setup", {}) or {}),
    }


def run_scenarios(config: AppConfig, input_path: str | Path) -> list[ScenarioResult]:
    scenarios = [
        ("baseline_current", None),
        ("oil_only_v1", "oil_only_v1"),
        ("oil_silver_v1", "oil_silver_v1"),
        ("oil_silver_index_v1", "oil_silver_index_v1"),
    ]
    results: list[ScenarioResult] = []
    for name, scenario in scenarios:
        if scenario is None:
            backtest = PodCBacktestRunner(config).run_jsonl(input_path).backtest
        else:
            service = ClusterAwareTradfiService(config.pod_c, scenario=scenario)
            backtest = ExperimentalPodCBacktestRunner(config, service).run_jsonl(input_path)
        results.append(ScenarioResult(name=name, summary=summarize_backtest(backtest), backtest=backtest))
    return results


def build_payload(results: list[ScenarioResult]) -> dict[str, object]:
    ranked = sorted(
        results,
        key=lambda item: (
            float(item.summary["realized_pnl_usd"]),
            -float(item.summary["max_drawdown_usd"]),
            -float(item.summary["fees_usd"]),
        ),
        reverse=True,
    )
    return {
        "scenario_count": len(results),
        "best_scenario": ranked[0].name if ranked else None,
        "leaderboard": [
            {
                "name": item.name,
                "realized_pnl_usd": item.summary["realized_pnl_usd"],
                "max_drawdown_usd": item.summary["max_drawdown_usd"],
                "fees_usd": item.summary["fees_usd"],
                "signal_count": item.summary["signal_count"],
                "closed_trade_count": item.summary["closed_trade_count"],
                "win_rate": item.summary["win_rate"],
            }
            for item in ranked
        ],
        "scenarios": {item.name: item.summary for item in results},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare Pod C cluster-aware pattern variants")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = build_payload(run_scenarios(load_config(args.config), args.input))
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
