from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.trident.types import SymbolMarketSnapshot, TradePlan


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


@dataclass(slots=True)
class OpenPosition:
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    entry_fee_usd: float
    target_notional_usd: float
    stop_bps: float
    opened_at: datetime | None
    time_stop_hours: int
    take_profit_bps: float = 0.0
    break_even_trigger_bps: float = 0.0
    trailing_activation_bps: float = 0.0
    trailing_distance_bps: float = 0.0
    reentry_cooldown_minutes: int = 0
    margin_usd: float = 0.0
    effective_leverage: float = 1.0
    risk_budget_usd: float = 0.0
    expected_loss_usd: float = 0.0
    invalidation_price: float | None = None
    isolated: bool = True
    best_price_seen: float = 0.0


@dataclass(slots=True)
class ClosedTrade:
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    exit_price: float
    target_notional_usd: float
    stop_bps: float
    time_stop_hours: int
    take_profit_bps: float
    gross_pnl_usd: float
    fees_usd: float
    pnl_usd: float
    close_reason: str
    opened_at: datetime | None
    closed_at: datetime | None
    break_even_trigger_bps: float = 0.0
    trailing_activation_bps: float = 0.0
    trailing_distance_bps: float = 0.0
    margin_usd: float = 0.0
    effective_leverage: float = 1.0
    risk_budget_usd: float = 0.0
    expected_loss_usd: float = 0.0
    invalidation_price: float | None = None
    isolated: bool = True


