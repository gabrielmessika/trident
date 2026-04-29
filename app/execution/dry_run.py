from __future__ import annotations

from dataclasses import dataclass

from app.settings import ExecutionConfig


@dataclass(slots=True)
class DryRunFill:
    symbol: str
    side: str
    action: str
    price: float
    notional_usd: float
    fee_usd: float
    slippage_bps: float
    timestamp: str | None


class DryRunExecutionVenue:
    """Simple venue model with taker fills, spread crossing, and fees."""

    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    def open_fill(
        self,
        *,
        symbol: str,
        side: str,
        mid_price: float,
        spread_bps: float,
        notional_usd: float,
        timestamp: str | None,
        plan: object | None = None,
    ) -> DryRunFill:
        return self._build_fill(
            symbol=symbol,
            side=side,
            action="open",
            mid_price=mid_price,
            spread_bps=spread_bps,
            notional_usd=notional_usd,
            timestamp=timestamp,
        )

    def close_fill(
        self,
        *,
        symbol: str,
        side: str,
        mid_price: float,
        spread_bps: float,
        notional_usd: float,
        timestamp: str | None,
        plan: object | None = None,
    ) -> DryRunFill:
        return self._build_fill(
            symbol=symbol,
            side=side,
            action="close",
            mid_price=mid_price,
            spread_bps=spread_bps,
            notional_usd=notional_usd,
            timestamp=timestamp,
        )

    def _build_fill(
        self,
        *,
        symbol: str,
        side: str,
        action: str,
        mid_price: float,
        spread_bps: float,
        notional_usd: float,
        timestamp: str | None,
    ) -> DryRunFill:
        half_spread_bps = max(spread_bps, 0.0) * self.config.dry_run_spread_multiplier
        impact_bps = half_spread_bps + self.config.dry_run_slippage_bps
        signed_impact = self._signed_impact_bps(action=action, side=side, impact_bps=impact_bps)
        price = round(mid_price * (1 + signed_impact / 10_000.0), 8)
        fee_usd = round(notional_usd * self.config.dry_run_taker_fee_bps / 10_000.0, 6)
        return DryRunFill(
            symbol=symbol,
            side=side,
            action=action,
            price=price,
            notional_usd=round(notional_usd, 6),
            fee_usd=fee_usd,
            slippage_bps=round(impact_bps, 4),
            timestamp=timestamp,
        )

    def _signed_impact_bps(self, *, action: str, side: str, impact_bps: float) -> float:
        if side == "long":
            return impact_bps if action == "open" else -impact_bps
        return -impact_bps if action == "open" else impact_bps
