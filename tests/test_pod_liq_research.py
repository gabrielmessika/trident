import json
import tempfile
import unittest
from pathlib import Path

from app.research.pod_liq_research import PodLiqResearchRunner


def _liq_record(
    timestamp: str,
    price: float,
    spread_bps: float,
    book_imbalance: float,
    trade_flow_bias: float,
    bucket_volume: float,
    bucket_trade_count: int,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 18.0,
            "atr_ratio": 0.8,
            "range_width_bps": 70.0,
            "structure_score": 0.1,
            "btc_impulse": False,
        },
        "symbols": [
            {
                "symbol": "SOL",
                "price": price,
                "ema_fast": price,
                "ema_slow": price,
                "vwap_distance_bps": 0.0,
                "structure_score": 0.0,
                "funding_rate": 0.0,
                "spread_bps": spread_bps,
                "btc_aligned": True,
                "book_imbalance": book_imbalance,
                "trade_flow_bias": trade_flow_bias,
                "bucket_volume": bucket_volume,
                "bucket_trade_count": bucket_trade_count,
                "bucket_range_bps": 15.0,
                "source": "test",
            }
        ],
    }


class PodLiqResearchTests(unittest.TestCase):
    def test_liq_research_outputs_go_for_repeatable_burst_direction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            json_path = Path(tmpdir) / "liq.json"
            md_path = Path(tmpdir) / "liq.md"
            records = [
                _liq_record("2026-04-05T10:00:00Z", 50.0, 1.0, 0.05, 0.05, 20.0, 3),
                _liq_record("2026-04-05T10:01:00Z", 50.2, 2.0, 0.60, 0.70, 120.0, 12),
                _liq_record("2026-04-05T10:02:00Z", 50.5, 1.5, 0.20, 0.30, 60.0, 5),
                _liq_record("2026-04-05T10:03:00Z", 50.7, 2.2, 0.65, 0.75, 140.0, 13),
                _liq_record("2026-04-05T10:04:00Z", 51.0, 1.4, 0.20, 0.25, 55.0, 5),
                _liq_record("2026-04-05T10:05:00Z", 51.2, 2.3, 0.70, 0.80, 150.0, 14),
                _liq_record("2026-04-05T10:06:00Z", 51.5, 1.3, 0.25, 0.30, 50.0, 4),
            ]
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            result = PodLiqResearchRunner().run(
                input_path=input_path,
                horizon_bars=1,
                output_json=json_path,
                output_md=md_path,
            )

            self.assertEqual(result.recommendation, "go")
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            self.assertIn("Recommendation: go", md_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
