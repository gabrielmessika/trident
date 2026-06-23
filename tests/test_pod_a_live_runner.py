import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from app.hyperliquid.private_state import parse_account_state
from app.live.pod_a_live_runner import PodALiveRunner
from app.live.state_store import LiveStateStore
from app.persistence.journal import JsonlJournal
from app.settings import load_config
from app.trident.types import TradePlan


class _FakeCollector:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records
        self.coins = ["BTC", "ETH"]
        self.stats = type(
            "Stats",
            (),
            {
                "messages_processed": 4,
                "snapshots_written": len(records),
                "reconnect_count": 0,
                "heartbeat_count": 0,
                "pong_count": 0,
                "timeout_count": 0,
                "api_error_count": 0,
                "rate_limit_error_count": 0,
                "last_error": None,
            },
        )()
        self.builder = type(
            "Builder",
            (),
            {
                "finalize": lambda self: [],
            },
        )()
        self.writer = type(
            "Writer",
            (),
            {
                "append_many": lambda self, records: list(records),
            },
        )()


class _FakeInfoClient:
    def __init__(self, mids: dict[str, float]) -> None:
        self._mids = mids

    def fetch_all_mids(self, *, symbols: list[str] | None = None) -> dict[str, float]:
        if not symbols:
            return dict(self._mids)
        requested = {str(symbol).strip().upper() for symbol in symbols}
        return {
            symbol: price
            for symbol, price in self._mids.items()
            if str(symbol).strip().upper() in requested
        }


class _FakePrivateClient:
    def __init__(self, account_state: object) -> None:
        self.account_state = account_state

    def fetch_account_state(self, *, fills_lookback_hours: float, **_: object) -> object:
        return self.account_state


def _timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _account_state_with_recent_fills(
    fills: list[dict[str, object]],
    *,
    recent_funding: list[dict[str, object]] | None = None,
) -> object:
    return parse_account_state(
        account_address="0x0000000000000000000000000000000000000000",
        user_state={
            "marginSummary": {"accountValue": "1000", "totalMarginUsed": "0"},
            "assetPositions": [],
        },
        spot_state={"balances": []},
        open_orders=[],
        frontend_open_orders=[],
        recent_fills=fills,
        recent_funding=recent_funding,
    )


