import json
import tempfile
import unittest
from pathlib import Path

from app.live.asset_ctx_enricher import SnapshotAssetCtxEnricher


def _snapshot_symbol(symbol: str, price: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "price": price,
        "ema_fast": price,
        "ema_slow": price,
        "vwap_distance_bps": 1.5,
        "structure_score": 0.25,
        "funding_rate": 0.0,
        "spread_bps": 1.0,
        "btc_aligned": True,
        "book_imbalance": 0.0,
        "trade_flow_bias": 0.0,
        "bucket_volume": 100.0,
        "bucket_trade_count": 8,
        "bucket_range_bps": 5.0,
        "source": "test",
    }


class SnapshotAssetCtxEnricherTests(unittest.TestCase):
    def test_enricher_aligns_asset_ctx_fields_onto_requested_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            funding_history_path = Path(tmpdir) / "funding.jsonl"
            output_path = Path(tmpdir) / "enriched.jsonl"

            input_record = {
                "timestamp": "2026-04-10T12:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 20.0,
                    "atr_ratio": 0.8,
                    "range_width_bps": 120.0,
                    "structure_score": 0.25,
                    "btc_impulse": False,
                },
                "symbols": [
                    _snapshot_symbol("SPX", 5200.0),
                    _snapshot_symbol("BTC", 70000.0),
                ],
            }
            funding_records = [
                {
                    "timestamp": "2026-04-10T11:59:30Z",
                    "symbol": "SPX",
                    "funding_rate": 0.0002,
                    "open_interest": 42.5,
                    "mark_px": 5201.5,
                    "oracle_px": 5202.0,
                    "premium": 0.0001,
                    "day_ntl_vlm": 1000000.0,
                    "day_base_vlm": 192.5,
                },
                {
                    "timestamp": "2026-04-10T11:59:30Z",
                    "symbol": "BTC",
                    "funding_rate": 0.001,
                    "open_interest": 10.0,
                },
            ]

            input_path.write_text(json.dumps(input_record) + "\n", encoding="utf-8")
            funding_history_path.write_text(
                "\n".join(json.dumps(record) for record in funding_records) + "\n",
                encoding="utf-8",
            )

            result = SnapshotAssetCtxEnricher().enrich(
                input_path=input_path,
                funding_history_path=funding_history_path,
                output_path=output_path,
                symbols=["SPX"],
                funding_max_age_seconds=120.0,
            )

            self.assertEqual(result["records_processed"], 1)
            self.assertEqual(result["symbols_enriched"], 1)
            enriched_payload = json.loads(output_path.read_text(encoding="utf-8").strip())
            spx = next(item for item in enriched_payload["symbols"] if item["symbol"] == "SPX")
            btc = next(item for item in enriched_payload["symbols"] if item["symbol"] == "BTC")

            self.assertEqual(spx["funding_rate"], 0.0002)
            self.assertEqual(spx["open_interest"], 42.5)
            self.assertEqual(spx["mark_px"], 5201.5)
            self.assertEqual(spx["oracle_px"], 5202.0)
            self.assertEqual(spx["premium"], 0.0001)
            self.assertEqual(spx["day_ntl_vlm"], 1000000.0)
            self.assertEqual(spx["day_base_vlm"], 192.5)
            self.assertEqual(spx["asset_ctx_observation_age_seconds"], 30.0)
            self.assertNotIn("mark_px", btc)
            self.assertNotIn("asset_ctx_observation_age_seconds", btc)


if __name__ == "__main__":
    unittest.main()
