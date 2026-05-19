from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.hyperliquid.private_state import ExchangePosition
from app.portfolio.directional_state import OpenPosition


def exchange_entry_notional_usd(position: ExchangePosition) -> float:
    try:
        notional = abs(position.size) * Decimal(str(position.entry_price))
    except (InvalidOperation, ValueError):
        notional = Decimal("0")
    if notional > 0:
        return round(float(notional), 6)
    return round(float(position.notional_usd), 6)


def exchange_current_price(position: ExchangePosition) -> float | None:
    size = abs(position.size)
    if size <= 0 or position.notional_usd <= 0:
        return None
    try:
        price = Decimal(str(position.notional_usd)) / size
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return None
    if price <= 0:
        return None
    return round(float(price), 8)


def apply_exchange_position_to_local(
    local: OpenPosition,
    exchange_position: ExchangePosition,
) -> None:
    local.entry_price = float(exchange_position.entry_price)
    local.target_notional_usd = exchange_entry_notional_usd(exchange_position)
    local.margin_usd = float(exchange_position.margin_used_usd)
    local.effective_leverage = float(exchange_position.leverage)
    local.isolated = bool(exchange_position.isolated)
