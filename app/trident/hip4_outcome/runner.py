from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.live.runtime_status import write_runtime_status
from app.trident.hip4_outcome.book import build_order_book
from app.trident.hip4_outcome.capital import OutcomeCapitalGuard, OutcomeCapitalSnapshot
from app.trident.hip4_outcome.client import HIP4OutcomeInfoClient
from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.edge import OutcomeEdgeDetector
from app.trident.hip4_outcome.execution import PaperOutcomeExecutor, TestnetOutcomeExecutor
from app.trident.hip4_outcome.external_prices import ExternalPriceAggregator
from app.trident.hip4_outcome.features import (
    ShortHorizonFeatureBuilder,
    update_price_history_payload,
)
from app.trident.hip4_outcome.logging import OutcomeEventLogger
from app.trident.hip4_outcome.locks import UnderlyingOverlapLock
from app.trident.hip4_outcome.models import (
    OutcomeExecutionResult,
    OutcomeMarket,
    OutcomeOpportunity,
    OutcomePosition,
    ShortExpiryAssessment,
    ShortHorizonFeatures,
    SupervisorDecision,
    utc_now_iso,
)
from app.trident.hip4_outcome.overlap import directional_overlap_snapshot
from app.trident.hip4_outcome.parser import parse_outcome_markets
from app.trident.hip4_outcome.probability import ProbabilityModel
from app.trident.hip4_outcome.reconciliation import (
    OutcomeReconciler,
    apply_reconciliation_to_positions,
)
from app.trident.hip4_outcome.reporting import build_daily_summary_rows
from app.trident.hip4_outcome.risk import OutcomeRiskManager
from app.trident.hip4_outcome.state import OutcomeStateStore

logger = logging.getLogger(__name__)


