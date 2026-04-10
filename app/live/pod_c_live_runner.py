from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.pod_report import PodABacktestReport
from app.execution.directional_executor import DirectionalExecutor
from app.live.collector import HyperliquidLiveCollector
from app.live.runtime_status import write_runtime_status
from app.persistence.journal import JsonlJournal, build_signal_journal_record, build_trade_journal_record
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, RiskDecision, SymbolMarketSnapshot

logger = logging.getLogger(__name__)


class PodCLiveRunner:
    """Runs Pod C on the native Hyperliquid collector using shared dry-run rules."""

    def __init__(self, config: AppConfig, coins: list[str] | None = None) -> None:
        selected_coins = (
            coins
            or config.pod_c.symbols
            or config.hyperliquid.observation_universe
            or config.hyperliquid.default_coins
        )
        self.coins = [str(coin).strip().upper() for coin in selected_coins if str(coin).strip()]
        self.config = replace(
            config,
            hyperliquid=replace(
                config.hyperliquid,
                observation_universe=list(self.coins),
            ),
        )
        self.collector = HyperliquidLiveCollector(self.config, coins=self.coins)
        self.supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-live-pod-c",
            mode="dry-run",
        )
        self.risk_gate = PodCRiskGate(self.config)
        self.executor = DirectionalExecutor(self.config)
        self.report = PodABacktestReport()
        self._latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}

    STATUS_HEARTBEAT_SECONDS = 60.0

    async def run(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
        journal_path: str | Path | None = None,
    ) -> dict[str, object]:
        journal = JsonlJournal(journal_path, truncate=True) if journal_path is not None else None
        status_path = Path("logs/pod_c_live_status.json")
        self._write_runtime_status(status_path)

        heartbeat_task = asyncio.create_task(
            self._status_heartbeat_loop(status_path)
        )
        try:
            async for record in self.collector.iter_records(
                max_runtime_seconds=max_runtime_seconds,
                max_messages=max_messages,
            ):
                self.collector.stats.snapshots_written += len(self.collector.writer.append_many([record]))
                self._process_record(record, journal=journal)
                self._write_runtime_status(status_path)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        final_records = self.collector.builder.finalize()
        self.collector.stats.snapshots_written += len(self.collector.writer.append_many(final_records))
        for record in final_records:
            self._process_record(record, journal=journal)
            self._write_runtime_status(status_path)

        result = self.report.to_dict()
        result["collector"] = {
            "coins": self.collector.coins,
            "messages_processed": self.collector.stats.messages_processed,
            "snapshots_written": self.collector.stats.snapshots_written,
            "reconnect_count": self.collector.stats.reconnect_count,
            "heartbeat_count": self.collector.stats.heartbeat_count,
            "pong_count": self.collector.stats.pong_count,
            "timeout_count": self.collector.stats.timeout_count,
            "api_error_count": self.collector.stats.api_error_count,
            "rate_limit_error_count": self.collector.stats.rate_limit_error_count,
            "last_error": self.collector.stats.last_error,
            "snapshot_output_dir": self.config.hyperliquid.snapshot_output_dir,
        }
        result["journal_path"] = str(journal_path) if journal_path is not None else None
        self._write_runtime_status(status_path)
        return result

    def _process_record(
        self,
        record: dict[str, object],
        *,
        journal: JsonlJournal | None,
    ) -> None:
        timestamp = str(record.get("timestamp"))
        date_key = timestamp[:10]
        regime_snapshot = record.get("regime_snapshot", {})
        symbols = record.get("symbols", [])
        if not isinstance(regime_snapshot, dict) or not isinstance(symbols, list):
            return

        self.report.records_processed += 1
        self.report.add_record_date(date_key)
        previous_regime = self.supervisor.state.regime.value
        self.supervisor.apply_regime_snapshot(RegimeSnapshot(**regime_snapshot))
        current_regime = self.supervisor.state.regime.value
        if current_regime != previous_regime:
            self.report.add_regime_transition(
                date_key=date_key,
                previous_regime=previous_regime,
                new_regime=current_regime,
            )
        self.report.add_record_regime(current_regime)

        snapshots = [SymbolMarketSnapshot(**item) for item in symbols if isinstance(item, dict)]
        self._latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
        previews = self.supervisor.preview_pod_c_signals(snapshots)
        trade_plans = self.supervisor.build_pod_c_trade_plans(snapshots)
        risk_decisions = self.risk_gate.evaluate_many(trade_plans)
        execution = self.executor.process_record(
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            allowed_symbols=self.supervisor.allowed_symbols_for(PodName.POD_C),
        )

        decisions_by_symbol: dict[str, RiskDecision] = {
            decision.trade_plan.symbol: decision for decision in risk_decisions
        }
        snapshot_by_symbol = {
            item["symbol"]: item for item in symbols if isinstance(item, dict) and "symbol" in item
        }
        fills_by_symbol: dict[str, list[dict[str, object]]] = {}
        for fill in execution.fills:
            fills_by_symbol.setdefault(str(fill["symbol"]), []).append(fill)

        for preview in previews:
            self.report.add_signal(
                date_key=date_key,
                symbol=preview.symbol,
                side=preview.side,
                setup=preview.setup,
                regime=current_regime,
                confidence=preview.confidence,
                market_cluster=cluster_for_symbol(self.config, preview.symbol),
            )
            if journal is not None:
                journal.append(
                    build_signal_journal_record(
                        timestamp=timestamp,
                        record_index=self.report.records_processed,
                        regime=current_regime,
                        regime_snapshot=regime_snapshot,
                        symbol_snapshot=snapshot_by_symbol.get(preview.symbol),
                        source="pod_c_live_signal",
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
                )

        for decision in risk_decisions:
            self.report.add_decision(
                date_key=date_key,
                setup=decision.trade_plan.setup,
                accepted=decision.accepted,
                reason=decision.reason,
            )
        self.report.add_execution_batch(
            opened_symbols=execution.opened_symbols,
            skipped_open_symbols=execution.skipped_open_symbols,
        )
        decisions_by_symbol = {decision.trade_plan.symbol: decision for decision in risk_decisions}
        for symbol in execution.opened_symbols:
            decision = decisions_by_symbol.get(symbol)
            if decision is not None:
                self.report.add_opened_setup(decision.trade_plan.setup)
        for symbol in execution.skipped_open_symbols:
            decision = decisions_by_symbol.get(symbol)
            if decision is not None:
                self.report.add_skipped_open_setup(decision.trade_plan.setup)
        for trade in execution.closed_trades:
            if journal is not None:
                journal.append(
                    build_trade_journal_record(
                        timestamp=trade.closed_at.isoformat() if trade.closed_at else timestamp,
                        record_index=self.report.records_processed,
                        trade=self._trade_to_record(trade),
                        source="pod_c_live_trade",
                    )
                )
            self.report.add_closed_trade(
                date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else "unknown"),
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
        self._emit_review_summary(
            timestamp=timestamp,
            regime=current_regime,
            previews=previews,
            trade_plans=trade_plans,
            risk_decisions=risk_decisions,
            execution=execution,
        )

    def _hold_hours(self, trade: object) -> float | None:
        opened_at = getattr(trade, "opened_at", None)
        closed_at = getattr(trade, "closed_at", None)
        if opened_at is None or closed_at is None:
            return None
        return (closed_at - opened_at).total_seconds() / 3600.0

    def _emit_review_summary(
        self,
        *,
        timestamp: str,
        regime: str,
        previews: list[object],
        trade_plans: list[object],
        risk_decisions: list[RiskDecision],
        execution: object,
    ) -> None:
        record_index = self.report.records_processed
        should_log = (
            record_index == 1
            or record_index % 30 == 0
            or bool(previews)
            or bool(execution.opened_symbols)
            or bool(execution.closed_trades)
            or bool(execution.skipped_open_symbols)
        )
        if not should_log:
            return
        tradable_count = sum(
            1 for item in self.supervisor.state.observed_symbol_status if item.tradable
        )
        owned_symbols = self.supervisor.registry.symbols_for(PodName.POD_C)
        accepted_count = sum(1 for decision in risk_decisions if decision.accepted)
        logger.info(
            "Pod C review summary; ts=%s regime=%s tradable_count=%s owned_symbols=%s previews=%s trade_plans=%s accepted=%s opened=%s skipped=%s closed=%s open_positions=%s realized_pnl_usd=%.2f",
            timestamp,
            regime,
            tradable_count,
            owned_symbols,
            len(previews),
            len(trade_plans),
            accepted_count,
            len(execution.opened_symbols),
            len(execution.skipped_open_symbols),
            len(execution.closed_trades),
            len(self.executor.portfolio.open_positions),
            self.report.realized_pnl_usd,
        )

    def _trade_to_record(self, trade: object) -> dict[str, object]:
        return {
            "symbol": getattr(trade, "symbol"),
            "side": getattr(trade, "side"),
            "setup": getattr(trade, "setup", None),
            "open_reason": getattr(trade, "setup", None),
            "confidence": getattr(trade, "confidence", None),
            "entry_price": getattr(trade, "entry_price"),
            "exit_price": getattr(trade, "exit_price"),
            "target_notional_usd": getattr(trade, "target_notional_usd"),
            "margin_usd": getattr(trade, "margin_usd", None),
            "leverage": getattr(trade, "effective_leverage", None),
            "effective_leverage": getattr(trade, "effective_leverage", None),
            "risk_budget_usd": getattr(trade, "risk_budget_usd", None),
            "expected_loss_usd": getattr(trade, "expected_loss_usd", None),
            "invalidation_price": getattr(trade, "invalidation_price", None),
            "stop_bps": getattr(trade, "stop_bps", None),
            "time_stop_hours": getattr(trade, "time_stop_hours", None),
            "take_profit_bps": getattr(trade, "take_profit_bps", None),
            "break_even_trigger_bps": getattr(trade, "break_even_trigger_bps", None),
            "trailing_activation_bps": getattr(trade, "trailing_activation_bps", None),
            "trailing_distance_bps": getattr(trade, "trailing_distance_bps", None),
            "gross_pnl_usd": getattr(trade, "gross_pnl_usd"),
            "fees_usd": getattr(trade, "fees_usd"),
            "pnl_usd": getattr(trade, "pnl_usd"),
            "close_reason": getattr(trade, "close_reason"),
            "opened_at": getattr(trade, "opened_at").isoformat() if getattr(trade, "opened_at") else None,
            "closed_at": getattr(trade, "closed_at").isoformat() if getattr(trade, "closed_at") else None,
        }

    async def _status_heartbeat_loop(self, path: str | Path) -> None:
        while True:
            await asyncio.sleep(self.STATUS_HEARTBEAT_SECONDS)
            self._write_runtime_status(path)

    def _write_runtime_status(self, path: str | Path) -> None:
        write_runtime_status(
            path,
            {
                "pod": "pod_c",
                "process_state": "running",
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "collector": {
                    "coins": self.collector.coins,
                    "messages_processed": self.collector.stats.messages_processed,
                    "snapshots_written": self.collector.stats.snapshots_written,
                    "reconnect_count": self.collector.stats.reconnect_count,
                    "heartbeat_count": self.collector.stats.heartbeat_count,
                    "pong_count": self.collector.stats.pong_count,
                    "timeout_count": self.collector.stats.timeout_count,
                    "api_error_count": self.collector.stats.api_error_count,
                    "rate_limit_error_count": self.collector.stats.rate_limit_error_count,
                    "last_error": self.collector.stats.last_error,
                },
                "report": self.report.to_dict(),
                "open_positions": self._build_open_positions_payload(),
                "supervisor": self.supervisor.snapshot(),
            },
        )

    def _build_open_positions_payload(self) -> list[dict[str, object]]:
        positions: list[dict[str, object]] = []
        for position in self.executor.portfolio.open_positions.values():
            current_snapshot = self._latest_snapshots_by_symbol.get(position.symbol)
            current_price = current_snapshot.price if current_snapshot is not None else None
            current_notional_usd = position.target_notional_usd
            unrealized_pnl_usd = 0.0
            if current_price is not None and position.entry_price > 0:
                current_notional_usd = round(
                    position.target_notional_usd * (current_price / position.entry_price),
                    4,
                )
                unrealized_pnl_usd = self.executor.portfolio._gross_pnl_usd(
                    position,
                    current_price,
                )
            positions.append(
                {
                    "symbol": position.symbol,
                    "side": position.side,
                    "setup": position.setup,
                    "open_reason": position.setup,
                    "confidence": position.confidence,
                    "entry_price": position.entry_price,
                    "current_price": current_price,
                    "target_notional_usd": position.target_notional_usd,
                    "margin_usd": position.margin_usd,
                    "leverage": position.effective_leverage,
                    "effective_leverage": position.effective_leverage,
                    "risk_budget_usd": position.risk_budget_usd,
                    "expected_loss_usd": position.expected_loss_usd,
                    "invalidation_price": position.invalidation_price,
                    "isolated": position.isolated,
                    "current_notional_usd": current_notional_usd,
                    "unrealized_pnl_usd": unrealized_pnl_usd,
                    "stop_bps": position.stop_bps,
                    "time_stop_hours": position.time_stop_hours,
                    "take_profit_bps": position.take_profit_bps,
                    "break_even_trigger_bps": position.break_even_trigger_bps,
                    "trailing_activation_bps": position.trailing_activation_bps,
                    "trailing_distance_bps": position.trailing_distance_bps,
                    "best_price_seen": position.best_price_seen,
                    "opened_at": position.opened_at.isoformat() if position.opened_at else None,
                }
            )
        return positions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Pod C directly on the Hyperliquid live collector")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--coins", help="Comma-separated coin list")
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--journal-output", help="Optional JSONL live journal path")
    return parser


async def _run_from_args() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    coins = None
    if args.coins:
        coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    result = await PodCLiveRunner(config, coins=coins).run(
        max_runtime_seconds=args.max_runtime_seconds,
        max_messages=args.max_messages,
        journal_path=args.journal_output,
    )
    for key in (
        "records_processed",
        "signal_count",
        "accepted_count",
        "rejected_count",
        "opened_count",
        "skipped_open_count",
        "closed_trade_count",
        "realized_pnl_usd",
        "records_by_regime",
        "signals_by_date",
    ):
        print(f"{key}={result[key]}")
    print(f"collector={result['collector']}")
    if result["journal_path"]:
        print(f"journal_path={result['journal_path']}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_from_args())


if __name__ == "__main__":
    main()
