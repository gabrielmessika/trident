import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.trident.hip4_outcome import HIP4OutcomeEdgePod
from app.trident.hip4_outcome.book import build_order_book, parse_side_book
from app.trident.hip4_outcome.capital import OutcomeCapitalGuard
from app.trident.hip4_outcome.config import Hip4OutcomeConfig, load_hip4_outcome_config
from app.trident.hip4_outcome.edge import OutcomeEdgeDetector
from app.trident.hip4_outcome.execution import TestnetOutcomeExecutor, build_order_legs
from app.trident.hip4_outcome.external_prices import (
    ExternalPriceAggregator,
    ReferencePriceQuote,
)
from app.trident.hip4_outcome.features import (
    ShortHorizonFeatureBuilder,
    update_price_history_payload,
)
from app.trident.hip4_outcome.models import (
    OutcomeExecutionResult,
    OutcomeFill,
    OutcomeOpportunity,
    OutcomePosition,
    ShortHorizonFeatures,
    SupervisorDecision,
    outcome_asset_id,
    outcome_coin,
    outcome_encoding,
)
from app.trident.hip4_outcome.parser import (
    parse_outcome_markets,
    parse_outcome_observations,
    parse_price_binary_outcome,
    parse_price_bucket_outcome,
)
from app.trident.hip4_outcome.probability import ProbabilityModel, probability_between_prices
from app.trident.hip4_outcome.reconciliation import (
    OutcomeReconciler,
    apply_reconciliation_to_positions,
    parse_spot_balances,
    parse_user_fills,
)
from app.trident.hip4_outcome.reporting import build_daily_summary_rows, replay_opportunities
from app.trident.hip4_outcome.risk import OutcomeRiskManager
from app.trident.hip4_outcome.runner import (
    _build_short_expiry_operator_brief,
    _short_expiry_watchlist_row,
)
from app.trident.hip4_outcome.state import OutcomeStateStore


