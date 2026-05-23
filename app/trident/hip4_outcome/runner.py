from __future__ import annotations

import csv
import logging
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from app.live.runtime_status import write_runtime_status
from app.trident.hip4_outcome.book import build_order_book, parse_side_book
from app.trident.hip4_outcome.capital import OutcomeCapitalGuard, OutcomeCapitalSnapshot
from app.trident.hip4_outcome.client import HIP4OutcomeInfoClient
from app.trident.hip4_outcome.config import Hip4OutcomeConfig, load_hip4_outcome_config
from app.trident.hip4_outcome.edge import OutcomeEdgeDetector
from app.trident.hip4_outcome.execution import PaperOutcomeExecutor, TestnetOutcomeExecutor
from app.trident.hip4_outcome.external_prices import ExternalPriceAggregator
from app.trident.hip4_outcome.features import (
    ShortHorizonFeatureBuilder,
    update_price_history_payload,
)
from app.trident.hip4_outcome.logging import OutcomeEventLogger
from app.trident.hip4_outcome.models import (
    OutcomeExecutionResult,
    OutcomeMarket,
    OutcomeOpportunity,
    OutcomeOrderBook,
    OutcomePosition,
    OutcomeSideBook,
    ShortExpiryAssessment,
    ShortHorizonFeatures,
    SupervisorDecision,
    outcome_coin,
    utc_now_iso,
)
from app.trident.hip4_outcome.parser import parse_outcome_markets, parse_outcome_observations
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
        self._last_markets_seen = 0
        self._last_supported_underlyings: list[str] = []
        self._last_market_observation: dict[str, Any] = {}
        self._last_named_outcome_basket_watchlist: list[dict[str, Any]] = []
        self._embedded_observer_threads: list[threading.Thread] = []
        self._embedded_observer_stop = threading.Event()
        self._embedded_observer_lock = threading.Lock()
        self._embedded_observer_summaries: dict[str, dict[str, Any]] = {}

    def run(
        self,
        *,
        max_loops: int | None = None,
        max_runtime_seconds: float | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        self._start_embedded_observers(looping=True)
        try:
            while True:
                summary = self.run_once()
                if max_loops is not None and self.loop_count >= max_loops:
                    return summary
                if max_runtime_seconds is not None and time.monotonic() - started >= max_runtime_seconds:
                    return summary
                time.sleep(max(self.config.loop_interval_seconds, 0.1))
        finally:
            self._stop_embedded_observers()

    def run_once(self) -> dict[str, Any]:
        one_shot_observers = self._start_embedded_observers(looping=False)
        self.loop_count += 1
        self.last_execution_results = []
        now_ts = int(time.time())
        loop_started = time.monotonic()
        timings: dict[str, float] = {
            "fetch_mids_ms": 0.0,
            "discover_markets_ms": 0.0,
            "market_observation_ms": 0.0,
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
            "early_exit_evaluations": 0,
            "early_exits": 0,
            "early_exit_net_usdc": 0.0,
            "shadow_exit_policy_evaluations": 0,
            "shadow_exit_policy_exits": 0,
            "shadow_exit_policy_settlements": 0,
            "shadow_sizing_evaluations": 0,
            "shadow_maker_quotes": 0,
            "short_expiry_markets": 0,
            "short_expiry_assessments": 0,
            "short_expiry_best_net_edge": None,
            "short_expiry_ready_count": 0,
            "short_expiry_watchlist": [],
            "next_short_expiry_seconds": None,
            "named_outcome_baskets": 0,
            "named_outcome_basket_opportunities": 0,
            "named_outcome_basket_watchlist": [],
            "decision_reasons": {},
            "opportunity_mix": {},
            "operator_brief": {},
            "market_observation": {},
            "embedded_observers": self._embedded_observer_status(),
            "shock_guard": {},
            "reconciliation": None,
            "capital": self.capital_guard.local_snapshot(
                open_positions=self._active_positions_for_mode()
            ).to_dict(),
        }
        decision_reasons: Counter[str] = Counter()
        opportunity_mix: Counter[str] = Counter()
        try:
            stage_started = time.monotonic()
            mids = self.info_client.fetch_all_mids()
            timings["fetch_mids_ms"] = _elapsed_ms(stage_started)

            stage_started = time.monotonic()
            outcome_meta = self.info_client.fetch_outcome_meta()
            markets = self._discover_markets(now_ts=now_ts, payload=outcome_meta)
            timings["discover_markets_ms"] = _elapsed_ms(stage_started)

            stage_started = time.monotonic()
            market_observation = self._observe_market_metadata(payload=outcome_meta, now_ts=now_ts)
            timings["market_observation_ms"] = _elapsed_ms(stage_started)
            summary["market_observation"] = market_observation

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
            shock_price_history = self._update_shock_guard_history(
                reference_prices=reference_prices,
                now_ts=now_ts,
            )
            timings["short_features_ms"] = _elapsed_ms(stage_started)
            summary["shock_guard"] = {
                "enabled": bool(self.config.enable_shock_guard),
                "history_underlyings": len(shock_price_history),
                "windows_seconds": list(self.config.shock_guard_windows_seconds),
                "adverse_move_bps": list(self.config.shock_guard_adverse_move_bps),
                "min_adverse_windows": int(self.config.shock_guard_min_adverse_windows),
            }
            summary["pnl_levers"] = _pnl_levers_payload(self.config)

            summary["markets_seen"] = self._last_markets_seen
            summary["markets_supported"] = len(markets)
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
                shock_assessment = self._shock_guard_assessment(
                    market=market,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    history=shock_price_history,
                )
                short_assessment = self.edge_detector.assess_short_expiry(
                    market=market,
                    order_book=order_book,
                    probability=probability,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    features=short_features,
                )
                if self._is_short_expiry_candidate(market=market, now_ts=now_ts):
                    self._append_short_expiry_watchlist(
                        summary=summary,
                        row=_short_expiry_watchlist_row(
                            market=market,
                            order_book=order_book,
                            reference_price=reference_price,
                            now_ts=now_ts,
                            short_features=short_features,
                            short_assessment=short_assessment,
                            config=self.config,
                        ),
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
                self._manage_shadow_exit_policies_for_market(
                    market=market,
                    order_book=order_book,
                    probability=probability,
                    short_assessment=short_assessment,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    summary=summary,
                )
                early_exit_closed = self._manage_early_exits_for_market(
                    market=market,
                    order_book=order_book,
                    probability=probability,
                    short_assessment=short_assessment,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    summary=summary,
                )
                if early_exit_closed or self._recent_early_exit_for_market(
                    market.market_id,
                    now_ts=now_ts,
                ):
                    decision_reasons["early_exit_reentry_cooldown"] += 1
                    continue
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
                opportunity_mix.update(opportunity.edge_type for opportunity in opportunities)
                for opportunity in sorted(opportunities, key=lambda item: item.net_edge, reverse=True):
                    opportunity.metadata.update(reference.to_metadata())
                    if shock_assessment:
                        opportunity.metadata["shock_guard"] = shock_assessment
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
                    decision = self.risk_manager.evaluate(
                        opportunity=opportunity,
                        market=market,
                        order_book=order_book,
                        open_positions=self._active_positions_for_mode(),
                        now_ts=now_ts,
                    )
                    if decision.approved:
                        decision = self._apply_capital_guard(decision)
                    decision_reasons[decision.reason] += 1
                    self._log_shadow_sizing(
                        opportunity=opportunity,
                        market=market,
                        order_book=order_book,
                        decision=decision,
                        reference_price=reference_price,
                        now_ts=now_ts,
                        summary=summary,
                    )
                    self._log_shadow_maker_quote(
                        opportunity=opportunity,
                        market=market,
                        order_book=order_book,
                        decision=decision,
                        reference_price=reference_price,
                        now_ts=now_ts,
                        summary=summary,
                    )
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
            named_basket_opportunities = self._named_outcome_basket_opportunities(
                payload=outcome_meta,
                anchor_markets=markets,
                now_ts=now_ts,
            )
            timings["edge_detection_ms"] += _elapsed_ms(stage_started)
            summary["named_outcome_baskets"] = len(named_basket_opportunities)
            summary["named_outcome_basket_opportunities"] = len(named_basket_opportunities)
            summary["named_outcome_basket_watchlist"] = list(
                self._last_named_outcome_basket_watchlist
            )
            summary["opportunities"] = int(summary["opportunities"]) + len(
                named_basket_opportunities
            )
            opportunity_mix.update(
                opportunity.edge_type for _, _, opportunity in named_basket_opportunities
            )
            for basket_market, basket_book, opportunity in sorted(
                named_basket_opportunities,
                key=lambda item: item[2].net_edge,
                reverse=True,
            ):
                reference = reference_prices.get(basket_market.underlying.upper())
                reference_price = 0.0 if reference is None else reference.price
                if reference is not None:
                    opportunity.metadata.update(reference.to_metadata())
                shock_assessment = self._shock_guard_assessment(
                    market=basket_market,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    history=shock_price_history,
                )
                if shock_assessment:
                    opportunity.metadata["shock_guard"] = shock_assessment
                self._record_edge_decay(
                    opportunity=opportunity,
                    reference_price=reference_price,
                    now_ts=now_ts,
                )
                self._log_opportunity(
                    opportunity=opportunity,
                    market=basket_market,
                    reference_price=reference_price,
                    now_ts=now_ts,
                )
                decision = self.risk_manager.evaluate(
                    opportunity=opportunity,
                    market=basket_market,
                    order_book=basket_book,
                    open_positions=self._active_positions_for_mode(),
                    now_ts=now_ts,
                )
                if decision.approved:
                    decision = self._apply_capital_guard(decision)
                decision_reasons[decision.reason] += 1
                self._log_shadow_sizing(
                    opportunity=opportunity,
                    market=basket_market,
                    order_book=basket_book,
                    decision=decision,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    summary=summary,
                )
                self._log_shadow_maker_quote(
                    opportunity=opportunity,
                    market=basket_market,
                    order_book=basket_book,
                    decision=decision,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    summary=summary,
                )
                self._log_decision(opportunity=opportunity, decision=decision)
                if not decision.approved:
                    continue
                summary["approved"] = int(summary["approved"]) + 1
                if executed_this_loop >= self.config.max_opportunities_per_loop:
                    continue
                stage_started = time.monotonic()
                result = self._execute(
                    opportunity=opportunity,
                    market=basket_market,
                    order_book=basket_book,
                    decision=decision,
                )
                self._record_execution_result(
                    market=basket_market,
                    opportunity=opportunity,
                    decision=decision,
                    result=result,
                )
                timings["execution_ms"] += _elapsed_ms(stage_started)

                if result.filled:
                    position = self._position_from_execution(
                        market=basket_market,
                        opportunity=opportunity,
                        decision=decision,
                        result=result,
                    )
                    self.positions.append(position)
                    self.state_store.save_positions(self.positions)
                    self._log_trades(market=basket_market, opportunity=opportunity, result=result)
                    executed_this_loop += 1
                    summary["executed"] = int(summary["executed"]) + 1
            stage_started = time.monotonic()
            self._settle_expired_positions(
                now_ts=now_ts,
                reference_prices=reference_prices,
                summary=summary,
            )
            timings["settlement_ms"] = _elapsed_ms(stage_started)

            stage_started = time.monotonic()
            reconciliation = self._maybe_reconcile_testnet(executed_this_loop=executed_this_loop)
            timings["reconciliation_ms"] = _elapsed_ms(stage_started)
            if reconciliation is not None:
                summary["reconciliation"] = _reconciliation_summary(reconciliation)

            self._write_daily_summary()
            summary["open_positions"] = len(self._active_positions_for_mode())
            self._refresh_capital_snapshot()
            summary["capital"] = self.last_capital_snapshot
            self.last_error = None
        except Exception as exc:
            logger.exception("HIP-4 outcome pod loop failed")
            self.last_error = str(exc)
            summary["last_error"] = self.last_error
        self._finalize_operator_summary(
            summary=summary,
            decision_reasons=decision_reasons,
            opportunity_mix=opportunity_mix,
        )
        self._join_one_shot_observers(one_shot_observers)
        summary["embedded_observers"] = self._embedded_observer_status()
        self.last_summary = summary
        timings["total_ms"] = _elapsed_ms(loop_started)
        stage_started = time.monotonic()
        self._write_status(summary)
        timings["status_ms"] = _elapsed_ms(stage_started)
        self._log_latency(summary=summary, timings=timings)
        return summary

    def _start_embedded_observers(self, *, looping: bool) -> list[threading.Thread]:
        if not self.config.enable_embedded_observers:
            return []
        config_paths = [path for path in self.config.embedded_observer_config_paths if path]
        if not config_paths:
            return []
        if looping:
            with self._embedded_observer_lock:
                if self._embedded_observer_threads:
                    return []
                self._embedded_observer_stop.clear()
                for path in config_paths:
                    thread = threading.Thread(
                        target=self._embedded_observer_loop,
                        args=(path,),
                        name=f"hip4-observer:{Path(path).stem}",
                        daemon=True,
                    )
                    self._embedded_observer_threads.append(thread)
                    thread.start()
            return []
        with self._embedded_observer_lock:
            if self._embedded_observer_threads:
                return []
        threads: list[threading.Thread] = []
        for path in config_paths:
            thread = threading.Thread(
                target=self._embedded_observer_once,
                args=(path,),
                name=f"hip4-observer-once:{Path(path).stem}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        return threads

    def _stop_embedded_observers(self) -> None:
        with self._embedded_observer_lock:
            threads = list(self._embedded_observer_threads)
            self._embedded_observer_threads = []
            self._embedded_observer_stop.set()
        for thread in threads:
            thread.join(timeout=max(float(self.config.loop_interval_seconds), 1.0) + 2.0)

    def _join_one_shot_observers(self, threads: list[threading.Thread]) -> None:
        timeout = max(float(self.config.embedded_observer_once_timeout_seconds), 0.1)
        started = time.monotonic()
        for thread in threads:
            remaining = max(timeout - (time.monotonic() - started), 0.1)
            thread.join(timeout=remaining)

    def _embedded_observer_loop(self, config_path: str) -> None:
        pod = self._build_embedded_observer(config_path)
        while not self._embedded_observer_stop.is_set():
            self._run_embedded_observer_pod(config_path=config_path, pod=pod)
            sleep_seconds = max(float(pod.config.loop_interval_seconds), 0.1)
            self._embedded_observer_stop.wait(timeout=sleep_seconds)

    def _embedded_observer_once(self, config_path: str) -> None:
        self._run_embedded_observer_pod(
            config_path=config_path,
            pod=self._build_embedded_observer(config_path),
        )

    def _build_embedded_observer(self, config_path: str) -> "HIP4OutcomeEdgePod":
        observer_config = load_hip4_outcome_config(config_path, apply_env=False)
        observer_config = replace(
            observer_config,
            mode="observer",
            allow_testnet_orders=False,
            write_pod_b_alias_status=False,
            enable_embedded_observers=False,
        )
        return HIP4OutcomeEdgePod(observer_config)

    def _run_embedded_observer_pod(
        self,
        *,
        config_path: str,
        pod: "HIP4OutcomeEdgePod",
    ) -> None:
        try:
            summary = pod.run_once()
            payload = {
                "config_path": config_path,
                "status_path": pod.config.status_path,
                "logs_dir": pod.config.logs_dir,
                "mode": pod.config.mode,
                "updated_at": utc_now_iso(),
                "summary": summary,
                "last_error": pod.last_error,
            }
        except Exception as exc:
            logger.exception("Embedded HIP-4 observer failed: %s", config_path)
            payload = {
                "config_path": config_path,
                "updated_at": utc_now_iso(),
                "last_error": str(exc),
            }
        with self._embedded_observer_lock:
            self._embedded_observer_summaries[config_path] = payload

    def _embedded_observer_status(self) -> dict[str, Any]:
        with self._embedded_observer_lock:
            return {
                "enabled": bool(self.config.enable_embedded_observers),
                "config_paths": list(self.config.embedded_observer_config_paths),
                "running_threads": len(self._embedded_observer_threads),
                "observers": dict(self._embedded_observer_summaries),
            }

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

    def _testnet_executor_for_capital(self) -> TestnetOutcomeExecutor | None:
        if self.config.mode != "testnet" or not self.config.enforce_testnet_balance_check:
            return None
        if self.testnet_executor is None:
            self.testnet_executor = TestnetOutcomeExecutor(self.config)
        return self.testnet_executor

    def _discover_markets(self, *, now_ts: int, payload: object | None = None) -> list[OutcomeMarket]:
        if payload is None:
            payload = self.info_client.fetch_outcome_meta()
        observations = parse_outcome_observations(payload)
        markets = parse_outcome_markets(
            payload,
            include_underlyings=self.config.include_underlyings,
        )
        self._last_markets_seen = len(observations)
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

    def _observe_market_metadata(self, *, payload: object, now_ts: int) -> dict[str, Any]:
        if not self.config.enable_market_observation:
            return {}
        observations = parse_outcome_observations(payload)
        by_class = Counter(observation.class_name or "unknown" for observation in observations)
        by_support = Counter(observation.support_status for observation in observations)
        books_logged = 0
        max_markets = max(int(self.config.max_observation_markets_per_loop), 0)
        max_books = max(int(self.config.max_observation_books_per_loop), 0)
        for observation in observations[:max_markets]:
            book_payload: dict[str, Any] = {}
            should_fetch_books = (
                self.config.observe_unsupported_books
                and observation.support_status != "trading_supported"
                and books_logged < max_books
            )
            if should_fetch_books:
                book_payload = self._observation_books(observation.coins)
                if book_payload:
                    books_logged += 1
            self.event_logger.log_market_observation(
                {
                    "ts": utc_now_iso(),
                    "mode": self.config.mode,
                    "now_ts": now_ts,
                    **observation.to_dict(),
                    "books": book_payload,
                }
            )
        summary = {
            "total": len(observations),
            "by_class": dict(sorted(by_class.items())),
            "by_support_status": dict(sorted(by_support.items())),
            "observe_only_count": by_support.get("observe_only", 0),
            "paper_supported_count": by_support.get("paper_supported", 0),
            "trading_supported_count": by_support.get("trading_supported", 0),
            "price_bucket_count": by_class.get("priceBucket", 0),
            "named_outcome_count": by_class.get("namedOutcome", 0),
            "books_logged": books_logged,
        }
        self._last_market_observation = summary
        return summary

    def _observation_books(self, coins: tuple[str, ...]) -> dict[str, Any]:
        books: dict[str, Any] = {}
        for index, coin in enumerate(coins[:2]):
            side = "yes" if index == 0 else "no"
            try:
                parsed = parse_side_book(
                    self.info_client.fetch_l2_book(coin),
                    max_slippage=self.config.max_order_slippage,
                )
            except Exception as exc:
                books[side] = {"coin": coin, "error": str(exc)}
                continue
            books[side] = {
                "coin": coin,
                "bid": parsed.bid,
                "ask": parsed.ask,
                "bid_size": parsed.bid_size,
                "ask_size": parsed.ask_size,
                "bid_depth_usdc": parsed.bid_depth_usdc,
                "ask_depth_usdc": parsed.ask_depth_usdc,
                "spread": parsed.spread,
                "time_ms": parsed.time_ms,
            }
        return books

    def _is_short_expiry_candidate(self, *, market: OutcomeMarket, now_ts: int) -> bool:
        if market.class_name != "priceBinary":
            return False
        time_left = market.expiry_ts - now_ts
        if time_left <= self.config.min_time_to_expiry_seconds:
            return False
        if time_left > self.config.short_expiry_window_seconds:
            return False
        allowed_periods = {period.strip().lower() for period in self.config.short_expiry_periods if period.strip()}
        return not allowed_periods or market.period.strip().lower() in allowed_periods

    def _append_short_expiry_watchlist(
        self,
        *,
        summary: dict[str, Any],
        row: dict[str, Any],
    ) -> None:
        watchlist = summary.get("short_expiry_watchlist")
        if not isinstance(watchlist, list):
            watchlist = []
            summary["short_expiry_watchlist"] = watchlist
        watchlist.append(row)
        if row.get("readiness") == "ready":
            summary["short_expiry_ready_count"] = int(summary.get("short_expiry_ready_count", 0) or 0) + 1
        seconds_left = _int_from_any(row.get("seconds_left"), 0)
        current_next = summary.get("next_short_expiry_seconds")
        if current_next is None or seconds_left < _int_from_any(current_next, seconds_left):
            summary["next_short_expiry_seconds"] = seconds_left

    def _finalize_operator_summary(
        self,
        *,
        summary: dict[str, Any],
        decision_reasons: Counter[str],
        opportunity_mix: Counter[str],
    ) -> None:
        watchlist = summary.get("short_expiry_watchlist")
        if not isinstance(watchlist, list):
            watchlist = []
        limit = max(int(self.config.short_expiry_watchlist_limit), 1)
        watchlist = sorted(
            (row for row in watchlist if isinstance(row, dict)),
            key=lambda row: (
                0 if row.get("readiness") == "ready" else 1,
                _int_from_any(row.get("seconds_left"), 10**9),
                -_float_from_any(row.get("best_net_edge"), -1.0),
            ),
        )[:limit]
        summary["short_expiry_watchlist"] = watchlist
        summary["short_expiry_ready_count"] = sum(1 for row in watchlist if row.get("readiness") == "ready")
        summary["decision_reasons"] = dict(sorted(decision_reasons.items()))
        summary["opportunity_mix"] = dict(sorted(opportunity_mix.items()))
        summary["operator_brief"] = _build_short_expiry_operator_brief(
            summary,
            config=self.config,
            last_error=self.last_error,
        )

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

    def _update_shock_guard_history(
        self,
        *,
        reference_prices: dict[str, Any],
        now_ts: int,
    ) -> dict[str, list[dict[str, float]]]:
        if not self.config.enable_shock_guard:
            return {}
        payload = self.state_store.load()
        payload["positions"] = [position.to_dict() for position in self.positions]
        raw_history = payload.get("shock_guard_price_history")
        if not isinstance(raw_history, dict) or not raw_history:
            seeded = _seed_price_history_from_opportunities(
                Path(self.config.logs_dir) / "opportunities.csv",
                now_ts=now_ts,
                max_age_seconds=self.config.shock_guard_history_seconds,
                sample_interval_seconds=self.config.shock_guard_sample_interval_seconds,
                sample_limit=self.config.shock_guard_price_history_limit,
            )
            if seeded:
                payload["shock_guard_price_history"] = seeded
        prices = {
            underlying.upper(): reference.price
            for underlying, reference in reference_prices.items()
            if getattr(reference, "price", 0.0) > 0
        }
        history = update_price_history_payload(
            payload,
            prices,
            now_ts=now_ts,
            max_age_seconds=self.config.shock_guard_history_seconds,
            sample_limit=self.config.shock_guard_price_history_limit,
            payload_key="shock_guard_price_history",
            min_sample_interval_seconds=self.config.shock_guard_sample_interval_seconds,
        )
        self.state_store.save(payload)
        return history

    def _shock_guard_assessment(
        self,
        *,
        market: OutcomeMarket,
        reference_price: float,
        now_ts: int,
        history: dict[str, list[dict[str, float]]],
    ) -> dict[str, Any]:
        if not self.config.enable_shock_guard or reference_price <= 0:
            return {}
        samples = _parse_price_samples(history.get(market.underlying.upper(), []))
        if not samples:
            return {}
        windows: list[dict[str, Any]] = []
        thresholds = _shock_thresholds_by_window(self.config)
        for window_seconds, threshold_bps in thresholds:
            cutoff = now_ts - int(window_seconds)
            candidates = [sample for sample in samples if int(sample["ts"]) <= cutoff]
            if not candidates:
                continue
            base = candidates[-1]
            base_price = float(base["price"])
            if base_price <= 0:
                continue
            move_bps = round((reference_price / base_price - 1.0) * 10_000.0, 6)
            windows.append(
                {
                    "window_seconds": int(window_seconds),
                    "threshold_bps": float(threshold_bps),
                    "move_bps": move_bps,
                    "base_price": round(base_price, 8),
                    "current_price": round(reference_price, 8),
                    "base_age_seconds": int(max(now_ts - int(base["ts"]), 0)),
                }
            )
        if not windows:
            return {}
        return {
            "underlying": market.underlying.upper(),
            "windows": windows,
        }

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
        if market.class_name != "priceBinary":
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

    def _named_outcome_basket_opportunities(
        self,
        *,
        payload: object,
        anchor_markets: list[OutcomeMarket],
        now_ts: int,
    ) -> list[tuple[OutcomeMarket, OutcomeOrderBook, OutcomeOpportunity]]:
        self._last_named_outcome_basket_watchlist = []
        if not self.config.enable_named_outcome_basket:
            return []
        if self.config.mode == "testnet":
            return []
        anchors_by_outcome = {
            market.outcome: market
            for market in anchor_markets
            if market.class_name == "priceBinary"
            and market.expiry_ts > now_ts + self.config.min_time_to_expiry_seconds
        }
        if not anchors_by_outcome:
            return []

        opportunities: list[tuple[OutcomeMarket, OutcomeOrderBook, OutcomeOpportunity]] = []
        current_anchor: OutcomeMarket | None = None
        current_group: list[Any] = []

        def flush_group() -> None:
            nonlocal current_group
            if current_anchor is not None and current_group:
                opportunities.extend(
                    self._build_named_no_basket_opportunity(
                        anchor_market=current_anchor,
                        observations=current_group,
                        now_ts=now_ts,
                    )
                )
            current_group = []

        for observation in parse_outcome_observations(payload):
            if observation.outcome in anchors_by_outcome:
                flush_group()
                current_anchor = anchors_by_outcome[observation.outcome]
                continue
            if observation.class_name == "namedOutcome":
                if current_anchor is not None:
                    current_group.append(observation)
                continue
            if observation.class_name == "fallback":
                continue
            if observation.support_status in {"trading_supported", "paper_supported"}:
                flush_group()
                current_anchor = None
        flush_group()
        return opportunities

    def _build_named_no_basket_opportunity(
        self,
        *,
        anchor_market: OutcomeMarket,
        observations: list[Any],
        now_ts: int,
    ) -> list[tuple[OutcomeMarket, OutcomeOrderBook, OutcomeOpportunity]]:
        min_count = max(int(self.config.named_outcome_basket_min_count), 2)
        unique_by_outcome = {
            int(observation.outcome): observation
            for observation in observations
            if getattr(observation, "outcome", None) is not None
        }
        ordered = sorted(
            unique_by_outcome.values(),
            key=lambda item: (
                10**9 if item.bucket_index is None else int(item.bucket_index),
                int(item.outcome or 0),
            ),
        )
        if len(ordered) < min_count:
            return []

        legs: list[dict[str, object]] = []
        for observation in ordered:
            leg = self._named_no_basket_leg(observation)
            if leg is not None:
                legs.append(leg)
        if len(legs) < min_count:
            return []

        unit_cost = round(sum(float(leg["ask"]) for leg in legs), 8)
        conservative_payout = float(len(legs) - 1)
        gross_edge = round(conservative_payout - unit_cost, 8)
        estimated_fees = round(
            conservative_payout * max(float(self.config.outcome_settlement_fee_rate), 0.0),
            8,
        )
        estimated_slippage = round(float(self.config.estimated_slippage) * len(legs), 8)
        net_edge = round(
            gross_edge - estimated_fees - estimated_slippage - float(self.config.safety_margin),
            8,
        )
        watch_row = {
            "anchor_market_id": anchor_market.market_id,
            "underlying": anchor_market.underlying,
            "expiry_ts": anchor_market.expiry_ts,
            "expiry_iso": anchor_market.expiry_iso,
            "leg_count": len(legs),
            "unit_cost": unit_cost,
            "conservative_payout": conservative_payout,
            "gross_edge": gross_edge,
            "net_edge": net_edge,
            "named_outcomes": [leg["outcome"] for leg in legs],
            "readiness": "ready",
        }
        if gross_edge < self.config.min_gross_edge or net_edge < self.config.min_net_edge:
            watch_row["readiness"] = "below_edge_threshold"
            self._append_named_basket_watch_row(watch_row)
            return []

        max_qty_by_depth = min(
            float(leg["ask_depth_usdc"]) / float(leg["ask"])
            for leg in legs
            if float(leg["ask"]) > 0
        )
        requested_size = round(
            min(float(self.config.max_position_usdc), max_qty_by_depth * unit_cost),
            6,
        )
        if requested_size <= 0:
            watch_row["readiness"] = "no_available_depth_or_size"
            self._append_named_basket_watch_row(watch_row)
            return []
        watch_row["requested_size_usdc"] = requested_size
        self._append_named_basket_watch_row(watch_row)

        leg_ids = "-".join(str(leg["outcome"]) for leg in legs)
        market_id = f"{anchor_market.market_id}:NAMED_NO_BASKET:{leg_ids}"
        basket_market = OutcomeMarket(
            market_id=market_id,
            outcome=anchor_market.outcome,
            name="Named outcome NO basket",
            description=f"anchor:{anchor_market.market_id}|legs:{leg_ids}",
            underlying=anchor_market.underlying,
            strike=anchor_market.strike,
            expiry_ts=anchor_market.expiry_ts,
            period=anchor_market.period,
            class_name="namedOutcomeBasket",
            settlement_source="named_outcome_basket_conservative",
            side_names=("Named YES", "Named NO basket"),
            raw={
                "anchor": dict(anchor_market.raw),
                "legs": [dict(leg) for leg in legs],
            },
        )
        order_book = OutcomeOrderBook(
            market_id=market_id,
            yes=OutcomeSideBook(coin="", bid=None, ask=None),
            no=OutcomeSideBook(coin="", bid=None, ask=None),
        )
        opportunity = OutcomeOpportunity(
            market_id=market_id,
            outcome=anchor_market.outcome,
            underlying=anchor_market.underlying,
            side="BUY_NAMED_NO_BASKET",
            edge_type="NAMED_OUTCOME_NO_BASKET",
            gross_edge=gross_edge,
            estimated_fees=estimated_fees,
            estimated_slippage=estimated_slippage,
            net_edge=net_edge,
            confidence=0.9,
            requested_size_usdc=requested_size,
            max_loss_usdc=requested_size,
            expiry_ts=anchor_market.expiry_ts,
            reason=(
                "NamedOutcome NO basket ask below conservative payout: "
                f"{unit_cost:.6f} < {conservative_payout:.6f}"
            ),
            metadata={
                "basket_legs": legs,
                "basket_leg_count": len(legs),
                "basket_unit_cost": unit_cost,
                "basket_conservative_payout": conservative_payout,
                "basket_group_anchor_market_id": anchor_market.market_id,
                "basket_group_anchor_outcome": anchor_market.outcome,
                "basket_named_outcomes": [leg["outcome"] for leg in legs],
                "settlement_policy": "conservative_named_outcome_no_basket",
                "assumption": "at_most_one_named_outcome_true",
                "strike": anchor_market.strike,
                "time_to_expiry_seconds": anchor_market.expiry_ts - now_ts,
            },
        )
        return [(basket_market, order_book, opportunity)]

    def _named_no_basket_leg(self, observation: Any) -> dict[str, object] | None:
        try:
            outcome = int(observation.outcome)
        except (TypeError, ValueError):
            return None
        coins = observation.coins if isinstance(observation.coins, tuple) else ()
        coin = str(coins[1]) if len(coins) >= 2 else outcome_coin(outcome, 1)
        book = parse_side_book(
            self.info_client.fetch_l2_book(coin),
            max_slippage=self.config.max_order_slippage,
        )
        if book.ask is None or book.ask <= 0:
            return None
        return {
            "outcome": outcome,
            "index": observation.bucket_index,
            "name": observation.name,
            "description": observation.description,
            "coin": coin,
            "side_name": "NO",
            "bid": book.bid,
            "ask": book.ask,
            "ask_size": book.ask_size,
            "ask_depth_usdc": book.ask_depth_usdc,
            "spread": book.spread,
            "time_ms": book.time_ms,
        }

    def _append_named_basket_watch_row(self, row: dict[str, object]) -> None:
        self._last_named_outcome_basket_watchlist.append(dict(row))
        self._last_named_outcome_basket_watchlist = sorted(
            self._last_named_outcome_basket_watchlist,
            key=lambda item: float(item.get("net_edge", -10**9)),
            reverse=True,
        )[:10]

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

    def _manage_shadow_exit_policies_for_market(
        self,
        *,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        probability: Any,
        short_assessment: ShortExpiryAssessment | None,
        reference_price: float,
        now_ts: int,
        summary: dict[str, Any],
    ) -> None:
        if not self.config.enable_shadow_exit_policies or self.config.mode != "paper":
            return
        policies = _shadow_exit_policy_specs(self.config)
        if not policies:
            return
        changed = False
        for position in list(self._positions_for_mode()):
            if position.market_id != market.market_id:
                continue
            if position.status not in {"open", "early_exited"}:
                continue
            if now_ts >= position.expiry_ts + self.config.settlement_grace_seconds:
                continue
            for policy in policies:
                row = self._shadow_exit_policy_row(
                    position=position,
                    market=market,
                    order_book=order_book,
                    probability=probability,
                    short_assessment=short_assessment,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    policy=policy,
                )
                if row is None:
                    continue
                summary["shadow_exit_policy_evaluations"] = (
                    int(summary.get("shadow_exit_policy_evaluations", 0)) + 1
                )
                if row["action"] == "hold":
                    continue
                self._apply_shadow_exit_policy(position=position, row=row)
                self.event_logger.log_shadow_exit_policy(row)
                summary["shadow_exit_policy_exits"] = (
                    int(summary.get("shadow_exit_policy_exits", 0)) + 1
                )
                changed = True
        if changed:
            self.state_store.save_positions(self.positions)

    def _shadow_exit_policy_row(
        self,
        *,
        position: OutcomePosition,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        probability: Any,
        short_assessment: ShortExpiryAssessment | None,
        reference_price: float,
        now_ts: int,
        policy: dict[str, Any],
    ) -> dict[str, Any] | None:
        policy_name = str(policy.get("policy", "")).strip()
        if not policy_name:
            return None
        policy_state = _shadow_exit_policy_state(position, policy_name)
        if str(policy_state.get("status", "open")) in {"closed", "settled"}:
            return None
        side_name = _single_position_side_name(position)
        if side_name is None:
            return None
        side_book = order_book.yes if side_name == "YES" else order_book.no
        bid = side_book.bid
        ask = side_book.ask
        remaining_qty = _shadow_remaining_token_qty(position, side_name, policy_name)
        if remaining_qty <= 0:
            return None
        seconds_left = max(market.expiry_ts - now_ts, 0)
        win_probability = _position_win_probability(
            side_name=side_name,
            probability_yes=float(getattr(probability, "probability_yes", 0.5)),
            short_assessment=short_assessment,
            seconds_left=seconds_left,
            short_window_seconds=self.config.short_expiry_window_seconds,
        )
        conservative_probability = max(
            min(win_probability - max(float(self.config.early_exit_probability_haircut), 0.0), 1.0),
            0.0,
        )
        fee_rate = max(float(self.config.outcome_settlement_fee_rate), 0.0)
        hold_ev_usdc = round(float(remaining_qty) * conservative_probability * (1.0 - fee_rate), 8)
        action = "hold"
        reason = "shadow_hold"
        exit_fraction = 0.0
        if bid is None or bid <= 0:
            reason = "missing_exit_bid"
        else:
            gross_full = float(remaining_qty) * float(bid)
            fee_full = gross_full * fee_rate
            net_full = gross_full - fee_full
            full_cost_basis = _cost_basis_for_exit(position, side_name, remaining_qty)
            full_exit_roi = (
                (net_full - full_cost_basis) / full_cost_basis
                if full_cost_basis > 0
                else 0.0
            )
            kind = str(policy.get("kind", "hold"))
            if kind == "take_profit_partial":
                threshold = float(policy.get("roi", 1.0))
                if _shadow_exit_entries(position, policy_name):
                    reason = "shadow_policy_already_exited"
                elif full_exit_roi >= threshold:
                    action = "partial_exit"
                    reason = f"shadow_take_profit_{int(round(threshold * 100))}"
                    exit_fraction = max(
                        min(float(self.config.shadow_exit_partial_fraction), 1.0),
                        0.0,
                    )
            elif kind == "ev_full":
                if (
                    hold_ev_usdc > 0
                    and net_full >= hold_ev_usdc * (1.0 + float(self.config.early_exit_min_ev_premium))
                    and full_exit_roi >= float(self.config.early_exit_min_ev_exit_roi)
                ):
                    action = "full_exit"
                    reason = "shadow_bid_over_conservative_hold_ev"
                    exit_fraction = 1.0
            elif kind == "last_window_full":
                window = int(policy.get("window_seconds", 0))
                if (
                    seconds_left <= window
                    and full_exit_roi >= float(self.config.early_exit_free_short_window_min_roi)
                ):
                    action = "full_exit"
                    reason = f"shadow_last_{window}s_window"
                    exit_fraction = 1.0
            elif kind == "prob_stop_full":
                if (
                    conservative_probability <= float(self.config.early_exit_stop_probability)
                    and full_exit_roi >= -float(self.config.early_exit_stop_max_loss_roi)
                ):
                    action = "full_exit"
                    reason = "shadow_probability_stop"
                    exit_fraction = 1.0
            else:
                reason = "shadow_hold_to_settlement"

        exit_qty = Decimal("0")
        if action != "hold" and bid is not None and bid > 0:
            exit_qty = _quantized_exit_qty(
                remaining_qty,
                fraction=exit_fraction,
                size_decimals=self.config.outcome_size_decimals,
            )
            if exit_qty <= 0:
                action = "hold"
                reason = "shadow_exit_size_zero_after_rounding"
            elif _effective_exit_order_value(exit_qty, float(bid)) < float(self.config.min_order_value_usdc):
                action = "hold"
                reason = "shadow_exit_below_exchange_min_order_value"
                exit_qty = Decimal("0")

        gross_exit_usdc = round(float(exit_qty) * float(bid or 0.0), 8)
        fee_usdc = round(gross_exit_usdc * fee_rate, 8)
        net_exit_usdc = round(gross_exit_usdc - fee_usdc, 8)
        cost_basis_usdc = round(_cost_basis_for_exit(position, side_name, exit_qty), 8)
        realized_pnl_usdc = round(net_exit_usdc - cost_basis_usdc, 8)
        exit_roi = (
            round(realized_pnl_usdc / cost_basis_usdc, 8)
            if cost_basis_usdc > 0
            else 0.0
        )
        return {
            "ts": utc_now_iso(),
            "event_type": "exit",
            "policy": policy_name,
            "market_id": market.market_id,
            "outcome": market.outcome,
            "underlying": market.underlying,
            "side": position.side,
            "action": action,
            "reason": reason,
            "position_status": position.status,
            "result": "",
            "remaining_qty_before": str(remaining_qty),
            "exit_fraction": round(exit_fraction, 8),
            "token_qty": str(exit_qty),
            "exit_price": bid,
            "gross_exit_usdc": gross_exit_usdc,
            "fee_usdc": fee_usdc,
            "net_exit_usdc": net_exit_usdc,
            "settlement_payout_usdc": 0.0,
            "total_payout_usdc": net_exit_usdc,
            "cost_basis_usdc": cost_basis_usdc,
            "gross_pnl_usdc": round(gross_exit_usdc - cost_basis_usdc, 8),
            "realized_pnl_usdc": realized_pnl_usdc,
            "net_pnl_usdc": realized_pnl_usdc,
            "exit_roi": exit_roi,
            "hold_ev_usdc": hold_ev_usdc,
            "win_probability": round(win_probability, 8),
            "conservative_win_probability": round(conservative_probability, 8),
            "bid": bid,
            "ask": ask,
            "reference_price": reference_price,
            "strike": market.strike,
            "seconds_left": seconds_left,
            "_side_name": side_name,
        }

    def _apply_shadow_exit_policy(self, *, position: OutcomePosition, row: dict[str, Any]) -> None:
        policy_name = str(row["policy"])
        state = _ensure_shadow_exit_policy_state(position, policy_name)
        exits = state.get("exits")
        if not isinstance(exits, list):
            exits = []
        exits.append(
            {
                "ts": row["ts"],
                "action": row["action"],
                "reason": row["reason"],
                "side_name": str(row.get("_side_name", "")),
                "token_qty": row["token_qty"],
                "exit_price": row["exit_price"],
                "gross_exit_usdc": row["gross_exit_usdc"],
                "fee_usdc": row["fee_usdc"],
                "net_exit_usdc": row["net_exit_usdc"],
                "cost_basis_usdc": row["cost_basis_usdc"],
                "realized_pnl_usdc": row["realized_pnl_usdc"],
                "exit_roi": row["exit_roi"],
            }
        )
        state["exits"] = exits
        state["status"] = "closed" if row["action"] == "full_exit" else "open"
        state["last_event_at"] = row["ts"]

    def _manage_early_exits_for_market(
        self,
        *,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        probability: Any,
        short_assessment: ShortExpiryAssessment | None,
        reference_price: float,
        now_ts: int,
        summary: dict[str, Any],
    ) -> bool:
        if not self.config.enable_early_exit or self.config.mode != "paper":
            return False
        changed = False
        full_exited = False
        for position in list(self._active_positions_for_mode()):
            if position.market_id != market.market_id:
                continue
            row = self._early_exit_row(
                position=position,
                market=market,
                order_book=order_book,
                probability=probability,
                short_assessment=short_assessment,
                reference_price=reference_price,
                now_ts=now_ts,
            )
            if row is None:
                continue
            summary["early_exit_evaluations"] = int(summary.get("early_exit_evaluations", 0)) + 1
            if row["action"] != "hold":
                self._apply_paper_early_exit(position=position, row=row)
                summary["early_exits"] = int(summary.get("early_exits", 0)) + 1
                summary["early_exit_net_usdc"] = round(
                    float(summary.get("early_exit_net_usdc", 0.0)) + float(row["net_exit_usdc"]),
                    8,
                )
                changed = True
                full_exited = full_exited or row["action"] == "full_exit"
            self.event_logger.log_early_exit(row)
        if changed:
            self.state_store.save_positions(self.positions)
            self._write_settlement_summary()
        return full_exited

    def _early_exit_row(
        self,
        *,
        position: OutcomePosition,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        probability: Any,
        short_assessment: ShortExpiryAssessment | None,
        reference_price: float,
        now_ts: int,
    ) -> dict[str, Any] | None:
        side_name = _single_position_side_name(position)
        if side_name is None:
            return None
        side_book = order_book.yes if side_name == "YES" else order_book.no
        bid = side_book.bid
        ask = side_book.ask
        remaining_qty = _remaining_token_qty(position, side_name)
        if remaining_qty <= 0:
            return None
        win_probability = _position_win_probability(
            side_name=side_name,
            probability_yes=float(getattr(probability, "probability_yes", 0.5)),
            short_assessment=short_assessment,
            seconds_left=max(market.expiry_ts - now_ts, 0),
            short_window_seconds=self.config.short_expiry_window_seconds,
        )
        conservative_probability = max(
            min(win_probability - max(float(self.config.early_exit_probability_haircut), 0.0), 1.0),
            0.0,
        )
        fee_rate = max(float(self.config.outcome_settlement_fee_rate), 0.0)
        hold_ev_usdc = round(float(remaining_qty) * conservative_probability * (1.0 - fee_rate), 8)
        action = "hold"
        reason = "early_exit_hold"
        exit_fraction = 0.0
        if bid is None or bid <= 0:
            reason = "missing_exit_bid"
        else:
            gross_full = float(remaining_qty) * float(bid)
            fee_full = gross_full * fee_rate
            net_full = gross_full - fee_full
            full_cost_basis = _cost_basis_for_exit(position, side_name, remaining_qty)
            full_exit_roi = (
                (net_full - full_cost_basis) / full_cost_basis
                if full_cost_basis > 0
                else 0.0
            )
            if full_exit_roi >= float(self.config.early_exit_full_take_profit_roi):
                action = "full_exit"
                reason = "full_take_profit"
                exit_fraction = 1.0
            elif (
                hold_ev_usdc > 0
                and net_full >= hold_ev_usdc * (1.0 + float(self.config.early_exit_min_ev_premium))
                and full_exit_roi >= float(self.config.early_exit_min_ev_exit_roi)
            ):
                action = "full_exit"
                reason = "bid_over_conservative_hold_ev"
                exit_fraction = 1.0
            elif (
                self.config.enable_early_exit_probability_stop
                and
                conservative_probability <= float(self.config.early_exit_stop_probability)
                and full_exit_roi >= -float(self.config.early_exit_stop_max_loss_roi)
            ):
                action = "full_exit"
                reason = "probability_stop"
                exit_fraction = 1.0
            elif (
                market.expiry_ts - now_ts <= int(self.config.early_exit_free_short_window_seconds)
                and full_exit_roi >= float(self.config.early_exit_free_short_window_min_roi)
            ):
                action = "full_exit"
                reason = "free_short_expiry_window"
                exit_fraction = 1.0
            elif (
                full_exit_roi >= float(self.config.early_exit_take_profit_roi)
                and not _has_partial_take_profit_exit(position)
            ):
                action = "partial_exit"
                reason = "partial_take_profit"
                exit_fraction = max(min(float(self.config.early_exit_take_profit_fraction), 1.0), 0.0)
            else:
                exit_fraction = 0.0

        exit_qty = Decimal("0")
        if action != "hold" and bid is not None and bid > 0:
            exit_qty = _quantized_exit_qty(
                remaining_qty,
                fraction=exit_fraction,
                size_decimals=self.config.outcome_size_decimals,
            )
            if exit_qty <= 0:
                action = "hold"
                reason = "exit_size_zero_after_rounding"
            elif _effective_exit_order_value(exit_qty, float(bid)) < float(self.config.min_order_value_usdc):
                action = "hold"
                reason = "exit_below_exchange_min_order_value"
                exit_qty = Decimal("0")

        gross_exit_usdc = round(float(exit_qty) * float(bid or 0.0), 8)
        fee_usdc = round(gross_exit_usdc * fee_rate, 8)
        net_exit_usdc = round(gross_exit_usdc - fee_usdc, 8)
        cost_basis_usdc = round(_cost_basis_for_exit(position, side_name, exit_qty), 8)
        realized_pnl_usdc = round(net_exit_usdc - cost_basis_usdc, 8)
        exit_roi = round(
            realized_pnl_usdc / cost_basis_usdc,
            8,
        ) if cost_basis_usdc > 0 else 0.0
        return {
            "ts": utc_now_iso(),
            "market_id": market.market_id,
            "outcome": market.outcome,
            "underlying": market.underlying,
            "side": position.side,
            "action": action,
            "reason": reason,
            "position_status_before": position.status,
            "exit_fraction": round(exit_fraction, 8),
            "token_qty": str(exit_qty),
            "exit_price": bid,
            "gross_exit_usdc": gross_exit_usdc,
            "fee_usdc": fee_usdc,
            "net_exit_usdc": net_exit_usdc,
            "cost_basis_usdc": cost_basis_usdc,
            "realized_pnl_usdc": realized_pnl_usdc,
            "exit_roi": exit_roi,
            "hold_ev_usdc": hold_ev_usdc,
            "win_probability": round(win_probability, 8),
            "conservative_win_probability": round(conservative_probability, 8),
            "bid": bid,
            "ask": ask,
            "reference_price": reference_price,
            "strike": market.strike,
            "seconds_left": max(market.expiry_ts - now_ts, 0),
            "_side_name": side_name,
        }

    def _apply_paper_early_exit(self, *, position: OutcomePosition, row: dict[str, Any]) -> None:
        side_name = str(row.pop("_side_name", ""))
        entry = {
            "ts": row["ts"],
            "action": row["action"],
            "reason": row["reason"],
            "side_name": side_name,
            "token_qty": row["token_qty"],
            "exit_price": row["exit_price"],
            "gross_exit_usdc": row["gross_exit_usdc"],
            "fee_usdc": row["fee_usdc"],
            "net_exit_usdc": row["net_exit_usdc"],
            "cost_basis_usdc": row["cost_basis_usdc"],
            "realized_pnl_usdc": row["realized_pnl_usdc"],
            "exit_roi": row["exit_roi"],
        }
        exits = position.metadata.get("early_exits")
        if not isinstance(exits, list):
            exits = []
        exits.append(entry)
        position.metadata["early_exits"] = exits
        if row["action"] != "full_exit":
            return
        position.status = "early_exited"
        position.settled_at = str(row["ts"])
        position.estimated_payout_usdc = 0.0
        position.metadata["settlement"] = {
            "result": "EARLY_EXIT",
            "source": "paper_early_exit_bid",
            "settlement_payout_usdc": 0.0,
            "fee_model": self._fee_model_payload(),
            "notes": str(row["reason"]),
        }
        _apply_settlement_accounting(position, self.config)

    def _recent_early_exit_for_market(self, market_id: str, *, now_ts: int) -> bool:
        cooldown = max(int(self.config.early_exit_reentry_cooldown_seconds), 0)
        if cooldown <= 0:
            return False
        for position in self._positions_for_mode():
            if position.market_id != market_id or position.status != "early_exited":
                continue
            exited_ms = _iso_to_epoch_ms(str(position.settled_at or ""))
            if exited_ms is None:
                continue
            if now_ts - int(exited_ms / 1000) <= cooldown:
                return True
        return False

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

    def _settle_expired_positions(
        self,
        *,
        now_ts: int,
        reference_prices: dict[str, Any],
        summary: dict[str, Any] | None = None,
    ) -> None:
        changed = self._settle_expired_shadow_exit_policies(
            now_ts=now_ts,
            reference_prices=reference_prices,
            summary=summary,
        )
        if self.config.mode == "testnet":
            if changed:
                self.state_store.save_positions(self.positions)
            return
        for position in self.positions:
            if position.status != "open":
                continue
            if now_ts < position.expiry_ts + self.config.settlement_grace_seconds:
                continue
            if position.edge_type == "NAMED_OUTCOME_NO_BASKET":
                changed = self._settle_named_no_basket_position(position) or changed
                continue
            reference = reference_prices.get(position.underlying.upper())
            reference_price = None if reference is None else reference.price
            strike = _position_strike(position)
            bucket = _position_price_bucket(position)
            if reference_price is None or (strike is None and bucket is None):
                continue
            if bucket is not None:
                lower, upper = bucket
                result_yes = lower <= reference_price <= upper
            else:
                result_yes = bool(strike is not None and reference_price > strike)
            remaining_qty = _remaining_token_qty_by_side(position)
            payout = 0.0
            if result_yes:
                payout += float(remaining_qty.get("YES", Decimal("0")))
            else:
                payout += float(remaining_qty.get("NO", Decimal("0")))
            position.status = "estimated_settled"
            position.settled_at = utc_now_iso()
            position.estimated_payout_usdc = round(payout, 8)
            settlement_payload = {
                "result": "YES" if result_yes else "NO",
                "reference_price": reference_price,
                "fee_model": self._fee_model_payload(),
                "notes": "estimated_from_reference_price",
                "settlement_payout_usdc": round(payout, 8),
            }
            if bucket is not None:
                settlement_payload["bucket_lower"] = bucket[0]
                settlement_payload["bucket_upper"] = bucket[1]
            else:
                settlement_payload["strike"] = strike
            position.metadata["settlement"] = settlement_payload
            _apply_settlement_accounting(position, self.config)
            self.event_logger.log_settlement(_settlement_row_from_position(position))
            changed = True
        if changed:
            self.state_store.save_positions(self.positions)
            self._write_settlement_summary()

    def _settle_expired_shadow_exit_policies(
        self,
        *,
        now_ts: int,
        reference_prices: dict[str, Any],
        summary: dict[str, Any] | None,
    ) -> bool:
        if not self.config.enable_shadow_exit_policies or self.config.mode != "paper":
            return False
        policies = _shadow_exit_policy_specs(self.config)
        if not policies:
            return False
        changed = False
        for position in self._positions_for_mode():
            if now_ts < position.expiry_ts + self.config.settlement_grace_seconds:
                continue
            side_name = _single_position_side_name(position)
            if side_name is None:
                continue
            if position.edge_type == "NAMED_OUTCOME_NO_BASKET":
                continue
            reference = reference_prices.get(position.underlying.upper())
            reference_price = None if reference is None else reference.price
            strike = _position_strike(position)
            bucket = _position_price_bucket(position)
            if reference_price is None or (strike is None and bucket is None):
                continue
            if bucket is not None:
                result_yes = bucket[0] <= reference_price <= bucket[1]
            else:
                result_yes = bool(strike is not None and reference_price > strike)
            result = "YES" if result_yes else "NO"
            for policy in policies:
                policy_name = str(policy.get("policy", "")).strip()
                if not policy_name:
                    continue
                state = _ensure_shadow_exit_policy_state(position, policy_name)
                if str(state.get("status", "open")) == "settled":
                    continue
                remaining_qty = _shadow_remaining_token_qty(position, side_name, policy_name)
                settlement_payout = (
                    float(remaining_qty)
                    if (result_yes and side_name == "YES") or (not result_yes and side_name == "NO")
                    else 0.0
                )
                early_gross, early_fee = _shadow_exit_cash_totals(position, policy_name)
                settlement_fee = settlement_payout * max(float(self.config.outcome_settlement_fee_rate), 0.0)
                total_fee = round(early_fee + settlement_fee, 8)
                total_payout = round(early_gross + settlement_payout, 8)
                gross_pnl = round(total_payout - float(position.cost_usdc or 0.0), 8)
                net_pnl = round(gross_pnl - total_fee, 8)
                row = {
                    "ts": utc_now_iso(),
                    "event_type": "settlement",
                    "policy": policy_name,
                    "market_id": position.market_id,
                    "outcome": position.outcome,
                    "underlying": position.underlying,
                    "side": position.side,
                    "action": "settlement",
                    "reason": "shadow_policy_settlement",
                    "position_status": position.status,
                    "result": result,
                    "remaining_qty_before": str(remaining_qty),
                    "exit_fraction": 0.0,
                    "token_qty": "0",
                    "exit_price": "",
                    "gross_exit_usdc": early_gross,
                    "fee_usdc": total_fee,
                    "net_exit_usdc": round(early_gross - early_fee, 8),
                    "settlement_payout_usdc": round(settlement_payout, 8),
                    "total_payout_usdc": total_payout,
                    "cost_basis_usdc": position.cost_usdc,
                    "gross_pnl_usdc": gross_pnl,
                    "realized_pnl_usdc": net_pnl,
                    "net_pnl_usdc": net_pnl,
                    "exit_roi": round(net_pnl / float(position.cost_usdc), 8)
                    if float(position.cost_usdc or 0.0) > 0
                    else 0.0,
                    "hold_ev_usdc": "",
                    "win_probability": "",
                    "conservative_win_probability": "",
                    "bid": "",
                    "ask": "",
                    "reference_price": reference_price,
                    "strike": strike,
                    "seconds_left": max(position.expiry_ts - now_ts, 0),
                }
                state["status"] = "settled"
                state["settled_at"] = row["ts"]
                state["settlement"] = {
                    "result": result,
                    "reference_price": reference_price,
                    "settlement_payout_usdc": round(settlement_payout, 8),
                    "total_payout_usdc": total_payout,
                    "fee_usdc": total_fee,
                    "gross_pnl_usdc": gross_pnl,
                    "net_pnl_usdc": net_pnl,
                }
                self.event_logger.log_shadow_exit_policy(row)
                if summary is not None:
                    summary["shadow_exit_policy_settlements"] = (
                        int(summary.get("shadow_exit_policy_settlements", 0)) + 1
                    )
                changed = True
        return changed

    def _settle_named_no_basket_position(self, position: OutcomePosition) -> bool:
        no_quantities = [
            float(fill.token_qty)
            for fill in position.fills
            if fill.side_name.upper() == "NO" and fill.token_qty > 0
        ]
        if not no_quantities:
            return False
        payout = max(sum(no_quantities) - max(no_quantities), 0.0)
        position.status = "estimated_settled"
        position.settled_at = utc_now_iso()
        position.estimated_payout_usdc = round(payout, 8)
        position.metadata["settlement"] = {
            "result": "CONSERVATIVE_NAMED_NO_BASKET",
            "source": "conservative_named_outcome_basket",
            "fee_model": self._fee_model_payload(),
            "notes": "conservative_named_outcome_no_basket",
            "leg_count": len(no_quantities),
            "assumption": "at_most_one_named_outcome_true",
        }
        _apply_settlement_accounting(position, self.config)
        self.event_logger.log_settlement(_settlement_row_from_position(position))
        return True

    def _log_shadow_sizing(
        self,
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        decision: SupervisorDecision,
        reference_price: float,
        now_ts: int,
        summary: dict[str, Any],
    ) -> None:
        if not self.config.enable_shadow_sizing or self.config.mode != "paper":
            return
        entry_price = _opportunity_entry_price(opportunity, order_book)
        if entry_price is None or entry_price <= 0 or entry_price >= 1.0:
            return
        win_probability = _opportunity_win_probability(opportunity)
        if win_probability is None:
            return
        conservative_probability = max(
            min(win_probability - max(float(self.config.shadow_sizing_probability_haircut), 0.0), 1.0),
            0.0,
        )
        kelly_fraction = _binary_kelly_fraction(conservative_probability, entry_price)
        capped_fraction = min(
            max(kelly_fraction, 0.0),
            max(float(self.config.shadow_sizing_kelly_fraction_cap), 0.0),
        )
        bankroll = max(float(self.config.shadow_sizing_bankroll_usdc), 0.0)
        max_position = max(float(self.config.max_position_usdc), 0.0)
        kelly_size = bankroll * max(kelly_fraction, 0.0)
        half_kelly_size = bankroll * max(kelly_fraction, 0.0) * 0.5
        capped_kelly_size = min(bankroll * capped_fraction, max_position)
        self.event_logger.log_shadow_sizing(
            {
                "ts": utc_now_iso(),
                "market_id": opportunity.market_id,
                "outcome": opportunity.outcome,
                "underlying": opportunity.underlying,
                "edge_type": opportunity.edge_type,
                "side": opportunity.side,
                "decision_approved": decision.approved,
                "decision_reason": decision.reason,
                "active_requested_size_usdc": opportunity.requested_size_usdc,
                "active_approved_size_usdc": decision.approved_size_usdc,
                "win_probability": round(win_probability, 8),
                "conservative_win_probability": round(conservative_probability, 8),
                "entry_price": round(entry_price, 8),
                "net_edge": opportunity.net_edge,
                "confidence": opportunity.confidence,
                "bankroll_usdc": bankroll,
                "kelly_fraction": round(kelly_fraction, 8),
                "capped_kelly_fraction": round(capped_fraction, 8),
                "kelly_size_usdc": round(kelly_size, 8),
                "half_kelly_size_usdc": round(min(half_kelly_size, max_position), 8),
                "capped_kelly_size_usdc": round(capped_kelly_size, 8),
                "max_position_usdc": self.config.max_position_usdc,
                "max_total_outcome_exposure_usdc": self.config.max_total_outcome_exposure_usdc,
                "seconds_left": market.expiry_ts - now_ts,
            }
        )
        summary["shadow_sizing_evaluations"] = int(summary.get("shadow_sizing_evaluations", 0)) + 1

    def _log_shadow_maker_quote(
        self,
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        decision: SupervisorDecision,
        reference_price: float,
        now_ts: int,
        summary: dict[str, Any],
    ) -> None:
        if not self.config.enable_shadow_maker_quotes or self.config.mode != "paper":
            return
        if opportunity.side not in {"BUY_YES", "BUY_NO"}:
            return
        side_book = order_book.yes if opportunity.side == "BUY_YES" else order_book.no
        win_probability = _opportunity_win_probability(opportunity)
        if win_probability is None:
            return
        bid = side_book.bid
        ask = side_book.ask
        maker_price: float | None = None
        reason = "shadow_maker_quote_ok"
        if ask is None or ask <= 0:
            reason = "missing_ask"
        elif bid is None or bid <= 0:
            maker_price = max(min(ask - float(self.config.shadow_maker_price_improvement), 0.99999), 0.00001)
            reason = "synthetic_bid_from_ask"
        else:
            spread = max(ask - bid, 0.0)
            if spread <= 0:
                reason = "non_positive_spread"
            else:
                improvement = min(spread / 2.0, max(float(self.config.shadow_maker_price_improvement), 0.0))
                maker_price = min(bid + improvement, ask - 0.00001)
        maker_edge = None if maker_price is None else win_probability - maker_price
        maker_net_edge = (
            None
            if maker_edge is None
            else round(
                maker_edge
                - win_probability * max(float(self.config.outcome_settlement_fee_rate), 0.0)
                - float(self.config.safety_margin),
                8,
            )
        )
        quote_size = min(
            float(decision.approved_size_usdc if decision.approved else opportunity.requested_size_usdc),
            float(self.config.max_position_usdc),
            float(self.config.max_total_outcome_exposure_usdc),
        )
        quote_qty = (
            _quantized_token_qty_for_spend(quote_size, maker_price, self.config.outcome_size_decimals)
            if maker_price is not None
            else Decimal("0")
        )
        min_order_ok = (
            maker_price is not None
            and quote_qty > 0
            and _effective_exit_order_value(quote_qty, maker_price) >= float(self.config.min_order_value_usdc)
        )
        would_quote = (
            maker_price is not None
            and ask is not None
            and maker_price < ask
            and maker_net_edge is not None
            and maker_net_edge >= float(self.config.shadow_maker_min_net_edge)
            and quote_size > 0
            and min_order_ok
        )
        if not would_quote and reason == "shadow_maker_quote_ok":
            reason = "maker_edge_or_size_too_low"
        self.event_logger.log_shadow_maker_quote(
            {
                "ts": utc_now_iso(),
                "market_id": opportunity.market_id,
                "outcome": opportunity.outcome,
                "underlying": opportunity.underlying,
                "edge_type": opportunity.edge_type,
                "side": opportunity.side,
                "decision_approved": decision.approved,
                "decision_reason": decision.reason,
                "would_quote": would_quote,
                "reason": reason,
                "bid": bid,
                "ask": ask,
                "mid_price": _midpoint(bid, ask),
                "maker_price": maker_price,
                "maker_edge": None if maker_edge is None else round(maker_edge, 8),
                "maker_net_edge": maker_net_edge,
                "spread_capture": None if maker_price is None or ask is None else round(ask - maker_price, 8),
                "quote_size_usdc": round(quote_size, 8),
                "quote_token_qty": str(quote_qty),
                "min_order_ok": min_order_ok,
                "win_probability": round(win_probability, 8),
                "reference_price": reference_price,
                "strike": market.strike,
                "seconds_left": market.expiry_ts - now_ts,
            }
        )
        if would_quote:
            summary["shadow_maker_quotes"] = int(summary.get("shadow_maker_quotes", 0)) + 1

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
        positions_to_reconcile = self._positions_for_mode()
        open_positions = [
            position for position in positions_to_reconcile if position.status == "open"
        ]
        testnet_executor = self._testnet_executor_for_capital()
        if testnet_executor is None:
            return None
        account_address = testnet_executor.resolve_account_address()
        report = self.reconciler.reconcile(
            account_address=account_address,
            positions=positions_to_reconcile,
            start_time_ms=(
                _fills_start_time_ms(
                    positions_to_reconcile,
                    lookback_hours=self.config.fills_lookback_hours,
                )
                if positions_to_reconcile
                else None
            ),
        )
        self.event_logger.log_reconciliation(report)
        if apply_reconciliation_to_positions(self.positions, report):
            self.state_store.save_positions(self.positions)
        return report

    def _sync_settlement_accounting(self) -> None:
        changed = False
        for position in self.positions:
            if position.status not in {"estimated_settled", "settled", "early_exited"}:
                continue
            if _is_exchange_settlement(position):
                continue
            changed = _apply_settlement_accounting(position, self.config) or changed
        if changed:
            self.state_store.save_positions(self.positions)
        self._write_settlement_summary()

    def _write_settlement_summary(self) -> None:
        rows = [
            _settlement_row_from_position(position)
            for position in self.positions
            if position.status in {"estimated_settled", "settled", "early_exited"}
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
                "market_observation_ms": timings.get("market_observation_ms", 0.0),
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
            "operator_brief": summary.get("operator_brief", {}),
            "short_expiry_watchlist": summary.get("short_expiry_watchlist", []),
            "capital": self.last_capital_snapshot,
            "last_error": self.last_error,
            "last_execution_results": self.last_execution_results,
            "open_positions": [position.to_dict() for position in open_positions],
            "settled_positions": [
                position.to_dict()
                for position in self.positions
                if position.status in {"estimated_settled", "settled", "early_exited"}
                and _position_execution_mode(position) == self.config.mode.upper()
            ],
            "fee_model": self._fee_model_payload(),
            "blocked_opportunity_slices": list(self.config.blocked_opportunity_slices),
            "reference_divergence_guard": self._reference_divergence_guard_payload(),
            "embedded_observers": self._embedded_observer_status(),
            "market_observation": self._last_market_observation,
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
            position for position in self._positions_for_mode()
        ]
        settled_positions = [
            position
            for position in mode_positions
            if position.status in {"estimated_settled", "settled", "early_exited"}
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
        win_count = sum(
            1
            for position in settled_positions
            if float(position.estimated_pnl_usdc) >= 0
        )
        loss_count = sum(
            1
            for position in settled_positions
            if float(position.estimated_pnl_usdc) < 0
        )
        win_rate = (
            round(win_count / (win_count + loss_count), 4)
            if (win_count + loss_count) > 0
            else None
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
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "settlement_payout_usdc": settlement_payout_usdc,
            "total_unrealized_pnl_usd": 0.0,
            "healthy": self.last_error is None,
            "last_error": self.last_error,
            "last_execution_results": self.last_execution_results,
            "summary": summary,
            "operator_brief": summary.get("operator_brief", {}),
            "short_expiry_watchlist": summary.get("short_expiry_watchlist", []),
            "capital": self.last_capital_snapshot,
            "fee_model": self._fee_model_payload(),
            "blocked_opportunity_slices": list(self.config.blocked_opportunity_slices),
            "reference_divergence_guard": self._reference_divergence_guard_payload(),
            "embedded_observers": self._embedded_observer_status(),
            "market_observation": self._last_market_observation,
            "report": {
                "strategy": "HIP4OutcomeEdgePod",
                "closed_trade_count": len(settled_positions),
                "total_fill_count": total_fill_count,
                "realized_pnl_usd": realized_pnl_usd,
                "gross_pnl_usd": gross_pnl_usd,
                "fees_usd": fees_usd,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": win_rate,
                "settlement_payout_usdc": settlement_payout_usdc,
                "open_position_count": len(position_payloads),
                "loop_count": summary.get("loop_count", 0),
                "opportunities": summary.get("opportunities", 0),
                "approved": summary.get("approved", 0),
                "executed": summary.get("executed", 0),
            },
        }

    def _reference_divergence_guard_payload(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.config.block_reference_divergence),
            "max_bps": float(self.config.reference_divergence_max_bps),
            "min_rejected_sources": int(self.config.reference_divergence_min_rejected_sources),
            "underlyings": list(self.config.reference_divergence_underlyings),
            "sides": list(self.config.reference_divergence_sides),
            "edge_types": list(self.config.reference_divergence_edge_types),
        }

    def _active_positions_for_mode(self) -> list[OutcomePosition]:
        return [
            position
            for position in self.positions
            if position.status == "open"
            and _position_execution_mode(position) == self.config.mode.upper()
        ]

    def _positions_for_mode(self) -> list[OutcomePosition]:
        return [
            position
            for position in self.positions
            if _position_execution_mode(position) == self.config.mode.upper()
        ]


def _short_expiry_watchlist_row(
    *,
    market: OutcomeMarket,
    order_book: Any,
    reference_price: float,
    now_ts: int,
    short_features: ShortHorizonFeatures | None,
    short_assessment: ShortExpiryAssessment | None,
    config: Hip4OutcomeConfig,
) -> dict[str, Any]:
    seconds_left = max(int(market.expiry_ts - now_ts), 0)
    distance_bps = None
    momentum_60s = None
    history_span_seconds = None
    sample_count = None
    if short_features is not None:
        distance_bps = short_features.distance_to_strike_bps
        momentum_60s = short_features.momentum_bps(60)
        history_span_seconds = short_features.history_span_seconds
        sample_count = short_features.sample_count
    elif market.strike > 0:
        distance_bps = ((reference_price - market.strike) / market.strike) * 10_000.0

    return {
        "market_id": market.market_id,
        "outcome": market.outcome,
        "underlying": market.underlying,
        "period": market.period,
        "expiry_ts": market.expiry_ts,
        "expiry_iso": market.expiry_iso,
        "seconds_left": seconds_left,
        "strike": market.strike,
        "reference_price": round(float(reference_price), 8),
        "distance_bps": None if distance_bps is None else round(float(distance_bps), 4),
        "momentum_bps_60s": None if momentum_60s is None else round(float(momentum_60s), 4),
        "history_span_seconds": history_span_seconds,
        "sample_count": sample_count,
        "yes_bid": getattr(order_book.yes, "bid", None),
        "yes_ask": getattr(order_book.yes, "ask", None),
        "no_bid": getattr(order_book.no, "bid", None),
        "no_ask": getattr(order_book.no, "ask", None),
        "book_probability_yes": (
            None
            if short_assessment is None
            else short_assessment.book_probability_yes
        ),
        "short_probability_yes": (
            None
            if short_assessment is None
            else short_assessment.probability_yes
        ),
        "best_side": "" if short_assessment is None else short_assessment.best_side,
        "best_gross_edge": None if short_assessment is None else short_assessment.best_gross_edge,
        "best_net_edge": None if short_assessment is None else short_assessment.best_net_edge,
        "confidence": None if short_assessment is None else short_assessment.confidence,
        "reason": "short_expiry_not_assessed" if short_assessment is None else short_assessment.reason,
        "readiness": _short_expiry_readiness(short_assessment, config=config),
    }


def _short_expiry_readiness(
    assessment: ShortExpiryAssessment | None,
    *,
    config: Hip4OutcomeConfig,
) -> str:
    if assessment is None:
        return "watch"
    if assessment.reason in {"short_expiry_missing_features", "short_expiry_history_warming"}:
        return "warming"
    if (
        assessment.reason == "Short-expiry model probability above visible ask"
        and assessment.best_gross_edge >= config.min_gross_edge
        and assessment.best_net_edge >= config.min_net_edge
        and assessment.confidence >= config.short_expiry_min_confidence
    ):
        return "ready"
    if assessment.reason.startswith("short_expiry_"):
        return "blocked"
    return "watch"


def _build_short_expiry_operator_brief(
    summary: dict[str, Any],
    *,
    config: Hip4OutcomeConfig,
    last_error: str | None,
) -> dict[str, Any]:
    watchlist = summary.get("short_expiry_watchlist")
    if not isinstance(watchlist, list):
        watchlist = []
    typed_rows = [row for row in watchlist if isinstance(row, dict)]
    ready_rows = [row for row in typed_rows if row.get("readiness") == "ready"]
    warming_rows = [row for row in typed_rows if row.get("readiness") == "warming"]
    blocked_rows = [row for row in typed_rows if row.get("readiness") == "blocked"]
    blocked_reasons = Counter(str(row.get("reason") or "unknown") for row in blocked_rows)
    decision_reasons = summary.get("decision_reasons")
    if not isinstance(decision_reasons, dict):
        decision_reasons = {}
    blocker_reasons = {
        str(reason): int(count)
        for reason, count in sorted(decision_reasons.items())
        if str(reason) != "local_outcome_risk_ok"
    }
    top_candidate = _top_short_candidate(ready_rows or typed_rows)
    executed = int(summary.get("executed", 0) or 0)
    approved = int(summary.get("approved", 0) or 0)
    opportunities = int(summary.get("opportunities", 0) or 0)

    if last_error:
        tone = "bad"
        label = "runtime_error"
    elif executed > 0:
        tone = "good"
        label = "executed"
    elif ready_rows:
        tone = "good"
        label = "short_edge_ready"
    elif warming_rows and not ready_rows:
        tone = "warn"
        label = "warming_history"
    elif typed_rows:
        tone = "neutral"
        label = "watching_window"
    elif int(summary.get("markets_supported", 0) or 0) > 0:
        tone = "neutral"
        label = "waiting_for_short_window"
    else:
        tone = "warn"
        label = "no_supported_market"

    return {
        "focus": "short_expiry",
        "tone": tone,
        "label": label,
        "candidate_count": len(typed_rows),
        "ready_count": len(ready_rows),
        "warming_count": len(warming_rows),
        "blocked_count": len(blocked_rows),
        "next_window_seconds": summary.get("next_short_expiry_seconds"),
        "top_candidate": top_candidate,
        "decision_reasons": dict(sorted(decision_reasons.items())),
        "blocker_reasons": blocker_reasons,
        "short_blocked_reasons": dict(sorted(blocked_reasons.items())),
        "opportunity_mix": summary.get("opportunity_mix", {}),
        "loop": {
            "opportunities": opportunities,
            "approved": approved,
            "executed": executed,
        },
        "thresholds": {
            "window_minutes": int(config.short_expiry_window_minutes),
            "min_net_edge": float(config.min_net_edge),
            "min_confidence": float(config.short_expiry_min_confidence),
        },
    }


def _top_short_candidate(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            _float_from_any(row.get("best_net_edge"), -1.0),
            _float_from_any(row.get("confidence"), 0.0),
            -_int_from_any(row.get("seconds_left"), 10**9),
        ),
    )


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


def _position_price_bucket(position: OutcomePosition) -> tuple[float, float] | None:
    signal = position.metadata.get("signal", {})
    if not isinstance(signal, dict):
        return None
    metadata = signal.get("metadata", {})
    if not isinstance(metadata, dict):
        return None
    try:
        lower = float(metadata.get("bucket_lower"))
        upper = float(metadata.get("bucket_upper"))
    except (TypeError, ValueError):
        return None
    if lower <= 0 or upper <= lower:
        return None
    return lower, upper


def _single_position_side_name(position: OutcomePosition) -> str | None:
    if position.side == "BUY_YES":
        return "YES"
    if position.side == "BUY_NO":
        return "NO"
    sides = {fill.side_name.upper() for fill in position.fills if fill.token_qty > 0}
    if len(sides) == 1:
        side_name = next(iter(sides))
        if side_name in {"YES", "NO"}:
            return side_name
    return None


def _position_win_probability(
    *,
    side_name: str,
    probability_yes: float,
    short_assessment: ShortExpiryAssessment | None,
    seconds_left: int,
    short_window_seconds: int,
) -> float:
    probability_yes = max(min(float(probability_yes), 1.0), 0.0)
    if short_assessment is not None and seconds_left <= max(int(short_window_seconds), 0):
        probability_yes = max(min(float(short_assessment.probability_yes), 1.0), 0.0)
    if side_name == "YES":
        return probability_yes
    if side_name == "NO":
        return 1.0 - probability_yes
    return 0.0


def _entry_token_qty_by_side(position: OutcomePosition) -> dict[str, Decimal]:
    totals = {"YES": Decimal("0"), "NO": Decimal("0")}
    for fill in position.fills:
        side_name = fill.side_name.upper()
        if side_name in totals and fill.token_qty > 0:
            totals[side_name] += fill.token_qty
    return totals


def _remaining_token_qty_by_side(position: OutcomePosition) -> dict[str, Decimal]:
    remaining = _entry_token_qty_by_side(position)
    for item in _early_exit_entries(position):
        side_name = str(item.get("side_name", "")).upper()
        if side_name not in remaining:
            continue
        try:
            qty = Decimal(str(item.get("token_qty", "0")))
        except Exception:
            qty = Decimal("0")
        remaining[side_name] = max(remaining[side_name] - qty, Decimal("0"))
    return remaining


def _remaining_token_qty(position: OutcomePosition, side_name: str) -> Decimal:
    return _remaining_token_qty_by_side(position).get(side_name.upper(), Decimal("0"))


def _early_exit_entries(position: OutcomePosition) -> list[dict[str, Any]]:
    exits = position.metadata.get("early_exits")
    if not isinstance(exits, list):
        return []
    return [item for item in exits if isinstance(item, dict)]


def _early_exit_cash_totals(position: OutcomePosition) -> tuple[float, float]:
    gross = 0.0
    fee = 0.0
    for item in _early_exit_entries(position):
        gross += _float_from_any(item.get("gross_exit_usdc"), 0.0)
        fee += _float_from_any(item.get("fee_usdc"), 0.0)
    return round(gross, 8), round(fee, 8)


def _cost_basis_for_exit(position: OutcomePosition, side_name: str, exit_qty: Decimal) -> float:
    if exit_qty <= 0:
        return 0.0
    side_name = side_name.upper()
    entry_qty = _entry_token_qty_by_side(position).get(side_name, Decimal("0"))
    if entry_qty <= 0:
        return 0.0
    side_cost = sum(
        float(fill.cost_usdc)
        for fill in position.fills
        if fill.side_name.upper() == side_name and fill.token_qty > 0
    )
    return round(side_cost * float(exit_qty / entry_qty), 8)


def _quantized_exit_qty(
    remaining_qty: Decimal,
    *,
    fraction: float,
    size_decimals: int,
) -> Decimal:
    if remaining_qty <= 0 or fraction <= 0:
        return Decimal("0")
    if fraction >= 0.999999:
        return remaining_qty
    decimals = max(int(size_decimals), 0)
    quantum = Decimal("1") if decimals == 0 else Decimal("1").scaleb(-decimals)
    return (remaining_qty * Decimal(str(fraction))).quantize(quantum, rounding=ROUND_DOWN)


def _effective_exit_order_value(exit_qty: Decimal, price: float) -> float:
    effective_price = max(min(float(price), 1.0 - float(price)), 0.00000001)
    return float(exit_qty) * effective_price


def _has_partial_take_profit_exit(position: OutcomePosition) -> bool:
    for item in _early_exit_entries(position):
        if item.get("action") == "partial_exit" or item.get("reason") == "partial_take_profit":
            return True
    return False


def _shadow_exit_policy_specs(config: Hip4OutcomeConfig) -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = [{"policy": "hold_to_settlement", "kind": "hold"}]
    seen: set[str] = {"hold_to_settlement"}
    for roi in sorted({round(float(value), 6) for value in config.shadow_exit_take_profit_rois if value > 0}):
        name = f"tp_{int(round(roi * 100))}_partial"
        if name in seen:
            continue
        policies.append({"policy": name, "kind": "take_profit_partial", "roi": roi})
        seen.add(name)
    for window in sorted({int(value) for value in config.shadow_exit_short_window_seconds if value > 0}):
        name = f"last_{int(window / 60)}m_full" if window % 60 == 0 else f"last_{window}s_full"
        if name in seen:
            continue
        policies.append({"policy": name, "kind": "last_window_full", "window_seconds": window})
        seen.add(name)
    for item in (
        {"policy": "ev_plus_2pct_full", "kind": "ev_full"},
        {"policy": "prob_stop_full", "kind": "prob_stop_full"},
    ):
        if str(item["policy"]) not in seen:
            policies.append(item)
            seen.add(str(item["policy"]))
    return policies


def _shock_thresholds_by_window(config: Hip4OutcomeConfig) -> list[tuple[int, float]]:
    windows = [int(value) for value in config.shock_guard_windows_seconds if int(value) > 0]
    thresholds = [float(value) for value in config.shock_guard_adverse_move_bps if float(value) > 0]
    if not windows or not thresholds:
        return []
    pairs: list[tuple[int, float]] = []
    last_threshold = thresholds[-1]
    for index, window in enumerate(windows):
        threshold = thresholds[index] if index < len(thresholds) else last_threshold
        pairs.append((window, threshold))
    return pairs


def _pnl_levers_payload(config: Hip4OutcomeConfig) -> dict[str, Any]:
    return {
        "active_dry_run": [
            {
                "name": "bid_over_conservative_hold_ev",
                "enabled": bool(config.enable_early_exit),
                "min_ev_premium": float(config.early_exit_min_ev_premium),
                "min_exit_roi": float(config.early_exit_min_ev_exit_roi),
                "log": "early_exits.csv",
            },
            {
                "name": "probability_stop_intermediate",
                "enabled": bool(config.enable_early_exit and config.enable_early_exit_probability_stop),
                "conservative_probability_lte": float(config.early_exit_stop_probability),
                "exit_roi_gte": -float(config.early_exit_stop_max_loss_roi),
                "log": "early_exits.csv",
            },
        ],
        "observe": [
            {
                "name": "shadow_policy_ev_plus_2pct_full",
                "enabled": bool(config.enable_shadow_exit_policies),
                "min_ev_premium": float(config.early_exit_min_ev_premium),
                "min_exit_roi": float(config.early_exit_min_ev_exit_roi),
                "log": "shadow_exit_policies.csv",
            },
            {
                "name": "shadow_sizing_half_kelly",
                "enabled": bool(config.enable_shadow_sizing),
                "bankroll_usdc": float(config.shadow_sizing_bankroll_usdc),
                "kelly_fraction_cap": float(config.shadow_sizing_kelly_fraction_cap),
                "probability_haircut": float(config.shadow_sizing_probability_haircut),
                "log": "shadow_sizing.csv",
            },
            {
                "name": "shock_guard_two_window_confirmation",
                "enabled": bool(
                    config.enable_shock_guard
                    and int(config.shock_guard_min_adverse_windows) >= 2
                ),
                "min_adverse_windows": int(config.shock_guard_min_adverse_windows),
                "windows_seconds": list(config.shock_guard_windows_seconds),
                "log": "decisions.jsonl",
            },
            {
                "name": "short_expiry_observe_only",
                "enabled": bool(config.enable_short_expiry and config.short_expiry_observe_only),
                "log": "short_expiry_features.csv",
            },
        ],
    }


def _parse_price_samples(raw_samples: object) -> list[dict[str, float]]:
    if not isinstance(raw_samples, list):
        return []
    samples: list[dict[str, float]] = []
    for raw in raw_samples:
        if not isinstance(raw, dict):
            continue
        try:
            ts = float(raw.get("ts"))
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            continue
        if ts <= 0 or price <= 0:
            continue
        samples.append({"ts": ts, "price": price})
    samples.sort(key=lambda item: item["ts"])
    return samples


def _seed_price_history_from_opportunities(
    path: Path,
    *,
    now_ts: int,
    max_age_seconds: int,
    sample_interval_seconds: int,
    sample_limit: int,
) -> dict[str, list[dict[str, float]]]:
    if not path.exists():
        return {}
    cutoff = now_ts - max(int(max_age_seconds), 1)
    min_interval = max(int(sample_interval_seconds), 0)
    limit = max(int(sample_limit), 2)
    history: dict[str, list[dict[str, float]]] = {}
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                underlying = str(row.get("underlying") or "").strip().upper()
                if not underlying:
                    continue
                try:
                    row_ts = _parse_iso_to_epoch(str(row.get("ts") or ""))
                    price = float(row.get("ref_price") or 0.0)
                except (TypeError, ValueError):
                    continue
                if row_ts < cutoff or row_ts > now_ts or price <= 0:
                    continue
                samples = history.get(underlying, [])
                if samples and int(row_ts - samples[-1]["ts"]) < min_interval:
                    continue
                samples.append({"ts": float(row_ts), "price": float(price)})
                history[underlying] = samples[-limit:]
    except OSError:
        return {}
    return history


def _parse_iso_to_epoch(value: str) -> int:
    text = value.strip()
    if not text:
        raise ValueError("empty timestamp")
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _shadow_exit_policies_payload(position: OutcomePosition) -> dict[str, Any]:
    payload = position.metadata.get("shadow_exit_policies")
    if not isinstance(payload, dict):
        payload = {}
        position.metadata["shadow_exit_policies"] = payload
    return payload


def _shadow_exit_policy_state(position: OutcomePosition, policy: str) -> dict[str, Any]:
    payload = position.metadata.get("shadow_exit_policies")
    if not isinstance(payload, dict):
        return {}
    state = payload.get(policy)
    return state if isinstance(state, dict) else {}


def _ensure_shadow_exit_policy_state(position: OutcomePosition, policy: str) -> dict[str, Any]:
    payload = _shadow_exit_policies_payload(position)
    state = payload.get(policy)
    if not isinstance(state, dict):
        state = {"status": "open", "exits": []}
        payload[policy] = state
    exits = state.get("exits")
    if not isinstance(exits, list):
        state["exits"] = []
    if not state.get("status"):
        state["status"] = "open"
    return state


def _shadow_exit_entries(position: OutcomePosition, policy: str) -> list[dict[str, Any]]:
    state = _shadow_exit_policy_state(position, policy)
    exits = state.get("exits")
    if not isinstance(exits, list):
        return []
    return [item for item in exits if isinstance(item, dict)]


def _shadow_remaining_token_qty(position: OutcomePosition, side_name: str, policy: str) -> Decimal:
    remaining = _entry_token_qty_by_side(position).get(side_name.upper(), Decimal("0"))
    for item in _shadow_exit_entries(position, policy):
        if str(item.get("side_name", "")).upper() != side_name.upper():
            continue
        try:
            qty = Decimal(str(item.get("token_qty", "0")))
        except Exception:
            qty = Decimal("0")
        remaining = max(remaining - qty, Decimal("0"))
    return remaining


def _shadow_exit_cash_totals(position: OutcomePosition, policy: str) -> tuple[float, float]:
    gross = 0.0
    fee = 0.0
    for item in _shadow_exit_entries(position, policy):
        gross += _float_from_any(item.get("gross_exit_usdc"), 0.0)
        fee += _float_from_any(item.get("fee_usdc"), 0.0)
    return round(gross, 8), round(fee, 8)


def _opportunity_entry_price(
    opportunity: OutcomeOpportunity,
    order_book: OutcomeOrderBook,
) -> float | None:
    if opportunity.side == "BUY_YES":
        return order_book.yes.ask or _optional_float(opportunity.metadata.get("yes_ask"))
    if opportunity.side == "BUY_NO":
        return order_book.no.ask or _optional_float(opportunity.metadata.get("no_ask"))
    if opportunity.side == "BUY_BOTH":
        yes_ask = order_book.yes.ask or _optional_float(opportunity.metadata.get("yes_ask"))
        no_ask = order_book.no.ask or _optional_float(opportunity.metadata.get("no_ask"))
        if yes_ask is None or no_ask is None:
            return None
        return yes_ask + no_ask
    return None


def _opportunity_win_probability(opportunity: OutcomeOpportunity) -> float | None:
    metadata = opportunity.metadata
    if opportunity.edge_type in {"LATE_EXPIRY", "PARITY"}:
        return 1.0
    if opportunity.side == "BUY_YES":
        probability = _first_float(
            metadata,
            ("short_probability_yes", "probability_bucket", "probability_yes"),
        )
        return None if probability is None else max(min(probability, 1.0), 0.0)
    if opportunity.side == "BUY_NO":
        direct_no = _first_float(metadata, ("probability_outside_bucket", "probability_no"))
        if direct_no is not None:
            return max(min(direct_no, 1.0), 0.0)
        probability_yes = _first_float(
            metadata,
            ("short_probability_yes", "probability_bucket", "probability_yes"),
        )
        return None if probability_yes is None else max(min(1.0 - probability_yes, 1.0), 0.0)
    return None


def _first_float(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _optional_float(payload.get(key))
        if value is not None:
            return value
    return None


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _binary_kelly_fraction(win_probability: float, entry_price: float) -> float:
    price = max(min(float(entry_price), 0.999999), 0.000001)
    probability = max(min(float(win_probability), 1.0), 0.0)
    return max((probability - price) / max(1.0 - price, 0.000001), 0.0)


def _quantized_token_qty_for_spend(
    spend_usdc: float,
    price: float | None,
    size_decimals: int,
) -> Decimal:
    if price is None or price <= 0 or spend_usdc <= 0:
        return Decimal("0")
    decimals = max(int(size_decimals), 0)
    quantum = Decimal("1") if decimals == 0 else Decimal("1").scaleb(-decimals)
    return (Decimal(str(spend_usdc)) / Decimal(str(price))).quantize(quantum, rounding=ROUND_DOWN)


def _midpoint(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return round(max(min((float(bid) + float(ask)) / 2.0, 1.0), 0.0), 8)
    if bid is not None:
        return round(max(min(float(bid), 1.0), 0.0), 8)
    if ask is not None:
        return round(max(min(float(ask), 1.0), 0.0), 8)
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
    if _is_exchange_settlement(position):
        return False
    settlement = position.metadata.get("settlement")
    if not isinstance(settlement, dict):
        settlement = {}
    early_gross, early_fee = _early_exit_cash_totals(position)
    if early_gross > 0 or early_fee > 0:
        settlement_payout = _float_from_any(
            settlement.get("settlement_payout_usdc"),
            max(float(position.estimated_payout_usdc or 0.0), 0.0),
        )
        settlement["settlement_payout_usdc"] = round(settlement_payout, 8)
        payout = round(settlement_payout + early_gross, 8)
        fee = round(
            settlement_payout * max(float(config.outcome_settlement_fee_rate), 0.0) + early_fee,
            8,
        )
    else:
        payout = max(float(position.estimated_payout_usdc or 0.0), 0.0)
        fee = round(payout * max(float(config.outcome_settlement_fee_rate), 0.0), 8)
    gross_pnl = round(payout - float(position.cost_usdc or 0.0), 8)
    net_pnl = round(gross_pnl - fee, 8)
    changed = (
        position.estimated_payout_usdc != payout
        or position.estimated_fee_usdc != fee
        or position.estimated_gross_pnl_usdc != gross_pnl
        or position.estimated_pnl_usdc != net_pnl
    )
    position.estimated_payout_usdc = payout
    position.estimated_fee_usdc = fee
    position.estimated_gross_pnl_usdc = gross_pnl
    position.estimated_pnl_usdc = net_pnl
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
        "is_win": position.estimated_pnl_usdc >= 0,
        "notes": notes,
    }


def _is_exchange_settlement(position: OutcomePosition) -> bool:
    settlement = position.metadata.get("settlement")
    return isinstance(settlement, dict) and settlement.get("source") == "hyperliquid_user_fills"


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
