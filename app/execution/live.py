from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable

from app.execution.dry_run import DryRunFill
from app.hyperliquid.private_state import (
    ExchangeAccountState,
    HyperliquidCredentials,
    HyperliquidPrivateInfoClient,
    sdk_base_url_from_info_url,
)
from app.hyperliquid.rate_limiter import SharedRateLimiter, jitter_seconds
from app.live.errors import (
    HyperliquidAPIError,
    HyperliquidRateLimitError,
    is_rate_limit_message,
)
from app.settings import AppConfig
from app.trident.types import TradePlan

logger = logging.getLogger(__name__)
LIVE_EXCHANGE_ACTION_RATE_LIMIT_KEY = "exchange_action:live_order"
POST_ONLY_UPGRADE_ERROR = "only post-only orders allowed immediately after network upgrade"


@dataclass(slots=True)
class LiveOrderResult:
    status: str
    oid: int | None = None
    cloid: str | None = None
    filled_size: Decimal = Decimal("0")
    avg_price: float = 0.0
    error: str | None = None
    raw: object | None = None

    @property
    def filled(self) -> bool:
        return self.filled_size > 0 and self.avg_price > 0


@dataclass(slots=True)
class LiveExecutionFill(DryRunFill):
    oid: int | None = None
    cloid: str | None = None
    filled_size: Decimal = Decimal("0")
    complete: bool = True
    protective_oids: dict[str, int | None] = field(default_factory=dict)
    raw_response: object | None = None