class HIP4OutcomeEdgePod:
    def __init__(
        self,
        config: Hip4OutcomeConfig,
        *,
        info_client: HIP4OutcomeInfoClient | None = None,
    ) -> None:
        self.config = config
        self.info_client = info_client or HIP4OutcomeInfoClient(config)
        self.probability_model = ProbabilityModel(config)
        self.price_aggregator = ExternalPriceAggregator(config)
        self.short_feature_builder = ShortHorizonFeatureBuilder(config)
        self.edge_detector = OutcomeEdgeDetector(config)
        self.risk_manager = OutcomeRiskManager(config)
        self.capital_guard = OutcomeCapitalGuard(config, self.info_client)
        self.reconciler = OutcomeReconciler(config, self.info_client)
        self.state_store = OutcomeStateStore(config.state_path)
        self.event_logger = OutcomeEventLogger(config.logs_dir)
        self.positions = self.state_store.load_positions()
        self._sync_settlement_accounting()
        self.paper_executor = PaperOutcomeExecutor(config)
        self.testnet_executor: TestnetOutcomeExecutor | None = None
        self.loop_count = 0
        self.last_error: str | None = None
        self.last_execution_results: list[dict[str, Any]] = []
        self.last_summary: dict[str, Any] = {}
        self.last_capital_snapshot: dict[str, Any] = self.capital_guard.local_snapshot(
            open_positions=self._active_positions_for_mode()
        ).to_dict()
        self._overlap_locks: dict[str, UnderlyingOverlapLock] = {}
        self._last_markets_seen = 0
        self._last_supported_underlyings: list[str] = []

    def run(
        self,
        *,
        max_loops: int | None = None,
        max_runtime_seconds: float | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            summary = self.run_once()
            if max_loops is not None and self.loop_count >= max_loops:
                return summary
            if max_runtime_seconds is not None and time.monotonic() - started >= max_runtime_seconds:
                return summary
            time.sleep(max(self.config.loop_interval_seconds, 0.1))

    def run_once(self) -> dict[str, Any]:
        self.loop_count += 1
        self.last_execution_results = []
        now_ts = int(time.time())
        loop_started = time.monotonic()
        timings: dict[str, float] = {
            "fetch_mids_ms": 0.0,
            "discover_markets_ms": 0.0,
            "reference_prices_ms": 0.0,
            "short_features_ms": 0.0,
            "books_ms": 0.0,
            "edge_detection_ms": 0.0,
            "execution_ms": 0.0,
            "settlement_ms": 0.0,
            "reconciliation_ms": 0.0,
            "status_ms": 0.0,
        }
        summary: dict[str, Any] = {
            "loop_count": self.loop_count,
            "mode": self.config.mode,
            "markets_seen": 0,
            "markets_supported": 0,
            "opportunities": 0,
            "approved": 0,
            "executed": 0,
            "open_positions": len(self._active_positions_for_mode()),
            "short_expiry_markets": 0,
            "short_expiry_assessments": 0,
            "short_expiry_best_net_edge": None,
            "reconciliation": None,
            "capital": self.capital_guard.local_snapshot(
                open_positions=self._active_positions_for_mode()
            ).to_dict(),
            "directional_overlap": {},
        }
        try:
            stage_started = time.monotonic()
            mids = self.info_client.fetch_all_mids()
            timings["fetch_mids_ms"] = _elapsed_ms(stage_started)

            stage_started = time.monotonic()
            markets = self._discover_markets(now_ts=now_ts)
            timings["discover_markets_ms"] = _elapsed_ms(stage_started)

            stage_started = time.monotonic()
            reference_prices = self.price_aggregator.fetch_many(
                self._reference_underlyings(markets=markets, now_ts=now_ts),
                hyperliquid_mids=mids,
            )
            timings["reference_prices_ms"] = _elapsed_ms(stage_started)

            stage_started = time.monotonic()
            short_price_history = self._update_short_expiry_history(
                reference_prices=reference_prices,
                now_ts=now_ts,
            )
            timings["short_features_ms"] = _elapsed_ms(stage_started)

            summary["markets_seen"] = self._last_markets_seen
            summary["markets_supported"] = len(markets)
            overlap_snapshot = directional_overlap_snapshot(
                self.config.directional_overlap_status_paths,
                enabled=self.config.block_directional_overlap,
            )
            summary["directional_overlap"] = overlap_snapshot.to_dict()
            summary["reference_prices"] = {
                underlying: reference.to_metadata()
                for underlying, reference in reference_prices.items()
            }
            executed_this_loop = 0
            for market in markets:
                reference = reference_prices.get(market.underlying.upper())
                if reference is None or reference.price <= 0:
                    continue
                reference_price = reference.price
                stage_started = time.monotonic()
                order_book = build_order_book(
                    market_id=market.market_id,
                    yes_payload=self.info_client.fetch_l2_book(market.yes_coin),
                    no_payload=self.info_client.fetch_l2_book(market.no_coin),
                    max_slippage=self.config.max_order_slippage,
                )
                timings["books_ms"] += _elapsed_ms(stage_started)

                stage_started = time.monotonic()
                probability = self.probability_model.estimate(
                    market,
                    reference_price=reference_price,
                    now_ts=now_ts,
                )
                short_features = self._short_features_for_market(
                    market=market,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    history=short_price_history,
                )
                short_assessment = self.edge_detector.assess_short_expiry(
                    market=market,
                    order_book=order_book,
                    probability=probability,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    features=short_features,
                )
                if short_assessment is not None:
                    summary["short_expiry_markets"] = int(summary["short_expiry_markets"]) + 1
                    summary["short_expiry_assessments"] = int(summary["short_expiry_assessments"]) + 1
                    best_short = summary.get("short_expiry_best_net_edge")
                    if best_short is None or short_assessment.best_net_edge > float(best_short):
                        summary["short_expiry_best_net_edge"] = short_assessment.best_net_edge
                    self._log_short_expiry_assessment(
                        market=market,
                        assessment=short_assessment,
                        now_ts=now_ts,
                    )
                opportunities = self.edge_detector.detect(
                    market=market,
                    order_book=order_book,
                    reference_price=reference_price,
                    probability=probability,
                    now_ts=now_ts,
                    short_features=short_features,
                    short_assessment=short_assessment,
                )
                timings["edge_detection_ms"] += _elapsed_ms(stage_started)

                summary["opportunities"] = int(summary["opportunities"]) + len(opportunities)
                for opportunity in sorted(opportunities, key=lambda item: item.net_edge, reverse=True):
                    opportunity.metadata.update(reference.to_metadata())
                    self._record_edge_decay(
                        opportunity=opportunity,
                        reference_price=reference_price,
                        now_ts=now_ts,
                    )
                    self._log_opportunity(
                        opportunity=opportunity,
                        market=market,
                        reference_price=reference_price,
                        now_ts=now_ts,
                    )
                    if market.underlying.upper() in overlap_snapshot.blocked_underlyings:
                        decision = SupervisorDecision(
                            approved=False,
                            approved_size_usdc=0.0,
                            reason="directional_pod_overlap",
                            execution_mode=self.config.mode.upper(),
                            constraints={
                                "overlap": overlap_snapshot.to_dict(),
                            },
                        )
                    else:
                        decision = self.risk_manager.evaluate(
                            opportunity=opportunity,
                            market=market,
                            order_book=order_book,
                            open_positions=self._active_positions_for_mode(),
                            now_ts=now_ts,
                        )
                        if decision.approved:
                            decision = self._apply_overlap_lock(market, decision)
                        if decision.approved:
                            decision = self._apply_capital_guard(decision)
                    self._log_decision(opportunity=opportunity, decision=decision)
                    if not decision.approved:
                        continue
                    summary["approved"] = int(summary["approved"]) + 1
                    if executed_this_loop >= self.config.max_opportunities_per_loop:
                        continue
                    stage_started = time.monotonic()
                    result = self._execute(
                        opportunity=opportunity,
                        market=market,
                        order_book=order_book,
                        decision=decision,
                    )
                    self._record_execution_result(
                        market=market,
                        opportunity=opportunity,
                        decision=decision,
                        result=result,
                    )
                    timings["execution_ms"] += _elapsed_ms(stage_started)

                    if result.filled:
                        position = self._position_from_execution(
                            market=market,
                            opportunity=opportunity,
                            decision=decision,
                            result=result,
                        )
                        self.positions.append(position)
                        self.state_store.save_positions(self.positions)
                        self._log_trades(market=market, opportunity=opportunity, result=result)
                        executed_this_loop += 1
                        summary["executed"] = int(summary["executed"]) + 1
            stage_started = time.monotonic()
            self._settle_expired_positions(now_ts=now_ts, reference_prices=reference_prices)
            timings["settlement_ms"] = _elapsed_ms(stage_started)

            stage_started = time.monotonic()
            reconciliation = self._maybe_reconcile_testnet(executed_this_loop=executed_this_loop)
            timings["reconciliation_ms"] = _elapsed_ms(stage_started)
            if reconciliation is not None:
                summary["reconciliation"] = _reconciliation_summary(reconciliation)

            self._write_daily_summary()
            summary["open_positions"] = len(self._active_positions_for_mode())
            self._release_closed_overlap_locks()
            self._refresh_capital_snapshot()
            summary["capital"] = self.last_capital_snapshot
            self.last_error = None
        except Exception as exc:
            logger.exception("HIP-4 outcome pod loop failed")
            self.last_error = str(exc)
            summary["last_error"] = self.last_error
        self.last_summary = summary
        timings["total_ms"] = _elapsed_ms(loop_started)
        stage_started = time.monotonic()
        self._write_status(summary)
        timings["status_ms"] = _elapsed_ms(stage_started)
        self._log_latency(summary=summary, timings=timings)
        return summary

    def _apply_overlap_lock(
        self,
        market: OutcomeMarket,
        decision: SupervisorDecision,
    ) -> SupervisorDecision:
        if not self.config.block_directional_overlap:
            return decision
        underlying = market.underlying.upper()
        if underlying in self._overlap_locks and self._overlap_locks[underlying].acquired:
            return decision
        lock = UnderlyingOverlapLock(underlying=underlying, owner="hip4_outcome")
        if lock.acquire():
            self._overlap_locks[underlying] = lock
            return decision
        return SupervisorDecision(
            approved=False,
            approved_size_usdc=0.0,
            reason="overlap_lock_busy",
            execution_mode=decision.execution_mode,
            constraints={
                **decision.constraints,
                "overlap_lock": {
                    "underlying": underlying,
                    "owner": "other_pod",
                },
            },
        )

    def _apply_capital_guard(self, decision: SupervisorDecision) -> SupervisorDecision:
        try:
            testnet_executor = self._testnet_executor_for_capital()
            decision, snapshot = self.capital_guard.apply(
                decision=decision,
                open_positions=self._active_positions_for_mode(),
                testnet_executor=testnet_executor,
            )
        except Exception as exc:
            snapshot = OutcomeCapitalSnapshot(
                mode=self.config.mode,
                budget_usdc=float(self.config.pod_b_budget_usdc),
                open_exposure_usdc=sum(
                    max(float(position.max_loss_usdc), 0.0)
                    for position in self._active_positions_for_mode()
                ),
                remaining_budget_usdc=0.0,
                approved_size_before_usdc=float(decision.approved_size_usdc),
                approved_size_after_usdc=0.0,
                testnet_balance_coin=self.config.testnet_balance_coin,
                testnet_balance_buffer_usdc=float(self.config.testnet_balance_buffer_usdc),
                reason="capital_guard_error",
                error=str(exc),
            )
            decision = SupervisorDecision(
                approved=False,
                approved_size_usdc=0.0,
                reason="capital_guard_error",
                execution_mode=decision.execution_mode,
                constraints={**decision.constraints, "capital": snapshot.to_dict()},
            )
        self.last_capital_snapshot = snapshot.to_dict()
        return decision

    def _refresh_capital_snapshot(self) -> None:
        open_positions = self._active_positions_for_mode()
        if self.config.mode == "testnet" and self.config.enforce_testnet_balance_check:
            local = self.capital_guard.testnet_balance_snapshot(
                open_positions=open_positions,
                testnet_executor=self._testnet_executor_for_capital(),
            ).to_dict()
        else:
            local = self.capital_guard.local_snapshot(open_positions=open_positions).to_dict()
        for key in ("account_address", "testnet_available_usdc", "testnet_balance_source", "error"):
            if local.get(key) is None and self.last_capital_snapshot.get(key) is not None:
                local[key] = self.last_capital_snapshot[key]
        local["reason"] = self.last_capital_snapshot.get("reason", local.get("reason"))
        self.last_capital_snapshot = local

    def _release_closed_overlap_locks(self) -> None:
        open_underlyings = {
            position.underlying.upper()
            for position in self._active_positions_for_mode()
        }
        for underlying, lock in list(self._overlap_locks.items()):
            if underlying not in open_underlyings:
                lock.release()
                self._overlap_locks.pop(underlying, None)

    def _testnet_executor_for_capital(self) -> TestnetOutcomeExecutor | None:
        if self.config.mode != "testnet" or not self.config.enforce_testnet_balance_check:
            return None
        if self.testnet_executor is None:
            self.testnet_executor = TestnetOutcomeExecutor(self.config)
        return self.testnet_executor

    def _discover_markets(self, *, now_ts: int) -> list[OutcomeMarket]:
        payload = self.info_client.fetch_outcome_meta()
        markets = parse_outcome_markets(
            payload,
            include_underlyings=self.config.include_underlyings,
        )
        self._last_markets_seen = len(markets)
        filtered = [
            market
            for market in markets
            if market.expiry_ts > now_ts + self.config.min_time_to_expiry_seconds
            and market.expiry_ts <= now_ts + self.config.max_time_to_expiry_seconds
        ]
        self._last_supported_underlyings = sorted({market.underlying for market in filtered})
        if self.config.enable_short_expiry:
            filtered.sort(
                key=lambda market: (
                    0 if self._is_short_expiry_candidate(market=market, now_ts=now_ts) else 1,
                    market.expiry_ts,
                    market.underlying,
                    market.outcome,
                )
            )
        return filtered[: max(self.config.max_markets_per_loop, 1)]

    def _is_short_expiry_candidate(self, *, market: OutcomeMarket, now_ts: int) -> bool:
        time_left = market.expiry_ts - now_ts
        if time_left <= self.config.min_time_to_expiry_seconds:
            return False
        if time_left > self.config.short_expiry_window_seconds:
            return False
        allowed_periods = {period.strip().lower() for period in self.config.short_expiry_periods if period.strip()}
        return not allowed_periods or market.period.strip().lower() in allowed_periods

    def _update_short_expiry_history(
        self,
        *,
        reference_prices: dict[str, Any],
        now_ts: int,
    ) -> dict[str, list[dict[str, float]]]:
        if not self.config.enable_short_expiry:
            return {}
        payload = self.state_store.load()
        payload["positions"] = [position.to_dict() for position in self.positions]
        prices = {
            underlying.upper(): reference.price
            for underlying, reference in reference_prices.items()
            if getattr(reference, "price", 0.0) > 0
        }
        history = update_price_history_payload(
            payload,
            prices,
            now_ts=now_ts,
            max_age_seconds=self.config.short_expiry_history_seconds,
            sample_limit=self.config.short_expiry_price_history_limit,
        )
        self.state_store.save(payload)
        return history

    def _short_features_for_market(
        self,
        *,
        market: OutcomeMarket,
        reference_price: float,
        now_ts: int,
        history: dict[str, list[dict[str, float]]],
    ) -> ShortHorizonFeatures | None:
        if not self.config.enable_short_expiry:
            return None
        if market.expiry_ts - now_ts > self.config.short_expiry_window_seconds:
            return None
        return self.short_feature_builder.build(
            market=market,
            reference_price=reference_price,
            now_ts=now_ts,
            history=history,
        )

    def _reference_underlyings(self, *, markets: list[OutcomeMarket], now_ts: int) -> list[str]:
        underlyings = [market.underlying for market in markets]
        for position in self._active_positions_for_mode():
            if now_ts >= position.expiry_ts + self.config.settlement_grace_seconds:
                underlyings.append(position.underlying)
        return list(dict.fromkeys(item.upper() for item in underlyings if item))

    def _execute(
        self,
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
        order_book: Any,
        decision: SupervisorDecision,
    ) -> OutcomeExecutionResult:
        if self.config.mode == "paper":
            return self.paper_executor.execute(
                opportunity=opportunity,
                market=market,
                order_book=order_book,
                approved_size_usdc=decision.approved_size_usdc,
            )
        if self.config.mode == "testnet":
            if self.testnet_executor is None:
                self.testnet_executor = TestnetOutcomeExecutor(self.config)
            return self.testnet_executor.execute(
                opportunity=opportunity,
                market=market,
                order_book=order_book,
                approved_size_usdc=decision.approved_size_usdc,
            )
        return OutcomeExecutionResult(status="observer_signal_only")

    def _position_from_execution(
        self,
        *,
        market: OutcomeMarket,
        opportunity: OutcomeOpportunity,
        decision: SupervisorDecision,
        result: OutcomeExecutionResult,
    ) -> OutcomePosition:
        opened_at = utc_now_iso()
        position_id = f"{market.market_id}:{int(time.time() * 1000)}"
        return OutcomePosition(
            position_id=position_id,
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            edge_type=opportunity.edge_type,
            side=opportunity.side,
            opened_at=opened_at,
            expiry_ts=market.expiry_ts,
            cost_usdc=result.total_cost_usdc,
            max_loss_usdc=min(decision.approved_size_usdc, result.total_cost_usdc or decision.approved_size_usdc),
            net_edge=opportunity.net_edge,
            confidence=opportunity.confidence,
            fills=[fill for fill in result.fills if fill.token_qty > 0],
            metadata={
                "signal": opportunity.to_signal(),
                "decision": {
                    "approved_size_usdc": decision.approved_size_usdc,
                    "reason": decision.reason,
                    "execution_mode": decision.execution_mode,
                    "constraints": decision.constraints,
                },
            },
        )

    def _settle_expired_positions(self, *, now_ts: int, reference_prices: dict[str, Any]) -> None:
        changed = False
        for position in self.positions:
            if position.status != "open":
                continue
            if now_ts < position.expiry_ts + self.config.settlement_grace_seconds:
                continue
            reference = reference_prices.get(position.underlying.upper())
            reference_price = None if reference is None else reference.price
            strike = _position_strike(position)
            if reference_price is None or strike is None:
                continue
            result_yes = reference_price > strike
            payout = 0.0
            for fill in position.fills:
                side_name = fill.side_name.upper()
                if side_name == "YES" and result_yes:
                    payout += float(fill.token_qty)
                if side_name == "NO" and not result_yes:
                    payout += float(fill.token_qty)
            position.status = "estimated_settled"
            position.settled_at = utc_now_iso()
            position.estimated_payout_usdc = round(payout, 8)
            position.metadata["settlement"] = {
                "result": "YES" if result_yes else "NO",
                "reference_price": reference_price,
                "strike": strike,
                "fee_model": self._fee_model_payload(),
                "notes": "estimated_from_reference_price",
            }
            _apply_settlement_accounting(position, self.config)
            self.event_logger.log_settlement(_settlement_row_from_position(position))
            changed = True
        if changed:
            self.state_store.save_positions(self.positions)
            self._write_settlement_summary()

    def _log_opportunity(
        self,
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
        reference_price: float,
        now_ts: int,
    ) -> None:
        self.event_logger.log_opportunity(
            {
                "ts": utc_now_iso(),
                "market_id": opportunity.market_id,
                "outcome": opportunity.outcome,
                "underlying": opportunity.underlying,
                "edge_type": opportunity.edge_type,
                "side": opportunity.side,
                "gross_edge": opportunity.gross_edge,
                "net_edge": opportunity.net_edge,
                "confidence": opportunity.confidence,
                "requested_size_usdc": opportunity.requested_size_usdc,
                "yes_ask": opportunity.metadata.get("yes_ask"),
                "no_ask": opportunity.metadata.get("no_ask"),
                "ref_price": reference_price,
                "strike": market.strike,
                "time_to_expiry": market.expiry_ts - now_ts,
                "reason": opportunity.reason,
            }
        )

    def _log_decision(self, *, opportunity: OutcomeOpportunity, decision: SupervisorDecision) -> None:
        self.event_logger.log_decision(
            {
                "ts": utc_now_iso(),
                "pod": "HIP4OutcomeEdgePod",
                "signal": opportunity.to_signal(),
                "supervisor_decision": {
                    "approved": decision.approved,
                    "approved_size_usdc": decision.approved_size_usdc,
                    "reason": decision.reason,
                    "execution_mode": decision.execution_mode,
                    "constraints": decision.constraints,
                },
            }
        )

    def _log_short_expiry_assessment(
        self,
        *,
        market: OutcomeMarket,
        assessment: ShortExpiryAssessment,
        now_ts: int,
    ) -> None:
        metadata = assessment.metadata
        self.event_logger.log_short_expiry_features(
            {
                "ts": utc_now_iso(),
                "market_id": market.market_id,
                "outcome": market.outcome,
                "underlying": market.underlying,
                "period": market.period,
                "seconds_left": market.expiry_ts - now_ts,
                "reference_price": metadata.get("reference_price"),
                "strike": market.strike,
                "distance_bps": metadata.get("short_distance_to_strike_bps"),
                "history_span_seconds": metadata.get("short_history_span_seconds"),
                "sample_count": metadata.get("short_sample_count"),
                "momentum_bps_30s": metadata.get("short_momentum_bps_30s"),
                "momentum_bps_60s": metadata.get("short_momentum_bps_60s"),
                "momentum_bps_180s": metadata.get("short_momentum_bps_180s"),
                "velocity_bps_per_minute": metadata.get("short_velocity_bps_per_minute"),
                "realized_vol_bps_60s": metadata.get("short_realized_vol_bps_60s"),
                "book_probability_yes": assessment.book_probability_yes,
                "book_imbalance_yes": assessment.book_imbalance_yes,
                "model_probability_yes": metadata.get("model_probability_yes"),
                "short_probability_yes": assessment.probability_yes,
                "yes_bid": metadata.get("yes_bid"),
                "yes_ask": metadata.get("yes_ask"),
                "no_bid": metadata.get("no_bid"),
                "no_ask": metadata.get("no_ask"),
                "best_side": assessment.best_side,
                "best_gross_edge": assessment.best_gross_edge,
                "best_net_edge": assessment.best_net_edge,
                "confidence": assessment.confidence,
                "reason": assessment.reason,
            }
        )

    def _log_trades(
        self,
        *,
        market: OutcomeMarket,
        opportunity: OutcomeOpportunity,
        result: OutcomeExecutionResult,
    ) -> None:
        for fill in result.fills:
            self.event_logger.log_trade(
                {
                    "ts": utc_now_iso(),
                    "market_id": market.market_id,
                    "outcome": market.outcome,
                    "underlying": market.underlying,
                    "edge_type": opportunity.edge_type,
                    "side": opportunity.side,
                    "coin": fill.coin,
                    "price": fill.avg_price,
                    "size_usdc": fill.cost_usdc,
                    "token_qty": str(fill.token_qty),
                    "status": fill.status,
                    "oid": fill.oid,
                    "cloid": fill.cloid,
                }
            )

    def _record_execution_result(
        self,
        *,
        market: OutcomeMarket,
        opportunity: OutcomeOpportunity,
        decision: SupervisorDecision,
        result: OutcomeExecutionResult,
    ) -> None:
        payload = {
            "ts": utc_now_iso(),
            "market_id": market.market_id,
            "outcome": market.outcome,
            "underlying": market.underlying,
            "edge_type": opportunity.edge_type,
            "side": opportunity.side,
            "approved_size_usdc": decision.approved_size_usdc,
            "status": result.status,
            "filled": result.filled,
            "total_cost_usdc": result.total_cost_usdc,
            "error": result.error,
            "fills": [fill.to_dict() for fill in result.fills],
            "raw": result.to_dict().get("raw"),
        }
        self.event_logger.log_execution_result(payload)
        self.last_execution_results.append(payload)
        self.last_execution_results = self.last_execution_results[-20:]

    def _record_edge_decay(
        self,
        *,
        opportunity: OutcomeOpportunity,
        reference_price: float,
        now_ts: int,
    ) -> None:
        if not self.config.enable_edge_decay_log:
            return
        key = f"{opportunity.market_id}:{opportunity.edge_type}:{opportunity.side}"
        payload = self.state_store.load()
        payload["positions"] = [position.to_dict() for position in self.positions]
        observations = payload.get("edge_observations", {})
        if not isinstance(observations, dict):
            observations = {}
        current = observations.get(key)
        if not isinstance(current, dict):
            current = {
                "first_seen_at": utc_now_iso(),
                "first_seen_ts": now_ts,
                "first_net_edge": opportunity.net_edge,
            }
        first_ts = _int_from_any(current.get("first_seen_ts"), now_ts)
        first_edge = _float_from_any(current.get("first_net_edge"), opportunity.net_edge)
        observations[key] = {
            **current,
            "last_seen_at": utc_now_iso(),
            "last_seen_ts": now_ts,
            "last_net_edge": opportunity.net_edge,
        }
        observations = _trim_observations(
            observations,
            limit=max(self.config.edge_decay_state_limit, 1),
        )
        payload["edge_observations"] = observations
        self.state_store.save(payload)
        self.event_logger.log_edge_decay(
            {
                "ts": utc_now_iso(),
                "market_id": opportunity.market_id,
                "underlying": opportunity.underlying,
                "edge_type": opportunity.edge_type,
                "side": opportunity.side,
                "first_seen_at": current.get("first_seen_at"),
                "first_net_edge": first_edge,
                "current_net_edge": opportunity.net_edge,
                "delta_net_edge": round(opportunity.net_edge - first_edge, 8),
                "elapsed_seconds": max(now_ts - first_ts, 0),
                "ref_price": reference_price,
                "yes_ask": opportunity.metadata.get("yes_ask"),
                "no_ask": opportunity.metadata.get("no_ask"),
                "source_count": opportunity.metadata.get("reference_source_count"),
            }
        )

    def _maybe_reconcile_testnet(self, *, executed_this_loop: int) -> dict[str, Any] | None:
        if self.config.mode != "testnet":
            return None
        if not self.config.reconcile_after_execution:
            return None
        reconcile_every = max(int(self.config.reconcile_every_loops), 0)
        if executed_this_loop <= 0 and (
            reconcile_every <= 0 or self.loop_count % reconcile_every != 0
        ):
            return None
        open_positions = self._active_positions_for_mode()
        if not open_positions or self.testnet_executor is None:
            return None
        account_address = self.testnet_executor.account_address
        if not account_address:
            return None
        report = self.reconciler.reconcile(
            account_address=account_address,
            positions=open_positions,
            start_time_ms=_fills_start_time_ms(
                open_positions,
                lookback_hours=self.config.fills_lookback_hours,
            ),
        )
        self.event_logger.log_reconciliation(report)
        if apply_reconciliation_to_positions(self.positions, report):
            self.state_store.save_positions(self.positions)
        return report

    def _sync_settlement_accounting(self) -> None:
        changed = False
        for position in self.positions:
            if position.status not in {"estimated_settled", "settled"}:
                continue
            changed = _apply_settlement_accounting(position, self.config) or changed
        if changed:
            self.state_store.save_positions(self.positions)
        self._write_settlement_summary()

    def _write_settlement_summary(self) -> None:
        rows = [
            _settlement_row_from_position(position)
            for position in self.positions
            if position.status in {"estimated_settled", "settled"}
        ]
        if not rows and self.event_logger.settlements_path.exists():
            return
        rows.sort(key=lambda row: str(row.get("ts", "")))
        self.event_logger.write_settlements(rows)

    def _fee_model_payload(self) -> dict[str, Any]:
        return {
            "open_fee_rate": float(self.config.outcome_open_fee_rate),
            "settlement_fee_rate": float(self.config.outcome_settlement_fee_rate),
            "estimated_edge_fee_rate": float(self.config.estimated_fees),
            "fee_timing": "open_fee_zero_settlement_fee_on_payout",
        }

    def _write_daily_summary(self) -> None:
        if not self.config.enable_daily_summary:
            return
        self._write_settlement_summary()
        self.event_logger.write_daily_summary(build_daily_summary_rows(self.positions))

    def _log_latency(self, *, summary: dict[str, Any], timings: dict[str, float]) -> None:
        if not self.config.enable_latency_log:
            return
        self.event_logger.log_latency(
            {
                "ts": utc_now_iso(),
                "loop_count": self.loop_count,
                "mode": self.config.mode,
                "markets_seen": summary.get("markets_seen", 0),
                "markets_supported": summary.get("markets_supported", 0),
                "opportunities": summary.get("opportunities", 0),
                "executed": summary.get("executed", 0),
                "total_ms": timings.get("total_ms", 0.0),
                "fetch_mids_ms": timings.get("fetch_mids_ms", 0.0),
                "discover_markets_ms": timings.get("discover_markets_ms", 0.0),
                "reference_prices_ms": timings.get("reference_prices_ms", 0.0),
                "short_features_ms": timings.get("short_features_ms", 0.0),
                "books_ms": round(timings.get("books_ms", 0.0), 3),
                "edge_detection_ms": round(timings.get("edge_detection_ms", 0.0), 3),
                "execution_ms": round(timings.get("execution_ms", 0.0), 3),
                "settlement_ms": timings.get("settlement_ms", 0.0),
                "reconciliation_ms": timings.get("reconciliation_ms", 0.0),
                "status_ms": timings.get("status_ms", 0.0),
                "error": self.last_error or "",
            }
        )

    def _write_status(self, summary: dict[str, Any]) -> None:
        open_positions = self._active_positions_for_mode()
        updated_at = utc_now_iso()
        payload = {
            "pod": "hip4_outcome_edge_pod",
            "process_state": "running",
            "mode": self.config.mode,
            "updated_at": updated_at,
            "poll_seconds": self.config.loop_interval_seconds,
            "summary": summary,
            "capital": self.last_capital_snapshot,
            "last_error": self.last_error,
            "last_execution_results": self.last_execution_results,
            "open_positions": [position.to_dict() for position in open_positions],
            "settled_positions": [
                position.to_dict()
                for position in self.positions
                if position.status in {"estimated_settled", "settled"}
                and _position_execution_mode(position) == self.config.mode.upper()
            ],
            "fee_model": self._fee_model_payload(),
            "logs_dir": str(Path(self.config.logs_dir)),
            "state_path": self.config.state_path,
        }
        write_runtime_status(self.config.status_path, payload)
        if self.config.write_pod_b_alias_status:
            write_runtime_status(
                self.config.pod_b_alias_status_path,
                self._pod_b_alias_payload(
                    summary=summary,
                    open_positions=open_positions,
                    updated_at=updated_at,
                ),
            )

    def _pod_b_alias_payload(
        self,
        *,
        summary: dict[str, Any],
        open_positions: list[OutcomePosition],
        updated_at: str,
    ) -> dict[str, Any]:
        mode_positions = [
            position
            for position in self.positions
            if _position_execution_mode(position) == self.config.mode.upper()
        ]
        settled_positions = [
            position
            for position in mode_positions
            if position.status in {"estimated_settled", "settled"}
        ]
        settlement_payout_usdc = round(
            sum(float(position.estimated_payout_usdc) for position in settled_positions),
            8,
        )
        fees_usd = round(
            sum(float(position.estimated_fee_usdc) for position in settled_positions),
            8,
        )
        gross_pnl_usd = round(
            sum(float(position.estimated_gross_pnl_usdc) for position in settled_positions),
            8,
        )
        realized_pnl_usd = round(
            sum(float(position.estimated_pnl_usdc) for position in settled_positions),
            8,
        )
        total_fill_count = sum(
            1
            for position in mode_positions
            for fill in position.fills
            if fill.token_qty > 0
        )
        managed_symbols = sorted(
            {str(position.underlying).upper() for position in open_positions}
            or {str(item).upper() for item in self._last_supported_underlyings}
            or {str(item).upper() for item in self.config.include_underlyings}
        )
        position_payloads = [position.to_dict() for position in open_positions]
        return {
            "pod": "pod_b",
            "pod_kind": "hip4_outcome_edge_pod",
            "strategy": "HIP4OutcomeEdgePod",
            "process_state": "running",
            "mode": self.config.mode,
            "updated_at": updated_at,
            "poll_seconds": self.config.loop_interval_seconds,
            "status_path": self.config.pod_b_alias_status_path,
            "hip4_outcome_status_path": self.config.status_path,
            "logs_dir": str(Path(self.config.logs_dir)),
            "state_path": self.config.state_path,
            "managed_symbols": managed_symbols,
            "open_positions": position_payloads,
            "settled_positions": [position.to_dict() for position in settled_positions],
            "position_count": len(position_payloads),
            "total_position_count": len(position_payloads),
            "open_order_count": 0,
            "total_open_order_count": 0,
            "total_fill_count": total_fill_count,
            "recent_fill_count": int(summary.get("executed", 0) or 0),
            "realized_pnl_usd": realized_pnl_usd,
            "gross_pnl_usd": gross_pnl_usd,
            "fees_usd": fees_usd,
            "settlement_payout_usdc": settlement_payout_usdc,
            "total_unrealized_pnl_usd": 0.0,
            "healthy": self.last_error is None,
            "last_error": self.last_error,
            "last_execution_results": self.last_execution_results,
            "summary": summary,
            "capital": self.last_capital_snapshot,
            "fee_model": self._fee_model_payload(),
            "report": {
                "strategy": "HIP4OutcomeEdgePod",
                "closed_trade_count": len(settled_positions),
                "total_fill_count": total_fill_count,
                "realized_pnl_usd": realized_pnl_usd,
                "gross_pnl_usd": gross_pnl_usd,
                "fees_usd": fees_usd,
                "settlement_payout_usdc": settlement_payout_usdc,
                "open_position_count": len(position_payloads),
                "loop_count": summary.get("loop_count", 0),
                "opportunities": summary.get("opportunities", 0),
                "approved": summary.get("approved", 0),
                "executed": summary.get("executed", 0),
            },
        }

    def _active_positions_for_mode(self) -> list[OutcomePosition]:
        return [
            position
            for position in self.positions
            if position.status == "open"
            and _position_execution_mode(position) == self.config.mode.upper()
        ]


def _position_strike(position: OutcomePosition) -> float | None:
    signal = position.metadata.get("signal", {})
    if not isinstance(signal, dict):
        return None
    metadata = signal.get("metadata", {})
    if not isinstance(metadata, dict):
        return None
    try:
        return float(metadata.get("strike"))
    except (TypeError, ValueError):
        return None


def _position_execution_mode(position: OutcomePosition) -> str:
    metadata = position.metadata.get("decision", {})
    if isinstance(metadata, dict):
        mode = str(metadata.get("execution_mode", "")).strip().upper()
        if mode:
            return mode
    return ""


def _apply_settlement_accounting(
    position: OutcomePosition,
    config: Hip4OutcomeConfig,
) -> bool:
    payout = max(float(position.estimated_payout_usdc or 0.0), 0.0)
    gross_pnl = round(payout - float(position.cost_usdc or 0.0), 8)
    fee = round(payout * max(float(config.outcome_settlement_fee_rate), 0.0), 8)
    net_pnl = round(gross_pnl - fee, 8)
    changed = (
        position.estimated_fee_usdc != fee
        or position.estimated_gross_pnl_usdc != gross_pnl
        or position.estimated_pnl_usdc != net_pnl
    )
    position.estimated_fee_usdc = fee
    position.estimated_gross_pnl_usdc = gross_pnl
    position.estimated_pnl_usdc = net_pnl
    settlement = position.metadata.get("settlement")
    if not isinstance(settlement, dict):
        settlement = {}
    expected_fee_model = {
        "open_fee_rate": float(config.outcome_open_fee_rate),
        "settlement_fee_rate": float(config.outcome_settlement_fee_rate),
        "estimated_edge_fee_rate": float(config.estimated_fees),
        "fee_timing": "open_fee_zero_settlement_fee_on_payout",
    }
    if settlement.get("fee_model") != expected_fee_model:
        settlement["fee_model"] = expected_fee_model
        position.metadata["settlement"] = settlement
        changed = True
    return changed


def _settlement_row_from_position(position: OutcomePosition) -> dict[str, Any]:
    settlement = position.metadata.get("settlement", {})
    if not isinstance(settlement, dict):
        settlement = {}
    result = str(settlement.get("result") or _infer_settlement_result(position))
    notes = str(settlement.get("notes") or "estimated_from_reference_price")
    fee_model = settlement.get("fee_model", {})
    if isinstance(fee_model, dict) and fee_model.get("fee_timing"):
        notes = f"{notes}; {fee_model['fee_timing']}"
    return {
        "ts": position.settled_at or position.opened_at,
        "market_id": position.market_id,
        "outcome": position.outcome,
        "underlying": position.underlying,
        "side": position.side,
        "result": result,
        "payout_usdc": position.estimated_payout_usdc,
        "fee_usdc": position.estimated_fee_usdc,
        "gross_pnl_usdc": position.estimated_gross_pnl_usdc,
        "net_pnl_usdc": position.estimated_pnl_usdc,
        "pnl_usdc": position.estimated_pnl_usdc,
        "notes": notes,
    }


def _infer_settlement_result(position: OutcomePosition) -> str:
    if position.side == "BUY_YES":
        return "YES" if position.estimated_payout_usdc > 0 else "NO"
    if position.side == "BUY_NO":
        return "NO" if position.estimated_payout_usdc > 0 else "YES"
    return "-"


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 3)


