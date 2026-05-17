from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.parse import urlparse

from app.hyperliquid.rate_limiter import SharedRateLimiter, jitter_seconds
from app.hyperliquid.symbols import normalize_hl_symbol
from app.live.errors import (
    HyperliquidAPIError,
    HyperliquidRateLimitError,
    is_rate_limit_message,
)
from app.settings import HyperliquidConfig


def _decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(_decimal(value))
    except (OverflowError, ValueError):
        return default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def sdk_base_url_from_info_url(info_url: str) -> str:
    parsed = urlparse(info_url)
    if not parsed.scheme or not parsed.netloc:
        return "https://api.hyperliquid.xyz"
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(slots=True)
class HyperliquidCredentials:
    account_address: str
    secret_key: str | None = None
    vault_address: str | None = None
    live_confirm: str | None = None

    @classmethod
    def from_env(cls) -> "HyperliquidCredentials":
        account_address = (
            os.getenv("TRIDENT_ACCOUNT_ADDRESS")
            or os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS")
            or ""
        ).strip()
        secret_key = (
            os.getenv("TRIDENT_SECRET_KEY")
            or os.getenv("HYPERLIQUID_SECRET_KEY")
            or os.getenv("HYPERLIQUID_PRIVATE_KEY")
            or ""
        ).strip()
        vault_address = (
            os.getenv("TRIDENT_VAULT_ADDRESS")
            or os.getenv("HYPERLIQUID_VAULT_ADDRESS")
            or ""
        ).strip()
        live_confirm = (os.getenv("TRIDENT_LIVE_CONFIRM") or "").strip()
        return cls(
            account_address=account_address,
            secret_key=secret_key or None,
            vault_address=vault_address or None,
            live_confirm=live_confirm or None,
        )

    @classmethod
    def from_hip4_outcome_env(cls) -> "HyperliquidCredentials":
        account_address = (
            os.getenv("HIP4_OUTCOME_ACCOUNT_ADDRESS")
            or ""
        ).strip()
        secret_key = (
            os.getenv("HIP4_OUTCOME_SECRET_KEY")
            or ""
        ).strip()
        vault_address = (
            os.getenv("HIP4_OUTCOME_VAULT_ADDRESS")
            or ""
        ).strip()
        return cls(
            account_address=account_address,
            secret_key=secret_key or None,
            vault_address=vault_address or None,
        )

    def validate_for_readonly(self) -> list[str]:
        errors: list[str] = []
        if not self._looks_like_address(self.account_address):
            errors.append("TRIDENT_ACCOUNT_ADDRESS missing or invalid")
        return errors

    def validate_for_trading(self) -> list[str]:
        errors = self.validate_for_readonly()
        if not self.secret_key:
            errors.append("TRIDENT_SECRET_KEY missing")
        elif not (self.secret_key.startswith("0x") and len(self.secret_key) == 66):
            errors.append("TRIDENT_SECRET_KEY must be a 0x-prefixed 32-byte private key")
        if self.live_confirm != "I_UNDERSTAND_REAL_ORDERS":
            errors.append("TRIDENT_LIVE_CONFIRM must equal I_UNDERSTAND_REAL_ORDERS")
        return errors

    @staticmethod
    def _looks_like_address(value: str) -> bool:
        return value.startswith("0x") and len(value) == 42


@dataclass(slots=True)
class ExchangePosition:
    symbol: str
    side: str
    size: Decimal
    entry_price: float
    notional_usd: float
    margin_used_usd: float
    unrealized_pnl_usd: float
    leverage: float
    isolated: bool
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ExchangeOrder:
    symbol: str
    oid: int | None
    side: str
    size: Decimal
    limit_price: float
    reduce_only: bool
    is_trigger: bool
    order_type: str
    trigger_price: float | None = None
    cloid: str | None = None
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ExchangeFill:
    symbol: str
    oid: int | None
    side: str
    direction: str
    size: Decimal
    price: float
    closed_pnl_usd: float
    fee_usd: float
    timestamp_ms: int
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ExchangeAccountState:
    account_address: str
    fetched_at: str
    account_value_usd: float
    withdrawable_usd: float
    total_margin_used_usd: float
    spot_usdc_total: float | None
    spot_usdc_hold: float | None
    positions: dict[str, ExchangePosition] = field(default_factory=dict)
    open_orders: list[ExchangeOrder] = field(default_factory=list)
    frontend_open_orders: list[ExchangeOrder] = field(default_factory=list)
    recent_fills: list[ExchangeFill] = field(default_factory=list)
    raw_user_state: object | None = None
    raw_spot_state: object | None = None

    @property
    def all_orders(self) -> list[ExchangeOrder]:
        by_key: dict[tuple[str, int | None, str | None], ExchangeOrder] = {}
        for order in [*self.open_orders, *self.frontend_open_orders]:
            by_key[(order.symbol, order.oid, order.cloid)] = order
        return list(by_key.values())


