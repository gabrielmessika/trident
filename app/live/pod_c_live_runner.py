from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.backtest.pod_report import PodABacktestReport
from app.execution.directional_executor import DirectionalExecutor
from app.live.collector import HyperliquidLiveCollector
from app.persistence.journal import JsonlJournal, build_signal_journal_record, build_trade_journal_record
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import AppConfig, load_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import RegimeSnapshot, RiskDecision, SymbolMarketSnapshot


class PodCLiveRunner:
    """Runs Pod C on the native Hyperliquid collector using shared dry-run rules."""

    def __init__(self, config: AppConfig, coins: list[str] | None = None) -> None:
        self.config = config
        self.coins = coins or config.hyperliquid.default_coins or config.pod_c.follower_symbols
        self.collector = HyperliquidLiveCollector(config, coins=self.coins)
        self.supervisor = TridentSupervisor(
            config=config,
            profile="trident-live-pod-c",
            mode="dry-run",
        )
        self.risk_gate = PodCRiskGate(config)
        self.executor = DirectionalExecutor(config)
        self.report = PodABacktestReport()

    async def run(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
        journal_path: str | Path | None = None,
    ) -> dict[str, object]:
        journal = JsonlJournal(journal_path) if journal_path is not None else None
        async for record in self.collector.iter_records(
            max_runtime_seconds=max_runtime_seconds,
            max_messages=max_messages,
        ):
            self.collector.stats.snapshots_written += len(self.collector.writer.append_many([record]))
            self._process_record(record, journal=journal)

        final_records = self.collector.builder.finalize()
        self.collector.stats.snapshots_written += len(self.collector.writer.append_many(final_records))
        for record in final_records:
            self._process_record(record, journal=journal)

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
        previews = self.supervisor.preview_pod_c_signals(snapshots)
        trade_plans = self.supervisor.build_pod_c_trade_plans(snapshots)
        risk_decisions = self.risk_gate.evaluate_many(trade_plans)
        execution = self.executor.process_record(
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
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
                accepted=decision.accepted,
                reason=decision.reason,
            )
        self.report.add_execution_batch(
            opened_symbols=execution.opened_symbols,
            skipped_open_symbols=execution.skipped_open_symbols,
        )
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
                pnl_usd=trade.pnl_usd,
                gross_pnl_usd=trade.gross_pnl_usd,
                fees_usd=trade.fees_usd,
                close_reason=trade.close_reason,
                hold_hours=self._hold_hours(trade),
                opened_at=trade.opened_at.isoformat() if trade.opened_at else None,
                closed_at=trade.closed_at.isoformat() if trade.closed_at else None,
            )

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
    asyncio.run(_run_from_args())


if __name__ == "__main__":
    main()
