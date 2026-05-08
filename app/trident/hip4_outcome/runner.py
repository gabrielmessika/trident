from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
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
    OutcomePosition,
    ShortExpiryAssessment,
    ShortHorizonFeatures,
    SupervisorDecision,
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
            "short_expiry_markets": 0,
            "short_expiry_assessments": 0,
            "short_expiry_best_net_edge": None,
            "short_expiry_ready_count": 0,
            "short_expiry_watchlist": [],
            "next_short_expiry_seconds": None,
            "decision_reasons": {},
            "opportunity_mix": {},
            "operator_brief": {},
            "market_observation": {},
            "embedded_observers": self._embedded_observer_status(),
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
            timings["short_features_ms"] = _elapsed_ms(stage_started)

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
        if self.config.mode == "testnet":
            return
        changed = False
        for position in self.positions:
            if position.status != "open":
                continue
            if now_ts < position.expiry_ts + self.config.settlement_grace_seconds:
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
            settlement_payload = {
                "result": "YES" if result_yes else "NO",
                "reference_price": reference_price,
                "fee_model": self._fee_model_payload(),
                "notes": "estimated_from_reference_price",
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
            if position.status not in {"estimated_settled", "settled"}:
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
                if position.status in {"estimated_settled", "settled"}
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
