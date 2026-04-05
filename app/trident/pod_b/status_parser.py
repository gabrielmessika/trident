from __future__ import annotations

import json
from pathlib import Path

from app.trident.pod_b.models import (
    PassivbotFill,
    PassivbotInventory,
    PassivbotOrder,
    PassivbotPosition,
    PassivbotStatus,
)


class PassivbotStatusParser:
    """Parses an optional local status file emitted by the Pod B wrapper process."""

    def parse(
        self,
        *,
        enabled: bool,
        config_path: str,
        status_path: str,
        target_usd: float,
        managed_symbols: list[str],
        default_reason: str,
    ) -> PassivbotStatus:
        path = Path(status_path)
        if not path.exists():
            inventory = self._build_inventory_defaults(
                managed_symbols=managed_symbols,
                target_usd=target_usd,
                positions=[],
                open_orders=[],
            )
            return PassivbotStatus(
                enabled=enabled,
                process_state="config_rendered" if enabled else "disabled",
                managed_symbols=managed_symbols,
                config_path=config_path,
                status_path=status_path,
                target_usd=target_usd,
                last_sync_reason=default_reason,
                inventory=inventory,
            )

        payload = json.loads(path.read_text(encoding="utf-8"))
        positions = [
            PassivbotPosition(
                symbol=str(item.get("symbol", "")),
                side=str(item.get("side", "flat")),
                size=float(item.get("size", 0.0)),
                entry_price=float(item.get("entry_price", 0.0)),
                mark_price=float(item.get("mark_price", 0.0)),
                notional_usd=float(item.get("notional_usd", 0.0)),
                unrealized_pnl_usd=float(item.get("unrealized_pnl_usd", 0.0)),
            )
            for item in payload.get("positions", [])
        ]
        open_orders = [
            PassivbotOrder(
                symbol=str(item.get("symbol", "")),
                side=str(item.get("side", "buy")),
                price=float(item.get("price", 0.0)),
                size=float(item.get("size", 0.0)),
                order_type=str(item.get("order_type", "maker")),
                status=str(item.get("status", "open")),
            )
            for item in payload.get("open_orders", [])
        ]
        recent_fills = [
            PassivbotFill(
                symbol=str(item.get("symbol", "")),
                side=str(item.get("side", "buy")),
                action=str(item.get("action", "fill")),
                price=float(item.get("price", 0.0)),
                size=float(item.get("size", 0.0)),
                notional_usd=float(item.get("notional_usd", 0.0)),
                fee_usd=float(item.get("fee_usd", 0.0)),
                timestamp=(
                    str(item.get("timestamp"))
                    if item.get("timestamp") not in (None, "")
                    else None
                ),
            )
            for item in payload.get("recent_fills", [])
        ]
        inventory = self._parse_inventory(
            payload=payload,
            managed_symbols=managed_symbols,
            target_usd=target_usd,
            positions=positions,
            open_orders=open_orders,
        )
        return PassivbotStatus(
            enabled=enabled,
            process_state=str(payload.get("process_state", "unknown")),
            managed_symbols=list(payload.get("managed_symbols", managed_symbols)),
            config_path=config_path,
            status_path=status_path,
            target_usd=float(payload.get("target_usd", target_usd)),
            last_sync_reason=str(payload.get("last_sync_reason", default_reason)),
            pid=(
                int(payload["pid"])
                if payload.get("pid") not in (None, "")
                else None
            ),
            launch_command=list(payload.get("launch_command", [])),
            stdout_path=str(payload.get("stdout_path", "")),
            stderr_path=str(payload.get("stderr_path", "")),
            started_at=(
                str(payload.get("started_at"))
                if payload.get("started_at") not in (None, "")
                else None
            ),
            positions=positions,
            open_orders=open_orders,
            inventory=inventory,
            recent_fills=recent_fills,
            total_fill_count=int(payload.get("total_fill_count", len(recent_fills))),
            total_position_count=int(
                payload.get("total_position_count", len(positions))
            ),
            total_open_order_count=int(
                payload.get("total_open_order_count", len(open_orders))
            ),
            realized_pnl_usd=float(payload.get("realized_pnl_usd", 0.0)),
            total_notional_usd=float(
                payload.get(
                    "total_notional_usd",
                    round(sum(position.notional_usd for position in positions), 4),
                )
            ),
            total_unrealized_pnl_usd=float(
                payload.get(
                    "total_unrealized_pnl_usd",
                    round(
                        sum(position.unrealized_pnl_usd for position in positions),
                        4,
                    ),
                )
            ),
        )

    def _parse_inventory(
        self,
        *,
        payload: dict[str, object],
        managed_symbols: list[str],
        target_usd: float,
        positions: list[PassivbotPosition],
        open_orders: list[PassivbotOrder],
    ) -> list[PassivbotInventory]:
        if "inventory" not in payload:
            return self._build_inventory_defaults(
                managed_symbols=managed_symbols,
                target_usd=target_usd,
                positions=positions,
                open_orders=open_orders,
            )
        return [
            PassivbotInventory(
                symbol=str(item.get("symbol", "")),
                target_notional_usd=float(item.get("target_notional_usd", 0.0)),
                current_notional_usd=float(item.get("current_notional_usd", 0.0)),
                inventory_skew_pct=float(item.get("inventory_skew_pct", 0.0)),
                has_position=bool(item.get("has_position", False)),
                open_order_count=int(item.get("open_order_count", 0)),
            )
            for item in payload.get("inventory", [])
        ]

    def _build_inventory_defaults(
        self,
        *,
        managed_symbols: list[str],
        target_usd: float,
        positions: list[PassivbotPosition],
        open_orders: list[PassivbotOrder],
    ) -> list[PassivbotInventory]:
        target_per_symbol = (
            round(target_usd / len(managed_symbols), 4) if managed_symbols else 0.0
        )
        position_by_symbol = {
            position.symbol: position for position in positions if position.symbol
        }
        order_counts: dict[str, int] = {}
        for order in open_orders:
            if not order.symbol:
                continue
            order_counts[order.symbol] = order_counts.get(order.symbol, 0) + 1

        inventory: list[PassivbotInventory] = []
        for symbol in managed_symbols:
            current_notional = round(
                position_by_symbol.get(symbol, PassivbotPosition(symbol, "flat", 0.0, 0.0, 0.0, 0.0)).notional_usd,
                4,
            )
            skew_pct = 0.0
            if target_per_symbol > 0:
                skew_pct = round((current_notional - target_per_symbol) / target_per_symbol, 4)
            inventory.append(
                PassivbotInventory(
                    symbol=symbol,
                    target_notional_usd=target_per_symbol,
                    current_notional_usd=current_notional,
                    inventory_skew_pct=skew_pct,
                    has_position=symbol in position_by_symbol,
                    open_order_count=order_counts.get(symbol, 0),
                )
            )
        return inventory
