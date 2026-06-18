from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.pod_report import PodABacktestReport
from app.execution.directional_executor import DirectionalExecutor
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
from app.live.pod_c_external_reference import PodCExternalReferenceEnricher
from app.live.reconciliation import ReconciliationReport, reconcile_exchange_state
from app.live.replay_capture import (
    annotate_snapshot_record,
    build_maintenance_snapshot_record,
)
from app.live.runtime_status import write_runtime_status
from app.live.state_store import LiveStateStore, live_state_path_for_pod
from app.live.trade_audit import (
    close_fills_for_trade,
    enrich_trade_record_for_audit,
    exchange_fill_to_close_record,
    funding_payments_for_symbol,
)
from app.live.user_stream import UserOrderUpdateMonitor, check_order_updates_subscription
from app.persistence.journal import (
    JsonlJournal,
    build_signal_journal_record,
    build_signal_review_journal_record,
    build_trade_journal_record,
)
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import AppConfig, load_config
from app.trident.market_clusters import (
    cluster_for_symbol,
    observation_universe_symbols,
    symbols_in_allowed_clusters,
)
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.pod_c.external_reference_shadow import (
    external_reference_shadow_setup_details,
)
from app.trident.pod_c.oil_shadow import (
    P109_OIL_PROMOTED_SETUP,
    build_p109_oil_shadow_features,
    p109_oil_shadow_details,
)
from app.trident.pod_c.signals import TradfiTrendSignal
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SignalPreview,
    SymbolMarketSnapshot,
    symbol_market_snapshot_from_mapping,
)

logger = logging.getLogger(__name__)


