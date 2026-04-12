from __future__ import annotations

from dataclasses import dataclass

from app.settings import AppConfig
from app.trident.pod_b.signals import BreakoutSignal
from app.trident.types import PodAllocation, TradePlan


@dataclass(slots=True)
class _SizedTrade:
    margin_usd: float
    target_notional_usd: float
    requested_leverage: float
    effective_leverage: float
    risk_budget_usd: float
    expected_loss_usd: float


class BreakoutPlanner:
    """Builds replay-only Pod B bis trade plans from breakout signals."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def build_trade_plan(
        self,
        signal: BreakoutSignal,
        pod_allocation: PodAllocation,
    ) -> TradePlan | None:
        symbol_allocation = next(
            (item for item in pod_allocation.symbols if item.symbol == signal.symbol),
            None,
        )
        if symbol_allocation is None or symbol_allocation.target_usd <= 0:
            return None
        sized_trade = self._size_from_stop(
            symbol=signal.symbol,
            margin_cap_usd=symbol_allocation.target_usd,
            stop_bps=signal.stop_bps_hint,
        )
        if sized_trade is None:
            return None
        exit_policy = self._exit_policy(signal.setup, signal.stop_bps_hint, signal.confidence)
        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=signal.confidence,
            target_notional_usd=sized_trade.target_notional_usd,
            stop_bps=signal.stop_bps_hint,
            time_stop_hours=self._config.pod_b.bis_time_stop_hours,
            take_profit_bps=exit_policy["take_profit_bps"],
            break_even_trigger_bps=exit_policy["break_even_trigger_bps"],
            trailing_activation_bps=exit_policy["trailing_activation_bps"],
            trailing_distance_bps=exit_policy["trailing_distance_bps"],
            reentry_cooldown_minutes=self._config.pod_b.bis_reentry_cooldown_minutes,
            confidence_components=signal.confidence_components,
            margin_usd=sized_trade.margin_usd,
            requested_leverage=sized_trade.requested_leverage,
            effective_leverage=sized_trade.effective_leverage,
            risk_budget_usd=sized_trade.risk_budget_usd,
            expected_loss_usd=sized_trade.expected_loss_usd,
            isolated=True,
            setup_details={
                **signal.setup_details,
                "market_cluster": signal.market_cluster,
                "cluster_leader": signal.cluster_leader,
            },
        )

    def _size_from_stop(
        self,
        *,
        symbol: str,
        margin_cap_usd: float,
        stop_bps: float,
    ) -> _SizedTrade | None:
        if margin_cap_usd <= 0 or stop_bps <= 0:
            return None
        total_equity = self._config.trident.capital.reference_equity_usd
        per_trade_pct = min(
            max(self._config.pod_b.bis_risk_per_trade_pct, 0.0),
            max(self._config.trident.risk.max_risk_per_trade_pct, 0.0),
        )
        risk_budget_usd = round(total_equity * per_trade_pct, 6)
        if risk_budget_usd <= 0:
            return None
        stop_fraction = stop_bps / 10_000.0
        desired_notional_usd = risk_budget_usd / stop_fraction
        requested_leverage = self._default_leverage(symbol)
        effective_leverage = max(
            requested_leverage,
            self._required_leverage(
                symbol=symbol,
                margin_cap_usd=margin_cap_usd,
                target_notional_usd=desired_notional_usd,
            ),
        )
        max_notional_from_margin = margin_cap_usd * effective_leverage
        target_notional_usd = min(desired_notional_usd, max_notional_from_margin)
        if target_notional_usd <= 0:
            return None
        margin_usd = target_notional_usd / effective_leverage
        expected_loss_usd = target_notional_usd * stop_fraction
        return _SizedTrade(
            margin_usd=round(margin_usd, 6),
            target_notional_usd=round(target_notional_usd, 6),
            requested_leverage=round(requested_leverage, 4),
            effective_leverage=round(effective_leverage, 4),
            risk_budget_usd=round(risk_budget_usd, 6),
            expected_loss_usd=round(expected_loss_usd, 6),
        )

    def _default_leverage(self, symbol: str) -> float:
        return max(1.0, min(self._config.pod_b.bis_default_leverage, self._max_leverage(symbol)))

    def _max_leverage(self, symbol: str) -> float:
        global_limit = max(self._config.pod_b.bis_max_leverage, 1.0)
        symbol_limit = self._config.pod_b.bis_max_leverage_by_symbol.get(symbol.upper(), global_limit)
        return max(1.0, min(symbol_limit, global_limit))

    def _required_leverage(
        self,
        *,
        symbol: str,
        margin_cap_usd: float,
        target_notional_usd: float,
    ) -> float:
        if margin_cap_usd <= 0:
            return self._default_leverage(symbol)
        return max(1.0, min(target_notional_usd / margin_cap_usd, self._max_leverage(symbol)))

    def _exit_policy(
        self,
        setup: str,
        stop_bps: float,
        confidence: float,
    ) -> dict[str, float]:
        confidence_bonus = max(confidence - 0.6, 0.0)
        if setup.startswith("compression_breakout"):
            return {
                "take_profit_bps": round(stop_bps * (2.0 + confidence_bonus), 4),
                "break_even_trigger_bps": round(stop_bps * 0.9, 4),
                "trailing_activation_bps": round(stop_bps * 1.4, 4),
                "trailing_distance_bps": round(stop_bps * 0.9, 4),
            }
        return {
            "take_profit_bps": round(stop_bps * (2.4 + confidence_bonus), 4),
            "break_even_trigger_bps": round(stop_bps * 1.0, 4),
            "trailing_activation_bps": round(stop_bps * 1.6, 4),
            "trailing_distance_bps": round(stop_bps * 1.0, 4),
        }
