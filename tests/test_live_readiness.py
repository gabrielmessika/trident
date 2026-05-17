import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from app.execution.live import (
    LIVE_EXCHANGE_ACTION_RATE_LIMIT_KEY,
    LiveExecutionVenue,
    parse_order_result,
)
from app.hyperliquid.private_state import (
    HyperliquidCredentials,
    HyperliquidPrivateInfoClient,
    parse_account_state,
)
from app.live.preflight import build_parser, run_preflight
from app.live.reconciliation import reconcile_exchange_state
from app.live.state_store import LiveStateStore, open_position_from_metadata
from app.portfolio.directional_state import DirectionalPortfolioState
from app.settings import load_config


class _FakeLimiterStats:
    circuit_open_count = 0


class _FakeRateLimiter:
    def __init__(self) -> None:
        self.stats = _FakeLimiterStats()
        self.acquires: list[dict[str, object]] = []
        self.successes: list[str] = []
        self.rate_limits: list[str] = []

    def acquire(
        self,
        key: str,
        *,
        capacity: int,
        window_seconds: float,
        sleep_fn,
        cost: float = 1.0,
    ) -> float:
        self.acquires.append(
            {
                "key": key,
                "capacity": capacity,
                "window_seconds": window_seconds,
                "cost": cost,
            }
        )
        return 0.0

    def record_success(self, key: str) -> None:
        self.successes.append(key)

    def record_rate_limit(
        self,
        key: str,
        *,
        threshold: int,
        breaker_seconds: float,
    ) -> None:
        self.rate_limits.append(key)


class _FakePrivateInfoSdk:
    def user_state(self, address: str) -> dict[str, object]:
        return {"marginSummary": {"accountValue": "1000", "totalMarginUsed": "0"}}

    def spot_user_state(self, address: str) -> dict[str, object]:
        return {"balances": []}

    def open_orders(self, address: str) -> list[object]:
        return []

    def frontend_open_orders(self, address: str) -> list[object]:
        return []

    def user_fills_by_time(
        self,
        address: str,
        start_ms: int,
        *,
        aggregate_by_time: bool = False,
    ) -> list[object]:
        return []


class _FakePrivateAccountClient:
    def fetch_account_state(self, *, fills_lookback_hours: float = 24.0, **_: object):
        return parse_account_state(
            account_address="0x0000000000000000000000000000000000000000",
            user_state={
                "marginSummary": {
                    "accountValue": "1000",
                    "totalMarginUsed": "0",
                }
            },
            spot_state={"balances": []},
            open_orders=[],
            frontend_open_orders=[],
            recent_fills=[],
        )


class _FakeExchange:
    def __init__(self, *, rate_limited: bool = False) -> None:
        self.rate_limited = rate_limited
        self.orders: list[dict[str, object]] = []
        self.cancels: list[tuple[str, int]] = []

    def order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        limit_px: float,
        order_type: dict[str, object],
        *,
        reduce_only: bool,
        cloid: object,
    ) -> dict[str, object]:
        self.orders.append(
            {
                "symbol": symbol,
                "is_buy": is_buy,
                "size": size,
                "limit_px": limit_px,
                "reduce_only": reduce_only,
            }
        )
        if self.rate_limited:
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "rate limit exceeded"}]},
                },
            }
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {
                            "filled": {
                                "oid": 7,
                                "totalSz": str(size),
                                "avgPx": str(limit_px),
                            }
                        }
                    ]
                },
            },
        }

    def cancel(self, symbol: str, oid: int) -> dict[str, object]:
        self.cancels.append((symbol, oid))
        return {"status": "ok"}


