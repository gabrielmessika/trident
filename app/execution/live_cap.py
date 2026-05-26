from __future__ import annotations

from dataclasses import replace

from app.trident.types import TradePlan


def apply_live_notional_cap(
    plan: TradePlan,
    max_notional_usd: float,
    *,
    max_leverage: float | None = None,
) -> TradePlan:
    """Return a live-sized plan that never asks the venue above the hard cap."""
    cap = max(float(max_notional_usd or 0.0), 0.0)
    target = float(plan.target_notional_usd or 0.0)
    if cap <= 0.0:
        return plan

    original_margin = max(float(plan.margin_usd or 0.0), 0.0)
    capped_target = min(target, cap)
    leverage_limit = None
    if max_leverage is not None and original_margin > 0.0:
        leverage_limit = max(float(max_leverage or 0.0), 1.0)
        capped_target = min(capped_target, original_margin * leverage_limit)
    if target <= capped_target:
        return plan

    capped_margin = min(original_margin, capped_target) if original_margin > 0.0 else 0.0
    if capped_margin > 0.0:
        effective_leverage = max(capped_target / capped_margin, 1.0)
    else:
        effective_leverage = max(float(plan.effective_leverage or 1.0), 1.0)

    requested_leverage = max(float(plan.requested_leverage or 1.0), 1.0)
    requested_leverage = min(requested_leverage, effective_leverage)
    expected_loss_usd = round(
        capped_target * max(float(plan.stop_bps or 0.0), 0.0) / 10_000.0,
        6,
    )
    setup_details = {
        **dict(plan.setup_details or {}),
        "live_cap_active": True,
        "live_cap_notional_usd": round(cap, 6),
        "live_cap_effective_target_notional_usd": round(capped_target, 6),
        "live_cap_max_leverage": round(leverage_limit, 4) if leverage_limit else 0.0,
        "live_cap_leverage_limited": bool(
            leverage_limit is not None and capped_target < min(target, cap)
        ),
        "live_cap_original_target_notional_usd": round(target, 6),
        "live_cap_original_margin_usd": round(original_margin, 6),
        "live_cap_original_effective_leverage": round(
            max(float(plan.effective_leverage or 1.0), 1.0),
            4,
        ),
    }
    return replace(
        plan,
        target_notional_usd=round(capped_target, 6),
        margin_usd=round(capped_margin, 6),
        requested_leverage=round(requested_leverage, 4),
        effective_leverage=round(effective_leverage, 4),
        expected_loss_usd=expected_loss_usd,
        setup_details=setup_details,
    )
