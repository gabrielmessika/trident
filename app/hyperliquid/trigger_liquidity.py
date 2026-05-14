from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.trident.trigger_liquidity.state import (
    TriggerLiquidityBook,
    TriggerLiquidityOrder,
    iter_jsonl_files,
    order_from_payload,
    parse_event_time_ms,
)


@dataclass(frozen=True, slots=True)
class TriggerLiquidityEvent:
    event_time_ms: int | None
    status: str
    order: TriggerLiquidityOrder


def iter_node_order_status_events(path: str | Path) -> Iterable[TriggerLiquidityEvent]:
    """Parses Hyperliquid node_order_statuses JSONL records into trigger events."""

    for file_path in iter_jsonl_files(path):
        with file_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    continue
                event = parse_node_order_status_event(payload)
                if event is not None:
                    yield event


def parse_node_order_status_event(
    payload: dict[str, object],
) -> TriggerLiquidityEvent | None:
    order_payload = payload.get("order")
    if not isinstance(order_payload, dict):
        return None
    event_time_ms = parse_event_time_ms(payload.get("time"))
    order = order_from_payload(
        order_payload,
        user=str(payload.get("user", "")),
        observed_at_ms=event_time_ms,
    )
    if order is None:
        return None
    return TriggerLiquidityEvent(
        event_time_ms=event_time_ms,
        status=str(payload.get("status", "open")),
        order=order,
    )


def parse_frontend_open_orders(
    payload: object,
    *,
    user: str = "",
    observed_at_ms: int | None = None,
) -> list[TriggerLiquidityOrder]:
    """Extracts active trigger orders from a frontendOpenOrders response."""

    if not isinstance(payload, list):
        return []
    orders: list[TriggerLiquidityOrder] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        order = order_from_payload(
            item,
            user=user,
            observed_at_ms=observed_at_ms,
        )
        if order is not None:
            orders.append(order)
    return orders


def build_trigger_liquidity_book_from_node(path: str | Path) -> TriggerLiquidityBook:
    book = TriggerLiquidityBook()
    events = sorted(
        iter_node_order_status_events(path),
        key=lambda event: event.event_time_ms or 0,
    )
    for event in events:
        book.apply_order_status(
            {
                "time": event.event_time_ms,
                "status": event.status,
                "user": event.order.user,
                "order": event.order.to_dict()
                | {
                    "coin": event.order.symbol,
                    "oid": event.order.oid,
                    "triggerPx": event.order.trigger_px,
                    "limitPx": event.order.limit_px,
                    "origSz": event.order.orig_sz,
                    "isTrigger": True,
                    "isPositionTpsl": event.order.is_position_tpsl,
                    "reduceOnly": event.order.reduce_only,
                    "orderType": event.order.order_type,
                    "triggerCondition": event.order.trigger_condition,
                },
            }
        )
    return book
