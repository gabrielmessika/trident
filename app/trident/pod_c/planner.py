from __future__ import annotations

from app.settings import PodCConfig
from app.trident.pod_c.exits import initial_stop_bps, smart_exit_policy
from app.trident.pod_c.signals import EventRaiderSignal
from app.trident.types import PodAllocation, TradePlan


class EventRaiderPlanner:
    """Builds executable Pod C trade plans from lead-lag signals."""

    def __init__(self, config: PodCConfig) -> None:
        self.config = config

    def build_trade_plan(
        self,
        signal: EventRaiderSignal,
        pod_allocation: PodAllocation,
    ) -> TradePlan | None:
        symbol_allocation = next(
            (item for item in pod_allocation.symbols if item.symbol == signal.symbol),
            None,
        )
        if symbol_allocation is None or symbol_allocation.target_usd <= 0:
            return None
        stop_bps = initial_stop_bps(signal.confidence)
        exit_policy = smart_exit_policy(stop_bps, signal.confidence)

        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=signal.confidence,
            target_notional_usd=symbol_allocation.target_usd,
            stop_bps=stop_bps,
            time_stop_hours=self.config.time_stop_hours,
            take_profit_bps=exit_policy["take_profit_bps"],
            break_even_trigger_bps=exit_policy["break_even_trigger_bps"],
            trailing_activation_bps=exit_policy["trailing_activation_bps"],
            trailing_distance_bps=exit_policy["trailing_distance_bps"],
            reentry_cooldown_minutes=self.config.reentry_cooldown_minutes,
            confidence_components=signal.confidence_components,
        )
