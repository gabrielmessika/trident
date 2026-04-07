from __future__ import annotations


def initial_stop_bps(confidence: float) -> float:
    if confidence >= 0.8:
        return 35.0
    if confidence >= 0.65:
        return 45.0
    return 55.0


def smart_exit_policy(stop_bps: float, confidence: float) -> dict[str, float]:
    take_profit_multiplier = 1.1 if confidence >= 0.75 else 0.95
    return {
        "take_profit_bps": round(stop_bps * take_profit_multiplier, 4),
        "break_even_trigger_bps": round(stop_bps * 0.5, 4),
        "trailing_activation_bps": round(stop_bps * 0.75, 4),
        "trailing_distance_bps": round(stop_bps * 0.35, 4),
    }
