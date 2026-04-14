import unittest

from app.live.funding_collector import FundingCollectorStats
from app.live.tradfi_funding_collector import TradfiFundingCollectorRunner
from app.settings import load_config


class _FakeFundingCollector:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        *,
        output_path,
        status_path=None,
        poll_seconds: float = 60.0,
        iterations: int | None = None,
        symbols: list[str] | None = None,
        include_delisted: bool = False,
        collector_name: str = "funding_collector",
        collector_label: str = "Funding Collector",
    ) -> FundingCollectorStats:
        self.calls.append(
            {
                "output_path": str(output_path),
                "status_path": str(status_path) if status_path is not None else None,
                "poll_seconds": poll_seconds,
                "iterations": iterations,
                "symbols": list(symbols or []),
                "include_delisted": include_delisted,
                "collector_name": collector_name,
                "collector_label": collector_label,
            }
        )
        return FundingCollectorStats(
            polls_completed=1,
            records_written=len(symbols or []),
            output_path=str(output_path),
            status_path=str(status_path) if status_path is not None else None,
        )


class TradfiFundingCollectorTests(unittest.TestCase):
    def test_runner_defaults_to_observation_universe_filtered_by_pod_c_clusters(self) -> None:
        config = load_config("config/trident.toml")
        config.hyperliquid.observation_universe = ["BTC", "PAXG", "SPY", "QQQ", "DOGE"]
        config.pod_c.allowed_market_clusters = ["gold", "index"]
        fake = _FakeFundingCollector()

        stats = TradfiFundingCollectorRunner(
            config,
            collector=fake,  # type: ignore[arg-type]
        ).run()

        self.assertEqual(stats.records_written, 3)
        self.assertEqual(
            fake.calls[0]["symbols"],
            ["PAXG", "SPY", "QQQ"],
        )
        self.assertEqual(
            fake.calls[0]["output_path"],
            "data/funding_history/pod_c_tradfi.jsonl",
        )
        self.assertEqual(
            fake.calls[0]["status_path"],
            "logs/tradfi_funding_collector_status.json",
        )
        self.assertEqual(
            fake.calls[0]["collector_name"],
            "tradfi_funding_collector",
        )

    def test_runner_keeps_blocked_gold_in_funding_collection(self) -> None:
        config = load_config("config/trident.toml")
        config.hyperliquid.observation_universe = ["XYZ:GOLD", "XYZ:CL", "BTC"]
        config.pod_c.allowed_market_clusters = ["gold", "oil"]
        config.pod_c.blocked_symbols = ["XYZ:GOLD"]
        fake = _FakeFundingCollector()

        TradfiFundingCollectorRunner(
            config,
            collector=fake,  # type: ignore[arg-type]
        ).run()

        self.assertEqual(fake.calls[0]["symbols"], ["XYZ:GOLD", "XYZ:CL"])


if __name__ == "__main__":
    unittest.main()
