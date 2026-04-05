from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass

from app.hyperliquid.rate_limiter import SharedRateLimiter, jitter_seconds
from app.live.errors import (
    HyperliquidRateLimitError,
    LiveCollectorRecoverableError,
    classify_payload_error,
)
from app.live.snapshot_builder import LiveSnapshotBuilder
from app.live.snapshot_writer import LiveSnapshotWriter
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class LiveCollectorStats:
    messages_processed: int = 0
    snapshots_written: int = 0
    reconnect_count: int = 0
    heartbeat_count: int = 0
    pong_count: int = 0
    timeout_count: int = 0
    invalid_message_count: int = 0
    api_error_count: int = 0
    rate_limit_error_count: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    throttle_wait_count: int = 0
    throttle_wait_seconds: float = 0.0
    circuit_open_count: int = 0


class HyperliquidLiveCollector:
    """Collects live l2Book and trades streams from Hyperliquid and writes TRIDENT snapshots."""

    def __init__(self, config: AppConfig, coins: list[str] | None = None) -> None:
        self.config = config
        self.coins = coins or config.hyperliquid.default_coins or config.pod_a.symbols
        self.builder = LiveSnapshotBuilder(
            coins=self.coins,
            bucket_ms=config.hyperliquid.bucket_ms,
        )
        self.writer = LiveSnapshotWriter(config.hyperliquid.snapshot_output_dir)
        self.stats = LiveCollectorStats()
        self.rate_limiter = SharedRateLimiter(
            config.hyperliquid.rate_limit_state_path,
            jitter_fn=lambda seconds: jitter_seconds(
                seconds,
                config.hyperliquid.shared_rate_limit_jitter_seconds,
            ),
        )

    async def run(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
    ) -> LiveCollectorStats:
        async for record in self.iter_records(
            max_runtime_seconds=max_runtime_seconds,
            max_messages=max_messages,
        ):
            self.stats.snapshots_written += len(self.writer.append_many([record]))
        final_records = self.builder.finalize()
        self.stats.snapshots_written += len(self.writer.append_many(final_records))
        return self.stats

    async def iter_records(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
    ):
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "websockets is required for the live collector. Install project dependencies first."
            ) from exc

        started = asyncio.get_running_loop().time()
        while True:
            if self._deadline_reached(started, max_runtime_seconds):
                break
            try:
                await self._respect_rate_limit(
                    "ws_connect",
                    capacity=self.config.hyperliquid.ws_connects_per_minute,
                    window_seconds=60.0,
                )
                async with asyncio.timeout(self.config.hyperliquid.connect_timeout_seconds):
                    websocket = await websockets.connect(
                        self.config.hyperliquid.ws_url,
                        ping_interval=None,
                    )
                async with websocket:
                    self.stats.consecutive_failures = 0
                    self.rate_limiter.record_success("ws_connect")
                    await self._subscribe(websocket)
                    idle_heartbeats = 0
                    while True:
                        raw_message = await self._recv_message(websocket)
                        if raw_message is None:
                            idle_heartbeats += 1
                            if idle_heartbeats > self.config.hyperliquid.max_idle_heartbeats:
                                raise LiveCollectorRecoverableError("max_idle_heartbeats_exceeded")
                            await self._send_heartbeat(websocket)
                            continue

                        idle_heartbeats = 0
                        payload = self._parse_message(raw_message)
                        if payload is None:
                            continue
                        records = self._handle_payload(payload)
                        for record in records:
                            yield record
                        if max_messages is not None and self.stats.messages_processed >= max_messages:
                            raise StopAsyncIteration
                        if self._deadline_reached(started, max_runtime_seconds):
                            raise StopAsyncIteration
            except StopAsyncIteration:
                break
            except HyperliquidRateLimitError as exc:
                self.stats.api_error_count += 1
                self.stats.rate_limit_error_count += 1
                self.stats.last_error = str(exc)
                self.rate_limiter.record_rate_limit(
                    "ws_connect",
                    threshold=self.config.hyperliquid.circuit_breaker_threshold,
                    breaker_seconds=self.config.hyperliquid.circuit_breaker_seconds,
                )
                self.rate_limiter.record_rate_limit(
                    "ws_send",
                    threshold=self.config.hyperliquid.circuit_breaker_threshold,
                    breaker_seconds=self.config.hyperliquid.circuit_breaker_seconds,
                )
                self._register_failure()
                self.stats.reconnect_count += 1
                await asyncio.sleep(self._backoff_delay(rate_limited=True))
            except (LiveCollectorRecoverableError, asyncio.TimeoutError) as exc:
                self.stats.api_error_count += 1
                self.stats.last_error = str(exc)
                self._register_failure()
                self.stats.reconnect_count += 1
                await asyncio.sleep(self._backoff_delay())
            except Exception as exc:
                self.stats.last_error = repr(exc)
                self._register_failure()
                self.stats.reconnect_count += 1
                await asyncio.sleep(self._backoff_delay())

    async def _subscribe(self, websocket: object) -> None:
        for coin in self.coins:
            await self._send_json(
                websocket,
                {
                    "method": "subscribe",
                    "subscription": {"type": "l2Book", "coin": coin},
                },
            )
            await self._send_json(
                websocket,
                {
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": coin},
                },
            )

    async def _recv_message(self, websocket: object) -> str | None:
        try:
            return await asyncio.wait_for(
                websocket.recv(),
                timeout=self.config.hyperliquid.message_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self.stats.timeout_count += 1
            return None

    async def _send_heartbeat(self, websocket: object) -> None:
        await self._send_json(websocket, {"method": "ping"})
        self.stats.heartbeat_count += 1

    async def _send_json(self, websocket: object, payload: dict[str, object]) -> None:
        await self._respect_rate_limit(
            "ws_send",
            capacity=self.config.hyperliquid.ws_messages_per_second,
            window_seconds=1.0,
        )
        await websocket.send(json.dumps(payload))
        self.rate_limiter.record_success("ws_send")

    def _parse_message(self, raw_message: object) -> dict[str, object] | None:
        if not isinstance(raw_message, str):
            self.stats.invalid_message_count += 1
            return None
        try:
            return json.loads(raw_message)
        except json.JSONDecodeError:
            self.stats.invalid_message_count += 1
            return None

    def _handle_payload(self, payload: dict[str, object]) -> list[dict[str, object]]:
        error = classify_payload_error(payload)
        if error is not None:
            raise error

        channel = str(payload.get("channel", ""))
        if channel == "pong":
            self.stats.pong_count += 1
            return []
        if channel == "subscriptionResponse":
            return []

        self.stats.messages_processed += 1
        return self.builder.ingest_ws_message(payload)

    def _deadline_reached(
        self,
        started: float,
        max_runtime_seconds: float | None,
    ) -> bool:
        if max_runtime_seconds is None:
            return False
        return asyncio.get_running_loop().time() - started >= max_runtime_seconds

    def _register_failure(self) -> None:
        self.stats.consecutive_failures += 1

    def _backoff_delay(self, *, rate_limited: bool = False) -> float:
        base = self.config.hyperliquid.reconnect_delay_seconds * (2 ** max(self.stats.consecutive_failures - 1, 0))
        if rate_limited:
            base *= 2.0
        return min(base, self.config.hyperliquid.max_reconnect_delay_seconds)

    async def _respect_rate_limit(
        self,
        key: str,
        *,
        capacity: int,
        window_seconds: float,
    ) -> None:
        total_wait = 0.0
        while True:
            wait_seconds = self.rate_limiter.reserve(
                key,
                capacity=capacity,
                window_seconds=window_seconds,
            )
            if wait_seconds <= 0:
                if total_wait > 0:
                    self.stats.throttle_wait_count += 1
                    self.stats.throttle_wait_seconds = round(
                        self.stats.throttle_wait_seconds + total_wait,
                        4,
                    )
                self.stats.circuit_open_count = self.rate_limiter.stats.circuit_open_count
                return
            total_wait += wait_seconds
            await asyncio.sleep(wait_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect live Hyperliquid snapshots into TRIDENT JSONL files")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--coins", help="Comma-separated coin list. Defaults to hyperliquid.default_coins or pod_a symbols.")
    parser.add_argument("--max-runtime-seconds", type=float, help="Optional limit for local smoke runs.")
    parser.add_argument("--max-messages", type=int, help="Optional max websocket messages before exit.")
    return parser


async def _run_from_args() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    coins = None
    if args.coins:
        coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    collector = HyperliquidLiveCollector(config, coins=coins)
    stats = await collector.run(
        max_runtime_seconds=args.max_runtime_seconds,
        max_messages=args.max_messages,
    )
    print(f"coins={collector.coins}")
    print(f"snapshot_output_dir={config.hyperliquid.snapshot_output_dir}")
    print(f"messages_processed={stats.messages_processed}")
    print(f"snapshots_written={stats.snapshots_written}")
    print(f"reconnect_count={stats.reconnect_count}")
    print(f"heartbeat_count={stats.heartbeat_count}")
    print(f"pong_count={stats.pong_count}")
    print(f"timeout_count={stats.timeout_count}")
    print(f"invalid_message_count={stats.invalid_message_count}")
    print(f"api_error_count={stats.api_error_count}")
    print(f"rate_limit_error_count={stats.rate_limit_error_count}")
    print(f"throttle_wait_count={stats.throttle_wait_count}")
    print(f"throttle_wait_seconds={stats.throttle_wait_seconds}")
    print(f"circuit_open_count={stats.circuit_open_count}")


def main() -> None:
    asyncio.run(_run_from_args())


if __name__ == "__main__":
    main()
