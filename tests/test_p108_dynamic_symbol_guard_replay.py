from scripts.export_trident_audit_pack import combine_setup_details, compact_setup_details
from scripts.run_p108_dynamic_symbol_guard_replay import _profit_factor, _win_rate


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
        },
    )
    compacted = compact_setup_details(details)

    assert compacted["family"] == "trend_pullback"
    assert compacted["symbol_guard_shadow_mode"] == "observation_only"
    assert compacted["symbol_guard_state"] == "throttle"
    assert compacted["falling_knife_score"] == 62.5
    assert compacted["would_throttle_dynamic_symbol_guard"] is True
    assert compacted["symbol_guard_live_action_unchanged"] is True