def _reconciliation_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": report.get("ts"),
        "tracked_coin_count": report.get("tracked_coin_count", 0),
        "recent_fill_count": report.get("recent_fill_count", 0),
        "matched_fill_count": report.get("matched_fill_count", 0),
        "open_position_count": report.get("open_position_count", 0),
        "unknown_outcome_balance_count": len(report.get("unknown_outcome_balances", {}) or {}),
    }


def _fills_start_time_ms(
    positions: list[OutcomePosition],
    *,
    lookback_hours: float,
) -> int:
    now_ms = int(time.time() * 1000)
    configured = int(now_ms - max(float(lookback_hours), 0.0) * 60.0 * 60.0 * 1000.0)
    opened = [_iso_to_epoch_ms(position.opened_at) for position in positions]
    opened = [item for item in opened if item is not None]
    if not opened:
        return configured
    return min(configured, min(opened) - 60_000)


def _iso_to_epoch_ms(value: str) -> int | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _trim_observations(observations: dict[str, Any], *, limit: int) -> dict[str, Any]:
    if len(observations) <= limit:
        return observations
    ordered = sorted(
        observations.items(),
        key=lambda item: _int_from_any(
            item[1].get("last_seen_ts", item[1].get("first_seen_ts", 0))
            if isinstance(item[1], dict)
            else 0,
            0,
        ),
        reverse=True,
    )
    return dict(ordered[:limit])


def _float_from_any(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_from_any(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