@dataclass(slots=True)
class DirectionalPortfolioState:
    open_positions: dict[str, OpenPosition] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    realized_pnl_usd: float = 0.0
    last_closed_at_by_symbol: dict[str, datetime | None] = field(default_factory=dict)
    last_close_reason_by_symbol: dict[str, str] = field(default_factory=dict)

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self.open_positions

    def open_from_plan(
        self,
        plan: TradePlan,
        price: float,
        entry_fee_usd: float,
        timestamp: str | None,
    ) -> bool:
        if self.has_open_position(plan.symbol):
            return False
        self.open_positions[plan.symbol] = OpenPosition(
            symbol=plan.symbol,
            side=plan.side,
            setup=plan.setup,
            confidence=plan.confidence,
            entry_price=price,
            entry_fee_usd=entry_fee_usd,
            target_notional_usd=plan.target_notional_usd,
            stop_bps=plan.stop_bps,
            opened_at=parse_timestamp(timestamp),
            time_stop_hours=plan.time_stop_hours,
            take_profit_bps=plan.take_profit_bps,
            break_even_trigger_bps=plan.break_even_trigger_bps,
            trailing_activation_bps=plan.trailing_activation_bps,
            trailing_distance_bps=plan.trailing_distance_bps,
            reentry_cooldown_minutes=plan.reentry_cooldown_minutes,
            margin_usd=plan.margin_usd,
            effective_leverage=plan.effective_leverage,
            risk_budget_usd=plan.risk_budget_usd,
            expected_loss_usd=plan.expected_loss_usd,
            invalidation_price=plan.invalidation_price,
            isolated=plan.isolated,
            best_price_seen=price,
        )
        return True

    def close_position(
        self,
        symbol: str,
        price: float,
        exit_fee_usd: float,
        timestamp: str | None,
        reason: str,
    ) -> ClosedTrade | None:
        position = self.open_positions.pop(symbol, None)
        if position is None:
            return None

        gross_pnl_usd = self._gross_pnl_usd(position, price)
        fees_usd = round(position.entry_fee_usd + exit_fee_usd, 6)
        pnl_usd = round(gross_pnl_usd - fees_usd, 2)
        trade = ClosedTrade(
            symbol=position.symbol,
            side=position.side,
            setup=position.setup,
            confidence=position.confidence,
            entry_price=position.entry_price,
            exit_price=price,
            target_notional_usd=position.target_notional_usd,
            stop_bps=position.stop_bps,
            time_stop_hours=position.time_stop_hours,
            take_profit_bps=position.take_profit_bps,
            gross_pnl_usd=gross_pnl_usd,
            fees_usd=fees_usd,
            pnl_usd=pnl_usd,
            close_reason=reason,
            opened_at=position.opened_at,
            closed_at=parse_timestamp(timestamp),
            break_even_trigger_bps=position.break_even_trigger_bps,
            trailing_activation_bps=position.trailing_activation_bps,
            trailing_distance_bps=position.trailing_distance_bps,
            margin_usd=position.margin_usd,
            effective_leverage=position.effective_leverage,
            risk_budget_usd=position.risk_budget_usd,
            expected_loss_usd=position.expected_loss_usd,
            invalidation_price=position.invalidation_price,
            isolated=position.isolated,
        )
        self.closed_trades.append(trade)
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl_usd, 2)
        self.last_closed_at_by_symbol[symbol] = trade.closed_at
        self.last_close_reason_by_symbol[symbol] = reason
        return trade

    def _stop_hit(self, position: OpenPosition, price: float) -> bool:
        threshold = position.stop_bps / 10_000.0
        if position.side == "long":
            return price <= position.entry_price * (1 - threshold)
        return price >= position.entry_price * (1 + threshold)

    def protective_exit_reason(self, position: OpenPosition, price: float) -> str | None:
        self._update_best_price(position, price)
        favorable_bps = self._favorable_move_bps(position, price)
        best_favorable_bps = self._best_favorable_move_bps(position)

        if position.take_profit_bps > 0 and favorable_bps >= position.take_profit_bps:
            return "take_profit_hit"
        if (
            position.trailing_activation_bps > 0
            and position.trailing_distance_bps > 0
            and best_favorable_bps >= position.trailing_activation_bps
            and favorable_bps <= best_favorable_bps - position.trailing_distance_bps
        ):
            return "trailing_stop"
        if (
            position.break_even_trigger_bps > 0
            and best_favorable_bps >= position.break_even_trigger_bps
            and favorable_bps <= 0.0
        ):
            return "break_even_stop"
        if self._stop_hit(position, price):
            return "stop_hit"
        return None

    def in_reentry_cooldown(
        self,
        symbol: str,
        *,
        timestamp: str | None,
        cooldown_minutes: int,
        bypass_reasons: set[str] | None = None,
    ) -> bool:
        if cooldown_minutes <= 0 or timestamp is None:
            return False
        if self.last_close_reason_by_symbol.get(symbol) in (bypass_reasons or set()):
            return False
        last_closed_at = self.last_closed_at_by_symbol.get(symbol)
        current = parse_timestamp(timestamp)
        if last_closed_at is None or current is None:
            return False
        return (current - last_closed_at).total_seconds() < cooldown_minutes * 60

    def _time_stop_hit(self, position: OpenPosition, timestamp: str | None) -> bool:
        if position.opened_at is None or timestamp is None:
            return False
        closed_at = parse_timestamp(timestamp)
        if closed_at is None:
            return False
        age = closed_at - position.opened_at
        return age.total_seconds() >= position.time_stop_hours * 3600

    def _gross_pnl_usd(self, position: OpenPosition, exit_price: float) -> float:
        if position.side == "long":
            ret = (exit_price - position.entry_price) / position.entry_price
        else:
            ret = (position.entry_price - exit_price) / position.entry_price
        return round(position.target_notional_usd * ret, 2)

    def _update_best_price(self, position: OpenPosition, price: float) -> None:
        if position.best_price_seen <= 0:
            position.best_price_seen = price
            return
        if position.side == "long":
            position.best_price_seen = max(position.best_price_seen, price)
        else:
            position.best_price_seen = min(position.best_price_seen, price)

    def _favorable_move_bps(self, position: OpenPosition, price: float) -> float:
        if position.entry_price <= 0:
            return 0.0
        if position.side == "long":
            return ((price - position.entry_price) / position.entry_price) * 10_000.0
        return ((position.entry_price - price) / position.entry_price) * 10_000.0

    def _best_favorable_move_bps(self, position: OpenPosition) -> float:
        reference = position.best_price_seen if position.best_price_seen > 0 else position.entry_price
        return self._favorable_move_bps(position, reference)
