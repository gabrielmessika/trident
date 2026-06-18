from __future__ import annotations


def _continuation_like_setup(setup: str) -> bool:
    return setup.startswith("tradfi_continuation") or setup == "p109_oil_short_4h_time_gate"


def initial_stop_bps(
    setup: str,
    confidence: float,
    market_cluster: str = "crypto",
) -> float:
    if _continuation_like_setup(setup):
        base = 55.0 if confidence >= 0.78 else 65.0
    else:
        base = 70.0 if confidence >= 0.75 else 82.0
    if market_cluster == "index":
        return round(base * 0.90, 4)
    if market_cluster in {"gold", "silver"}:
        return round(base * 0.95, 4)
    if market_cluster == "oil":
        return round(base * 1.05, 4)
    return round(base, 4)


def time_stop_hours_for_cluster(
    base_time_stop_hours: int,
    market_cluster: str = "crypto",
) -> int:
    if market_cluster == "index":
        return max(2, int(round(base_time_stop_hours * 0.75)))
    if market_cluster in {"gold", "silver"}:
        return max(3, int(round(base_time_stop_hours * 0.85)))
    if market_cluster == "oil":
        return max(2, int(round(base_time_stop_hours * 0.65)))
    return max(1, int(base_time_stop_hours))


def smart_exit_policy(
    setup: str,
    stop_bps: float,
    confidence: float,
    market_cluster: str = "crypto",
) -> dict[str, float]:
    if _continuation_like_setup(setup):
        take_profit_multiplier = 1.8 if confidence >= 0.75 else 1.6
        break_even_multiplier = 0.85
        trailing_activation_multiplier = 1.15
        trailing_distance_multiplier = 0.60
    else:
        take_profit_multiplier = 1.55 if confidence >= 0.72 else 1.40
        break_even_multiplier = 0.72
        trailing_activation_multiplier = 0.95
        trailing_distance_multiplier = 0.55

    if market_cluster == "index":
        take_profit_multiplier *= 0.95
        break_even_multiplier *= 0.95
        trailing_activation_multiplier *= 0.90
        trailing_distance_multiplier *= 0.90
    elif market_cluster in {"gold", "silver"}:
        take_profit_multiplier *= 1.00
        break_even_multiplier *= 0.92
        trailing_activation_multiplier *= 0.95
        trailing_distance_multiplier *= 0.95
    elif market_cluster == "oil":
        take_profit_multiplier *= 1.10
        break_even_multiplier *= 0.90
        trailing_activation_multiplier *= 1.05
        trailing_distance_multiplier *= 1.05

    return {
        "take_profit_bps": round(stop_bps * take_profit_multiplier, 4),
        "break_even_trigger_bps": round(stop_bps * break_even_multiplier, 4),
        "trailing_activation_bps": round(stop_bps * trailing_activation_multiplier, 4),
        "trailing_distance_bps": round(stop_bps * trailing_distance_multiplier, 4),
    }
