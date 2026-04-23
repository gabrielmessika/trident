import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.research.memecoin_concept_research import MemecoinConceptResearchRunner


def _symbol_snapshot(
    *,
    symbol: str,
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
        "symbol": symbol,
        "price": price,
        "ema_fast": price,
        "ema_slow": price,
        "vwap_distance_bps": 0.0,
        "structure_score": 0.15 if symbol == "PUMP" else 0.05,
        "funding_rate": 0.0,
        "spread_bps": spread_bps,
        "btc_aligned": True,
        "book_imbalance": book_imbalance,
        "trade_flow_bias": trade_flow_bias,
        "bucket_volume": bucket_volume,
        "bucket_trade_count": bucket_trade_count,
        "bucket_range_bps": 18.0,
        "bid_depth_10bps": bid_depth_10bps,
        "ask_depth_10bps": ask_depth_10bps,
        "microprice_dislocation_bps": microprice_dislocation_bps,
        "source": "test",
    }


def _record(timestamp: str, pump_price: float, mode: str) -> dict[str, object]:
    if mode == "event":
        pump = _symbol_snapshot(
            symbol="PUMP",
            price=pump_price,
            spread_bps=1.3,
            book_imbalance=0.55,
            trade_flow_bias=0.68,
            bucket_volume=180.0,
            bucket_trade_count=22,
            bid_depth_10bps=155.0,
            ask_depth_10bps=52.0,
            microprice_dislocation_bps=1.35,
        )
    elif mode == "follow":
        pump = _symbol_snapshot(
            symbol="PUMP",
            price=pump_price,
            spread_bps=1.1,
            book_imbalance=0.32,
            trade_flow_bias=0.36,
            bucket_volume=95.0,
            bucket_trade_count=10,
            bid_depth_10bps=142.0,
            ask_depth_10bps=88.0,
            microprice_dislocation_bps=0.65,
        )
    else:
        pump = _symbol_snapshot(
            symbol="PUMP",
            price=pump_price,
            spread_bps=1.0,
            book_imbalance=0.04,
            trade_flow_bias=0.03,
            bucket_volume=18.0,
            bucket_trade_count=3,
            bid_depth_10bps=100.0,
            ask_depth_10bps=98.0,
            microprice_dislocation_bps=0.05,
        )
    btc = _symbol_snapshot(
        symbol="BTC",
        price=100.0,
        spread_bps=1.1,
        book_imbalance=0.02,
        trade_flow_bias=0.02,
        bucket_volume=25.0,
        bucket_trade_count=4,
        bid_depth_10bps=120.0,
        ask_depth_10bps=118.0,
        microprice_dislocation_bps=0.05,
    )
    eth = _symbol_snapshot(
        symbol="ETH",
        price=50.0,
        spread_bps=1.1,
        book_imbalance=0.01,
        trade_flow_bias=0.01,
        bucket_volume=22.0,
        bucket_trade_count=4,
        bid_depth_10bps=116.0,
        ask_depth_10bps=114.0,
        microprice_dislocation_bps=0.04,
    )
    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 28.0,
            "atr_ratio": 1.3,
            "range_width_bps": 140.0,
            "structure_score": 0.45,
            "btc_impulse": True,
        },
        "symbols": [pump, btc, eth],
    }


class MemecoinConceptResearchTests(unittest.TestCase):
    def test_runner_outputs_go_for_repeatable_ranked_event_momentum(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            output_json = Path(tmpdir) / "memecoin.json"
            output_md = Path(tmpdir) / "memecoin.md"
            records = []
            current = datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc)
            price = 10.0
            for _ in range(30):
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price,
                        "baseline",
                    )
                )
                current += timedelta(minutes=1)
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price + 0.30,
                        "event",
                    )
                )
                current += timedelta(minutes=1)
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price + 0.60,
                        "follow",
                    )
                )
                current += timedelta(minutes=1)
                price += 0.70
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            result = MemecoinConceptResearchRunner().run(
                input_path=input_path,
                horizon_bars=1,
                output_json=output_json,
                output_md=output_md,
            )

            self.assertEqual(result.recommendation, "go")
            self.assertIsNotNone(result.best_variant)
            self.assertTrue(output_json.exists())
            self.assertTrue(output_md.exists())
            markdown = output_md.read_text(encoding="utf-8")
            self.assertIn("- Recommendation: `go`", markdown)
            self.assertIn("event_momentum_top_5", markdown)

    def test_runner_can_use_slower_snapshot_cadence_with_gap_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "slow_snapshots.jsonl"
            records = []
            current = datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc)
            price = 10.0
            for _ in range(12):
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price,
                        "baseline",
                    )
                )
                current += timedelta(minutes=15)
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price + 0.30,
                        "event",
                    )
                )
                current += timedelta(minutes=15)
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price + 0.60,
                        "follow",
                    )
                )
                current += timedelta(minutes=15)
                price += 0.70
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            result = MemecoinConceptResearchRunner().run(
                input_path=input_path,
                horizon_bars=1,
                max_bar_gap_seconds=1200,
            )

            self.assertGreater(result.universe_slices[0]["sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
