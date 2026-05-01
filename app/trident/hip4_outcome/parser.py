from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.trident.hip4_outcome.models import OutcomeMarket


def parse_description_fields(description: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in description.split("|"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key:
            fields[key] = value
    return fields


def parse_expiry_ts(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    for fmt in ("%Y%m%d-%H%M", "%Y%m%d-%H%M%S"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def parse_price_binary_outcome(raw: dict[str, Any]) -> OutcomeMarket | None:
    try:
        outcome = int(raw.get("outcome"))
    except (TypeError, ValueError):
        return None

    description = str(raw.get("description", ""))
    fields = parse_description_fields(description)
    if fields.get("class") != "priceBinary":
        return None
    underlying = fields.get("underlying", "").strip().upper()
    if not underlying:
        return None
    expiry_ts = parse_expiry_ts(fields.get("expiry", ""))
    if expiry_ts is None:
        return None
    try:
        strike = float(fields.get("targetPrice", ""))
    except ValueError:
        return None
    side_specs = raw.get("sideSpecs", [])
    side_names = ("Yes", "No")
    if isinstance(side_specs, list) and len(side_specs) >= 2:
        first = side_specs[0] if isinstance(side_specs[0], dict) else {}
        second = side_specs[1] if isinstance(side_specs[1], dict) else {}
        side_names = (
            str(first.get("name", "Yes")),
            str(second.get("name", "No")),
        )
    expiry_label = datetime.fromtimestamp(expiry_ts, timezone.utc).strftime("%Y%m%d_%H%M")
    strike_label = ("%f" % strike).rstrip("0").rstrip(".")
    market_id = f"{underlying}_GT_{strike_label}_{expiry_label}"
    return OutcomeMarket(
        market_id=market_id,
        outcome=outcome,
        name=str(raw.get("name", "")),
        description=description,
        underlying=underlying,
        strike=strike,
        expiry_ts=expiry_ts,
        period=fields.get("period", ""),
        class_name=fields.get("class", "priceBinary"),
        side_names=side_names,
        raw=dict(raw),
    )


def parse_outcome_markets(payload: object, *, include_underlyings: list[str] | None = None) -> list[OutcomeMarket]:
    if not isinstance(payload, dict):
        return []
    allowed = {item.upper() for item in include_underlyings or [] if item}
    markets: list[OutcomeMarket] = []
    outcomes = payload.get("outcomes", [])
    if not isinstance(outcomes, list):
        return []
    for raw in outcomes:
        if not isinstance(raw, dict):
            continue
        market = parse_price_binary_outcome(raw)
        if market is None:
            continue
        if allowed and market.underlying not in allowed:
            continue
        markets.append(market)
    markets.sort(key=lambda item: (item.expiry_ts, item.underlying, item.outcome))
    return markets
