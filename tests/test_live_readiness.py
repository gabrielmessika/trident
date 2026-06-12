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
from app.execution.live_cap import apply_live_notional_cap
from app.hyperliquid.private_state import (
    HyperliquidCredentials,
    HyperliquidPrivateInfoClient,
    parse_account_state,
)
from app.live.errors import HyperliquidAPIError
from app.live.preflight import build_parser, run_preflight
from app.live.reconciliation import reconcile_exchange_state
from app.live.state_store import LiveStateStore, open_position_from_metadata
from app.portfolio.directional_state import DirectionalPortfolioState
from app.risk.pod_a_gate import PodARiskGate
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import load_config
from app.trident.types import TradePlan


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
    def __init__(
        self,
        *,
        user_states_by_dex: dict[str, dict[str, object]] | None = None,
        open_orders_by_dex: dict[str, list[object]] | None = None,
        frontend_open_orders_by_dex: dict[str, list[object]] | None = None,
        recent_fills: list[object] | None = None,
        recent_funding: list[object] | None = None,
    ) -> None:
        self.user_states_by_dex = user_states_by_dex or {
            "": {"marginSummary": {"accountValue": "1000", "totalMarginUsed": "0"}}
        }
        self.open_orders_by_dex = open_orders_by_dex or {}
        self.frontend_open_orders_by_dex = frontend_open_orders_by_dex or {}
        self.recent_fills = recent_fills or []
        self.recent_funding = recent_funding or []
        self.user_state_calls: list[str] = []
        self.open_order_calls: list[str] = []
        self.frontend_open_order_calls: list[str] = []
        self.user_funding_calls: list[tuple[str, int, object]] = []

    def query_user_abstraction_state(self, address: str) -> str:
        return "unifiedAccount"

    def user_state(self, address: str, dex: str = "") -> dict[str, object]:
        self.user_state_calls.append(dex)
        return self.user_states_by_dex.get(
            dex,
            {"marginSummary": {"accountValue": "1000", "totalMarginUsed": "0"}},
        )

    def spot_user_state(self, address: str) -> dict[str, object]:
        return {"balances": []}

    def open_orders(self, address: str, dex: str = "") -> list[object]:
        self.open_order_calls.append(dex)
        return self.open_orders_by_dex.get(dex, [])

    def frontend_open_orders(self, address: str, dex: str = "") -> list[object]:
        self.frontend_open_order_calls.append(dex)
        return self.frontend_open_orders_by_dex.get(dex, [])

    def user_fills_by_time(
        self,
        address: str,
        start_ms: int,
        *,
        aggregate_by_time: bool = False,
    ) -> list[object]:
        return self.recent_fills

    def user_funding_history(
        self,
        address: str,
        startTime: int,
        endTime: object | None = None,
    ) -> list[object]:
        self.user_funding_calls.append((address, startTime, endTime))
        return self.recent_funding


class _FakePrivateAccountClient:
    def __init__(
        self,
        *,
        meta: dict[str, object] | None = None,
        user_state: dict[str, object] | None = None,
        recent_fills: list[object] | None = None,
        recent_funding: list[object] | None = None,
    ) -> None:
        self.info_client = _FakeMetaInfoClient(meta or {"universe": []})
        self.user_state = user_state or {
            "marginSummary": {
                "accountValue": "1000",
                "totalMarginUsed": "0",
            }
        }
        self.recent_fills = recent_fills or []
        self.recent_funding = recent_funding or []

    def fetch_account_state(self, *, fills_lookback_hours: float = 24.0, **_: object):
        return parse_account_state(
            account_address="0x0000000000000000000000000000000000000000",
            user_state=self.user_state,
            spot_state={"balances": []},
            open_orders=[],
            frontend_open_orders=[],
            recent_fills=self.recent_fills,
            recent_funding=self.recent_funding,
        )


class _FakeMetaInfoClient:
    def __init__(self, meta: dict[str, object]) -> None:
        self._meta = meta
        self.meta_calls: list[str | None] = []

    def meta(self, dex: str | None = None) -> dict[str, object]:
        self.meta_calls.append(dex)
        by_dex = self._meta.get("by_dex")
        if isinstance(by_dex, dict):
            payload = by_dex.get(dex or "", {"universe": []})
            return payload if isinstance(payload, dict) else {"universe": []}
        return self._meta


