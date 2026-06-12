from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable

from app.execution.dry_run import DryRunFill
from app.hyperliquid.private_state import (
    ExchangeAccountState,
    ExchangeFill,
    ExchangeFundingPayment,
    HyperliquidCredentials,
    HyperliquidPrivateInfoClient,
    sdk_base_url_from_info_url,
)
from app.hyperliquid.rate_limiter import SharedRateLimiter, jitter_seconds
from app.hyperliquid.symbols import (
    group_hl_symbols_by_dex,
    normalize_hl_symbol,
    ws_subscription_symbol,
)
from app.live.errors import (
    HyperliquidAPIError,
    HyperliquidRateLimitError,
    is_rate_limit_message,
)
from app.settings import AppConfig
from app.trident.pod_a.live_risk import (
    catastrophic_stop_bps_for_plan,
    stop_grace_minutes_for_setup,
)
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
    exchange_fill_available: bool = False
    exchange_fee_usd: float | None = None
    exchange_closed_pnl_usd: float | None = None
    exchange_direction: str | None = None
    exchange_timestamp_ms: int | None = None
    fee_source: str = "unavailable"
    exchange_fill: dict[str, object] | None = None
    funding_usd: float | None = None
    funding_source: str = "not_collected"
    funding_payment_count: int = 0
    funding_payments: list[dict[str, object]] = field(default_factory=list)


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
        orders_changed_callback: Callable[[], None] | None = None,
    ) -> None:
        errors = credentials.validate_for_trading()
        if errors:
            raise HyperliquidAPIError("; ".join(errors))
        self.config = config
        self.credentials = credentials
        self._builder_perp_dexs = self._configured_builder_perp_dexs()
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
        self.stop_grace_catastrophic_sl_bps = float(
            getattr(
                config.trident.execution,
                "live_stop_grace_catastrophic_sl_bps",
                300.0,
            )
        )
        self.post_only_retry_on_upgrade = bool(
            getattr(config.trident.execution, "live_post_only_retry_on_upgrade", False)
        )
        self.post_only_buffer_bps = float(
            getattr(config.trident.execution, "live_post_only_buffer_bps", 1.0)
        )
        self.funding_lookback_hours = self._funding_lookback_hours()
        self.orders_by_symbol: dict[str, dict[str, object]] = {}
        self.orders_changed_callback = orders_changed_callback
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
        kwargs: dict[str, object] = {
            "vault_address": self.credentials.vault_address,
            "account_address": self.credentials.account_address,
            "timeout": self.config.hyperliquid.connect_timeout_seconds,
        }
        if self._builder_perp_dexs:
            kwargs["perp_dexs"] = ["", *self._builder_perp_dexs]
        return Exchange(
            wallet,
            sdk_base_url_from_info_url(self.config.hyperliquid.info_url),
            **kwargs,
        )

    def _funding_lookback_hours(self) -> float:
        candidates = [24.0]
        for pod_name in ("pod_a", "pod_c"):
            pod_config = getattr(self.config, pod_name, None)
            if pod_config is None:
                continue
            try:
                candidates.append(float(getattr(pod_config, "time_stop_hours", 0) or 0))
            except (TypeError, ValueError):
                continue
        return min(max(candidates) + 6.0, 168.0)

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
        symbol = normalize_hl_symbol(symbol)
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
        stop_grace_block = self._stop_grace_entry_block_reason(plan)
        if stop_grace_block is not None:
            self.last_block_reason_by_symbol[symbol] = stop_grace_block
            logger.warning("Live open blocked for %s: %s", symbol, stop_grace_block)
            return None
        state = self.private_info_client.fetch_account_state(fills_lookback_hours=2.0)
        if self._has_exchange_exposure(state, symbol):
            self.last_block_reason_by_symbol[symbol] = "exchange_position_or_order_exists"
            logger.warning("Live open blocked for %s: exchange exposure already exists", symbol)
            return None
        is_buy = side == "long"
        limit_px = self._limit_price(
            mid_price,
            symbol=symbol,
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
                symbol=symbol,
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
        exchange_fill = self._recent_exchange_fill_for_order(
            symbol=symbol,
            oid=result.oid,
            action="open",
        )
        exchange_metadata = self._exchange_fill_metadata(exchange_fill)
        fill = LiveExecutionFill(
            symbol=symbol,
            side=side,
            action="open",
            price=result.avg_price,
            notional_usd=round(actual_notional, 6),
            fee_usd=float(exchange_fill.fee_usd) if exchange_fill is not None else 0.0,
            slippage_bps=round(abs((result.avg_price - mid_price) / mid_price) * 10_000.0, 4),
            timestamp=timestamp,
            oid=result.oid,
            cloid=result.cloid,
            filled_size=result.filled_size,
            complete=True,
            raw_response=result.raw,
            **exchange_metadata,
        )
        initial_stop_grace_metadata = (
            self._stop_grace_metadata(plan, fill) if plan is not None else None
        )
        order_metadata: dict[str, object] = {
            "entry_oid": fill.oid,
            "entry_cloid": fill.cloid,
            "entry_filled_size": str(fill.filled_size),
            "entry_avg_price": fill.price,
            "side": side,
            "pending_position": self._pending_position_metadata(
                symbol=symbol,
                side=side,
                notional_usd=fill.notional_usd,
                timestamp=timestamp,
                plan=plan,
                entry_price=fill.price,
                entry_fee_usd=fill.fee_usd,
            ),
            "protective_oids": dict(fill.protective_oids),
            "last_open_response": result.raw,
        }
        if initial_stop_grace_metadata is not None:
            order_metadata["stop_grace"] = initial_stop_grace_metadata
        self.orders_by_symbol[symbol] = order_metadata
        self._notify_orders_changed()

        stop_grace_metadata: dict[str, object] | None = None
        if plan is not None:
            fill.protective_oids, stop_grace_metadata = self._place_protective_orders(
                plan=plan,
                fill=fill,
            )
            order_metadata["protective_oids"] = dict(fill.protective_oids)
            if stop_grace_metadata is not None:
                order_metadata["stop_grace"] = stop_grace_metadata
            self.orders_by_symbol[symbol] = order_metadata
            self._notify_orders_changed()
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
        symbol = normalize_hl_symbol(symbol)
        if notional_usd <= 0 or mid_price <= 0:
            self.last_block_reason_by_symbol[symbol] = "invalid_close_notional_or_price"
            return None
        is_buy = side == "short"
        limit_px = self._limit_price(
            mid_price,
            symbol=symbol,
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
        self._cancel_known_protective_orders(symbol)
        account_state = self._fetch_account_state_for_fill_match(include_funding=True)
        exchange_fill = self._exchange_fill_for_order(
            account_state,
            symbol=symbol,
            oid=result.oid,
            action="close",
        )
        exchange_metadata = self._exchange_fill_metadata(exchange_fill)
        funding_metadata = self._funding_metadata(account_state, symbol=symbol)
        actual_notional = (
            float(exchange_fill.size) * exchange_fill.price
            if exchange_fill is not None and exchange_fill.size > 0 and exchange_fill.price > 0
            else float(result.filled_size) * result.avg_price
        )
        remaining_size = self._remaining_position_size_from_state(
            account_state,
            symbol=symbol,
        )
        return LiveExecutionFill(
            symbol=symbol,
            side=side,
            action="close",
            price=exchange_fill.price if exchange_fill is not None else result.avg_price,
            notional_usd=round(actual_notional, 6),
            fee_usd=float(exchange_fill.fee_usd) if exchange_fill is not None else 0.0,
            slippage_bps=round(abs((result.avg_price - mid_price) / mid_price) * 10_000.0, 4),
            timestamp=timestamp,
            oid=result.oid,
            cloid=result.cloid,
            filled_size=result.filled_size,
            complete=remaining_size == Decimal("0"),
            raw_response=result.raw,
            **exchange_metadata,
            **funding_metadata,
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
        wire_symbol = self._exchange_wire_symbol(symbol)
        if wire_symbol is None:
            error = f"asset_not_resolved:{normalize_hl_symbol(symbol)}"
            logger.error("Live order blocked for %s: %s", symbol, error)
            return LiveOrderResult(status="asset_not_resolved", cloid=cloid, error=error)
        self._acquire_exchange_action(action="order")
        try:
            from hyperliquid.utils.types import Cloid
            raw = self.exchange_client.order(
                wire_symbol,
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

    def _stop_grace_entry_block_reason(self, plan: TradePlan | None) -> str | None:
        if plan is None:
            return None
        if not bool(
            getattr(self.config.trident.execution, "live_block_stop_grace_setups", True)
        ):
            return None
        stop_grace_minutes = self._stop_grace_minutes_for_plan(plan)
        if stop_grace_minutes <= 0:
            return None
        return (
            "stop_grace_exchange_sl_mismatch:"
            f"setup={plan.setup},grace_minutes={stop_grace_minutes}"
        )

    def load_order_metadata(self, orders: dict[str, object] | None) -> None:
        if not isinstance(orders, dict):
            return
        for symbol, metadata in orders.items():
            if not isinstance(metadata, dict):
                continue
            normalized = normalize_hl_symbol(str(symbol))
            if not normalized:
                continue
            self.orders_by_symbol[normalized] = dict(metadata)

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
        self._notify_orders_changed()

    def _notify_orders_changed(self) -> None:
        if self.orders_changed_callback is None:
            return
        self.orders_changed_callback()

    def _place_protective_orders(
        self,
        *,
        plan: TradePlan,
        fill: LiveExecutionFill,
    ) -> tuple[dict[str, int | None], dict[str, object] | None]:
        protective: dict[str, int | None] = {}
        stop_grace_metadata = self._stop_grace_metadata(plan, fill)
        if fill.filled_size <= 0:
            return protective, stop_grace_metadata
        stop_price = (
            float(stop_grace_metadata["catastrophic_stop_price"])
            if stop_grace_metadata is not None
            else self._stop_price(plan, fill.price)
        )
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
                if stop_grace_metadata is not None:
                    stop_grace_metadata["catastrophic_sl_oid"] = sl.oid
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
        if stop_grace_metadata is not None and protective.get("sl") is None:
            stop_grace_metadata = None
        return protective, stop_grace_metadata

    def refresh_stop_grace_orders(
        self,
        symbol: str | None = None,
        *,
        now: datetime | str | None = None,
    ) -> bool:
        now_dt = _parse_utc_datetime(now) if now is not None else datetime.now(timezone.utc)
        if now_dt is None:
            now_dt = datetime.now(timezone.utc)
        symbols = [normalize_hl_symbol(symbol)] if symbol is not None else list(self.orders_by_symbol)
        changed = False
        for normalized in symbols:
            if not normalized:
                continue
            metadata = self.orders_by_symbol.get(normalized)
            if not isinstance(metadata, dict):
                continue
            stop_grace = metadata.get("stop_grace")
            if not isinstance(stop_grace, dict):
                continue
            if bool(stop_grace.get("normal_stop_placed")):
                continue
            grace_until = _parse_utc_datetime(stop_grace.get("grace_until"))
            if grace_until is None or now_dt < grace_until:
                continue
            normal_stop_price = _float_or_zero(stop_grace.get("normal_stop_price"))
            side = str(stop_grace.get("side") or metadata.get("side") or "").lower()
            if normal_stop_price <= 0 or side not in {"long", "short"}:
                logger.warning(
                    "Stop-grace SL refresh skipped for %s: invalid metadata=%s",
                    normalized,
                    stop_grace,
                )
                continue
            size = self._stop_grace_close_size(normalized, metadata)
            if size is None or size <= 0:
                logger.warning("Stop-grace SL refresh skipped for %s: no close size", normalized)
                continue
            protective = metadata.get("protective_oids", {})
            old_sl_oid = None
            if isinstance(protective, dict):
                old_sl_oid = _maybe_int(protective.get("sl"))
            if old_sl_oid is None:
                old_sl_oid = _maybe_int(stop_grace.get("catastrophic_sl_oid"))
            try:
                normal_sl = self._submit_trigger(
                    symbol=normalized,
                    side=side,
                    trigger_price=normal_stop_price,
                    size=size,
                    tpsl="sl",
                )
            except HyperliquidAPIError:
                logger.exception("Stop-grace normal SL refresh failed for %s", normalized)
                continue
            if old_sl_oid is not None:
                try:
                    self._cancel_order(normalized, old_sl_oid)
                except Exception as exc:
                    logger.warning(
                        "Failed to cancel stop-grace catastrophe SL oid=%s for %s: %s",
                        old_sl_oid,
                        normalized,
                        exc,
                    )
            if not isinstance(protective, dict):
                protective = {}
            protective["sl"] = normal_sl.oid
            metadata["protective_oids"] = protective
            stop_grace["active"] = False
            stop_grace["normal_stop_placed"] = True
            stop_grace["normal_sl_oid"] = normal_sl.oid
            stop_grace["activated_at"] = _format_utc(now_dt)
            metadata["stop_grace"] = stop_grace
            self.orders_by_symbol[normalized] = metadata
            logger.info(
                "Stop-grace SL refreshed for %s: old_oid=%s new_oid=%s trigger=%s",
                normalized,
                old_sl_oid,
                normal_sl.oid,
                normal_stop_price,
            )
            changed = True
        return changed

    def _stop_grace_metadata(
        self,
        plan: TradePlan,
        fill: LiveExecutionFill,
    ) -> dict[str, object] | None:
        stop_grace_minutes = self._stop_grace_minutes_for_plan(plan)
        if stop_grace_minutes <= 0:
            return None
        opened_at = _parse_utc_datetime(fill.timestamp) or datetime.now(timezone.utc)
        normal_stop_price = self._stop_price(plan, fill.price)
        catastrophe_stop_price = self._catastrophic_stop_price(plan, fill.price, normal_stop_price)
        if catastrophe_stop_price <= 0:
            return None
        return {
            "active": True,
            "setup": plan.setup,
            "side": plan.side,
            "grace_minutes": stop_grace_minutes,
            "opened_at": _format_utc(opened_at),
            "grace_until": _format_utc(opened_at + timedelta(minutes=stop_grace_minutes)),
            "entry_price": fill.price,
            "normal_stop_price": normal_stop_price,
            "catastrophic_stop_price": catastrophe_stop_price,
            "normal_stop_placed": False,
        }

    def _stop_grace_minutes_for_plan(self, plan: TradePlan | None) -> int:
        if plan is None:
            return 0
        stop_grace_minutes = max(int(getattr(self.config.pod_a, "stop_grace_minutes", 0)), 0)
        if stop_grace_minutes <= 0:
            return 0
        if str(plan.setup or "") != "trend_pullback_long":
            return 0
        details = dict(plan.setup_details or {})
        return stop_grace_minutes_for_setup(
            self.config.pod_a,
            setup=plan.setup,
            confidence=float(plan.confidence or 0.0),
            details=details,
            fallback_minutes=stop_grace_minutes,
        )

    def _catastrophic_stop_price(
        self,
        plan: TradePlan,
        entry_price: float,
        normal_stop_price: float,
    ) -> float:
        bps = catastrophic_stop_bps_for_plan(
            self.config.trident.execution,
            stop_bps=float(plan.stop_bps or 0.0),
        )
        if bps <= 0 or entry_price <= 0:
            return normal_stop_price
        if plan.side == "long":
            catastrophe = self._round_price(
                entry_price * (1.0 - bps / 10_000.0),
                symbol=plan.symbol,
            )
            if normal_stop_price > 0:
                catastrophe = min(catastrophe, normal_stop_price)
            return catastrophe
        catastrophe = self._round_price(
            entry_price * (1.0 + bps / 10_000.0),
            symbol=plan.symbol,
        )
        if normal_stop_price > 0:
            catastrophe = max(catastrophe, normal_stop_price)
        return catastrophe

    def _stop_grace_close_size(
        self,
        symbol: str,
        metadata: dict[str, object],
    ) -> float | None:
        size = self._close_size(symbol)
        if size is not None:
            return size
        for key in ("entry_filled_size", "entry_size"):
            raw = metadata.get(key)
            try:
                value = float(Decimal(str(raw)))
            except (InvalidOperation, TypeError, ValueError):
                continue
            decimals = self._size_decimals(symbol)
            rounded = self._round_size(value, decimals=decimals)
            if rounded > 0:
                return rounded
        return None

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
            limit_px=self._round_price(trigger_price, symbol=symbol),
            reduce_only=True,
            cloid=cloid,
            order_type={
                "trigger": {
                    "isMarket": True,
                    "triggerPx": self._round_price(trigger_price, symbol=symbol),
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
        wire_symbol = self._exchange_wire_symbol(symbol)
        if wire_symbol is None:
            raise HyperliquidAPIError(f"asset_not_resolved:{normalize_hl_symbol(symbol)}")
        self._acquire_exchange_action(action="cancel")
        try:
            raw = self.exchange_client.cancel(wire_symbol, oid)
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
        return self._remaining_position_size_from_state(state, symbol=symbol)

    def _remaining_position_size_from_state(
        self,
        state: ExchangeAccountState | None,
        *,
        symbol: str,
    ) -> Decimal:
        if state is None:
            return Decimal("1")
        position = state.positions.get(symbol)
        if position is None:
            return Decimal("0")
        return abs(position.size)

    def _fetch_account_state_for_fill_match(
        self,
        *,
        include_funding: bool = False,
    ) -> ExchangeAccountState | None:
        try:
            return self.private_info_client.fetch_account_state(
                fills_lookback_hours=1.0,
                funding_lookback_hours=(
                    self.funding_lookback_hours if include_funding else None
                ),
            )
        except Exception as exc:
            logger.warning("Live fill enrichment unavailable: %s", exc)
            return None

    def _recent_exchange_fill_for_order(
        self,
        *,
        symbol: str,
        oid: int | None,
        action: str,
    ) -> ExchangeFill | None:
        state = self._fetch_account_state_for_fill_match()
        return self._exchange_fill_for_order(state, symbol=symbol, oid=oid, action=action)

    def _exchange_fill_for_order(
        self,
        state: ExchangeAccountState | None,
        *,
        symbol: str,
        oid: int | None,
        action: str | None = None,
    ) -> ExchangeFill | None:
        if state is None or oid is None:
            return None
        normalized = normalize_hl_symbol(symbol)
        matches = [
            fill
            for fill in state.recent_fills
            if normalize_hl_symbol(fill.symbol) == normalized and fill.oid == oid
        ]
        if action == "close":
            matches = [
                fill
                for fill in matches
                if "close" in str(fill.direction).lower()
                or abs(float(fill.closed_pnl_usd)) > 0
            ] or matches
        if action == "open":
            matches = [
                fill
                for fill in matches
                if "open" in str(fill.direction).lower()
                or not ("close" in str(fill.direction).lower())
            ] or matches
        if not matches:
            return None
        return max(matches, key=lambda fill: int(fill.timestamp_ms or 0))

    def _exchange_fill_metadata(self, fill: ExchangeFill | None) -> dict[str, object]:
        if fill is None:
            return {
                "exchange_fill_available": False,
                "exchange_fee_usd": None,
                "exchange_closed_pnl_usd": None,
                "exchange_direction": None,
                "exchange_timestamp_ms": None,
                "fee_source": "unavailable",
                "exchange_fill": None,
            }
        return {
            "exchange_fill_available": True,
            "exchange_fee_usd": float(fill.fee_usd),
            "exchange_closed_pnl_usd": float(fill.closed_pnl_usd),
            "exchange_direction": fill.direction,
            "exchange_timestamp_ms": fill.timestamp_ms,
            "fee_source": "exchange_user_fills",
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

    def _funding_metadata(
        self,
        state: ExchangeAccountState | None,
        *,
        symbol: str,
    ) -> dict[str, object]:
        payments = self._funding_payments_for_symbol(state, symbol=symbol)
        return {
            "funding_usd": None,
            "funding_source": (
                "exchange_user_funding_history_unattributed"
                if payments
                else "not_collected"
            ),
            "funding_payment_count": len(payments),
            "funding_payments": [self._funding_payment_record(payment) for payment in payments],
        }

    def _funding_payments_for_symbol(
        self,
        state: ExchangeAccountState | None,
        *,
        symbol: str,
    ) -> list[ExchangeFundingPayment]:
        if state is None:
            return []
        normalized = normalize_hl_symbol(symbol)
        return [
            payment
            for payment in state.recent_funding
            if normalize_hl_symbol(payment.symbol) == normalized
        ]

    def _funding_payment_record(
        self,
        payment: ExchangeFundingPayment,
    ) -> dict[str, object]:
        return {
            "symbol": payment.symbol,
            "amount_usd": payment.amount_usd,
            "funding_rate": payment.funding_rate,
            "size": payment.size,
            "timestamp_ms": payment.timestamp_ms,
            "hash": payment.hash,
            "raw": payment.raw,
        }

    def _has_exchange_exposure(self, state: ExchangeAccountState, symbol: str) -> bool:
        if symbol in state.positions:
            return True
        return any(order.symbol == symbol for order in state.all_orders)

    def _limit_price(
        self,
        mid_price: float,
        *,
        symbol: str,
        side: str,
        action: str,
        slippage_bps: float,
    ) -> float:
        sign = 1.0
        if side == "long":
            sign = 1.0 if action == "open" else -1.0
        else:
            sign = -1.0 if action == "open" else 1.0
        return self._round_price(
            mid_price * (1.0 + sign * slippage_bps / 10_000.0),
            symbol=symbol,
        )

    def _post_only_limit_price(
        self,
        mid_price: float,
        *,
        symbol: str,
        side: str,
        action: str,
        spread_bps: float,
    ) -> float:
        is_buy = (side == "long" and action == "open") or (
            side == "short" and action == "close"
        )
        buffer_bps = max(float(spread_bps) / 2.0 + self.post_only_buffer_bps, 0.1)
        sign = -1.0 if is_buy else 1.0
        return self._round_price(
            mid_price * (1.0 + sign * buffer_bps / 10_000.0),
            symbol=symbol,
        )

    def _pending_position_metadata(
        self,
        *,
        symbol: str,
        side: str,
        notional_usd: float,
        timestamp: str | None,
        plan: TradePlan | None,
        entry_price: float | None = None,
        entry_fee_usd: float | None = None,
    ) -> dict[str, object]:
        if plan is None:
            metadata: dict[str, object] = {
                "symbol": symbol,
                "side": side,
                "setup": "live_post_only_entry",
                "confidence": 0.0,
                "target_notional_usd": notional_usd,
                "opened_at": timestamp,
            }
        else:
            metadata = {
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
        if entry_price is not None:
            metadata["entry_price"] = entry_price
        if entry_fee_usd is not None:
            metadata["entry_fee_usd"] = entry_fee_usd
        return metadata

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
            return self._round_price(plan.invalidation_price, symbol=plan.symbol)
        delta = plan.stop_bps / 10_000.0
        if plan.side == "long":
            return self._round_price(entry_price * (1.0 - delta), symbol=plan.symbol)
        return self._round_price(entry_price * (1.0 + delta), symbol=plan.symbol)

    def _take_profit_price(self, plan: TradePlan, entry_price: float) -> float:
        delta = plan.take_profit_bps / 10_000.0
        if plan.side == "long":
            return self._round_price(entry_price * (1.0 + delta), symbol=plan.symbol)
        return self._round_price(entry_price * (1.0 - delta), symbol=plan.symbol)

    def _new_cloid(self) -> str:
        millis = int(time.time() * 1000) & ((1 << 48) - 1)
        random_bits = secrets.randbits(80)
        return f"0x{((millis << 80) | random_bits):032x}"

    def _round_wire(self, value: float) -> float:
        return float(f"{value:.8f}")

    def _round_price(self, value: float, *, symbol: str | None = None) -> float:
        try:
            price = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return self._round_wire(value)
        if price <= 0:
            return 0.0
        significant_quantum = Decimal("1e{}".format(price.adjusted() - 4))
        rounded = price.quantize(significant_quantum, rounding=ROUND_HALF_UP)
        max_price_decimals = self._max_price_decimals(symbol)
        if rounded.as_tuple().exponent < -max_price_decimals:
            decimal_quantum = Decimal("1").scaleb(-max_price_decimals)
            rounded = rounded.quantize(decimal_quantum, rounding=ROUND_HALF_UP)
        return float(rounded)

    def _max_price_decimals(self, symbol: str | None) -> int:
        if not symbol:
            return 6
        return max(6 - self._size_decimals(symbol), 0)

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
        return self._size_decimals_by_symbol.get(normalize_hl_symbol(symbol), 8)

    def _load_size_decimals(self) -> dict[str, int]:
        decimals_by_symbol: dict[str, int] = {}
        try:
            info_client = self.private_info_client.info_client
        except Exception as exc:
            logger.warning("Unable to load Hyperliquid meta for size rounding: %s", exc)
            return decimals_by_symbol
        if not hasattr(info_client, "meta"):
            return decimals_by_symbol
        for dex in [None, *self._builder_perp_dexs]:
            try:
                meta = self._fetch_perp_meta(info_client, dex=dex)
            except Exception as exc:
                logger.warning(
                    "Unable to load Hyperliquid %smeta for size rounding: %s",
                    f"{dex} " if dex else "",
                    exc,
                )
                continue
            decimals_by_symbol.update(self._parse_size_decimals_meta(meta, dex=dex))
        return decimals_by_symbol

    def _fetch_perp_meta(self, info_client: Any, *, dex: str | None) -> object:
        if dex is None:
            return info_client.meta()
        return info_client.meta(dex=dex)

    def _parse_size_decimals_meta(
        self,
        meta: object,
        *,
        dex: str | None,
    ) -> dict[str, int]:
        if not isinstance(meta, dict):
            return {}
        universe = meta.get("universe", [])
        if not isinstance(universe, list):
            return {}
        decimals_by_symbol: dict[str, int] = {}
        for item in universe:
            if not isinstance(item, dict):
                continue
            name = self._canonical_meta_symbol(item.get("name"), dex=dex)
            if not name:
                continue
            try:
                decimals_by_symbol[name] = int(item.get("szDecimals", 8))
            except (TypeError, ValueError):
                continue
        return decimals_by_symbol

    def _canonical_meta_symbol(self, name: object, *, dex: str | None) -> str:
        raw = str(name or "").strip()
        if not raw:
            return ""
        if dex is not None and ":" not in raw:
            return normalize_hl_symbol(f"{dex}:{raw}")
        return normalize_hl_symbol(raw)

    def _exchange_wire_symbol(self, symbol: str) -> str | None:
        canonical = normalize_hl_symbol(symbol)
        if not canonical:
            return None
        candidates = [ws_subscription_symbol(canonical)]
        if canonical not in candidates:
            candidates.append(canonical)
        name_to_coin = getattr(getattr(self.exchange_client, "info", None), "name_to_coin", None)
        if not isinstance(name_to_coin, dict):
            return candidates[0]
        for candidate in candidates:
            if candidate in name_to_coin:
                return candidate
        return None

    def _configured_builder_perp_dexs(self) -> list[str]:
        symbols = self._configured_symbols()
        grouped = group_hl_symbols_by_dex(symbols)
        return sorted(dex for dex in grouped if dex is not None)

    def _configured_symbols(self) -> list[str]:
        symbols: list[str] = []
        hyperliquid_config = self.config.hyperliquid
        symbols.extend(hyperliquid_config.observation_universe or [])
        symbols.extend(hyperliquid_config.default_coins or [])
        symbols.extend(hyperliquid_config.market_cluster_overrides.keys())
        for leaders in hyperliquid_config.cluster_leaders.values():
            symbols.extend(leaders)
        symbols.extend(self.config.pod_a.max_leverage_by_symbol.keys())
        symbols.extend(self.config.pod_b.bis_max_leverage_by_symbol.keys())
        symbols.extend(self.config.pod_c.max_leverage_by_symbol.keys())
        symbols.extend(self.config.pod_c.blocked_symbols)
        return symbols


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


def _float_or_zero(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_utc_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text or text.lower() == "none":
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
