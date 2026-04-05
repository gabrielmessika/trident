import unittest

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
                    target_notional_usd=150.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
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
                    target_notional_usd=150.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "confidence_below_min")
