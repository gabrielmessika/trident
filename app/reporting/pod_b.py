from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.trident.pod_b.models import PassivbotFill, PassivbotStatus


@dataclass(slots=True)
class PodBReport:
    records_processed: int = 0
    fills_emitted: int = 0
    total_fill_count: int = 0
    total_position_count: int = 0
    total_open_order_count: int = 0
    realized_pnl_usd: float = 0.0
    total_notional_usd: float = 0.0
    total_unrealized_pnl_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    fills_by_symbol: dict[str, int] = field(default_factory=dict)
    fills_by_date: dict[str, int] = field(default_factory=dict)
    fill_notional_by_symbol: dict[str, float] = field(default_factory=dict)
    fill_notional_by_date: dict[str, float] = field(default_factory=dict)
    realized_pnl_by_date: dict[str, float] = field(default_factory=dict)
    open_order_count_by_symbol: dict[str, int] = field(default_factory=dict)
    position_count_by_symbol: dict[str, int] = field(default_factory=dict)
    inventory_skew_by_symbol: dict[str, dict[str, float]] = field(default_factory=dict)

    _last_realized_pnl_usd: float = 0.0
    _peak_realized_pnl_usd: float = 0.0

    def add_tick(
        self,
        *,
        timestamp: str | None,
        status: PassivbotStatus,
        fills: list[PassivbotFill],
    ) -> None:
        self.records_processed += 1
        self.fills_emitted += len(fills)
        self.total_fill_count = status.total_fill_count
        self.total_position_count = status.total_position_count
        self.total_open_order_count = status.total_open_order_count
        self.realized_pnl_usd = round(status.realized_pnl_usd, 4)
        self.total_notional_usd = round(status.total_notional_usd, 4)
        self.total_unrealized_pnl_usd = round(status.total_unrealized_pnl_usd, 4)
        self._peak_realized_pnl_usd = max(self._peak_realized_pnl_usd, self.realized_pnl_usd)
        self.max_drawdown_usd = max(
            self.max_drawdown_usd,
            round(self._peak_realized_pnl_usd - self.realized_pnl_usd, 4),
        )

        date_key = (timestamp or "unknown")[:10]
        realized_delta = round(status.realized_pnl_usd - self._last_realized_pnl_usd, 4)
        if realized_delta != 0.0:
            self.realized_pnl_by_date[date_key] = round(
                self.realized_pnl_by_date.get(date_key, 0.0) + realized_delta,
                4,
            )
        self._last_realized_pnl_usd = status.realized_pnl_usd

        self.position_count_by_symbol = {
            position.symbol: 1 for position in status.positions
        }
        self.open_order_count_by_symbol = {}
        for order in status.open_orders:
            self.open_order_count_by_symbol[order.symbol] = (
                self.open_order_count_by_symbol.get(order.symbol, 0) + 1
            )
        self._update_inventory_skew(status)

        for fill in fills:
            fill_date = (fill.timestamp or date_key)[:10]
            self.fills_by_symbol[fill.symbol] = self.fills_by_symbol.get(fill.symbol, 0) + 1
            self.fills_by_date[fill_date] = self.fills_by_date.get(fill_date, 0) + 1
            self.fill_notional_by_symbol[fill.symbol] = round(
                self.fill_notional_by_symbol.get(fill.symbol, 0.0) + fill.notional_usd,
                6,
            )
            self.fill_notional_by_date[fill_date] = round(
                self.fill_notional_by_date.get(fill_date, 0.0) + fill.notional_usd,
                6,
            )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("_last_realized_pnl_usd", None)
        payload.pop("_peak_realized_pnl_usd", None)
        return payload

    def _update_inventory_skew(self, status: PassivbotStatus) -> None:
        for item in status.inventory:
            bucket = self.inventory_skew_by_symbol.setdefault(
                item.symbol,
                {
                    "last_skew_pct": 0.0,
                    "max_abs_skew_pct": 0.0,
                    "target_notional_usd": item.target_notional_usd,
                    "current_notional_usd": 0.0,
                },
            )
            bucket["last_skew_pct"] = item.inventory_skew_pct
            bucket["current_notional_usd"] = item.current_notional_usd
            bucket["target_notional_usd"] = item.target_notional_usd
            bucket["max_abs_skew_pct"] = max(
                float(bucket["max_abs_skew_pct"]),
                abs(item.inventory_skew_pct),
            )
