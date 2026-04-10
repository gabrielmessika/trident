from __future__ import annotations

from app.settings import PodCConfig
from app.trident.pod_c.exits import initial_stop_bps, smart_exit_policy, time_stop_hours_for_cluster
from app.trident.pod_c.signals import TradfiTrendSignal
from app.trident.types import PodAllocation, TradePlan


class TradfiTrendPlanner:
    """Builds executable Pod C trade plans for Tradfi directionals."""

    def __init__(self, config: PodCConfig) -> None:
        self.config = config

    def build_trade_plan(
        self,
        signal: TradfiTrendSignal,
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
        stop_bps = initial_stop_bps(
            signal.setup,
            signal.confidence,
            signal.market_cluster,
        )
        exit_policy = smart_exit_policy(
            signal.setup,
            stop_bps,
            signal.confidence,
            signal.market_cluster,
        )

        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=signal.confidence,
            target_notional_usd=round(symbol_allocation.target_usd * size_multiplier, 4),
            stop_bps=stop_bps,
            time_stop_hours=time_stop_hours_for_cluster(
                self.config.time_stop_hours,
                signal.market_cluster,
            ),
            take_profit_bps=exit_policy["take_profit_bps"],
            break_even_trigger_bps=exit_policy["break_even_trigger_bps"],
            trailing_activation_bps=exit_policy["trailing_activation_bps"],
            trailing_distance_bps=exit_policy["trailing_distance_bps"],
            reentry_cooldown_minutes=self.config.reentry_cooldown_minutes,
            confidence_components=signal.confidence_components,
            setup_details={
                "market_cluster": signal.market_cluster,
                "cluster_leader": signal.cluster_leader,
            },
        )


# Backward-compatible alias while the pod is rewired from squeeze to Tradfi trend.
SqueezeBreakoutPlanner = TradfiTrendPlanner
