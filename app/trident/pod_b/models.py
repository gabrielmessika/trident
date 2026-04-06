from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PassivbotConfig:
    config_path: str
    approved_coins: list[str]
    target_pct: float
    target_usd: float
    leverage: float = 3.0
    execution_delay_seconds: int = 5
    market_orders_allowed: bool = False
    time_in_force: str = "post_only"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class PassivbotPosition:
    symbol: str
    side: str
    size: float
    entry_price: float
    mark_price: float
    notional_usd: float
    unrealized_pnl_usd: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "notional_usd": self.notional_usd,
            "unrealized_pnl_usd": self.unrealized_pnl_usd,
        }


@dataclass(slots=True)
class PassivbotOrder:
    symbol: str
    side: str
    price: float
    size: float
    order_type: str = "maker"
    status: str = "open"

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "order_type": self.order_type,
            "status": self.status,
        }


@dataclass(slots=True)
class PassivbotFill:
    symbol: str
    side: str
    action: str
    price: float
    size: float
    notional_usd: float
    fee_usd: float = 0.0
    timestamp: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "action": self.action,
            "price": self.price,
            "size": self.size,
            "notional_usd": self.notional_usd,
            "fee_usd": self.fee_usd,
            "timestamp": self.timestamp,
        }


@dataclass(slots=True)
class PassivbotInventory:
    symbol: str
    target_notional_usd: float
    current_notional_usd: float
    inventory_skew_pct: float
    has_position: bool
    open_order_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "target_notional_usd": self.target_notional_usd,
            "current_notional_usd": self.current_notional_usd,
            "inventory_skew_pct": self.inventory_skew_pct,
            "has_position": self.has_position,
            "open_order_count": self.open_order_count,
        }


@dataclass(slots=True)
class PassivbotStatus:
    enabled: bool
    process_state: str
    managed_symbols: list[str]
    config_path: str
    status_path: str
    target_usd: float
    last_sync_reason: str
    leverage: float | None = None
    updated_at: str | None = None
    pid: int | None = None
    launch_command: list[str] = field(default_factory=list)
    stdout_path: str = ""
    stderr_path: str = ""
    started_at: str | None = None
    positions: list[PassivbotPosition] = field(default_factory=list)
    open_orders: list[PassivbotOrder] = field(default_factory=list)
    inventory: list[PassivbotInventory] = field(default_factory=list)
    recent_fills: list[PassivbotFill] = field(default_factory=list)
    total_fill_count: int = 0
    total_position_count: int = 0
    total_open_order_count: int = 0
    realized_pnl_usd: float = 0.0
    total_notional_usd: float = 0.0
    total_unrealized_pnl_usd: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "process_state": self.process_state,
            "managed_symbols": self.managed_symbols,
            "config_path": self.config_path,
            "status_path": self.status_path,
            "target_usd": self.target_usd,
            "last_sync_reason": self.last_sync_reason,
            "leverage": self.leverage,
            "updated_at": self.updated_at,
            "pid": self.pid,
            "launch_command": self.launch_command,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "started_at": self.started_at,
            "positions": [position.as_dict() for position in self.positions],
            "open_orders": [order.as_dict() for order in self.open_orders],
            "inventory": [item.as_dict() for item in self.inventory],
            "recent_fills": [fill.as_dict() for fill in self.recent_fills],
            "total_fill_count": self.total_fill_count,
            "total_position_count": self.total_position_count,
            "total_open_order_count": self.total_open_order_count,
            "realized_pnl_usd": self.realized_pnl_usd,
            "total_notional_usd": self.total_notional_usd,
            "total_unrealized_pnl_usd": self.total_unrealized_pnl_usd,
        }
