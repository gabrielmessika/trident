from __future__ import annotations

from app.settings import AppConfig, load_config
from app.trident.pod_a.exits import (
    initial_stop_bps,
    stop_bps_for_signal,
    time_stop_hours_for_cluster,
    smart_exit_policy,
)
from app.trident.pod_a.sizing import PositionSizer
from app.trident.pod_a.signals import AnchorTrendSignal
from app.trident.pod_a.symbol_mode import active_symbol_mode, scale_exit_policy
from app.trident.types import PodAllocation, TradePlan


class AnchorTrendPlanner:
    """Builds executable trade plans from Pod A signals and capital limits."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or load_config("config/trident.toml")
        self._position_sizer = PositionSizer(self._config)

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

        stop_bps = stop_bps_for_signal(
            entry_price=signal.entry_price,
            invalidation_price=signal.invalidation_price,
            side=signal.side,
            fallback_bps=initial_stop_bps(signal.confidence),
        )
        symbol_mode = active_symbol_mode(self._config.pod_a, signal.symbol)
        if symbol_mode is not None:
            stop_bps = max(
                stop_bps * max(symbol_mode.stop_bps_multiplier, 0.0),
                max(symbol_mode.stop_bps_floor, 0.0),
            )
        sized_trade = self._position_sizer.size_from_stop(
            symbol=signal.symbol,
            margin_cap_usd=symbol_allocation.target_usd,
            stop_bps=stop_bps,
        )
        if sized_trade is None:
            return None
        exit_policy = smart_exit_policy(
            signal.setup,
            stop_bps,
            signal.confidence,
            signal.market_cluster,
        )
        if symbol_mode is not None:
            exit_policy = scale_exit_policy(exit_policy, symbol_mode)
        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=signal.confidence,
            target_notional_usd=sized_trade.target_notional_usd,
            stop_bps=stop_bps,
            time_stop_hours=(
                symbol_mode.time_stop_hours
                if symbol_mode is not None
                else time_stop_hours_for_cluster(signal.market_cluster)
            ),
            take_profit_bps=exit_policy["take_profit_bps"],
            break_even_trigger_bps=exit_policy["break_even_trigger_bps"],
            trailing_activation_bps=exit_policy["trailing_activation_bps"],
            trailing_distance_bps=exit_policy["trailing_distance_bps"],
            confidence_components=signal.confidence_components,
            margin_usd=sized_trade.margin_usd,
            requested_leverage=sized_trade.requested_leverage,
            effective_leverage=sized_trade.effective_leverage,
            risk_budget_usd=sized_trade.risk_budget_usd,
            expected_loss_usd=sized_trade.expected_loss_usd,
            invalidation_price=signal.invalidation_price,
            isolated=self._config.pod_a.prefer_isolated,
            setup_details={
                **signal.setup_details,
                "market_cluster": signal.market_cluster,
                "cluster_leader": signal.cluster_leader,
                "special_symbol_mode_active": symbol_mode is not None,
            },
        )
