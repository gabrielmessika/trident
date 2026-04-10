import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.live.pod_a_live_runner import PodALiveRunner
from app.settings import load_config


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


class PodALiveRunnerTests(unittest.TestCase):
    def test_live_runner_processes_stream_records(self) -> None:
        config = load_config("config/trident.toml")
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


if __name__ == "__main__":
    unittest.main()
