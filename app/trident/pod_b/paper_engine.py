from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.settings import PodBConfig
from app.trident.pod_b.models import (
    PassivbotFill,
    PassivbotInventory,
    PassivbotOrder,
    PassivbotPosition,
    PassivbotStatus,
)
from app.trident.types import SymbolMarketSnapshot


@dataclass(slots=True)
class PaperPositionState:
    signed_size: float = 0.0
    avg_entry_price: float = 0.0


@dataclass(slots=True)
class PodBPaperEngine:
    managed_symbols: list[str]
    target_usd: float
    config: PodBConfig
    positions_by_symbol: dict[str, PaperPositionState] = field(default_factory=dict)
    open_orders_by_symbol: dict[str, list[PassivbotOrder]] = field(default_factory=dict)
    recent_fills: list[PassivbotFill] = field(default_factory=list)
    total_fill_count: int = 0
    realized_pnl_usd: float = 0.0
    last_mark_price_by_symbol: dict[str, float] = field(default_factory=dict)

    def update_allocation(self, *, managed_symbols: list[str], target_usd: float) -> None:
        managed = [symbol.upper() for symbol in managed_symbols]
        previous = set(self.managed_symbols)
        current = set(managed)
        removed_symbols = previous - current
        for symbol in removed_symbols:
            self.positions_by_symbol.pop(symbol, None)
            self.open_orders_by_symbol.pop(symbol, None)
            self.last_mark_price_by_symbol.pop(symbol, None)
        self.managed_symbols = managed
        self.target_usd = target_usd

    def process_record(
        self,
        *,
        timestamp: str | None,
        snapshots: list[SymbolMarketSnapshot],
        status_meta: dict[str, object],
        regime_snapshot: dict[str, object] | None = None,
        last_sync_reason: str = "paper_runner_tick",
    ) -> tuple[PassivbotStatus, list[PassivbotFill]]:
        relevant_snapshots = [
            snapshot for snapshot in snapshots if snapshot.symbol in self.managed_symbols
        ]
        fills: list[PassivbotFill] = []

        for snapshot in relevant_snapshots:
            self.last_mark_price_by_symbol[snapshot.symbol] = snapshot.price
            fills.extend(self._execute_resting_orders(snapshot=snapshot, timestamp=timestamp))

        for snapshot in relevant_snapshots:
            self.open_orders_by_symbol[snapshot.symbol] = self._build_quotes(
                snapshot,
                regime_snapshot=regime_snapshot,
            )

        if fills:
            self.recent_fills.extend(fills)
            self.recent_fills = self.recent_fills[-self.config.paper_recent_fills_limit :]
            self.total_fill_count += len(fills)

        return self.build_status(
            process_state="running",
            last_sync_reason=last_sync_reason,
            status_meta=status_meta,
        ), fills

    def build_status(
        self,
        *,
        process_state: str,
        last_sync_reason: str,
        status_meta: dict[str, object],
    ) -> PassivbotStatus:
        positions = self._positions()
        open_orders = self._open_orders()
        inventory = self._inventory(positions=positions, open_orders=open_orders)
        return PassivbotStatus(
            enabled=True,
            process_state=process_state,
            managed_symbols=self.managed_symbols,
            config_path=str(status_meta.get("config_path", "")),
            status_path=str(status_meta.get("status_path", "")),
            target_usd=self.target_usd,
            last_sync_reason=last_sync_reason,
            leverage=(
                float(status_meta["leverage"])
                if status_meta.get("leverage") not in (None, "")
                else None
            ),
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            pid=(
                int(status_meta["pid"])
                if status_meta.get("pid") not in (None, "")
                else None
            ),
            launch_command=list(status_meta.get("launch_command", [])),
            stdout_path=str(status_meta.get("stdout_path", "")),
            stderr_path=str(status_meta.get("stderr_path", "")),
            started_at=(
                str(status_meta.get("started_at"))
                if status_meta.get("started_at") not in (None, "")
                else None
            ),
            positions=positions,
            open_orders=open_orders,
            inventory=inventory,
            recent_fills=self.recent_fills,
            total_fill_count=self.total_fill_count,
            total_position_count=len(positions),
            total_open_order_count=len(open_orders),
            realized_pnl_usd=round(self.realized_pnl_usd, 4),
            total_notional_usd=round(sum(position.notional_usd for position in positions), 4),
            total_unrealized_pnl_usd=round(
                sum(position.unrealized_pnl_usd for position in positions),
                4,
            ),
        )

    def _execute_resting_orders(
        self,
        *,
        snapshot: SymbolMarketSnapshot,
        timestamp: str | None,
    ) -> list[PassivbotFill]:
        fills: list[PassivbotFill] = []
        resting_orders = self.open_orders_by_symbol.get(snapshot.symbol, [])
        if not resting_orders:
            return fills

        for order in resting_orders:
            should_fill = (
                order.side == "buy" and snapshot.price <= order.price
            ) or (
                order.side == "sell" and snapshot.price >= order.price
            )
            if not should_fill:
                continue
            fill = PassivbotFill(
                symbol=order.symbol,
                side=order.side,
                action="fill",
                price=order.price,
                size=order.size,
                notional_usd=round(order.price * order.size, 6),
                fee_usd=round(
                    order.price
                    * order.size
                    * max(self.config.paper_maker_fee_bps, 0.0)
                    / 10_000.0,
                    6,
                ),
                timestamp=timestamp,
            )
            self._apply_fill(fill)
            fills.append(fill)

        self.open_orders_by_symbol[snapshot.symbol] = []
        return fills

    def _apply_fill(self, fill: PassivbotFill) -> None:
        delta_size = fill.size if fill.side == "buy" else -fill.size
        position = self.positions_by_symbol.setdefault(fill.symbol, PaperPositionState())
        existing_size = position.signed_size
        realized_pnl_usd = 0.0

        if existing_size == 0 or existing_size * delta_size > 0:
            new_size = existing_size + delta_size
            weighted_notional = (
                abs(existing_size) * position.avg_entry_price
                + abs(delta_size) * fill.price
            )
            position.signed_size = new_size
            position.avg_entry_price = weighted_notional / abs(new_size) if new_size else 0.0
        else:
            closing_size = min(abs(existing_size), abs(delta_size))
            if existing_size > 0:
                realized_pnl_usd = (fill.price - position.avg_entry_price) * closing_size
            else:
                realized_pnl_usd = (position.avg_entry_price - fill.price) * closing_size
            new_size = existing_size + delta_size
            if abs(new_size) < 1e-12:
                position.signed_size = 0.0
                position.avg_entry_price = 0.0
            elif existing_size * new_size > 0:
                position.signed_size = new_size
            else:
                position.signed_size = new_size
                position.avg_entry_price = fill.price

        self.realized_pnl_usd = round(
            self.realized_pnl_usd + realized_pnl_usd - fill.fee_usd,
            6,
        )
        if abs(position.signed_size) < 1e-12:
            self.positions_by_symbol.pop(fill.symbol, None)

    def _build_quotes(
        self,
        snapshot: SymbolMarketSnapshot,
        *,
        regime_snapshot: dict[str, object] | None = None,
    ) -> list[PassivbotOrder]:
        target_per_symbol = self._target_per_symbol()
        if target_per_symbol <= 0:
            return []

        signed_notional = self._signed_notional(snapshot.symbol, snapshot.price)
        abs_inventory_limit = target_per_symbol * max(
            self.config.paper_max_inventory_skew_pct,
            0.1,
        )
        toxicity = self._toxicity_score(snapshot)
        toxicity_size_discount = self._clamp(
            1.0 - toxicity * max(self.config.paper_order_size_toxicity_discount, 0.0),
            0.25,
            1.0,
        )
        order_notional = max(
            target_per_symbol * self.config.paper_order_size_pct * toxicity_size_discount,
            25.0,
        )
        base_width_bps = max(
            self.config.paper_quote_width_bps,
            snapshot.spread_bps * 8.0,
            snapshot.bucket_range_bps * self.config.paper_quote_width_bucket_multiplier,
        )
        base_width_bps *= 1.0 + toxicity * max(
            self.config.paper_quote_width_toxicity_multiplier,
            0.0,
        )
        skew_ratio = signed_notional / target_per_symbol if target_per_symbol > 0 else 0.0
        bid_width_bps = base_width_bps * self._clamp(
            1.0 + max(skew_ratio, 0.0) - max(-skew_ratio, 0.0) * 0.5,
            0.5,
            2.5,
        )
        ask_width_bps = base_width_bps * self._clamp(
            1.0 + max(-skew_ratio, 0.0) - max(skew_ratio, 0.0) * 0.5,
            0.5,
            2.5,
        )

        allow_buy = signed_notional < abs_inventory_limit
        allow_sell = signed_notional > -abs_inventory_limit
        one_sided_threshold = max(
            self.config.paper_one_sided_inventory_threshold_pct,
            0.1,
        )
        if skew_ratio >= one_sided_threshold:
            allow_buy = False
        elif skew_ratio <= -one_sided_threshold:
            allow_sell = False

        if not self._regime_allows_quoting(regime_snapshot):
            return self._unwind_only_quotes(
                snapshot=snapshot,
                signed_notional=signed_notional,
                order_notional=order_notional,
                width_bps=base_width_bps * 2.0,
            )
        if self._is_toxic_flow(snapshot):
            return self._unwind_only_quotes(
                snapshot=snapshot,
                signed_notional=signed_notional,
                order_notional=order_notional,
                width_bps=base_width_bps * 1.5,
            )

        orders: list[PassivbotOrder] = []
        if allow_buy:
            bid_price = round(snapshot.price * (1.0 - bid_width_bps / 10_000.0), 8)
            if bid_price > 0:
                orders.append(
                    PassivbotOrder(
                        symbol=snapshot.symbol,
                        side="buy",
                        price=bid_price,
                        size=round(order_notional / bid_price, 8),
                    )
                )
        if allow_sell:
            ask_price = round(snapshot.price * (1.0 + ask_width_bps / 10_000.0), 8)
            if ask_price > 0:
                orders.append(
                    PassivbotOrder(
                        symbol=snapshot.symbol,
                        side="sell",
                        price=ask_price,
                        size=round(order_notional / ask_price, 8),
                    )
                )
        return orders

    def _unwind_only_quotes(
        self,
        *,
        snapshot: SymbolMarketSnapshot,
        signed_notional: float,
        order_notional: float,
        width_bps: float,
    ) -> list[PassivbotOrder]:
        if abs(signed_notional) < 1e-9:
            return []
        if signed_notional > 0:
            ask_price = round(snapshot.price * (1.0 + width_bps / 10_000.0), 8)
            if ask_price <= 0:
                return []
            return [
                PassivbotOrder(
                    symbol=snapshot.symbol,
                    side="sell",
                    price=ask_price,
                    size=round(order_notional / ask_price, 8),
                )
            ]
        bid_price = round(snapshot.price * (1.0 - width_bps / 10_000.0), 8)
        if bid_price <= 0:
            return []
        return [
            PassivbotOrder(
                symbol=snapshot.symbol,
                side="buy",
                price=bid_price,
                size=round(order_notional / bid_price, 8),
            )
        ]

    def _regime_allows_quoting(self, regime_snapshot: dict[str, object] | None) -> bool:
        if not self.config.paper_pause_outside_range:
            return True
        if not isinstance(regime_snapshot, dict):
            return True
        if not bool(regime_snapshot.get("ready", False)):
            return False
        if bool(regime_snapshot.get("btc_impulse", False)):
            return False
        adx = self._float_value(regime_snapshot.get("adx"))
        atr_ratio = self._float_value(regime_snapshot.get("atr_ratio"))
        range_width_bps = self._float_value(regime_snapshot.get("range_width_bps"))
        structure_score = abs(self._float_value(regime_snapshot.get("structure_score")))
        return (
            adx <= self.config.paper_guard_max_adx
            and atr_ratio <= self.config.paper_guard_max_atr_ratio
            and structure_score <= self.config.paper_guard_max_abs_structure_score
            and range_width_bps <= self.config.paper_guard_max_range_width_bps
        )

    def _toxicity_score(self, snapshot: SymbolMarketSnapshot) -> float:
        return self._clamp(
            max(
                abs(snapshot.trade_flow_bias),
                abs(snapshot.book_imbalance),
            ),
            0.0,
            1.0,
        )

    def _is_toxic_flow(self, snapshot: SymbolMarketSnapshot) -> bool:
        threshold = max(self.config.paper_flow_toxicity_threshold, 0.0)
        if threshold <= 0:
            return False
        if snapshot.trade_flow_bias >= threshold and snapshot.book_imbalance >= threshold:
            return True
        if snapshot.trade_flow_bias <= -threshold and snapshot.book_imbalance <= -threshold:
            return True
        return False

    def _float_value(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _positions(self) -> list[PassivbotPosition]:
        positions: list[PassivbotPosition] = []
        for symbol in self.managed_symbols:
            position = self.positions_by_symbol.get(symbol)
            mark_price = self.last_mark_price_by_symbol.get(symbol, 0.0)
            if position is None or abs(position.signed_size) < 1e-12 or mark_price <= 0:
                continue
            side = "long" if position.signed_size > 0 else "short"
            size = abs(position.signed_size)
            notional_usd = round(size * mark_price, 4)
            unrealized_pnl_usd = round(self._unrealized_pnl(symbol, mark_price), 4)
            positions.append(
                PassivbotPosition(
                    symbol=symbol,
                    side=side,
                    size=round(size, 8),
                    entry_price=round(position.avg_entry_price, 8),
                    mark_price=round(mark_price, 8),
                    notional_usd=notional_usd,
                    unrealized_pnl_usd=unrealized_pnl_usd,
                )
            )
        return positions

    def _open_orders(self) -> list[PassivbotOrder]:
        orders: list[PassivbotOrder] = []
        for symbol in self.managed_symbols:
            orders.extend(self.open_orders_by_symbol.get(symbol, []))
        return orders

    def _inventory(
        self,
        *,
        positions: list[PassivbotPosition],
        open_orders: list[PassivbotOrder],
    ) -> list[PassivbotInventory]:
        target_per_symbol = self._target_per_symbol()
        position_by_symbol = {position.symbol: position for position in positions}
        open_order_count_by_symbol: dict[str, int] = {}
        for order in open_orders:
            open_order_count_by_symbol[order.symbol] = (
                open_order_count_by_symbol.get(order.symbol, 0) + 1
            )
        inventory: list[PassivbotInventory] = []
        for symbol in self.managed_symbols:
            position = position_by_symbol.get(symbol)
            current_notional_usd = position.notional_usd if position is not None else 0.0
            signed_notional = self._signed_notional(
                symbol,
                self.last_mark_price_by_symbol.get(symbol, 0.0),
            )
            inventory.append(
                PassivbotInventory(
                    symbol=symbol,
                    target_notional_usd=round(target_per_symbol, 4),
                    current_notional_usd=round(current_notional_usd, 4),
                    inventory_skew_pct=round(
                        signed_notional / target_per_symbol if target_per_symbol > 0 else 0.0,
                        4,
                    ),
                    has_position=position is not None,
                    open_order_count=open_order_count_by_symbol.get(symbol, 0),
                )
            )
        return inventory

    def _target_per_symbol(self) -> float:
        if not self.managed_symbols:
            return 0.0
        return self.target_usd / len(self.managed_symbols)

    def _signed_notional(self, symbol: str, mark_price: float) -> float:
        position = self.positions_by_symbol.get(symbol)
        if position is None or mark_price <= 0:
            return 0.0
        return position.signed_size * mark_price

    def _unrealized_pnl(self, symbol: str, mark_price: float) -> float:
        position = self.positions_by_symbol.get(symbol)
        if position is None or mark_price <= 0:
            return 0.0
        if position.signed_size > 0:
            return (mark_price - position.avg_entry_price) * position.signed_size
        return (position.avg_entry_price - mark_price) * abs(position.signed_size)

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))
