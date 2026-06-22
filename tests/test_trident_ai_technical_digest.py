from __future__ import annotations

import json
import unittest

from app.trident_ai import (
    MAX_TECHNICAL_DIGEST_CHARS,
    TECHNICAL_INDICATOR_COVERAGE,
    TRIDENT_AI_TECHNICAL_DIGEST_SCHEMA_VERSION,
    build_technical_digest,
    compact_technical_digest,
)


def _encoded_len(payload: dict[str, object]) -> int:
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")))


class TridentAITechnicalDigestTests(unittest.TestCase):
    def test_top50_contract_is_complete_and_compact(self) -> None:
        digest = build_technical_digest(
            {
                "ema_fast": 60020.0,
                "ema_slow": 59880.0,
                "ema_alignment": "bullish",
                "vwap_distance_bps": 12.0,
                "structure_score": 0.62,
                "trade_flow_bias": 0.18,
                "book_imbalance": 0.12,
                "bucket_volume": 120.0,
                "signed_trade_delta": 18.0,
                "volume_ratio": 1.2,
                "bucket_range_bps": 18.0,
                "realized_vol_short_bps": 42.0,
                "compression_score": 0.18,
                "external_momentum_60s_bps": 1.4,
                "external_momentum_300s_bps": 0.9,
            }
        )

        ranks = {item[0] for item in TECHNICAL_INDICATOR_COVERAGE}
        ids = {item[1] for item in TECHNICAL_INDICATOR_COVERAGE}
        self.assertEqual(ranks, set(range(1, 51)))
        self.assertEqual(len(ids), 50)
        self.assertEqual(digest["schema_version"], TRIDENT_AI_TECHNICAL_DIGEST_SCHEMA_VERSION)
        self.assertEqual(digest["coverage"]["universe"], "tradingview_top50")
        self.assertEqual(digest["coverage"]["used_count"], 50)
        self.assertEqual(digest["coverage"]["missing_count"], 0)
        self.assertLessEqual(_encoded_len(digest), MAX_TECHNICAL_DIGEST_CHARS)
        self.assertLessEqual(digest["char_count"], MAX_TECHNICAL_DIGEST_CHARS)
        self.assertIn("trend_ma", digest["families"])
        self.assertLessEqual(len(digest["top_signals"]), 8)
        self.assertNotIn("ema_fast", json.dumps(digest))

    def test_digest_surfaces_vetoes_and_conflicts_without_series(self) -> None:
        digest = build_technical_digest(
            {
                "ema_fast": 60020.0,
                "ema_slow": 59880.0,
                "ema_alignment": "bullish",
                "vwap_distance_bps": 55.0,
                "structure_score": 0.6,
                "trade_flow_bias": 0.45,
                "book_imbalance": -0.5,
                "volume_ratio": 0.45,
                "bucket_range_bps": 35.0,
                "realized_vol_short_bps": 82.0,
                "external_momentum_60s_bps": -6.0,
                "external_momentum_300s_bps": -4.0,
            }
        )

        veto_ids = {item["id"] for item in digest["veto_signals"]}
        conflict_ids = {item["id"] for item in digest["conflicts"]}
        self.assertTrue({"vwap_overextension", "atr_extreme"}.intersection(veto_ids))
        self.assertIn("flow_book_conflict", conflict_ids)
        self.assertEqual(digest["bias"]["quality"], "veto_or_conflict")
        self.assertLessEqual(_encoded_len(digest), MAX_TECHNICAL_DIGEST_CHARS)

    def test_compact_technical_digest_sanitizes_external_payload(self) -> None:
        digest = build_technical_digest(
            {
                "ema_alignment": "bearish",
                "ema_fast": 99.0,
                "ema_slow": 100.0,
                "vwap_distance_bps": -8.0,
                "structure_score": -0.4,
                "trade_flow_bias": -0.3,
                "book_imbalance": -0.2,
                "realized_vol_short_bps": 20.0,
            }
        )
        digest["top_signals"] = list(digest["top_signals"]) * 5

        compact = compact_technical_digest(digest)

        self.assertEqual(compact["coverage"]["used_count"], 50)
        self.assertLessEqual(len(compact["top_signals"]), 8)
        self.assertLessEqual(_encoded_len(compact), MAX_TECHNICAL_DIGEST_CHARS)


if __name__ == "__main__":
    unittest.main()
