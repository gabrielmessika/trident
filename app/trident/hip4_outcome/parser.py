from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from app.trident.hip4_outcome.models import OutcomeMarket, OutcomeMarketObservation, outcome_coin


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


def parse_price_bucket_outcome(raw: dict[str, Any]) -> OutcomeMarket | None:
    try:
        outcome = int(raw.get("outcome"))
    except (TypeError, ValueError):
        return None

    description = str(raw.get("description", ""))
    fields = parse_description_fields(description)
    if fields.get("class") != "priceBucket":
        return None
    underlying = fields.get("underlying", "").strip().upper()
    if not underlying:
        return None
    expiry_ts = parse_expiry_ts(fields.get("expiry", ""))
    if expiry_ts is None:
        return None
    thresholds = _parse_price_thresholds(fields)
    bucket_index = _parse_int_field(fields, ("bucketIndex", "bucket", "rangeIndex", "index"))
    lower: float | None = None
    upper: float | None = None
    if len(thresholds) == 2:
        lower, upper = thresholds[0], thresholds[1]
        bucket_index = 0 if bucket_index is None else bucket_index
    elif bucket_index is not None and 0 <= bucket_index < len(thresholds) - 1:
        lower, upper = thresholds[bucket_index], thresholds[bucket_index + 1]
    if lower is None or upper is None or lower >= upper:
        return None
    side_names = _side_names(raw)
    expiry_label = datetime.fromtimestamp(expiry_ts, timezone.utc).strftime("%Y%m%d_%H%M")
    lower_label = _price_label(lower)
    upper_label = _price_label(upper)
    market_id = f"{underlying}_BUCKET_{lower_label}_{upper_label}_{expiry_label}"
    return OutcomeMarket(
        market_id=market_id,
        outcome=outcome,
        name=str(raw.get("name", "")),
        description=description,
        underlying=underlying,
        strike=(lower + upper) / 2.0,
        expiry_ts=expiry_ts,
        period=fields.get("period", ""),
        class_name="priceBucket",
        side_names=side_names,
        raw=dict(raw),
        thresholds=tuple(thresholds),
        bucket_lower=lower,
        bucket_upper=upper,
        bucket_index=bucket_index,
    )


def parse_known_outcome(raw: dict[str, Any]) -> OutcomeMarket | None:
    return parse_price_binary_outcome(raw) or parse_price_bucket_outcome(raw)


def parse_outcome_observation(raw: dict[str, Any]) -> OutcomeMarketObservation:
    description = str(raw.get("description", ""))
    fields = parse_description_fields(description)
    class_name = fields.get("class", "").strip()
    if not class_name:
        class_name = _infer_class_name(raw, fields)
    outcome = _parse_outcome_id(raw)
    market = parse_known_outcome(raw)
    side_names = _side_names(raw)
    coins: tuple[str, ...] = ()
    if outcome is not None:
        coins = tuple(outcome_coin(outcome, side) for side in range(min(len(side_names), 2)))
    if market is not None:
        support_status = "paper_supported" if market.class_name == "priceBucket" else "trading_supported"
        support_reason = (
            "price_bucket_paper_only"
            if market.class_name == "priceBucket"
            else "price_binary_supported"
        )
        return OutcomeMarketObservation(
            outcome=market.outcome,
            name=market.name,
            description=market.description,
            class_name=market.class_name,
            side_names=tuple(market.side_names),
            market_id=market.market_id,
            underlying=market.underlying,
            expiry_ts=market.expiry_ts,
            period=market.period,
            support_status=support_status,
            support_reason=support_reason,
            coins=coins,
            thresholds=market.thresholds,
            bucket_lower=market.bucket_lower,
            bucket_upper=market.bucket_upper,
            bucket_index=market.bucket_index,
            raw=dict(raw),
        )
    thresholds = _parse_price_thresholds(fields)
    return OutcomeMarketObservation(
        outcome=outcome,
        name=str(raw.get("name", "")),
        description=description,
        class_name=class_name,
        side_names=side_names,
        underlying=fields.get("underlying", "").strip().upper(),
        expiry_ts=parse_expiry_ts(fields.get("expiry", "")),
        period=fields.get("period", ""),
        support_status="observe_only",
        support_reason=_unsupported_reason(class_name=class_name, fields=fields),
        coins=coins,
        thresholds=tuple(thresholds),
        bucket_index=_parse_int_field(fields, ("bucketIndex", "bucket", "rangeIndex", "index")),
        raw=dict(raw),
    )


