from __future__ import annotations

from app.settings import PodCConfig
from app.trident.pod_c.exits import initial_stop_bps, smart_exit_policy
from app.trident.pod_c.signals import SqueezeSignal
from app.trident.types import PodAllocation, TradePlan


class SqueezeBreakoutPlanner:
    """Builds executable Pod C trade plans from squeeze breakout signals."""

    def __init__(self, config: PodCConfig) -> None:
        self.config = config

    def build_trade_plan(
        self,
        signal: SqueezeSignal,
        pod_allocation: PodAllocation,
    ) -> TradePlan | None:
        symbol_allocation = next(
            (item for item in pod_allocation.symbols if item.symbol == signal.symbol),
            None,
        )
        if symbol_allocation is None or symbol_allocation.target_usd <= 0:
            return None
        size_multiplier = max(self.config.size_multiplier, 0.0)
        if size_multiplier <= 0:
            return None
        stop_bps = initial_stop_bps(signal.confidence, signal.market_cluster)
        exit_policy = smart_exit_policy(stop_bps, signal.confidence, signal.market_cluster)
        time_stop_hours = self.config.time_stop_hours
        if signal.market_cluster == "index":
            time_stop_hours = max(1, int(round(self.config.time_stop_hours * 0.5)))
        elif signal.market_cluster == "gold":
            time_stop_hours = max(2, int(round(self.config.time_stop_hours * 0.75)))

        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=signal.confidence,
            target_notional_usd=round(symbol_allocation.target_usd * size_multiplier, 4),
            stop_bps=stop_bps,
            time_stop_hours=time_stop_hours,
            take_profit_bps=exit_policy["take_profit_bps"],
            break_even_trigger_bps=exit_policy["break_even_trigger_bps"],
            trailing_activation_bps=exit_policy["trailing_activation_bps"],
            trailing_distance_bps=exit_policy["trailing_distance_bps"],
            reentry_cooldown_minutes=self.config.reentry_cooldown_minutes,
            confidence_components=signal.confidence_components,
            setup_details={
                "market_cluster": signal.market_cluster,
            },
        )
