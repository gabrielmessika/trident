import asyncio
import tempfile
import unittest
from pathlib import Path

from app.live.tradfi_snapshot_collector import TradfiSnapshotCollectionRunner
from app.settings import load_config


class _FakeCollector:
    def __init__(self, config, coins: list[str]) -> None:
        self.config = config
        self.coins = coins
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
    ):
        self.calls.append(
            {
                "max_runtime_seconds": max_runtime_seconds,
                "max_messages": max_messages,
            }
        )
        await asyncio.sleep(0.02)
        return type(
            "Stats",
            (),
            {
                "messages_processed": 9,
                "snapshots_written": 2,
                "reconnect_count": 0,
                "heartbeat_count": 1,
                "pong_count": 0,
                "timeout_count": 0,
                "api_error_count": 0,
                "rate_limit_error_count": 0,
                "last_error": None,
            },
        )()


class _FakeFundingCollector:
    def __init__(self, config) -> None:
        self.config = config
        self.calls: list[dict[str, object]] = []

    def collect_once(
        self,
        *,
        output_path: str | Path | None = None,
        symbols: list[str] | None = None,
        include_delisted: bool = False,
        timestamp: str | None = None,
    ) -> list[dict[str, object]]:
        self.calls.append(
            {
                "output_path": str(output_path) if output_path is not None else None,
                "symbols": list(symbols or []),
                "include_delisted": include_delisted,
                "timestamp": timestamp,
            }
        )
        return [{"symbol": symbol, "funding_rate": 0.0} for symbol in (symbols or [])]


class TradfiSnapshotCollectorTests(unittest.TestCase):
    def test_runner_collects_tradfi_snapshots_and_parallel_funding_history(self) -> None:
        config = load_config("config/trident.toml")
        created: dict[str, object] = {}

        def collector_factory(runtime_config, symbols: list[str]) -> _FakeCollector:
            collector = _FakeCollector(runtime_config, symbols)
            created["collector"] = collector
            return collector

        def funding_collector_factory(runtime_config) -> _FakeFundingCollector:
            collector = _FakeFundingCollector(runtime_config)
            created["funding"] = collector
            return collector

        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_output_dir = Path(tmpdir) / "snapshots"
            funding_output_path = Path(tmpdir) / "funding.jsonl"
            result = asyncio.run(
                TradfiSnapshotCollectionRunner(
                    config,
                    collector_factory=collector_factory,
                    funding_collector_factory=funding_collector_factory,
                ).run(
                    symbols=["spx", "gold"],
                    snapshot_output_dir=snapshot_output_dir,
                    max_runtime_seconds=30.0,
                    max_messages=12,
                    funding_output_path=funding_output_path,
                    funding_poll_seconds=0.001,
                )
            )

            collector = created["collector"]
            funding = created["funding"]
            self.assertEqual(result.symbols, ["SPX", "GOLD"])
            self.assertEqual(result.snapshot_output_dir, str(snapshot_output_dir))
            self.assertEqual(result.snapshot_records_written, 2)
            self.assertEqual(result.collector["messages_processed"], 9)
            self.assertGreaterEqual(result.funding_polls_completed, 1)
            self.assertGreaterEqual(result.funding_records_written, 2)
            self.assertEqual(collector.coins, ["SPX", "GOLD"])
            self.assertEqual(collector.config.hyperliquid.snapshot_output_dir, str(snapshot_output_dir))
            self.assertEqual(collector.calls[0]["max_messages"], 12)
            self.assertEqual(funding.calls[0]["output_path"], str(funding_output_path))
            self.assertEqual(funding.calls[0]["symbols"], ["SPX", "GOLD"])


if __name__ == "__main__":
    unittest.main()
