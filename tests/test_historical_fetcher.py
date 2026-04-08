import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app.hyperliquid.historical_fetcher import (
    HyperliquidHistoricalFetcher,
    _date_to_ms,
    _write_candles_by_day,
    _write_funding_by_day,
)
from app.settings import load_config


class DateToMsTests(unittest.TestCase):
    def test_converts_date_to_utc_millis(self) -> None:
        d = date(2026, 1, 1)
        ms = _date_to_ms(d)
        self.assertEqual(ms, 1_767_225_600_000)


class WriteCandlesByDayTests(unittest.TestCase):
    def test_splits_candles_into_daily_files(self) -> None:
        candles = [
            {"t": 1_767_225_600_000, "o": "100", "h": "110", "l": "90", "c": "105", "v": "50"},
            {"t": 1_767_225_660_000, "o": "105", "h": "115", "l": "95", "c": "110", "v": "60"},
            {"t": 1_767_312_000_000, "o": "110", "h": "120", "l": "100", "c": "115", "v": "70"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            coin_dir = Path(tmpdir)
            days_written = _write_candles_by_day(candles, coin_dir)
            self.assertEqual(days_written, 2)
            day1 = coin_dir / "2026-01-01.jsonl"
            day2 = coin_dir / "2026-01-02.jsonl"
            self.assertTrue(day1.exists())
            self.assertTrue(day2.exists())
            lines1 = [json.loads(l) for l in day1.read_text().strip().split("\n")]
            lines2 = [json.loads(l) for l in day2.read_text().strip().split("\n")]
            self.assertEqual(len(lines1), 2)
            self.assertEqual(len(lines2), 1)


class WriteFundingByDayTests(unittest.TestCase):
    def test_splits_funding_into_daily_files(self) -> None:
        records = [
            {"time": 1_767_225_600_000, "coin": "BTC", "fundingRate": "0.0001", "premium": "0.0"},
            {"time": 1_767_312_000_000, "coin": "BTC", "fundingRate": "0.0002", "premium": "0.0"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            coin_dir = Path(tmpdir)
            days_written = _write_funding_by_day(records, coin_dir)
            self.assertEqual(days_written, 2)


class FetcherCandlePaginationTests(unittest.TestCase):
    def test_fetches_candles_single_batch(self) -> None:
        """A single batch that returns < 5000 candles stops immediately."""
        config = load_config("config/trident.toml")

        def fake_post_info(payload, **kwargs):
            start = payload["req"]["startTime"]
            return [
                {"t": start + i * 3_600_000, "o": "100", "h": "110", "l": "90", "c": "105", "v": "50", "n": 10}
                for i in range(24)
            ]

        fetcher = HyperliquidHistoricalFetcher(config.hyperliquid, sleep_fn=lambda _: None)
        fetcher.client.post_info = fake_post_info

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher.fetch_candles(
                coins=["BTC"],
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 1),
                interval="1h",
                output_dir=tmpdir,
            )

        self.assertEqual(fetcher.stats.candle_requests, 1)
        self.assertEqual(fetcher.stats.candles_fetched, 24)

    def test_invalid_interval_raises(self) -> None:
        config = load_config("config/trident.toml")
        fetcher = HyperliquidHistoricalFetcher(config.hyperliquid, sleep_fn=lambda _: None)
        with self.assertRaises(ValueError):
            fetcher.fetch_candles(
                coins=["BTC"],
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2),
                interval="2h",
                output_dir="/tmp/test",
            )

    def test_cursor_stall_protection(self) -> None:
        """If the API returns data whose last_ts is before cursor_ms, the loop stops."""
        config = load_config("config/trident.toml")
        call_count = 0

        def fake_post_info(payload, **kwargs):
            nonlocal call_count
            call_count += 1
            start = payload["req"]["startTime"]
            # Return 5000 candles but last one has timestamp BEFORE startTime
            # This simulates a buggy API or data that would cause cursor to go backwards
            return [{"t": start - 3_600_000, "o": "100", "h": "110", "l": "90", "c": "105", "v": "50", "n": 10}] * 5000

        fetcher = HyperliquidHistoricalFetcher(config.hyperliquid, sleep_fn=lambda _: None)
        fetcher.client.post_info = fake_post_info

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher.fetch_candles(
                coins=["BTC"],
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
                interval="1h",
                output_dir=tmpdir,
            )

        # Should stop after first batch due to stall protection
        self.assertEqual(call_count, 1)


class FetcherFundingTests(unittest.TestCase):
    def test_fetches_and_stores_funding(self) -> None:
        config = load_config("config/trident.toml")

        def fake_post_info(payload, **kwargs):
            if payload.get("type") == "fundingHistory":
                start = payload.get("startTime", 0)
                # Only return data for the first query; subsequent ones return empty
                if start <= 1_767_225_600_000:
                    return [
                        {"time": 1_767_225_600_000, "coin": "BTC", "fundingRate": "0.0001", "premium": "0.0"},
                        {"time": 1_767_229_200_000, "coin": "BTC", "fundingRate": "0.0002", "premium": "0.0"},
                    ]
                return []
            return []

        fetcher = HyperliquidHistoricalFetcher(config.hyperliquid, sleep_fn=lambda _: None)
        fetcher.client.post_info = fake_post_info

        with tempfile.TemporaryDirectory() as tmpdir:
            days = fetcher.fetch_funding(
                coins=["BTC"],
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 1),
                output_dir=tmpdir,
            )
            self.assertEqual(days, 1)
            path = Path(tmpdir) / "BTC" / "2026-01-01.jsonl"
            self.assertTrue(path.exists())
            lines = path.read_text().strip().split("\n")
            self.assertEqual(len(lines), 2)

    def test_funding_cursor_stall_protection(self) -> None:
        """If funding API returns data that doesn't advance cursor, the loop stops."""
        config = load_config("config/trident.toml")
        call_count = 0

        def fake_post_info(payload, **kwargs):
            nonlocal call_count
            call_count += 1
            start = payload.get("startTime", 0)
            # Return record with timestamp before startTime → cursor can't advance
            return [{"time": start - 3_600_000, "coin": "BTC", "fundingRate": "0.0001", "premium": "0.0"}]

        fetcher = HyperliquidHistoricalFetcher(config.hyperliquid, sleep_fn=lambda _: None)
        fetcher.client.post_info = fake_post_info

        with tempfile.TemporaryDirectory() as tmpdir:
            fetcher.fetch_funding(
                coins=["BTC"],
                start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31),
                output_dir=tmpdir,
            )

        # Should stop after first batch, not loop forever
        self.assertEqual(call_count, 1)


if __name__ == "__main__":
    unittest.main()