def parse_outcome_observations(payload: object) -> list[OutcomeMarketObservation]:
    if not isinstance(payload, dict):
        return []
    outcomes = payload.get("outcomes", [])
    if not isinstance(outcomes, list):
        return []
    observations: list[OutcomeMarketObservation] = []
    for raw in outcomes:
        if isinstance(raw, dict):
            observations.append(parse_outcome_observation(raw))
    return observations


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
        market = parse_known_outcome(raw)
        if market is None:
            continue
        if allowed and market.underlying not in allowed:
            continue
        markets.append(market)
    markets.sort(key=lambda item: (item.expiry_ts, item.underlying, item.outcome))
    return markets


def _side_names(raw: dict[str, Any]) -> tuple[str, str]:
    side_specs = raw.get("sideSpecs", [])
    side_names = ("Yes", "No")
    if isinstance(side_specs, list) and len(side_specs) >= 2:
        first = side_specs[0] if isinstance(side_specs[0], dict) else {}
        second = side_specs[1] if isinstance(side_specs[1], dict) else {}
        side_names = (
            str(first.get("name", "Yes")),
            str(second.get("name", "No")),
        )
    return side_names


def _parse_price_thresholds(fields: dict[str, str]) -> list[float]:
    thresholds: list[float] = []
    for lower_key, upper_key in (
        ("lowerPrice", "upperPrice"),
        ("lower", "upper"),
        ("minPrice", "maxPrice"),
        ("low", "high"),
    ):
        if lower_key in fields and upper_key in fields:
            parsed = [_parse_float(fields[lower_key]), _parse_float(fields[upper_key])]
            thresholds.extend(item for item in parsed if item is not None)
            if len(thresholds) >= 2:
                return _unique_sorted(thresholds)
    for key in (
        "thresholds",
        "priceThresholds",
        "targetPrices",
        "prices",
        "bounds",
        "range",
        "bucket",
        "targetPrice",
    ):
        value = fields.get(key)
        if value is None:
            continue
        thresholds.extend(_numbers_from_text(value))
        if len(thresholds) >= 2:
            return _unique_sorted(thresholds)
    return _unique_sorted(thresholds)


def _numbers_from_text(value: str) -> list[float]:
    parsed: list[float] = []
    for match in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value):
        try:
            parsed.append(float(match))
        except ValueError:
            continue
    return parsed


def _parse_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _unique_sorted(values: list[float]) -> list[float]:
    return sorted({float(value) for value in values if value > 0})


def _parse_int_field(fields: dict[str, str], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = fields.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except ValueError:
            continue
    return None


def _parse_outcome_id(raw: dict[str, Any]) -> int | None:
    try:
        return int(raw.get("outcome"))
    except (TypeError, ValueError):
        return None


def _price_label(value: float) -> str:
    return ("%f" % value).rstrip("0").rstrip(".")


def _infer_class_name(raw: dict[str, Any], fields: dict[str, str]) -> str:
    name = str(raw.get("name", ""))
    description = str(raw.get("description", ""))
    if "Named Outcome" in name or "Named Outcome" in description or "index" in fields:
        return "namedOutcome"
    if description.strip().lower() == "other":
        return "fallback"
    return "unknown"


def _unsupported_reason(*, class_name: str, fields: dict[str, str]) -> str:
    if class_name == "priceBucket":
        thresholds = _parse_price_thresholds(fields)
        if len(thresholds) < 2:
            return "price_bucket_missing_thresholds"
        return "price_bucket_unsupported_shape"
    if class_name == "namedOutcome":
        return "named_outcome_observation_only"
    if class_name == "fallback":
        return "fallback_outcome_observation_only"
    if not class_name:
        return "missing_outcome_class"
    return "unsupported_outcome_class"
