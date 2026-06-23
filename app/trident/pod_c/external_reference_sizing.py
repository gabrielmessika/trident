from __future__ import annotations

from dataclasses import replace

from app.trident.types import TradePlan


FRESH_CAP_GATE_FIELDS = {
    "fresh_abs_premium_gt_50": "would_block_external_reference_fresh_abs_premium_gt_50",
    "fresh_counter_momentum_5m_6bps": (
        "would_block_external_reference_fresh_counter_momentum_5m_6bps"
    ),
    "fresh_candidate_loose_5m": (
        "would_block_external_reference_fresh_candidate_loose_5m"
    ),
    "fresh_candidate_default_5m": (
        "would_block_external_reference_fresh_candidate_default_5m"
    ),
}


def apply_external_reference_sizing_policy(
    plan: TradePlan,
    pod_c_config: object,
) -> TradePlan:
    if not bool(getattr(pod_c_config, "external_reference_fresh_cap_sizing_enabled", False)):
        return plan

    gate = str(
        getattr(
            pod_c_config,
            "external_reference_fresh_cap_gate",
            "fresh_candidate_default_5m",
        )
        or "fresh_candidate_default_5m"
    )
    gate_field = FRESH_CAP_GATE_FIELDS.get(gate)
    if gate_field is None:
        return _annotate_external_reference_policy(
            plan,
            active=False,
            gate=gate,
            multiplier=1.0,
            reason="unsupported_gate",
        )

    details = dict(plan.setup_details or {})
    if details.get(gate_field) is not True:
        return _annotate_external_reference_policy(
            plan,
            active=False,
            gate=gate,
            multiplier=1.0,
            reason="gate_not_triggered",
        )

    multiplier = max(
        min(
            float(
                getattr(
                    pod_c_config,
                    "external_reference_fresh_cap_multiplier",
                    0.50,
                )
                or 0.50
            ),
            1.0,
        ),
        0.0,
    )
    if multiplier >= 0.9999:
        return _annotate_external_reference_policy(
            plan,
            active=False,
            gate=gate,
            multiplier=1.0,
            reason="multiplier_full_size",
        )
    return _scale_plan_for_external_reference_cap(
        plan,
        multiplier=multiplier,
        gate=gate,
        reason=str(details.get("external_reference_fresh_shadow_reason") or gate),
    )


def _annotate_external_reference_policy(
    plan: TradePlan,
    *,
    active: bool,
    gate: str,
    multiplier: float,
    reason: str,
) -> TradePlan:
    setup_details = {
        **dict(plan.setup_details or {}),
        "external_reference_live_policy_enabled": True,
        "external_reference_fresh_cap_sizing_active": bool(active),
        "external_reference_fresh_cap_gate": gate,
        "external_reference_fresh_cap_multiplier": round(float(multiplier), 4),
        "external_reference_fresh_cap_reason": reason,
    }
    return replace(plan, setup_details=setup_details)


def _scale_plan_for_external_reference_cap(
    plan: TradePlan,
    *,
    multiplier: float,
    gate: str,
    reason: str,
) -> TradePlan:
    setup_details = {
        **dict(plan.setup_details or {}),
        "external_reference_live_policy_enabled": True,
        "external_reference_fresh_cap_sizing_active": True,
        "external_reference_fresh_cap_gate": gate,
        "external_reference_fresh_cap_multiplier": round(multiplier, 4),
        "external_reference_fresh_cap_reason": reason,
        "external_reference_fresh_cap_original_target_notional_usd": round(
            float(plan.target_notional_usd or 0.0),
            6,
        ),
        "external_reference_fresh_cap_original_margin_usd": round(
            float(plan.margin_usd or 0.0),
            6,
        ),
        "external_reference_fresh_cap_original_risk_budget_usd": round(
            float(plan.risk_budget_usd or 0.0),
            6,
        ),
        "external_reference_fresh_cap_original_expected_loss_usd": round(
            float(plan.expected_loss_usd or 0.0),
            6,
        ),
        "external_reference_shadow_live_action_unchanged": False,
    }
    return replace(
        plan,
        target_notional_usd=round(float(plan.target_notional_usd or 0.0) * multiplier, 6),
        margin_usd=round(float(plan.margin_usd or 0.0) * multiplier, 6),
        risk_budget_usd=round(float(plan.risk_budget_usd or 0.0) * multiplier, 6),
        expected_loss_usd=round(float(plan.expected_loss_usd or 0.0) * multiplier, 6),
        setup_details=setup_details,
    )