class LiveReadinessTests(unittest.TestCase):
    def test_parse_account_state_keeps_exchange_positions_and_orders(self) -> None:
        state = parse_account_state(
            account_address="0x0000000000000000000000000000000000000000",
            user_state={
                "marginSummary": {
                    "accountValue": "1000.5",
                    "totalMarginUsed": "42.0",
                },
                "withdrawable": "900.25",
                "assetPositions": [
                    {
                        "position": {
                            "coin": "ETH",
                            "szi": "0.2",
                            "entryPx": "3000",
                            "positionValue": "600",
                            "marginUsed": "60",
                            "unrealizedPnl": "12.5",
                            "leverage": {"type": "isolated", "value": 10},
                        }
                    }
                ],
            },
            spot_state={"balances": [{"coin": "USDC", "total": "50", "hold": "5"}]},
            open_orders=[
                {
                    "coin": "ETH",
                    "oid": 123,
                    "side": "A",
                    "sz": "0.2",
                    "limitPx": "3010",
                    "reduceOnly": True,
                }
            ],
            frontend_open_orders=[
                {
                    "coin": "ETH",
                    "oid": 456,
                    "side": "A",
                    "sz": "0.2",
                    "limitPx": "2950",
                    "reduceOnly": True,
                    "isTrigger": True,
                    "triggerPx": "2950",
                    "orderType": "Stop Market",
                }
            ],
            recent_fills=[
                {
                    "coin": "ETH",
                    "oid": 100,
                    "side": "B",
                    "dir": "Open Long",
                    "sz": "0.2",
                    "px": "3000",
                    "closedPnl": "0",
                    "fee": "0.1",
                    "time": 1,
                }
            ],
        )

        self.assertEqual(state.account_value_usd, 1000.5)
        self.assertEqual(state.spot_usdc_total, 50.0)
        self.assertEqual(state.spot_usdc_hold, 5.0)
        self.assertEqual(state.positions["ETH"].side, "long")
        self.assertEqual(len(state.all_orders), 2)
        self.assertTrue(any(order.is_trigger for order in state.all_orders))
        self.assertEqual(state.recent_fills[0].price, 3000.0)

    def test_reconciliation_recovers_known_exchange_position_from_state_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveStateStore(Path(tmpdir) / "live_state.json")
            store.save(
                {
                    "positions": {
                        "ETH": {
                            "symbol": "ETH",
                            "side": "long",
                            "setup": "trend_pullback_long",
                            "confidence": 0.7,
                            "entry_price": 2990,
                            "target_notional_usd": 500,
                            "stop_bps": 80,
                            "time_stop_hours": 24,
                            "effective_leverage": 5,
                        }
                    },
                    "orders": {"ETH": {"protective_oids": {"sl": 456}}},
                    "events": [],
                }
            )
            account_state = parse_account_state(
                account_address="0x0000000000000000000000000000000000000000",
                user_state={
                    "marginSummary": {"accountValue": "1000", "totalMarginUsed": "50"},
                    "withdrawable": "950",
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "ETH",
                                "szi": "0.1",
                                "entryPx": "3000",
                                "positionValue": "300",
                                "marginUsed": "60",
                                "unrealizedPnl": "0",
                                "leverage": {"type": "isolated", "value": 5},
                            }
                        }
                    ],
                },
                spot_state={"balances": []},
                open_orders=[],
                frontend_open_orders=[
                    {
                        "coin": "ETH",
                        "oid": 456,
                        "side": "A",
                        "sz": "0.1",
                        "limitPx": "2900",
                        "reduceOnly": True,
                        "isTrigger": True,
                    }
                ],
                recent_fills=[],
            )
            portfolio = DirectionalPortfolioState()
            report = reconcile_exchange_state(
                account_state=account_state,
                portfolio=portfolio,
                state_store=store,
            )

            self.assertTrue(report.ready)
            self.assertEqual(report.recovered_symbols, ["ETH"])
            self.assertIn("ETH", portfolio.open_positions)
            self.assertEqual(portfolio.open_positions["ETH"].target_notional_usd, 300.0)

    def test_reconciliation_blocks_unknown_exchange_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            account_state = parse_account_state(
                account_address="0x0000000000000000000000000000000000000000",
                user_state={
                    "marginSummary": {"accountValue": "1000", "totalMarginUsed": "50"},
                    "withdrawable": "950",
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "SOL",
                                "szi": "-1",
                                "entryPx": "150",
                                "positionValue": "150",
                                "marginUsed": "30",
                                "unrealizedPnl": "0",
                                "leverage": {"type": "isolated", "value": 5},
                            }
                        }
                    ],
                },
                spot_state={"balances": []},
                open_orders=[],
                frontend_open_orders=[],
                recent_fills=[],
            )
            report = reconcile_exchange_state(
                account_state=account_state,
                portfolio=DirectionalPortfolioState(),
                state_store=LiveStateStore(Path(tmpdir) / "live_state.json"),
            )

            self.assertFalse(report.ready)
            self.assertEqual(report.unknown_exchange_positions, ["SOL"])

    def test_reconciliation_accepts_position_known_by_external_pod_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            own_store = LiveStateStore(Path(tmpdir) / "pod_a_state.json")
            external_store = LiveStateStore(Path(tmpdir) / "pod_c_state.json")
            external_store.save(
                {
                    "positions": {
                        "ETH": {
                            "symbol": "ETH",
                            "side": "long",
                            "setup": "tradfi_cluster_long",
                            "confidence": 0.72,
                        }
                    },
                    "orders": {"ETH": {"protective_oids": {"sl": 456}}},
                    "events": [],
                }
            )
            account_state = parse_account_state(
                account_address="0x0000000000000000000000000000000000000000",
                user_state={
                    "marginSummary": {"accountValue": "1000", "totalMarginUsed": "50"},
                    "withdrawable": "950",
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "ETH",
                                "szi": "0.1",
                                "entryPx": "3000",
                                "positionValue": "300",
                                "marginUsed": "60",
                                "unrealizedPnl": "0",
                                "leverage": {"type": "isolated", "value": 5},
                            }
                        }
                    ],
                },
                spot_state={"balances": []},
                open_orders=[],
                frontend_open_orders=[
                    {
                        "coin": "ETH",
                        "oid": 456,
                        "side": "A",
                        "sz": "0.1",
                        "limitPx": "2900",
                        "reduceOnly": True,
                        "isTrigger": True,
                    }
                ],
                recent_fills=[],
            )
            portfolio = DirectionalPortfolioState()
            report = reconcile_exchange_state(
                account_state=account_state,
                portfolio=portfolio,
                state_store=own_store,
                external_state_stores=[external_store],
            )

            self.assertTrue(report.ready)
            self.assertEqual(report.external_known_positions, ["ETH"])
            self.assertEqual(portfolio.open_positions, {})
            self.assertEqual(report.trigger_orders, [])

    def test_live_state_save_preserves_recovered_order_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveStateStore(Path(tmpdir) / "pod_c_state.json")
            store.save(
                {
                    "positions": {},
                    "orders": {"ETH": {"protective_oids": {"sl": 456}}},
                    "events": [],
                }
            )
            portfolio = DirectionalPortfolioState()
            portfolio.open_positions["ETH"] = open_position_from_metadata(
                {
                    "symbol": "ETH",
                    "side": "long",
                    "setup": "tradfi_cluster_long",
                    "confidence": 0.7,
                    "entry_price": 3000,
                    "target_notional_usd": 300,
                    "margin_usd": 60,
                    "effective_leverage": 5,
                }
            )

            store.save_portfolio(portfolio, orders=None)
            self.assertEqual(store.load()["orders"]["ETH"]["protective_oids"]["sl"], 456)

            portfolio.open_positions.clear()
            store.save_portfolio(portfolio, orders=None)
            self.assertEqual(store.load()["orders"], {})

    def test_parse_order_result_detects_filled_and_error(self) -> None:
        filled = parse_order_result(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"filled": {"oid": 7, "totalSz": "0.1", "avgPx": "3000"}}
                        ]
                    },
                },
            },
            cloid="0x00000000000000000000000000000001",
        )
        rejected = parse_order_result(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "Order must be reduce only"}]},
                },
            }
        )

        self.assertTrue(filled.filled)
        self.assertEqual(filled.oid, 7)
        self.assertEqual(float(filled.filled_size), 0.1)
        self.assertEqual(rejected.status, "error")
        self.assertIn("reduce only", rejected.error or "")

    def test_private_info_client_uses_shared_rate_limiter(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
        config.private_info_requests_per_minute = 5
        limiter = _FakeRateLimiter()
        client = HyperliquidPrivateInfoClient(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
            ),
            info_client=_FakePrivateInfoSdk(),
            now_ms_fn=lambda: 1_000_000,
            sleep_fn=lambda _: None,
            rate_limiter=limiter,
        )

        state = client.fetch_account_state()

        self.assertEqual(state.account_value_usd, 1000.0)
        self.assertEqual(len(limiter.acquires), 5)
        self.assertTrue(
            all(call["key"] == "http_private_info" for call in limiter.acquires)
        )
        self.assertTrue(all(call["capacity"] == 5 for call in limiter.acquires))

    def test_live_exchange_actions_use_order_rate_limiter(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_order_actions_per_minute = 7
        limiter = _FakeRateLimiter()
        exchange = _FakeExchange()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(),  # type: ignore[arg-type]
            order_rate_limiter=limiter,
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="ETH",
            side="long",
            mid_price=100.0,
            spread_bps=1.0,
            notional_usd=10.0,
            timestamp="2026-05-17T00:00:00Z",
        )
        venue.orders_by_symbol["ETH"] = {"protective_oids": {"sl": 123}}
        venue._cancel_known_protective_orders("ETH")

        self.assertIsNotNone(fill)
        self.assertEqual(exchange.cancels, [("ETH", 123)])
        self.assertEqual(len(limiter.acquires), 2)
        self.assertTrue(
            all(
                call["key"] == LIVE_EXCHANGE_ACTION_RATE_LIMIT_KEY
                for call in limiter.acquires
            )
        )
        self.assertTrue(all(call["capacity"] == 7 for call in limiter.acquires))
        self.assertEqual(limiter.successes, [LIVE_EXCHANGE_ACTION_RATE_LIMIT_KEY] * 2)

    def test_live_exchange_rate_limit_response_opens_shared_breaker(self) -> None:
        config = load_config("config/trident.toml")
        limiter = _FakeRateLimiter()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=_FakeExchange(rate_limited=True),
            private_info_client=_FakePrivateAccountClient(),  # type: ignore[arg-type]
            order_rate_limiter=limiter,
            sleep_fn=lambda _: None,
        )

        result = venue._submit_order(
            symbol="ETH",
            is_buy=True,
            size=0.1,
            limit_px=100.0,
            reduce_only=False,
            cloid="0x00000000000000000000000000000001",
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(limiter.rate_limits, [LIVE_EXCHANGE_ACTION_RATE_LIMIT_KEY])

    def test_preflight_blocks_without_credentials(self) -> None:
        old_env = dict(os.environ)
        try:
            for key in (
                "TRIDENT_ACCOUNT_ADDRESS",
                "TRIDENT_SECRET_KEY",
                "TRIDENT_LIVE_CONFIRM",
            ):
                os.environ.pop(key, None)
            args = build_parser().parse_args(["--config", "config/trident.toml", "--skip-user-ws-check"])
            ready, payload = asyncio.run(run_preflight(args))
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertFalse(ready)
        self.assertIn("credential_error", payload["reasons"])


if __name__ == "__main__":
    unittest.main()
