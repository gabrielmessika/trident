from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.hyperliquid.private_state import ExchangeFill, ExchangeFundingPayment


def close_fills_for_trade(
    trade: object,
    fills: list[dict[str, object]],
) -> list[dict[str, object]]:
    symbol = str(getattr(trade, "symbol", "")).upper()
    if not symbol:
        return []
    return [
        dict(fill)
        for fill in fills
        if str(fill.get("symbol", "")).upper() == symbol
        and str(fill.get("action", "")).lower() == "close"
    ]


def funding_payments_for_symbol(
    payments: list[ExchangeFundingPayment],
    *,
    symbol: str,
) -> list[ExchangeFundingPayment]:
    normalized = str(symbol).upper()
    return [
        payment
        for payment in payments
        if str(payment.symbol).upper() == normalized
    ]


def exchange_fill_to_close_record(
    fill: ExchangeFill,
    *,
    timestamp: str,
    close_reason: str,
    funding_payments: list[ExchangeFundingPayment] | None = None,
) -> dict[str, object]:
    notional_usd = 0.0
    if fill.size > 0 and fill.price > 0:
        notional_usd = round(float(fill.size) * float(fill.price), 6)
    funding_records = [
        _funding_payment_record(payment) for payment in (funding_payments or [])
    ]
    return {
        "symbol": fill.symbol,
        "side": fill.side,
        "action": "close",
        "price": fill.price,
        "notional_usd": notional_usd,
        "fee_usd": fill.fee_usd,
        "slippage_bps": None,
        "timestamp": timestamp,
        "oid": fill.oid,
        "cloid": None,
        "filled_size": fill.size,
        "complete": True,
        "raw_response": None,
        "exchange_fill_available": True,
        "exchange_fee_usd": fill.fee_usd,
        "exchange_closed_pnl_usd": fill.closed_pnl_usd,
        "exchange_direction": fill.direction,
        "exchange_timestamp_ms": fill.timestamp_ms,
        "fee_source": "exchange_user_fills",
        "close_reason": close_reason,
        "funding_usd": None,
        "funding_source": (
            "exchange_user_funding_history_unattributed"
            if funding_records
            else "not_collected"
        ),
        "funding_payment_count": len(funding_records),
        "funding_payments": funding_records,
        "exchange_fill": {
            "symbol": fill.symbol,
            "oid": fill.oid,
            "side": fill.side,
            "direction": fill.direction,
            "size": fill.size,
            "price": fill.price,
            "closed_pnl_usd": fill.closed_pnl_usd,
            "fee_usd": fill.fee_usd,
            "timestamp_ms": fill.timestamp_ms,
            "raw": fill.raw,
        },
    }


def enrich_trade_record_for_audit(
    trade_record: dict[str, object],
    *,
    close_fills: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    record = dict(trade_record)
    normalized_fills = [_jsonable_mapping(fill) for fill in (close_fills or [])]
    exchange_fills = [
        fill for fill in normalized_fills if bool(fill.get("exchange_fill_available"))
    ]
    funding_payments = _attributed_funding_payments(record, normalized_fills)
    funding_usd = round(
        sum(float(payment.get("amount_usd") or 0.0) for payment in funding_payments),
        8,
    )
    exchange_fee_usd = round(
        sum(float(fill.get("exchange_fee_usd") or fill.get("fee_usd") or 0.0) for fill in exchange_fills),
        8,
    )
    exchange_closed_pnl_usd = round(
        sum(float(fill.get("exchange_closed_pnl_usd") or 0.0) for fill in exchange_fills),
        8,
    )
    record["close_fills"] = normalized_fills
    record["close_fill_count"] = len(normalized_fills)
    record["exchange_close_fill_count"] = len(exchange_fills)
    record["exchange_fee_usd"] = exchange_fee_usd if exchange_fills else None
    record["exchange_closed_pnl_usd"] = exchange_closed_pnl_usd if exchange_fills else None
    record["fee_source"] = (
        "exchange_user_fills"
        if exchange_fills
        else str(record.get("fee_source") or "portfolio_fill")
    )
    record["funding_payments"] = funding_payments
    record["funding_payment_count"] = len(funding_payments)
    if funding_payments:
        record["funding_usd"] = funding_usd
        record["funding_source"] = "exchange_user_funding_history"
    else:
        record.setdefault("funding_usd", None)
        record["funding_source"] = str(record.get("funding_source") or "not_collected")
    return record


def _funding_payment_record(payment: ExchangeFundingPayment) -> dict[str, object]:
    return {
        "symbol": payment.symbol,
        "amount_usd": payment.amount_usd,
        "funding_rate": payment.funding_rate,
        "size": payment.size,
        "timestamp_ms": payment.timestamp_ms,
        "hash": payment.hash,
        "raw": payment.raw,
    }


def _attributed_funding_payments(
    trade_record: dict[str, object],
    close_fills: list[dict[str, object]],
) -> list[dict[str, object]]:
    opened_ms = _timestamp_ms(trade_record.get("opened_at"))
    closed_ms = _timestamp_ms(trade_record.get("closed_at"))
    symbol = str(trade_record.get("symbol") or "").upper()
    if opened_ms is None or closed_ms is None or not symbol:
        return []
    by_identity: dict[tuple[str, int, str], dict[str, object]] = {}
    for fill in close_fills:
        raw_payments = fill.get("funding_payments") or []
        if not isinstance(raw_payments, list):
            continue
        for raw_payment in raw_payments:
            if not isinstance(raw_payment, dict):
                continue
            payment = _jsonable_mapping(raw_payment)
            payment_symbol = str(payment.get("symbol") or "").upper()
            payment_ms = _int_or_none(payment.get("timestamp_ms"))
            if payment_symbol != symbol or payment_ms is None:
                continue
            if payment_ms < opened_ms or payment_ms > closed_ms:
                continue
            identity = (
                payment_symbol,
                payment_ms,
                str(payment.get("hash") or payment.get("amount_usd") or ""),
            )
            by_identity[identity] = payment
    return [
        by_identity[key]
        for key in sorted(by_identity, key=lambda item: (item[1], item[2]))
    ]


def _timestamp_ms(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    if text.isdigit():
        return int(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _jsonable_mapping(value: dict[str, object]) -> dict[str, object]:
    return {str(key): _jsonable(item) for key, item in value.items()}


def _jsonable(value: Any) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value
