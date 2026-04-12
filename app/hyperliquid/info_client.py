from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable
from urllib import error, request

from app.hyperliquid.symbols import group_hl_symbols_by_dex, normalize_hl_symbol
from app.live.errors import HyperliquidAPIError, HyperliquidRateLimitError, is_rate_limit_message
from app.hyperliquid.rate_limiter import SharedRateLimiter, jitter_seconds
from app.settings import AppConfig, HyperliquidConfig, override_app_config


@dataclass(slots=True)
class HyperliquidHttpStats:
    attempts: int = 0
    retry_count: int = 0
    rate_limit_count: int = 0
    server_error_count: int = 0
    last_status_code: int | None = None
    last_error: str | None = None
    throttle_wait_count: int = 0
    throttle_wait_seconds: float = 0.0
    circuit_open_count: int = 0


class HyperliquidInfoClient:
    """Small retriable HTTP client for Hyperliquid info requests."""

    def __init__(
        self,
        config: HyperliquidConfig,
        *,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.sleep_fn = sleep_fn or time.sleep
        self.stats = HyperliquidHttpStats()
        self.rate_limiter = SharedRateLimiter(
            config.rate_limit_state_path,
            jitter_fn=lambda seconds: jitter_seconds(
                seconds,
                config.shared_rate_limit_jitter_seconds,
            ),
        )

    def post_info(
        self,
        payload: dict[str, object],
        *,
        max_attempts: int = 3,
        timeout: float | None = None,
    ) -> object:
        timeout_seconds = timeout or self.config.connect_timeout_seconds
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        for attempt in range(1, max_attempts + 1):
            self.stats.attempts += 1
            waited = self.rate_limiter.acquire(
                "http_info",
                capacity=self.config.info_requests_per_minute,
                window_seconds=60.0,
                sleep_fn=self.sleep_fn,
            )
            if waited > 0:
                self.stats.throttle_wait_count += 1
                self.stats.throttle_wait_seconds = round(
                    self.stats.throttle_wait_seconds + waited,
                    4,
                )
            self.stats.circuit_open_count = self.rate_limiter.stats.circuit_open_count
            try:
                req = request.Request(
                    self.config.info_url,
                    data=body,
                    headers=headers,
                    method="POST",
                )
                with request.urlopen(req, timeout=timeout_seconds) as response:
                    self.stats.last_status_code = response.status
                    raw = response.read().decode("utf-8")
                    self.rate_limiter.record_success("http_info")
                    return json.loads(raw)
            except error.HTTPError as exc:
                self.stats.last_status_code = exc.code
                response_text = exc.read().decode("utf-8", errors="replace")
                self.stats.last_error = response_text or str(exc)
                if exc.code == 429 or is_rate_limit_message(response_text):
                    self.rate_limiter.record_rate_limit(
                        "http_info",
                        threshold=self.config.circuit_breaker_threshold,
                        breaker_seconds=self.config.circuit_breaker_seconds,
                    )
                if self._should_retry_status(exc.code) and attempt < max_attempts:
                    self._register_retry(exc.code, response_text, attempt)
                    continue
                raise self._classify_http_error(exc.code, response_text) from exc
            except error.URLError as exc:
                self.stats.last_error = str(exc.reason)
                if attempt < max_attempts:
                    self._register_retry(None, str(exc.reason), attempt)
                    continue
                raise HyperliquidAPIError(str(exc.reason)) from exc
            except json.JSONDecodeError as exc:
                self.stats.last_error = str(exc)
                raise HyperliquidAPIError("Invalid JSON response from Hyperliquid info API") from exc

        raise HyperliquidAPIError("Exhausted Hyperliquid info retries")

    def fetch_all_mids(
        self,
        *,
        symbols: list[str] | None = None,
    ) -> dict[str, float]:
        """Fetch current mid prices for the requested symbols via ``allMids``."""
        grouped_symbols = group_hl_symbols_by_dex(symbols)
        if not grouped_symbols:
            payload = self.post_info({"type": "allMids"})
            return self._parse_all_mids_payload(payload)
        result: dict[str, float] = {}
        for dex, requested_symbols in grouped_symbols.items():
            payload_body: dict[str, object] = {"type": "allMids"}
            if dex is not None:
                payload_body["dex"] = dex
            payload = self.post_info(payload_body)
            result.update(
                self._parse_all_mids_payload(
                    payload,
                    symbols=requested_symbols,
                )
            )
        return result

    def _parse_all_mids_payload(
        self,
        payload: object,
        *,
        symbols: list[str] | None = None,
    ) -> dict[str, float]:
        if not isinstance(payload, dict):
            return {}
        requested = None if symbols is None else {normalize_hl_symbol(symbol) for symbol in symbols}
        result: dict[str, float] = {}
        for symbol, price_str in payload.items():
            normalized = normalize_hl_symbol(str(symbol))
            if requested is not None and normalized not in requested:
                continue
            try:
                price = float(price_str)
                if price > 0:
                    result[normalized] = price
            except (TypeError, ValueError):
                continue
        return result

    def fetch_max_leverage_by_symbol(
        self,
        *,
        symbols: list[str] | None = None,
        include_delisted: bool = False,
    ) -> dict[str, float]:
        grouped_symbols = group_hl_symbols_by_dex(symbols)
        if not grouped_symbols:
            payload = self.post_info({"type": "metaAndAssetCtxs"})
            return extract_max_leverage_by_symbol(
                payload,
                symbols=symbols,
                include_delisted=include_delisted,
            )
        result: dict[str, float] = {}
        for dex, requested_symbols in grouped_symbols.items():
            payload_body: dict[str, object] = {"type": "metaAndAssetCtxs"}
            if dex is not None:
                payload_body["dex"] = dex
            payload = self.post_info(payload_body)
            result.update(
                extract_max_leverage_by_symbol(
                    payload,
                    symbols=requested_symbols,
                    include_delisted=include_delisted,
                )
            )
        return result

    def _register_retry(
        self,
        status_code: int | None,
        message: str,
        attempt: int,
    ) -> None:
        self.stats.retry_count += 1
        if status_code == 429 or is_rate_limit_message(message):
            self.stats.rate_limit_count += 1
        elif status_code is not None and status_code >= 500:
            self.stats.server_error_count += 1
        self.sleep_fn(self._backoff_delay(attempt))

    def _backoff_delay(self, attempt: int) -> float:
        return min(
            self.config.reconnect_delay_seconds * (2 ** max(attempt - 1, 0)),
            self.config.max_reconnect_delay_seconds,
        )

    def _should_retry_status(self, status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    def _classify_http_error(self, status_code: int, message: str) -> HyperliquidAPIError:
        if status_code == 429 or is_rate_limit_message(message):
            return HyperliquidRateLimitError(message or f"HTTP {status_code}")
        return HyperliquidAPIError(message or f"HTTP {status_code}")


def extract_max_leverage_by_symbol(
    payload: object,
    *,
    symbols: list[str] | None = None,
    include_delisted: bool = False,
) -> dict[str, float]:
    if not isinstance(payload, list) or not payload:
        return {}
    meta = payload[0]
    if not isinstance(meta, dict):
        return {}
    universe = meta.get("universe", [])
    if not isinstance(universe, list):
        return {}
    requested = None if symbols is None else {normalize_hl_symbol(str(symbol)) for symbol in symbols}
    parsed: dict[str, float] = {}
    for item in universe:
        if not isinstance(item, dict):
            continue
        name = normalize_hl_symbol(str(item.get("name", "")))
        if not name:
            continue
        if requested is not None and name not in requested:
            continue
        if not include_delisted and bool(item.get("isDelisted", False)):
            continue
        max_leverage = item.get("maxLeverage")
        if max_leverage in (None, ""):
            continue
        parsed[name] = float(max_leverage)
    return parsed


def apply_live_asset_leverage_caps(
    config: AppConfig,
    *,
    symbols: list[str] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> AppConfig:
    client = HyperliquidInfoClient(config.hyperliquid, sleep_fn=sleep_fn)
    try:
        live_caps = client.fetch_max_leverage_by_symbol(symbols=symbols)
    except HyperliquidAPIError:
        return config
    if not live_caps:
        return config
    requested_symbols = {
        str(symbol).strip().upper()
        for symbol in (symbols or [])
        if str(symbol).strip()
    }
    resolved_caps = dict(live_caps)
    for symbol in requested_symbols:
        resolved_caps.setdefault(symbol, 1.0)
    merged_pod_a_caps = dict(config.pod_a.max_leverage_by_symbol)
    merged_pod_a_caps.update(resolved_caps)
    merged_pod_c_caps = dict(config.pod_c.max_leverage_by_symbol)
    merged_pod_c_caps.update(resolved_caps)
    return override_app_config(
        config,
        pod_a_max_leverage_by_symbol=merged_pod_a_caps,
        pod_c_max_leverage_by_symbol=merged_pod_c_caps,
    )