class PodALiveRunnerTests(unittest.TestCase):
    def test_live_runner_processes_stream_records(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(
                config.pod_a,
                allowed_setups=["liquidity_sweep_reclaim_long", "trend_pullback_long"],
                disabled_setups=[],
                blocked_regimes=[],
                allowed_setups_in_blocked_regimes=["liquidity_sweep_reclaim_long"],
            ),
        )
        runner = PodALiveRunner(config, coins=["BTC", "ETH"])
        records = [
            {
                "timestamp": "2026-04-05T09:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 32.0,
                    "atr_ratio": 1.2,
                    "range_width_bps": 180.0,
                    "structure_score": 0.55,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "ETH",
                        "price": 3100.0,
                        "ema_fast": 3090.0,
                        "ema_slow": 3050.0,
                        "vwap_distance_bps": -8.0,
                        "structure_score": 0.62,
                        "funding_rate": 0.0001,
                        "spread_bps": 1.2,
                        "btc_aligned": True,
                        "book_imbalance": 0.1,
                        "trade_flow_bias": 0.4,
                        "bucket_volume": 100.0,
                        "bucket_trade_count": 20,
                        "bucket_range_bps": 25.0,
                        "source": "test_live",
                    },
                    {
                        "symbol": "BTC",
                        "price": 68000.0,
                        "ema_fast": 67950.0,
                        "ema_slow": 67800.0,
                        "vwap_distance_bps": -5.0,
                        "structure_score": 0.55,
                        "funding_rate": 0.0,
                        "spread_bps": 0.8,
                        "btc_aligned": True,
                        "book_imbalance": 0.1,
                        "trade_flow_bias": 0.3,
                        "bucket_volume": 2.0,
                        "bucket_trade_count": 10,
                        "bucket_range_bps": 40.0,
                        "source": "test_live",
                    },
                ],
            }
        ]
        runner.collector = _FakeCollector(records)  # type: ignore[assignment]

        async def fake_iter_live_records(**_: object):
            for record in records:
                yield record

        runner._iter_live_records = fake_iter_live_records  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = Path(tmpdir) / "live_journal.jsonl"
            result = asyncio.run(
                runner.run(
                    max_runtime_seconds=0.1,
                    journal_path=journal_path,
                )
            )

            self.assertEqual(result["records_processed"], 1)
            self.assertEqual(result["signal_count"], 2)
            self.assertEqual(result["accepted_count"], 2)
            self.assertEqual(result["opened_count"], 2)
            self.assertEqual(result["collector"]["snapshots_written"], 1)
            self.assertTrue(journal_path.exists())
            journal_records = [
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            signal_records = [
                record for record in journal_records if record.get("event_type") == "signal"
            ]
            self.assertTrue(signal_records)
            regime_shadow = signal_records[0]["signal"]["regime_shadow"]
            self.assertEqual(regime_shadow["regime_shadow_mode"], "observation_only")
            self.assertTrue(regime_shadow["live_action_unchanged"])
            order_block_shadow = signal_records[0]["signal"]["order_block_shadow"]
            self.assertEqual(order_block_shadow["order_block_shadow_mode"], "observation_only")
            self.assertTrue(order_block_shadow["live_action_unchanged"])
            dynamic_symbol_guard = signal_records[0]["signal"]["dynamic_symbol_guard"]
            self.assertEqual(dynamic_symbol_guard["symbol_guard_shadow_mode"], "observation_only")
            self.assertTrue(dynamic_symbol_guard["symbol_guard_live_action_unchanged"])
            self.assertIn("bull_regime_score", signal_records[0]["signal"]["setup_details"])
            self.assertIn("would_block_long", signal_records[0]["signal"]["setup_details"])
            self.assertIn(
                "would_block_long_order_block_shadow",
                signal_records[0]["signal"]["setup_details"],
            )
            self.assertIn(
                "would_block_dynamic_symbol_guard",
                signal_records[0]["signal"]["setup_details"],
            )
            runtime_status = json.loads(Path("logs/pod_a_live_status.json").read_text(encoding="utf-8"))
            open_positions = runtime_status["open_positions"]
            self.assertEqual(len(open_positions), 2)
            eth_position = next(item for item in open_positions if item["symbol"] == "ETH")
            self.assertEqual(eth_position["current_price"], 3100.0)
            self.assertIn("unrealized_pnl_usd", eth_position)
            self.assertIn("take_profit_bps", eth_position)
            self.assertIn("trailing_activation_bps", eth_position)
            self.assertIn("trailing_distance_bps", eth_position)
            self.assertIn("best_price_seen", eth_position)

    def test_live_quality_sizing_reduces_weak_plan_without_blocking_it(self) -> None:
        config = load_config("config/trident.toml")
        runner = PodALiveRunner(config, coins=["BTC"])
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="trend_pullback_long",
            confidence=0.55,
            target_notional_usd=200.0,
            stop_bps=45.0,
            time_stop_hours=24,
            margin_usd=100.0,
            risk_budget_usd=2.0,
            expected_loss_usd=0.9,
            setup_details={"market_cluster": "crypto"},
        )

        shaped = runner._shape_live_trade_plans(
            [plan],
            timestamp="2026-06-09T00:00:00Z",
        )

        self.assertEqual(len(shaped), 1)
        shaped_plan = shaped[0]
        self.assertEqual(shaped_plan.target_notional_usd, 100.0)
        self.assertEqual(shaped_plan.margin_usd, 50.0)
        self.assertTrue(bool(shaped_plan.setup_details["live_quality_sizing_active"]))
        self.assertEqual(shaped_plan.setup_details["live_quality_sizing_multiplier"], 0.5)
        self.assertIn("low_confidence", shaped_plan.setup_details["live_quality_sizing_reasons"])

    def test_dynamic_symbol_guard_sizing_can_be_disabled_by_config(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(
                config.pod_a,
                live_quality_sizing_enabled=False,
                dynamic_symbol_guard_live_sizing_enabled=False,
            ),
        )
        runner = PodALiveRunner(config, coins=["BTC"])
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="trend_pullback_long",
            confidence=0.80,
            target_notional_usd=200.0,
            stop_bps=45.0,
            time_stop_hours=24,
            margin_usd=100.0,
            risk_budget_usd=2.0,
            expected_loss_usd=0.9,
            setup_details={
                "market_cluster": "crypto",
                "symbol_guard_state": "quarantine",
                "would_block_dynamic_symbol_guard": True,
                "would_reduce_cap_dynamic_symbol_guard": True,
                "symbol_guard_live_action_unchanged": True,
            },
        )

        shaped = runner._shape_live_trade_plans(
            [plan],
            timestamp="2026-06-22T00:00:00Z",
        )

        self.assertEqual(len(shaped), 1)
        shaped_plan = shaped[0]
        self.assertEqual(shaped_plan.target_notional_usd, 200.0)
        self.assertNotIn("dynamic_symbol_guard_live_sizing_active", shaped_plan.setup_details)
        self.assertTrue(bool(shaped_plan.setup_details["symbol_guard_live_action_unchanged"]))

    def test_dynamic_symbol_guard_sizing_reduces_quarantine_without_blocking(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(
                config.pod_a,
                live_quality_sizing_enabled=False,
                dynamic_symbol_guard_live_sizing_enabled=True,
            ),
        )
        runner = PodALiveRunner(config, coins=["BTC"])
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="trend_pullback_long",
            confidence=0.80,
            target_notional_usd=200.0,
            stop_bps=45.0,
            time_stop_hours=24,
            margin_usd=100.0,
            risk_budget_usd=2.0,
            expected_loss_usd=0.9,
            setup_details={
                "market_cluster": "crypto",
                "symbol_guard_state": "quarantine",
                "would_block_dynamic_symbol_guard": True,
                "would_reduce_cap_dynamic_symbol_guard": True,
                "symbol_guard_live_action_unchanged": True,
            },
        )

        shaped = runner._shape_live_trade_plans(
            [plan],
            timestamp="2026-06-22T00:00:00Z",
        )

        self.assertEqual(len(shaped), 1)
        shaped_plan = shaped[0]
        self.assertEqual(shaped_plan.target_notional_usd, 100.0)
        self.assertEqual(shaped_plan.margin_usd, 50.0)
        self.assertEqual(shaped_plan.risk_budget_usd, 1.0)
        self.assertEqual(shaped_plan.expected_loss_usd, 0.45)
        self.assertTrue(bool(shaped_plan.setup_details["dynamic_symbol_guard_live_sizing_active"]))
        self.assertEqual(
            shaped_plan.setup_details["dynamic_symbol_guard_live_sizing_multiplier"],
            0.5,
        )
        self.assertEqual(
            shaped_plan.setup_details["dynamic_symbol_guard_live_sizing_reason"],
            "quarantine",
        )
        self.assertFalse(bool(shaped_plan.setup_details["symbol_guard_live_action_unchanged"]))

    def test_dynamic_symbol_guard_recovery_sizing_reduces_unproven_normal_symbol(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(
                config.pod_a,
                live_quality_sizing_enabled=False,
                dynamic_symbol_guard_live_sizing_enabled=False,
                dynamic_symbol_guard_recovery_sizing_enabled=True,
            ),
        )
        runner = PodALiveRunner(config, coins=["BTC"])
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="trend_pullback_long",
            confidence=0.80,
            target_notional_usd=200.0,
            stop_bps=45.0,
            time_stop_hours=24,
            margin_usd=100.0,
            risk_budget_usd=2.0,
            expected_loss_usd=0.9,
            setup_details={
                "market_cluster": "crypto",
                "symbol_guard_state": "normal",
                "symbol_setup_rolling_trades": 2,
                "symbol_setup_rolling_expectancy_usd": 0.5,
                "symbol_setup_rolling_profit_factor": 1.8,
                "symbol_guard_live_action_unchanged": True,
            },
        )

        shaped = runner._shape_live_trade_plans(
            [plan],
            timestamp="2026-06-22T00:00:00Z",
        )

        self.assertEqual(len(shaped), 1)
        shaped_plan = shaped[0]
        self.assertEqual(shaped_plan.target_notional_usd, 140.0)
        self.assertEqual(shaped_plan.margin_usd, 70.0)
        self.assertTrue(
            bool(shaped_plan.setup_details["dynamic_symbol_guard_recovery_sizing_active"])
        )
        self.assertEqual(
            shaped_plan.setup_details["dynamic_symbol_guard_recovery_multiplier"],
            0.7,
        )
        self.assertEqual(
            shaped_plan.setup_details["dynamic_symbol_guard_recovery_reason"],
            "insufficient_rolling_history",
        )
        self.assertFalse(bool(shaped_plan.setup_details["symbol_guard_live_action_unchanged"]))

    def test_dynamic_symbol_guard_recovery_sizing_keeps_full_size_after_positive_stats(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(
                config.pod_a,
                live_quality_sizing_enabled=False,
                dynamic_symbol_guard_live_sizing_enabled=False,
                dynamic_symbol_guard_recovery_sizing_enabled=True,
            ),
        )
        runner = PodALiveRunner(config, coins=["BTC"])
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="trend_pullback_long",
            confidence=0.80,
            target_notional_usd=200.0,
            stop_bps=45.0,
            time_stop_hours=24,
            margin_usd=100.0,
            risk_budget_usd=2.0,
            expected_loss_usd=0.9,
            setup_details={
                "market_cluster": "crypto",
                "symbol_guard_state": "normal",
                "symbol_setup_rolling_trades": 4,
                "symbol_setup_rolling_expectancy_usd": 0.25,
                "symbol_setup_rolling_profit_factor": 1.2,
                "symbol_guard_live_action_unchanged": True,
            },
        )

        shaped = runner._shape_live_trade_plans(
            [plan],
            timestamp="2026-06-22T00:00:00Z",
        )

        self.assertEqual(len(shaped), 1)
        shaped_plan = shaped[0]
        self.assertEqual(shaped_plan.target_notional_usd, 200.0)
        self.assertFalse(
            bool(shaped_plan.setup_details["dynamic_symbol_guard_recovery_sizing_active"])
        )
        self.assertEqual(
            shaped_plan.setup_details["dynamic_symbol_guard_recovery_multiplier"],
            1.0,
        )
        self.assertEqual(
            shaped_plan.setup_details["dynamic_symbol_guard_recovery_reason"],
            "rolling_pf_expectancy_positive",
        )
        self.assertTrue(bool(shaped_plan.setup_details["symbol_guard_live_action_unchanged"]))

    def test_maintenance_refresh_updates_open_position_market_data_without_new_records(self) -> None:
        config = load_config("config/trident.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            config.hyperliquid.snapshot_output_dir = str(Path(tmpdir) / "snapshots")
            runner = PodALiveRunner(config, coins=["ETH"])
            plan = TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.8,
                target_notional_usd=120.0,
                stop_bps=45.0,
                time_stop_hours=999999,
                take_profit_bps=500.0,
                break_even_trigger_bps=40.0,
                trailing_activation_bps=80.0,
                trailing_distance_bps=30.0,
            )
            opened = runner.executor.portfolio.open_from_plan(
                plan,
                price=3100.0,
                entry_fee_usd=0.1,
                timestamp="2026-04-12T09:00:00Z",
            )
            self.assertTrue(opened)
            runner._info_client = _FakeInfoClient({"ETH": 3150.0})  # type: ignore[assignment]
            runner._last_record_monotonic = 0.0

            refreshed = runner._refresh_open_positions_without_stream(
                journal=None,
                now=runner.MARKET_DATA_FALLBACK_IDLE_SECONDS + 1.0,
            )

            self.assertTrue(refreshed)
            open_positions = runner._build_open_positions_payload()
            self.assertEqual(len(open_positions), 1)
            self.assertEqual(open_positions[0]["current_price"], 3150.0)
            self.assertGreater(open_positions[0]["unrealized_pnl_usd"], 0.0)
            self.assertEqual(open_positions[0]["break_even_trigger_bps"], 40.0)
            self.assertEqual(open_positions[0]["trailing_activation_bps"], 80.0)
            self.assertEqual(open_positions[0]["trailing_distance_bps"], 30.0)

            written_files = list((Path(tmpdir) / "snapshots").glob("*.jsonl"))
            self.assertEqual(len(written_files), 1)
            snapshot_payload = json.loads(written_files[0].read_text(encoding="utf-8").strip())
            self.assertEqual(snapshot_payload["stream_source"], "pod_a_live")
            self.assertEqual(snapshot_payload["capture_reason"], "maintenance_refresh")
            self.assertEqual(snapshot_payload["symbols"][0]["symbol"], "ETH")
            self.assertEqual(snapshot_payload["symbols"][0]["source"], "rest_fallback")

            replay_runner = PodALiveRunner(config, coins=["ETH"])
            replay_opened = replay_runner.executor.portfolio.open_from_plan(
                plan,
                price=3100.0,
                entry_fee_usd=0.1,
                timestamp="2026-04-12T09:00:00Z",
            )
            self.assertTrue(replay_opened)
            replay_runner._process_record(snapshot_payload, journal=None)
            self.assertEqual(replay_runner.report.records_processed, 0)
            replay_positions = replay_runner._build_open_positions_payload()
            self.assertEqual(replay_positions[0]["current_price"], 3150.0)

    def test_open_position_payload_prefers_exchange_position_values(self) -> None:
        config = load_config("config/trident.toml")
        runner = PodALiveRunner(config, coins=["ETH"])
        plan = TradePlan(
            symbol="ETH",
            side="long",
            setup="trend_pullback_long",
            confidence=0.8,
            target_notional_usd=200.0,
            stop_bps=45.0,
            time_stop_hours=24,
        )
        self.assertTrue(
            runner.executor.portfolio.open_from_plan(
                plan,
                price=100.0,
                entry_fee_usd=0.1,
                timestamp="2026-05-19T00:00:00Z",
            )
        )
        account_state = parse_account_state(
            account_address="0x0000000000000000000000000000000000000000",
            user_state={
                "marginSummary": {"accountValue": "1000", "totalMarginUsed": "22"},
                "assetPositions": [
                    {
                        "position": {
                            "coin": "ETH",
                            "szi": "2",
                            "entryPx": "100",
                            "positionValue": "220",
                            "marginUsed": "22",
                            "unrealizedPnl": "20",
                            "leverage": {"type": "cross", "value": 10},
                        }
                    }
                ],
            },
            spot_state={"balances": []},
            open_orders=[],
            frontend_open_orders=[],
            recent_fills=[],
        )
        runner._latest_exchange_positions_by_symbol = dict(account_state.positions)

        open_positions = runner._build_open_positions_payload()

        self.assertEqual(open_positions[0]["current_price"], 110.0)
        self.assertEqual(open_positions[0]["current_notional_usd"], 220.0)
        self.assertEqual(open_positions[0]["unrealized_pnl_usd"], 20.0)
        self.assertEqual(open_positions[0]["margin_usd"], 22.0)
        self.assertEqual(open_positions[0]["effective_leverage"], 10.0)
        self.assertFalse(open_positions[0]["isolated"])

    def test_live_sync_ignores_exchange_fills_before_local_open(self) -> None:
        config = load_config("config/trident.toml")
        runner = PodALiveRunner(config, coins=["ETH"])
        runner.mode = "live"
        with tempfile.TemporaryDirectory() as tmpdir:
            runner.live_state_store = LiveStateStore(Path(tmpdir) / "live_state.json")
            plan = TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.8,
                target_notional_usd=100.0,
                stop_bps=45.0,
                time_stop_hours=24,
            )
            self.assertTrue(
                runner.executor.portfolio.open_from_plan(
                    plan,
                    price=2132.51,
                    entry_fee_usd=0.0,
                    timestamp="2026-05-19T13:37:00Z",
                )
            )
            runner._live_private_client = _FakePrivateClient(  # type: ignore[assignment]
                _account_state_with_recent_fills(
                    [
                        {
                            "coin": "ETH",
                            "oid": 1,
                            "side": "A",
                            "dir": "Close Long",
                            "sz": "0.0409",
                            "px": "2123.1",
                            "closedPnl": "-0.38",
                            "fee": "0.01",
                            "time": _timestamp_ms("2026-05-19T13:06:09Z"),
                        }
                    ]
                )
            )

            changed = runner._sync_live_exchange_state(journal=None)

            self.assertFalse(changed)
            self.assertIn("ETH", runner.executor.portfolio.open_positions)
            self.assertEqual(runner.executor.portfolio.closed_trades, [])

    def test_live_sync_uses_post_open_close_fill_for_exchange_closed_position(self) -> None:
        config = load_config("config/trident.toml")
        runner = PodALiveRunner(config, coins=["ETH"])
        runner.mode = "live"
        with tempfile.TemporaryDirectory() as tmpdir:
            runner.live_state_store = LiveStateStore(Path(tmpdir) / "live_state.json")
            plan = TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.8,
                target_notional_usd=100.0,
                stop_bps=45.0,
                time_stop_hours=24,
            )
            self.assertTrue(
                runner.executor.portfolio.open_from_plan(
                    plan,
                    price=2132.51,
                    entry_fee_usd=0.0,
                    timestamp="2026-05-19T13:37:00Z",
                )
            )
            runner._live_private_client = _FakePrivateClient(  # type: ignore[assignment]
                _account_state_with_recent_fills(
                    [
                        {
                            "coin": "ETH",
                            "oid": 1,
                            "side": "A",
                            "dir": "Close Long",
                            "sz": "0.0409",
                            "px": "2123.1",
                            "closedPnl": "-0.38",
                            "fee": "0.01",
                            "time": _timestamp_ms("2026-05-19T13:06:09Z"),
                        },
                        {
                            "coin": "ETH",
                            "oid": 2,
                            "side": "A",
                            "dir": "Close Long",
                            "sz": "0.0409",
                            "px": "2120.0",
                            "closedPnl": "-0.58",
                            "fee": "0.01",
                            "time": _timestamp_ms("2026-05-19T13:45:00Z"),
                        },
                    ],
                    recent_funding=[
                        {
                            "time": _timestamp_ms("2026-05-19T13:40:00Z"),
                            "hash": "0xfunding",
                            "delta": {
                                "type": "funding",
                                "coin": "ETH",
                                "usdc": "-0.03",
                                "szi": "0.0409",
                                "fundingRate": "0.0001",
                            },
                        }
                    ],
                )
            )

            journal_path = Path(tmpdir) / "pod_a_live.jsonl"
            changed = runner._sync_live_exchange_state(journal=JsonlJournal(journal_path))

            self.assertTrue(changed)
            self.assertNotIn("ETH", runner.executor.portfolio.open_positions)
            trade = runner.executor.portfolio.closed_trades[-1]
            self.assertEqual(trade.close_reason, "exchange_closed")
            self.assertEqual(trade.closed_at, datetime.fromisoformat("2026-05-19T13:45:00+00:00"))
            self.assertGreaterEqual(trade.closed_at, trade.opened_at)
            records = [
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["event_type"], "trade_close")
            journal_trade = records[0]["trade"]
            self.assertEqual(journal_trade["close_fill_count"], 1)
            self.assertEqual(journal_trade["exchange_close_fill_count"], 1)
            self.assertEqual(journal_trade["exchange_fee_usd"], 0.01)
            self.assertEqual(journal_trade["exchange_closed_pnl_usd"], -0.58)
            self.assertEqual(journal_trade["funding_usd"], -0.03)
            self.assertEqual(journal_trade["funding_source"], "exchange_user_funding_history")
            self.assertEqual(journal_trade["funding_payment_count"], 1)
            self.assertEqual(journal_trade["close_fills"][0]["oid"], 2)
            self.assertEqual(journal_trade["close_fills"][0]["fee_source"], "exchange_user_fills")

    def test_live_sync_labels_exchange_closed_stop_loss_from_protective_oid(self) -> None:
        config = load_config("config/trident.toml")
        runner = PodALiveRunner(config, coins=["ETH"])
        runner.mode = "live"
        with tempfile.TemporaryDirectory() as tmpdir:
            runner.live_state_store = LiveStateStore(Path(tmpdir) / "live_state.json")
            runner.live_state_store.save(
                {
                    "positions": {},
                    "orders": {"ETH": {"protective_oids": {"sl": 2, "tp": 3}}},
                    "events": [],
                }
            )
            plan = TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.8,
                target_notional_usd=100.0,
                stop_bps=45.0,
                time_stop_hours=24,
            )
            self.assertTrue(
                runner.executor.portfolio.open_from_plan(
                    plan,
                    price=2132.51,
                    entry_fee_usd=0.0,
                    timestamp="2026-05-19T13:37:00Z",
                )
            )
            runner._live_private_client = _FakePrivateClient(  # type: ignore[assignment]
                _account_state_with_recent_fills(
                    [
                        {
                            "coin": "ETH",
                            "oid": 2,
                            "side": "A",
                            "dir": "Close Long",
                            "sz": "0.0409",
                            "px": "2120.0",
                            "closedPnl": "-0.58",
                            "fee": "0.01",
                            "time": _timestamp_ms("2026-05-19T13:45:00Z"),
                        },
                    ]
                )
            )

            changed = runner._sync_live_exchange_state(journal=None)

            self.assertTrue(changed)
            trade = runner.executor.portfolio.closed_trades[-1]
            self.assertEqual(trade.close_reason, "exchange_closed_stop_loss")

    def test_live_runner_can_write_to_custom_status_path_for_specialized_shadow(self) -> None:
        config = load_config("config/trident.toml")
        runner = PodALiveRunner(
            config,
            coins=["BTC"],
            runtime_name="special_symbols",
            status_path="logs/special_symbols_live_status.json",
            supervisor_profile="trident-live-special-symbols-test",
            signal_source="special_symbols_live_signal",
            filtered_source="special_symbols_live_filtered",
            trade_source="special_symbols_live_trade",
            review_label="Special Symbols",
        )
        records = [
            {
                "timestamp": "2026-04-05T09:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 32.0,
                    "atr_ratio": 1.2,
                    "range_width_bps": 180.0,
                    "structure_score": 0.55,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 68000.0,
                        "ema_fast": 67950.0,
                        "ema_slow": 67800.0,
                        "vwap_distance_bps": -5.0,
                        "structure_score": 0.55,
                        "funding_rate": 0.0,
                        "spread_bps": 0.8,
                        "btc_aligned": True,
                        "book_imbalance": 0.1,
                        "trade_flow_bias": 0.3,
                        "bucket_volume": 2.0,
                        "bucket_trade_count": 10,
                        "bucket_range_bps": 40.0,
                        "source": "test_live",
                    },
                ],
            }
        ]
        runner.collector = _FakeCollector(records)  # type: ignore[assignment]

        async def fake_iter_live_records(**_: object):
            for record in records:
                yield record

        runner._iter_live_records = fake_iter_live_records  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = Path(tmpdir) / "special_symbols_live.jsonl"
            result = asyncio.run(
                runner.run(
                    max_runtime_seconds=0.1,
                    journal_path=journal_path,
                )
            )

        self.assertEqual(result["records_processed"], 1)
        runtime_status = json.loads(
            Path("logs/special_symbols_live_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(runtime_status["pod"], "special_symbols")
        self.assertEqual(runtime_status["collector"]["coins"], ["BTC", "ETH"])

if __name__ == "__main__":
    unittest.main()
