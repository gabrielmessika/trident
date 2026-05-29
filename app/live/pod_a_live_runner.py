from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.execution.live import LiveExecutionVenue
from app.execution.live_cap import apply_live_notional_cap
from app.hyperliquid.info_client import HyperliquidInfoClient, apply_live_asset_leverage_caps
from app.hyperliquid.private_state import (
    ExchangePosition,
    HyperliquidCredentials,
    HyperliquidPrivateInfoClient,
)
from app.live.collector import HyperliquidLiveCollector
from app.live.crash_alerts import notify_crash
from app.live.exchange_closed_fills import (
    exchange_closed_reason_for_fill,
    exchange_fill_timestamp,
    known_exit_order_ids_for_symbol,
    known_exit_order_roles_for_symbol,
    select_exchange_closed_fill,
)
from app.live.exchange_position_metrics import exchange_current_price
from app.live.reconciliation import ReconciliationReport, reconcile_exchange_state
from app.live.replay_capture import (
    annotate_snapshot_record,
    build_maintenance_snapshot_record,
)
from app.live.runtime_status import write_runtime_status
from app.live.state_store import LiveStateStore, live_state_path_for_pod
from app.live.user_stream import UserOrderUpdateMonitor, check_order_updates_subscription
from app.persistence.journal import (
    JsonlJournal,
    build_signal_journal_record,
    build_signal_review_journal_record,
    build_trade_journal_record,
)
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, RiskDecision, SymbolMarketSnapshot, symbol_market_snapshot_from_mapping

logger = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


