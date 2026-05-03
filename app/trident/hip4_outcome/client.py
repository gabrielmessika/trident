from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.hyperliquid.info_client import HyperliquidInfoClient
from app.settings import HyperliquidConfig
from app.trident.hip4_outcome.config import Hip4OutcomeConfig


class HIP4OutcomeInfoClient:
    """Public Hyperliquid info client for HIP-4 outcome metadata and books."""

    def __init__(
        self,
        config: Hip4OutcomeConfig,
        *,
        sleep_fn: object | None = None,
        inner: HyperliquidInfoClient | None = None,
    ) -> None:
        self.config = config
        hyperliquid_config = HyperliquidConfig(
            ws_url=config.ws_url,
            info_url=config.info_url,
            rate_limit_state_path=config.rate_limit_state_path,
            connect_timeout_seconds=config.request_timeout_seconds,
            info_requests_per_minute=config.info_requests_per_minute,
        )
        self.inner = inner or HyperliquidInfoClient(
            hyperliquid_config,
            sleep_fn=sleep_fn,  # type: ignore[arg-type]
        )

    def fetch_outcome_meta(self) -> object:
        return self.inner.post_info({"type": "outcomeMeta"}, timeout=self.config.request_timeout_seconds)

    def fetch_all_mids_raw(self) -> dict[str, str]:
        payload = self.inner.post_info({"type": "allMids"}, timeout=self.config.request_timeout_seconds)
        return payload if isinstance(payload, dict) else {}

    def fetch_all_mids(self) -> dict[str, float]:
        parsed: dict[str, float] = {}
        for symbol, raw_price in self.fetch_all_mids_raw().items():
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if price > 0:
                parsed[str(symbol).upper()] = price
        return parsed

    def fetch_l2_book(self, coin: str) -> object:
        return self.inner.post_info(
            {"type": "l2Book", "coin": coin},
            timeout=self.config.request_timeout_seconds,
        )

    def fetch_spot_state(self, user: str) -> object:
        return self.inner.post_info(
            {"type": "spotClearinghouseState", "user": user},
            timeout=self.config.request_timeout_seconds,
        )

    def fetch_clearinghouse_state(self, user: str) -> object:
        return self.inner.post_info(
            {"type": "clearinghouseState", "user": user},
            timeout=self.config.request_timeout_seconds,
        )

    def fetch_user_fills_by_time(
        self,
        *,
        user: str,
        start_time_ms: int,
        end_time_ms: int | None = None,
        aggregate_by_time: bool = False,
    ) -> object:
        return self.inner.post_info(
            {
                "type": "userFillsByTime",
                "user": user,
                "startTime": start_time_ms,
                "endTime": end_time_ms,
                "aggregateByTime": aggregate_by_time,
            },
            timeout=self.config.request_timeout_seconds,
        )

    def with_config(self, config: Hip4OutcomeConfig) -> "HIP4OutcomeInfoClient":
        inner_config = replace(
            self.inner.config,
            ws_url=config.ws_url,
            info_url=config.info_url,
            rate_limit_state_path=config.rate_limit_state_path,
            connect_timeout_seconds=config.request_timeout_seconds,
            info_requests_per_minute=config.info_requests_per_minute,
        )
        return HIP4OutcomeInfoClient(
            config,
            inner=HyperliquidInfoClient(inner_config, sleep_fn=self.inner.sleep_fn),
        )
