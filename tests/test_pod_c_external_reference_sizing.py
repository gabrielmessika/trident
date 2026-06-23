from dataclasses import replace

from app.settings import load_config
from app.trident.pod_c.external_reference_sizing import (
    apply_external_reference_sizing_policy,
)
from app.trident.types import TradePlan


def test_external_reference_fresh_cap_sizing_disabled_leaves_plan_unchanged() -> None:
    config = load_config("config/trident.toml")
    config = replace(
        config,
        pod_c=replace(
            config.pod_c,
            external_reference_fresh_cap_sizing_enabled=False,
        ),
    )
    plan = _plan()

    shaped = apply_external_reference_sizing_policy(plan, config.pod_c)

    assert shaped.target_notional_usd == 200.0
    assert "external_reference_fresh_cap_sizing_active" not in shaped.setup_details
    assert shaped.setup_details["external_reference_shadow_live_action_unchanged"] is True


def test_external_reference_fresh_cap_sizing_halves_triggered_plan() -> None:
    config = load_config("config/trident.toml")
    config = replace(
        config,
        pod_c=replace(
            config.pod_c,
            external_reference_fresh_cap_sizing_enabled=True,
            external_reference_fresh_cap_gate="fresh_candidate_default_5m",
            external_reference_fresh_cap_multiplier=0.50,
        ),
    )
    plan = _plan()

    shaped = apply_external_reference_sizing_policy(plan, config.pod_c)

    assert shaped.target_notional_usd == 100.0
    assert shaped.margin_usd == 25.0
    assert shaped.risk_budget_usd == 1.5
    assert shaped.expected_loss_usd == 0.4
    assert shaped.setup_details["external_reference_live_policy_enabled"] is True
    assert shaped.setup_details["external_reference_fresh_cap_sizing_active"] is True
    assert shaped.setup_details["external_reference_fresh_cap_multiplier"] == 0.5
    assert shaped.setup_details["external_reference_fresh_cap_gate"] == (
        "fresh_candidate_default_5m"
    )
    assert shaped.setup_details["external_reference_fresh_cap_reason"] == (
        "fresh_abs_premium_gt_50"
    )
    assert shaped.setup_details["external_reference_shadow_live_action_unchanged"] is False


def test_external_reference_fresh_cap_sizing_full_size_when_gate_not_triggered() -> None:
    config = load_config("config/trident.toml")
    config = replace(
        config,
        pod_c=replace(
            config.pod_c,
            external_reference_fresh_cap_sizing_enabled=True,
        ),
    )
    plan = _plan(
        would_block_external_reference_fresh_candidate_default_5m=False,
        external_reference_fresh_shadow_reason="",
    )

    shaped = apply_external_reference_sizing_policy(plan, config.pod_c)

    assert shaped.target_notional_usd == 200.0
    assert shaped.setup_details["external_reference_fresh_cap_sizing_active"] is False
    assert shaped.setup_details["external_reference_fresh_cap_multiplier"] == 1.0
    assert shaped.setup_details["external_reference_fresh_cap_reason"] == "gate_not_triggered"
    assert shaped.setup_details["external_reference_shadow_live_action_unchanged"] is True


def _plan(**details: object) -> TradePlan:
    setup_details = {
        "external_reference_shadow_mode": "observation_only",
        "external_reference_fresh_shadow_available": True,
        "would_block_external_reference_fresh_candidate_default_5m": True,
        "external_reference_fresh_shadow_reason": "fresh_abs_premium_gt_50",
        "external_reference_shadow_live_action_unchanged": True,
    }
    setup_details.update(details)
    return TradePlan(
        symbol="XYZ:GOLD",
        side="long",
        setup="tradfi_continuation_long",
        confidence=0.78,
        target_notional_usd=200.0,
        stop_bps=40.0,
        time_stop_hours=4,
        margin_usd=50.0,
        risk_budget_usd=3.0,
        expected_loss_usd=0.8,
        setup_details=setup_details,
    )
