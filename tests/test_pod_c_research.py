import json
import tempfile
import unittest
from pathlib import Path

from app.research.pod_c_leadlag import LeadLagResearchRunner


def _record(timestamp: str, btc: float, sol: float, hype: float) -> dict[str, object]:
    def row(symbol: str, price: float) -> dict[str, object]:
        return {
            "symbol": symbol,
            "price": price,
            "ema_fast": price,
            "ema_slow": price,
            "vwap_distance_bps": 0.0,
            "structure_score": 0.0,
            "funding_rate": 0.0,
            "spread_bps": 1.0,
            "btc_aligned": True,
            "book_imbalance": 0.0,
            "trade_flow_bias": 0.0,
            "bucket_volume": 1.0,
            "bucket_trade_count": 1,
            "bucket_range_bps": 1.0,
            "source": "test",
        }

    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 20.0,
            "atr_ratio": 1.0,
            "range_width_bps": 80.0,
            "structure_score": 0.2,
            "btc_impulse": True,
        },
        "symbols": [
            row("BTC", btc),
            row("SOL", sol),
            row("HYPE", hype),
        ],
    }


class PodCResearchTests(unittest.TestCase):
    def test_leadlag_runner_outputs_go_when_follower_expectancy_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            json_path = Path(tmpdir) / "study.json"
            md_path = Path(tmpdir) / "memo.md"
            input_path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        _record("2026-04-05T10:00:00Z", 100.0, 50.0, 20.0),
                        _record("2026-04-05T10:01:00Z", 100.2, 50.0, 20.0),
                        _record("2026-04-05T10:02:00Z", 100.25, 50.2, 20.0),
                        _record("2026-04-05T10:03:00Z", 100.45, 50.2, 20.0),
                        _record("2026-04-05T10:04:00Z", 100.5, 50.4, 20.0),
                        _record("2026-04-05T10:05:00Z", 100.7, 50.4, 20.0),
                        _record("2026-04-05T10:06:00Z", 100.75, 50.6, 20.0),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = LeadLagResearchRunner().run(
                input_path=input_path,
                leader_symbol="BTC",
                follower_symbols=["SOL", "HYPE"],
                impulse_threshold_bps=8.0,
                horizon_bars=1,
                output_json=json_path,
                output_md=md_path,
            )

            self.assertEqual(result.best_symbol, "SOL")
            self.assertEqual(result.recommendation, "go")
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            self.assertIn("Recommendation: go", md_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