class PodALiveRunner:
    """Runs Pod A on top of the native Hyperliquid live collector."""

    STATUS_HEARTBEAT_SECONDS = 60.0
    MARKET_DATA_FALLBACK_IDLE_SECONDS = 15.0
    MAINTENANCE_POLL_SECONDS = 5.0

    def __init__(
        self,
        config: AppConfig,
        coins: list[str] | None = None,
        *,
        use_live_asset_caps: bool = False,
        runtime_name: str = "pod_a",
        status_path: str | Path = "logs/pod_a_live_status.json",
        supervisor_profile: str = "trident-live-pod-a",
        signal_source: str = "pod_a_live_signal",
        filtered_source: str = "pod_a_live_filtered",
        trade_source: str = "pod_a_live_trade",
        review_label: str = "Pod A",
        mode: str | None = None,
    ) -> None:
        self.config = config
        self.mode = mode or os.getenv("TRIDENT_MODE", "dry-run")
        self.coins = (
            coins
            or config.hyperliquid.observation_universe
            or config.hyperliquid.default_coins
        )
        self.runtime_name = str(runtime_name)
        self.status_path = Path(status_path)
        self.supervisor_profile = str(supervisor_profile)
        self.signal_source = str(signal_source)
        self.filtered_source = str(filtered_source)
        self.trade_source = str(trade_source)
        self.review_label = str(review_label)
        self.snapshot_stream_source = f"{self.runtime_name}_live"
        if use_live_asset_caps:
            self.config = apply_live_asset_leverage_caps(
                self.config,
                symbols=self.coins,
            )
        self.collector = HyperliquidLiveCollector(self.config, coins=self.coins)
        self.supervisor = TridentSupervisor(
            config=self.config,
            profile=self.supervisor_profile,
            mode="live" if self.mode == "live" else "dry-run",
        )
        self.risk_gate = PodARiskGate(self.config)
        self.executor = PodAExecutor(self.config)
        self.live_state_store: LiveStateStore | None = None
        self.live_external_state_stores: list[LiveStateStore] = []
        self.live_reconciliation_report: ReconciliationReport | None = None
        self._live_private_client: HyperliquidPrivateInfoClient | None = None
        self._live_user_stream: UserOrderUpdateMonitor | None = None
        self._live_trading_paused = False
        if self.mode == "live":
            credentials = HyperliquidCredentials.from_env()
            self.live_state_store = LiveStateStore(
                live_state_path_for_pod(self.runtime_name, allow_global=True)
            )
            if _env_flag("TRIDENT_ENABLE_POD_C") and self.runtime_name != "pod_c":
                self.live_external_state_stores.append(
                    LiveStateStore(live_state_path_for_pod("pod_c", allow_global=False))
                )
            self._live_private_client = HyperliquidPrivateInfoClient(
                self.config.hyperliquid,
                credentials,
            )
            self.executor.venue = LiveExecutionVenue(
                self.config,
                credentials,
                private_info_client=self._live_private_client,
            )
            self._live_user_stream = UserOrderUpdateMonitor(
                self.config.hyperliquid,
                account_address=credentials.account_address,
            )
        self.report = PodABacktestReport()
        self._latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        self._latest_exchange_positions_by_symbol: dict[str, ExchangePosition] = {}
        self._info_client = HyperliquidInfoClient(self.config.hyperliquid)
        self._last_record_monotonic = time.monotonic()
        self._last_market_data_refresh_monotonic = 0.0
    async def run(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
        journal_path: str | Path | None = None,
    ) -> dict[str, object]:
        journal = (
            JsonlJournal(journal_path, truncate=(self.mode != "live"))
            if journal_path is not None
            else None
        )
        status_path = self.status_path
        if self.mode == "live":
            await self._prepare_live_execution()
            if self._live_user_stream is not None:
                self._live_user_stream.start()
        self._write_runtime_status(status_path)
        maintenance_task = asyncio.create_task(
            self._maintenance_loop(status_path, journal=journal)
        )
        try:
            async for record in self._iter_live_records(
                max_runtime_seconds=max_runtime_seconds,
                max_messages=max_messages,
            ):
                record = self._annotate_snapshot_record(record)
                self._process_record(record, journal=journal)
                self._persist_live_state()
                self._write_runtime_status(status_path)
        finally:
            maintenance_task.cancel()
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass
            if self._live_user_stream is not None:
                await self._live_user_stream.stop()

        final_records = self.collector.builder.finalize()
        final_records = [self._annotate_snapshot_record(record) for record in final_records]
        self.collector.stats.snapshots_written += len(self.collector.writer.append_many(final_records))
        for record in final_records:
            self._process_record(record, journal=journal)
            self._persist_live_state()
            self._write_runtime_status(status_path)

        if self.mode != "live":
            final_trades, _ = self.executor.finalize(
                snapshots=[],
                timestamp=None,
            )
            for trade in final_trades:
                self._record_closed_trade(
                    trade,
                    current_regime=self.supervisor.state.regime.value,
                    date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else "unknown"),
                    journal=journal,
                    timestamp=trade.closed_at.isoformat() if trade.closed_at else None,
                )
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

    async def _prepare_live_execution(self) -> None:
        if self.live_state_store is None or self._live_private_client is None:
            raise RuntimeError("Live execution requested without live state components")
        account_state = self._live_private_client.fetch_account_state(
            fills_lookback_hours=float(os.getenv("TRIDENT_LIVE_FILLS_LOOKBACK_HOURS", "24")),
            include_account_mode=True,
        )
        self._latest_exchange_positions_by_symbol = dict(account_state.positions)
        self.live_reconciliation_report = reconcile_exchange_state(
            account_state=account_state,
            portfolio=self.executor.portfolio,
            state_store=self.live_state_store,
            allow_unknown_exchange_positions=os.getenv("TRIDENT_LIVE_ALLOW_UNKNOWN_POSITIONS") == "true",
            allow_open_orders=os.getenv("TRIDENT_LIVE_ALLOW_OPEN_ORDERS") == "true",
            external_state_stores=self.live_external_state_stores,
        )
        if not self.live_reconciliation_report.ready:
            raise RuntimeError(
                "Live reconciliation failed: "
                + ",".join(self.live_reconciliation_report.reasons)
            )
        self._load_live_order_metadata()
        if self._refresh_live_stop_grace_orders():
            self._persist_live_state()
        if os.getenv("TRIDENT_LIVE_SKIP_USER_WS_CHECK") != "true":
            credentials = HyperliquidCredentials.from_env()
            ws_check = await check_order_updates_subscription(
                self.config.hyperliquid,
                account_address=credentials.account_address,
                timeout_seconds=min(self.config.hyperliquid.connect_timeout_seconds, 10.0),
            )
            if not ws_check.ok:
                raise RuntimeError(f"orderUpdates websocket check failed: {ws_check.error}")
        self._persist_live_state()

    def _load_live_order_metadata(self) -> None:
        if self.live_state_store is None:
            return
        venue = getattr(self.executor, "venue", None)
        if not isinstance(venue, LiveExecutionVenue):
            return
        orders = self.live_state_store.load().get("orders", {})
        venue.load_order_metadata(orders if isinstance(orders, dict) else None)

    def _refresh_live_stop_grace_orders(self) -> bool:
        venue = getattr(self.executor, "venue", None)
        if not isinstance(venue, LiveExecutionVenue):
            return False
        changed = False
        for symbol in list(self.executor.portfolio.open_positions):
            changed = venue.refresh_stop_grace_orders(symbol) or changed
        return changed

    def _persist_live_state(self) -> None:
        if self.mode != "live" or self.live_state_store is None:
            return
        orders = getattr(self.executor.venue, "orders_by_symbol", None)
        self.live_state_store.save_portfolio(
            self.executor.portfolio,
            orders=orders if isinstance(orders, dict) and orders else None,
            mode="live",
        )

    def _live_ready_for_entries(self) -> bool:
        if self._live_trading_paused:
            return False
        if self._live_user_stream is None:
            return False
        return self._live_user_stream.healthy(
            max_stale_seconds=max(self.config.hyperliquid.message_timeout_seconds * 3, 60.0)
        )

    def _sync_live_exchange_state(self, *, journal: JsonlJournal | None) -> bool:
        if self.mode != "live" or self._live_private_client is None or self.live_state_store is None:
            return False
        try:
            account_state = self._live_private_client.fetch_account_state(
                fills_lookback_hours=float(os.getenv("TRIDENT_LIVE_FILLS_LOOKBACK_HOURS", "24")),
                include_account_mode=True,
            )
        except Exception as exc:
            logger.warning("Live exchange reconciliation failed; entries paused: %s", exc)
            self._live_trading_paused = True
            return False
        self._latest_exchange_positions_by_symbol = dict(account_state.positions)
        report = reconcile_exchange_state(
            account_state=account_state,
            portfolio=self.executor.portfolio,
            state_store=self.live_state_store,
            allow_unknown_exchange_positions=os.getenv("TRIDENT_LIVE_ALLOW_UNKNOWN_POSITIONS") == "true",
            allow_open_orders=os.getenv("TRIDENT_LIVE_ALLOW_OPEN_ORDERS") == "true",
            external_state_stores=self.live_external_state_stores,
        )
        self.live_reconciliation_report = report
        self._live_trading_paused = not report.ready
        changed = False
        for symbol in list(self.executor.portfolio.open_positions):
            if symbol in account_state.positions:
                continue
            position = self.executor.portfolio.open_positions[symbol]
            known_order_roles = known_exit_order_roles_for_symbol(self.live_state_store, symbol)
            fill = select_exchange_closed_fill(
                position,
                account_state.recent_fills,
                known_order_ids=set(known_order_roles)
                or known_exit_order_ids_for_symbol(self.live_state_store, symbol),
            )
            if fill is None:
                logger.warning(
                    "Local %s position missing on exchange, but no post-open close fill was found; keeping local state",
                    symbol,
                )
                continue
            timestamp = exchange_fill_timestamp(fill)
            close_reason = exchange_closed_reason_for_fill(
                fill,
                known_order_roles=known_order_roles,
            )
            trade = self.executor.portfolio.close_position(
                symbol,
                fill.price,
                fill.fee_usd,
                timestamp,
                close_reason,
            )
            if trade is not None:
                self._record_closed_trade(
                    trade,
                    current_regime=self.supervisor.state.regime.value,
                    date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else timestamp[:10]),
                    journal=journal,
                    timestamp=timestamp,
                )
                changed = True
        changed = self._refresh_live_stop_grace_orders() or changed
        if changed:
            self._persist_live_state()
        return changed

    async def _iter_live_records(
        self,
        *,
        max_runtime_seconds: float | None,
        max_messages: int | None,
    ):
        async for record in self.collector.iter_records(
            max_runtime_seconds=max_runtime_seconds,
            max_messages=max_messages,
        ):
            annotated = self._annotate_snapshot_record(record)
            self.collector.stats.snapshots_written += len(self.collector.writer.append_many([annotated]))
            yield annotated

    def _process_record(
        self,
        record: dict[str, object],
        *,
        journal: JsonlJournal | None,
    ) -> None:
        if str(record.get("capture_reason", "")) == "maintenance_refresh":
            self._process_maintenance_record(record, journal=journal)
            return
        timestamp = str(record.get("timestamp"))
        self._last_record_monotonic = time.monotonic()
        date_key = timestamp[:10]
        regime_snapshot = record.get("regime_snapshot", {})
        cluster_regime_snapshots_raw = record.get("cluster_regime_snapshots", {})
        symbols = record.get("symbols", [])
        if not isinstance(regime_snapshot, dict) or not isinstance(symbols, list):
            return

        cluster_regime_snapshots = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (cluster_regime_snapshots_raw or {}).items()
            if isinstance(snap, dict)
        }
        self.report.records_processed += 1
        self.report.add_record_date(date_key)

        previous_regime = self.supervisor.state.regime.value
        self.supervisor.apply_regime_snapshot(
            RegimeSnapshot(**regime_snapshot),
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        current_regime = self.supervisor.state.regime.value
        if current_regime != previous_regime:
            self.report.add_regime_transition(
                date_key=date_key,
                previous_regime=previous_regime,
                new_regime=current_regime,
            )
        self.report.add_record_regime(current_regime)

        snapshots = [symbol_market_snapshot_from_mapping(item) for item in symbols if isinstance(item, dict)]
        self._latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
        snapshots = self._backfill_missing_position_snapshots(snapshots)
        previews = self.supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp)
        trade_plans = self.supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp)
        for plan in trade_plans:
            plan.setup_details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
        if self.mode == "live":
            leverage_policy = LeveragePolicy(self.config.pod_a)
            trade_plans = [
                apply_live_notional_cap(
                    plan,
                    self.config.trident.execution.live_max_order_notional_usd,
                    max_leverage=leverage_policy.max_allowed(plan.symbol),
                )
                for plan in trade_plans
            ]
        risk_decisions = self.risk_gate.evaluate_many(trade_plans)
        if self.mode == "live" and not self._live_ready_for_entries():
            risk_decisions = []
        entry_allowed_symbols = self.supervisor.opening_symbols_for(PodName.POD_A)
        managed_symbols = self.supervisor.managed_symbols_for(
            PodName.POD_A,
            {
                str(symbol).upper()
                for symbol in self.executor.portfolio.open_positions
            },
        )
        execution = self.executor.process_record(
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            entry_allowed_symbols=entry_allowed_symbols,
            managed_symbols=managed_symbols,
        )
        snapshot_by_symbol = {
            item["symbol"]: item for item in symbols if isinstance(item, dict) and "symbol" in item
        }
        decisions_by_symbol: dict[str, RiskDecision] = {
            decision.trade_plan.symbol: decision for decision in risk_decisions
        }
        pod_allocation = self.supervisor.capital_plan.pod_allocations[PodName.POD_A]
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
                        source=self.signal_source,
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
                                "symbol_target_pct": (
                                    self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol).target_pct
                                    if self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol) is not None
                                    else 0.0
                                ),
                                "symbol_target_usd": (
                                    self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol).target_usd
                                    if self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol) is not None
                                    else 0.0
                                ),
                                "reason_summary": (
                                    self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol).reason_summary
                                    if self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol) is not None
                                    else ""
                                ),
                                "correlation_group": (
                                    self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol).correlation_group
                                    if self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol) is not None
                                    else ""
                                ),
                                "correlation_density_factor": (
                                    self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol).correlation_density_factor
                                    if self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol) is not None
                                    else 1.0
                                ),
                                "capped_by_correlation": (
                                    self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol).capped_by_correlation
                                    if self.supervisor.allocation_for_symbol(PodName.POD_A, preview.symbol) is not None
                                    else False
                                ),
                            },
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
        if journal is not None:
            for review in self.supervisor.state.pod_a_signal_review:
                if str(review.get("status")) != "filtered":
                    continue
                journal.append(
                    build_signal_review_journal_record(
                        timestamp=timestamp,
                        record_index=self.report.records_processed,
                        regime=current_regime,
                        regime_snapshot=regime_snapshot,
                        symbol_snapshot=snapshot_by_symbol.get(str(review.get("symbol", ""))),
                        source=self.filtered_source,
                        review=review,
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
        for symbol in execution.opened_symbols:
            decision = decisions_by_symbol.get(symbol)
            if decision is not None:
                self.report.add_opened_setup(decision.trade_plan.setup)
        for symbol in execution.skipped_open_symbols:
            decision = decisions_by_symbol.get(symbol)
            if decision is not None:
                self.report.add_skipped_open_setup(decision.trade_plan.setup)
        for trade in execution.closed_trades:
            self._record_closed_trade(
                trade,
                current_regime=current_regime,
                date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else date_key),
                journal=journal,
                timestamp=timestamp,
            )
        self._emit_review_summary(
            timestamp=timestamp,
            regime=current_regime,
            previews=previews,
            trade_plans=trade_plans,
            risk_decisions=risk_decisions,
            execution=execution,
        )

    def _process_maintenance_record(
        self,
        record: dict[str, object],
        *,
        journal: JsonlJournal | None,
    ) -> None:
        timestamp = str(record.get("timestamp"))
        symbols = record.get("symbols", [])
        if not isinstance(symbols, list):
            return
        snapshots = [symbol_market_snapshot_from_mapping(item) for item in symbols if isinstance(item, dict)]
        if not snapshots:
            return
        self._latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
        managed_symbols = self.supervisor.managed_symbols_for(
            PodName.POD_A,
            {
                str(symbol).upper()
                for symbol in self.executor.portfolio.open_positions
            },
        )
        execution = self.executor.process_record(
            snapshots=snapshots,
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp=timestamp,
            entry_allowed_symbols=self.supervisor.opening_symbols_for(PodName.POD_A),
            managed_symbols=managed_symbols,
        )
        current_regime = self.supervisor.state.regime.value
        for trade in execution.closed_trades:
            self._record_closed_trade(
                trade,
                current_regime=current_regime,
                date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else timestamp[:10]),
                journal=journal,
                timestamp=timestamp,
            )

    def _hold_hours(self, trade: object) -> float | None:
        opened_at = getattr(trade, "opened_at", None)
        closed_at = getattr(trade, "closed_at", None)
        if opened_at is None or closed_at is None:
            return None
        return round((closed_at - opened_at).total_seconds() / 3600.0, 4)

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
        owned_symbols = self.supervisor.registry.symbols_for(PodName.POD_A)
        accepted_count = sum(1 for decision in risk_decisions if decision.accepted)
        logger.info(
            "%s review summary; ts=%s regime=%s tradable_count=%s owned_symbols=%s previews=%s trade_plans=%s accepted=%s opened=%s skipped=%s closed=%s open_positions=%s realized_pnl_usd=%.2f",
            self.review_label,
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
            "symbol": trade.symbol,
            "side": trade.side,
            "setup": getattr(trade, "setup", None),
            "open_reason": getattr(trade, "setup", None),
            "confidence": getattr(trade, "confidence", None),
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "target_notional_usd": trade.target_notional_usd,
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
            "gross_pnl_usd": trade.gross_pnl_usd,
            "fees_usd": trade.fees_usd,
            "pnl_usd": trade.pnl_usd,
            "is_win": trade.pnl_usd >= 0,
            "close_reason": trade.close_reason,
            "hold_hours": self._hold_hours(trade),
            "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
            "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
            "setup_details": dict(getattr(trade, "setup_details", {}) or {}),
        }

    def _record_closed_trade(
        self,
        trade: object,
        *,
        current_regime: str,
        date_key: str,
        journal: JsonlJournal | None,
        timestamp: str | None,
    ) -> None:
        self.risk_gate.record_closed_trade(
            symbol=str(getattr(trade, "symbol", "")),
            setup=getattr(trade, "setup", None),
            pnl_usd=getattr(trade, "pnl_usd", None),
            date_key=date_key,
        )
        if journal is not None:
            journal.append(
                build_trade_journal_record(
                    timestamp=timestamp,
                    record_index=self.report.records_processed,
                    trade=self._trade_to_record(trade),
                    source=self.trade_source,
                )
            )
        self.report.add_closed_trade(
            date_key=date_key,
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

    async def _maintenance_loop(
        self,
        path: str | Path,
        *,
        journal: JsonlJournal | None,
    ) -> None:
        last_status_write = time.monotonic()
        while True:
            await asyncio.sleep(self.MAINTENANCE_POLL_SECONDS)
            now = time.monotonic()
            live_synced = self._sync_live_exchange_state(journal=journal)
            refreshed = self._refresh_open_positions_without_stream(journal=journal, now=now)
            if refreshed:
                self._persist_live_state()
            if live_synced or refreshed or (now - last_status_write) >= self.STATUS_HEARTBEAT_SECONDS:
                self._write_runtime_status(path)
                last_status_write = now

    def _write_runtime_status(self, path: str | Path) -> None:
        write_runtime_status(
            path,
            {
                "pod": self.runtime_name,
                "mode": self.mode,
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
                "live_reconciliation": (
                    self.live_reconciliation_report.to_dict()
                    if self.live_reconciliation_report is not None
                    else None
                ),
                "live_trading_paused": self._live_trading_paused,
                "user_order_updates": (
                    self._live_user_stream.stats.to_dict()
                    if self._live_user_stream is not None
                    else None
                ),
                "report": self.report.to_dict(),
                "open_positions": self._build_open_positions_payload(),
                "supervisor": self.supervisor.snapshot(),
            },
        )

    def _backfill_missing_position_snapshots(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SymbolMarketSnapshot]:
        """Fetch REST mid-prices for open positions missing from the WS snapshot batch."""
        snapshot_symbols = {s.symbol for s in snapshots}
        missing = [
            sym for sym in self.executor.portfolio.open_positions
            if sym not in snapshot_symbols
        ]
        if not missing:
            return snapshots
        try:
            all_mids = self._info_client.fetch_all_mids(symbols=missing)
        except Exception:
            logger.warning("REST allMids fallback failed for symbols: %s", missing)
            return snapshots
        extended = list(snapshots)
        for sym in missing:
            fallback = self._rest_fallback_snapshot(sym, all_mids)
            if fallback is None:
                continue
            extended.append(fallback)
        return extended

    def _refresh_open_positions_without_stream(
        self,
        *,
        journal: JsonlJournal | None,
        now: float | None = None,
    ) -> bool:
        open_symbols = sorted(self.executor.portfolio.open_positions)
        if not open_symbols:
            return False
        current = now if now is not None else time.monotonic()
        idle_seconds = current - self._last_record_monotonic
        refresh_age = current - self._last_market_data_refresh_monotonic
        if idle_seconds < self.MARKET_DATA_FALLBACK_IDLE_SECONDS:
            return False
        if refresh_age < self.MARKET_DATA_FALLBACK_IDLE_SECONDS:
            return False
        try:
            all_mids = self._info_client.fetch_all_mids(symbols=open_symbols)
        except Exception:
            logger.warning("REST allMids maintenance fallback failed for symbols: %s", open_symbols)
            self._last_market_data_refresh_monotonic = current
            return False
        self._last_market_data_refresh_monotonic = current
        snapshots = [
            fallback
            for symbol in open_symbols
            if (fallback := self._rest_fallback_snapshot(symbol, all_mids)) is not None
        ]
        if not snapshots:
            return False
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        maintenance_record = build_maintenance_snapshot_record(
            timestamp=timestamp,
            stream_source=self.snapshot_stream_source,
            regime_snapshot=self.supervisor.state.regime_snapshot,
            cluster_regime_snapshots=self.supervisor.state.cluster_regime_snapshots,
            snapshots=snapshots,
        )
        snapshots = [
            symbol_market_snapshot_from_mapping(item)
            for item in maintenance_record.get("symbols", [])
            if isinstance(item, dict)
        ]
        self.collector.stats.snapshots_written += len(
            self.collector.writer.append_many([maintenance_record])
        )
        managed_symbols = self.supervisor.managed_symbols_for(
            PodName.POD_A,
            set(open_symbols),
        )
        execution = self.executor.process_record(
            snapshots=snapshots,
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp=timestamp,
            entry_allowed_symbols=self.supervisor.opening_symbols_for(PodName.POD_A),
            managed_symbols=managed_symbols,
        )
        current_regime = self.supervisor.state.regime.value
        for trade in execution.closed_trades:
            self._record_closed_trade(
                trade,
                current_regime=current_regime,
                date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else timestamp[:10]),
                journal=journal,
                timestamp=timestamp,
            )
        logger.info(
            "Pod A maintenance refresh; idle_seconds=%.1f refreshed_symbols=%s closed=%s",
            idle_seconds,
            [snapshot.symbol for snapshot in snapshots],
            len(execution.closed_trades),
        )
        return True

    def _annotate_snapshot_record(self, record: dict[str, object]) -> dict[str, object]:
        annotated = annotate_snapshot_record(
            record,
            stream_source=self.snapshot_stream_source,
        )
        return annotated

    def _rest_fallback_snapshot(
        self,
        symbol: str,
        all_mids: dict[str, float],
    ) -> SymbolMarketSnapshot | None:
        mid = all_mids.get(symbol)
        if mid is None or mid <= 0:
            return None
        latest = self._latest_snapshots_by_symbol.get(symbol)
        fallback = SymbolMarketSnapshot(
            symbol=symbol,
            price=mid,
            ema_fast=latest.ema_fast if latest is not None else mid,
            ema_slow=latest.ema_slow if latest is not None else mid,
            vwap_distance_bps=latest.vwap_distance_bps if latest is not None else 0.0,
            structure_score=latest.structure_score if latest is not None else 0.0,
            funding_rate=latest.funding_rate if latest is not None else 0.0,
            spread_bps=latest.spread_bps if latest is not None else 0.0,
            btc_aligned=latest.btc_aligned if latest is not None else True,
            market_cluster=latest.market_cluster if latest is not None else cluster_for_symbol(self.config, symbol),
            cluster_aligned=latest.cluster_aligned if latest is not None else True,
            cluster_leader=latest.cluster_leader if latest is not None else symbol,
            book_imbalance=latest.book_imbalance if latest is not None else 0.0,
            trade_flow_bias=latest.trade_flow_bias if latest is not None else 0.0,
            bucket_volume=latest.bucket_volume if latest is not None else 0.0,
            bucket_trade_count=latest.bucket_trade_count if latest is not None else 0,
            bucket_range_bps=latest.bucket_range_bps if latest is not None else 0.0,
            open_interest=latest.open_interest if latest is not None else None,
            mark_px=latest.mark_px if latest is not None else None,
            oracle_px=latest.oracle_px if latest is not None else None,
            premium=latest.premium if latest is not None else None,
            day_ntl_vlm=latest.day_ntl_vlm if latest is not None else None,
            day_base_vlm=latest.day_base_vlm if latest is not None else None,
            asset_ctx_observation_age_seconds=(
                latest.asset_ctx_observation_age_seconds if latest is not None else None
            ),
            source="rest_fallback",
        )
        self._latest_snapshots_by_symbol[symbol] = fallback
        return fallback

    def _build_open_positions_payload(self) -> list[dict[str, object]]:
        positions: list[dict[str, object]] = []
        for position in self.executor.portfolio.open_positions.values():
            current_snapshot = self._latest_snapshots_by_symbol.get(position.symbol)
            exchange_position = self._latest_exchange_positions_by_symbol.get(position.symbol)
            current_price = current_snapshot.price if current_snapshot is not None else None
            current_notional_usd = position.target_notional_usd
            unrealized_pnl_usd = 0.0
            margin_usd = position.margin_usd
            leverage = position.effective_leverage
            isolated = position.isolated
            if exchange_position is not None:
                current_price = exchange_current_price(exchange_position) or current_price
                current_notional_usd = round(float(exchange_position.notional_usd), 4)
                unrealized_pnl_usd = round(float(exchange_position.unrealized_pnl_usd), 4)
                margin_usd = float(exchange_position.margin_used_usd)
                leverage = float(exchange_position.leverage)
                isolated = bool(exchange_position.isolated)
            elif current_price is not None and position.entry_price > 0:
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
                    "margin_usd": margin_usd,
                    "leverage": leverage,
                    "effective_leverage": leverage,
                    "risk_budget_usd": position.risk_budget_usd,
                    "expected_loss_usd": position.expected_loss_usd,
                    "invalidation_price": position.invalidation_price,
                    "isolated": isolated,
                    "current_notional_usd": current_notional_usd,
                    "unrealized_pnl_usd": unrealized_pnl_usd,
                    "stop_bps": position.stop_bps,
                    "time_stop_hours": position.time_stop_hours,
                    "take_profit_bps": position.take_profit_bps,
                    "break_even_trigger_bps": position.break_even_trigger_bps,
                    "trailing_activation_bps": position.trailing_activation_bps,
                    "trailing_distance_bps": position.trailing_distance_bps,
                    "best_price_seen": position.best_price_seen,
                    "campaign_mode_active": bool(
                        getattr(position, "setup_details", {}).get("campaign_mode_active")
                    ),
                    "routing_revoke_exempt": bool(
                        getattr(position, "setup_details", {}).get("routing_revoke_exempt")
                    ),
                    "opened_at": position.opened_at.isoformat() if position.opened_at else None,
                }
            )
        return positions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Pod A directly on the Hyperliquid live collector")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--coins", help="Comma-separated coin list")
    parser.add_argument("--max-runtime-seconds", type=float, help="Optional local smoke duration")
    parser.add_argument("--max-messages", type=int, help="Optional max websocket messages")
    parser.add_argument("--journal-output", help="Optional JSONL live journal path")
    parser.add_argument("--mode", default=os.getenv("TRIDENT_MODE", "dry-run"), choices=["dry-run", "live"])
    return parser


async def _run_from_args() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    coins = None
    if args.coins:
        coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    result = await PodALiveRunner(config, coins=coins, use_live_asset_caps=True, mode=args.mode).run(
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
    try:
        asyncio.run(_run_from_args())
    except Exception as exc:
        notify_crash(service_name="pod-a-live", exc=exc)
        raise


if __name__ == "__main__":
    main()
