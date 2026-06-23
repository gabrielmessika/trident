from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Mapping

from app.trident.types import TradePlan


def _float_detail(details: Mapping[str, object], key: str, default: float = 0.0) -> float:
    try:
        return float(details.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _int_detail(details: Mapping[str, object], key: str, default: int = 0) -> int:
    try:
        return int(float(details.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def is_crypto_trend_pullback(
    *,
    setup: str | None,
    details: Mapping[str, object] | None,
) -> bool:
    if str(setup or "") != "trend_pullback_long":
        return False
    market_cluster = str((details or {}).get("market_cluster", "") or "").strip().lower()
    return market_cluster == "crypto"


def stop_grace_minutes_for_setup(
    pod_a_config: object | None,
    *,
    setup: str | None,
    confidence: float,
    details: Mapping[str, object] | None,
    fallback_minutes: int = 0,
) -> int:
    raw_details = details or {}
    base_minutes = max(
        int(getattr(pod_a_config, "stop_grace_minutes", fallback_minutes) or 0),
        0,
    )
    if base_minutes <= 0:
        return 0
    if not is_crypto_trend_pullback(setup=setup, details=raw_details):
        return 0

    strong_minutes = max(
        int(getattr(pod_a_config, "stop_grace_strong_minutes", 0) or 0),
        0,
    )
    if strong_minutes <= base_minutes:
        return base_minutes

    min_confidence = float(
        getattr(pod_a_config, "stop_grace_strong_min_confidence", 1.01) or 1.01
    )
    min_a_grade_score = int(
        getattr(pod_a_config, "stop_grace_strong_min_a_grade_score", 999) or 999
    )
    require_no_watch = bool(
        getattr(pod_a_config, "stop_grace_strong_require_no_watch_hits", True)
    )
    watch_count = _int_detail(raw_details, "pattern_watch_count", 0)
    a_grade_score = _int_detail(raw_details, "a_grade_score", 0)
    if confidence < min_confidence:
        return base_minutes
    if a_grade_score < min_a_grade_score:
        return base_minutes
    if require_no_watch and watch_count > 0:
        return base_minutes
    return strong_minutes


def catastrophic_stop_bps_for_plan(execution_config: object, *, stop_bps: float) -> float:
    planned = max(float(stop_bps or 0.0), 0.0)
    legacy_fixed = max(
        float(getattr(execution_config, "live_stop_grace_catastrophic_sl_bps", 0.0) or 0.0),
        0.0,
    )
    multiplier = max(
        float(getattr(execution_config, "live_stop_grace_catastrophic_sl_multiplier", 0.0) or 0.0),
        0.0,
    )
    buffer_bps = max(
        float(getattr(execution_config, "live_stop_grace_catastrophic_sl_buffer_bps", 0.0) or 0.0),
        0.0,
    )
    max_bps = max(
        float(getattr(execution_config, "live_stop_grace_catastrophic_sl_max_bps", 0.0) or 0.0),
        0.0,
    )

    dynamic = 0.0
    if multiplier > 0.0 and planned > 0.0:
        dynamic = max(dynamic, planned * multiplier)
    if buffer_bps > 0.0 and planned > 0.0:
        dynamic = max(dynamic, planned + buffer_bps)

    if dynamic <= 0.0:
        dynamic = legacy_fixed
    elif max_bps > 0.0:
        dynamic = min(dynamic, max_bps)
    elif legacy_fixed > 0.0:
        dynamic = max(dynamic, legacy_fixed)

    return round(max(planned, dynamic), 4)


def pod_a_live_quality_score(plan: TradePlan) -> float:
    details = dict(plan.setup_details or {})
    score = float(plan.confidence or 0.0)
    score += _int_detail(details, "a_grade_score", 0) * 0.015
    score -= _int_detail(details, "pattern_watch_count", 0) * 0.035
    if str(details.get("a_grade_level", "")) == "strong":
        score += 0.05
    if bool(details.get("campaign_mode_active", False)):
        score += 0.02
    return score


def apply_pod_a_live_quality_sizing(
    plan: TradePlan,
    pod_a_config: object,
    *,
    timestamp: str | None,
    loss_tax_until_by_symbol: Mapping[str, datetime],
    correlated_open_count: int,
) -> TradePlan:
    if not bool(getattr(pod_a_config, "live_quality_sizing_enabled", False)):
        return plan
    if not is_crypto_trend_pullback(setup=plan.setup, details=plan.setup_details):
        return plan

    multiplier = 1.0
    reasons: list[str] = []
    confidence = float(plan.confidence or 0.0)
    low_threshold = float(
        getattr(pod_a_config, "live_quality_low_confidence_threshold", 0.0) or 0.0
    )
    mid_threshold = float(
        getattr(pod_a_config, "live_quality_mid_confidence_threshold", 0.0) or 0.0
    )
    if low_threshold > 0.0 and confidence < low_threshold:
        multiplier *= max(
            float(getattr(pod_a_config, "live_quality_low_confidence_multiplier", 1.0) or 1.0),
            0.0,
        )
        reasons.append("low_confidence")
    elif mid_threshold > 0.0 and confidence < mid_threshold:
        multiplier *= max(
            float(getattr(pod_a_config, "live_quality_mid_confidence_multiplier", 1.0) or 1.0),
            0.0,
        )
        reasons.append("mid_confidence")

    details = dict(plan.setup_details or {})
    a_grade_level = str(details.get("a_grade_level", "") or "")
    if not bool(details.get("a_grade_active", False)):
        multiplier *= max(
            float(getattr(pod_a_config, "live_quality_no_a_grade_multiplier", 1.0) or 1.0),
            0.0,
        )
        reasons.append("no_a_grade")
    elif a_grade_level != "strong":
        multiplier *= max(
            float(getattr(pod_a_config, "live_quality_standard_a_grade_multiplier", 1.0) or 1.0),
            0.0,
        )
        reasons.append("standard_a_grade")

    watch_count = max(_int_detail(details, "pattern_watch_count", 0), 0)
    if watch_count > 0:
        watch_multiplier = max(
            float(getattr(pod_a_config, "live_quality_watch_hit_multiplier", 1.0) or 1.0),
            0.0,
        )
        multiplier *= watch_multiplier**watch_count
        reasons.append(f"watch_hits:{watch_count}")

    now = _parse_utc(timestamp)
    loss_tax_until = loss_tax_until_by_symbol.get(str(plan.symbol).upper())
    if (
        bool(getattr(pod_a_config, "live_loss_tax_enabled", False))
        and now is not None
        and loss_tax_until is not None
        and now < loss_tax_until
    ):
        multiplier *= max(
            float(getattr(pod_a_config, "live_loss_tax_multiplier", 1.0) or 1.0),
            0.0,
        )
        reasons.append("symbol_loss_tax")

    full_size_slots = max(
        int(getattr(pod_a_config, "live_correlation_full_size_slots", 0) or 0),
        0,
    )
    if full_size_slots > 0 and correlated_open_count >= full_size_slots:
        multiplier *= max(
            float(getattr(pod_a_config, "live_correlation_extra_multiplier", 1.0) or 1.0),
            0.0,
        )
        reasons.append(f"correlated_slot:{correlated_open_count + 1}")

    floor = max(
        float(getattr(pod_a_config, "live_quality_min_multiplier", 0.0) or 0.0),
        0.0,
    )
    multiplier = max(min(multiplier, 1.0), floor)
    if multiplier >= 0.9999:
        return plan
    return _scale_plan_for_live_quality(plan, multiplier, reasons)


def apply_pod_a_dynamic_symbol_guard_sizing(
    plan: TradePlan,
    pod_a_config: object,
) -> TradePlan:
    live_state_sizing_enabled = bool(
        getattr(pod_a_config, "dynamic_symbol_guard_live_sizing_enabled", False)
    )
    recovery_sizing_enabled = bool(
        getattr(pod_a_config, "dynamic_symbol_guard_recovery_sizing_enabled", False)
    )
    loss_probation_sizing_enabled = bool(
        getattr(
            pod_a_config,
            "dynamic_symbol_guard_loss_probation_sizing_enabled",
            False,
        )
    )
    if (
        not live_state_sizing_enabled
        and not recovery_sizing_enabled
        and not loss_probation_sizing_enabled
    ):
        return plan
    if not is_crypto_trend_pullback(setup=plan.setup, details=plan.setup_details):
        return _annotate_dynamic_symbol_guard_policy(
            plan,
            active=False,
            reason="not_crypto_trend_pullback",
        )

    details = dict(plan.setup_details or {})
    state = str(details.get("symbol_guard_state", "") or "").strip().lower()
    would_reduce = bool(details.get("would_reduce_cap_dynamic_symbol_guard", False))
    would_block = bool(details.get("would_block_dynamic_symbol_guard", False))
    if live_state_sizing_enabled and (state == "quarantine" or would_block):
        multiplier = float(
            getattr(pod_a_config, "dynamic_symbol_guard_quarantine_multiplier", 0.50)
            or 0.50
        )
        reason = "quarantine"
    elif live_state_sizing_enabled and (state == "throttle" or would_reduce):
        multiplier = float(
            getattr(pod_a_config, "dynamic_symbol_guard_throttle_multiplier", 0.50)
            or 0.50
        )
        reason = "throttle"
    elif loss_probation_sizing_enabled:
        multiplier, reason = _dynamic_symbol_guard_loss_probation_multiplier(
            details,
            pod_a_config,
        )
        if multiplier < 0.9999:
            floor = max(
                float(
                    getattr(pod_a_config, "dynamic_symbol_guard_min_multiplier", 0.0)
                    or 0.0
                ),
                0.0,
            )
            multiplier = max(min(multiplier, 1.0), floor)
            return _scale_plan_for_dynamic_symbol_guard_loss_probation(
                plan,
                multiplier,
                reason,
            )
        if recovery_sizing_enabled:
            multiplier, recovery_reason = _dynamic_symbol_guard_recovery_multiplier(
                details,
                pod_a_config,
            )
            if multiplier >= 0.9999:
                return _annotate_dynamic_symbol_guard_policy(
                    plan,
                    active=False,
                    reason="recovery_full_size",
                    recovery_active=False,
                    recovery_multiplier=1.0,
                    recovery_reason=recovery_reason,
                    loss_probation_active=False,
                    loss_probation_multiplier=1.0,
                    loss_probation_reason=reason,
                )
            floor = max(
                float(
                    getattr(pod_a_config, "dynamic_symbol_guard_min_multiplier", 0.0)
                    or 0.0
                ),
                0.0,
            )
            multiplier = max(min(multiplier, 1.0), floor)
            return _scale_plan_for_dynamic_symbol_guard_recovery(
                plan,
                multiplier,
                recovery_reason,
            )
        return _annotate_dynamic_symbol_guard_policy(
            plan,
            active=False,
            reason="normal",
            loss_probation_active=False,
            loss_probation_multiplier=1.0,
            loss_probation_reason=reason,
        )
    elif recovery_sizing_enabled:
        multiplier, reason = _dynamic_symbol_guard_recovery_multiplier(
            details,
            pod_a_config,
        )
        if multiplier >= 0.9999:
            return _annotate_dynamic_symbol_guard_policy(
                plan,
                active=False,
                reason="recovery_full_size",
                recovery_active=False,
                recovery_multiplier=1.0,
                recovery_reason=reason,
            )
        floor = max(
            float(
                getattr(pod_a_config, "dynamic_symbol_guard_min_multiplier", 0.0)
                or 0.0
            ),
            0.0,
        )
        multiplier = max(min(multiplier, 1.0), floor)
        return _scale_plan_for_dynamic_symbol_guard_recovery(plan, multiplier, reason)
    else:
        return _annotate_dynamic_symbol_guard_policy(plan, active=False, reason="normal")

    floor = max(
        float(
            getattr(pod_a_config, "dynamic_symbol_guard_min_multiplier", 0.0)
            or 0.0
        ),
        0.0,
    )
    multiplier = max(min(multiplier, 1.0), floor)
    if multiplier >= 0.9999:
        return _annotate_dynamic_symbol_guard_policy(plan, active=False, reason=reason)
    return _scale_plan_for_dynamic_symbol_guard(plan, multiplier, reason)


def _annotate_dynamic_symbol_guard_policy(
    plan: TradePlan,
    *,
    active: bool,
    reason: str,
    recovery_active: bool | None = None,
    recovery_multiplier: float | None = None,
    recovery_reason: str | None = None,
    loss_probation_active: bool | None = None,
    loss_probation_multiplier: float | None = None,
    loss_probation_reason: str | None = None,
) -> TradePlan:
    setup_details = {
        **dict(plan.setup_details or {}),
        "dynamic_symbol_guard_live_policy_enabled": True,
        "dynamic_symbol_guard_live_sizing_active": bool(active),
        "dynamic_symbol_guard_live_sizing_reason": reason,
    }
    if recovery_active is not None:
        setup_details["dynamic_symbol_guard_recovery_sizing_active"] = bool(recovery_active)
    if recovery_multiplier is not None:
        setup_details["dynamic_symbol_guard_recovery_multiplier"] = round(
            float(recovery_multiplier),
            4,
        )
    if recovery_reason is not None:
        setup_details["dynamic_symbol_guard_recovery_reason"] = recovery_reason
    if loss_probation_active is not None:
        setup_details["dynamic_symbol_guard_loss_probation_sizing_active"] = bool(
            loss_probation_active
        )
    if loss_probation_multiplier is not None:
        setup_details["dynamic_symbol_guard_loss_probation_multiplier"] = round(
            float(loss_probation_multiplier),
            4,
        )
    if loss_probation_reason is not None:
        setup_details["dynamic_symbol_guard_loss_probation_reason"] = loss_probation_reason
    return replace(plan, setup_details=setup_details)


def _scale_plan_for_dynamic_symbol_guard(
    plan: TradePlan,
    multiplier: float,
    reason: str,
) -> TradePlan:
    setup_details = {
        **dict(plan.setup_details or {}),
        "dynamic_symbol_guard_live_policy_enabled": True,
        "dynamic_symbol_guard_live_sizing_active": True,
        "dynamic_symbol_guard_live_sizing_multiplier": round(multiplier, 4),
        "dynamic_symbol_guard_live_sizing_reason": reason,
        "dynamic_symbol_guard_original_target_notional_usd": round(
            float(plan.target_notional_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_original_margin_usd": round(
            float(plan.margin_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_original_risk_budget_usd": round(
            float(plan.risk_budget_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_original_expected_loss_usd": round(
            float(plan.expected_loss_usd or 0.0),
            6,
        ),
        "symbol_guard_live_action_unchanged": False,
    }
    return replace(
        plan,
        target_notional_usd=round(
            float(plan.target_notional_usd or 0.0) * multiplier,
            6,
        ),
        margin_usd=round(float(plan.margin_usd or 0.0) * multiplier, 6),
        risk_budget_usd=round(float(plan.risk_budget_usd or 0.0) * multiplier, 6),
        expected_loss_usd=round(
            float(plan.expected_loss_usd or 0.0) * multiplier,
            6,
        ),
        setup_details=setup_details,
    )


def _scale_plan_for_dynamic_symbol_guard_recovery(
    plan: TradePlan,
    multiplier: float,
    reason: str,
) -> TradePlan:
    setup_details = {
        **dict(plan.setup_details or {}),
        "dynamic_symbol_guard_live_policy_enabled": True,
        "dynamic_symbol_guard_live_sizing_active": False,
        "dynamic_symbol_guard_recovery_sizing_active": True,
        "dynamic_symbol_guard_recovery_multiplier": round(multiplier, 4),
        "dynamic_symbol_guard_recovery_reason": reason,
        "dynamic_symbol_guard_recovery_original_target_notional_usd": round(
            float(plan.target_notional_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_recovery_original_margin_usd": round(
            float(plan.margin_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_recovery_original_risk_budget_usd": round(
            float(plan.risk_budget_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_recovery_original_expected_loss_usd": round(
            float(plan.expected_loss_usd or 0.0),
            6,
        ),
        "symbol_guard_live_action_unchanged": False,
    }
    return replace(
        plan,
        target_notional_usd=round(
            float(plan.target_notional_usd or 0.0) * multiplier,
            6,
        ),
        margin_usd=round(float(plan.margin_usd or 0.0) * multiplier, 6),
        risk_budget_usd=round(float(plan.risk_budget_usd or 0.0) * multiplier, 6),
        expected_loss_usd=round(
            float(plan.expected_loss_usd or 0.0) * multiplier,
            6,
        ),
        setup_details=setup_details,
    )


def _scale_plan_for_dynamic_symbol_guard_loss_probation(
    plan: TradePlan,
    multiplier: float,
    reason: str,
) -> TradePlan:
    setup_details = {
        **dict(plan.setup_details or {}),
        "dynamic_symbol_guard_live_policy_enabled": True,
        "dynamic_symbol_guard_live_sizing_active": False,
        "dynamic_symbol_guard_loss_probation_sizing_active": True,
        "dynamic_symbol_guard_loss_probation_multiplier": round(multiplier, 4),
        "dynamic_symbol_guard_loss_probation_reason": reason,
        "dynamic_symbol_guard_loss_probation_original_target_notional_usd": round(
            float(plan.target_notional_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_loss_probation_original_margin_usd": round(
            float(plan.margin_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_loss_probation_original_risk_budget_usd": round(
            float(plan.risk_budget_usd or 0.0),
            6,
        ),
        "dynamic_symbol_guard_loss_probation_original_expected_loss_usd": round(
            float(plan.expected_loss_usd or 0.0),
            6,
        ),
        "symbol_guard_live_action_unchanged": False,
    }
    return replace(
        plan,
        target_notional_usd=round(
            float(plan.target_notional_usd or 0.0) * multiplier,
            6,
        ),
        margin_usd=round(float(plan.margin_usd or 0.0) * multiplier, 6),
        risk_budget_usd=round(float(plan.risk_budget_usd or 0.0) * multiplier, 6),
        expected_loss_usd=round(
            float(plan.expected_loss_usd or 0.0) * multiplier,
            6,
        ),
        setup_details=setup_details,
    )


def _dynamic_symbol_guard_recovery_multiplier(
    details: Mapping[str, object],
    pod_a_config: object,
) -> tuple[float, str]:
    base = max(
        float(
            getattr(pod_a_config, "dynamic_symbol_guard_recovery_base_multiplier", 0.70)
            or 0.70
        ),
        0.0,
    )
    partial = max(
        float(
            getattr(pod_a_config, "dynamic_symbol_guard_recovery_partial_multiplier", 0.85)
            or 0.85
        ),
        base,
    )
    min_trades = max(
        int(getattr(pod_a_config, "dynamic_symbol_guard_recovery_min_closed_trades", 4) or 4),
        0,
    )
    min_profit_factor = max(
        float(
            getattr(pod_a_config, "dynamic_symbol_guard_recovery_min_profit_factor", 1.05)
            or 1.05
        ),
        0.0,
    )
    min_expectancy = float(
        getattr(pod_a_config, "dynamic_symbol_guard_recovery_min_expectancy_usd", 0.0)
        or 0.0
    )
    trades = max(_int_detail(details, "symbol_setup_rolling_trades", 0), 0)
    expectancy = _float_detail(details, "symbol_setup_rolling_expectancy_usd", 0.0)
    profit_factor = _float_detail(details, "symbol_setup_rolling_profit_factor", 0.0)
    if trades < min_trades:
        return base, "insufficient_rolling_history"
    expectancy_ok = expectancy > min_expectancy
    profit_factor_ok = profit_factor >= min_profit_factor
    if expectancy_ok and profit_factor_ok:
        return 1.0, "rolling_pf_expectancy_positive"
    if expectancy_ok or profit_factor_ok:
        return partial, "partial_rolling_recovery"
    return base, "rolling_pf_expectancy_not_positive"


def _dynamic_symbol_guard_loss_probation_multiplier(
    details: Mapping[str, object],
    pod_a_config: object,
) -> tuple[float, str]:
    multiplier = max(
        float(
            getattr(
                pod_a_config,
                "dynamic_symbol_guard_loss_probation_multiplier",
                0.50,
            )
            or 0.50
        ),
        0.0,
    )
    min_trades = max(
        int(
            getattr(
                pod_a_config,
                "dynamic_symbol_guard_loss_probation_min_closed_trades",
                2,
            )
            or 2
        ),
        0,
    )
    max_pnl = float(
        getattr(
            pod_a_config,
            "dynamic_symbol_guard_loss_probation_max_pnl_usd",
            -16.0,
        )
    )
    max_profit_factor = max(
        float(
            getattr(
                pod_a_config,
                "dynamic_symbol_guard_loss_probation_max_profit_factor",
                0.60,
            )
            or 0.60
        ),
        0.0,
    )
    rehab_min_profit_factor = max(
        float(
            getattr(
                pod_a_config,
                "dynamic_symbol_guard_loss_probation_rehab_min_profit_factor",
                1.05,
            )
            or 1.05
        ),
        0.0,
    )
    rehab_min_expectancy = float(
        getattr(
            pod_a_config,
            "dynamic_symbol_guard_loss_probation_rehab_min_expectancy_usd",
            0.0,
        )
        or 0.0
    )

    trades = max(_int_detail(details, "symbol_setup_rolling_trades", 0), 0)
    pnl = _float_detail(details, "symbol_setup_rolling_pnl_usd", 0.0)
    expectancy = _float_detail(details, "symbol_setup_rolling_expectancy_usd", 0.0)
    profit_factor = _float_detail(details, "symbol_setup_rolling_profit_factor", 0.0)
    if trades < min_trades:
        return 1.0, "insufficient_rolling_history"
    if expectancy > rehab_min_expectancy and profit_factor >= rehab_min_profit_factor:
        return 1.0, "rolling_loss_probation_rehabilitated"
    if pnl <= max_pnl:
        return multiplier, "rolling_loss_probation_pnl"
    if expectancy < 0.0 and profit_factor <= max_profit_factor:
        return multiplier, "rolling_loss_probation_pf_expectancy"
    return 1.0, "rolling_loss_probation_not_degraded"


def _scale_plan_for_live_quality(
    plan: TradePlan,
    multiplier: float,
    reasons: list[str],
) -> TradePlan:
    setup_details = {
        **dict(plan.setup_details or {}),
        "live_quality_sizing_active": True,
        "live_quality_sizing_multiplier": round(multiplier, 4),
        "live_quality_sizing_reasons": ",".join(reasons),
        "live_quality_original_target_notional_usd": round(
            float(plan.target_notional_usd or 0.0),
            6,
        ),
        "live_quality_original_margin_usd": round(float(plan.margin_usd or 0.0), 6),
        "live_quality_original_risk_budget_usd": round(
            float(plan.risk_budget_usd or 0.0),
            6,
        ),
        "live_quality_original_expected_loss_usd": round(
            float(plan.expected_loss_usd or 0.0),
            6,
        ),
    }
    return replace(
        plan,
        target_notional_usd=round(float(plan.target_notional_usd or 0.0) * multiplier, 6),
        margin_usd=round(float(plan.margin_usd or 0.0) * multiplier, 6),
        risk_budget_usd=round(float(plan.risk_budget_usd or 0.0) * multiplier, 6),
        expected_loss_usd=round(float(plan.expected_loss_usd or 0.0) * multiplier, 6),
        setup_details=setup_details,
    )


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
