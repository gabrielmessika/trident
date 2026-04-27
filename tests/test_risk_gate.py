import unittest
from dataclasses import replace

from app.risk.pod_a_gate import PodARiskGate
from app.settings import PodAPatternVetoConfig, load_config
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

    def test_launch_fast_crypto_config_loads_pattern_watchers(self) -> None:
        config = load_config("config/trident_crypto_launch_fast_crypto_only.toml")
        self.assertEqual(
            [rule.name for rule in config.pod_a.pattern_watchers],
            [
                "vwap_weak_trend4h_positive",
                "vwap_weak",
                "trend4h_flat",
            ],
        )

    def test_default_config_loads_btc_overextension_veto(self) -> None:
        rule = next(
            rule
            for rule in self.config.pod_a.pattern_vetoes
            if rule.name == "btc_overextension_4h"
        )

        self.assertEqual(rule.symbols, ["BTC"])
        self.assertEqual(rule.sides, ["long"])
        self.assertEqual(rule.min_rsi21_4h, 65.0)
        self.assertEqual(rule.min_ema50_distance_4h_pct, 4.0)
        self.assertEqual(rule.min_btc_overextension_score, 0.70)
        self.assertIsNone(rule.max_macd_hist_delta_4h)

    def test_default_config_loads_xrp_overextension_veto(self) -> None:
        rule = next(
            rule
            for rule in self.config.pod_a.pattern_vetoes
            if rule.name == "xrp_overextension_4h_targeted"
        )

        self.assertEqual(rule.symbols, ["XRP"])
        self.assertEqual(rule.sides, ["long"])
        self.assertEqual(rule.setups, ["trend_pullback_long"])
        self.assertEqual(rule.min_rsi21_4h, 65.0)
        self.assertEqual(rule.min_ema50_distance_4h_pct, 4.0)
        self.assertEqual(rule.min_ema50_distance_4h_atr, 2.0)
        self.assertEqual(rule.min_btc_overextension_score, 0.70)

    def test_default_config_loads_hype_trend_pullback_veto(self) -> None:
        rule = next(
            rule
            for rule in self.config.pod_a.pattern_vetoes
            if rule.name == "hype_trend_pullback_long_targeted"
        )

        self.assertEqual(rule.symbols, ["HYPE"])
        self.assertEqual(rule.sides, ["long"])
        self.assertEqual(rule.setups, ["trend_pullback_long"])

    def test_default_config_loads_mtf_candidate_vetoes(self) -> None:
        rules = {rule.name: rule for rule in self.config.pod_a.pattern_vetoes}

        self.assertEqual(rules["mtf_4h_rsi14_weakness"].max_prev_rsi14_4h, 40.0)
        self.assertTrue(rules["mtf_4h_close_below_ema50"].require_prev_ema50_ready_4h)
        self.assertEqual(
            rules["mtf_1h_chop_ema20_under_ema50_rsi40_50"].max_prev_rsi14_1h,
            50.0,
        )
        self.assertEqual(
            rules["mtf_1h_chop_ema20_under_ema50_rsi40_50"].max_prev_ema20_distance_ema50_1h_pct,
            0.0,
        )
        self.assertEqual(rules["mtf_1h_overextension_chase"].min_entry_vs_open_1h_bps, 50.0)

    def test_rejects_hype_trend_pullback_targeted_veto(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="HYPE",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.72,
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
        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "pattern_veto_hype_trend_pullback_long_targeted")

    def test_rejects_xrp_overextension_targeted_veto(self) -> None:
        decisions = self.gate.evaluate_many(
            [
                TradePlan(
                    symbol="XRP",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.72,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=150.0,
                    effective_leverage=3.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                    setup_details={
                        "rsi21_4h": 70.0,
                        "ema50_distance_4h_pct": 4.6,
                        "ema50_distance_4h_atr": 2.2,
                        "btc_overextension_score": 0.73,
                    },
                )
            ]
        )

        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "pattern_veto_xrp_overextension_4h_targeted")

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
        self.assertEqual(decisions[0].reason, "setup_not_allowed")

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
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                allowed_setups=["trend_pullback_long", "liquidity_sweep_reclaim_long"],
                disabled_setups=[
                    item
                    for item in self.config.pod_a.disabled_setups
                    if item != "liquidity_sweep_reclaim_long"
                ],
                allowed_setups_in_blocked_regimes=["liquidity_sweep_reclaim_long"],
            ),
        )
        gate = PodARiskGate(config)
        decisions = gate.evaluate_many(
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
        self.assertEqual(decisions[0].reason, "setup_not_allowed")

    def test_rejects_setup_not_in_allowlist(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                allowed_setups=["trend_pullback_long"],
            ),
        )
        gate = PodARiskGate(config)

        decisions = gate.evaluate_many(
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
                    setup_details={"regime": "TrendExpansion"},
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "setup_not_allowed")

    def test_rejects_pattern_veto_for_negative_trend_1h(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                pattern_vetoes=[
                    PodAPatternVetoConfig(
                        name="trend1h_negative",
                        setups=["trend_pullback_long"],
                        max_trend_1h_bps=-5.0,
                    )
                ],
            ),
        )
        gate = PodARiskGate(config)

        decisions = gate.evaluate_many(
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
                    setup_details={
                        "regime": "TrendExpansion",
                        "trend_1h_bps": -12.0,
                    },
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "pattern_veto_trend1h_negative")

    def test_rejects_pattern_veto_for_completed_4h_weakness(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                pattern_vetoes=[
                    PodAPatternVetoConfig(
                        name="candidate_4h_rsi_weakness",
                        setups=["trend_pullback_long"],
                        require_prev_ema50_ready_4h=True,
                        max_prev_rsi14_4h=40.0,
                    )
                ],
            ),
        )
        gate = PodARiskGate(config)

        decisions = gate.evaluate_many(
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
                    setup_details={
                        "regime": "TrendExpansion",
                        "prev_ema50_ready_4h": True,
                        "prev_rsi14_4h": 38.0,
                    },
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "pattern_veto_candidate_4h_rsi_weakness")

    def test_rejects_pattern_veto_for_completed_hourly_chop(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                pattern_vetoes=[
                    PodAPatternVetoConfig(
                        name="candidate_1h_chop",
                        setups=["trend_pullback_long"],
                        require_prev_ema50_ready_1h=True,
                        min_prev_rsi14_1h=40.0,
                        max_prev_rsi14_1h=50.0,
                        max_prev_ema20_distance_ema50_1h_pct=0.0,
                    )
                ],
            ),
        )
        gate = PodARiskGate(config)

        decisions = gate.evaluate_many(
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
                    setup_details={
                        "regime": "TrendExpansion",
                        "prev_ema50_ready_1h": True,
                        "prev_rsi14_1h": 46.0,
                        "prev_ema20_distance_ema50_1h_pct": -0.3,
                    },
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "pattern_veto_candidate_1h_chop")

    def test_rejects_pattern_veto_for_hourly_overextension_chase(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                pattern_vetoes=[
                    PodAPatternVetoConfig(
                        name="candidate_1h_overextension",
                        setups=["trend_pullback_long"],
                        min_prev_rsi14_1h=70.0,
                        min_entry_vs_open_1h_bps=50.0,
                    )
                ],
            ),
        )
        gate = PodARiskGate(config)

        decisions = gate.evaluate_many(
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
                    setup_details={
                        "regime": "TrendExpansion",
                        "prev_rsi14_1h": 72.0,
                        "entry_vs_open_1h_bps": 66.0,
                    },
                )
            ]
        )

        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "pattern_veto_candidate_1h_overextension")

    def test_accepts_trade_when_pattern_veto_does_not_match(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                pattern_vetoes=[
                    PodAPatternVetoConfig(
                        name="trend4h_positive_cci_mid",
                        setups=["trend_pullback_long"],
                        min_trend_4h_bps=10.0,
                        min_cci20=-100.0,
                        max_cci20=100.0,
                    )
                ],
            ),
        )
        gate = PodARiskGate(config)

        decisions = gate.evaluate_many(
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
                    setup_details={
                        "regime": "TrendExpansion",
                        "trend_4h_bps": 18.0,
                        "cci20": 120.0,
                    },
                )
            ]
        )

        self.assertTrue(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "accepted")

    def test_rejects_btc_overextension_pattern_veto_only_for_btc(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                pattern_vetoes=[
                    PodAPatternVetoConfig(
                        name="btc_overextension_4h",
                        symbols=["BTC"],
                        sides=["long"],
                        setups=["trend_pullback_long"],
                        min_rsi21_4h=65.0,
                        min_ema50_distance_4h_pct=4.0,
                        max_macd_hist_delta_4h=0.0,
                        min_btc_overextension_score=0.65,
                    )
                ],
            ),
        )
        gate = PodARiskGate(config)
        base_plan = TradePlan(
            symbol="BTC",
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
            setup_details={
                "regime": "TrendExpansion",
                "rsi21_4h": 70.0,
                "ema50_distance_4h_pct": 4.6,
                "macd_hist_delta_4h": -12.5,
                "btc_overextension_score": 0.73,
            },
        )

        rejected = gate.evaluate_many([base_plan])[0]
        accepted_other_symbol = gate.evaluate_many([replace(base_plan, symbol="ETH")])[0]

        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "pattern_veto_btc_overextension_4h")
        self.assertTrue(accepted_other_symbol.accepted)

    def test_tags_trade_with_pattern_watch_hit_without_rejecting(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                pattern_watchers=[
                    PodAPatternVetoConfig(
                        name="vwap_weak",
                        setups=["trend_pullback_long"],
                        max_vwap_reclaim_score=0.45,
                    )
                ],
            ),
        )
        gate = PodARiskGate(config)

        decisions = gate.evaluate_many(
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
                    setup_details={
                        "regime": "TrendExpansion",
                        "vwap_reclaim_score": 0.21,
                    },
                )
            ]
        )

        self.assertTrue(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "accepted")
        self.assertEqual(
            decisions[0].trade_plan.setup_details.get("pattern_watch_hits"),
            "vwap_weak",
        )
        self.assertEqual(
            decisions[0].trade_plan.setup_details.get("pattern_watch_count"),
            1,
        )

    def test_applies_symbol_setup_guardrail(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                guardrail_enabled=True,
                guardrail_lookback_trades=2,
                guardrail_min_closed_trades=2,
                guardrail_max_cumulative_loss_usd=-5.0,
            ),
        )
        gate = PodARiskGate(config)
        plan = TradePlan(
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
            setup_details={"regime": "TrendExpansion"},
        )

        gate.record_closed_trade(symbol="ETH", setup="trend_pullback_long", pnl_usd=-2.6)
        first_pass = gate.evaluate_many([plan])
        self.assertTrue(first_pass[0].accepted)

        gate.record_closed_trade(symbol="ETH", setup="trend_pullback_long", pnl_usd=-2.5)
        blocked = gate.evaluate_many([plan])
        self.assertFalse(blocked[0].accepted)
        self.assertEqual(blocked[0].reason, "rolling_guardrail_symbol_setup")

        gate.record_closed_trade(symbol="ETH", setup="trend_pullback_long", pnl_usd=8.0)
        recovered = gate.evaluate_many([plan])
        self.assertTrue(recovered[0].accepted)

    def test_applies_setup_guardrail_across_symbols(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                setup_guardrail_enabled=True,
                setup_guardrail_lookback_trades=3,
                setup_guardrail_min_closed_trades=3,
                setup_guardrail_max_cumulative_loss_usd=-6.0,
            ),
        )
        gate = PodARiskGate(config)
        plan = TradePlan(
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
            setup_details={"regime": "TrendExpansion"},
        )

        gate.record_closed_trade(symbol="BTC", setup="trend_pullback_long", pnl_usd=-2.5)
        gate.record_closed_trade(symbol="SOL", setup="trend_pullback_long", pnl_usd=-2.1)
        first_pass = gate.evaluate_many([plan])
        self.assertTrue(first_pass[0].accepted)

        gate.record_closed_trade(symbol="DOGE", setup="trend_pullback_long", pnl_usd=-1.9)
        blocked = gate.evaluate_many([plan])
        self.assertFalse(blocked[0].accepted)
        self.assertEqual(blocked[0].reason, "rolling_guardrail_setup")

        gate.record_closed_trade(symbol="XRP", setup="trend_pullback_long", pnl_usd=9.0)
        recovered = gate.evaluate_many([plan])
        self.assertTrue(recovered[0].accepted)

    def test_applies_intraday_setup_guardrail_for_current_date(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                intraday_setup_guardrail_enabled=True,
                intraday_setup_guardrail_lookback_trades=3,
                intraday_setup_guardrail_min_closed_trades=3,
                intraday_setup_guardrail_max_cumulative_loss_usd=-6.0,
                intraday_setup_guardrail_max_average_pnl_usd=-1.5,
            ),
        )
        gate = PodARiskGate(config)
        plan = TradePlan(
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
            setup_details={
                "regime": "TrendExpansion",
                "current_date_key": "2026-04-17",
            },
        )

        gate.record_closed_trade(
            symbol="BTC",
            setup="trend_pullback_long",
            pnl_usd=-2.5,
            date_key="2026-04-17",
        )
        gate.record_closed_trade(
            symbol="SOL",
            setup="trend_pullback_long",
            pnl_usd=-2.1,
            date_key="2026-04-17",
        )
        first_pass = gate.evaluate_many([plan])
        self.assertTrue(first_pass[0].accepted)

        gate.record_closed_trade(
            symbol="DOGE",
            setup="trend_pullback_long",
            pnl_usd=-1.9,
            date_key="2026-04-17",
        )
        blocked = gate.evaluate_many([plan])
        self.assertFalse(blocked[0].accepted)
        self.assertEqual(blocked[0].reason, "rolling_guardrail_intraday_setup")

    def test_intraday_setup_guardrail_resets_next_day(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                intraday_setup_guardrail_enabled=True,
                intraday_setup_guardrail_lookback_trades=3,
                intraday_setup_guardrail_min_closed_trades=3,
                intraday_setup_guardrail_max_cumulative_loss_usd=-6.0,
                intraday_setup_guardrail_max_average_pnl_usd=-1.5,
            ),
        )
        gate = PodARiskGate(config)
        gate.record_closed_trade(
            symbol="BTC",
            setup="trend_pullback_long",
            pnl_usd=-2.5,
            date_key="2026-04-17",
        )
        gate.record_closed_trade(
            symbol="SOL",
            setup="trend_pullback_long",
            pnl_usd=-2.1,
            date_key="2026-04-17",
        )
        gate.record_closed_trade(
            symbol="DOGE",
            setup="trend_pullback_long",
            pnl_usd=-1.9,
            date_key="2026-04-17",
        )

        next_day_plan = TradePlan(
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
            setup_details={
                "regime": "TrendExpansion",
                "current_date_key": "2026-04-18",
            },
        )

        accepted = gate.evaluate_many([next_day_plan])[0]
        self.assertTrue(accepted.accepted)
