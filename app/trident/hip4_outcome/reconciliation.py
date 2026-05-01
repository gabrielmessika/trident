from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.trident.hip4_outcome.client import HIP4OutcomeInfoClient
from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import OutcomePosition, utc_now_iso


@dataclass(slots=True)
class OutcomeBalance:
    coin: str
    total: Decimal = Decimal("0")
    hold: Decimal = Decimal("0")
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> Decimal:
        return self.total - self.hold

    def to_dict(self) -> dict[str, Any]:
        return {
            "coin": self.coin,
            "total": str(self.total),
            "hold": str(self.hold),
            "available": str(self.available),
            "raw": self.raw,
        }


def parse_spot_balances(payload: object) -> dict[str, OutcomeBalance]:
    balances: dict[str, OutcomeBalance] = {}
    for item in _iter_balance_items(payload):
        if not isinstance(item, dict):
            continue
        coin = _extract_coin(item)
        if not coin:
            continue
        balances[coin] = OutcomeBalance(
            coin=coin,
            total=_decimal_from_any(
                item.get("total", item.get("balance", item.get("sz", item.get("amount", "0"))))
            ),
            hold=_decimal_from_any(item.get("hold", item.get("reserved", "0"))),
            raw=dict(item),
        )
    return balances


def parse_user_fills(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_fills = payload.get("fills", payload.get("data", []))
    else:
        raw_fills = payload
    if not isinstance(raw_fills, list):
        return []

    fills: list[dict[str, Any]] = []
    for item in raw_fills:
        if not isinstance(item, dict):
            continue
        coin = _extract_coin(item)
        oid = item.get("oid", item.get("orderId"))
        cloid = item.get("cloid", item.get("clientOrderId"))
        px = _float_from_any(item.get("px", item.get("price")))
        sz = _decimal_from_any(item.get("sz", item.get("size", item.get("qty", "0"))))
        fills.append(
            {
                "coin": coin,
                "oid": None if oid in (None, "") else str(oid),
                "cloid": None if cloid in (None, "") else str(cloid),
                "side": str(item.get("side", item.get("dir", ""))),
                "px": px,
                "sz": str(sz),
                "fee": str(_decimal_from_any(item.get("fee", "0"))),
                "time": _int_from_any(item.get("time", item.get("timestamp", 0))),
                "raw": dict(item),
            }
        )
    return fills


class OutcomeReconciler:
    def __init__(self, config: Hip4OutcomeConfig, info_client: HIP4OutcomeInfoClient) -> None:
        self.config = config
        self.info_client = info_client

    def reconcile(
        self,
        *,
        account_address: str,
        positions: list[OutcomePosition],
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        start_ms = start_time_ms or int(
            now_ms - max(self.config.fills_lookback_hours, 0.0) * 60.0 * 60.0 * 1000.0
        )
        end_ms = end_time_ms or now_ms
        spot_payload = self.info_client.fetch_spot_state(account_address)
        fills_payload = self.info_client.fetch_user_fills_by_time(
            user=account_address,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            aggregate_by_time=False,
        )
        balances = parse_spot_balances(spot_payload)
        recent_fills = parse_user_fills(fills_payload)
        tracked_coins = _tracked_coins(positions)
        position_reports = [
            _position_report(position, recent_fills=recent_fills, balances=balances)
            for position in positions
        ]
        matched_count = sum(int(report["matched_fill_count"]) for report in position_reports)
        tracked_balances = {
            coin: balance.to_dict()
            for coin, balance in sorted(balances.items())
            if coin in tracked_coins
        }
        unknown_outcome_balances = {
            coin: balance.to_dict()
            for coin, balance in sorted(balances.items())
            if _looks_like_outcome_coin(coin)
            and coin not in tracked_coins
            and balance.total != 0
        }
        return {
            "ts": utc_now_iso(),
            "account_address": account_address,
            "start_time_ms": start_ms,
            "end_time_ms": end_ms,
            "tracked_coin_count": len(tracked_coins),
            "tracked_balances": tracked_balances,
            "unknown_outcome_balances": unknown_outcome_balances,
            "recent_fill_count": len(recent_fills),
            "matched_fill_count": matched_count,
            "open_position_count": len([position for position in positions if position.status == "open"]),
            "positions": position_reports,
        }


def apply_reconciliation_to_positions(
    positions: list[OutcomePosition],
    report: dict[str, Any],
) -> bool:
    changed = False
    reports_by_id = {
        str(item.get("position_id")): item
        for item in report.get("positions", [])
        if isinstance(item, dict)
    }
    for position in positions:
        position_report = reports_by_id.get(position.position_id)
        if position_report is None:
            continue
        current = position.metadata.get("last_reconciliation")
        summary = {
            "ts": report.get("ts"),
            "account_address": report.get("account_address"),
            "matched_fill_count": position_report.get("matched_fill_count", 0),
            "expected_fill_count": position_report.get("expected_fill_count", 0),
            "exchange_confirmed": bool(position_report.get("exchange_confirmed")),
            "balances": position_report.get("balances", {}),
        }
        if current != summary:
            position.metadata["last_reconciliation"] = summary
            changed = True
    return changed


def _position_report(
    position: OutcomePosition,
    *,
    recent_fills: list[dict[str, Any]],
    balances: dict[str, OutcomeBalance],
) -> dict[str, Any]:
    expected = [
        fill
        for fill in position.fills
        if fill.token_qty > 0 and (fill.oid is not None or fill.cloid)
    ]
    matched: list[dict[str, Any]] = []
    for fill in expected:
        match = _find_matching_exchange_fill(fill.oid, fill.cloid, fill.coin, recent_fills)
        if match is not None:
            matched.append(match)
    coins = sorted({fill.coin for fill in position.fills if fill.coin})
    position_balances: dict[str, dict[str, Any]] = {}
    for coin in coins:
        for balance_coin in _outcome_coin_aliases(coin):
            if balance_coin in balances:
                position_balances[balance_coin] = balances[balance_coin].to_dict()
                break
    return {
        "position_id": position.position_id,
        "market_id": position.market_id,
        "underlying": position.underlying,
        "side": position.side,
        "expected_fill_count": len(expected),
        "matched_fill_count": len(matched),
        "exchange_confirmed": len(expected) > 0 and len(matched) == len(expected),
        "balances": position_balances,
        "matched_fills": matched,
    }


def _find_matching_exchange_fill(
    oid: int | None,
    cloid: str | None,
    coin: str,
    recent_fills: list[dict[str, Any]],
) -> dict[str, Any] | None:
    oid_str = None if oid is None else str(oid)
    cloid_str = None if not cloid else str(cloid)
    allowed_coins = _outcome_coin_aliases(coin)
    allowed_coins.add("")
    for fill in recent_fills:
        if fill.get("coin") not in allowed_coins:
            continue
        if oid_str and fill.get("oid") == oid_str:
            return fill
        if cloid_str and fill.get("cloid") == cloid_str:
            return fill
    return None


def _tracked_coins(positions: list[OutcomePosition]) -> set[str]:
    coins: set[str] = set()
    for position in positions:
        for fill in position.fills:
            if fill.coin:
                coins.update(_outcome_coin_aliases(fill.coin))
    return coins


def _iter_balance_items(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("balances", "spotBalances", "tokens", "assetPositions"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _extract_coin(item: dict[str, Any]) -> str:
    for key in ("coin", "token", "name", "symbol"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _looks_like_outcome_coin(coin: str) -> bool:
    if coin.startswith("#") or coin.startswith("+"):
        return True
    if coin.startswith("@"):
        return _int_from_any(coin[1:]) >= 100_000_000
    return coin.isdigit() and _int_from_any(coin) >= 100_000_000


def _outcome_coin_aliases(coin: str) -> set[str]:
    aliases = {coin}
    encoding = _outcome_encoding_from_coin(coin)
    if encoding is None:
        return aliases
    asset_id = 100_000_000 + encoding
    aliases.update({f"#{encoding}", f"+{encoding}", f"@{asset_id}", str(asset_id)})
    return aliases


def _outcome_encoding_from_coin(coin: str) -> int | None:
    if coin.startswith("#"):
        return _int_or_none(coin[1:])
    if coin.startswith("+"):
        return _int_or_none(coin[1:])
    if coin.startswith("@"):
        asset_id = _int_or_none(coin[1:])
        if asset_id is not None and asset_id >= 100_000_000:
            return asset_id - 100_000_000
    if coin.isdigit():
        asset_id = _int_or_none(coin)
        if asset_id is not None and asset_id >= 100_000_000:
            return asset_id - 100_000_000
    return None


def _decimal_from_any(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _float_from_any(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int_from_any(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
