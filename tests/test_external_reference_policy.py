from __future__ import annotations

import unittest

from app.backtest.external_reference_policy import (
    ExternalReferenceDecisionPolicy,
    ExternalReferencePolicyConfig,
)
from app.trident.types import PodName, RiskDecision, TradePlan


def _plan(**details: object) -> TradePlan:
    return TradePlan(
        symbol="BTC",
        side="long",
        setup="trend_pullback_long",
        confidence=0.7,
        target_notional_usd=100.0,
        stop_bps=30.0,
        time_stop_hours=4,
        setup_details={
            "external_reference_source_count": 1.0,
            "external_reference_age_seconds": 20.0,
            "external_reference_max_deviation_bps": 0.0,
            "external_premium_bps": 0.0,
            "external_momentum_60s_bps": 0.0,
            "external_momentum_300s_bps": 0.0,
            **details,
        },
    )


class ExternalReferencePolicyTests(unittest.TestCase):
    def test_counter_momentum_vetoes_accepted_plan(self) -> None:
        policy = ExternalReferenceDecisionPolicy(ExternalReferencePolicyConfig())
        decisions = [
            RiskDecision(
                accepted=True,
                reason="accepted",
                trade_plan=_plan(external_momentum_60s_bps=-8.0),
            )
        ]

        filtered = policy.apply_decisions(PodName.POD_A, decisions)

        self.assertFalse(filtered[0].accepted)
        self.assertEqual(filtered[0].reason, "external_reference_counter_momentum_60s")

    def test_missing_reference_passes_by_default(self) -> None:
        policy = ExternalReferenceDecisionPolicy(ExternalReferencePolicyConfig())
        decision = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=_plan(external_reference_source_count=0.0),
        )

        filtered = policy.apply_decisions(PodName.POD_C, [decision])

        self.assertTrue(filtered[0].accepted)
        self.assertEqual(filtered[0].reason, "accepted")

    def test_confidence_adjustment_boosts_aligned_plan(self) -> None:
        policy = ExternalReferenceDecisionPolicy(
            ExternalReferencePolicyConfig(confidence_adjustment_enabled=True)
        )
        plan = _plan(
            external_momentum_60s_bps=14.0,
            external_momentum_300s_bps=18.0,
        )

        policy.adjust_plans(PodName.POD_A, [plan])

        self.assertGreater(plan.confidence, 0.7)
        self.assertIn("external_reference_adjustment", plan.confidence_components)


if __name__ == "__main__":
    unittest.main()
