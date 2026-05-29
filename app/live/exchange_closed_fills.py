from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from app.hyperliquid.private_state import ExchangeFill
from app.portfolio.directional_state import OpenPosition

MATCH_GRACE_MS = 60_000


def select_exchange_closed_fill(
    position: OpenPosition,
    recent_fills: Iterable[ExchangeFill],
    *,
    known_order_ids: set[int] | None = None,
) -> ExchangeFill | None:
    """Return a plausible exchange-side close fill for a missing local position."""

    symbol = str(position.symbol).upper()
    opened_at_ms = _timestamp_ms(position.opened_at)
    min_timestamp_ms = max(opened_at_ms - MATCH_GRACE_MS, 0) if opened_at_ms is not None else 0
    known_order_ids = known_order_ids or set()
    candidates = [
        fill
        for fill in recent_fills
        if str(fill.symbol).upper() == symbol
        and fill.price > 0
        and fill.timestamp_ms > 0
        and fill.timestamp_ms >= min_timestamp_ms
        and (
            _is_known_exit_order(fill, known_order_ids)
            or _looks_like_close_fill(fill, position)
        )
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda fill: _close_fill_score(fill, known_order_ids))


def exchange_closed_reason_for_fill(
    fill: ExchangeFill,
    *,
    known_order_roles: dict[int, str] | None = None,
) -> str:
    if fill.oid is not None and known_order_roles:
        role = known_order_roles.get(int(fill.oid))
        if role == "stop_loss":
            return "exchange_closed_stop_loss"
        if role == "take_profit":
            return "exchange_closed_take_profit"
    direction = str(fill.direction).lower()
    if "liquid" in direction:
        return "exchange_closed_liquidation"
    return "exchange_closed"


def exchange_fill_timestamp(fill: ExchangeFill) -> str:
    return (
        datetime.fromtimestamp(fill.timestamp_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def known_exit_order_roles_for_symbol(state_store: Any, symbol: str) -> dict[int, str]:
    metadata = _order_metadata_for_symbol(state_store, symbol)
    if not metadata:
        return {}
    known: dict[int, str] = {}
    for key, role in (("sl_oid", "stop_loss"), ("tp_oid", "take_profit")):
        _add_order_role(known, metadata.get(key), role)
    protective = metadata.get("protective_oids", {})
    if isinstance(protective, dict):
        for key, value in protective.items():
            role = _protective_order_role(key)
            if role is not None:
                _add_order_role(known, value, role)
    stop_grace = metadata.get("stop_grace", {})
    if isinstance(stop_grace, dict):
        for key in ("catastrophic_sl_oid", "normal_sl_oid"):
            _add_order_role(known, stop_grace.get(key), "stop_loss")
    return known


def known_exit_order_ids_for_symbol(state_store: Any, symbol: str) -> set[int]:
    metadata = _order_metadata_for_symbol(state_store, symbol)
    if not metadata:
        return set()
    known: set[int] = set()
    for key in ("sl_oid", "tp_oid"):
        _add_order_id(known, metadata.get(key))
    protective = metadata.get("protective_oids", {})
    if isinstance(protective, dict):
        for value in protective.values():
            _add_order_id(known, value)
    stop_grace = metadata.get("stop_grace", {})
    if isinstance(stop_grace, dict):
        for key in ("catastrophic_sl_oid", "normal_sl_oid"):
            _add_order_id(known, stop_grace.get(key))
    return known


def _order_metadata_for_symbol(state_store: Any, symbol: str) -> dict[str, Any] | None:
    try:
        payload = state_store.load()
    except Exception:
        return None
    orders = payload.get("orders", {}) if isinstance(payload, dict) else {}
    if not isinstance(orders, dict):
        return None
    metadata = orders.get(str(symbol).upper()) or orders.get(symbol)
    if not isinstance(metadata, dict):
        return None
    return metadata


def _timestamp_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    timestamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return int(timestamp.timestamp() * 1000)


def _looks_like_close_fill(fill: ExchangeFill, position: OpenPosition) -> bool:
    direction = str(fill.direction).lower()
    if "close" in direction:
        return True
    if abs(float(fill.closed_pnl_usd)) > 0:
        return True
    if position.side == "long":
        return fill.side == "sell"
    if position.side == "short":
        return fill.side == "buy"
    return False


def _is_known_exit_order(fill: ExchangeFill, known_order_ids: set[int]) -> bool:
    return fill.oid is not None and int(fill.oid) in known_order_ids


def _close_fill_score(fill: ExchangeFill, known_order_ids: set[int]) -> tuple[int, int, int, int]:
    return (
        1 if _is_known_exit_order(fill, known_order_ids) else 0,
        1 if "close" in str(fill.direction).lower() else 0,
        1 if abs(float(fill.closed_pnl_usd)) > 0 else 0,
        int(fill.timestamp_ms),
    )


def _protective_order_role(key: object) -> str | None:
    normalized = str(key).strip().lower()
    if normalized in {"sl", "stop", "stop_loss"}:
        return "stop_loss"
    if normalized in {"tp", "take_profit", "profit"}:
        return "take_profit"
    return None


def _add_order_role(known: dict[int, str], value: object, role: str) -> None:
    if value is None:
        return
    try:
        known[int(value)] = role
    except (TypeError, ValueError):
        return


def _add_order_id(known: set[int], value: object) -> None:
    if value is None:
        return
    try:
        known.add(int(value))
    except (TypeError, ValueError):
        return
