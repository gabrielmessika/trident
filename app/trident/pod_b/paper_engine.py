"""Pod B market-making engine based on Avellaneda-Stoikov principles.

Core ideas:
1. Fair value estimation via EMA — quotes center on fair value, not spot price
2. Inventory-based mid-price skew — when long, shift mid down to attract sells
3. Volatility-adaptive spread — wider in volatile markets, tighter in calm
4. Multi-level grid — multiple bid/ask levels for deeper capture
5. Aggressive inventory reduction — hard limits + active unwind
"""

from __future__ import annotations

import math
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
class SymbolMMState:
    """Per-symbol market-making state for fair value and volatility tracking."""

    fair_value: float = 0.0
    volatility_bps: float = 25.0
    last_price: float = 0.0
    initialized: bool = False

    def update(
        self,
        price: float,
        vwap_distance_bps: float,
        *,
        fv_alpha: float,
        vol_alpha: float,
    ) -> None:
        if not self.initialized:
            self.fair_value = price
            self.last_price = price
            self.initialized = True
            return

        # Fair value: EMA of price, biased toward VWAP
        # If price is far above VWAP, fair value lags (avoids chasing)
        vwap_drag = 1.0 - min(abs(vwap_distance_bps) / 50.0, 0.5) * 0.3
        effective_alpha = fv_alpha * vwap_drag
        self.fair_value = self.fair_value * (1.0 - effective_alpha) + price * effective_alpha

        # Realized volatility: EMA of absolute returns in bps
        if self.last_price > 0:
            ret_bps = abs(price - self.last_price) / self.last_price * 10_000.0
            self.volatility_bps = max(
                self.volatility_bps * (1.0 - vol_alpha) + ret_bps * vol_alpha,
                1.0,
            )
        self.last_price = price


