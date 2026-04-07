import json
import tempfile
import unittest
from pathlib import Path

from app.research.pod_funding_research import FundingResearchRunner


def _funding_record(timestamp: str, btc: float, sol: float, funding_btc: float, funding_sol: float) -> dict[str, object]:
    def row(symbol: str, price: float, funding_rate: float) -> dict[str, object]:
        return {
            "symbol": symbol,
            "price": price,
            "ema_fast": price,
            "ema_slow": price,
            "vwap_distance_bps": 0.0,
            "structure_score": 0.0,
            "funding_rate": funding_rate,
            "spread_bps": 1.0,
            "btc_aligned": True,
            "book_imbalance": 0.0,
            "trade_flow_bias": 0.0,
            "bucket_volume": 10.0,
            "bucket_trade_count": 4,
            "bucket_range_bps": 3.0,
            "source": "test",
        }

    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 12.0,
            "atr_ratio": 0.4,
            "range_width_bps": 40.0,
            "structure_score": 0.05,
            "btc_impulse": False,
        },
        "symbols": [
            row("BTC", btc, funding_btc),
            row("SOL", sol, funding_sol),
        ],
    }


class PodFundingResearchTests(unittest.TestCase):
    def test_funding_research_outputs_go_for_positive_mean_reversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            json_path = Path(tmpdir) / "funding.json"
            md_path = Path(tmpdir) / "funding.md"
            records = [
                _funding_record("2026-04-05T10:00:00Z", 100.0, 50.0, 0.0006, -0.0006),
                _funding_record("2026-04-05T10:01:00Z", 99.8, 50.2, 0.0006, -0.0006),
                _funding_record("2026-04-05T10:02:00Z", 99.6, 50.4, 0.0006, -0.0006),
                _funding_record("2026-04-05T10:03:00Z", 99.4, 50.6, 0.0006, -0.0006),
                _funding_record("2026-04-05T10:04:00Z", 99.2, 50.8, 0.0006, -0.0006),
                _funding_record("2026-04-05T10:05:00Z", 99.0, 51.0, 0.0006, -0.0006),
            ]
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            result = FundingResearchRunner().run(
                input_path=input_path,
                funding_threshold_bps=4.0,
                horizon_bars=1,
                output_json=json_path,
                output_md=md_path,
            )

            self.assertEqual(result.recommendation, "go")
            self.assertEqual(result.best_variant, "pure_mean_reversion")
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            self.assertIn("Recommendation: go", md_path.read_text(encoding="utf-8"))

    def test_funding_research_can_use_funding_history_when_snapshots_have_zero_funding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            funding_history_path = Path(tmpdir) / "funding_history.jsonl"
            records = [
                _funding_record("2026-04-05T10:00:00Z", 100.0, 50.0, 0.0, 0.0),
                _funding_record("2026-04-05T10:01:00Z", 99.8, 50.2, 0.0, 0.0),
                _funding_record("2026-04-05T10:02:00Z", 99.6, 50.4, 0.0, 0.0),
                _funding_record("2026-04-05T10:03:00Z", 99.4, 50.6, 0.0, 0.0),
                _funding_record("2026-04-05T10:04:00Z", 99.2, 50.8, 0.0, 0.0),
                _funding_record("2026-04-05T10:05:00Z", 99.0, 51.0, 0.0, 0.0),
            ]
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            funding_history = [
                {"timestamp": "2026-04-05T10:00:00Z", "symbol": "BTC", "funding_rate": 0.0006, "open_interest": 10},
                {"timestamp": "2026-04-05T10:00:00Z", "symbol": "SOL", "funding_rate": -0.0006, "open_interest": 20},
                {"timestamp": "2026-04-05T10:01:00Z", "symbol": "BTC", "funding_rate": 0.0006, "open_interest": 10},
                {"timestamp": "2026-04-05T10:01:00Z", "symbol": "SOL", "funding_rate": -0.0006, "open_interest": 20},
                {"timestamp": "2026-04-05T10:02:00Z", "symbol": "BTC", "funding_rate": 0.0006, "open_interest": 10},
                {"timestamp": "2026-04-05T10:02:00Z", "symbol": "SOL", "funding_rate": -0.0006, "open_interest": 20},
                {"timestamp": "2026-04-05T10:03:00Z", "symbol": "BTC", "funding_rate": 0.0006, "open_interest": 10},
                {"timestamp": "2026-04-05T10:03:00Z", "symbol": "SOL", "funding_rate": -0.0006, "open_interest": 20},
                {"timestamp": "2026-04-05T10:04:00Z", "symbol": "BTC", "funding_rate": 0.0006, "open_interest": 10},
                {"timestamp": "2026-04-05T10:04:00Z", "symbol": "SOL", "funding_rate": -0.0006, "open_interest": 20},
                {"timestamp": "2026-04-05T10:05:00Z", "symbol": "BTC", "funding_rate": 0.0006, "open_interest": 10},
                {"timestamp": "2026-04-05T10:05:00Z", "symbol": "SOL", "funding_rate": -0.0006, "open_interest": 20},
            ]
            funding_history_path.write_text(
                "\n".join(json.dumps(record) for record in funding_history) + "\n",
                encoding="utf-8",
            )

            result = FundingResearchRunner().run(
                input_path=input_path,
                funding_history_path=funding_history_path,
                funding_threshold_bps=4.0,
                horizon_bars=1,
            )

            self.assertEqual(result.recommendation, "go")
            self.assertEqual(result.best_variant, "pure_mean_reversion")
            self.assertGreater(result.variants[0]["sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
