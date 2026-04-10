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
        poll_seconds: float = 60.0,
        iterations: int | None = None,
        symbols: list[str] | None = None,
        include_delisted: bool = False,
    ) -> FundingCollectorStats:
        self.calls.append(
            {
                "output_path": str(output_path),
                "poll_seconds": poll_seconds,
                "iterations": iterations,
                "symbols": list(symbols or []),
                "include_delisted": include_delisted,
            }
        )
        return FundingCollectorStats(
            polls_completed=1,
            records_written=len(symbols or []),
            output_path=str(output_path),
        )


class TradfiFundingCollectorTests(unittest.TestCase):
    def test_runner_defaults_to_pod_c_symbols(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.symbols = ["SPX", "PAXG", "XYZ100"]
        fake = _FakeFundingCollector()

        stats = TradfiFundingCollectorRunner(
            config,
            collector=fake,  # type: ignore[arg-type]
        ).run()

        self.assertEqual(stats.records_written, 3)
        self.assertEqual(
            fake.calls[0]["symbols"],
            ["SPX", "PAXG", "XYZ100"],
        )
        self.assertEqual(
            fake.calls[0]["output_path"],
            "data/funding_history/pod_c_tradfi.jsonl",
        )


if __name__ == "__main__":
    unittest.main()