class _FakeExchange:
    def __init__(
        self,
        *,
        rate_limited: bool = False,
        post_only_required: bool = False,
        trigger_error: bool = False,
        resolved_symbols: list[str] | None = None,
    ) -> None:
        self.rate_limited = rate_limited
        self.post_only_required = post_only_required
        self.trigger_error = trigger_error
        self.orders: list[dict[str, object]] = []
        self.cancels: list[tuple[str, int]] = []
        if resolved_symbols is not None:
            self.info = type(
                "FakeExchangeInfo",
                (),
                {"name_to_coin": {symbol: symbol for symbol in resolved_symbols}},
            )()

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
        trigger = order_type.get("trigger")
        if isinstance(trigger, dict) and not isinstance(trigger.get("triggerPx"), (int, float)):
            raise ValueError("triggerPx must be numeric")
        self.orders.append(
            {
                "symbol": symbol,
                "is_buy": is_buy,
                "size": size,
                "limit_px": limit_px,
                "order_type": order_type,
                "reduce_only": reduce_only,
            }
        )
        if self.trigger_error and isinstance(trigger, dict):
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "trigger rejected"}]},
                },
            }
        if self.rate_limited:
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "rate limit exceeded"}]},
                },
            }
        if self.post_only_required and order_type != {"limit": {"tif": "Alo"}}:
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"error": "Only post-only orders allowed immediately after network upgrade"}
                        ]
                    },
                },
            }
        if self.post_only_required:
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 8}}]},
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
            recent_funding=[
                {
                    "time": 2,
                    "hash": "0xfunding",
                    "delta": {
                        "type": "funding",
                        "coin": "ETH",
                        "usdc": "-0.03",
                        "szi": "0.2",
                        "fundingRate": "0.0001",
                    },
                }
            ],
        )

        self.assertEqual(state.account_value_usd, 1000.5)
        self.assertEqual(state.spot_usdc_total, 50.0)
        self.assertEqual(state.spot_usdc_hold, 5.0)
        self.assertEqual(state.spot_usdc_available, 45.0)
        self.assertEqual(state.positions["ETH"].side, "long")
        self.assertEqual(len(state.all_orders), 2)
        self.assertTrue(any(order.is_trigger for order in state.all_orders))
        self.assertEqual(state.recent_fills[0].price, 3000.0)
        self.assertEqual(state.recent_funding[0].symbol, "ETH")
        self.assertEqual(state.recent_funding[0].amount_usd, -0.03)
        self.assertEqual(state.recent_funding[0].funding_rate, 0.0001)

    def test_unified_account_uses_spot_usdc_as_available_hl_capital(self) -> None:
        state = parse_account_state(
            account_address="0x0000000000000000000000000000000000000000",
            account_mode="unifiedAccount",
            user_state={
                "marginSummary": {"accountValue": "0", "totalMarginUsed": "0"},
                "withdrawable": "0",
                "assetPositions": [],
            },
            spot_state={"balances": [{"coin": "USDC", "total": "994.363948", "hold": "1.5"}]},
            open_orders=[],
            frontend_open_orders=[],
            recent_fills=[],
        )
        report = reconcile_exchange_state(
            account_state=state,
            portfolio=DirectionalPortfolioState(),
            state_store=LiveStateStore(Path(tempfile.gettempdir()) / "unused_live_state.json"),
        )

        self.assertEqual(state.spot_usdc_available, 992.863948)
        self.assertEqual(state.hl_available_usd, 992.863948)
        self.assertEqual(state.hl_capital_source, "unified_spot_usdc")
        self.assertEqual(report.account_mode, "unifiedAccount")
        self.assertEqual(report.hl_available_usd, 992.863948)
        self.assertEqual(report.to_dict()["spot_usdc_available"], 992.863948)

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
                                "positionValue": "330",
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

    def test_live_state_save_merges_existing_open_order_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveStateStore(Path(tmpdir) / "pod_c_state.json")
            store.save(
                {
                    "positions": {},
                    "orders": {"BTC": {"protective_oids": {"sl": 111, "tp": 222}}},
                    "events": [],
                }
            )
            portfolio = DirectionalPortfolioState()
            for symbol in ("BTC", "SOL"):
                portfolio.open_positions[symbol] = open_position_from_metadata(
                    {
                        "symbol": symbol,
                        "side": "long",
                        "setup": "tradfi_cluster_long",
                        "confidence": 0.7,
                        "entry_price": 100,
                        "target_notional_usd": 100,
                    }
                )

            store.save_portfolio(
                portfolio,
                orders={"SOL": {"protective_oids": {"sl": 333, "tp": 444}}},
            )
            stored_orders = store.load()["orders"]

            self.assertEqual(stored_orders["BTC"]["protective_oids"], {"sl": 111, "tp": 222})
            self.assertEqual(stored_orders["SOL"]["protective_oids"], {"sl": 333, "tp": 444})

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
        self.assertEqual(len(limiter.acquires), 8)
        self.assertTrue(
            all(call["key"] == "http_private_info" for call in limiter.acquires)
        )
        self.assertTrue(all(call["capacity"] == 5 for call in limiter.acquires))

    def test_private_info_client_can_fetch_user_funding_history(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
        limiter = _FakeRateLimiter()
        sdk = _FakePrivateInfoSdk(
            recent_funding=[
                {
                    "time": 1_000_000,
                    "hash": "0xfunding",
                    "delta": {
                        "type": "funding",
                        "coin": "ETH",
                        "usdc": "-0.03",
                        "szi": "0.2",
                        "fundingRate": "0.0001",
                    },
                }
            ]
        )
        client = HyperliquidPrivateInfoClient(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
            ),
            info_client=sdk,
            now_ms_fn=lambda: 2_000_000,
            sleep_fn=lambda _: None,
            rate_limiter=limiter,
        )

        state = client.fetch_account_state(funding_lookback_hours=1.0)

        self.assertEqual(len(state.recent_funding), 1)
        self.assertEqual(state.recent_funding[0].symbol, "ETH")
        self.assertEqual(state.recent_funding[0].amount_usd, -0.03)
        self.assertEqual(sdk.user_funding_calls[0][1], 2_000_000 - 3_600_000)
        self.assertEqual(len(limiter.acquires), 9)

    def test_private_info_client_merges_builder_dex_account_state(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
        sdk = _FakePrivateInfoSdk(
            user_states_by_dex={
                "": {
                    "marginSummary": {"accountValue": "1000", "totalMarginUsed": "0"},
                    "assetPositions": [],
                },
                "xyz": {
                    "marginSummary": {"accountValue": "1000", "totalMarginUsed": "25"},
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "xyz:GOLD",
                                "szi": "0.0562",
                                "entryPx": "4442.39",
                                "positionValue": "249.90",
                                "marginUsed": "25",
                                "unrealizedPnl": "0.24",
                                "leverage": {"type": "isolated", "value": 10},
                            }
                        }
                    ],
                },
            },
            open_orders_by_dex={
                "xyz": [
                    {
                        "coin": "xyz:GOLD",
                        "oid": 10,
                        "side": "A",
                        "sz": "0.0562",
                        "limitPx": "4500",
                        "reduceOnly": True,
                    }
                ]
            },
            frontend_open_orders_by_dex={
                "xyz": [
                    {
                        "coin": "xyz:GOLD",
                        "oid": 11,
                        "side": "A",
                        "origSz": "0.0562",
                        "limitPx": "4380",
                        "reduceOnly": True,
                        "isTrigger": True,
                        "triggerPx": "4380",
                        "orderType": "Stop Market",
                    }
                ]
            },
        )
        client = HyperliquidPrivateInfoClient(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
            ),
            info_client=sdk,
            now_ms_fn=lambda: 1_000_000,
            sleep_fn=lambda _: None,
            rate_limiter=_FakeRateLimiter(),
        )

        state = client.fetch_account_state()

        self.assertEqual(sdk.user_state_calls, ["", "xyz"])
        self.assertEqual(sdk.open_order_calls, ["", "xyz"])
        self.assertEqual(sdk.frontend_open_order_calls, ["", "xyz"])
        self.assertIn("XYZ:GOLD", state.positions)
        self.assertEqual(state.positions["XYZ:GOLD"].side, "long")
        self.assertEqual([order.symbol for order in state.all_orders], ["XYZ:GOLD", "XYZ:GOLD"])
        self.assertTrue(any(order.is_trigger for order in state.all_orders))

    def test_private_info_client_can_fetch_account_mode_for_ui_capital(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
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

        state = client.fetch_account_state(include_account_mode=True)

        self.assertEqual(state.account_mode, "unifiedAccount")
        self.assertEqual(len(limiter.acquires), 9)

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

    def test_live_open_fill_persists_pending_position_for_recovery(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        callback_count = 0

        def note_orders_changed() -> None:
            nonlocal callback_count
            callback_count += 1

        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=_FakeExchange(),
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "ETH", "szDecimals": 4}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
            orders_changed_callback=note_orders_changed,
        )

        fill = venue.open_fill(
            symbol="ETH",
            side="long",
            mid_price=3000.0,
            spread_bps=1.0,
            notional_usd=100.0,
            timestamp="2026-06-07T02:46:06Z",
            plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.8,
                target_notional_usd=100.0,
                stop_bps=80.0,
                time_stop_hours=24,
                setup_details={"market_cluster": "crypto"},
            ),
        )

        self.assertIsNotNone(fill)
        self.assertEqual(callback_count, 2)
        metadata = venue.orders_by_symbol["ETH"]
        self.assertEqual(metadata["entry_oid"], 7)
        self.assertEqual(metadata["pending_position"]["setup"], "trend_pullback_long")  # type: ignore[index]
        self.assertEqual(metadata["pending_position"]["entry_price"], fill.price)  # type: ignore[index]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveStateStore(Path(tmpdir) / "live_state.json")
            store.save_portfolio(DirectionalPortfolioState(), orders=venue.orders_by_symbol)
            self.assertIn("ETH", store.load()["orders"])
            self.assertEqual(store.load()["positions"], {})

            portfolio = DirectionalPortfolioState()
            report = reconcile_exchange_state(
                account_state=parse_account_state(
                    account_address="0x0000000000000000000000000000000000000000",
                    user_state={
                        "marginSummary": {
                            "accountValue": "1000",
                            "totalMarginUsed": "10",
                        },
                        "assetPositions": [
                            {
                                "position": {
                                    "coin": "ETH",
                                    "szi": "0.03",
                                    "entryPx": "3000",
                                    "positionValue": "90",
                                    "marginUsed": "10",
                                    "unrealizedPnl": "0",
                                    "leverage": {"type": "isolated", "value": 10},
                                }
                            }
                        ],
                    },
                    spot_state={"balances": []},
                    open_orders=[],
                    frontend_open_orders=[],
                    recent_fills=[],
                ),
                portfolio=portfolio,
                state_store=store,
            )

        self.assertTrue(report.ready)
        self.assertEqual(report.recovered_symbols, ["ETH"])
        self.assertIn("ETH", portfolio.open_positions)
        self.assertEqual(portfolio.open_positions["ETH"].setup, "trend_pullback_long")
        self.assertEqual(portfolio.open_positions["ETH"].target_notional_usd, 90.0)

    def test_live_open_fill_persists_before_protective_order_failure(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        callback_count = 0

        def note_orders_changed() -> None:
            nonlocal callback_count
            callback_count += 1

        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=_FakeExchange(trigger_error=True),
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "ETH", "szDecimals": 4}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
            orders_changed_callback=note_orders_changed,
        )

        with self.assertRaises(HyperliquidAPIError):
            venue.open_fill(
                symbol="ETH",
                side="long",
                mid_price=3000.0,
                spread_bps=1.0,
                notional_usd=100.0,
                timestamp="2026-06-07T02:46:06Z",
                plan=TradePlan(
                    symbol="ETH",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.8,
                    target_notional_usd=100.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    setup_details={"market_cluster": "crypto"},
                ),
            )

        self.assertEqual(callback_count, 1)
        metadata = venue.orders_by_symbol["ETH"]
        self.assertEqual(metadata["entry_oid"], 7)
        self.assertEqual(metadata["protective_oids"], {})
        self.assertEqual(metadata["pending_position"]["setup"], "trend_pullback_long")  # type: ignore[index]
        self.assertEqual(metadata["stop_grace"]["normal_stop_price"], 2978.4)  # type: ignore[index]

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveStateStore(Path(tmpdir) / "live_state.json")
            store.save_portfolio(DirectionalPortfolioState(), orders=venue.orders_by_symbol)
            self.assertIn("ETH", store.load()["orders"])

    def test_live_orders_use_hyperliquid_price_and_size_precision(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        exchange = _FakeExchange()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "ETH", "szDecimals": 4}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="ETH",
            side="long",
            mid_price=2140.45,
            spread_bps=1.0,
            notional_usd=100.0,
            timestamp="2026-05-17T00:00:00Z",
        )

        self.assertIsNotNone(fill)
        self.assertEqual(exchange.orders[0]["limit_px"], 2142.2)
        self.assertEqual(exchange.orders[0]["size"], 0.0466)

    def test_live_builder_dex_orders_use_sdk_wire_symbol_and_canonical_state(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        exchange = _FakeExchange(resolved_symbols=["xyz:GOLD"])
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={
                    "by_dex": {
                        "": {"universe": []},
                        "xyz": {"universe": [{"name": "xyz:GOLD", "szDecimals": 3}]},
                    }
                }
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="XYZ:GOLD",
            side="long",
            mid_price=4700.0,
            spread_bps=1.0,
            notional_usd=100.0,
            timestamp="2026-05-27T00:00:00Z",
        )
        venue.orders_by_symbol["XYZ:GOLD"] = {"protective_oids": {"sl": 123}}
        venue._cancel_known_protective_orders("XYZ:GOLD")

        self.assertIsNotNone(fill)
        self.assertEqual(fill.symbol, "XYZ:GOLD")
        self.assertEqual(exchange.orders[0]["symbol"], "xyz:GOLD")
        self.assertEqual(exchange.orders[0]["size"], 0.021)
        self.assertIn("XYZ:GOLD", venue.orders_by_symbol)
        self.assertEqual(exchange.cancels, [("xyz:GOLD", 123)])

    def test_live_unresolved_builder_dex_asset_blocks_without_sdk_keyerror(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        exchange = _FakeExchange(resolved_symbols=["ETH"])
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="XYZ:SILVER",
            side="long",
            mid_price=32.0,
            spread_bps=1.0,
            notional_usd=50.0,
            timestamp="2026-05-27T00:01:00Z",
        )

        self.assertIsNone(fill)
        self.assertEqual(exchange.orders, [])
        self.assertEqual(
            venue.last_block_reason_by_symbol["XYZ:SILVER"],
            "asset_not_resolved:XYZ:SILVER",
        )

    def test_live_cap_resizes_plan_before_pod_c_risk_gate(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 250.0
        plan = TradePlan(
            symbol="XYZ:GOLD",
            side="long",
            setup="tradfi_continuation_long",
            confidence=0.8,
            target_notional_usd=687.5,
            stop_bps=65.0,
            time_stop_hours=6,
            margin_usd=27.5,
            requested_leverage=2.0,
            effective_leverage=25.0,
            risk_budget_usd=12.5,
            expected_loss_usd=4.46875,
        )

        capped = apply_live_notional_cap(
            plan,
            config.trident.execution.live_max_order_notional_usd,
        )
        decision = PodCRiskGate(config).evaluate_many([capped])[0]

        self.assertEqual(capped.target_notional_usd, 250.0)
        self.assertEqual(capped.margin_usd, 27.5)
        self.assertAlmostEqual(capped.effective_leverage, 9.0909, places=4)
        self.assertAlmostEqual(capped.expected_loss_usd, 1.625, places=6)
        self.assertTrue(capped.setup_details["live_cap_active"])
        self.assertTrue(decision.accepted)

    def test_live_cap_does_not_hide_pod_c_margin_floor(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 250.0
        config.pod_c.blocked_symbols = []
        plan = TradePlan(
            symbol="XYZ:SILVER",
            side="long",
            setup="tradfi_continuation_long",
            confidence=0.8,
            target_notional_usd=412.5,
            stop_bps=65.0,
            time_stop_hours=6,
            margin_usd=16.5,
            requested_leverage=2.0,
            effective_leverage=25.0,
            risk_budget_usd=12.5,
            expected_loss_usd=2.68125,
        )

        capped = apply_live_notional_cap(
            plan,
            config.trident.execution.live_max_order_notional_usd,
        )
        decision = PodCRiskGate(config).evaluate_many([capped])[0]

        self.assertEqual(capped.target_notional_usd, 250.0)
        self.assertEqual(capped.margin_usd, 16.5)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "margin_below_min")

    def test_live_cap_respects_asset_leverage_limit(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 250.0
        plan = TradePlan(
            symbol="JUP",
            side="long",
            setup="trend_pullback_long",
            confidence=0.8,
            target_notional_usd=344.875,
            stop_bps=80.0,
            time_stop_hours=24,
            margin_usd=34.4875,
            requested_leverage=2.0,
            effective_leverage=10.0,
            risk_budget_usd=17.5,
            expected_loss_usd=2.759,
            setup_details={"regime": "TrendExpansion"},
        )

        capped = apply_live_notional_cap(plan, 250.0, max_leverage=5.0)
        decision = PodARiskGate(config).evaluate_many([capped])[0]

        self.assertEqual(capped.target_notional_usd, 172.4375)
        self.assertEqual(capped.margin_usd, 34.4875)
        self.assertEqual(capped.effective_leverage, 5.0)
        self.assertTrue(capped.setup_details["live_cap_leverage_limited"])
        self.assertTrue(decision.accepted)

    def test_live_close_uses_exact_exchange_position_size(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        exchange = _FakeExchange()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "BTC", "szDecimals": 5}]},
                user_state={
                    "marginSummary": {"accountValue": "1000", "totalMarginUsed": "10"},
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "BTC",
                                "szi": "0.12345",
                                "entryPx": "100",
                                "positionValue": "12.5",
                                "marginUsed": "1.25",
                                "unrealizedPnl": "0.15",
                                "leverage": {"type": "isolated", "value": 10},
                            }
                        }
                    ],
                },
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.close_fill(
            symbol="BTC",
            side="long",
            mid_price=101.0,
            spread_bps=1.0,
            notional_usd=999.0,
            timestamp="2026-05-19T00:00:00Z",
        )

        self.assertIsNotNone(fill)
        self.assertEqual(exchange.orders[0]["size"], 0.12345)
        self.assertTrue(exchange.orders[0]["reduce_only"])

    def test_live_close_fill_persists_exchange_fee_metadata(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        exchange = _FakeExchange()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "ETH", "szDecimals": 4}]},
                recent_fills=[
                    {
                        "coin": "ETH",
                        "oid": 7,
                        "side": "A",
                        "dir": "Close Long",
                        "sz": "0.5",
                        "px": "99.5",
                        "closedPnl": "1.23",
                        "fee": "0.04",
                        "time": 1780619400000,
                    }
                ],
                recent_funding=[
                    {
                        "time": 1780617600000,
                        "hash": "0xfunding",
                        "delta": {
                            "type": "funding",
                            "coin": "ETH",
                            "usdc": "-0.03",
                            "szi": "0.5",
                            "fundingRate": "0.0001",
                        },
                    }
                ],
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.close_fill(
            symbol="ETH",
            side="long",
            mid_price=100.0,
            spread_bps=1.0,
            notional_usd=50.0,
            timestamp="2026-06-05T00:30:00Z",
        )

        self.assertIsNotNone(fill)
        self.assertEqual(fill.action, "close")
        self.assertEqual(fill.oid, 7)
        self.assertEqual(fill.price, 99.5)
        self.assertEqual(fill.notional_usd, 49.75)
        self.assertEqual(fill.fee_usd, 0.04)
        self.assertTrue(fill.exchange_fill_available)
        self.assertEqual(fill.exchange_fee_usd, 0.04)
        self.assertEqual(fill.exchange_closed_pnl_usd, 1.23)
        self.assertEqual(fill.exchange_direction, "Close Long")
        self.assertEqual(fill.exchange_timestamp_ms, 1780619400000)
        self.assertEqual(fill.fee_source, "exchange_user_fills")
        self.assertEqual(fill.exchange_fill["oid"], 7)  # type: ignore[index]
        self.assertEqual(fill.funding_source, "exchange_user_funding_history_unattributed")
        self.assertEqual(fill.funding_payment_count, 1)
        self.assertEqual(fill.funding_payments[0]["amount_usd"], -0.03)

    def test_live_protective_triggers_use_hyperliquid_price_precision(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        exchange = _FakeExchange()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "ETH", "szDecimals": 4}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="ETH",
            side="long",
            mid_price=2140.45,
            spread_bps=1.0,
            notional_usd=100.0,
            timestamp="2026-05-17T00:00:00Z",
            plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="test_mainnet_precision",
                confidence=0.8,
                target_notional_usd=100.0,
                stop_bps=45.0,
                time_stop_hours=6,
                invalidation_price=2132.56789,
            ),
        )

        self.assertIsNotNone(fill)
        self.assertEqual(exchange.orders[1]["limit_px"], 2132.6)
        self.assertEqual(
            exchange.orders[1]["order_type"],
            {"trigger": {"isMarket": True, "triggerPx": 2132.6, "tpsl": "sl"}},
        )

    def test_live_sub_dollar_trigger_price_respects_hyperliquid_decimal_limit(self) -> None:
        config = load_config("config/trident.toml")
        exchange = _FakeExchange()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "ARB", "szDecimals": 1}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        self.assertEqual(venue._round_price(0.0803928, symbol="ARB"), 0.08039)
        self.assertEqual(venue._round_price(0.079249, symbol="ARB"), 0.07925)

    def test_live_stop_grace_uses_catastrophic_sl_then_refreshes_normal_sl(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        config.trident.execution.live_block_stop_grace_setups = False
        exchange = _FakeExchange()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "ETH", "szDecimals": 4}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="ETH",
            side="long",
            mid_price=3000.0,
            spread_bps=1.0,
            notional_usd=100.0,
            timestamp="2026-05-27T00:00:00Z",
            plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.8,
                target_notional_usd=100.0,
                stop_bps=80.0,
                time_stop_hours=24,
                setup_details={"market_cluster": "crypto"},
            ),
        )

        self.assertIsNotNone(fill)
        self.assertEqual(len(exchange.orders), 2)
        self.assertEqual(exchange.orders[1]["limit_px"], 2954.4)
        self.assertEqual(
            exchange.orders[1]["order_type"],
            {"trigger": {"isMarket": True, "triggerPx": 2954.4, "tpsl": "sl"}},
        )
        metadata = venue.orders_by_symbol["ETH"]
        stop_grace = metadata["stop_grace"]  # type: ignore[index]
        self.assertEqual(stop_grace["grace_minutes"], config.pod_a.stop_grace_minutes)  # type: ignore[index]
        self.assertEqual(stop_grace["normal_stop_price"], 2978.4)  # type: ignore[index]
        self.assertEqual(stop_grace["catastrophic_stop_price"], 2954.4)  # type: ignore[index]
        self.assertFalse(stop_grace["normal_stop_placed"])  # type: ignore[index]

        self.assertFalse(
            venue.refresh_stop_grace_orders(
                "ETH",
                now="2026-05-27T00:30:00Z",
            )
        )
        self.assertTrue(
            venue.refresh_stop_grace_orders(
                "ETH",
                now="2026-05-27T01:01:00Z",
            )
        )
        self.assertEqual(exchange.cancels, [("ETH", 7)])
        self.assertEqual(exchange.orders[2]["limit_px"], 2978.4)
        refreshed = venue.orders_by_symbol["ETH"]["stop_grace"]  # type: ignore[index]
        self.assertTrue(refreshed["normal_stop_placed"])  # type: ignore[index]
        self.assertFalse(refreshed["active"])  # type: ignore[index]

    def test_live_stop_grace_kill_switch_can_still_block_entries(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        config.trident.execution.live_block_stop_grace_setups = True
        exchange = _FakeExchange()
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "ETH", "szDecimals": 4}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="ETH",
            side="long",
            mid_price=3000.0,
            spread_bps=1.0,
            notional_usd=100.0,
            timestamp="2026-05-27T00:00:00Z",
            plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.8,
                target_notional_usd=100.0,
                stop_bps=80.0,
                time_stop_hours=24,
                setup_details={"market_cluster": "crypto"},
            ),
        )

        self.assertIsNone(fill)
        self.assertEqual(exchange.orders, [])
        self.assertTrue(
            venue.last_block_reason_by_symbol["ETH"].startswith(
                "stop_grace_exchange_sl_mismatch:"
            )
        )

    def test_optional_live_protective_trigger_failure_keeps_entry_fill(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        config.trident.execution.live_require_protective_orders = False
        exchange = _FakeExchange(trigger_error=True)
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "SOL", "szDecimals": 2}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="SOL",
            side="long",
            mid_price=85.0,
            spread_bps=1.0,
            notional_usd=10.0,
            timestamp="2026-05-19T10:22:00Z",
            plan=TradePlan(
                symbol="SOL",
                side="long",
                setup="test_optional_protective",
                confidence=0.8,
                target_notional_usd=10.0,
                stop_bps=45.0,
                time_stop_hours=6,
                take_profit_bps=80.0,
            ),
        )

        self.assertIsNotNone(fill)
        self.assertEqual(fill.protective_oids, {})
        self.assertEqual(venue.orders_by_symbol["SOL"]["entry_oid"], 7)
        self.assertEqual(len(exchange.orders), 3)

    def test_live_open_retries_post_only_after_upgrade_and_tracks_pending_order(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 200.0
        config.trident.execution.live_post_only_retry_on_upgrade = True
        config.trident.execution.live_post_only_buffer_bps = 1.0
        exchange = _FakeExchange(post_only_required=True)
        venue = LiveExecutionVenue(
            config,
            HyperliquidCredentials(
                account_address="0x0000000000000000000000000000000000000000",
                secret_key="0x" + "1" * 64,
                live_confirm="I_UNDERSTAND_REAL_ORDERS",
            ),
            exchange_client=exchange,
            private_info_client=_FakePrivateAccountClient(
                meta={"universe": [{"name": "BTC", "szDecimals": 5}]}
            ),  # type: ignore[arg-type]
            order_rate_limiter=_FakeRateLimiter(),
            sleep_fn=lambda _: None,
        )

        fill = venue.open_fill(
            symbol="BTC",
            side="long",
            mid_price=100.0,
            spread_bps=4.0,
            notional_usd=50.0,
            timestamp="2026-05-19T08:25:00Z",
        )

        self.assertIsNone(fill)
        self.assertEqual(exchange.orders[0]["order_type"], {"limit": {"tif": "Ioc"}})
        self.assertEqual(exchange.orders[1]["order_type"], {"limit": {"tif": "Alo"}})
        self.assertEqual(exchange.orders[1]["limit_px"], 100.0)
        self.assertEqual(venue.orders_by_symbol["BTC"]["entry_oid"], 8)
        self.assertEqual(
            venue.orders_by_symbol["BTC"]["pending_position"]["side"],  # type: ignore[index]
            "long",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LiveStateStore(Path(tmpdir) / "live_state.json")
            store.save_portfolio(DirectionalPortfolioState(), orders=venue.orders_by_symbol)
            self.assertIn("BTC", store.load()["orders"])

            portfolio = DirectionalPortfolioState()
            report = reconcile_exchange_state(
                account_state=parse_account_state(
                    account_address="0x0000000000000000000000000000000000000000",
                    user_state={
                        "marginSummary": {
                            "accountValue": "1000",
                            "totalMarginUsed": "5",
                        },
                        "assetPositions": [
                            {
                                "position": {
                                    "coin": "BTC",
                                    "szi": "0.5",
                                    "entryPx": "100",
                                    "positionValue": "50",
                                    "marginUsed": "5",
                                    "unrealizedPnl": "0",
                                    "leverage": {"type": "isolated", "value": 10},
                                }
                            }
                        ],
                    },
                    spot_state={"balances": []},
                    open_orders=[],
                    frontend_open_orders=[],
                    recent_fills=[],
                ),
                portfolio=portfolio,
                state_store=store,
            )
            self.assertTrue(report.ready)
            self.assertEqual(report.recovered_symbols, ["BTC"])
            self.assertIn("BTC", portfolio.open_positions)

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
