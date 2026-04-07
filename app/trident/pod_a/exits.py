from __future__ import annotations

from app.trident.pod_a.structure import stop_bps_from_invalidation


def initial_stop_bps(structure_score: float) -> float:
    base_stop = 90.0
    if abs(structure_score) >= 0.70:
        return 70.0
    if abs(structure_score) >= 0.55:
        return 80.0
    return base_stop


def time_stop_hours() -> int:
    return 24


def stop_bps_for_signal(
    *,
    entry_price: float,
    invalidation_price: float | None,
    side: str,
    fallback_bps: float,
) -> float:
    return stop_bps_from_invalidation(
        entry_price=entry_price,
        invalidation_price=invalidation_price,
        side=side,
        fallback_bps=fallback_bps,
    )


def smart_exit_policy(setup: str, stop_bps: float, confidence: float) -> dict[str, float]:
    if setup.startswith("liquidity_sweep_reclaim") or setup.startswith("bos_retest"):
        take_profit_multiplier = 1.9 if confidence >= 0.8 else 1.7
        break_even_multiplier = 0.95
        trailing_activation_multiplier = 1.35
        trailing_distance_multiplier = 0.7
    elif setup.startswith("vwap_reclaim"):
        take_profit_multiplier = 1.55 if confidence >= 0.75 else 1.4
        break_even_multiplier = 0.8
        trailing_activation_multiplier = 1.15
        trailing_distance_multiplier = 0.65
    else:
        take_profit_multiplier = 1.35 if confidence >= 0.7 else 1.2
        break_even_multiplier = 0.7
        trailing_activation_multiplier = 1.0
        trailing_distance_multiplier = 0.55

    return {
        "take_profit_bps": round(stop_bps * take_profit_multiplier, 4),
        "break_even_trigger_bps": round(stop_bps * break_even_multiplier, 4),
        "trailing_activation_bps": round(stop_bps * trailing_activation_multiplier, 4),
        "trailing_distance_bps": round(stop_bps * trailing_distance_multiplier, 4),
    }
