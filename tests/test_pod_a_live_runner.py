import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.live.pod_a_live_runner import PodALiveRunner
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

    def test_maintenance_refresh_updates_open_position_market_data_without_new_records(self) -> None:
        config = load_config("config/trident.toml")
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
