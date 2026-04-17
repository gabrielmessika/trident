import gzip
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.research.hyperliquid_top30_research import (
    CandleRecord,
    HyperliquidTop30Analyzer,
    HyperliquidTop30DatasetBuilder,
)


def _write_gzip_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _synthetic_candles(symbol: str, *, count: int, leader: bool = False, follower: bool = False) -> list[dict[str, object]]:
    candles: list[dict[str, object]] = []
    for index in range(count):
        timestamp = 1_700_000_000_000 + index * 3_600_000
        if leader:
            close = 100.0 + index * 0.14 + math.sin(index / 5.0) * 1.4
        elif follower:
            close = 70.0 + index * 0.10 + math.sin((index - 1) / 5.0) * 1.0
        else:
            close = 35.0 + math.sin(index / 2.0) * 2.8
        open_px = close - 0.35
        high = close + 0.7
        low = close - 0.8
        volume = 1000.0 + (index % 12) * 25.0 + (120.0 if leader and index % 18 == 0 else 0.0)
        candles.append(
            {
                "start_time": timestamp,
                "end_time": timestamp + 3_599_999,
                "interval": "1h",
                "symbol": symbol,
                "open": round(open_px, 6),
                "high": round(high, 6),
                "low": round(low, 6),
                "close": round(close, 6),
                "volume": round(volume, 6),
                "trade_count": 25 + (index % 7),
            }
        )
    return candles


class HyperliquidTop30ResearchTests(unittest.TestCase):
    def test_fetch_top_symbols_ranks_by_volume_then_oi(self) -> None:
        builder = HyperliquidTop30DatasetBuilder()
        payload = [
            {
                "universe": [
                    {"name": "BTC", "maxLeverage": 40},
                    {"name": "ETH", "maxLeverage": 25},
                    {"name": "MATIC", "maxLeverage": 20, "isDelisted": True},
                ]
            },
            [
                {"markPx": "60000", "dayNtlVlm": "1000000", "openInterest": "100"},
                {"markPx": "3000", "dayNtlVlm": "1000000", "openInterest": "50"},
                {"markPx": "1", "dayNtlVlm": "9999999", "openInterest": "1"},
            ],
        ]

        with patch.object(builder.client, "post_info", return_value=payload):
            ranked = builder._fetch_top_symbols(top_n=2)

        self.assertEqual([item.symbol for item in ranked], ["BTC", "ETH"])
        self.assertEqual(ranked[0].rank, 1)
        self.assertEqual(ranked[1].rank, 2)

    def test_fetch_funding_history_paginates_until_short_page(self) -> None:
        builder = HyperliquidTop30DatasetBuilder()
        first_page = [
            {
                "coin": "BTC",
                "fundingRate": "0.0001",
                "premium": "0.01",
                "time": 1000 + index * 1000,
            }
            for index in range(500)
        ]
        second_page = [
            {"coin": "BTC", "fundingRate": "0.0003", "premium": "0.03", "time": 600000},
        ]

        with patch.object(builder.client, "post_info", side_effect=[first_page, second_page]) as mocked_post:
            funding = builder._fetch_funding_history(symbol="BTC", start_ms=1000, end_ms=1_000_000)

        self.assertEqual(len(funding), 501)
        self.assertEqual(funding[0].time, 1000)
        self.assertEqual(funding[-1].time, 600000)
        self.assertEqual(mocked_post.call_count, 2)

    def test_analyzer_builds_correlation_report_from_synthetic_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_dir = Path(tmpdir)
            manifest = {
                "requested_start": "2026-01-01T00:00:00Z",
                "requested_end": "2026-01-10T00:00:00Z",
                "symbols": ["BTC", "ETH", "SOL"],
                "intervals": ["1h"],
                "ranking": [
                    {"rank": 1, "symbol": "BTC", "day_ntl_vlm": 1_000_000_000, "open_interest_usd": 900_000_000},
                    {"rank": 2, "symbol": "ETH", "day_ntl_vlm": 500_000_000, "open_interest_usd": 400_000_000},
                    {"rank": 3, "symbol": "SOL", "day_ntl_vlm": 100_000_000, "open_interest_usd": 80_000_000},
                ],
                "availability": {
                    "1h": {
                        "symbols": {
                            "BTC": {"available": True, "full_requested_window": True, "coverage_ratio_vs_request": 1.0},
                            "ETH": {"available": True, "full_requested_window": True, "coverage_ratio_vs_request": 1.0},
                            "SOL": {"available": True, "full_requested_window": True, "coverage_ratio_vs_request": 1.0},
                        }
                    }
                },
            }
            (dataset_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            _write_gzip_json(
                dataset_dir / "raw" / "candles" / "1h" / "BTC.json.gz",
                _synthetic_candles("BTC", count=240, leader=True),
            )
            _write_gzip_json(
                dataset_dir / "raw" / "candles" / "1h" / "ETH.json.gz",
                _synthetic_candles("ETH", count=240, follower=True),
            )
            _write_gzip_json(
                dataset_dir / "raw" / "candles" / "1h" / "SOL.json.gz",
                _synthetic_candles("SOL", count=240),
            )
            _write_gzip_json(dataset_dir / "raw" / "funding" / "BTC.json.gz", [])
            _write_gzip_json(dataset_dir / "raw" / "funding" / "ETH.json.gz", [])
            _write_gzip_json(dataset_dir / "raw" / "funding" / "SOL.json.gz", [])

            result = HyperliquidTop30Analyzer().analyze(dataset_dir)

        self.assertEqual(len(result.symbol_reports), 3)
        self.assertTrue(result.top_correlations)
        top_pair = result.top_correlations[0]
        self.assertEqual({top_pair.left_symbol, top_pair.right_symbol}, {"BTC", "ETH"})
        self.assertGreater(top_pair.correlation, 0.8)
        eth_report = next(item for item in result.symbol_reports if item.symbol == "ETH")
        self.assertIsNotNone(eth_report.btc_correlation_1h)
        self.assertGreater(eth_report.btc_correlation_1h, 0.8)

    def test_build_features_exposes_extended_indicator_pack(self) -> None:
        analyzer = HyperliquidTop30Analyzer()
        candle_rows = [
            CandleRecord(**row)
            for row in _synthetic_candles("BTC", count=180, leader=True)
        ]

        features = analyzer._build_features(interval="1h", candles=candle_rows, funding=[])

        for key in (
            "ichimoku_tenkan",
            "ichimoku_kijun",
            "ichimoku_cloud_top",
            "stoch_rsi_k",
            "rolling_vwap_20",
            "cci20",
            "supertrend_direction",
            "keltner_upper",
            "squeeze_on",
            "obv_slope_5",
            "mfi14",
        ):
            self.assertIn(key, features)
            self.assertTrue(any(value is not None for value in features[key]))


if __name__ == "__main__":
    unittest.main()