class PodCLiveRunner:
    """Runs Pod C on the native Hyperliquid collector using shared dry-run rules."""

    STATUS_HEARTBEAT_SECONDS = 60.0
    MARKET_DATA_FALLBACK_IDLE_SECONDS = 15.0
    MAINTENANCE_POLL_SECONDS = 5.0

    def __init__(
        self,
        config: AppConfig,
        coins: list[str] | None = None,
        *,
        use_live_asset_caps: bool = False,
        mode: str | None = None,
    ) -> None:
        self.mode = mode or os.getenv("TRIDENT_MODE", "dry-run")
        selected_coins = (
            coins
            or symbols_in_allowed_clusters(
                config,
                observation_universe_symbols(config),
                config.pod_c.allowed_market_clusters,
            )
        )
        self.coins = [str(coin).strip().upper() for coin in selected_coins if str(coin).strip()]
        runtime_config = config
        if use_live_asset_caps:
            runtime_config = apply_live_asset_leverage_caps(
                config,
                symbols=self.coins,
            )
        self.config = replace(
            runtime_config,
            hyperliquid=replace(
                runtime_config.hyperliquid,
                observation_universe=list(self.coins),
            ),
        )
        self.collector = HyperliquidLiveCollector(self.config, coins=self.coins)
        self.supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-live-pod-c",
            mode="live" if self.mode == "live" else "dry-run",
        )
        self.snapshot_stream_source = "pod_c_live"
        self.risk_gate = PodCRiskGate(self.config)
        self.executor = DirectionalExecutor(self.config)
        self.live_state_store: LiveStateStore | None = None
        self.live_external_state_stores: list[LiveStateStore] = []
        self.live_reconciliation_report: ReconciliationReport | None = None
        self._live_private_client: HyperliquidPrivateInfoClient | None = None
        self._live_user_stream: UserOrderUpdateMonitor | None = None
        self._live_trading_paused = False
        if self.mode == "live":
            credentials = HyperliquidCredentials.from_env()
            self.live_state_store = LiveStateStore(
                live_state_path_for_pod("pod_c", allow_global=False)
            )
            self.live_external_state_stores.append(
                LiveStateStore(live_state_path_for_pod("pod_a", allow_global=True))
            )
            self._live_private_client = HyperliquidPrivateInfoClient(
                self.config.hyperliquid,
                credentials,
            )
            self.executor.venue = LiveExecutionVenue(
                self.config,
                credentials,
                private_info_client=self._live_private_client,
                orders_changed_callback=self._persist_live_state,
            )
            self._live_user_stream = UserOrderUpdateMonitor(
                self.config.hyperliquid,
                account_address=credentials.account_address,
            )
        self.report = PodABacktestReport()
        self._latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        self._latest_exchange_positions_by_symbol: dict[str, ExchangePosition] = {}
        self._info_client = HyperliquidInfoClient(self.config.hyperliquid)
        self.external_reference_enricher = (
            PodCExternalReferenceEnricher(self.config)
            if self.config.pod_c.external_reference.enabled
            else None
        )
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
        status_path = Path("logs/pod_c_live_status.json")
        if self.mode == "live":
            await self._prepare_live_execution()
            if self._live_user_stream is not None:
                self._live_user_stream.start()
        self._write_runtime_status(status_path)

        maintenance_task = asyncio.create_task(
            self._maintenance_loop(status_path, journal=journal)
        )
        try:
            async for record in self.collector.iter_records(
                max_runtime_seconds=max_runtime_seconds,
                max_messages=max_messages,
            ):
                record = await self._enrich_external_reference_record(record)
                record = self._annotate_snapshot_record(record)
                self.collector.stats.snapshots_written += len(self.collector.writer.append_many([record]))
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

        final_records = []
        for record in self.collector.builder.finalize():
            enriched = await self._enrich_external_reference_record(record)
            final_records.append(self._annotate_snapshot_record(enriched))
        self.collector.stats.snapshots_written += len(self.collector.writer.append_many(final_records))
        for record in final_records:
            self._process_record(record, journal=journal)
            self._persist_live_state()
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
                funding_lookback_hours=float(os.getenv("TRIDENT_LIVE_FUNDING_LOOKBACK_HOURS", "72")),
                include_account_mode=True,
            )
        except Exception as exc:
            logger.warning("Pod C live exchange reconciliation failed; entries paused: %s", exc)
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
                    "Pod C local %s position missing on exchange, but no post-open close fill was found; keeping local state",
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
                close_fill_record = exchange_fill_to_close_record(
                    fill,
                    timestamp=timestamp,
                    close_reason=close_reason,
                    funding_payments=funding_payments_for_symbol(
                        account_state.recent_funding,
                        symbol=symbol,
                    ),
                )
                self._record_closed_trade(
                    trade,
                    current_regime=self.supervisor.state.regime.value,
                    date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else timestamp[:10]),
                    journal=journal,
                    timestamp=timestamp,
                    close_fills=[close_fill_record],
                )
                changed = True
        changed = self._refresh_live_stop_grace_orders() or changed
        if changed:
            self._persist_live_state()
        return changed

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
        p109_oil_shadow_by_symbol = self._p109_oil_shadow_details_by_symbol(
            snapshots=snapshots,
            timestamp=timestamp,
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        previews = self.supervisor.preview_pod_c_signals(snapshots)
        trade_plans = self.supervisor.build_pod_c_trade_plans(snapshots)
        previews, trade_plans = self._apply_p109_oil_promotion(
            previews=previews,
            trade_plans=trade_plans,
            snapshots=snapshots,
            details_by_symbol=p109_oil_shadow_by_symbol,
        )
        trade_plans = self._apply_external_reference_shadow_to_plans(trade_plans)
        trade_plans = self._apply_p109_oil_shadow_to_plans(
            trade_plans,
            p109_oil_shadow_by_symbol,
        )
        if self.mode == "live":
            leverage_policy = LeveragePolicy(self.config.pod_c)
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
        entry_allowed_symbols = self.supervisor.opening_symbols_for(PodName.POD_C)
        managed_symbols = self.supervisor.managed_symbols_for(
            PodName.POD_C,
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

        decisions_by_symbol: dict[str, RiskDecision] = {
            decision.trade_plan.symbol: decision for decision in risk_decisions
        }
        snapshot_by_symbol = {
            item["symbol"]: item for item in symbols if isinstance(item, dict) and "symbol" in item
        }
        fills_by_symbol: dict[str, list[dict[str, object]]] = {}
        for fill in execution.fills:
            fills_by_symbol.setdefault(str(fill["symbol"]), []).append(fill)
        preview_setup_details_by_symbol = self._preview_setup_details_with_external_reference_shadow(
            previews,
            p109_oil_shadow_by_symbol=p109_oil_shadow_by_symbol,
        )
        self._apply_external_reference_shadow_to_signal_reviews()
        self._apply_p109_oil_shadow_to_signal_reviews(p109_oil_shadow_by_symbol)

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
                            "reason_summary": preview.reason_summary,
                            "setup_details": preview_setup_details_by_symbol.get(
                                preview.symbol,
                                dict(preview.setup_details),
                            ),
                            "p109_oil_shadow": p109_oil_shadow_by_symbol.get(preview.symbol, {}),
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
                                "take_profit_bps": (
                                    decisions_by_symbol[preview.symbol].trade_plan.take_profit_bps
                                    if preview.symbol in decisions_by_symbol
                                    else 0.0
                                ),
                            },
                            "execution": {
                                "opened": preview.symbol in execution.opened_symbols,
                                "skipped_open": preview.symbol in execution.skipped_open_symbols,
                                "skip_reason": execution.skip_reasons_by_symbol.get(preview.symbol),
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
            self._apply_external_reference_shadow_to_signal_reviews()
            self._apply_p109_oil_shadow_to_signal_reviews(p109_oil_shadow_by_symbol)
            for review in self.supervisor.state.pod_c_signal_review:
                if str(review.get("status")) != "filtered":
                    continue
                journal.append(
                    build_signal_review_journal_record(
                        timestamp=timestamp,
                        record_index=self.report.records_processed,
                        regime=current_regime,
                        regime_snapshot=regime_snapshot,
                        symbol_snapshot=snapshot_by_symbol.get(str(review.get("symbol", ""))),
                        source="pod_c_live_filtered",
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
            self._record_closed_trade(
                trade,
                current_regime=current_regime,
                date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else date_key),
                journal=journal,
                timestamp=trade.closed_at.isoformat() if trade.closed_at else timestamp,
                close_fills=close_fills_for_trade(trade, execution.fills),
            )
        self._emit_review_summary(
            timestamp=timestamp,
            regime=current_regime,
            previews=previews,
            trade_plans=trade_plans,
            risk_decisions=risk_decisions,
            execution=execution,
        )

    def _apply_external_reference_shadow_to_plans(self, trade_plans: list[object]) -> list[object]:
        for plan in trade_plans:
            details = {
                **dict(getattr(plan, "setup_details", {}) or {}),
                **external_reference_shadow_setup_details(
                    getattr(plan, "setup_details", {}) or {},
                    side=str(getattr(plan, "side", "")),
                ),
            }
            plan.setup_details = details
        return trade_plans

    def _apply_p109_oil_shadow_to_plans(
        self,
        trade_plans: list[object],
        details_by_symbol: dict[str, dict[str, object]],
    ) -> list[object]:
        for plan in trade_plans:
            symbol = str(getattr(plan, "symbol", "")).strip().upper()
            shadow = details_by_symbol.get(symbol)
            if not shadow:
                continue
            plan.setup_details = {
                **dict(getattr(plan, "setup_details", {}) or {}),
                **shadow,
            }
        return trade_plans

    def _apply_p109_oil_promotion(
        self,
        *,
        previews: list[SignalPreview],
        trade_plans: list[object],
        snapshots: list[SymbolMarketSnapshot],
        details_by_symbol: dict[str, dict[str, object]],
    ) -> tuple[list[SignalPreview], list[object]]:
        if not self.config.pod_c.p109_oil_short_enabled:
            return previews, trade_plans
        snapshot_by_symbol = {snapshot.symbol.upper(): snapshot for snapshot in snapshots}
        occupied_symbols = {
            str(getattr(item, "symbol", "")).strip().upper()
            for item in [*previews, *trade_plans]
        }
        open_symbols = {
            str(symbol).strip().upper()
            for symbol in self.executor.portfolio.open_positions
        }
        pod_allocation = self.supervisor.capital_plan.pod_allocations.get(PodName.POD_C)
        if pod_allocation is None:
            return previews, trade_plans

        promoted_previews: list[SignalPreview] = []
        promoted_plans: list[object] = []
        for symbol, shadow in sorted(details_by_symbol.items()):
            normalized = str(symbol).strip().upper()
            if not bool(shadow.get("would_open_p109_oil_short_shadow")):
                continue
            if normalized in occupied_symbols or normalized in open_symbols:
                continue
            snapshot = snapshot_by_symbol.get(normalized)
            if snapshot is None:
                continue
            signal = self._p109_oil_promoted_signal(snapshot=snapshot, shadow=shadow)
            plan = self.supervisor.pod_c_planner.build_trade_plan(signal, pod_allocation)
            if plan is None:
                continue
            preview = self.supervisor._build_signal_preview(signal)
            promoted_previews.append(preview)
            promoted_plans.append(plan)
            occupied_symbols.add(normalized)

        if not promoted_previews:
            return previews, trade_plans
        self._replace_pod_c_reviews_with_promoted(previews=promoted_previews)
        merged_previews = [*previews, *promoted_previews]
        self.supervisor.state.pod_c_signal_preview = merged_previews
        return merged_previews, [*trade_plans, *promoted_plans]

    def _replace_pod_c_reviews_with_promoted(self, *, previews: list[SignalPreview]) -> None:
        promoted_symbols = {preview.symbol.upper() for preview in previews}
        self.supervisor.state.pod_c_signal_review = [
            review
            for review in self.supervisor.state.pod_c_signal_review
            if str(review.get("symbol", "")).strip().upper() not in promoted_symbols
        ]
        self.supervisor.state.pod_c_signal_review.extend(
            self.supervisor._build_signal_review(preview) for preview in previews
        )

    def _p109_oil_promoted_signal(
        self,
        *,
        snapshot: SymbolMarketSnapshot,
        shadow: dict[str, object],
    ) -> TradfiTrendSignal:
        confidence = self._p109_oil_promoted_confidence(shadow)
        details = {
            **dict(shadow),
            "global_regime": self.supervisor.state.regime.value,
            "cluster_regime": self._cluster_regime_value("oil"),
            "market_cluster": "oil",
            "cluster_leader": snapshot.cluster_leader,
            "cluster_aligned": snapshot.cluster_aligned,
            "btc_aligned": snapshot.btc_aligned,
            "reclaim_context": False,
            "cluster_strategy": "oil_short_4h_time_gate",
            "trend_bps": round(self._snapshot_trend_bps(snapshot), 4),
            "structure_score": round(snapshot.structure_score, 4),
            "vwap_distance_bps": round(snapshot.vwap_distance_bps, 4),
            "spread_bps": round(snapshot.spread_bps, 4),
            "funding_rate": round(snapshot.funding_rate, 8),
            "bucket_range_bps": round(snapshot.bucket_range_bps, 4),
            "bucket_trade_count": float(snapshot.bucket_trade_count),
            "bucket_notional_usd": round(
                float(snapshot.bucket_notional_usd or snapshot.bucket_volume * snapshot.price),
                4,
            ),
            "activity_ratio": round(float(snapshot.volume_ratio or 1.0), 4),
            "trade_count_ratio": round(float(snapshot.trade_count_ratio or 1.0), 4),
            "book_imbalance": round(snapshot.book_imbalance, 4),
            "trade_flow_bias": round(snapshot.trade_flow_bias, 4),
            "flow_support_score": round(snapshot.book_imbalance + snapshot.trade_flow_bias, 4),
            "external_reference_available": snapshot.external_reference_source_count > 0,
            "external_reference_price": round(snapshot.external_reference_price or 0.0, 8),
            "external_reference_source_count": float(snapshot.external_reference_source_count),
            "external_reference_sources": snapshot.external_reference_sources,
            "external_reference_symbol": snapshot.external_reference_symbol,
            "external_reference_time": snapshot.external_reference_time,
            "external_reference_age_seconds": round(
                float(snapshot.external_reference_age_seconds or 0.0),
                4,
            ),
            "external_reference_max_deviation_bps": round(
                snapshot.external_reference_max_deviation_bps,
                4,
            ),
            "external_premium_bps": round(snapshot.external_premium_bps, 4),
            "external_momentum_60s_bps": round(snapshot.external_momentum_60s_bps, 4),
            "external_momentum_300s_bps": round(snapshot.external_momentum_300s_bps, 4),
            "external_alignment_score": round(snapshot.external_alignment_score, 4),
            "p109_oil_promoted": True,
            "p109_oil_promoted_mode": "active",
            "p109_oil_promoted_decision_date": "2026-06-18",
            "p109_oil_promoted_source": "operator_accepted_risk_after_shadow_audit",
            "p109_oil_promoted_setup": P109_OIL_PROMOTED_SETUP,
            "p109_oil_promoted_live_action": "short_entry_candidate",
            "p109_oil_promoted_confidence": confidence,
        }
        components = self._p109_oil_promoted_confidence_components(shadow)
        return TradfiTrendSignal(
            symbol=snapshot.symbol,
            side="short",
            setup=P109_OIL_PROMOTED_SETUP,
            confidence=confidence,
            entry_price=snapshot.price,
            market_cluster="oil",
            cluster_leader=snapshot.cluster_leader,
            setup_details=details,
            confidence_components=components,
        )

    def _p109_oil_promoted_confidence(self, shadow: dict[str, object]) -> float:
        base = max(
            float(self.config.pod_c.min_confidence),
            float(self.config.pod_c.p109_oil_short_min_confidence),
        )
        score = self._p109_oil_shadow_score(shadow)
        uplift = min(max(score - 8.0, 0.0) * 0.01, 0.06)
        return round(min(base + uplift, 0.74), 3)

    def _p109_oil_promoted_confidence_components(
        self,
        shadow: dict[str, object],
    ) -> dict[str, float]:
        score = self._p109_oil_shadow_score(shadow)
        return {
            "p109_oil_score_quality": round(min(max(score / 12.0, 0.0), 1.0), 4),
            "p109_time_gate_quality": 1.0,
            "p109_regime_gate_quality": 1.0,
            "setup_bonus": 0.06,
        }

    def _p109_oil_shadow_score(self, shadow: dict[str, object]) -> float:
        try:
            return float(shadow.get("p109_oil_shadow_score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _snapshot_trend_bps(self, snapshot: SymbolMarketSnapshot) -> float:
        if snapshot.ema_slow == 0:
            return 0.0
        return (snapshot.ema_fast - snapshot.ema_slow) / snapshot.ema_slow * 10_000.0

    def _cluster_regime_value(self, cluster: str) -> str:
        regime = (self.supervisor.state.cluster_regimes or {}).get(cluster)
        return getattr(regime, "value", str(regime or ""))

    def _preview_setup_details_with_external_reference_shadow(
        self,
        previews: list[object],
        *,
        p109_oil_shadow_by_symbol: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, dict[str, object]]:
        by_symbol: dict[str, dict[str, object]] = {}
        for preview in previews:
            symbol = str(getattr(preview, "symbol", "")).strip().upper()
            details = {
                **dict(getattr(preview, "setup_details", {}) or {}),
                **external_reference_shadow_setup_details(
                    getattr(preview, "setup_details", {}) or {},
                    side=str(getattr(preview, "side", "")),
                ),
                **((p109_oil_shadow_by_symbol or {}).get(symbol) or {}),
            }
            by_symbol[symbol] = details
        return by_symbol

    def _apply_external_reference_shadow_to_signal_reviews(self) -> None:
        for review in self.supervisor.state.pod_c_signal_review:
            details = dict(review.get("setup_details", {}) or {})
            review["setup_details"] = {
                **details,
                **external_reference_shadow_setup_details(
                    details,
                    side=str(review.get("preferred_side", "")),
                ),
            }

    def _apply_p109_oil_shadow_to_signal_reviews(
        self,
        details_by_symbol: dict[str, dict[str, object]],
    ) -> None:
        for review in self.supervisor.state.pod_c_signal_review:
            symbol = str(review.get("symbol", "")).strip().upper()
            shadow = details_by_symbol.get(symbol)
            if not shadow:
                continue
            review["setup_details"] = {
                **dict(review.get("setup_details", {}) or {}),
                **shadow,
            }
            review["p109_oil_shadow"] = dict(shadow)

    def _p109_oil_shadow_details_by_symbol(
        self,
        *,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str,
        cluster_regime_snapshots: dict[str, RegimeSnapshot],
    ) -> dict[str, dict[str, object]]:
        oil_regime = cluster_regime_snapshots.get("oil")
        details_by_symbol: dict[str, dict[str, object]] = {}
        for snapshot in snapshots:
            features = build_p109_oil_shadow_features(
                snapshot=snapshot,
                timestamp=timestamp,
                cluster_regime_snapshot=oil_regime,
            )
            details = p109_oil_shadow_details(features)
            if details:
                details_by_symbol[snapshot.symbol] = details
        return details_by_symbol

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
            PodName.POD_C,
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
            entry_allowed_symbols=self.supervisor.opening_symbols_for(PodName.POD_C),
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
                close_fills=close_fills_for_trade(trade, execution.fills),
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

    def _trade_to_record(
        self,
        trade: object,
        *,
        close_fills: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return enrich_trade_record_for_audit({
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
            "best_price_seen": getattr(trade, "best_price_seen", None),
            "worst_price_seen": getattr(trade, "worst_price_seen", None),
            "mfe_bps": getattr(trade, "mfe_bps", None),
            "mae_bps": getattr(trade, "mae_bps", None),
            "gross_pnl_usd": getattr(trade, "gross_pnl_usd"),
            "fees_usd": getattr(trade, "fees_usd"),
            "pnl_usd": getattr(trade, "pnl_usd"),
            "is_win": getattr(trade, "pnl_usd") >= 0,
            "close_reason": getattr(trade, "close_reason"),
            "opened_at": getattr(trade, "opened_at").isoformat() if getattr(trade, "opened_at") else None,
            "closed_at": getattr(trade, "closed_at").isoformat() if getattr(trade, "closed_at") else None,
            "setup_details": dict(getattr(trade, "setup_details", {}) or {}),
        }, close_fills=close_fills)

    def _record_closed_trade(
        self,
        trade: object,
        *,
        current_regime: str,
        date_key: str,
        journal: JsonlJournal | None,
        timestamp: str | None,
        close_fills: list[dict[str, object]] | None = None,
    ) -> None:
        if journal is not None:
            journal.append(
                build_trade_journal_record(
                    timestamp=timestamp,
                    record_index=self.report.records_processed,
                    trade=self._trade_to_record(trade, close_fills=close_fills),
                    source="pod_c_live_trade",
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
            best_price_seen=getattr(trade, "best_price_seen", None),
            worst_price_seen=getattr(trade, "worst_price_seen", None),
            mfe_bps=getattr(trade, "mfe_bps", None),
            mae_bps=getattr(trade, "mae_bps", None),
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
                "pod": "pod_c",
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
                "external_reference": (
                    self.external_reference_enricher.stats_payload()
                    if self.external_reference_enricher is not None
                    else {"enabled": False}
                ),
                "report": self.report.to_dict(),
                "open_positions": self._build_open_positions_payload(),
                "supervisor": self.supervisor.snapshot(),
            },
        )

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
        if self.external_reference_enricher is not None:
            maintenance_record = self.external_reference_enricher.enrich_record(
                maintenance_record
            )
        self.collector.stats.snapshots_written += len(
            self.collector.writer.append_many([maintenance_record])
        )
        managed_symbols = self.supervisor.managed_symbols_for(
            PodName.POD_C,
            set(open_symbols),
        )
        execution = self.executor.process_record(
            snapshots=snapshots,
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp=timestamp,
            entry_allowed_symbols=self.supervisor.opening_symbols_for(PodName.POD_C),
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
                close_fills=close_fills_for_trade(trade, execution.fills),
            )
        logger.info(
            "Pod C maintenance refresh; idle_seconds=%.1f refreshed_symbols=%s closed=%s",
            idle_seconds,
            [snapshot.symbol for snapshot in snapshots],
            len(execution.closed_trades),
        )
        return True

    def _annotate_snapshot_record(self, record: dict[str, object]) -> dict[str, object]:
        return annotate_snapshot_record(
            record,
            stream_source=self.snapshot_stream_source,
        )

    async def _enrich_external_reference_record(
        self,
        record: dict[str, object],
    ) -> dict[str, object]:
        if self.external_reference_enricher is None:
            return record
        return await asyncio.to_thread(
            self.external_reference_enricher.enrich_record,
            record,
        )

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
            external_reference_price=latest.external_reference_price if latest is not None else None,
            external_reference_source_count=(
                latest.external_reference_source_count if latest is not None else 0
            ),
            external_reference_sources=(
                latest.external_reference_sources if latest is not None else ""
            ),
            external_reference_symbol=(
                latest.external_reference_symbol if latest is not None else ""
            ),
            external_reference_time=latest.external_reference_time if latest is not None else "",
            external_reference_age_seconds=(
                latest.external_reference_age_seconds if latest is not None else None
            ),
            external_reference_max_deviation_bps=(
                latest.external_reference_max_deviation_bps if latest is not None else 0.0
            ),
            external_premium_bps=latest.external_premium_bps if latest is not None else 0.0,
            external_momentum_60s_bps=(
                latest.external_momentum_60s_bps if latest is not None else 0.0
            ),
            external_momentum_300s_bps=(
                latest.external_momentum_300s_bps if latest is not None else 0.0
            ),
            external_alignment_score=(
                latest.external_alignment_score if latest is not None else 0.0
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
                    "worst_price_seen": position.worst_price_seen,
                    "mfe_bps": round(
                        self.executor.portfolio._best_favorable_move_bps(position),
                        4,
                    ),
                    "mae_bps": round(
                        self.executor.portfolio._worst_favorable_move_bps(position),
                        4,
                    ),
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
    parser.add_argument("--mode", default=os.getenv("TRIDENT_MODE", "dry-run"), choices=["dry-run", "live"])
    return parser


async def _run_from_args() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    coins = None
    if args.coins:
        coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    result = await PodCLiveRunner(config, coins=coins, use_live_asset_caps=True, mode=args.mode).run(
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
        notify_crash(service_name="pod-c-live", exc=exc)
        raise


if __name__ == "__main__":
    main()
