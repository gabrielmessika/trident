import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from app.execution.live import parse_order_result
from app.hyperliquid.private_state import parse_account_state
from app.live.preflight import build_parser, run_preflight
from app.live.reconciliation import reconcile_exchange_state
from app.live.state_store import LiveStateStore, open_position_from_metadata
from app.portfolio.directional_state import DirectionalPortfolioState


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
