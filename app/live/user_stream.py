from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import websockets

from app.live.errors import HyperliquidAPIError, classify_payload_error
from app.settings import HyperliquidConfig


@dataclass(slots=True)
class UserStreamCheck:
    ok: bool
    subscription_ack: bool = False
    messages_received: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "subscription_ack": self.subscription_ack,
            "messages_received": self.messages_received,
            "error": self.error,
        }


@dataclass(slots=True)
class UserOrderUpdateStats:
    connected: bool = False
    subscription_ack: bool = False
    messages_received: int = 0
    order_update_count: int = 0
    reconnect_count: int = 0
    timeout_count: int = 0
    heartbeat_count: int = 0
    pong_count: int = 0
    last_error: str | None = None
    last_message_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "subscription_ack": self.subscription_ack,
            "messages_received": self.messages_received,
            "order_update_count": self.order_update_count,
            "reconnect_count": self.reconnect_count,
            "timeout_count": self.timeout_count,
            "heartbeat_count": self.heartbeat_count,
            "pong_count": self.pong_count,
            "last_error": self.last_error,
            "last_message_at": self.last_message_at,
        }


class UserOrderUpdateMonitor:
    """Background orderUpdates listener used as the primary fill signal."""

    def __init__(
        self,
        config: HyperliquidConfig,
        *,
        account_address: str,
    ) -> None:
        self.config = config
        self.account_address = account_address
        self.stats = UserOrderUpdateStats()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def healthy(self, *, max_stale_seconds: float = 90.0) -> bool:
        if not self.stats.connected or not self.stats.subscription_ack:
            return False
        if self.stats.last_error:
            return False
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.connected = False
                self.stats.subscription_ack = False
                self.stats.last_error = str(exc)
                self.stats.reconnect_count += 1
                await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self.config.ws_url,
            open_timeout=self.config.connect_timeout_seconds,
            ping_interval=None,
        ) as ws:
            self.stats.connected = True
            self.stats.last_error = None
            await ws.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {
                            "type": "orderUpdates",
                            "user": self.account_address,
                        },
                    }
                )
            )
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=self.config.message_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    self.stats.timeout_count += 1
                    await ws.send(json.dumps({"method": "ping"}))
                    self.stats.heartbeat_count += 1
                    continue
                self.stats.messages_received += 1
                self.stats.last_message_at = time.monotonic()
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                classified = classify_payload_error(payload)
                if classified is not None:
                    raise classified
                if _is_order_updates_ack(payload, self.account_address):
                    self.stats.subscription_ack = True
                    continue
                if payload.get("channel") == "pong":
                    self.stats.pong_count += 1
                    continue
                if payload.get("channel") == "orderUpdates":
                    self.stats.order_update_count += 1
                    if not self.queue.full():
                        self.queue.put_nowait(payload)


async def check_order_updates_subscription(
    config: HyperliquidConfig,
    *,
    account_address: str,
    timeout_seconds: float = 10.0,
) -> UserStreamCheck:
    started = time.monotonic()
    try:
        async with websockets.connect(
            config.ws_url,
            open_timeout=config.connect_timeout_seconds,
            ping_interval=None,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "method": "subscribe",
                        "subscription": {
                            "type": "orderUpdates",
                            "user": account_address,
                        },
                    }
                )
            )
            messages = 0
            while (time.monotonic() - started) < timeout_seconds:
                raw = await asyncio.wait_for(
                    ws.recv(),
                    timeout=max(0.1, timeout_seconds - (time.monotonic() - started)),
                )
                messages += 1
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    classified = classify_payload_error(payload)
                    if classified is not None:
                        return UserStreamCheck(
                            ok=False,
                            messages_received=messages,
                            error=str(classified),
                        )
                    if _is_order_updates_ack(payload, account_address):
                        return UserStreamCheck(
                            ok=True,
                            subscription_ack=True,
                            messages_received=messages,
                        )
            return UserStreamCheck(
                ok=False,
                messages_received=messages,
                error="orderUpdates subscription ack timeout",
            )
    except Exception as exc:
        return UserStreamCheck(ok=False, error=str(exc))


def _is_order_updates_ack(payload: dict[str, Any], account_address: str) -> bool:
    if payload.get("channel") != "subscriptionResponse":
        return False
    data = payload.get("data", {})
    if not isinstance(data, dict):
        return False
    subscription = data.get("subscription", {})
    if not isinstance(subscription, dict):
        return False
    return (
        subscription.get("type") == "orderUpdates"
        and str(subscription.get("user", "")).lower() == account_address.lower()
    )


def require_user_stream_check(check: UserStreamCheck) -> None:
    if not check.ok:
        raise HyperliquidAPIError(check.error or "orderUpdates websocket check failed")
