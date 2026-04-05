from __future__ import annotations

from app.trident.pod_a.exits import initial_stop_bps, time_stop_hours
from app.trident.pod_a.signals import AnchorTrendSignal
from app.trident.types import PodAllocation, TradePlan


class AnchorTrendPlanner:
    """Builds executable trade plans from Pod A signals and capital limits."""

    def build_trade_plan(
        self,
        signal: AnchorTrendSignal,
        pod_allocation: PodAllocation,
    ) -> TradePlan | None:
        symbol_allocation = next(
            (item for item in pod_allocation.symbols if item.symbol == signal.symbol),
            None,
        )
        if symbol_allocation is None or symbol_allocation.target_usd <= 0:
            return None

        stop_bps = initial_stop_bps(signal.confidence)
        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=signal.confidence,
            target_notional_usd=symbol_allocation.target_usd,
            stop_bps=stop_bps,
            time_stop_hours=time_stop_hours(),
            confidence_components=signal.confidence_components,
        )
