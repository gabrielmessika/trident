"""Fetches and stores historical candles and funding rates from Hyperliquid API."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.hyperliquid.info_client import HyperliquidInfoClient
from app.settings import HyperliquidConfig

logger = logging.getLogger(__name__)

# HL candleSnapshot returns up to 5000 candles per request.
MAX_CANDLES_PER_REQUEST = 5000

VALID_INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


@dataclass(slots=True)
class FetchStats:
    candle_requests: int = 0
    candles_fetched: int = 0
    funding_requests: int = 0
    funding_records_fetched: int = 0
    coins_processed: int = 0
    days_written: int = 0


class HyperliquidHistoricalFetcher:
    """Downloads historical candles and funding rates, stores them as daily JSONL files."""

    def __init__(
        self,
        config: HyperliquidConfig,
        *,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.client = HyperliquidInfoClient(config, sleep_fn=sleep_fn)
        self.sleep_fn = sleep_fn or time.sleep
        self.stats = FetchStats()

    def fetch_candles(
        self,
        *,
        coins: list[str],
        start_date: date,
        end_date: date,
        interval: str = "1h",
        output_dir: str | Path = "data/historical_candles",
    ) -> int:
        if interval not in VALID_INTERVALS:
            raise ValueError(f"Invalid interval {interval!r}, must be one of {VALID_INTERVALS}")

        output_path = Path(output_dir)
        total_written = 0

        for coin in coins:
            coin_upper = coin.upper()
            coin_dir = output_path / interval / coin_upper
            coin_dir.mkdir(parents=True, exist_ok=True)

            start_ms = _date_to_ms(start_date)
            end_ms = _date_to_ms(end_date + timedelta(days=1))
            interval_ms = INTERVAL_MS[interval]

            all_candles = self._fetch_candle_range(
                coin=coin_upper,
                interval=interval,
                start_ms=start_ms,
                end_ms=end_ms,
                interval_ms=interval_ms,
            )
            self.stats.coins_processed += 1

            written = _write_candles_by_day(all_candles, coin_dir)
            total_written += written
            self.stats.days_written += written
            logger.info(
                "candles coin=%s interval=%s candles=%d days=%d",
                coin_upper, interval, len(all_candles), written,
            )

        return total_written

    def fetch_funding(
        self,
        *,
        coins: list[str],
        start_date: date,
        end_date: date,
        output_dir: str | Path = "data/historical_funding",
    ) -> int:
        output_path = Path(output_dir)
        total_written = 0

        for coin in coins:
            coin_upper = coin.upper()
            coin_dir = output_path / coin_upper
            coin_dir.mkdir(parents=True, exist_ok=True)

            start_ms = _date_to_ms(start_date)
            end_ms = _date_to_ms(end_date + timedelta(days=1))

            all_records = self._fetch_funding_range(
                coin=coin_upper,
                start_ms=start_ms,
                end_ms=end_ms,
            )

            written = _write_funding_by_day(all_records, coin_dir)
            total_written += written
            logger.info(
                "funding coin=%s records=%d days=%d",
                coin_upper, len(all_records), written,
            )

        return total_written

    def _fetch_candle_range(
        self,
        *,
        coin: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        interval_ms: int,
    ) -> list[dict[str, object]]:
        all_candles: list[dict[str, object]] = []
        cursor_ms = start_ms
        max_window_ms = MAX_CANDLES_PER_REQUEST * interval_ms

        while cursor_ms < end_ms:
            chunk_end = min(cursor_ms + max_window_ms, end_ms)
            payload = {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": cursor_ms,
                    "endTime": chunk_end,
                },
            }
            result = self.client.post_info(payload)
            self.stats.candle_requests += 1

            if not isinstance(result, list) or not result:
                break

            self.stats.candles_fetched += len(result)
            all_candles.extend(result)

            if len(result) < MAX_CANDLES_PER_REQUEST:
                break

            last_ts = int(result[-1].get("t", chunk_end))
            next_cursor = last_ts + interval_ms
            if next_cursor <= cursor_ms:
                logger.warning("candle cursor did not advance for %s, stopping", coin)
                break
            cursor_ms = next_cursor

        return all_candles

    def _fetch_funding_range(
        self,
        *,
        coin: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, object]]:
        all_records: list[dict[str, object]] = []
        cursor_ms = start_ms
        # Funding is hourly, fetch in 30-day chunks
        chunk_ms = 30 * 24 * 3_600_000

        while cursor_ms < end_ms:
            chunk_end = min(cursor_ms + chunk_ms, end_ms)
            payload = {
                "type": "fundingHistory",
                "coin": coin,
                "startTime": cursor_ms,
                "endTime": chunk_end,
            }
            result = self.client.post_info(payload)
            self.stats.funding_requests += 1

            if not isinstance(result, list) or not result:
                break

            self.stats.funding_records_fetched += len(result)
            all_records.extend(result)

            last_ts = int(result[-1].get("time", chunk_end))
            next_cursor = last_ts + 3_600_000
            if next_cursor <= cursor_ms:
                logger.warning("funding cursor did not advance for %s, stopping", coin)
                break
            cursor_ms = next_cursor

        return all_records


def _date_to_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _write_candles_by_day(
    candles: list[dict[str, object]],
    coin_dir: Path,
) -> int:
    by_day: dict[str, list[dict[str, object]]] = {}
    for candle in candles:
        ts_ms = int(candle.get("t", 0))
        day_key = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day_key, []).append(candle)

    for day_key, day_candles in sorted(by_day.items()):
        path = coin_dir / f"{day_key}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for candle in day_candles:
                f.write(json.dumps(candle) + "\n")

    return len(by_day)


def _write_funding_by_day(
    records: list[dict[str, object]],
    coin_dir: Path,
) -> int:
    by_day: dict[str, list[dict[str, object]]] = {}
    for record in records:
        ts_ms = int(record.get("time", 0))
        day_key = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day_key, []).append(record)

    for day_key, day_records in sorted(by_day.items()):
        path = coin_dir / f"{day_key}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for record in day_records:
                f.write(json.dumps(record) + "\n")

    return len(by_day)
