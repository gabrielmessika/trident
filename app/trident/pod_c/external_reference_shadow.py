from __future__ import annotations

from typing import Mapping


def external_reference_shadow_details(
    details: Mapping[str, object] | None,
    *,
    side: str = "long",
) -> dict[str, object]:
    """Observation-only P1-03 candidate gates for Pod C external references."""

    payload = dict(details or {})
    available = _source_count(payload) > 0
    premium = _optional_float(payload.get("external_premium_bps"))
    age = _optional_float(payload.get("external_reference_age_seconds"))
    momentum_60 = _optional_float(payload.get("external_momentum_60s_bps"))
    momentum_300 = _optional_float(payload.get("external_momentum_300s_bps"))
    side_normalized = str(side).strip().lower()

    missing = not available
    stale_15m = available and age is not None and age > 900.0
    abs_premium_gt_50 = available and premium is not None and abs(premium) > 50.0
    abs_premium_gt_100 = available and premium is not None and abs(premium) > 100.0
    long_chase_gt_50 = (
        available
        and side_normalized == "long"
        and premium is not None
        and premium > 50.0
    )
    counter_momentum_5m_6bps = (
        available
        and momentum_300 is not None
        and (
            (side_normalized == "long" and momentum_300 <= -6.0)
            or (side_normalized == "short" and momentum_300 >= 6.0)
        )
    )

    candidate_loose = (
        missing
        or stale_15m
        or abs_premium_gt_100
        or long_chase_gt_50
        or counter_momentum_5m_6bps
    )
    candidate_default = (
        missing
        or stale_15m
        or abs_premium_gt_50
        or (
            available
            and side_normalized == "long"
            and premium is not None
            and premium > 25.0
        )
        or counter_momentum_5m_6bps
    )

    reasons = []
    if missing:
        reasons.append("missing_reference")
    if stale_15m:
        reasons.append("stale_gt_15m")
    if abs_premium_gt_50:
        reasons.append("abs_premium_gt_50")
    if abs_premium_gt_100:
        reasons.append("abs_premium_gt_100")
    if long_chase_gt_50:
        reasons.append("long_chase_premium_gt_50")
    if counter_momentum_5m_6bps:
        reasons.append("counter_momentum_5m_6bps")

    return {
        "external_reference_shadow_mode": "observation_only",
        "external_reference_shadow_available": available,
        "would_block_external_reference_abs_premium_gt_50": abs_premium_gt_50,
        "would_block_external_reference_abs_premium_gt_100": abs_premium_gt_100,
        "would_block_external_reference_counter_momentum_5m_6bps": counter_momentum_5m_6bps,
        "would_block_external_reference_candidate_loose_5m": candidate_loose,
        "would_block_external_reference_candidate_default_5m": candidate_default,
        "external_reference_shadow_reason": ",".join(reasons),
        "external_reference_shadow_live_action_unchanged": True,
    }


def external_reference_shadow_setup_details(
    details: Mapping[str, object] | None,
    *,
    side: str = "long",
) -> dict[str, object]:
    return external_reference_shadow_details(details, side=side)


def _source_count(details: Mapping[str, object]) -> int:
    try:
        return int(float(details.get("external_reference_source_count") or 0.0))
    except (TypeError, ValueError):
        return 0


def _optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
