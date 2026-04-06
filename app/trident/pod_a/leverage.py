from __future__ import annotations

from app.settings import PodAConfig


def clamp_leverage(value: float, limit: float) -> float:
    return max(1.0, min(value, max(limit, 1.0)))


class LeveragePolicy:
    """Small policy helper for bounded, non-aggressive leverage selection."""

    def __init__(self, config: PodAConfig) -> None:
        self._config = config

    def default(self, symbol: str | None = None) -> float:
        return clamp_leverage(self._config.default_leverage, self.max_allowed(symbol))

    def max_allowed(self, symbol: str | None = None) -> float:
        global_limit = clamp_leverage(self._config.max_leverage, self._config.max_leverage)
        if symbol is None:
            return global_limit
        symbol_limit = self._config.max_leverage_by_symbol.get(str(symbol).upper(), global_limit)
        return clamp_leverage(symbol_limit, min(symbol_limit, global_limit))

    def required_for_target(
        self,
        *,
        symbol: str | None = None,
        margin_cap_usd: float,
        target_notional_usd: float,
    ) -> float:
        if margin_cap_usd <= 0:
            return self.default(symbol)
        return clamp_leverage(target_notional_usd / margin_cap_usd, self.max_allowed(symbol))
