import asyncio
import json
import unittest
from unittest.mock import patch

from app.live.user_stream import UserOrderUpdateMonitor
from app.settings import load_config


class _FakeWebSocket:
    def __init__(self, monitor: UserOrderUpdateMonitor) -> None:
        self.monitor = monitor
        self.sent: list[str] = []
        self._recv_count = 0

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        self._recv_count += 1
        if self._recv_count == 1:
            return json.dumps(
                {
                    "channel": "subscriptionResponse",
                    "data": {
                        "subscription": {
                            "type": "orderUpdates",
                            "user": "0x0000000000000000000000000000000000000000",
                        }
                    },
                }
            )
        if self._recv_count == 2:
            raise asyncio.TimeoutError
        if self._recv_count == 3:
            return json.dumps({"channel": "pong"})
        self.monitor._stop.set()
        return json.dumps({"channel": "orderUpdates", "data": [{"oid": 123}]})


class _FakeConnect:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> _FakeWebSocket:
        return self.websocket

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class UserStreamTests(unittest.TestCase):
    def test_idle_timeout_sends_ping_without_reconnect(self) -> None:
        async def run_case() -> tuple[UserOrderUpdateMonitor, _FakeWebSocket]:
            config = load_config("config/trident.toml").hyperliquid
            config.message_timeout_seconds = 0.01
            monitor = UserOrderUpdateMonitor(
                config,
                account_address="0x0000000000000000000000000000000000000000",
            )
            websocket = _FakeWebSocket(monitor)
            with patch(
                "app.live.user_stream.websockets.connect",
                return_value=_FakeConnect(websocket),
            ):
                await monitor._connect_once()
            return monitor, websocket

        monitor, websocket = asyncio.run(run_case())

        self.assertTrue(monitor.stats.connected)
        self.assertTrue(monitor.stats.subscription_ack)
        self.assertEqual(monitor.stats.timeout_count, 1)
        self.assertEqual(monitor.stats.heartbeat_count, 1)
        self.assertEqual(monitor.stats.pong_count, 1)
        self.assertEqual(monitor.stats.order_update_count, 1)
        self.assertEqual(monitor.stats.reconnect_count, 0)
        self.assertIn('"method": "ping"', websocket.sent[-1])
        self.assertTrue(monitor.healthy(max_stale_seconds=0.0))


if __name__ == "__main__":
    unittest.main()
