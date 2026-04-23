import json
import tempfile
import unittest
from pathlib import Path

from app.research.pod_liq_features import PodLiqFeatureBuilder


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
            "adx": 18.0,
            "atr_ratio": 0.8,
            "range_width_bps": 60.0,
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
                "bucket_range_bps": 12.0,
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
                "realized_vol_short_bps": 2.0,
                "realized_vol_long_bps": 1.0,
                "compression_score": 0.3,
                "source": "test",
            }
        ],
    }


class PodLiqFeatureBuilderTests(unittest.TestCase):
    def test_builder_skips_rows_across_large_timestamp_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            records = [
                _rich_record(
                    "2026-04-13T10:00:00Z",
                    price=100.0,
                    spread_bps=1.2,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=20.0,
                    bucket_trade_count=2,
                    bid_depth_10bps=100.0,
                    ask_depth_10bps=100.0,
                    best_bid_size=50.0,
                    best_ask_size=50.0,
                    microprice_dislocation_bps=0.0,
                ),
                _rich_record(
                    "2026-04-14T10:00:00Z",
                    price=100.4,
                    spread_bps=1.1,
                    book_imbalance=0.25,
                    trade_flow_bias=0.20,
                    bucket_volume=60.0,
                    bucket_trade_count=8,
                    bid_depth_10bps=140.0,
                    ask_depth_10bps=80.0,
                    best_bid_size=70.0,
                    best_ask_size=35.0,
                    microprice_dislocation_bps=0.9,
                ),
            ]
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            rows = PodLiqFeatureBuilder().build_rows(input_path=input_path)

            self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
