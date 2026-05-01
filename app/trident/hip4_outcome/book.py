from __future__ import annotations

from typing import Any

from app.trident.hip4_outcome.models import BookLevel, OutcomeOrderBook, OutcomeSideBook


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def parse_levels(raw_levels: object) -> list[BookLevel]:
    if not isinstance(raw_levels, list):
        return []
    levels: list[BookLevel] = []
    for item in raw_levels:
        if not isinstance(item, dict):
            continue
        price = _float(item.get("px"))
        size = _float(item.get("sz"))
        if price <= 0 or size <= 0:
            continue
        levels.append(BookLevel(price=price, size=size, order_count=_int(item.get("n"))))
    return levels


def depth_usdc_within_slippage(levels: list[BookLevel], *, is_ask: bool, slippage: float) -> float:
    if not levels:
        return 0.0
    best = min(level.price for level in levels) if is_ask else max(level.price for level in levels)
    if best <= 0:
        return 0.0
    if is_ask:
        limit = best * (1.0 + max(slippage, 0.0))
        selected = [level for level in levels if level.price <= limit]
    else:
        limit = best * (1.0 - max(slippage, 0.0))
        selected = [level for level in levels if level.price >= limit]
    return round(sum(level.notional_usdc for level in selected), 8)


def parse_side_book(payload: object, *, max_slippage: float = 0.03) -> OutcomeSideBook:
    raw = payload if isinstance(payload, dict) else {}
    levels = raw.get("levels", [])
    bids_raw: object = []
    asks_raw: object = []
    if isinstance(levels, list) and len(levels) >= 2:
        bids_raw = levels[0]
        asks_raw = levels[1]
    bids = parse_levels(bids_raw)
    asks = parse_levels(asks_raw)
    best_bid = max((level.price for level in bids), default=None)
    best_ask = min((level.price for level in asks), default=None)
    best_bid_size = next((level.size for level in bids if level.price == best_bid), 0.0)
    best_ask_size = next((level.size for level in asks if level.price == best_ask), 0.0)
    return OutcomeSideBook(
        coin=str(raw.get("coin", "")),
        bid=best_bid,
        ask=best_ask,
        bid_size=best_bid_size,
        ask_size=best_ask_size,
        bid_depth_usdc=depth_usdc_within_slippage(bids, is_ask=False, slippage=max_slippage),
        ask_depth_usdc=depth_usdc_within_slippage(asks, is_ask=True, slippage=max_slippage),
        time_ms=_int(raw.get("time")),
        raw=dict(raw),
    )


def build_order_book(
    *,
    market_id: str,
    yes_payload: object,
    no_payload: object,
    max_slippage: float,
) -> OutcomeOrderBook:
    return OutcomeOrderBook(
        market_id=market_id,
        yes=parse_side_book(yes_payload, max_slippage=max_slippage),
        no=parse_side_book(no_payload, max_slippage=max_slippage),
    )
