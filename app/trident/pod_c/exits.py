from __future__ import annotations


def initial_stop_bps(confidence: float, market_cluster: str = "crypto") -> float:
    if confidence >= 0.8:
        base = 40.0
    elif confidence >= 0.65:
        base = 55.0
    else:
        base = 70.0
    if market_cluster == "index":
        return round(base * 0.85, 4)
    if market_cluster == "gold":
        return round(base * 0.9, 4)
    return base


def smart_exit_policy(
    stop_bps: float,
    confidence: float,
    market_cluster: str = "crypto",
) -> dict[str, float]:
    take_profit_multiplier = 1.5 if confidence >= 0.75 else 1.2
    break_even_multiplier = 0.45
    trailing_activation_multiplier = 0.65
    trailing_distance_multiplier = 0.30
    if market_cluster == "index":
        take_profit_multiplier *= 0.9
        break_even_multiplier *= 0.8
        trailing_activation_multiplier *= 0.8
        trailing_distance_multiplier *= 0.8
    elif market_cluster == "gold":
        take_profit_multiplier *= 1.0
        break_even_multiplier *= 0.9
        trailing_activation_multiplier *= 0.9
        trailing_distance_multiplier *= 0.9
    return {
        "take_profit_bps": round(stop_bps * take_profit_multiplier, 4),
        "break_even_trigger_bps": round(stop_bps * break_even_multiplier, 4),
        "trailing_activation_bps": round(stop_bps * trailing_activation_multiplier, 4),
        "trailing_distance_bps": round(stop_bps * trailing_distance_multiplier, 4),
    }
