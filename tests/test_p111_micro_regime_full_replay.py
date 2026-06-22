from __future__ import annotations

import unittest

from app.trident.types import PodName, RiskDecision, SymbolMarketSnapshot, TradePlan
from scripts.run_p111_micro_regime_full_replay import (
    apply_micro_regime_policy_to_plans,
    combine_policy_and_gate_decisions,
    default_profiles,
)


class P111MicroRegimeFullReplayTests(unittest.TestCase):
    def test_veto_range_mid_vol_high_blocks_before_gate_decisions(self) -> None:
        profile = _profile("veto_range_mid_vol_high")
        plans = [
            _plan("HYPE", side="long"),
            _plan("BTC", side="long"),
        ]
        results = apply_micro_regime_policy_to_plans(
            profile,
            plans,
            [
                _snapshot("HYPE", bucket_range_bps=58.0, realized_vol_short_bps=24.0),
                _snapshot("BTC", bucket_range_bps=40.0, realized_vol_short_bps=10.0),
            ],
            pod_name=PodName.POD_A,
        )

        self.assertEqual(results[0].veto_reason, "micro_regime_veto_range_vol_regime_range_mid_vol_high")
        self.assertIsNone(results[1].veto_reason)

        combined = combine_policy_and_gate_decisions(
            results,
            [RiskDecision(accepted=True, reason="accepted", trade_plan=results[1].plan)],
        )
        self.assertFalse(combined[0].accepted)
        self.assertTrue(combined[1].accepted)
        self.assertEqual(combined[1].trade_plan.symbol, "BTC")

    def test_half_size_micro_adverse_scales_plan_risk_values(self) -> None:
        profile = _profile("half_size_micro_adverse")
        plan = _plan(
            "ARB",
            side="long",
            target_notional_usd=200.0,
            margin_usd=40.0,
            risk_budget_usd=8.0,
            expected_loss_usd=4.0,
        )
        results = apply_micro_regime_policy_to_plans(
            profile,
            [plan],
            [_snapshot("ARB", microprice_dislocation_bps=-0.25)],
            pod_name=PodName.POD_A,
        )

        adjusted = results[0].plan
        self.assertIsNone(results[0].veto_reason)
        self.assertEqual(adjusted.target_notional_usd, 100.0)
        self.assertEqual(adjusted.margin_usd, 20.0)
        self.assertEqual(adjusted.risk_budget_usd, 4.0)
        self.assertEqual(adjusted.expected_loss_usd, 2.0)
        self.assertEqual(adjusted.setup_details["microprice_bucket"], "micro_adverse")
        self.assertEqual(adjusted.setup_details["micro_regime_notional_scale"], 0.5)


def _profile(name: str):
    for profile in default_profiles():
        if profile.name == name:
            return profile
    raise AssertionError(f"missing profile {name}")


def _plan(
    symbol: str,
    *,
    side: str,
    target_notional_usd: float = 100.0,
    margin_usd: float = 20.0,
    risk_budget_usd: float = 5.0,
    expected_loss_usd: float = 2.5,
) -> TradePlan:
    return TradePlan(
        symbol=symbol,
        side=side,
        setup="trend_pullback_long" if side == "long" else "trend_pullback_short",
        confidence=0.8,
        target_notional_usd=target_notional_usd,
        stop_bps=120.0,
        time_stop_hours=6,
        margin_usd=margin_usd,
        risk_budget_usd=risk_budget_usd,
        expected_loss_usd=expected_loss_usd,
    )


def _snapshot(
    symbol: str,
    *,
    bucket_range_bps: float = 40.0,
    realized_vol_short_bps: float = 10.0,
    volume_ratio: float = 1.0,
    vwap_distance_bps: float = 2.0,
    microprice_dislocation_bps: float = 0.25,
) -> SymbolMarketSnapshot:
    return SymbolMarketSnapshot(
        symbol=symbol,
        price=100.0,
        ema_fast=100.0,
        ema_slow=99.0,
        vwap_distance_bps=vwap_distance_bps,
        structure_score=0.2,
        funding_rate=0.0,
        spread_bps=1.0,
        btc_aligned=True,
        bucket_range_bps=bucket_range_bps,
        realized_vol_short_bps=realized_vol_short_bps,
        volume_ratio=volume_ratio,
        microprice_dislocation_bps=microprice_dislocation_bps,
    )


if __name__ == "__main__":
    unittest.main()
