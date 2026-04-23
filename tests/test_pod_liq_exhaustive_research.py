import json
import tempfile
import unittest
from pathlib import Path

from app.research.pod_liq_exhaustive_research import PodLiqExhaustiveResearchRunner


def _rich_record(
    timestamp: str,
    *,
    price: float,
    spread_bps: float,
    book_imbalance: float,
    trade_flow_bias: float,
    bucket_volume: float,
    bucket_trade_count: int,
    bid_depth_10bps: float,
    ask_depth_10bps: float,
    best_bid_size: float,
    best_ask_size: float,
    microprice_dislocation_bps: float,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 24.0,
            "atr_ratio": 1.1,
            "range_width_bps": 70.0,
            "structure_score": 0.3,
            "btc_impulse": True,
        },
        "symbols": [
            {
                "symbol": "SOL",
                "price": price,
                "ema_fast": price,
                "ema_slow": price,
                "vwap_distance_bps": 4.0,
                "structure_score": 0.25,
                "funding_rate": 0.0,
                "spread_bps": spread_bps,
                "btc_aligned": True,
                "book_imbalance": book_imbalance,
                "trade_flow_bias": trade_flow_bias,
                "bucket_volume": bucket_volume,
                "bucket_trade_count": bucket_trade_count,
                "bucket_range_bps": 14.0,
                "best_bid": price - 0.01,
                "best_ask": price + 0.01,
                "best_bid_size": best_bid_size,
                "best_ask_size": best_ask_size,
                "bid_depth_10bps": bid_depth_10bps,
                "ask_depth_10bps": ask_depth_10bps,
                "microprice": price,
                "microprice_dislocation_bps": microprice_dislocation_bps,
                "buy_count": bucket_trade_count,
                "sell_count": 0,
                "buy_volume": bucket_volume,
                "sell_volume": 0.0,
                "bucket_notional_usd": bucket_volume * price,
                "delta_spread_bps": 0.0,
                "delta_book_imbalance": 0.0,
                "delta_trade_flow_bias": 0.0,
                "volume_ratio": 1.0,
                "trade_count_ratio": 1.0,
                "realized_vol_short_bps": 3.5,
                "realized_vol_long_bps": 1.8,
                "compression_score": 0.2,
                "source": "test",
            }
        ],
    }


class PodLiqExhaustiveResearchTests(unittest.TestCase):
    def test_runner_keeps_depth_refill_family_when_holdout_edge_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            json_path = Path(tmpdir) / "research.json"
            md_path = Path(tmpdir) / "research.md"
            records = []
            daily_specs = [
                ("2026-04-13", 100.00, 100.28),
                ("2026-04-14", 100.30, 100.58),
                ("2026-04-15", 100.60, 100.88),
                ("2026-04-16", 100.90, 101.18),
                ("2026-04-20", 101.20, 101.48),
                ("2026-04-21", 101.50, 101.78),
            ]
            for day, start_price, future_price in daily_specs:
                records.append(
                    _rich_record(
                        f"{day}T10:00:00Z",
                        price=start_price,
                        spread_bps=1.4,
                        book_imbalance=0.02,
                        trade_flow_bias=0.03,
                        bucket_volume=20.0,
                        bucket_trade_count=3,
                        bid_depth_10bps=100.0,
                        ask_depth_10bps=100.0,
                        best_bid_size=50.0,
                        best_ask_size=50.0,
                        microprice_dislocation_bps=0.1,
                    )
                )
                records.append(
                    _rich_record(
                        f"{day}T10:01:00Z",
                        price=start_price + 0.05,
                        spread_bps=0.9,
                        book_imbalance=0.45,
                        trade_flow_bias=0.32,
                        bucket_volume=140.0,
                        bucket_trade_count=14,
                        bid_depth_10bps=165.0,
                        ask_depth_10bps=78.0,
                        best_bid_size=92.0,
                        best_ask_size=35.0,
                        microprice_dislocation_bps=1.2,
                    )
                )
                records.append(
                    _rich_record(
                        f"{day}T10:02:00Z",
                        price=future_price,
                        spread_bps=1.0,
                        book_imbalance=0.20,
                        trade_flow_bias=0.12,
                        bucket_volume=40.0,
                        bucket_trade_count=5,
                        bid_depth_10bps=130.0,
                        ask_depth_10bps=90.0,
                        best_bid_size=70.0,
                        best_ask_size=40.0,
                        microprice_dislocation_bps=0.4,
                    )
                )
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            runner = PodLiqExhaustiveResearchRunner()
            runner.HORIZONS = [1]
            runner.MIN_SCORE_VALUES = [0.55]
            runner.MAX_SPREAD_VALUES = [3.0]
            runner.MIN_NOTIONAL_VALUES = [100.0]
            runner.MIN_TRAIN_SAMPLES = 3
            runner.MIN_VALIDATION_SAMPLES = 2
            result = runner.run(
                input_path=input_path,
                start_date="2026-04-13",
                end_date="2026-04-21",
                train_end_date="2026-04-16",
                validation_start_date="2026-04-20",
                output_json=json_path,
                output_md=md_path,
            )

            family_map = {
                item["family"]: item
                for item in result.family_summaries
            }
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            self.assertEqual(
                family_map["depth_refill"]["final_decision"],
                "keep_watch_only",
            )
            markdown = md_path.read_text(encoding="utf-8")
            self.assertIn("depth_refill", markdown)
            self.assertIn("keep_watch_only", markdown)


if __name__ == "__main__":
    unittest.main()