class HIP4OutcomePodTests(unittest.TestCase):
    def _market(self):
        market = parse_price_binary_outcome(
            {
                "outcome": 5721,
                "name": "Recurring",
                "description": "class:priceBinary|underlying:BTC|expiry:20260502-0300|targetPrice:76775|period:1d",
                "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
            }
        )
        self.assertIsNotNone(market)
        return market

    def _book(self):
        return build_order_book(
            market_id="BTC_GT_76775_20260502_0300",
            yes_payload={
                "coin": "#57210",
                "time": 1,
                "levels": [
                    [{"px": "0.22", "sz": "100", "n": 1}],
                    [{"px": "0.24", "sz": "100", "n": 1}],
                ],
            },
            no_payload={
                "coin": "#57211",
                "time": 1,
                "levels": [
                    [{"px": "0.75", "sz": "100", "n": 1}],
                    [{"px": "0.76", "sz": "100", "n": 1}],
                ],
            },
            max_slippage=0.03,
        )

    def test_outcome_asset_encoding_matches_hip4_doc(self) -> None:
        self.assertEqual(outcome_encoding(5721, 0), 57210)
        self.assertEqual(outcome_coin(5721, 1), "#57211")
        self.assertEqual(outcome_asset_id(5721, 0), 100057210)

    def test_parses_price_binary_outcome_meta(self) -> None:
        market = self._market()

        self.assertEqual(market.outcome, 5721)
        self.assertEqual(market.underlying, "BTC")
        self.assertEqual(market.strike, 76775.0)
        self.assertEqual(market.yes_coin, "#57210")
        self.assertEqual(market.no_coin, "#57211")
        self.assertEqual(market.expiry_iso, "2026-05-02T03:00:00Z")

    def test_filters_out_non_price_binary_outcomes(self) -> None:
        payload = {
            "outcomes": [
                {
                    "outcome": 9,
                    "name": "Who wins?",
                    "description": "This race is yet to be scheduled.",
                    "sideSpecs": [{"name": "A"}, {"name": "B"}],
                },
                {
                    "outcome": 5789,
                    "name": "Recurring",
                    "description": "class:priceBinary|underlying:HYPE|expiry:20260501-2000|targetPrice:26.121|period:15m",
                    "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                },
            ]
        }

        markets = parse_outcome_markets(payload, include_underlyings=["HYPE"])

        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0].underlying, "HYPE")
        self.assertEqual(markets[0].strike, 26.121)

    def test_parses_price_bucket_outcome_meta(self) -> None:
        market = parse_price_bucket_outcome(
            {
                "outcome": 6200,
                "name": "BTC range",
                "description": "class:priceBucket|underlying:BTC|expiry:20260505-1015|thresholds:80000,81000|period:15m",
                "sideSpecs": [{"name": "Inside"}, {"name": "Outside"}],
            }
        )

        self.assertIsNotNone(market)
        self.assertEqual(market.class_name, "priceBucket")
        self.assertEqual(market.market_id, "BTC_BUCKET_80000_81000_20260505_1015")
        self.assertEqual(market.bucket_lower, 80000.0)
        self.assertEqual(market.bucket_upper, 81000.0)
        self.assertEqual(market.thresholds, (80000.0, 81000.0))
        self.assertEqual(market.side_names, ("Inside", "Outside"))

    def test_price_bucket_with_multiple_thresholds_uses_indexed_adjacent_range(self) -> None:
        market = parse_price_bucket_outcome(
            {
                "outcome": 6201,
                "name": "BTC range",
                "description": "class:priceBucket|underlying:BTC|expiry:20260505-1015|thresholds:80000,81000,82000|index:1|period:15m",
                "sideSpecs": [{"name": "Inside"}, {"name": "Outside"}],
            }
        )

        self.assertIsNotNone(market)
        self.assertEqual(market.bucket_lower, 81000.0)
        self.assertEqual(market.bucket_upper, 82000.0)
        self.assertEqual(market.bucket_index, 1)

    def test_outcome_observations_keep_named_outcomes_watch_only(self) -> None:
        observations = parse_outcome_observations(
            {
                "outcomes": [
                    {
                        "outcome": 6290,
                        "name": "Recurring Named Outcome",
                        "description": "index:0",
                        "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                    },
                    {
                        "outcome": 6200,
                        "name": "BTC range",
                        "description": "class:priceBucket|underlying:BTC|expiry:20260505-1015|thresholds:80000,81000|period:15m",
                        "sideSpecs": [{"name": "Inside"}, {"name": "Outside"}],
                    },
                ]
            }
        )

        self.assertEqual(observations[0].class_name, "namedOutcome")
        self.assertEqual(observations[0].support_status, "observe_only")
        self.assertEqual(observations[0].support_reason, "named_outcome_observation_only")
        self.assertEqual(observations[0].coins, ("#62900", "#62901"))
        self.assertEqual(observations[1].class_name, "priceBucket")
        self.assertEqual(observations[1].support_status, "paper_supported")

    def test_parses_order_book_depth_within_slippage(self) -> None:
        book = parse_side_book(
            {
                "coin": "#1",
                "time": 1,
                "levels": [
                    [{"px": "0.49", "sz": "10", "n": 1}],
                    [
                        {"px": "0.50", "sz": "20", "n": 1},
                        {"px": "0.51", "sz": "30", "n": 1},
                        {"px": "0.60", "sz": "40", "n": 1},
                    ],
                ],
            },
            max_slippage=0.03,
        )

        self.assertEqual(book.bid, 0.49)
        self.assertEqual(book.ask, 0.50)
        self.assertAlmostEqual(book.ask_depth_usdc, 25.3)

    def test_model_detector_finds_yes_mispricing(self) -> None:
        market = self._market()
        book = self._book()
        config = Hip4OutcomeConfig(
            mode="paper",
            min_gross_edge=0.01,
            min_net_edge=0.001,
            max_position_usdc=5.0,
            min_yes_depth_usdc=1.0,
            min_no_depth_usdc=1.0,
        )
        probability = ProbabilityModel(config).estimate(
            market,
            reference_price=80000.0,
            now_ts=market.expiry_ts - 3600,
        )
        opportunities = OutcomeEdgeDetector(config).detect(
            market=market,
            order_book=book,
            reference_price=80000.0,
            probability=probability,
            now_ts=market.expiry_ts - 3600,
        )

        self.assertTrue(any(item.side == "BUY_YES" for item in opportunities))

    def test_price_bucket_probability_and_detector_are_paper_safe(self) -> None:
        market = parse_price_bucket_outcome(
            {
                "outcome": 6200,
                "name": "BTC range",
                "description": "class:priceBucket|underlying:BTC|expiry:20260505-1015|thresholds:80000,81000|period:15m",
                "sideSpecs": [{"name": "Inside"}, {"name": "Outside"}],
            }
        )
        self.assertIsNotNone(market)
        book = build_order_book(
            market_id=market.market_id,
            yes_payload={
                "coin": market.yes_coin,
                "time": 1,
                "levels": [
                    [{"px": "0.30", "sz": "100", "n": 1}],
                    [{"px": "0.32", "sz": "100", "n": 1}],
                ],
            },
            no_payload={
                "coin": market.no_coin,
                "time": 1,
                "levels": [
                    [{"px": "0.66", "sz": "100", "n": 1}],
                    [{"px": "0.68", "sz": "100", "n": 1}],
                ],
            },
            max_slippage=0.03,
        )
        config = Hip4OutcomeConfig(
            mode="paper",
            min_gross_edge=0.01,
            min_net_edge=0.001,
            max_position_usdc=5.0,
            min_yes_depth_usdc=1.0,
            min_no_depth_usdc=1.0,
        )

        probability = ProbabilityModel(config).estimate(
            market,
            reference_price=80500.0,
            now_ts=market.expiry_ts - 900,
        )
        opportunities = OutcomeEdgeDetector(config).detect(
            market=market,
            order_book=book,
            reference_price=80500.0,
            probability=probability,
            now_ts=market.expiry_ts - 900,
        )

        self.assertGreater(probability.probability_yes, 0.0)
        self.assertEqual(probability.model_name, "lognormal_static_vol_range_v1")
        self.assertTrue(any(item.edge_type == "PRICE_BUCKET_MODEL" for item in opportunities))

        testnet_decision = OutcomeRiskManager(
            Hip4OutcomeConfig(
                mode="testnet",
                allow_testnet_orders=True,
                min_yes_depth_usdc=1.0,
                min_no_depth_usdc=1.0,
            )
        ).evaluate(
            opportunity=opportunities[0],
            market=market,
            order_book=book,
            open_positions=[],
            now_ts=market.expiry_ts - 900,
        )

        self.assertFalse(testnet_decision.approved)
        self.assertEqual(testnet_decision.reason, "price_bucket_paper_only")

    def test_short_expiry_watchlist_row_marks_ready_edge(self) -> None:
        market = self._market()
        now_ts = market.expiry_ts - 120
        book = self._book()
        config = Hip4OutcomeConfig(
            min_gross_edge=0.025,
            min_net_edge=0.015,
            short_expiry_periods=["1d"],
            short_expiry_min_confidence=0.55,
        )
        features = ShortHorizonFeatures(
            underlying="BTC",
            reference_price=77000.0,
            strike=market.strike,
            seconds_left=120,
            sample_count=12,
            history_span_seconds=90,
            distance_to_strike_bps=29.3,
            momentum_bps_by_window={60: 5.5},
            has_min_history=True,
        )
        probability = ProbabilityModel(config).estimate(
            market,
            reference_price=77000.0,
            now_ts=now_ts,
        )
        assessment = OutcomeEdgeDetector(config).assess_short_expiry(
            market=market,
            order_book=book,
            probability=probability,
            reference_price=77000.0,
            now_ts=now_ts,
            features=features,
        )

        self.assertIsNotNone(assessment)
        row = _short_expiry_watchlist_row(
            market=market,
            order_book=book,
            reference_price=77000.0,
            now_ts=now_ts,
            short_features=features,
            short_assessment=assessment,
            config=config,
        )

        self.assertEqual(row["underlying"], "BTC")
        self.assertEqual(row["seconds_left"], 120)
        self.assertEqual(row["readiness"], "ready")
        self.assertEqual(row["best_side"], "BUY_YES")

    def test_short_expiry_operator_brief_surfaces_ready_focus(self) -> None:
        config = Hip4OutcomeConfig(min_net_edge=0.015, short_expiry_min_confidence=0.55)
        summary = {
            "markets_supported": 3,
            "opportunities": 1,
            "approved": 1,
            "executed": 0,
            "next_short_expiry_seconds": 120,
            "decision_reasons": {"local_outcome_risk_ok": 1},
            "opportunity_mix": {"SHORT_EXPIRY": 1},
            "short_expiry_watchlist": [
                {
                    "readiness": "ready",
                    "underlying": "BTC",
                    "seconds_left": 120,
                    "best_net_edge": 0.042,
                    "confidence": 0.72,
                }
            ],
        }

        brief = _build_short_expiry_operator_brief(
            summary,
            config=config,
            last_error=None,
        )

        self.assertEqual(brief["tone"], "good")
        self.assertEqual(brief["label"], "short_edge_ready")
        self.assertEqual(brief["ready_count"], 1)
        self.assertEqual(brief["top_candidate"]["underlying"], "BTC")

    def test_probability_between_prices_settles_deterministically_at_expiry(self) -> None:
        self.assertEqual(
            probability_between_prices(
                spot=80500.0,
                lower=80000.0,
                upper=81000.0,
                time_to_expiry_years=0.0,
                annualized_vol=0.65,
            ),
            1.0,
        )
        self.assertEqual(
            probability_between_prices(
                spot=82000.0,
                lower=80000.0,
                upper=81000.0,
                time_to_expiry_years=0.0,
                annualized_vol=0.65,
            ),
            0.0,
        )

    def test_short_horizon_features_compute_recent_momentum(self) -> None:
        market = self._market()
        now_ts = market.expiry_ts - 300
        payload = {}
        history = update_price_history_payload(
            payload,
            {"BTC": 100.0},
            now_ts=now_ts - 65,
            max_age_seconds=900,
            sample_limit=20,
        )
        history = update_price_history_payload(
            payload,
            {"BTC": 101.0},
            now_ts=now_ts,
            max_age_seconds=900,
            sample_limit=20,
        )

        features = ShortHorizonFeatureBuilder(
            Hip4OutcomeConfig(
                short_expiry_min_history_seconds=30,
                short_expiry_momentum_windows_seconds=[60],
            )
        ).build(
            market=market,
            reference_price=101.0,
            now_ts=now_ts,
            history=history,
        )

        self.assertTrue(features.has_min_history)
        self.assertEqual(features.sample_count, 2)
        self.assertAlmostEqual(features.momentum_bps(60), 100.0)

    def test_short_expiry_detector_finds_yes_opportunity(self) -> None:
        market = self._market()
        book = self._book()
        now_ts = market.expiry_ts - 300
        config = Hip4OutcomeConfig(
            mode="paper",
            enable_late_expiry=False,
            enable_parity=False,
            enable_model=False,
            enable_short_expiry=True,
            short_expiry_window_minutes=6,
            short_expiry_periods=["1d"],
            short_expiry_min_history_seconds=30,
            short_expiry_momentum_windows_seconds=[60],
            short_expiry_min_confidence=0.5,
            min_gross_edge=0.01,
            min_net_edge=0.001,
            max_position_usdc=5.0,
            min_yes_depth_usdc=1.0,
            min_no_depth_usdc=1.0,
        )
        history = {
            "BTC": [
                {"ts": float(now_ts - 70), "price": 76800.0},
                {"ts": float(now_ts), "price": 77000.0},
            ]
        }
        probability = ProbabilityModel(config).estimate(
            market,
            reference_price=77000.0,
            now_ts=now_ts,
        )
        features = ShortHorizonFeatureBuilder(config).build(
            market=market,
            reference_price=77000.0,
            now_ts=now_ts,
            history=history,
        )
        detector = OutcomeEdgeDetector(config)

        opportunities = detector.detect(
            market=market,
            order_book=book,
            reference_price=77000.0,
            probability=probability,
            now_ts=now_ts,
            short_features=features,
        )

        self.assertTrue(any(item.edge_type == "SHORT_EXPIRY" for item in opportunities))
        self.assertTrue(any(item.side == "BUY_YES" for item in opportunities))

    def test_risk_rejects_observer_mode_after_signal_logging(self) -> None:
        market = self._market()
        book = self._book()
        config = Hip4OutcomeConfig(mode="observer", min_yes_depth_usdc=1, min_no_depth_usdc=1)
        opportunity = OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_YES",
            edge_type="MODEL",
            gross_edge=0.2,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.18,
            confidence=0.8,
            requested_size_usdc=5.0,
            max_loss_usdc=5.0,
            expiry_ts=market.expiry_ts,
            reason="test",
            metadata={"strike": market.strike},
        )

        decision = OutcomeRiskManager(config).evaluate(
            opportunity=opportunity,
            market=market,
            order_book=book,
            open_positions=[],
            now_ts=market.expiry_ts - 3600,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "observer_mode_signal_only")

    def test_risk_rejects_blocked_opportunity_slice_before_execution(self) -> None:
        market = parse_price_binary_outcome(
            {
                "outcome": 5789,
                "name": "Recurring",
                "description": "class:priceBinary|underlying:HYPE|expiry:20260502-0300|targetPrice:26.121|period:15m",
                "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
            }
        )
        self.assertIsNotNone(market)
        book = build_order_book(
            market_id=market.market_id,
            yes_payload={
                "coin": market.yes_coin,
                "time": 1,
                "levels": [
                    [{"px": "0.22", "sz": "100", "n": 1}],
                    [{"px": "0.24", "sz": "100", "n": 1}],
                ],
            },
            no_payload={
                "coin": market.no_coin,
                "time": 1,
                "levels": [
                    [{"px": "0.75", "sz": "100", "n": 1}],
                    [{"px": "0.76", "sz": "100", "n": 1}],
                ],
            },
            max_slippage=0.03,
        )
        opportunity = OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_YES",
            edge_type="LATE_EXPIRY",
            gross_edge=0.3,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.28,
            confidence=0.9,
            requested_size_usdc=25.0,
            max_loss_usdc=25.0,
            expiry_ts=market.expiry_ts,
            reason="test",
            metadata={"strike": market.strike},
        )
        config = Hip4OutcomeConfig(
            mode="testnet",
            allow_testnet_orders=True,
            blocked_opportunity_slices=["hype/late_expiry/buy_yes"],
            max_position_usdc=50.0,
            max_total_outcome_exposure_usdc=100.0,
            max_per_underlying_outcome_exposure_usdc=100.0,
            min_yes_depth_usdc=1.0,
            min_no_depth_usdc=1.0,
        )

        decision = OutcomeRiskManager(config).evaluate(
            opportunity=opportunity,
            market=market,
            order_book=book,
            open_positions=[],
            now_ts=market.expiry_ts - 300,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "blocked_outcome_slice")
        self.assertEqual(
            decision.constraints["blocked_slice"],
            "HYPE:LATE_EXPIRY:BUY_YES",
        )

    def test_risk_rejects_reference_divergence_before_execution(self) -> None:
        market = self._market()
        market.underlying = "HYPE"
        book = self._book()
        opportunity = OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_NO",
            edge_type="LATE_EXPIRY",
            gross_edge=0.3,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.28,
            confidence=0.9,
            requested_size_usdc=25.0,
            max_loss_usdc=25.0,
            expiry_ts=market.expiry_ts,
            reason="test",
            metadata={
                "reference_price": 30.92,
                "reference_max_deviation_bps": 3855.11,
                "reference_rejected_sources": [
                    {"source": "okx", "symbol": "HYPE-USDT", "price": 42.81},
                    {"source": "bybit", "symbol": "HYPEUSDT", "price": 42.83},
                ],
            },
        )
        config = Hip4OutcomeConfig(
            mode="testnet",
            allow_testnet_orders=True,
            block_reference_divergence=True,
            reference_divergence_max_bps=250.0,
            reference_divergence_min_rejected_sources=2,
            reference_divergence_underlyings=["HYPE"],
            max_position_usdc=50.0,
            max_total_outcome_exposure_usdc=100.0,
            max_per_underlying_outcome_exposure_usdc=100.0,
            min_yes_depth_usdc=1.0,
            min_no_depth_usdc=1.0,
        )

        decision = OutcomeRiskManager(config).evaluate(
            opportunity=opportunity,
            market=market,
            order_book=book,
            open_positions=[],
            now_ts=market.expiry_ts - 300,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "reference_divergence_guard")
        self.assertEqual(decision.constraints["underlying"], "HYPE")
        self.assertEqual(decision.constraints["reference_rejected_source_count"], 2)
        self.assertEqual(decision.constraints["reference_max_deviation_bps"], 3855.11)

    def test_risk_rejects_testnet_order_below_effective_hl_minimum(self) -> None:
        market = self._market()
        book = build_order_book(
            market_id=market.market_id,
            yes_payload={
                "coin": market.yes_coin,
                "time": 1,
                "levels": [
                    [],
                    [{"px": "0.71", "sz": "40", "n": 1}],
                ],
            },
            no_payload={
                "coin": market.no_coin,
                "time": 1,
                "levels": [[], []],
            },
            max_slippage=0.03,
        )
        opportunity = OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_YES",
            edge_type="MODEL",
            gross_edge=0.3,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.28,
            confidence=0.9,
            requested_size_usdc=25.0,
            max_loss_usdc=25.0,
            expiry_ts=market.expiry_ts,
            reason="test",
            metadata={"strike": market.strike},
        )
        config = Hip4OutcomeConfig(
            mode="testnet",
            allow_testnet_orders=True,
            max_position_usdc=25.0,
            max_total_outcome_exposure_usdc=100.0,
            max_per_underlying_outcome_exposure_usdc=100.0,
            min_yes_depth_usdc=1.0,
            min_no_depth_usdc=1.0,
            min_order_value_usdc=10.0,
            outcome_size_decimals=0,
        )

        decision = OutcomeRiskManager(config).evaluate(
            opportunity=opportunity,
            market=market,
            order_book=book,
            open_positions=[],
            now_ts=market.expiry_ts - 3600,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "below_exchange_min_order_value_yes")

    def test_risk_approves_testnet_order_once_effective_hl_minimum_is_met(self) -> None:
        market = self._market()
        book = build_order_book(
            market_id=market.market_id,
            yes_payload={
                "coin": market.yes_coin,
                "time": 1,
                "levels": [
                    [],
                    [{"px": "0.71", "sz": "40", "n": 1}],
                ],
            },
            no_payload={
                "coin": market.no_coin,
                "time": 1,
                "levels": [[], []],
            },
            max_slippage=0.03,
        )
        opportunity = OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_YES",
            edge_type="MODEL",
            gross_edge=0.3,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.28,
            confidence=0.9,
            requested_size_usdc=28.4,
            max_loss_usdc=28.4,
            expiry_ts=market.expiry_ts,
            reason="test",
            metadata={"strike": market.strike},
        )
        config = Hip4OutcomeConfig(
            mode="testnet",
            allow_testnet_orders=True,
            max_position_usdc=50.0,
            max_total_outcome_exposure_usdc=100.0,
            max_per_underlying_outcome_exposure_usdc=100.0,
            min_yes_depth_usdc=1.0,
            min_no_depth_usdc=1.0,
            min_order_value_usdc=10.0,
            outcome_size_decimals=0,
        )

        decision = OutcomeRiskManager(config).evaluate(
            opportunity=opportunity,
            market=market,
            order_book=book,
            open_positions=[],
            now_ts=market.expiry_ts - 3600,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.approved_size_usdc, 28.4)

    def test_builds_parity_order_legs_with_equal_token_qty(self) -> None:
        market = self._market()
        book = self._book()
        opportunity = OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_BOTH",
            edge_type="PARITY",
            gross_edge=0.02,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.02,
            confidence=0.9,
            requested_size_usdc=5.0,
            max_loss_usdc=5.0,
            expiry_ts=market.expiry_ts,
            reason="test",
        )

        legs = build_order_legs(
            opportunity=opportunity,
            market=market,
            order_book=book,
            approved_size_usdc=5.0,
            max_order_slippage=0.0,
            size_decimals=0,
        )

        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0]["token_qty"], legs[1]["token_qty"])

    def test_builds_named_outcome_no_basket_order_legs(self) -> None:
        market = self._market()
        opportunity = OutcomeOpportunity(
            market_id="BTC_GT_76775_20260502_0300:NAMED_NO_BASKET:10-11-12",
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_NAMED_NO_BASKET",
            edge_type="NAMED_OUTCOME_NO_BASKET",
            gross_edge=0.5,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.48,
            confidence=0.9,
            requested_size_usdc=12.0,
            max_loss_usdc=12.0,
            expiry_ts=market.expiry_ts,
            reason="test",
            metadata={
                "basket_legs": [
                    {"coin": "#101", "side_name": "NO", "ask": 0.4},
                    {"coin": "#111", "side_name": "NO", "ask": 0.4},
                    {"coin": "#121", "side_name": "NO", "ask": 0.4},
                ]
            },
        )

        legs = build_order_legs(
            opportunity=opportunity,
            market=market,
            order_book=self._book(),
            approved_size_usdc=12.0,
            max_order_slippage=0.0,
            size_decimals=0,
        )

        self.assertEqual(len(legs), 3)
        self.assertEqual({leg["side_name"] for leg in legs}, {"NO"})
        self.assertEqual({leg["token_qty"] for leg in legs}, {Decimal("10")})

    def test_order_legs_respect_min_order_value_after_rounding(self) -> None:
        market = self._market()
        book = self._book()
        opportunity = OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_YES",
            edge_type="MODEL",
            gross_edge=0.2,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.18,
            confidence=0.8,
            requested_size_usdc=5.0,
            max_loss_usdc=5.0,
            expiry_ts=market.expiry_ts,
            reason="test",
        )

        too_small = build_order_legs(
            opportunity=opportunity,
            market=market,
            order_book=book,
            approved_size_usdc=2.5,
            max_order_slippage=0.03,
            min_order_value_usdc=10.0,
            size_decimals=0,
        )
        large_enough = build_order_legs(
            opportunity=opportunity,
            market=market,
            order_book=book,
            approved_size_usdc=12.0,
            max_order_slippage=0.03,
            min_order_value_usdc=10.0,
            size_decimals=0,
        )

        self.assertEqual(too_small, [])
        self.assertEqual(len(large_enough), 1)

    def test_order_legs_use_outcome_effective_min_value(self) -> None:
        market = self._market()
        book = build_order_book(
            market_id=market.market_id,
            yes_payload={
                "coin": market.yes_coin,
                "time": 1,
                "levels": [[{"px": "0.29", "sz": "40", "n": 1}], []],
            },
            no_payload={
                "coin": market.no_coin,
                "time": 1,
                "levels": [[], [{"px": "0.69", "sz": "40", "n": 1}]],
            },
            max_slippage=0.03,
        )
        opportunity = OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_NO",
            edge_type="MODEL",
            gross_edge=0.2,
            estimated_fees=0.0,
            estimated_slippage=0.0,
            net_edge=0.18,
            confidence=0.8,
            requested_size_usdc=25.0,
            max_loss_usdc=25.0,
            expiry_ts=market.expiry_ts,
            reason="test",
        )

        too_small = build_order_legs(
            opportunity=opportunity,
            market=market,
            order_book=book,
            approved_size_usdc=12.0,
            max_order_slippage=0.03,
            min_order_value_usdc=10.0,
            size_decimals=0,
        )
        large_enough = build_order_legs(
            opportunity=opportunity,
            market=market,
            order_book=book,
            approved_size_usdc=25.0,
            max_order_slippage=0.03,
            min_order_value_usdc=10.0,
            size_decimals=0,
        )

        self.assertEqual(too_small, [])
        self.assertEqual(len(large_enough), 1)
        self.assertGreaterEqual(float(large_enough[0]["expected_order_value_usdc"]), 10.0)

    def test_reference_price_aggregator_uses_median_and_rejects_outlier(self) -> None:
        config = Hip4OutcomeConfig(
            reference_price_sources=["binance", "okx", "hyperliquid"],
            anchor_reference_to_hyperliquid=False,
            max_source_deviation_bps=100.0,
            min_reference_sources=2,
        )
        aggregator = ExternalPriceAggregator(config)
        reference = aggregator._select_reference(  # noqa: SLF001 - targeted unit coverage
            "BTC",
            [
                ReferencePriceQuote(source="binance", symbol="BTCUSDT", price=100.0),
                ReferencePriceQuote(source="okx", symbol="BTC-USDT", price=101.0),
                ReferencePriceQuote(source="hyperliquid", symbol="BTC", price=130.0),
            ],
        )

        self.assertIsNotNone(reference)
        self.assertEqual(reference.price, 100.5)
        self.assertEqual(reference.source_count, 2)
        self.assertEqual(reference.rejected_quotes[0].source, "hyperliquid")

    def test_reference_price_aggregator_reads_public_exchange_payloads(self) -> None:
        config = Hip4OutcomeConfig(
            reference_price_sources=["binance", "okx", "bybit", "coinbase", "kraken", "hyperliquid"],
            min_reference_sources=2,
        )
        aggregator = ExternalPriceAggregator(config)

        def fake_get_json(url: str):
            if "binance.com" in url:
                return {"symbol": "BTCUSDT", "price": "100.0"}
            if "okx.com" in url:
                return {"code": "0", "data": [{"instId": "BTC-USDT", "last": "101.0"}]}
            if "bybit.com" in url:
                return {
                    "retCode": 0,
                    "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100.25"}]},
                }
            if "coinbase.com" in url:
                return {"price": "100.75", "bid": "100.7", "ask": "100.8"}
            if "kraken.com" in url:
                return {
                    "error": [],
                    "result": {"XXBTZUSD": {"c": ["100.5", "0.1"]}},
                }
            raise AssertionError(url)

        aggregator._get_json = fake_get_json  # type: ignore[method-assign]  # noqa: SLF001
        reference = aggregator.fetch_one("BTC", hyperliquid_mids={"BTC": 100.5})

        self.assertIsNotNone(reference)
        self.assertEqual(reference.source_count, 6)
        self.assertEqual(reference.price, 100.5)

    def test_reference_price_aggregator_observes_and_rejects_external_divergence(self) -> None:
        config = Hip4OutcomeConfig(
            reference_price_sources=["binance", "okx", "hyperliquid"],
            anchor_reference_to_hyperliquid=True,
            max_source_deviation_bps=50.0,
            min_reference_sources=1,
        )
        aggregator = ExternalPriceAggregator(config)
        reference = aggregator._select_reference(  # noqa: SLF001 - targeted unit coverage
            "HYPE",
            [
                ReferencePriceQuote(source="binance", symbol="HYPEUSDT", price=23.0),
                ReferencePriceQuote(source="okx", symbol="HYPE-USDT", price=23.1),
                ReferencePriceQuote(source="hyperliquid", symbol="HYPE", price=58.5),
            ],
        )

        self.assertIsNotNone(reference)
        self.assertEqual(reference.price, 58.5)
        self.assertEqual(reference.source_count, 1)
        self.assertEqual([quote.source for quote in reference.rejected_quotes], ["binance", "okx"])

    def test_reference_price_sources_can_be_overridden_by_underlying(self) -> None:
        config = Hip4OutcomeConfig(
            reference_price_sources=["binance", "hyperliquid"],
            reference_price_sources_by_underlying={"HYPE": ["hyperliquid"]},
            min_reference_sources=1,
        )
        aggregator = ExternalPriceAggregator(config)

        def fake_get_json(url: str):
            raise AssertionError(f"External venue should not be queried for HYPE: {url}")

        aggregator._get_json = fake_get_json  # type: ignore[method-assign]  # noqa: SLF001
        reference = aggregator.fetch_one("HYPE", hyperliquid_mids={"HYPE": 26.1})

        self.assertIsNotNone(reference)
        self.assertEqual(reference.price, 26.1)
        self.assertEqual(reference.source_count, 1)
        self.assertEqual(reference.quotes[0].source, "hyperliquid")

    def test_loads_testnet_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "hip4.toml"
            path.write_text(
                """
[hyperliquid]
info_url = "https://api.hyperliquid-testnet.xyz/info"

[hip4_outcome]
mode = "paper"
include_underlyings = ["btc"]
allow_testnet_orders = false
enable_named_outcome_basket = true
named_outcome_basket_min_count = 3

[hip4_outcome.annualized_vol_by_underlying]
BTC = 0.5
""",
                encoding="utf-8",
            )

            config = load_hip4_outcome_config(path)

            self.assertEqual(config.mode, "paper")
            self.assertEqual(config.include_underlyings, ["BTC"])
            self.assertEqual(config.annualized_vol_by_underlying["BTC"], 0.5)
            self.assertTrue(config.write_pod_b_alias_status)
            self.assertEqual(config.pod_b_alias_status_path, "./logs/pod_b_live_status.json")
            self.assertEqual(config.outcome_open_fee_rate, 0.0)
            self.assertEqual(config.outcome_settlement_fee_rate, config.estimated_fees)
            self.assertEqual(config.blocked_opportunity_slices, [])
            self.assertFalse(config.block_reference_divergence)
            self.assertTrue(config.enable_price_bucket)
            self.assertTrue(config.enable_named_outcome_basket)
            self.assertEqual(config.named_outcome_basket_min_count, 3)
            self.assertTrue(config.enable_market_observation)

    def test_loads_outcome_guardrails_from_config_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "hip4.toml"
            path.write_text(
                """
[hip4_outcome]
blocked_opportunity_slices = ["hype/late_expiry/buy_yes", "BTC:MODEL:BUY_NO", "bad"]
block_reference_divergence = true
reference_divergence_max_bps = 250
reference_divergence_min_rejected_sources = 2
reference_divergence_underlyings = ["hype"]
reference_divergence_sides = ["buy_yes"]
reference_divergence_edge_types = ["model"]
""",
                encoding="utf-8",
            )

            config = load_hip4_outcome_config(path)

            self.assertEqual(
                config.blocked_opportunity_slices,
                ["HYPE:LATE_EXPIRY:BUY_YES", "BTC:MODEL:BUY_NO"],
            )
            self.assertTrue(config.block_reference_divergence)
            self.assertEqual(config.reference_divergence_max_bps, 250.0)
            self.assertEqual(config.reference_divergence_min_rejected_sources, 2)
            self.assertEqual(config.reference_divergence_underlyings, ["HYPE"])
            self.assertEqual(config.reference_divergence_sides, ["BUY_YES"])
            self.assertEqual(config.reference_divergence_edge_types, ["MODEL"])

            with patch.dict(
                os.environ,
                {
                    "HIP4_OUTCOME_BLOCKED_OPPORTUNITY_SLICES": "ETH:MODEL:BUY_YES",
                    "HIP4_OUTCOME_BLOCK_REFERENCE_DIVERGENCE": "false",
                    "HIP4_OUTCOME_REFERENCE_DIVERGENCE_MAX_BPS": "100",
                    "HIP4_OUTCOME_REFERENCE_DIVERGENCE_MIN_REJECTED_SOURCES": "3",
                    "HIP4_OUTCOME_REFERENCE_DIVERGENCE_UNDERLYINGS": "BTC,ETH",
                    "HIP4_OUTCOME_REFERENCE_DIVERGENCE_SIDES": "BUY_NO",
                    "HIP4_OUTCOME_REFERENCE_DIVERGENCE_EDGE_TYPES": "SHORT_EXPIRY",
                },
            ):
                overridden = load_hip4_outcome_config(path)

            self.assertEqual(
                overridden.blocked_opportunity_slices,
                ["ETH:MODEL:BUY_YES"],
            )
            self.assertFalse(overridden.block_reference_divergence)
            self.assertEqual(overridden.reference_divergence_max_bps, 100.0)
            self.assertEqual(overridden.reference_divergence_min_rejected_sources, 3)
            self.assertEqual(overridden.reference_divergence_underlyings, ["BTC", "ETH"])
            self.assertEqual(overridden.reference_divergence_sides, ["BUY_NO"])
            self.assertEqual(overridden.reference_divergence_edge_types, ["SHORT_EXPIRY"])

    def test_testnet_config_keeps_external_price_sources_visible(self) -> None:
        config = load_hip4_outcome_config("config/hip4_outcome_testnet.toml")

        self.assertIn("binance", config.reference_price_sources)
        self.assertIn("okx", config.reference_price_sources)
        self.assertIn("hyperliquid", config.reference_price_sources)
        self.assertTrue(config.anchor_reference_to_hyperliquid)
        self.assertNotIn("HYPE", config.reference_price_sources_by_underlying)
        self.assertFalse(config.enable_model)
        self.assertEqual(config.blocked_opportunity_slices, [])
        self.assertFalse(config.block_reference_divergence)
        self.assertEqual(config.reference_divergence_underlyings, [])
        self.assertEqual(config.reference_divergence_max_bps, 250.0)
        self.assertEqual(config.reference_divergence_min_rejected_sources, 2)
        self.assertTrue(config.enable_embedded_observers)
        self.assertIn(
            "config/hip4_outcome_mainnet_observer.toml",
            config.embedded_observer_config_paths,
        )

    def test_loads_embedded_observer_config_without_testnet_env_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HIP4_OUTCOME_MODE": "testnet",
                "HIP4_OUTCOME_ALLOW_TESTNET_ORDERS": "true",
                "HIP4_OUTCOME_WRITE_POD_B_ALIAS_STATUS": "true",
            },
            clear=False,
        ):
            config = load_hip4_outcome_config(
                "config/hip4_outcome_mainnet_observer.toml",
                apply_env=False,
            )

        self.assertEqual(config.mode, "observer")
        self.assertFalse(config.allow_testnet_orders)
        self.assertFalse(config.write_pod_b_alias_status)
        self.assertFalse(config.enable_embedded_observers)

    def test_testnet_executor_prefers_dedicated_hip4_credentials(self) -> None:
        env = {
            "TRIDENT_ACCOUNT_ADDRESS": "0x0000000000000000000000000000000000000001",
            "TRIDENT_SECRET_KEY": "0x" + "1" * 64,
            "TRIDENT_VAULT_ADDRESS": "0x0000000000000000000000000000000000000002",
            "HIP4_OUTCOME_ACCOUNT_ADDRESS": "0x0000000000000000000000000000000000000003",
            "HIP4_OUTCOME_SECRET_KEY": "0x" + "2" * 64,
            "HIP4_OUTCOME_VAULT_ADDRESS": "0x0000000000000000000000000000000000000004",
        }
        with patch.dict(os.environ, env, clear=False):
            executor = TestnetOutcomeExecutor(Hip4OutcomeConfig(mode="testnet"))

        self.assertEqual(
            executor.credentials.account_address,
            "0x0000000000000000000000000000000000000003",
        )
        self.assertEqual(executor.credentials.secret_key, "0x" + "2" * 64)
        self.assertEqual(
            executor.credentials.vault_address,
            "0x0000000000000000000000000000000000000004",
        )

    def test_writes_pod_b_alias_runtime_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Hip4OutcomeConfig(
                mode="paper",
                logs_dir=str(root / "logs"),
                state_path=str(root / "state.json"),
                status_path=str(root / "hip4_status.json"),
                pod_b_alias_status_path=str(root / "pod_b_live_status.json"),
                blocked_opportunity_slices=["HYPE:LATE_EXPIRY:BUY_YES"],
                block_reference_divergence=True,
                reference_divergence_max_bps=250.0,
                reference_divergence_min_rejected_sources=2,
                reference_divergence_underlyings=["HYPE"],
            )
            pod = HIP4OutcomeEdgePod(config)

            pod._write_status(  # noqa: SLF001 - verifies compatibility status output
                {
                    "loop_count": 1,
                    "mode": "paper",
                    "opportunities": 2,
                    "approved": 1,
                    "executed": 1,
                }
            )

            alias = json.loads((root / "pod_b_live_status.json").read_text(encoding="utf-8"))
            self.assertEqual(alias["pod"], "pod_b")
            self.assertEqual(alias["pod_kind"], "hip4_outcome_edge_pod")
            self.assertEqual(alias["process_state"], "running")
            self.assertEqual(alias["report"]["strategy"], "HIP4OutcomeEdgePod")
            self.assertEqual(alias["hip4_outcome_status_path"], str(root / "hip4_status.json"))
            self.assertEqual(alias["blocked_opportunity_slices"], ["HYPE:LATE_EXPIRY:BUY_YES"])
            self.assertEqual(
                alias["reference_divergence_guard"],
                {
                    "enabled": True,
                    "max_bps": 250.0,
                    "min_rejected_sources": 2,
                    "underlyings": ["HYPE"],
                    "sides": [],
                    "edge_types": [],
                },
            )

    def test_run_once_logs_observed_unsupported_markets(self) -> None:
        class FakeInfoClient:
            def fetch_all_mids(self):
                return {"BTC": 80500.0}

            def fetch_outcome_meta(self):
                return {
                    "outcomes": [
                        {
                            "outcome": 6290,
                            "name": "Recurring Named Outcome",
                            "description": "index:0",
                            "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                        },
                        {
                            "outcome": 7000,
                            "name": "Recurring",
                            "description": "class:priceBinary|underlying:BTC|expiry:20991231-0000|targetPrice:80000|period:1d",
                            "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                        },
                    ]
                }

            def fetch_l2_book(self, coin: str):
                return {
                    "coin": coin,
                    "time": 1,
                    "levels": [
                        [{"px": "0.40", "sz": "100", "n": 1}],
                        [{"px": "0.42", "sz": "100", "n": 1}],
                    ],
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Hip4OutcomeConfig(
                mode="observer",
                logs_dir=str(root / "logs"),
                state_path=str(root / "state.json"),
                status_path=str(root / "status.json"),
                write_pod_b_alias_status=False,
                reference_price_sources=["hyperliquid"],
                include_underlyings=[],
                max_time_to_expiry_minutes=100_000_000,
                min_yes_depth_usdc=1.0,
                min_no_depth_usdc=1.0,
                enable_embedded_observers=False,
            )
            pod = HIP4OutcomeEdgePod(config, info_client=FakeInfoClient())  # type: ignore[arg-type]

            summary = pod.run_once()

            observation_path = root / "logs" / "market_observations.jsonl"
            rows = [
                json.loads(line)
                for line in observation_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(summary["markets_seen"], 2)
            self.assertEqual(summary["markets_supported"], 1)
            self.assertEqual(summary["market_observation"]["named_outcome_count"], 1)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["class_name"], "namedOutcome")
            self.assertEqual(rows[0]["support_status"], "observe_only")
            self.assertIn("yes", rows[0]["books"])

    def test_run_once_executes_named_outcome_no_basket_in_paper(self) -> None:
        named_no_coins = {"#91021", "#91031", "#91041"}

        class FakeInfoClient:
            def fetch_all_mids(self):
                return {"BTC": 80000.0}

            def fetch_outcome_meta(self):
                return {
                    "outcomes": [
                        {
                            "outcome": 9100,
                            "name": "Recurring",
                            "description": (
                                "class:priceBinary|underlying:BTC|expiry:20991231-0000|"
                                "targetPrice:80000|period:1d"
                            ),
                            "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                        },
                        {
                            "outcome": 9101,
                            "name": "Recurring Fallback",
                            "description": "other",
                            "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                        },
                        {
                            "outcome": 9102,
                            "name": "Recurring Named Outcome",
                            "description": "index:0",
                            "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                        },
                        {
                            "outcome": 9103,
                            "name": "Recurring Named Outcome",
                            "description": "index:1",
                            "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                        },
                        {
                            "outcome": 9104,
                            "name": "Recurring Named Outcome",
                            "description": "index:2",
                            "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                        },
                    ]
                }

            def fetch_l2_book(self, coin: str):
                ask = "0.40" if coin in named_no_coins else "0.55"
                bid = "0.39" if coin in named_no_coins else "0.54"
                return {
                    "coin": coin,
                    "time": 1,
                    "levels": [
                        [{"px": bid, "sz": "100", "n": 1}],
                        [{"px": ask, "sz": "100", "n": 1}],
                    ],
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Hip4OutcomeConfig(
                mode="paper",
                logs_dir=str(root / "logs"),
                state_path=str(root / "state.json"),
                status_path=str(root / "status.json"),
                write_pod_b_alias_status=False,
                reference_price_sources=["hyperliquid"],
                include_underlyings=[],
                enable_late_expiry=False,
                enable_parity=False,
                enable_model=False,
                enable_short_expiry=False,
                enable_named_outcome_basket=True,
                enable_market_observation=False,
                max_time_to_expiry_minutes=100_000_000,
                max_position_usdc=12.0,
                max_total_outcome_exposure_usdc=100.0,
                max_per_underlying_outcome_exposure_usdc=100.0,
                min_yes_depth_usdc=1.0,
                min_no_depth_usdc=1.0,
                min_order_value_usdc=0.0,
                min_gross_edge=0.01,
                min_net_edge=0.001,
            )
            pod = HIP4OutcomeEdgePod(config, info_client=FakeInfoClient())  # type: ignore[arg-type]

            summary = pod.run_once()

            self.assertEqual(summary["named_outcome_baskets"], 1)
            self.assertEqual(summary["executed"], 1)
            self.assertEqual(summary["opportunity_mix"]["NAMED_OUTCOME_NO_BASKET"], 1)
            self.assertEqual(summary["named_outcome_basket_watchlist"][0]["readiness"], "ready")
            self.assertEqual(len(pod.positions), 1)
            self.assertEqual(pod.positions[0].edge_type, "NAMED_OUTCOME_NO_BASKET")
            self.assertEqual(len(pod.positions[0].fills), 3)
            self.assertEqual({fill.side_name for fill in pod.positions[0].fills}, {"NO"})

    def test_named_outcome_no_basket_settles_conservatively_in_paper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Hip4OutcomeConfig(
                mode="paper",
                logs_dir=str(root / "logs"),
                state_path=str(root / "state.json"),
                status_path=str(root / "status.json"),
                write_pod_b_alias_status=False,
                settlement_grace_seconds=0,
            )
            pod = HIP4OutcomeEdgePod(config)
            position = OutcomePosition(
                position_id="named-basket-1",
                market_id="BTC_GT_80000_20991231_0000:NAMED_NO_BASKET:1-2-3",
                outcome=9100,
                underlying="BTC",
                edge_type="NAMED_OUTCOME_NO_BASKET",
                side="BUY_NAMED_NO_BASKET",
                opened_at="2026-05-16T00:00:00Z",
                expiry_ts=1,
                cost_usdc=12.0,
                max_loss_usdc=12.0,
                net_edge=0.5,
                confidence=0.9,
                fills=[
                    OutcomeFill("#11", "NO", Decimal("10"), 0.4, 4.0, "paper_filled"),
                    OutcomeFill("#21", "NO", Decimal("10"), 0.4, 4.0, "paper_filled"),
                    OutcomeFill("#31", "NO", Decimal("10"), 0.4, 4.0, "paper_filled"),
                ],
                metadata={"decision": {"execution_mode": "PAPER"}},
            )
            pod.positions = [position]

            pod._settle_expired_positions(now_ts=2, reference_prices={})  # noqa: SLF001

            self.assertEqual(position.status, "estimated_settled")
            self.assertEqual(position.estimated_payout_usdc, 20.0)
            self.assertEqual(position.estimated_gross_pnl_usdc, 8.0)
            self.assertEqual(position.estimated_fee_usdc, 0.04)
            self.assertEqual(position.estimated_pnl_usdc, 7.96)
            self.assertEqual(
                position.metadata["settlement"]["notes"],
                "conservative_named_outcome_no_basket",
            )

    def test_run_once_executes_embedded_observer_in_same_process(self) -> None:
        class FakeInfoClient:
            def fetch_all_mids(self):
                return {}

            def fetch_outcome_meta(self):
                return {"outcomes": []}

        class FakeObserverPod:
            def __init__(self) -> None:
                self.config = type(
                    "Config",
                    (),
                    {
                        "status_path": "logs/sidecar_status.json",
                        "logs_dir": "logs/sidecar",
                        "mode": "observer",
                        "loop_interval_seconds": 0.01,
                    },
                )()
                self.last_error = None

            def run_once(self):
                return {"markets_seen": 1, "markets_supported": 1}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Hip4OutcomeConfig(
                mode="observer",
                logs_dir=str(root / "logs"),
                state_path=str(root / "state.json"),
                status_path=str(root / "status.json"),
                write_pod_b_alias_status=False,
                enable_embedded_observers=True,
                embedded_observer_config_paths=["config/hip4_outcome_mainnet_observer.toml"],
                embedded_observer_once_timeout_seconds=5,
            )
            pod = HIP4OutcomeEdgePod(config, info_client=FakeInfoClient())  # type: ignore[arg-type]
            with patch.object(
                HIP4OutcomeEdgePod,
                "_build_embedded_observer",
                return_value=FakeObserverPod(),
            ):
                pod.run_once()

            embedded = pod._embedded_observer_status()  # noqa: SLF001 - verifies in-process sidecar
            self.assertIn("config/hip4_outcome_mainnet_observer.toml", embedded["observers"])
            self.assertEqual(
                embedded["observers"]["config/hip4_outcome_mainnet_observer.toml"]["summary"]["markets_seen"],
                1,
            )

    def test_capital_guard_caps_paper_size_by_pod_b_budget(self) -> None:
        config = Hip4OutcomeConfig(mode="paper", pod_b_budget_usdc=10.0)
        guard = OutcomeCapitalGuard(config, info_client=None)  # type: ignore[arg-type]
        open_position = OutcomePosition(
            position_id="pos-1",
            market_id="BTC_GT_1",
            outcome=1,
            underlying="BTC",
            edge_type="MODEL",
            side="BUY_YES",
            opened_at="2026-05-01T00:00:00Z",
            expiry_ts=1,
            cost_usdc=6.0,
            max_loss_usdc=6.0,
            net_edge=0.1,
            confidence=0.9,
        )
        decision = SupervisorDecision(
            approved=True,
            approved_size_usdc=8.0,
            reason="local_outcome_risk_ok",
            execution_mode="PAPER",
        )

        adjusted, snapshot = guard.apply(
            decision=decision,
            open_positions=[open_position],
        )

        self.assertTrue(adjusted.approved)
        self.assertEqual(adjusted.approved_size_usdc, 4.0)
        self.assertEqual(snapshot.remaining_budget_usdc, 4.0)
        self.assertEqual(adjusted.constraints["capital"]["budget_usdc"], 10.0)

    def test_capital_guard_caps_testnet_size_by_available_usdc(self) -> None:
        class FakeInfoClient:
            def fetch_spot_state(self, user: str):
                self.user = user
                return {"balances": [{"coin": "USDC", "total": "2", "hold": "0"}]}

        class FakeExecutor:
            def resolve_account_address(self):
                return "0x0000000000000000000000000000000000000001"

        config = Hip4OutcomeConfig(
            mode="testnet",
            pod_b_budget_usdc=25.0,
            testnet_balance_buffer_usdc=1.0,
            testnet_balance_coin="USDC",
        )
        guard = OutcomeCapitalGuard(config, info_client=FakeInfoClient())  # type: ignore[arg-type]
        decision = SupervisorDecision(
            approved=True,
            approved_size_usdc=5.0,
            reason="local_outcome_risk_ok",
            execution_mode="TESTNET",
        )

        adjusted, snapshot = guard.apply(
            decision=decision,
            open_positions=[],
            testnet_executor=FakeExecutor(),
        )

        self.assertTrue(adjusted.approved)
        self.assertEqual(adjusted.approved_size_usdc, 1.0)
        self.assertEqual(snapshot.testnet_available_usdc, 2.0)
        self.assertEqual(snapshot.testnet_balance_source, "spotClearinghouseState")

    def test_capital_guard_defaults_to_usdh_for_outcome_quote_balance(self) -> None:
        class FakeInfoClient:
            def fetch_spot_state(self, user: str):
                return {
                    "balances": [
                        {"coin": "USDC", "total": "50", "hold": "0"},
                        {"coin": "USDH", "total": "12", "hold": "2"},
                    ]
                }

        class FakeExecutor:
            def resolve_account_address(self):
                return "0x0000000000000000000000000000000000000001"

        guard = OutcomeCapitalGuard(
            Hip4OutcomeConfig(mode="testnet", testnet_balance_buffer_usdc=1.0),
            info_client=FakeInfoClient(),  # type: ignore[arg-type]
        )
        decision = SupervisorDecision(
            approved=True,
            approved_size_usdc=20.0,
            reason="local_outcome_risk_ok",
            execution_mode="TESTNET",
        )

        adjusted, snapshot = guard.apply(
            decision=decision,
            open_positions=[],
            testnet_executor=FakeExecutor(),
        )

        self.assertTrue(adjusted.approved)
        self.assertEqual(snapshot.testnet_balance_coin, "USDH")
        self.assertEqual(snapshot.testnet_available_usdc, 10.0)
        self.assertEqual(adjusted.approved_size_usdc, 9.0)

    def test_capital_guard_transfers_testnet_withdrawable_to_spot_when_empty(self) -> None:
        class FakeInfoClient:
            def __init__(self) -> None:
                self.spot_calls = 0

            def fetch_spot_state(self, user: str):
                self.spot_user = user
                self.spot_calls += 1
                if self.spot_calls == 1:
                    return {"balances": []}
                return {"balances": [{"coin": "USDC", "total": "26", "hold": "0"}]}

            def fetch_clearinghouse_state(self, user: str):
                self.clearinghouse_user = user
                return {"withdrawable": "999.0", "marginSummary": {"accountValue": "999.0"}}

        class FakeExecutor:
            transfer_amount = None

            def resolve_account_address(self):
                return "0x0000000000000000000000000000000000000001"

            def transfer_usd_to_spot(self, amount_usdc: float):
                self.transfer_amount = amount_usdc
                return {"status": "ok", "response": {"type": "default"}}

        config = Hip4OutcomeConfig(
            mode="testnet",
            pod_b_budget_usdc=25.0,
            testnet_balance_buffer_usdc=1.0,
            testnet_balance_coin="USDC",
            auto_transfer_testnet_spot_usdc=True,
        )
        fake_executor = FakeExecutor()
        guard = OutcomeCapitalGuard(config, info_client=FakeInfoClient())  # type: ignore[arg-type]
        decision = SupervisorDecision(
            approved=True,
            approved_size_usdc=5.0,
            reason="local_outcome_risk_ok",
            execution_mode="TESTNET",
        )

        adjusted, snapshot = guard.apply(
            decision=decision,
            open_positions=[],
            testnet_executor=fake_executor,
        )

        self.assertTrue(adjusted.approved)
        self.assertEqual(adjusted.approved_size_usdc, 5.0)
        self.assertEqual(snapshot.testnet_available_usdc, 26.0)
        self.assertEqual(snapshot.testnet_perp_withdrawable_usdc, 999.0)
        self.assertEqual(snapshot.testnet_balance_source, "spotClearinghouseState_after_usdClassTransfer")
        self.assertEqual(snapshot.testnet_spot_transfer_usdc, 26.0)
        self.assertEqual(fake_executor.transfer_amount, 26.0)

    def test_capital_guard_does_not_use_perp_withdrawable_without_spot_transfer(self) -> None:
        class FakeInfoClient:
            def fetch_spot_state(self, user: str):
                return {"balances": []}

            def fetch_clearinghouse_state(self, user: str):
                return {"withdrawable": "999.0"}

        class FakeExecutor:
            def resolve_account_address(self):
                return "0x0000000000000000000000000000000000000001"

        guard = OutcomeCapitalGuard(
            Hip4OutcomeConfig(
                mode="testnet",
                testnet_balance_coin="USDC",
                auto_transfer_testnet_spot_usdc=False,
            ),
            info_client=FakeInfoClient(),  # type: ignore[arg-type]
        )
        decision = SupervisorDecision(
            approved=True,
            approved_size_usdc=5.0,
            reason="local_outcome_risk_ok",
            execution_mode="TESTNET",
        )

        adjusted, snapshot = guard.apply(
            decision=decision,
            open_positions=[],
            testnet_executor=FakeExecutor(),
        )

        self.assertFalse(adjusted.approved)
        self.assertEqual(adjusted.reason, "insufficient_testnet_quote_balance")
        self.assertEqual(snapshot.testnet_available_usdc, 0.0)
        self.assertEqual(snapshot.testnet_perp_withdrawable_usdc, 999.0)
        self.assertEqual(snapshot.testnet_balance_source, "spotClearinghouseState")
        self.assertEqual(snapshot.testnet_spot_transfer_status, "manual_usd_class_transfer_required")

    def test_capital_guard_can_refresh_testnet_balance_without_decision(self) -> None:
        class FakeInfoClient:
            def __init__(self) -> None:
                self.spot_calls = 0

            def fetch_spot_state(self, user: str):
                self.spot_calls += 1
                if self.spot_calls == 1:
                    return {"balances": []}
                return {"balances": [{"coin": "USDC", "total": "26", "hold": "0"}]}

            def fetch_clearinghouse_state(self, user: str):
                return {"withdrawable": "999.0"}

        class FakeExecutor:
            def resolve_account_address(self):
                return "0x0000000000000000000000000000000000000001"

            def transfer_usd_to_spot(self, amount_usdc: float):
                return {"status": "ok", "response": {"type": "default"}}

        snapshot = OutcomeCapitalGuard(
            Hip4OutcomeConfig(
                mode="testnet",
                testnet_balance_coin="USDC",
                auto_transfer_testnet_spot_usdc=True,
            ),
            info_client=FakeInfoClient(),  # type: ignore[arg-type]
        ).testnet_balance_snapshot(
            open_positions=[],
            testnet_executor=FakeExecutor(),
        )

        self.assertEqual(snapshot.testnet_available_usdc, 26.0)
        self.assertEqual(snapshot.testnet_balance_source, "spotClearinghouseState_after_usdClassTransfer")

    def test_reconciler_matches_testnet_fills_and_balances(self) -> None:
        class FakeInfoClient:
            def fetch_spot_state(self, user: str):
                self.user = user
                return {"balances": [{"coin": "#57210", "total": "3", "hold": "0"}]}

            def fetch_user_fills_by_time(
                self,
                *,
                user: str,
                start_time_ms: int,
                end_time_ms: int | None = None,
                aggregate_by_time: bool = False,
            ):
                return [
                    {
                        "coin": "#57210",
                        "oid": 42,
                        "cloid": "0xabc",
                        "px": "0.25",
                        "sz": "3",
                        "time": start_time_ms + 1,
                    }
                ]

        position = OutcomePosition(
            position_id="pos-1",
            market_id="BTC_GT_76775_20260502_0300",
            outcome=5721,
            underlying="BTC",
            edge_type="MODEL",
            side="BUY_YES",
            opened_at="2026-05-01T00:00:00Z",
            expiry_ts=1777681200,
            cost_usdc=0.75,
            max_loss_usdc=0.75,
            net_edge=0.1,
            confidence=0.8,
            fills=[
                OutcomeFill(
                    coin="#57210",
                    side_name="YES",
                    token_qty=Decimal("3"),
                    avg_price=0.25,
                    cost_usdc=0.75,
                    status="filled",
                    oid=42,
                    cloid="0xabc",
                )
            ],
            metadata={"decision": {"execution_mode": "TESTNET"}},
        )

        report = OutcomeReconciler(
            Hip4OutcomeConfig(mode="testnet"),
            FakeInfoClient(),  # type: ignore[arg-type]
        ).reconcile(
            account_address="0x123",
            positions=[position],
            start_time_ms=1,
            end_time_ms=2,
        )

        self.assertEqual(report["matched_fill_count"], 1)
        self.assertEqual(report["tracked_balances"]["#57210"]["total"], "3")
        self.assertTrue(apply_reconciliation_to_positions([position], report))
        self.assertTrue(position.metadata["last_reconciliation"]["exchange_confirmed"])

    def test_reconciliation_overrides_testnet_estimated_settlement_with_exchange_closed_pnl(self) -> None:
        class FakeInfoClient:
            def fetch_spot_state(self, user: str):
                return {"balances": [{"coin": "USDH", "total": "285.5", "hold": "0"}]}

            def fetch_user_fills_by_time(
                self,
                *,
                user: str,
                start_time_ms: int,
                end_time_ms: int | None = None,
                aggregate_by_time: bool = False,
            ):
                return [
                    {
                        "coin": "#59450",
                        "oid": 52412557964,
                        "cloid": "0xabc",
                        "px": "0.5",
                        "sz": "97",
                        "side": "B",
                        "dir": "Buy",
                        "fee": "0",
                        "closedPnl": "0.0",
                        "time": start_time_ms + 1,
                    },
                    {
                        "coin": "#59450",
                        "oid": 52412782934,
                        "px": "0.0",
                        "sz": "97",
                        "side": "A",
                        "dir": "Settlement",
                        "fee": "0",
                        "closedPnl": "-48.5",
                        "time": start_time_ms + 2,
                    },
                ]

        position = OutcomePosition(
            position_id="pos-1",
            market_id="HYPE_GT_55.983_20260503_1030",
            outcome=5945,
            underlying="HYPE",
            edge_type="LATE_EXPIRY",
            side="BUY_YES",
            opened_at="2026-05-03T10:22:29.457348Z",
            expiry_ts=1777804200,
            cost_usdc=48.5,
            max_loss_usdc=48.5,
            net_edge=0.1,
            confidence=0.8,
            fills=[
                OutcomeFill(
                    coin="#59450",
                    side_name="YES",
                    token_qty=Decimal("97"),
                    avg_price=0.5,
                    cost_usdc=48.5,
                    status="filled",
                    oid=52412557964,
                    cloid="0xabc",
                )
            ],
            status="estimated_settled",
            estimated_payout_usdc=97.0,
            estimated_gross_pnl_usdc=48.5,
            estimated_fee_usdc=0.194,
            estimated_pnl_usdc=48.306,
            metadata={
                "decision": {"execution_mode": "TESTNET"},
                "settlement": {"source": "estimated_from_reference_price", "result": "YES"},
            },
        )

        report = OutcomeReconciler(
            Hip4OutcomeConfig(mode="testnet"),
            FakeInfoClient(),  # type: ignore[arg-type]
        ).reconcile(
            account_address="0x123",
            positions=[position],
            start_time_ms=1777803749000,
            end_time_ms=1777804210000,
        )

        self.assertTrue(apply_reconciliation_to_positions([position], report))
        self.assertEqual(position.status, "settled")
        self.assertEqual(position.estimated_payout_usdc, 0.0)
        self.assertEqual(position.estimated_gross_pnl_usdc, -48.5)
        self.assertEqual(position.estimated_fee_usdc, 0.0)
        self.assertEqual(position.estimated_pnl_usdc, -48.5)
        self.assertEqual(position.metadata["settlement"]["source"], "hyperliquid_user_fills")
        self.assertEqual(position.metadata["settlement"]["result"], "NO")
        self.assertTrue(position.metadata["last_reconciliation"]["exchange_settled"])

    def test_parses_reconciliation_payload_variants(self) -> None:
        balances = parse_spot_balances(
            {"spotBalances": [{"token": "+57210", "balance": "2", "hold": "0.5"}]}
        )
        fills = parse_user_fills(
            {
                "fills": [
                    {
                        "coin": "+57210",
                        "orderId": "7",
                        "price": "0.4",
                        "size": "2",
                        "dir": "Settlement",
                        "closedPnl": "-1.2",
                    }
                ]
            }
        )

        self.assertEqual(balances["+57210"].available, Decimal("1.5"))
        self.assertEqual(fills[0]["oid"], "7")
        self.assertEqual(fills[0]["px"], 0.4)
        self.assertEqual(fills[0]["dir"], "Settlement")
        self.assertEqual(fills[0]["closed_pnl"], "-1.2")

    def test_builds_daily_summary_rows(self) -> None:
        position = OutcomePosition(
            position_id="pos-1",
            market_id="BTC_GT_76775_20260502_0300",
            outcome=5721,
            underlying="BTC",
            edge_type="MODEL",
            side="BUY_YES",
            opened_at="2026-05-01T00:00:00Z",
            expiry_ts=1777681200,
            cost_usdc=1.0,
            max_loss_usdc=1.0,
            net_edge=0.1,
            confidence=0.8,
            status="estimated_settled",
            estimated_payout_usdc=1.5,
            estimated_fee_usdc=0.003,
            estimated_gross_pnl_usdc=0.5,
            estimated_pnl_usdc=0.497,
            metadata={"decision": {"execution_mode": "PAPER"}},
        )

        rows = build_daily_summary_rows([position])

        self.assertEqual(rows[0]["date"], "2026-05-01")
        self.assertEqual(rows[0]["mode"], "PAPER")
        self.assertEqual(rows[0]["estimated_fee_usdc"], 0.003)
        self.assertEqual(rows[0]["estimated_gross_pnl_usdc"], 0.5)
        self.assertEqual(rows[0]["estimated_pnl_usdc"], 0.497)

    def test_paper_settlement_charges_hip4_fee_only_on_payout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Hip4OutcomeConfig(
                mode="paper",
                logs_dir=str(root / "logs"),
                state_path=str(root / "state.json"),
                status_path=str(root / "hip4_status.json"),
                outcome_open_fee_rate=0.0,
                outcome_settlement_fee_rate=0.002,
                settlement_grace_seconds=0,
            )
            position = OutcomePosition(
                position_id="pos-1",
                market_id="BTC_GT_100_20260501_0000",
                outcome=1,
                underlying="BTC",
                edge_type="LATE_EXPIRY",
                side="BUY_YES",
                opened_at="2026-05-01T00:00:00Z",
                expiry_ts=1,
                cost_usdc=4.8,
                max_loss_usdc=4.8,
                net_edge=0.1,
                confidence=0.9,
                fills=[
                    OutcomeFill(
                        coin="#10",
                        side_name="YES",
                        token_qty=Decimal("10"),
                        avg_price=0.48,
                        cost_usdc=4.8,
                        status="paper_filled",
                    )
                ],
                metadata={
                    "decision": {"execution_mode": "PAPER"},
                    "signal": {"metadata": {"strike": 100.0}},
                },
            )
            pod = HIP4OutcomeEdgePod(config)
            pod.positions = [position]

            reference = type("Reference", (), {"price": 101.0})()
            pod._settle_expired_positions(  # noqa: SLF001 - targeted settlement accounting coverage
                now_ts=2,
                reference_prices={"BTC": reference},
            )

            self.assertEqual(position.estimated_payout_usdc, 10.0)
            self.assertEqual(position.estimated_gross_pnl_usdc, 5.2)
            self.assertEqual(position.estimated_fee_usdc, 0.02)
            self.assertEqual(position.estimated_pnl_usdc, 5.18)
            self.assertEqual(position.metadata["settlement"]["fee_model"]["open_fee_rate"], 0.0)

            settlement_log = (root / "logs" / "settlements.csv").read_text(encoding="utf-8")
            self.assertIn("fee_usdc", settlement_log)
            self.assertIn("gross_pnl_usdc", settlement_log)
            self.assertIn("net_pnl_usdc", settlement_log)
            self.assertIn(",0.02,5.2,5.18,5.18,", settlement_log)

    def test_testnet_does_not_settle_from_local_reference_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Hip4OutcomeConfig(
                mode="testnet",
                logs_dir=str(root / "logs"),
                state_path=str(root / "state.json"),
                status_path=str(root / "hip4_status.json"),
                settlement_grace_seconds=0,
            )
            position = OutcomePosition(
                position_id="pos-1",
                market_id="BTC_GT_100_20260501_0000",
                outcome=1,
                underlying="BTC",
                edge_type="LATE_EXPIRY",
                side="BUY_YES",
                opened_at="2026-05-01T00:00:00Z",
                expiry_ts=1,
                cost_usdc=4.8,
                max_loss_usdc=4.8,
                net_edge=0.1,
                confidence=0.9,
                fills=[
                    OutcomeFill(
                        coin="#10",
                        side_name="YES",
                        token_qty=Decimal("10"),
                        avg_price=0.48,
                        cost_usdc=4.8,
                        status="filled",
                    )
                ],
                metadata={
                    "decision": {"execution_mode": "TESTNET"},
                    "signal": {"metadata": {"strike": 100.0}},
                },
            )
            pod = HIP4OutcomeEdgePod(config)
            pod.positions = [position]

            reference = type("Reference", (), {"price": 101.0})()
            pod._settle_expired_positions(  # noqa: SLF001 - testnet must wait for HL settlement
                now_ts=2,
                reference_prices={"BTC": reference},
            )

            self.assertEqual(position.status, "open")
            self.assertEqual(position.estimated_pnl_usdc, 0.0)

    def test_execution_result_to_dict_is_json_safe(self) -> None:
        result = OutcomeExecutionResult(
            status="testnet_no_fill",
            fills=[
                OutcomeFill(
                    coin="#57210",
                    side_name="YES",
                    token_qty=Decimal("0"),
                    avg_price=0.0,
                    cost_usdc=0.0,
                    status="Order must have minimum value of 10 USDH",
                    raw={"nested": {"qty": Decimal("1.2")}},
                )
            ],
            raw=[{"status": "ok", "qty": Decimal("1.2")}],
        )

        payload = result.to_dict()

        self.assertFalse(payload["filled"])
        self.assertEqual(payload["fills"][0]["raw"]["nested"]["qty"], "1.2")
        self.assertEqual(payload["raw"][0]["qty"], "1.2")
        json.dumps(payload)

    def test_state_store_round_trips_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OutcomeStateStore(Path(tmpdir) / "state.json")
            position = OutcomePosition(
                position_id="pos-1",
                market_id="HYPE_GT_58.5_20260503_0800",
                outcome=5935,
                underlying="HYPE",
                edge_type="LATE_EXPIRY",
                side="BUY_YES",
                opened_at="2026-05-03T07:50:44Z",
                expiry_ts=1777795200,
                cost_usdc=26.98,
                max_loss_usdc=26.98,
                net_edge=0.273,
                confidence=0.6925,
                fills=[
                    OutcomeFill(
                        coin="#59350",
                        side_name="YES",
                        token_qty=Decimal("38.0"),
                        avg_price=0.71,
                        cost_usdc=26.98,
                        status="filled",
                        oid=52407686267,
                        cloid="0xabc",
                        raw={"qty": Decimal("38")},
                    )
                ],
                status="estimated_settled",
                estimated_pnl_usdc=10.944,
                metadata={"settlement": {"result": "YES"}},
            )

            store.save_positions([position])
            loaded = store.load_positions()

            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].position_id, "pos-1")
            self.assertEqual(loaded[0].fills[0].token_qty, Decimal("38.0"))
            self.assertEqual(loaded[0].metadata["settlement"]["result"], "YES")

    def test_runner_persists_execution_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = Hip4OutcomeConfig(
                mode="testnet",
                logs_dir=str(root / "logs"),
                state_path=str(root / "state.json"),
                status_path=str(root / "hip4_status.json"),
            )
            pod = HIP4OutcomeEdgePod(config)
            market = self._market()
            opportunity = OutcomeOpportunity(
                market_id=market.market_id,
                outcome=market.outcome,
                underlying=market.underlying,
                edge_type="MODEL",
                side="BUY_YES",
                gross_edge=0.1,
                estimated_fees=0.002,
                estimated_slippage=0.005,
                net_edge=0.08,
                confidence=0.9,
                requested_size_usdc=12.0,
                max_loss_usdc=12.0,
                expiry_ts=market.expiry_ts,
                reason="test_signal",
            )
            decision = SupervisorDecision(
                approved=True,
                approved_size_usdc=12.0,
                reason="local_outcome_risk_ok",
                execution_mode="TESTNET",
            )
            result = OutcomeExecutionResult(
                status="testnet_no_fill",
                fills=[
                    OutcomeFill(
                        coin=market.yes_coin,
                        side_name="YES",
                        token_qty=Decimal("0"),
                        avg_price=0.0,
                        cost_usdc=0.0,
                        status="Order must have minimum value of 10 USDH",
                    )
                ],
            )

            pod._record_execution_result(  # noqa: SLF001 - verifies the persisted execution audit trail
                market=market,
                opportunity=opportunity,
                decision=decision,
                result=result,
            )

            path = root / "logs" / "execution_results.jsonl"
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "testnet_no_fill")
            self.assertEqual(payload["fills"][0]["status"], "Order must have minimum value of 10 USDH")
            self.assertEqual(pod.last_execution_results[0]["market_id"], market.market_id)

    def test_replays_opportunity_log_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "opportunities.csv"
            path.write_text(
                "\n".join(
                    [
                        "ts,underlying,edge_type,side,gross_edge,net_edge,confidence",
                        "2026-05-01T00:00:00Z,BTC,MODEL,BUY_YES,0.10,0.08,0.7",
                        "2026-05-01T00:00:01Z,BTC,MODEL,BUY_YES,0.20,0.18,0.9",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            rows = replay_opportunities(path)

        self.assertEqual(rows[0]["opportunity_count"], 2)
        self.assertEqual(rows[0]["avg_net_edge"], 0.13)
        self.assertEqual(rows[0]["max_net_edge"], 0.18)

    def test_mainnet_observer_config_is_observer_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_hip4_outcome_config("config/hip4_outcome_mainnet_observer.toml")

        self.assertEqual(config.mode, "observer")
        self.assertEqual(config.info_url, "https://api.hyperliquid.xyz/info")
        self.assertEqual(config.ws_url, "wss://api.hyperliquid.xyz/ws")
        self.assertFalse(config.allow_testnet_orders)
        self.assertFalse(config.require_testnet_url)
        self.assertFalse(config.write_pod_b_alias_status)
        self.assertFalse(config.enforce_testnet_balance_check)
        self.assertIn("hip4_outcome_mainnet", config.logs_dir)
        self.assertIn("hip4_outcome_mainnet", config.status_path)

    def test_mainnet_paper_config_is_paper_dry_run(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_hip4_outcome_config("config/hip4_outcome_mainnet_paper.toml")

        self.assertEqual(config.mode, "paper")
        self.assertEqual(config.info_url, "https://api.hyperliquid.xyz/info")
        self.assertEqual(config.ws_url, "wss://api.hyperliquid.xyz/ws")
        self.assertFalse(config.allow_testnet_orders)
        self.assertFalse(config.require_testnet_url)
        self.assertTrue(config.write_pod_b_alias_status)
        self.assertFalse(config.enforce_testnet_balance_check)
        self.assertEqual(config.include_underlyings, [])
        self.assertTrue(config.enable_named_outcome_basket)
        self.assertIn("hip4_outcome_mainnet_paper", config.logs_dir)
        self.assertIn("hip4_outcome_mainnet_paper", config.state_path)
        self.assertIn("hip4_outcome_mainnet_paper", config.rate_limit_state_path)


if __name__ == "__main__":
    unittest.main()
