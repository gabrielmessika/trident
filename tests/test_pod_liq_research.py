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
    bid_depth_10bps: float,
    ask_depth_10bps: float,
    microprice_dislocation_bps: float,
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
                "bid_depth_10bps": bid_depth_10bps,
                "ask_depth_10bps": ask_depth_10bps,
                "microprice_dislocation_bps": microprice_dislocation_bps,
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
            records = []
            price = 50.0
            minute = 0
            for _ in range(8):
                records.append(
                    _liq_record(
                        f"2026-04-05T10:{minute:02d}:00Z",
                        price,
                        1.0,
                        0.03,
                        0.03,
                        20.0,
                        3,
                        100.0,
                        100.0,
                        0.05,
                    )
                )
                minute += 1
                records.append(
                    _liq_record(
                        f"2026-04-05T10:{minute:02d}:00Z",
                        price + 0.05,
                        2.2,
                        0.55,
                        0.68,
                        145.0,
                        14,
                        118.0,
                        44.0,
                        1.15,
                    )
                )
                minute += 1
                price += 0.22
                records.append(
                    _liq_record(
                        f"2026-04-05T10:{minute:02d}:00Z",
                        price,
                        1.4,
                        0.18,
                        0.22,
                        55.0,
                        5,
                        112.0,
                        72.0,
                        0.35,
                    )
                )
                minute += 1
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
            markdown = md_path.read_text(encoding="utf-8")
            self.assertIn("- Recommendation: `go`", markdown)
            self.assertIn("liquidity_pull_continuation", markdown)


if __name__ == "__main__":
    unittest.main()
