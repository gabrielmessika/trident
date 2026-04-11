import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.cohabitation_replay import CohabitationReplayRunner
from app.settings import load_config


def _cohabitation_record(timestamp: str, btc_price: float, eth_price: float, xrp_price: float) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 32.0,
            "atr_ratio": 1.1,
            "range_width_bps": 160.0,
            "structure_score": 0.55,
            "btc_impulse": False,
        },
        "symbols": [
            {
                "symbol": "BTC",
                "price": btc_price,
                "ema_fast": btc_price * 0.999,
                "ema_slow": btc_price * 0.995,
                "vwap_distance_bps": -5.0,
                "structure_score": 0.48,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
                "book_imbalance": 0.2,
                "trade_flow_bias": 0.2,
                "bucket_volume": 100.0,
                "bucket_trade_count": 20,
                "bucket_range_bps": 20.0,
                "source": "test",
            },
            {
                "symbol": "ETH",
                "price": eth_price,
                "ema_fast": eth_price * 0.999,
                "ema_slow": eth_price * 0.99,
                "vwap_distance_bps": -8.0,
                "structure_score": 0.62,
                "funding_rate": 0.0001,
                "spread_bps": 1.2,
                "btc_aligned": True,
                "book_imbalance": 0.25,
                "trade_flow_bias": 0.3,
                "bucket_volume": 120.0,
                "bucket_trade_count": 24,
                "bucket_range_bps": 30.0,
                "source": "test",
            },
            {
                "symbol": "XRP",
                "price": xrp_price,
                "ema_fast": xrp_price,
                "ema_slow": xrp_price,
                "vwap_distance_bps": 0.0,
                "structure_score": 0.0,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
                "book_imbalance": 0.0,
                "trade_flow_bias": 0.0,
                "bucket_volume": 10.0,
                "bucket_trade_count": 5,
                "bucket_range_bps": 10.0,
                "source": "test",
            },
        ],
    }


class CohabitationReplayTests(unittest.TestCase):
    def test_replay_separates_pod_a_and_pod_b_symbol_ownership(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_a.enabled = True
        config.pod_b.enabled = True
        config.pod_b.paper_pause_outside_range = False

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            input_path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        _cohabitation_record("2026-04-05T10:00:00Z", 1000.0, 100.0, 100.0),
                        _cohabitation_record("2026-04-05T10:01:00Z", 1005.0, 100.2, 99.5),
                        _cohabitation_record("2026-04-05T10:02:00Z", 1010.0, 100.6, 100.5),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = CohabitationReplayRunner(config).run_jsonl(input_path)

            self.assertEqual(result.records_processed, 3)
            self.assertEqual(result.ownership_conflict_count, 0)
            self.assertEqual(result.ownership_conflicts, [])
            self.assertEqual(result.pod_a_owned_symbols, ["BTC", "ETH"])
            self.assertEqual(result.pod_b_owned_symbols, ["XRP"])
            self.assertTrue(result.no_symbol_overlap)
            self.assertGreaterEqual(result.pod_b_total_fill_count, 2)
            self.assertGreaterEqual(result.pod_a_signal_count, 1)


if __name__ == "__main__":
    unittest.main()
