import json
import tempfile
import unittest
from pathlib import Path

from app.research.pod_c_research_suite import PodCResearchSuite


class PodCResearchSuiteTests(unittest.TestCase):
    def test_suite_outputs_best_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            json_path = Path(tmpdir) / "suite.json"
            md_path = Path(tmpdir) / "suite.md"
            input_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-04-05T10:00:00Z",
                                "regime_snapshot": {
                                    "ready": True,
                                    "adx": 20.0,
                                    "atr_ratio": 1.0,
                                    "range_width_bps": 80.0,
                                    "structure_score": 0.2,
                                    "btc_impulse": True,
                                },
                                "symbols": [
                                    {"symbol": "BTC", "price": 100.0, "ema_fast": 100.0, "ema_slow": 100.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "SOL", "price": 50.0, "ema_fast": 50.0, "ema_slow": 50.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "HYPE", "price": 20.0, "ema_fast": 20.0, "ema_slow": 20.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-05T10:01:00Z",
                                "regime_snapshot": {
                                    "ready": True,
                                    "adx": 20.0,
                                    "atr_ratio": 1.0,
                                    "range_width_bps": 80.0,
                                    "structure_score": 0.2,
                                    "btc_impulse": True,
                                },
                                "symbols": [
                                    {"symbol": "BTC", "price": 100.2, "ema_fast": 100.2, "ema_slow": 100.2, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "SOL", "price": 50.0, "ema_fast": 50.0, "ema_slow": 50.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "HYPE", "price": 20.0, "ema_fast": 20.0, "ema_slow": 20.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-05T10:02:00Z",
                                "regime_snapshot": {
                                    "ready": True,
                                    "adx": 20.0,
                                    "atr_ratio": 1.0,
                                    "range_width_bps": 80.0,
                                    "structure_score": 0.2,
                                    "btc_impulse": True,
                                },
                                "symbols": [
                                    {"symbol": "BTC", "price": 100.25, "ema_fast": 100.25, "ema_slow": 100.25, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "SOL", "price": 50.2, "ema_fast": 50.2, "ema_slow": 50.2, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "HYPE", "price": 20.0, "ema_fast": 20.0, "ema_slow": 20.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-05T10:03:00Z",
                                "regime_snapshot": {
                                    "ready": True,
                                    "adx": 20.0,
                                    "atr_ratio": 1.0,
                                    "range_width_bps": 80.0,
                                    "structure_score": 0.2,
                                    "btc_impulse": True,
                                },
                                "symbols": [
                                    {"symbol": "BTC", "price": 100.45, "ema_fast": 100.45, "ema_slow": 100.45, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "SOL", "price": 50.2, "ema_fast": 50.2, "ema_slow": 50.2, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "HYPE", "price": 20.0, "ema_fast": 20.0, "ema_slow": 20.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-05T10:04:00Z",
                                "regime_snapshot": {
                                    "ready": True,
                                    "adx": 20.0,
                                    "atr_ratio": 1.0,
                                    "range_width_bps": 80.0,
                                    "structure_score": 0.2,
                                    "btc_impulse": True,
                                },
                                "symbols": [
                                    {"symbol": "BTC", "price": 100.5, "ema_fast": 100.5, "ema_slow": 100.5, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "SOL", "price": 50.4, "ema_fast": 50.4, "ema_slow": 50.4, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "HYPE", "price": 20.0, "ema_fast": 20.0, "ema_slow": 20.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-05T10:05:00Z",
                                "regime_snapshot": {
                                    "ready": True,
                                    "adx": 20.0,
                                    "atr_ratio": 1.0,
                                    "range_width_bps": 80.0,
                                    "structure_score": 0.2,
                                    "btc_impulse": True,
                                },
                                "symbols": [
                                    {"symbol": "BTC", "price": 100.7, "ema_fast": 100.7, "ema_slow": 100.7, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "SOL", "price": 50.4, "ema_fast": 50.4, "ema_slow": 50.4, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "HYPE", "price": 20.0, "ema_fast": 20.0, "ema_slow": 20.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-05T10:06:00Z",
                                "regime_snapshot": {
                                    "ready": True,
                                    "adx": 20.0,
                                    "atr_ratio": 1.0,
                                    "range_width_bps": 80.0,
                                    "structure_score": 0.2,
                                    "btc_impulse": True,
                                },
                                "symbols": [
                                    {"symbol": "BTC", "price": 100.75, "ema_fast": 100.75, "ema_slow": 100.75, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "SOL", "price": 50.6, "ema_fast": 50.6, "ema_slow": 50.6, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                    {"symbol": "HYPE", "price": 20.0, "ema_fast": 20.0, "ema_slow": 20.0, "vwap_distance_bps": 0.0, "structure_score": 0.0, "funding_rate": 0.0, "spread_bps": 1.0, "btc_aligned": True, "book_imbalance": 0.0, "trade_flow_bias": 0.0, "bucket_volume": 1.0, "bucket_trade_count": 1, "bucket_range_bps": 1.0, "source": "test"},
                                ],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = PodCResearchSuite().run(
                input_path=input_path,
                leader_symbols=["BTC"],
                follower_symbols=["SOL", "HYPE"],
                impulse_threshold_bps=8.0,
                horizon_bars=1,
                output_json=json_path,
                output_md=md_path,
            )

            self.assertEqual(result.recommendation, "go")
            self.assertEqual(result.best_candidate["best_symbol"], "SOL")
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())


if __name__ == "__main__":
    unittest.main()
