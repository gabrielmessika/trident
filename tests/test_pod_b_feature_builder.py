import unittest

from app.live.pod_b_feature_builder import PodBFeatureBuilder


class PodBFeatureBuilderTests(unittest.TestCase):
    def test_builder_emits_intraminute_sidecar_rows(self) -> None:
        builder = PodBFeatureBuilder(["BTC"], bucket_ms=10_000)

        builder.ingest_book(
            {
                "coin": "BTC",
                "time": 1_000,
                "levels": [
                    [{"px": "68000", "sz": "2.0", "n": 1}],
                    [{"px": "68002", "sz": "3.0", "n": 1}],
                ],
            }
        )
        rows = builder.ingest_trade(
            {
                "coin": "BTC",
                "side": "B",
                "px": "68001",
                "sz": "0.5",
                "time": 11_000,
            }
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["timestamp"], "1970-01-01T00:00:00Z")
        self.assertEqual(row["symbol"], "BTC")
        self.assertEqual(row["trade_count_10s"], 0)
        self.assertEqual(row["trade_count_30s"], 0)
        self.assertEqual(row["source"], "hyperliquid_live_collector_pod_b_sidecar")
        self.assertIn("delta_mid_10s_bps", row)
        self.assertIn("microprice_dislocation_bps", row)
        self.assertIn("sweep_signature_score", row)

    def test_builder_tracks_10s_and_30s_activity_features(self) -> None:
        builder = PodBFeatureBuilder(["BTC"], bucket_ms=10_000)

        builder.ingest_book(
            {
                "coin": "BTC",
                "time": 1_000,
                "levels": [
                    [{"px": "68000", "sz": "2.0", "n": 1}],
                    [{"px": "68002", "sz": "3.0", "n": 1}],
                ],
            }
        )
        builder.ingest_trade(
            {
                "coin": "BTC",
                "side": "B",
                "px": "68001",
                "sz": "0.5",
                "time": 2_000,
            }
        )
        builder.ingest_book(
            {
                "coin": "BTC",
                "time": 11_000,
                "levels": [
                    [{"px": "68010", "sz": "4.0", "n": 1}],
                    [{"px": "68014", "sz": "1.0", "n": 1}],
                ],
            }
        )
        builder.ingest_trade(
            {
                "coin": "BTC",
                "side": "B",
                "px": "68012",
                "sz": "1.0",
                "time": 12_000,
            }
        )
        builder.ingest_book(
            {
                "coin": "BTC",
                "time": 21_000,
                "levels": [
                    [{"px": "68030", "sz": "5.0", "n": 1}],
                    [{"px": "68034", "sz": "1.0", "n": 1}],
                ],
            }
        )
        builder.ingest_trade(
            {
                "coin": "BTC",
                "side": "B",
                "px": "68033",
                "sz": "1.5",
                "time": 22_000,
            }
        )
        rows = builder.ingest_book(
            {
                "coin": "BTC",
                "time": 31_000,
                "levels": [
                    [{"px": "68045", "sz": "5.0", "n": 1}],
                    [{"px": "68049", "sz": "1.2", "n": 1}],
                ],
            }
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["trade_count_10s"], 1)
        self.assertEqual(row["trade_count_30s"], 3)
        self.assertGreater(row["notional_volume_10s_usd"], 0.0)
        self.assertGreater(row["notional_volume_30s_usd"], row["notional_volume_10s_usd"])
        self.assertGreater(row["volume_ratio"], 1.0)
        self.assertGreater(row["trade_count_ratio"], 1.0)
        self.assertGreater(row["delta_mid_10s_bps"], 0.0)
        self.assertGreater(row["delta_mid_30s_bps"], row["delta_mid_10s_bps"])
        self.assertGreater(row["activity_score"], 0.0)
        self.assertGreater(row["sweep_signature_score"], 0.0)
        self.assertGreaterEqual(row["compression_score"], 0.0)
        self.assertLessEqual(row["compression_score"], 1.0)


if __name__ == "__main__":
    unittest.main()
