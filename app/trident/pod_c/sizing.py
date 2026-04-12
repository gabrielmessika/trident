from __future__ import annotations

from dataclasses import dataclass

from app.settings import AppConfig
from app.trident.pod_a.leverage import LeveragePolicy


@dataclass(slots=True)
class SizedTrade:
    margin_usd: float
    target_notional_usd: float
    requested_leverage: float
    effective_leverage: float
    risk_budget_usd: float
    expected_loss_usd: float


class PositionSizer:
    """Risk-based sizing for Pod C with bounded leverage."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._policy = LeveragePolicy(config.pod_c)

    def size_from_stop(
        self,
        *,
        symbol: str,
        margin_cap_usd: float,
        stop_bps: float,
    ) -> SizedTrade | None:
        if margin_cap_usd <= 0 or stop_bps <= 0:
            return None

        total_equity = self._config.trident.capital.reference_equity_usd
        per_trade_pct = min(
            max(self._config.pod_c.risk_per_trade_pct, 0.0),
            max(self._config.trident.risk.max_risk_per_trade_pct, 0.0),
        )
        risk_budget_usd = round(total_equity * per_trade_pct, 6)
        if risk_budget_usd <= 0:
            return None

        stop_fraction = stop_bps / 10_000.0
        desired_notional_usd = risk_budget_usd / stop_fraction
        requested_leverage = self._policy.default(symbol)
        effective_leverage = max(
            requested_leverage,
            self._policy.required_for_target(
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
        return SizedTrade(
            margin_usd=round(margin_usd, 6),
            target_notional_usd=round(target_notional_usd, 6),
            requested_leverage=round(requested_leverage, 4),
            effective_leverage=round(effective_leverage, 4),
            risk_budget_usd=round(risk_budget_usd, 6),
            expected_loss_usd=round(expected_loss_usd, 6),
        )
