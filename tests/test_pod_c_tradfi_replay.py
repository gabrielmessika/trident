import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.pod_c_tradfi_replay import PodCTradfiReplayRunner
from app.backtest.pod_c_runner import PodCBacktestResult
from app.settings import load_config


class _FakeEnricher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def enrich(
        self,
        *,
        input_path: str | Path,
        funding_history_path: str | Path,
        output_path: str | Path,
        symbols: list[str] | None = None,
        funding_max_age_seconds: float = 900.0,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "input_path": str(input_path),
                "funding_history_path": str(funding_history_path),
                "output_path": str(output_path),
                "symbols": list(symbols or []),
                "funding_max_age_seconds": funding_max_age_seconds,
            }
        )
        Path(output_path).write_text(Path(input_path).read_text(encoding="utf-8"), encoding="utf-8")
        return {"records_processed": 1, "symbols_enriched": 1}


class _FakeBacktestRunner:
    def __init__(self, config) -> None:
        self.config = config
        self.calls: list[dict[str, object]] = []

    def run_jsonl(
        self,
        input_path: str | Path,
        output_path: str | Path | None = None,
    ) -> PodCBacktestResult:
        self.calls.append(
            {
                "input_path": str(input_path),
                "output_path": str(output_path) if output_path is not None else None,
            }
        )
        return PodCBacktestResult(
            backtest={
                "records_processed": 1,
                "signal_count": 0,
                "accepted_count": 0,
                "closed_trade_count": 0,
                "realized_pnl_usd": 0.0,
            },
            output_path=str(output_path) if output_path is not None else None,
        )


class PodCTradfiReplayTests(unittest.TestCase):
    def test_runner_enriches_input_before_replay_and_writes_report(self) -> None:
        created: dict[str, object] = {}

        def enricher_factory() -> _FakeEnricher:
            enricher = _FakeEnricher()
            created["enricher"] = enricher
            return enricher

        def backtest_runner_factory(config) -> _FakeBacktestRunner:
            runner = _FakeBacktestRunner(config)
            created["backtest"] = runner
            return runner

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            funding_history_path = Path(tmpdir) / "funding.jsonl"
            output_path = Path(tmpdir) / "journal.jsonl"
            report_path = Path(tmpdir) / "report.json"
            input_path.write_text('{"timestamp":"2026-04-10T12:00:00Z","regime_snapshot":{"ready":true,"adx":20.0,"atr_ratio":0.8,"range_width_bps":100.0,"structure_score":0.2,"btc_impulse":false},"symbols":[]}\n', encoding="utf-8")
            funding_history_path.write_text('{"timestamp":"2026-04-10T12:00:00Z","symbol":"SPX","funding_rate":0.0}\n', encoding="utf-8")

            result = PodCTradfiReplayRunner(
                config_loader=load_config,
                backtest_runner_factory=backtest_runner_factory,
                enricher_factory=enricher_factory,
            ).run(
                config_path="config/trident.toml",
                input_path=input_path,
                output_path=output_path,
                report_output=report_path,
                funding_history_path=funding_history_path,
                symbols=["spx"],
                funding_max_age_seconds=120.0,
            )

            enricher = created["enricher"]
            backtest = created["backtest"]
            self.assertEqual(enricher.calls[0]["symbols"], ["SPX"])
            self.assertEqual(enricher.calls[0]["funding_max_age_seconds"], 120.0)
            self.assertNotEqual(backtest.calls[0]["input_path"], str(input_path))
            self.assertEqual(backtest.calls[0]["output_path"], str(output_path))
            self.assertEqual(result.symbols, ["SPX"])
            self.assertIsNotNone(result.enriched_input_path)
            self.assertTrue(Path(result.enriched_input_path).exists())
            self.assertTrue(report_path.exists())
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report_payload["backtest"]["records_processed"], 1)
            self.assertEqual(report_payload["symbols"], ["SPX"])


if __name__ == "__main__":
    unittest.main()
