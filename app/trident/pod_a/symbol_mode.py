from __future__ import annotations

from app.settings import PodAConfig, PodASymbolModeConfig


def active_symbol_mode(
    config: PodAConfig,
    symbol: str | None,
) -> PodASymbolModeConfig | None:
    if symbol is None:
        return None
    mode = config.symbol_modes.get(str(symbol).strip().upper())
    if mode is None or not mode.enabled:
        return None
    return mode


def scale_exit_policy(
    exit_policy: dict[str, float],
    mode: PodASymbolModeConfig,
) -> dict[str, float]:
    return {
        "take_profit_bps": round(
            exit_policy["take_profit_bps"] * max(mode.take_profit_multiplier, 0.0),
            4,
        ),
        "break_even_trigger_bps": round(
            exit_policy["break_even_trigger_bps"] * max(mode.break_even_multiplier, 0.0),
            4,
        ),
        "trailing_activation_bps": round(
            exit_policy["trailing_activation_bps"]
            * max(mode.trailing_activation_multiplier, 0.0),
            4,
        ),
        "trailing_distance_bps": round(
            exit_policy["trailing_distance_bps"]
            * max(mode.trailing_distance_multiplier, 0.0),
            4,
        ),
    }
