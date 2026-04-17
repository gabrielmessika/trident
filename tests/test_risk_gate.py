import unittest
from dataclasses import replace

from app.risk.pod_a_gate import PodARiskGate
from app.settings import load_config
from app.trident.types import TradePlan


class PodARiskGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")
        self.gate = PodARiskGate(self.config)

    def test_accepts_valid_trade_plan(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="ETH",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.62,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=150.0,
                    effective_leverage=3.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                )
            ]
        )

        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "accepted")

    def test_rejects_low_confidence_trade_plan(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="ETH",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.49,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=150.0,
                    effective_leverage=3.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "confidence_below_min")

    def test_rejects_trade_plan_when_risk_budget_is_exceeded(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="ETH",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.62,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=150.0,
                    effective_leverage=3.0,
                    risk_budget_usd=2.0,
                    expected_loss_usd=3.6,
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "risk_budget_exceeded")

    def test_rejects_trade_plan_when_asset_leverage_limit_is_exceeded(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                max_leverage=10.0,
                max_leverage_by_symbol={"ETH": 5.0},
            ),
        )
        gate = PodARiskGate(config)

        decisions = gate.evaluate_many(
            [
                TradePlan(
                    symbol="ETH",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.62,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=75.0,
                    effective_leverage=6.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "leverage_above_asset_limit")

    def test_rejects_disabled_setup(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="ETH",
                    side="short",
                    setup="bos_retest_short",
                    confidence=0.82,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=150.0,
                    effective_leverage=3.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                    setup_details={"regime": "TrendExpansion"},
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "setup_disabled")

    def test_rejects_non_whitelisted_setup_in_blocked_regime(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="ETH",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.82,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=150.0,
                    effective_leverage=3.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                    setup_details={"regime": "DeadZone"},
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "regime_filtered")

    def test_accepts_whitelisted_setup_in_blocked_regime(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="ETH",
                    side="long",
                    setup="liquidity_sweep_reclaim_long",
                    confidence=0.82,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=150.0,
                    effective_leverage=3.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                    setup_details={"regime": "DeadZone"},
                )
            ]
        )

        self.assertTrue(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "accepted")

    def test_rejects_vwap_reclaim_long_from_default_config(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="ETH",
                    side="long",
                    setup="vwap_reclaim_long",
                    confidence=0.82,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=150.0,
                    effective_leverage=3.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                    setup_details={"regime": "TrendExpansion"},
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "setup_disabled")
