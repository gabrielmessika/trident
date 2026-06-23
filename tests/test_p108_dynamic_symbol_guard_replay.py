from scripts.export_trident_audit_pack import combine_setup_details, compact_setup_details
from scripts.run_p108_dynamic_symbol_guard_replay import (
    ScenarioSpec,
    _profit_factor,
    _win_rate,
    filter_scenarios,
    p108_loss_probation_multiplier,
    p108_recovery_multiplier,
)


def test_win_rate_uses_reported_value_when_available() -> None:
    assert _win_rate({"win_rate": 0.61538, "closed_trade_count": 10, "win_count": 1}) == 0.6154


def test_win_rate_falls_back_to_win_count() -> None:
    assert _win_rate({"closed_trade_count": 4, "win_count": 3}) == 0.75


def test_profit_factor_can_use_closed_trade_log() -> None:
    assert _profit_factor(
        {
            "closed_trade_log": [
                {"pnl_usd": 2.0},
                {"pnl_usd": 1.0},
                {"pnl_usd": -1.5},
            ]
        }
    ) == 2.0


def test_dynamic_symbol_guard_details_are_compacted_for_export() -> None:
    details = combine_setup_details(
        {"family": "trend_pullback"},
        {
            "symbol_guard_shadow_mode": "observation_only",
            "symbol_guard_state": "throttle",
            "falling_knife_score": 62.5,
            "would_throttle_dynamic_symbol_guard": True,
            "would_block_dynamic_symbol_guard": False,
            "symbol_guard_live_action_unchanged": True,
            "symbol_setup_rolling_profit_factor": 1.2,
            "dynamic_symbol_guard_recovery_sizing_active": True,
            "dynamic_symbol_guard_recovery_multiplier": 0.7,
        },
    )
    compacted = compact_setup_details(details)

    assert compacted["family"] == "trend_pullback"
    assert compacted["symbol_guard_shadow_mode"] == "observation_only"
    assert compacted["symbol_guard_state"] == "throttle"
    assert compacted["falling_knife_score"] == 62.5
    assert compacted["would_throttle_dynamic_symbol_guard"] is True
    assert compacted["symbol_guard_live_action_unchanged"] is True
    assert compacted["symbol_setup_rolling_profit_factor"] == 1.2
    assert compacted["dynamic_symbol_guard_recovery_sizing_active"] is True
    assert compacted["dynamic_symbol_guard_recovery_multiplier"] == 0.7


def test_filter_scenarios_keeps_requested_order_and_rejects_unknown() -> None:
    scenarios = [
        ScenarioSpec(name="current_ac", description="current"),
        ScenarioSpec(name="candidate", description="candidate"),
    ]

    filtered = filter_scenarios(scenarios, "candidate,current_ac")

    assert [scenario.name for scenario in filtered] == ["candidate", "current_ac"]
    try:
        filter_scenarios(scenarios, "missing")
    except SystemExit as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing scenario should raise SystemExit")


def test_recovery_multiplier_requires_positive_pf_and_expectancy() -> None:
    spec = ScenarioSpec(
        name="recovery",
        description="test",
        action="recovery_sizing_policy",
    )

    assert p108_recovery_multiplier(
        spec,
        {
            "symbol_setup_rolling_trades": 2,
            "symbol_setup_rolling_expectancy_usd": 1.0,
            "symbol_setup_rolling_profit_factor": 2.0,
        },
    ) == 0.70
    assert p108_recovery_multiplier(
        spec,
        {
            "symbol_setup_rolling_trades": 4,
            "symbol_setup_rolling_expectancy_usd": 0.25,
            "symbol_setup_rolling_profit_factor": 0.9,
        },
    ) == 0.85
    assert p108_recovery_multiplier(
        spec,
        {
            "symbol_setup_rolling_trades": 4,
            "symbol_setup_rolling_expectancy_usd": 0.25,
            "symbol_setup_rolling_profit_factor": 1.2,
        },
    ) == 1.0


def test_loss_probation_multiplier_caps_only_after_negative_rolling_history() -> None:
    spec = ScenarioSpec(
        name="loss_probation",
        description="test",
        action="loss_probation_sizing_policy",
    )

    assert p108_loss_probation_multiplier(
        spec,
        {
            "symbol_setup_rolling_trades": 1,
            "symbol_setup_rolling_pnl_usd": -5.0,
            "symbol_setup_rolling_expectancy_usd": -5.0,
            "symbol_setup_rolling_profit_factor": 0.0,
        },
    ) == 1.0
    assert p108_loss_probation_multiplier(
        spec,
        {
            "symbol_setup_rolling_trades": 2,
            "symbol_setup_rolling_pnl_usd": -2.1,
            "symbol_setup_rolling_expectancy_usd": -1.05,
            "symbol_setup_rolling_profit_factor": 0.0,
        },
    ) == 0.50
    assert p108_loss_probation_multiplier(
        spec,
        {
            "symbol_setup_rolling_trades": 3,
            "symbol_setup_rolling_pnl_usd": 2.5,
            "symbol_setup_rolling_expectancy_usd": 0.833333,
            "symbol_setup_rolling_profit_factor": 1.5,
        },
    ) == 1.0