class LiveExecutionVenue:
    """Hyperliquid execution venue using the official Python SDK.

    The venue uses IOC reduce-only/order calls for the initial canary profile and
    places protective reduce-only trigger orders after entry fills.
    """

    def __init__(
        self,
        config: AppConfig,
        credentials: HyperliquidCredentials,
        *,
        exchange_client: Any | None = None,
        private_info_client: HyperliquidPrivateInfoClient | None = None,
        order_rate_limiter: Any | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        errors = credentials.validate_for_trading()
        if errors:
            raise HyperliquidAPIError("; ".join(errors))
        self.config = config
        self.credentials = credentials
        self.exchange_client = exchange_client or self._build_exchange_client()
        self.private_info_client = private_info_client or HyperliquidPrivateInfoClient(
            config.hyperliquid,
            credentials,
        )
        self.sleep_fn = sleep_fn or time.sleep
        self.order_rate_limiter = order_rate_limiter or SharedRateLimiter(
            config.hyperliquid.rate_limit_state_path,
            jitter_fn=lambda seconds: jitter_seconds(
                seconds,
                config.hyperliquid.shared_rate_limit_jitter_seconds,
            ),
        )
        self.order_slippage_bps = float(
            getattr(config.trident.execution, "live_order_slippage_bps", 8.0)
        )
        self.close_slippage_bps = float(
            getattr(config.trident.execution, "live_close_slippage_bps", 12.0)
        )
        self.max_order_notional_usd = float(
            getattr(config.trident.execution, "live_max_order_notional_usd", 50.0)
        )
        self.order_actions_per_minute = int(
            getattr(config.trident.execution, "live_order_actions_per_minute", 12)
        )
        self.require_protective_orders = bool(
            getattr(config.trident.execution, "live_require_protective_orders", True)
        )
        self.post_only_retry_on_upgrade = bool(
            getattr(config.trident.execution, "live_post_only_retry_on_upgrade", False)
        )
        self.post_only_buffer_bps = float(
            getattr(config.trident.execution, "live_post_only_buffer_bps", 1.0)
        )
        self.orders_by_symbol: dict[str, dict[str, object]] = {}
        self.last_block_reason_by_symbol: dict[str, str] = {}
        self._size_decimals_by_symbol: dict[str, int] | None = None

    def _build_exchange_client(self) -> Any:
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
        except Exception as exc:  # pragma: no cover - dependency guard
            raise HyperliquidAPIError(
                "hyperliquid-python-sdk and eth-account are required for live trading"
            ) from exc
        wallet = Account.from_key(self.credentials.secret_key)
        return Exchange(
            wallet,
            sdk_base_url_from_info_url(self.config.hyperliquid.info_url),
            vault_address=self.credentials.vault_address,
            account_address=self.credentials.account_address,
            timeout=self.config.hyperliquid.connect_timeout_seconds,
        )

    def open_fill(
        self,
        *,
        symbol: str,
        side: str,
        mid_price: float,
        spread_bps: float,
        notional_usd: float,
        timestamp: str | None,
        plan: TradePlan | None = None,
    ) -> LiveExecutionFill | None:
        symbol = symbol.upper()
        if notional_usd <= 0 or mid_price <= 0:
            self.last_block_reason_by_symbol[symbol] = "invalid_notional_or_price"
            logger.warning("Live open blocked for %s: invalid notional or price", symbol)
            return None
        if notional_usd > self.max_order_notional_usd:
            self.last_block_reason_by_symbol[symbol] = (
                f"notional_above_live_cap:{notional_usd:.2f}>{self.max_order_notional_usd:.2f}"
            )
            logger.warning(
                "Live open blocked for %s: %s",
                symbol,
                self.last_block_reason_by_symbol[symbol],
            )
            return None
        state = self.private_info_client.fetch_account_state(fills_lookback_hours=2.0)
        if self._has_exchange_exposure(state, symbol):
            self.last_block_reason_by_symbol[symbol] = "exchange_position_or_order_exists"
            logger.warning("Live open blocked for %s: exchange exposure already exists", symbol)
            return None
        is_buy = side == "long"
        limit_px = self._limit_price(
            mid_price,
            side=side,
            action="open",
            slippage_bps=self.order_slippage_bps,
        )
        size = self._size_from_notional(notional_usd, limit_px, symbol=symbol)
        if size <= 0:
            self.last_block_reason_by_symbol[symbol] = "size_rounds_to_zero"
            logger.warning("Live open blocked for %s: rounded size is zero", symbol)
            return None
        cloid = self._new_cloid()
        result = self._submit_order(
            symbol=symbol,
            is_buy=is_buy,
            size=size,
            limit_px=limit_px,
            reduce_only=False,
            cloid=cloid,
        )
        if self._should_retry_post_only(result):
            limit_px = self._post_only_limit_price(
                mid_price,
                side=side,
                action="open",
                spread_bps=spread_bps,
            )
            size = self._size_from_notional(notional_usd, limit_px, symbol=symbol)
            if size <= 0:
                self.last_block_reason_by_symbol[symbol] = "post_only_size_rounds_to_zero"
                logger.warning("Live open blocked for %s: post-only rounded size is zero", symbol)
                return None
            result = self._submit_order(
                symbol=symbol,
                is_buy=is_buy,
                size=size,
                limit_px=limit_px,
                reduce_only=False,
                cloid=cloid,
                order_type={"limit": {"tif": "Alo"}},
            )
            if result.status == "resting":
                self._remember_pending_entry_order(
                    symbol=symbol,
                    side=side,
                    size=size,
                    limit_px=limit_px,
                    notional_usd=notional_usd,
                    timestamp=timestamp,
                    cloid=cloid,
                    result=result,
                    plan=plan,
                )
                self.last_block_reason_by_symbol[symbol] = "entry_order_resting_post_only"
                logger.warning(
                    "Live open resting post-only for %s: oid=%s limit_px=%s size=%s",
                    symbol,
                    result.oid,
                    limit_px,
                    size,
                )
                return None
        if not result.filled:
            self.last_block_reason_by_symbol[symbol] = result.error or result.status
            logger.warning(
                "Live open not filled for %s: status=%s error=%s raw=%s",
                symbol,
                result.status,
                result.error,
                result.raw,
            )
            return None

        actual_notional = float(result.filled_size) * result.avg_price
        fill = LiveExecutionFill(
            symbol=symbol,
            side=side,
            action="open",
            price=result.avg_price,
            notional_usd=round(actual_notional, 6),
            fee_usd=0.0,
            slippage_bps=round(abs((result.avg_price - mid_price) / mid_price) * 10_000.0, 4),
            timestamp=timestamp,
            oid=result.oid,
            cloid=result.cloid,
            filled_size=result.filled_size,
            complete=True,
            raw_response=result.raw,
        )
        if plan is not None:
            fill.protective_oids = self._place_protective_orders(
                plan=plan,
                fill=fill,
            )
        self.orders_by_symbol[symbol] = {
            "entry_oid": fill.oid,
            "entry_cloid": fill.cloid,
            "protective_oids": dict(fill.protective_oids),
            "last_open_response": result.raw,
        }
        return fill

    def close_fill(
        self,
        *,
        symbol: str,
        side: str,
        mid_price: float,
        spread_bps: float,
        notional_usd: float,
        timestamp: str | None,
        plan: TradePlan | None = None,
    ) -> LiveExecutionFill | None:
        symbol = symbol.upper()
        if notional_usd <= 0 or mid_price <= 0:
            self.last_block_reason_by_symbol[symbol] = "invalid_close_notional_or_price"
            return None
        is_buy = side == "short"
        limit_px = self._limit_price(
            mid_price,
            side=side,
            action="close",
            slippage_bps=self.close_slippage_bps,
        )
        size = self._close_size(symbol)
        if size is None:
            size = self._size_from_notional(notional_usd, limit_px, symbol=symbol)
        cloid = self._new_cloid()
        result = self._submit_order(
            symbol=symbol,
            is_buy=is_buy,
            size=size,
            limit_px=limit_px,
            reduce_only=True,
            cloid=cloid,
        )
        if not result.filled:
            self.last_block_reason_by_symbol[symbol] = result.error or result.status
            return None
        actual_notional = float(result.filled_size) * result.avg_price
        self._cancel_known_protective_orders(symbol)
        return LiveExecutionFill(
            symbol=symbol,
            side=side,
            action="close",
            price=result.avg_price,
            notional_usd=round(actual_notional, 6),
            fee_usd=0.0,
            slippage_bps=round(abs((result.avg_price - mid_price) / mid_price) * 10_000.0, 4),
            timestamp=timestamp,
            oid=result.oid,
            cloid=result.cloid,
            filled_size=result.filled_size,
            complete=self._remaining_position_size(symbol) == Decimal("0"),
            raw_response=result.raw,
        )

    def _close_size(self, symbol: str) -> float | None:
        try:
            state = self.private_info_client.fetch_account_state(fills_lookback_hours=1.0)
        except Exception as exc:
            logger.warning(
                "Live close size fallback for %s: exchange state unavailable: %s",
                symbol,
                exc,
            )
            return None
        position = state.positions.get(symbol)
        if position is None or position.size == 0:
            return None
        decimals = self._size_decimals(symbol)
        size = self._round_size(float(abs(position.size)), decimals=decimals)
        return size if size > 0 else None

    def _submit_order(
        self,
        *,
        symbol: str,
        is_buy: bool,
        size: float,
        limit_px: float,
        reduce_only: bool,
        cloid: str,
        order_type: dict[str, object] | None = None,
    ) -> LiveOrderResult:
        self._acquire_exchange_action(action="order")
        try:
            from hyperliquid.utils.types import Cloid
            raw = self.exchange_client.order(
                symbol,
                is_buy,
                size,
                limit_px,
                order_type or {"limit": {"tif": "Ioc"}},
                reduce_only=reduce_only,
                cloid=Cloid.from_str(cloid),
            )
        except Exception as exc:
            self._record_exchange_action_exception(exc)
            if is_rate_limit_message(str(exc)):
                raise HyperliquidRateLimitError(
                    f"Hyperliquid order rate limit for {symbol}: {exc}"
                ) from exc
            raise HyperliquidAPIError(f"Hyperliquid order call failed for {symbol}: {exc}") from exc
        result = parse_order_result(raw, cloid=cloid)
        if (result.error and is_rate_limit_message(result.error)) or is_rate_limit_message(
            str(raw)
        ):
            self._record_exchange_action_rate_limit()
        else:
            self._record_exchange_action_success()
        return result

    def _should_retry_post_only(self, result: LiveOrderResult) -> bool:
        return (
            self.post_only_retry_on_upgrade
            and result.status == "error"
            and POST_ONLY_UPGRADE_ERROR in str(result.error or "").lower()
        )

    def _remember_pending_entry_order(
        self,
        *,
        symbol: str,
        side: str,
        size: float,
        limit_px: float,
        notional_usd: float,
        timestamp: str | None,
        cloid: str,
        result: LiveOrderResult,
        plan: TradePlan | None,
    ) -> None:
        self.orders_by_symbol[symbol] = {
            "entry_oid": result.oid,
            "entry_cloid": result.cloid or cloid,
            "entry_status": result.status,
            "entry_order_type": {"limit": {"tif": "Alo"}},
            "entry_limit_px": limit_px,
            "entry_size": size,
            "pending_position": self._pending_position_metadata(
                symbol=symbol,
                side=side,
                notional_usd=notional_usd,
                timestamp=timestamp,
                plan=plan,
            ),
            "last_open_response": result.raw,
        }

    def _place_protective_orders(
        self,
        *,
        plan: TradePlan,
        fill: LiveExecutionFill,
    ) -> dict[str, int | None]:
        protective: dict[str, int | None] = {}
        if fill.filled_size <= 0:
            return protective
        stop_price = self._stop_price(plan, fill.price)
        if stop_price > 0:
            try:
                sl = self._submit_trigger(
                    symbol=fill.symbol,
                    side=plan.side,
                    trigger_price=stop_price,
                    size=float(fill.filled_size),
                    tpsl="sl",
                )
                protective["sl"] = sl.oid
            except HyperliquidAPIError:
                logger.exception("Protective SL trigger failed for %s", fill.symbol)
                if self.require_protective_orders:
                    logger.error(
                        "SL trigger missing after live entry; attempting emergency close for %s",
                        fill.symbol,
                    )
                    self.close_fill(
                        symbol=fill.symbol,
                        side=plan.side,
                        mid_price=fill.price,
                        spread_bps=0.0,
                        notional_usd=fill.notional_usd,
                        timestamp=fill.timestamp,
                    )
                    raise
        if plan.take_profit_bps > 0:
            tp_price = self._take_profit_price(plan, fill.price)
            try:
                tp = self._submit_trigger(
                    symbol=fill.symbol,
                    side=plan.side,
                    trigger_price=tp_price,
                    size=float(fill.filled_size),
                    tpsl="tp",
                )
                protective["tp"] = tp.oid
            except HyperliquidAPIError:
                logger.exception("Protective TP trigger failed for %s", fill.symbol)
        if self.require_protective_orders and protective.get("sl") is None:
            logger.error("SL trigger missing after live entry; attempting emergency close for %s", fill.symbol)
            self.close_fill(
                symbol=fill.symbol,
                side=plan.side,
                mid_price=fill.price,
                spread_bps=0.0,
                notional_usd=fill.notional_usd,
                timestamp=fill.timestamp,
            )
            raise HyperliquidAPIError(f"Missing SL trigger after live entry on {fill.symbol}")
        return protective

    def _submit_trigger(
        self,
        *,
        symbol: str,
        side: str,
        trigger_price: float,
        size: float,
        tpsl: str,
    ) -> LiveOrderResult:
        is_buy = side == "short"
        cloid = self._new_cloid()
        result = self._submit_order(
            symbol=symbol,
            is_buy=is_buy,
            size=size,
            limit_px=self._round_price(trigger_price),
            reduce_only=True,
            cloid=cloid,
            order_type={
                "trigger": {
                    "isMarket": True,
                    "triggerPx": self._round_price(trigger_price),
                    "tpsl": tpsl,
                }
            },
        )
        if result.error:
            raise HyperliquidAPIError(f"Protective {tpsl} trigger rejected for {symbol}: {result.error}")
        return result

    def _cancel_known_protective_orders(self, symbol: str) -> None:
        metadata = self.orders_by_symbol.get(symbol, {})
        protective = metadata.get("protective_oids", {})
        if not isinstance(protective, dict):
            return
        for oid in list(protective.values()):
            if oid is None:
                continue
            try:
                self._cancel_order(symbol, int(oid))
            except Exception as exc:
                logger.warning("Failed to cancel protective oid=%s for %s: %s", oid, symbol, exc)

    def _cancel_order(self, symbol: str, oid: int) -> object:
        self._acquire_exchange_action(action="cancel")
        try:
            raw = self.exchange_client.cancel(symbol, oid)
        except Exception as exc:
            self._record_exchange_action_exception(exc)
            if is_rate_limit_message(str(exc)):
                raise HyperliquidRateLimitError(
                    f"Hyperliquid cancel rate limit for {symbol}: {exc}"
                ) from exc
            raise
        if is_rate_limit_message(str(raw)):
            self._record_exchange_action_rate_limit()
        else:
            self._record_exchange_action_success()
        return raw

    def _acquire_exchange_action(self, *, action: str) -> None:
        waited = self.order_rate_limiter.acquire(
            LIVE_EXCHANGE_ACTION_RATE_LIMIT_KEY,
            capacity=max(int(self.order_actions_per_minute), 1),
            window_seconds=60.0,
            sleep_fn=self.sleep_fn,
        )
        if waited > 0:
            logger.warning(
                "Live exchange action throttled for %.2fs before %s",
                waited,
                action,
            )

    def _record_exchange_action_success(self) -> None:
        self.order_rate_limiter.record_success(LIVE_EXCHANGE_ACTION_RATE_LIMIT_KEY)

    def _record_exchange_action_rate_limit(self) -> None:
        self.order_rate_limiter.record_rate_limit(
            LIVE_EXCHANGE_ACTION_RATE_LIMIT_KEY,
            threshold=self.config.hyperliquid.circuit_breaker_threshold,
            breaker_seconds=self.config.hyperliquid.circuit_breaker_seconds,
        )

    def _record_exchange_action_exception(self, exc: Exception) -> None:
        if is_rate_limit_message(str(exc)):
            self._record_exchange_action_rate_limit()

    def _remaining_position_size(self, symbol: str) -> Decimal:
        try:
            state = self.private_info_client.fetch_account_state(fills_lookback_hours=1.0)
        except HyperliquidAPIError:
            return Decimal("1")
        position = state.positions.get(symbol)
        if position is None:
            return Decimal("0")
        return abs(position.size)

    def _has_exchange_exposure(self, state: ExchangeAccountState, symbol: str) -> bool:
        if symbol in state.positions:
            return True
        return any(order.symbol == symbol for order in state.all_orders)

    def _limit_price(
        self,
        mid_price: float,
        *,
        side: str,
        action: str,
        slippage_bps: float,
    ) -> float:
        sign = 1.0
        if side == "long":
            sign = 1.0 if action == "open" else -1.0
        else:
            sign = -1.0 if action == "open" else 1.0
        return self._round_price(mid_price * (1.0 + sign * slippage_bps / 10_000.0))

    def _post_only_limit_price(
        self,
        mid_price: float,
        *,
        side: str,
        action: str,
        spread_bps: float,
    ) -> float:
        is_buy = (side == "long" and action == "open") or (
            side == "short" and action == "close"
        )
        buffer_bps = max(float(spread_bps) / 2.0 + self.post_only_buffer_bps, 0.1)
        sign = -1.0 if is_buy else 1.0
        return self._round_price(mid_price * (1.0 + sign * buffer_bps / 10_000.0))

    def _pending_position_metadata(
        self,
        *,
        symbol: str,
        side: str,
        notional_usd: float,
        timestamp: str | None,
        plan: TradePlan | None,
    ) -> dict[str, object]:
        if plan is None:
            return {
                "symbol": symbol,
                "side": side,
                "setup": "live_post_only_entry",
                "confidence": 0.0,
                "target_notional_usd": notional_usd,
                "opened_at": timestamp,
            }
        return {
            "symbol": plan.symbol,
            "side": plan.side,
            "setup": plan.setup,
            "confidence": plan.confidence,
            "target_notional_usd": plan.target_notional_usd,
            "stop_bps": plan.stop_bps,
            "time_stop_hours": plan.time_stop_hours,
            "take_profit_bps": plan.take_profit_bps,
            "break_even_trigger_bps": plan.break_even_trigger_bps,
            "trailing_activation_bps": plan.trailing_activation_bps,
            "trailing_distance_bps": plan.trailing_distance_bps,
            "reentry_cooldown_minutes": plan.reentry_cooldown_minutes,
            "margin_usd": plan.margin_usd,
            "effective_leverage": plan.effective_leverage,
            "risk_budget_usd": plan.risk_budget_usd,
            "expected_loss_usd": plan.expected_loss_usd,
            "invalidation_price": plan.invalidation_price,
            "isolated": plan.isolated,
            "setup_details": dict(plan.setup_details),
            "opened_at": timestamp,
        }

    def _size_from_notional(
        self,
        notional_usd: float,
        limit_px: float,
        *,
        symbol: str,
    ) -> float:
        decimals = self._size_decimals(symbol)
        return self._round_size(notional_usd / limit_px, decimals=decimals)

    def _stop_price(self, plan: TradePlan, entry_price: float) -> float:
        if plan.invalidation_price and plan.invalidation_price > 0:
            return self._round_price(plan.invalidation_price)
        delta = plan.stop_bps / 10_000.0
        if plan.side == "long":
            return self._round_price(entry_price * (1.0 - delta))
        return self._round_price(entry_price * (1.0 + delta))

    def _take_profit_price(self, plan: TradePlan, entry_price: float) -> float:
        delta = plan.take_profit_bps / 10_000.0
        if plan.side == "long":
            return self._round_price(entry_price * (1.0 + delta))
        return self._round_price(entry_price * (1.0 - delta))

    def _new_cloid(self) -> str:
        millis = int(time.time() * 1000) & ((1 << 48) - 1)
        random_bits = secrets.randbits(80)
        return f"0x{((millis << 80) | random_bits):032x}"

    def _round_wire(self, value: float) -> float:
        return float(f"{value:.8f}")

    def _round_price(self, value: float) -> float:
        try:
            price = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return self._round_wire(value)
        if price <= 0:
            return 0.0
        significant_quantum = Decimal("1e{}".format(price.adjusted() - 4))
        rounded = price.quantize(significant_quantum, rounding=ROUND_HALF_UP)
        if rounded.as_tuple().exponent < -6:
            rounded = rounded.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return float(rounded)

    def _round_size(self, value: float, *, decimals: int) -> float:
        try:
            size = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return self._round_wire(value)
        if size <= 0:
            return 0.0
        bounded_decimals = max(0, min(int(decimals), 8))
        quantum = Decimal("1").scaleb(-bounded_decimals)
        return float(size.quantize(quantum, rounding=ROUND_DOWN))

    def _size_decimals(self, symbol: str) -> int:
        if self._size_decimals_by_symbol is None:
            self._size_decimals_by_symbol = self._load_size_decimals()
        return self._size_decimals_by_symbol.get(symbol.upper(), 8)

    def _load_size_decimals(self) -> dict[str, int]:
        try:
            info_client = self.private_info_client.info_client
            meta = info_client.meta() if hasattr(info_client, "meta") else None
        except Exception as exc:
            logger.warning("Unable to load Hyperliquid meta for size rounding: %s", exc)
            return {}
        if not isinstance(meta, dict):
            return {}
        universe = meta.get("universe", [])
        if not isinstance(universe, list):
            return {}
        decimals_by_symbol: dict[str, int] = {}
        for item in universe:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().upper()
            if not name:
                continue
            try:
                decimals_by_symbol[name] = int(item.get("szDecimals", 8))
            except (TypeError, ValueError):
                continue
        return decimals_by_symbol


def parse_order_result(raw: object, *, cloid: str | None = None) -> LiveOrderResult:
    if not isinstance(raw, dict):
        return LiveOrderResult(status="invalid_response", cloid=cloid, raw=raw)
    if str(raw.get("status", "")).lower() == "err":
        return LiveOrderResult(status="error", cloid=cloid, error=str(raw.get("response", raw)), raw=raw)
    response = raw.get("response", {})
    if not isinstance(response, dict):
        return LiveOrderResult(status=str(raw.get("status", "unknown")), cloid=cloid, raw=raw)
    data = response.get("data", {})
    if not isinstance(data, dict):
        return LiveOrderResult(status=str(response.get("type", "unknown")), cloid=cloid, raw=raw)
    statuses = data.get("statuses", [])
    if not isinstance(statuses, list) or not statuses:
        return LiveOrderResult(status="no_status", cloid=cloid, raw=raw)
    first = statuses[0]
    if not isinstance(first, dict):
        return LiveOrderResult(status="invalid_status", cloid=cloid, raw=raw)
    if "error" in first:
        return LiveOrderResult(status="error", cloid=cloid, error=str(first.get("error")), raw=raw)
    resting = first.get("resting")
    if isinstance(resting, dict):
        return LiveOrderResult(
            status="resting",
            oid=_maybe_int(resting.get("oid")),
            cloid=cloid,
            raw=raw,
        )
    filled = first.get("filled")
    if isinstance(filled, dict):
        return LiveOrderResult(
            status="filled",
            oid=_maybe_int(filled.get("oid")),
            cloid=cloid,
            filled_size=Decimal(str(filled.get("totalSz", "0"))),
            avg_price=float(filled.get("avgPx", 0.0) or 0.0),
            raw=raw,
        )
    return LiveOrderResult(status="unknown_status", cloid=cloid, raw=raw)


def _maybe_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