@dataclass(slots=True)
class HyperliquidPrivateInfoStats:
    request_count: int = 0
    throttle_wait_count: int = 0
    throttle_wait_seconds: float = 0.0
    rate_limit_count: int = 0
    circuit_open_count: int = 0
    last_error: str | None = None


class HyperliquidPrivateInfoClient:
    """Read-only private Hyperliquid account client.

    This intentionally propagates API failures. Returning an empty list on a 429
    or timeout was one of the historical failure modes we want to avoid.
    """

    def __init__(
        self,
        config: HyperliquidConfig,
        credentials: HyperliquidCredentials,
        *,
        info_client: Any | None = None,
        now_ms_fn: Any | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        rate_limiter: Any | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self._info_client = info_client
        self._now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self.sleep_fn = sleep_fn or time.sleep
        self.stats = HyperliquidPrivateInfoStats()
        self.rate_limiter = rate_limiter or SharedRateLimiter(
            config.rate_limit_state_path,
            jitter_fn=lambda seconds: jitter_seconds(
                seconds,
                config.shared_rate_limit_jitter_seconds,
            ),
        )

    @property
    def info_client(self) -> Any:
        if self._info_client is None:
            try:
                from hyperliquid.info import Info
            except Exception as exc:  # pragma: no cover - dependency guard
                raise HyperliquidAPIError(
                    "hyperliquid-python-sdk is required for private account state"
                ) from exc
            self._info_client = Info(
                sdk_base_url_from_info_url(self.config.info_url),
                skip_ws=True,
                timeout=self.config.connect_timeout_seconds,
            )
        return self._info_client

    def fetch_account_state(
        self,
        *,
        fills_lookback_hours: float = 24.0,
        aggregate_fills_by_time: bool = False,
    ) -> ExchangeAccountState:
        errors = self.credentials.validate_for_readonly()
        if errors:
            raise HyperliquidAPIError("; ".join(errors))

        address = self.credentials.account_address
        try:
            user_state = self._call_private_info(self.info_client.user_state, address)
            spot_state = self._call_private_info(self.info_client.spot_user_state, address)
            open_orders = self._call_private_info(self.info_client.open_orders, address)
            frontend_orders = self._call_private_info(
                self.info_client.frontend_open_orders,
                address,
            )
            start_ms = self._now_ms_fn() - int(max(fills_lookback_hours, 0.0) * 3600_000)
            fills = self._call_private_info(
                self.info_client.user_fills_by_time,
                address,
                start_ms,
                aggregate_by_time=aggregate_fills_by_time,
            )
        except HyperliquidAPIError:
            raise
        except Exception as exc:
            raise HyperliquidAPIError(f"Hyperliquid private info request failed: {exc}") from exc

        return parse_account_state(
            account_address=address,
            user_state=user_state,
            spot_state=spot_state,
            open_orders=open_orders,
            frontend_open_orders=frontend_orders,
            recent_fills=fills,
        )

    def _call_private_info(
        self,
        fn: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> object:
        key = "http_private_info"
        waited = self.rate_limiter.acquire(
            key,
            capacity=max(int(self.config.private_info_requests_per_minute), 1),
            window_seconds=60.0,
            sleep_fn=self.sleep_fn,
        )
        self.stats.request_count += 1
        if waited > 0:
            self.stats.throttle_wait_count += 1
            self.stats.throttle_wait_seconds = round(
                self.stats.throttle_wait_seconds + waited,
                4,
            )
        limiter_stats = getattr(self.rate_limiter, "stats", None)
        self.stats.circuit_open_count = int(
            getattr(limiter_stats, "circuit_open_count", 0)
        )
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            message = str(exc)
            self.stats.last_error = message
            if is_rate_limit_message(message):
                self.stats.rate_limit_count += 1
                self.rate_limiter.record_rate_limit(
                    key,
                    threshold=self.config.circuit_breaker_threshold,
                    breaker_seconds=self.config.circuit_breaker_seconds,
                )
                raise HyperliquidRateLimitError(
                    f"Hyperliquid private info rate limit: {message}"
                ) from exc
            raise
        self.rate_limiter.record_success(key)
        return result


def parse_account_state(
    *,
    account_address: str,
    user_state: object,
    spot_state: object,
    open_orders: object,
    frontend_open_orders: object,
    recent_fills: object,
) -> ExchangeAccountState:
    user_state_dict = user_state if isinstance(user_state, dict) else {}
    margin_summary = user_state_dict.get("marginSummary", {})
    if not isinstance(margin_summary, dict):
        margin_summary = {}

    positions: dict[str, ExchangePosition] = {}
    raw_positions = user_state_dict.get("assetPositions", [])
    if isinstance(raw_positions, list):
        for item in raw_positions:
            position = _parse_position(item)
            if position is not None:
                positions[position.symbol] = position

    spot_total, spot_hold = _parse_spot_usdc(spot_state)
    return ExchangeAccountState(
        account_address=account_address,
        fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        account_value_usd=_float(margin_summary.get("accountValue")),
        withdrawable_usd=_float(user_state_dict.get("withdrawable")),
        total_margin_used_usd=_float(margin_summary.get("totalMarginUsed")),
        spot_usdc_total=spot_total,
        spot_usdc_hold=spot_hold,
        positions=positions,
        open_orders=_parse_orders(open_orders),
        frontend_open_orders=_parse_orders(frontend_open_orders),
        recent_fills=_parse_fills(recent_fills),
        raw_user_state=user_state,
        raw_spot_state=spot_state,
    )


def _parse_position(item: object) -> ExchangePosition | None:
    if not isinstance(item, dict):
        return None
    raw_position = item.get("position", item)
    if not isinstance(raw_position, dict):
        return None
    size = _decimal(raw_position.get("szi"))
    if size == 0:
        return None
    symbol = normalize_hl_symbol(str(raw_position.get("coin", "")))
    if not symbol:
        return None
    leverage_payload = raw_position.get("leverage", {})
    leverage_value = 1.0
    isolated = False
    if isinstance(leverage_payload, dict):
        leverage_value = _float(leverage_payload.get("value"), 1.0)
        isolated = str(leverage_payload.get("type", "")).lower() == "isolated"
    return ExchangePosition(
        symbol=symbol,
        side="long" if size > 0 else "short",
        size=size,
        entry_price=_float(raw_position.get("entryPx")),
        notional_usd=abs(_float(raw_position.get("positionValue"))),
        margin_used_usd=_float(raw_position.get("marginUsed")),
        unrealized_pnl_usd=_float(raw_position.get("unrealizedPnl")),
        leverage=leverage_value,
        isolated=isolated,
        raw=dict(raw_position),
    )


def _parse_orders(payload: object) -> list[ExchangeOrder]:
    if not isinstance(payload, list):
        return []
    parsed: list[ExchangeOrder] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = normalize_hl_symbol(str(item.get("coin", "")))
        if not symbol:
            continue
        side_raw = str(item.get("side", ""))
        side = "buy" if side_raw == "B" else "sell" if side_raw == "A" else side_raw.lower()
        order = ExchangeOrder(
            symbol=symbol,
            oid=_int(item.get("oid"), None),  # type: ignore[arg-type]
            side=side,
            size=_decimal(item.get("sz", item.get("origSz"))),
            limit_price=_float(item.get("limitPx")),
            reduce_only=bool(item.get("reduceOnly", False)),
            is_trigger=bool(item.get("isTrigger", False)),
            order_type=str(item.get("orderType", "")),
            trigger_price=(
                _float(item.get("triggerPx"))
                if item.get("triggerPx") not in (None, "", "0", "0.0")
                else None
            ),
            cloid=str(item.get("cloid")) if item.get("cloid") not in (None, "") else None,
            raw=dict(item),
        )
        parsed.append(order)
    return parsed


def _parse_fills(payload: object) -> list[ExchangeFill]:
    if not isinstance(payload, list):
        return []
    parsed: list[ExchangeFill] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = normalize_hl_symbol(str(item.get("coin", "")))
        if not symbol:
            continue
        side_raw = str(item.get("side", ""))
        parsed.append(
            ExchangeFill(
                symbol=symbol,
                oid=_int(item.get("oid"), None),  # type: ignore[arg-type]
                side="buy" if side_raw == "B" else "sell" if side_raw == "A" else side_raw.lower(),
                direction=str(item.get("dir", "")),
                size=_decimal(item.get("sz")),
                price=_float(item.get("px")),
                closed_pnl_usd=_float(item.get("closedPnl")),
                fee_usd=abs(_float(item.get("fee"))),
                timestamp_ms=_int(item.get("time")),
                raw=dict(item),
            )
        )
    return parsed


def _parse_spot_usdc(payload: object) -> tuple[float | None, float | None]:
    if not isinstance(payload, dict):
        return None, None
    balances = payload.get("balances", [])
    if not isinstance(balances, list):
        return None, None
    for item in balances:
        if not isinstance(item, dict):
            continue
        if str(item.get("coin", "")).upper() != "USDC":
            continue
        total = _float(item.get("total"), 0.0)
        hold = _float(item.get("hold"), 0.0)
        return total, hold
    return None, None