@dataclass(slots=True)
class PodBPaperEngine:
    managed_symbols: list[str]
    target_usd: float
    config: PodBConfig
    quoted_symbols: list[str] = field(default_factory=list)
    positions_by_symbol: dict[str, PaperPositionState] = field(default_factory=dict)
    open_orders_by_symbol: dict[str, list[PassivbotOrder]] = field(default_factory=dict)
    recent_fills: list[PassivbotFill] = field(default_factory=list)
    total_fill_count: int = 0
    realized_pnl_usd: float = 0.0
    last_mark_price_by_symbol: dict[str, float] = field(default_factory=dict)
    mm_state_by_symbol: dict[str, SymbolMMState] = field(default_factory=dict)
    parked_last_mark_price_by_symbol: dict[str, float] = field(default_factory=dict)
    parked_mm_state_by_symbol: dict[str, SymbolMMState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.managed_symbols = [symbol.upper() for symbol in self.managed_symbols]
        if not self.quoted_symbols:
            self.quoted_symbols = list(self.managed_symbols)
        else:
            self.quoted_symbols = [symbol.upper() for symbol in self.quoted_symbols]

    def update_allocation(
        self,
        *,
        managed_symbols: list[str],
        target_usd: float,
        quoted_symbols: list[str] | None = None,
    ) -> None:
        managed = [symbol.upper() for symbol in managed_symbols]
        quoted = (
            [symbol.upper() for symbol in quoted_symbols]
            if quoted_symbols is not None
            else list(managed)
        )
        quoted_set = set(quoted)
        quoted = [symbol for symbol in managed if symbol in quoted_set]
        previous = set(self.managed_symbols)
        current = set(managed)
        added_symbols = current - previous
        removed_symbols = previous - current
        for symbol in removed_symbols:
            self.positions_by_symbol.pop(symbol, None)
            self.open_orders_by_symbol.pop(symbol, None)
            last_mark = self.last_mark_price_by_symbol.pop(symbol, None)
            if last_mark is not None:
                self.parked_last_mark_price_by_symbol[symbol] = last_mark
            mm_state = self.mm_state_by_symbol.pop(symbol, None)
            if mm_state is not None:
                self.parked_mm_state_by_symbol[symbol] = mm_state
        for symbol in added_symbols:
            last_mark = self.parked_last_mark_price_by_symbol.pop(symbol, None)
            if last_mark is not None:
                self.last_mark_price_by_symbol[symbol] = last_mark
            mm_state = self.parked_mm_state_by_symbol.pop(symbol, None)
            if mm_state is not None:
                self.mm_state_by_symbol[symbol] = mm_state
        for symbol in current - set(quoted):
            self.open_orders_by_symbol[symbol] = []
        self.managed_symbols = managed
        self.quoted_symbols = quoted
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
            # Update MM state before executing orders
            mm = self.mm_state_by_symbol.setdefault(snapshot.symbol, SymbolMMState())
            mm.update(
                snapshot.price,
                snapshot.vwap_distance_bps,
                fv_alpha=self.config.paper_fair_value_ema_alpha,
                vol_alpha=self.config.paper_volatility_ema_alpha,
            )
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

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Avellaneda-Stoikov quoting
    # ------------------------------------------------------------------

    def _build_quotes(
        self,
        snapshot: SymbolMarketSnapshot,
        *,
        regime_snapshot: dict[str, object] | None = None,
    ) -> list[PassivbotOrder]:
        signed_notional = self._signed_notional(snapshot.symbol, snapshot.price)
        target_per_symbol = self._target_per_symbol()

        if snapshot.symbol not in self.quoted_symbols:
            return self._unwind_quotes(
                snapshot=snapshot,
                signed_notional=signed_notional,
                target_per_symbol=target_per_symbol,
                width_bps=max(snapshot.spread_bps * 8.0, self.config.paper_quote_width_bps),
            )
        if target_per_symbol <= 0:
            return []

        mm = self.mm_state_by_symbol.get(snapshot.symbol)
        if mm is None or not mm.initialized:
            return []

        # --- Regime / toxicity guards ---
        if not self._regime_allows_quoting(regime_snapshot):
            return self._unwind_quotes(
                snapshot=snapshot,
                signed_notional=signed_notional,
                target_per_symbol=target_per_symbol,
                width_bps=mm.volatility_bps * 2.0,
            )
        if self._is_toxic_flow(snapshot):
            return self._unwind_quotes(
                snapshot=snapshot,
                signed_notional=signed_notional,
                target_per_symbol=target_per_symbol,
                width_bps=mm.volatility_bps * 1.5,
            )

        # --- Avellaneda-Stoikov pricing ---
        reservation_price, half_spread_bps = self._compute_reservation_price(
            mm=mm,
            snapshot=snapshot,
            signed_notional=signed_notional,
            target_per_symbol=target_per_symbol,
        )
        allow_buy, allow_sell = self._compute_side_permissions(
            snapshot=snapshot,
            signed_notional=signed_notional,
            target_per_symbol=target_per_symbol,
            fair_value=mm.fair_value,
            half_spread_bps=half_spread_bps,
        )

        return self._place_grid_orders(
            snapshot=snapshot,
            reservation_price=reservation_price,
            half_spread_bps=half_spread_bps,
            target_per_symbol=target_per_symbol,
            allow_buy=allow_buy,
            allow_sell=allow_sell,
        )

    def _compute_reservation_price(
        self,
        *,
        mm: SymbolMMState,
        snapshot: SymbolMarketSnapshot,
        signed_notional: float,
        target_per_symbol: float,
    ) -> tuple[float, float]:
        """Return (reservation_price, half_spread_bps) via Avellaneda-Stoikov."""
        inventory_ratio = self._clamp(
            signed_notional / target_per_symbol if target_per_symbol > 0 else 0.0,
            -1.0,
            1.0,
        )
        skew_bps = -inventory_ratio * self.config.paper_inventory_skew_intensity * mm.volatility_bps
        reservation_price = mm.fair_value * (1.0 + skew_bps / 10_000.0)

        vol_spread_bps = mm.volatility_bps * self.config.paper_volatility_spread_multiplier
        base_spread_bps = max(
            self.config.paper_quote_width_bps,
            vol_spread_bps,
            snapshot.spread_bps * 6.0,
            snapshot.bucket_range_bps * self.config.paper_quote_width_bucket_multiplier,
        )
        base_spread_bps *= self._quote_width_multiplier(snapshot.symbol)
        half_spread_bps = self._clamp(
            base_spread_bps / 2.0,
            self.config.paper_min_spread_bps / 2.0,
            self.config.paper_max_spread_bps / 2.0,
        )
        return reservation_price, half_spread_bps

    def _compute_side_permissions(
        self,
        *,
        snapshot: SymbolMarketSnapshot,
        signed_notional: float,
        target_per_symbol: float,
        fair_value: float,
        half_spread_bps: float,
    ) -> tuple[bool, bool]:
        """Determine which sides are allowed based on inventory + trend guards."""
        abs_inventory_limit = target_per_symbol * max(
            self._inventory_skew_limit_pct(snapshot.symbol), 0.1,
        )
        allow_buy = signed_notional < abs_inventory_limit
        allow_sell = signed_notional > -abs_inventory_limit

        inventory_ratio = self._clamp(
            signed_notional / target_per_symbol if target_per_symbol > 0 else 0.0,
            -1.0, 1.0,
        )
        one_sided_threshold = max(self.config.paper_one_sided_inventory_threshold_pct, 0.1)
        if inventory_ratio >= one_sided_threshold:
            allow_buy = False
        elif inventory_ratio <= -one_sided_threshold:
            allow_sell = False

        # Trend guard: don't add to losing side when price diverges from FV
        fv_divergence_bps = (snapshot.price - fair_value) / fair_value * 10_000.0
        divergence_threshold = half_spread_bps * 0.8
        if fv_divergence_bps > divergence_threshold and inventory_ratio < -0.05:
            allow_sell = False
        elif fv_divergence_bps < -divergence_threshold and inventory_ratio > 0.05:
            allow_buy = False

        return allow_buy, allow_sell

    def _place_grid_orders(
        self,
        *,
        snapshot: SymbolMarketSnapshot,
        reservation_price: float,
        half_spread_bps: float,
        target_per_symbol: float,
        allow_buy: bool,
        allow_sell: bool,
    ) -> list[PassivbotOrder]:
        """Place multi-level grid orders around the reservation price."""
        grid_levels = max(self.config.paper_grid_levels, 1)
        spacing_mult = max(self.config.paper_grid_spacing_multiplier, 1.1)
        toxicity_size_discount = self._clamp(
            1.0 - self._toxicity_score(snapshot) * max(
                self.config.paper_order_size_toxicity_discount, 0.0,
            ),
            0.25, 1.0,
        )
        base_order_notional = max(
            target_per_symbol
            * self.config.paper_order_size_pct
            * self._order_size_multiplier(snapshot.symbol)
            * toxicity_size_discount
            / grid_levels,
            10.0,
        )

        orders: list[PassivbotOrder] = []
        for level in range(grid_levels):
            level_offset_bps = half_spread_bps * (spacing_mult ** level)
            level_notional = base_order_notional / (1.0 + level * 0.4)

            if allow_buy:
                bid_price = round(
                    reservation_price * (1.0 - level_offset_bps / 10_000.0), 8,
                )
                if bid_price > 0:
                    orders.append(PassivbotOrder(
                        symbol=snapshot.symbol, side="buy",
                        price=bid_price, size=round(level_notional / bid_price, 8),
                    ))
            if allow_sell:
                ask_price = round(
                    reservation_price * (1.0 + level_offset_bps / 10_000.0), 8,
                )
                if ask_price > 0:
                    orders.append(PassivbotOrder(
                        symbol=snapshot.symbol, side="sell",
                        price=ask_price, size=round(level_notional / ask_price, 8),
                    ))
        return orders

    def _unwind_quotes(
        self,
        *,
        snapshot: SymbolMarketSnapshot,
        signed_notional: float,
        target_per_symbol: float,
        width_bps: float,
    ) -> list[PassivbotOrder]:
        """When regime or toxicity forces us out, aggressively reduce inventory."""
        if abs(signed_notional) < 1e-9:
            return []
        unwind_basis = target_per_symbol if target_per_symbol > 0 else abs(signed_notional)
        order_notional = max(
            unwind_basis * self.config.paper_order_size_pct,
            10.0,
        )
        order_notional = min(order_notional, abs(signed_notional))
        clamped_width = max(width_bps, self.config.paper_min_spread_bps)
        if signed_notional > 0:
            ask_price = round(snapshot.price * (1.0 + clamped_width / 10_000.0), 8)
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
        bid_price = round(snapshot.price * (1.0 - clamped_width / 10_000.0), 8)
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

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Per-symbol config helpers
    # ------------------------------------------------------------------

    def _quote_width_multiplier(self, symbol: str) -> float:
        return max(
            float(self.config.paper_quote_width_multiplier_by_symbol.get(symbol.upper(), 1.0)),
            0.25,
        )

    def _order_size_multiplier(self, symbol: str) -> float:
        return max(
            float(self.config.paper_order_size_multiplier_by_symbol.get(symbol.upper(), 1.0)),
            0.1,
        )

    def _inventory_skew_limit_pct(self, symbol: str) -> float:
        return max(
            float(
                self.config.paper_max_inventory_skew_pct_by_symbol.get(
                    symbol.upper(),
                    self.config.paper_max_inventory_skew_pct,
                )
            ),
            0.1,
        )

    # ------------------------------------------------------------------
    # Position / inventory helpers
    # ------------------------------------------------------------------

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
        quoted_symbols = set(self.quoted_symbols)
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
            target_notional_usd = target_per_symbol if symbol in quoted_symbols else 0.0
            inventory.append(
                PassivbotInventory(
                    symbol=symbol,
                    target_notional_usd=round(target_notional_usd, 4),
                    current_notional_usd=round(current_notional_usd, 4),
                    inventory_skew_pct=round(
                        signed_notional / target_notional_usd if target_notional_usd > 0 else 0.0,
                        4,
                    ),
                    has_position=position is not None,
                    open_order_count=open_order_count_by_symbol.get(symbol, 0),
                )
            )
        return inventory

    def _target_per_symbol(self) -> float:
        if not self.quoted_symbols:
            return 0.0
        return self.target_usd / len(self.quoted_symbols)

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

    def _float_value(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))
