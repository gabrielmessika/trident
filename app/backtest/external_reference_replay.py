from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import json
import statistics
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

from app.backtest.external_reference_policy import (
    ExternalReferenceDecisionPolicy,
    ExternalReferencePolicyConfig,
)
from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.backtest.snapshot_loader import open_jsonl_text, resolve_jsonl_files
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol

MINUTE_MS = 60_000


@dataclass(slots=True)
class KlinePoint:
    open_time_ms: int
    close_price: float


@dataclass(slots=True)
class SourceQuote:
    source: str
    source_symbol: str
    open_time_ms: int
    price: float
    momentum_60s_bps: float
    momentum_300s_bps: float


@dataclass(slots=True)
class InputScan:
    records: int
    symbol_records: int
    start_ms: int
    end_ms: int
    symbols: list[str]


@dataclass(slots=True)
class ExternalReferenceCoverage:
    records: int
    symbol_records: int
    enriched_symbol_records: int
    coverage_pct: float
    requested_symbols: list[str]
    fetched_symbols: list[str]
    missing_symbols: list[str]
    sources: list[str] = field(default_factory=list)
    fetched_symbols_by_source: dict[str, list[str]] = field(default_factory=dict)
    missing_symbols_by_source: dict[str, list[str]] = field(default_factory=dict)
    enriched_symbol_records_by_source: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ExternalReferenceReplayResult:
    baseline_input_path: str
    enriched_input_path: str
    coverage: ExternalReferenceCoverage
    baseline: dict[str, object]
    guardrail: dict[str, object]
    guardrail_plus_confidence: dict[str, object]
    report_path: str
    summary_path: str


class HistoricalKlineClient:
    def __init__(
        self,
        *,
        source: str,
        base_url: str,
        timeout_seconds: float = 10.0,
        sleep_seconds: float = 0.05,
    ) -> None:
        self.source = source
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.sleep_seconds = sleep_seconds

    def exchange_symbols(self, hl_symbol: str) -> list[str]:
        raise NotImplementedError

    def fetch_1m(
        self,
        exchange_symbol: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[KlinePoint]:
        raise NotImplementedError

    def _get_json(self, url: str) -> object | None:
        req = request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "trident-external-reference-replay/0.1",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, OSError, error.HTTPError, error.URLError, json.JSONDecodeError):
            return None


class BinanceKlineClient(HistoricalKlineClient):
    def __init__(
        self,
        *,
        base_url: str = "https://data-api.binance.vision",
        timeout_seconds: float = 10.0,
        sleep_seconds: float = 0.05,
    ) -> None:
        super().__init__(
            source="binance",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            sleep_seconds=sleep_seconds,
        )

    def exchange_symbols(self, hl_symbol: str) -> list[str]:
        symbol = _external_symbol_root(hl_symbol)
        return [f"{symbol}USDT"] if symbol else []

    def fetch_1m(
        self,
        exchange_symbol: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[KlinePoint]:
        cursor = max(start_ms, 0)
        points: list[KlinePoint] = []
        while cursor <= end_ms:
            url = self.base_url + "/api/v3/klines?" + parse.urlencode(
                {
                    "symbol": exchange_symbol,
                    "interval": "1m",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                }
            )
            payload = self._get_json(url)
            if not isinstance(payload, list):
                break
            rows = [row for row in payload if isinstance(row, list) and len(row) >= 5]
            if not rows:
                break
            for row in rows:
                try:
                    open_time_ms = int(row[0])
                    close_price = float(row[4])
                except (TypeError, ValueError):
                    continue
                if close_price > 0:
                    points.append(KlinePoint(open_time_ms=open_time_ms, close_price=close_price))
            last_open_time = int(rows[-1][0])
            next_cursor = last_open_time + MINUTE_MS
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)
        deduped: dict[int, KlinePoint] = {point.open_time_ms: point for point in points}
        return [deduped[key] for key in sorted(deduped)]


class OkxKlineClient(HistoricalKlineClient):
    def __init__(
        self,
        *,
        base_url: str = "https://www.okx.com",
        timeout_seconds: float = 10.0,
        sleep_seconds: float = 0.05,
    ) -> None:
        super().__init__(
            source="okx",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            sleep_seconds=sleep_seconds,
        )

    def exchange_symbols(self, hl_symbol: str) -> list[str]:
        symbol = _external_symbol_root(hl_symbol)
        return [f"{symbol}-USDT"] if symbol else []

    def fetch_1m(
        self,
        exchange_symbol: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[KlinePoint]:
        points: list[KlinePoint] = []
        for chunk_start, chunk_end in _minute_chunks(start_ms, end_ms, 300):
            url = self.base_url + "/api/v5/market/history-candles?" + parse.urlencode(
                {
                    "instId": exchange_symbol,
                    "bar": "1m",
                    "before": max(chunk_start - 1, 0),
                    "after": chunk_end + 1,
                    "limit": 300,
                }
            )
            payload = self._get_json(url)
            if not isinstance(payload, dict) or str(payload.get("code", "")) != "0":
                continue
            data = payload.get("data", [])
            if not isinstance(data, list):
                continue
            for row in data:
                if not isinstance(row, list) or len(row) < 5:
                    continue
                try:
                    open_time_ms = int(row[0])
                    close_price = float(row[4])
                except (TypeError, ValueError):
                    continue
                if chunk_start <= open_time_ms <= chunk_end and close_price > 0:
                    points.append(KlinePoint(open_time_ms=open_time_ms, close_price=close_price))
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)
        return _dedupe_points(points)


class BybitKlineClient(HistoricalKlineClient):
    def __init__(
        self,
        *,
        base_url: str = "https://api.bybit.com",
        timeout_seconds: float = 10.0,
        sleep_seconds: float = 0.05,
    ) -> None:
        super().__init__(
            source="bybit",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            sleep_seconds=sleep_seconds,
        )

    def exchange_symbols(self, hl_symbol: str) -> list[str]:
        symbol = _external_symbol_root(hl_symbol)
        return [f"{symbol}USDT"] if symbol else []

    def fetch_1m(
        self,
        exchange_symbol: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[KlinePoint]:
        points: list[KlinePoint] = []
        for chunk_start, chunk_end in _minute_chunks(start_ms, end_ms, 1000):
            url = self.base_url + "/v5/market/kline?" + parse.urlencode(
                {
                    "category": "spot",
                    "symbol": exchange_symbol,
                    "interval": "1",
                    "start": chunk_start,
                    "end": chunk_end,
                    "limit": 1000,
                }
            )
            payload = self._get_json(url)
            try:
                ret_code = int(payload.get("retCode", -1)) if isinstance(payload, dict) else -1
            except (TypeError, ValueError):
                ret_code = -1
            if not isinstance(payload, dict) or ret_code != 0:
                continue
            result = payload.get("result", {})
            rows = result.get("list", []) if isinstance(result, dict) else []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, list) or len(row) < 5:
                    continue
                try:
                    open_time_ms = int(row[0])
                    close_price = float(row[4])
                except (TypeError, ValueError):
                    continue
                if chunk_start <= open_time_ms <= chunk_end and close_price > 0:
                    points.append(KlinePoint(open_time_ms=open_time_ms, close_price=close_price))
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)
        return _dedupe_points(points)


class CoinbaseKlineClient(HistoricalKlineClient):
    def __init__(
        self,
        *,
        base_url: str = "https://api.coinbase.com",
        timeout_seconds: float = 10.0,
        sleep_seconds: float = 0.05,
    ) -> None:
        super().__init__(
            source="coinbase",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            sleep_seconds=sleep_seconds,
        )

    def exchange_symbols(self, hl_symbol: str) -> list[str]:
        symbol = _external_symbol_root(hl_symbol)
        return [f"{symbol}-USD", f"{symbol}-USDT"] if symbol else []

    def fetch_1m(
        self,
        exchange_symbol: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[KlinePoint]:
        points: list[KlinePoint] = []
        for chunk_start, chunk_end in _minute_chunks(start_ms, end_ms, 350):
            url = (
                self.base_url
                + f"/api/v3/brokerage/market/products/{parse.quote(exchange_symbol)}/candles?"
                + parse.urlencode(
                    {
                        "start": chunk_start // 1000,
                        "end": chunk_end // 1000,
                        "granularity": "ONE_MINUTE",
                        "limit": 350,
                    }
                )
            )
            payload = self._get_json(url)
            if not isinstance(payload, dict):
                continue
            rows = payload.get("candles", [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    open_time_ms = int(row["start"]) * 1000
                    close_price = float(row["close"])
                except (KeyError, TypeError, ValueError):
                    continue
                if chunk_start <= open_time_ms <= chunk_end and close_price > 0:
                    points.append(KlinePoint(open_time_ms=open_time_ms, close_price=close_price))
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)
        return _dedupe_points(points)


class KrakenKlineClient(HistoricalKlineClient):
    def __init__(
        self,
        *,
        base_url: str = "https://api.kraken.com",
        timeout_seconds: float = 10.0,
        sleep_seconds: float = 0.05,
    ) -> None:
        super().__init__(
            source="kraken",
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            sleep_seconds=sleep_seconds,
        )

    def exchange_symbols(self, hl_symbol: str) -> list[str]:
        symbol = _external_symbol_root(hl_symbol)
        if not symbol:
            return []
        base = "XBT" if symbol == "BTC" else symbol
        return [f"{base}USDT", f"{base}USD"]

    def fetch_1m(
        self,
        exchange_symbol: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> list[KlinePoint]:
        url = self.base_url + "/0/public/OHLC?" + parse.urlencode(
            {
                "pair": exchange_symbol,
                "interval": 1,
                "since": start_ms // 1000,
            }
        )
        payload = self._get_json(url)
        if not isinstance(payload, dict):
            return []
        errors = payload.get("error", [])
        if isinstance(errors, list) and errors:
            return []
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return []
        rows: list[object] = []
        for key, value in result.items():
            if key == "last":
                continue
            if isinstance(value, list):
                rows = value
                break
        points: list[KlinePoint] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 5:
                continue
            try:
                open_time_ms = int(float(row[0]) * 1000)
                close_price = float(row[4])
            except (TypeError, ValueError):
                continue
            if start_ms <= open_time_ms <= end_ms and close_price > 0:
                points.append(KlinePoint(open_time_ms=open_time_ms, close_price=close_price))
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)
        return _dedupe_points(points)


class KlineIndex:
    def __init__(self, points: list[KlinePoint]) -> None:
        self.points = sorted(points, key=lambda item: item.open_time_ms)
        self.open_times = [point.open_time_ms for point in self.points]

    def close_at_or_before(self, timestamp_ms: int) -> tuple[int, float] | None:
        if not self.open_times:
            return None
        bucket_ms = timestamp_ms - (timestamp_ms % MINUTE_MS)
        index = bisect.bisect_right(self.open_times, bucket_ms) - 1
        if index < 0:
            return None
        point = self.points[index]
        return point.open_time_ms, point.close_price

    def close_before(self, open_time_ms: int, minutes: int) -> float | None:
        target = open_time_ms - minutes * MINUTE_MS
        index = bisect.bisect_right(self.open_times, target) - 1
        if index < 0:
            return None
        return self.points[index].close_price


def _timestamp_ms(timestamp: str) -> int:
    normalized = timestamp.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _iso_from_ms(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _dedupe_points(points: list[KlinePoint]) -> list[KlinePoint]:
    deduped: dict[int, KlinePoint] = {point.open_time_ms: point for point in points}
    return [deduped[key] for key in sorted(deduped)]


def _minute_chunks(
    start_ms: int,
    end_ms: int,
    max_minutes: int,
) -> Iterator[tuple[int, int]]:
    cursor = start_ms - (start_ms % MINUTE_MS)
    limit_ms = max(max_minutes, 1) * MINUTE_MS
    while cursor <= end_ms:
        chunk_end = min(cursor + limit_ms - MINUTE_MS, end_ms)
        yield cursor, chunk_end
        cursor = chunk_end + MINUTE_MS


def _external_symbol_root(hl_symbol: str) -> str | None:
    symbol = hl_symbol.strip().upper()
    if not symbol or ":" in symbol:
        return None
    return symbol


def _is_external_crypto_symbol(
    item: dict[str, object],
    *,
    config: AppConfig | None = None,
) -> bool:
    symbol = str(item.get("symbol", "")).strip().upper()
    if _external_symbol_root(symbol) is None:
        return False
    raw_cluster = item.get("market_cluster")
    cluster = (
        str(raw_cluster or "").strip().lower()
        if raw_cluster is not None
        else (cluster_for_symbol(config, symbol) if config is not None else "crypto")
    )
    return cluster == "crypto"


def _input_files(input_path: Path) -> list[Path]:
    return resolve_jsonl_files(input_path)


def _default_date_window(input_path: Path) -> tuple[str | None, str | None]:
    dates = [path.stem for path in _input_files(input_path) if path.stem[:4].isdigit()]
    if not dates:
        return None, None
    dates = sorted(dates)
    return dates[-2] if len(dates) >= 2 else dates[-1], dates[-1]


def _record_in_window(
    record: dict[str, object],
    *,
    start_date: str | None,
    end_date: str | None,
) -> bool:
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        return False
    date_key = timestamp[:10]
    if start_date is not None and date_key < start_date:
        return False
    if end_date is not None and date_key > end_date:
        return False
    return True


def _load_records(
    input_path: Path,
    *,
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for file_path in _input_files(input_path):
        if start_date is not None and file_path.stem[:10] < start_date:
            continue
        if end_date is not None and file_path.stem[:10] > end_date:
            continue
        with open_jsonl_text(file_path) as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if _record_in_window(payload, start_date=start_date, end_date=end_date):
                    records.append(payload)
    return records


def _iter_records(
    input_path: Path,
    *,
    start_date: str | None,
    end_date: str | None,
) -> Iterator[dict[str, object]]:
    for file_path in _input_files(input_path):
        if start_date is not None and file_path.stem[:10] < start_date:
            continue
        if end_date is not None and file_path.stem[:10] > end_date:
            continue
        with open_jsonl_text(file_path) as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if _record_in_window(payload, start_date=start_date, end_date=end_date):
                    yield payload


def _scan_input(
    input_path: Path,
    *,
    start_date: str | None,
    end_date: str | None,
    config: AppConfig | None = None,
) -> InputScan:
    timestamps: list[int] = []
    symbols: set[str] = set()
    records = 0
    symbol_records = 0
    for record in _iter_records(input_path, start_date=start_date, end_date=end_date):
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str):
            timestamps.append(_timestamp_ms(timestamp))
        records += 1
        for item in record.get("symbols", []):
            if not isinstance(item, dict):
                continue
            symbol_records += 1
            if not _is_external_crypto_symbol(item, config=config):
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            symbols.add(symbol)
    if not records or not timestamps:
        raise RuntimeError("no replay records matched the requested date window")
    return InputScan(
        records=records,
        symbol_records=symbol_records,
        start_ms=min(timestamps),
        end_ms=max(timestamps),
        symbols=sorted(symbols),
    )


def _binance_symbol(hl_symbol: str) -> str | None:
    symbol = _external_symbol_root(hl_symbol)
    return f"{symbol}USDT" if symbol else None


def _collect_symbols(records: list[dict[str, object]]) -> list[str]:
    symbols: set[str] = set()
    for record in records:
        for item in record.get("symbols", []):
            if not isinstance(item, dict):
                continue
            if not _is_external_crypto_symbol(item):
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            symbols.add(symbol)
    return sorted(symbols)


def _cache_path(
    cache_dir: Path,
    *,
    source: str,
    exchange_symbol: str,
    start_ms: int,
    end_ms: int,
) -> Path:
    start_key = _iso_from_ms(start_ms)[:10].replace("-", "")
    end_key = _iso_from_ms(end_ms)[:10].replace("-", "")
    safe_symbol = exchange_symbol.replace("/", "_").replace(":", "_").replace("-", "_")
    return cache_dir / f"{source}_1m" / f"{safe_symbol}_1m_{start_key}_{end_key}.json"


def _build_clients(
    *,
    sources: list[str],
    binance_base_url: str,
    okx_base_url: str,
    bybit_base_url: str,
    coinbase_base_url: str,
    kraken_base_url: str,
) -> list[HistoricalKlineClient]:
    clients: dict[str, HistoricalKlineClient] = {
        "binance": BinanceKlineClient(base_url=binance_base_url),
        "okx": OkxKlineClient(base_url=okx_base_url),
        "bybit": BybitKlineClient(base_url=bybit_base_url),
        "coinbase": CoinbaseKlineClient(base_url=coinbase_base_url),
        "kraken": KrakenKlineClient(base_url=kraken_base_url),
    }
    return [
        clients[source]
        for source in dict.fromkeys(item.strip().lower() for item in sources if item.strip())
        if source in clients
    ]


def _load_or_fetch_source_klines(
    *,
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    cache_dir: Path,
    clients: list[HistoricalKlineClient],
    fetch_workers: int = 4,
) -> dict[str, dict[str, tuple[str, KlineIndex]]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    indexes: dict[str, dict[str, tuple[str, KlineIndex]]] = {
        client.source: {} for client in clients
    }
    fetch_start_ms = max(start_ms - 10 * MINUTE_MS, 0)
    fetch_end_ms = end_ms + MINUTE_MS
    for client in clients:
        (cache_dir / f"{client.source}_1m").mkdir(parents=True, exist_ok=True)
        worker_count = max(1, min(fetch_workers, 3 if client.source == "okx" else 6))

        def load_symbol(symbol: str) -> tuple[str, str, KlineIndex] | None:
            exchange_symbols = client.exchange_symbols(symbol)
            for exchange_symbol in exchange_symbols:
                points = _load_or_fetch_exchange_symbol(
                    client=client,
                    cache_dir=cache_dir,
                    symbol=symbol,
                    exchange_symbol=exchange_symbol,
                    fetch_start_ms=fetch_start_ms,
                    fetch_end_ms=fetch_end_ms,
                )
                if points:
                    return symbol, exchange_symbol, KlineIndex(points)
            return None

        if worker_count == 1:
            results = [load_symbol(symbol) for symbol in symbols]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(load_symbol, symbols))
        for result in results:
            if result is None:
                continue
            symbol, exchange_symbol, index = result
            indexes[client.source][symbol] = (exchange_symbol, index)
    return indexes


def _load_or_fetch_exchange_symbol(
    *,
    client: HistoricalKlineClient,
    cache_dir: Path,
    symbol: str,
    exchange_symbol: str,
    fetch_start_ms: int,
    fetch_end_ms: int,
) -> list[KlinePoint]:
    path = _cache_path(
        cache_dir,
        source=client.source,
        exchange_symbol=exchange_symbol,
        start_ms=fetch_start_ms,
        end_ms=fetch_end_ms,
    )
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [
            KlinePoint(
                open_time_ms=int(item["open_time_ms"]),
                close_price=float(item["close_price"]),
            )
            for item in payload.get("points", [])
            if isinstance(item, dict)
        ]
    points = client.fetch_1m(
        exchange_symbol,
        start_ms=fetch_start_ms,
        end_ms=fetch_end_ms,
    )
    path.write_text(
        json.dumps(
            {
                "source": client.source,
                "symbol": symbol,
                "exchange_symbol": exchange_symbol,
                "interval": "1m",
                "points": [asdict(point) for point in points],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return points


def _source_symbol_sets(
    indexes: dict[str, dict[str, tuple[str, KlineIndex]]],
    requested_symbols: list[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    fetched: dict[str, list[str]] = {}
    missing: dict[str, list[str]] = {}
    for source, source_indexes in indexes.items():
        fetched[source] = sorted(source_indexes)
        missing[source] = [
            symbol for symbol in requested_symbols if symbol not in source_indexes
        ]
    return fetched, missing


def _merged_fetched_symbols(
    indexes: dict[str, dict[str, tuple[str, KlineIndex]]],
) -> list[str]:
    symbols: set[str] = set()
    for source_indexes in indexes.values():
        symbols.update(source_indexes)
    return sorted(symbols)


def _source_quote(
    *,
    source: str,
    source_symbol: str,
    index: KlineIndex,
    timestamp_ms: int,
    max_quote_age_seconds: float,
) -> SourceQuote | None:
    close = index.close_at_or_before(timestamp_ms)
    if close is None:
        return None
    open_time_ms, price = close
    age_seconds = max(timestamp_ms - open_time_ms, 0) / 1000.0
    if age_seconds > max_quote_age_seconds:
        return None
    previous_60s = index.close_before(open_time_ms, 1)
    previous_300s = index.close_before(open_time_ms, 5)
    return SourceQuote(
        source=source,
        source_symbol=source_symbol,
        open_time_ms=open_time_ms,
        price=price,
        momentum_60s_bps=_momentum_bps(price, previous_60s),
        momentum_300s_bps=_momentum_bps(price, previous_300s),
    )


def _select_reference_quotes(
    quotes: list[SourceQuote],
    *,
    max_source_deviation_bps: float,
) -> tuple[list[SourceQuote], float]:
    if not quotes:
        return [], 0.0
    anchor = float(statistics.median([quote.price for quote in quotes]))
    kept: list[SourceQuote] = []
    max_deviation = 0.0
    for quote in quotes:
        deviation = abs(quote.price - anchor) / anchor * 10_000.0 if anchor > 0 else 0.0
        max_deviation = max(max_deviation, deviation)
        if deviation <= max_source_deviation_bps:
            kept.append(quote)
    return kept, round(max_deviation, 4)


def _median_or_zero(values: Iterable[float]) -> float:
    items = [value for value in values]
    if not items:
        return 0.0
    return float(statistics.median(items))


def _reference_from_sources(
    *,
    symbol: str,
    timestamp_ms: int,
    hl_price: float,
    indexes: dict[str, dict[str, tuple[str, KlineIndex]]],
    max_quote_age_seconds: float,
    max_source_deviation_bps: float,
) -> tuple[dict[str, object] | None, list[str]]:
    quotes: list[SourceQuote] = []
    for source, source_indexes in indexes.items():
        payload = source_indexes.get(symbol)
        if payload is None:
            continue
        source_symbol, index = payload
        quote = _source_quote(
            source=source,
            source_symbol=source_symbol,
            index=index,
            timestamp_ms=timestamp_ms,
            max_quote_age_seconds=max_quote_age_seconds,
        )
        if quote is not None:
            quotes.append(quote)
    kept, max_deviation = _select_reference_quotes(
        quotes,
        max_source_deviation_bps=max_source_deviation_bps,
    )
    if not kept:
        return None, []
    reference_price = _median_or_zero(quote.price for quote in kept)
    premium_bps = (
        (hl_price - reference_price) / reference_price * 10_000.0
        if hl_price > 0 and reference_price > 0
        else 0.0
    )
    reference_time_ms = max(quote.open_time_ms for quote in kept)
    momentum_60s = _median_or_zero(quote.momentum_60s_bps for quote in kept)
    momentum_300s = _median_or_zero(quote.momentum_300s_bps for quote in kept)
    sources = [quote.source for quote in kept]
    return (
        {
            "external_reference_price": round(reference_price, 8),
            "external_reference_source_count": len(kept),
            "external_reference_sources": ",".join(sources),
            "external_reference_symbol": ",".join(
                f"{quote.source}:{quote.source_symbol}" for quote in kept
            ),
            "external_reference_time": _iso_from_ms(reference_time_ms),
            "external_reference_age_seconds": round(
                max(timestamp_ms - reference_time_ms, 0) / 1000.0,
                4,
            ),
            "external_reference_max_deviation_bps": max_deviation,
            "external_premium_bps": round(premium_bps, 4),
            "external_momentum_60s_bps": round(momentum_60s, 4),
            "external_momentum_300s_bps": round(momentum_300s, 4),
            "external_alignment_score": round(
                _external_alignment_score(momentum_60s, momentum_300s),
                4,
            ),
        },
        sources,
    )


def _momentum_bps(current: float, previous: float | None) -> float:
    if previous is None or previous <= 0 or current <= 0:
        return 0.0
    return (current - previous) / previous * 10_000.0


def _external_alignment_score(momentum_60s: float, momentum_300s: float) -> float:
    return _clamp(momentum_60s / 15.0, -1.0, 1.0) * 0.65 + _clamp(
        momentum_300s / 35.0,
        -1.0,
        1.0,
    ) * 0.35


def _enrich_record(
    record: dict[str, object],
    indexes: dict[str, dict[str, tuple[str, KlineIndex]]],
    *,
    max_quote_age_seconds: float,
    max_source_deviation_bps: float,
) -> tuple[dict[str, object], int, int, dict[str, int]]:
    enriched = dict(record)
    timestamp = enriched.get("timestamp")
    if not isinstance(timestamp, str):
        return enriched, 0, 0, {}
    timestamp_ms = _timestamp_ms(timestamp)
    symbol_records = 0
    enriched_symbol_records = 0
    enriched_by_source: dict[str, int] = {}
    enriched_symbols: list[dict[str, object]] = []
    for item in enriched.get("symbols", []):
        if not isinstance(item, dict):
            continue
        symbol_records += 1
        symbol_payload = dict(item)
        symbol = str(symbol_payload.get("symbol", "")).strip().upper()
        try:
            hl_price = float(symbol_payload.get("price", 0.0))
        except (TypeError, ValueError):
            hl_price = 0.0
        reference_payload, sources = _reference_from_sources(
            symbol=symbol,
            timestamp_ms=timestamp_ms,
            hl_price=hl_price,
            indexes=indexes,
            max_quote_age_seconds=max_quote_age_seconds,
            max_source_deviation_bps=max_source_deviation_bps,
        )
        if reference_payload is None:
            symbol_payload.setdefault("external_reference_source_count", 0)
            enriched_symbols.append(symbol_payload)
            continue
        symbol_payload.update(reference_payload)
        enriched_symbol_records += 1
        for source in sources:
            enriched_by_source[source] = enriched_by_source.get(source, 0) + 1
        enriched_symbols.append(symbol_payload)
    enriched["symbols"] = enriched_symbols
    return enriched, symbol_records, enriched_symbol_records, enriched_by_source


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _write_baseline_and_enriched_jsonl(
    *,
    input_path: Path,
    start_date: str | None,
    end_date: str | None,
    baseline_path: Path,
    enriched_path: Path,
    indexes: dict[str, dict[str, tuple[str, KlineIndex]]],
    max_quote_age_seconds: float,
    max_source_deviation_bps: float,
) -> tuple[int, int, int, dict[str, int]]:
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    enriched_path.parent.mkdir(parents=True, exist_ok=True)
    records = 0
    symbol_records = 0
    enriched_symbol_records = 0
    enriched_by_source: dict[str, int] = {}
    with baseline_path.open("w", encoding="utf-8") as baseline_handle, enriched_path.open(
        "w",
        encoding="utf-8",
    ) as enriched_handle:
        for record in _iter_records(input_path, start_date=start_date, end_date=end_date):
            baseline_handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            enriched_record, record_symbols, record_enriched, record_sources = _enrich_record(
                record,
                indexes,
                max_quote_age_seconds=max_quote_age_seconds,
                max_source_deviation_bps=max_source_deviation_bps,
            )
            enriched_handle.write(json.dumps(enriched_record, separators=(",", ":")) + "\n")
            records += 1
            symbol_records += record_symbols
            enriched_symbol_records += record_enriched
            for source, count in record_sources.items():
                enriched_by_source[source] = enriched_by_source.get(source, 0) + count
    return records, symbol_records, enriched_symbol_records, enriched_by_source


def _run_full_bot(
    *,
    config_path: str,
    input_path: Path,
    report_path: Path,
    summary_path: Path,
    policy: ExternalReferenceDecisionPolicy | None = None,
) -> FullBotBacktestResult:
    return FullBotBacktestRunner(
        load_config(config_path),
        external_reference_policy=policy,
    ).run_jsonl(
        input_path=input_path,
        report_output=report_path,
        summary_output=summary_path,
    )


def _pod_summary(payload: dict[str, object]) -> dict[str, object]:
    rejections = dict(payload.get("rejections_by_reason", {}) or {})
    external_rejections = {
        reason: count
        for reason, count in rejections.items()
        if str(reason).startswith("external_reference_")
    }
    return {
        "realized_pnl_usd": payload.get("realized_pnl_usd", 0.0),
        "closed_trade_count": payload.get("closed_trade_count", 0),
        "signal_count": payload.get("signal_count", 0),
        "accepted_count": payload.get("accepted_count", 0),
        "rejected_count": payload.get("rejected_count", 0),
        "external_rejections": external_rejections,
    }


def _scenario_summary(result: FullBotBacktestResult) -> dict[str, object]:
    return {
        "total_realized_pnl_usd": result.total_realized_pnl_usd,
        "directional_fees_usd": result.directional_fees_usd,
        "total_activity_count": result.total_activity_count,
        "pod_a": _pod_summary(result.pod_a),
        "pod_b": _pod_summary(result.pod_b),
        "pod_c": _pod_summary(result.pod_c),
        "records_processed": result.records_processed,
        "dates_covered": result.dates_covered,
        "report_path": result.report_path,
        "summary_path": result.summary_path,
    }


def _render_summary(result: ExternalReferenceReplayResult) -> str:
    scenarios = [
        ("baseline", result.baseline),
        ("guardrail", result.guardrail),
        ("guardrail_plus_confidence", result.guardrail_plus_confidence),
    ]
    lines = [
        "# External reference replay\n\n",
        f"- baseline_input: `{result.baseline_input_path}`\n",
        f"- enriched_input: `{result.enriched_input_path}`\n",
        f"- coverage: `{result.coverage.enriched_symbol_records}/{result.coverage.symbol_records}` "
        f"symbol-records (`{result.coverage.coverage_pct:.2f}%`)\n",
        f"- sources: `{', '.join(result.coverage.sources)}`\n",
        f"- fetched_symbols: `{', '.join(result.coverage.fetched_symbols)}`\n",
        f"- missing_symbols: `{', '.join(result.coverage.missing_symbols)}`\n",
        f"- enriched_by_source: `{result.coverage.enriched_symbol_records_by_source}`\n",
        "\n",
        "| scenario | total pnl | Pod A pnl/trades | Pod C pnl/trades | ext rejections A | ext rejections C |\n",
        "| --- | ---: | ---: | ---: | --- | --- |\n",
    ]
    for name, payload in scenarios:
        pod_a = payload["pod_a"]
        pod_c = payload["pod_c"]
        lines.append(
            f"| {name} | {float(payload['total_realized_pnl_usd']):.4f} | "
            f"{float(pod_a['realized_pnl_usd']):.4f}/{int(pod_a['closed_trade_count'])} | "
            f"{float(pod_c['realized_pnl_usd']):.4f}/{int(pod_c['closed_trade_count'])} | "
            f"{pod_a['external_rejections']} | {pod_c['external_rejections']} |\n"
        )
    lines.extend(
        [
            "\n",
            "## Notes\n\n",
            "- External references use the same off-Hyperliquid venues as Pod B: Binance, OKX, Bybit, Coinbase and Kraken when history is available.\n",
            "- The per-minute reference is the median of sources that agree within the configured deviation threshold.\n",
            "- Missing external references pass through in the guardrail scenarios, so uncovered symbols keep baseline behavior.\n",
            "- Guardrail effects are applied only in this replay runner; live Pod A/C decisions are unchanged.\n",
        ]
    )
    return "".join(lines)


def run_external_reference_replay(
    *,
    config_path: str,
    input_path: Path,
    start_date: str | None,
    end_date: str | None,
    output_prefix: str,
    replay_input_dir: Path,
    report_dir: Path,
    cache_dir: Path,
    sources: list[str],
    binance_base_url: str,
    okx_base_url: str,
    bybit_base_url: str,
    coinbase_base_url: str,
    kraken_base_url: str,
    max_quote_age_seconds: float,
    max_source_deviation_bps: float,
    fetch_workers: int,
) -> ExternalReferenceReplayResult:
    if start_date is None and end_date is None:
        start_date, end_date = _default_date_window(input_path)
    app_config = load_config(config_path)
    scan = _scan_input(
        input_path,
        start_date=start_date,
        end_date=end_date,
        config=app_config,
    )
    clients = _build_clients(
        sources=sources,
        binance_base_url=binance_base_url,
        okx_base_url=okx_base_url,
        bybit_base_url=bybit_base_url,
        coinbase_base_url=coinbase_base_url,
        kraken_base_url=kraken_base_url,
    )
    indexes = _load_or_fetch_source_klines(
        symbols=scan.symbols,
        start_ms=scan.start_ms,
        end_ms=scan.end_ms,
        cache_dir=cache_dir,
        clients=clients,
        fetch_workers=fetch_workers,
    )

    baseline_input_path = replay_input_dir / f"{output_prefix}_baseline.jsonl"
    enriched_input_path = replay_input_dir / f"{output_prefix}_external_enriched.jsonl"
    records, symbol_records, enriched_symbol_records, enriched_by_source = (
        _write_baseline_and_enriched_jsonl(
            input_path=input_path,
            start_date=start_date,
            end_date=end_date,
            baseline_path=baseline_input_path,
            enriched_path=enriched_input_path,
            indexes=indexes,
            max_quote_age_seconds=max_quote_age_seconds,
            max_source_deviation_bps=max_source_deviation_bps,
        )
    )

    fetched_by_source, missing_by_source = _source_symbol_sets(indexes, scan.symbols)
    fetched_symbols = _merged_fetched_symbols(indexes)
    coverage = ExternalReferenceCoverage(
        records=records,
        symbol_records=symbol_records,
        enriched_symbol_records=enriched_symbol_records,
        coverage_pct=(
            enriched_symbol_records / symbol_records * 100.0 if symbol_records else 0.0
        ),
        requested_symbols=scan.symbols,
        fetched_symbols=fetched_symbols,
        missing_symbols=[symbol for symbol in scan.symbols if symbol not in fetched_symbols],
        sources=[client.source for client in clients],
        fetched_symbols_by_source=fetched_by_source,
        missing_symbols_by_source=missing_by_source,
        enriched_symbol_records_by_source=enriched_by_source,
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    baseline = _run_full_bot(
        config_path=config_path,
        input_path=baseline_input_path,
        report_path=report_dir / f"{output_prefix}_baseline.json",
        summary_path=report_dir / f"{output_prefix}_baseline.md",
    )
    guardrail_policy = ExternalReferenceDecisionPolicy(ExternalReferencePolicyConfig())
    guardrail = _run_full_bot(
        config_path=config_path,
        input_path=enriched_input_path,
        report_path=report_dir / f"{output_prefix}_guardrail.json",
        summary_path=report_dir / f"{output_prefix}_guardrail.md",
        policy=guardrail_policy,
    )
    guardrail_confidence_policy = ExternalReferenceDecisionPolicy(
        ExternalReferencePolicyConfig(confidence_adjustment_enabled=True)
    )
    guardrail_plus_confidence = _run_full_bot(
        config_path=config_path,
        input_path=enriched_input_path,
        report_path=report_dir / f"{output_prefix}_guardrail_plus_confidence.json",
        summary_path=report_dir / f"{output_prefix}_guardrail_plus_confidence.md",
        policy=guardrail_confidence_policy,
    )

    report_path = report_dir / f"{output_prefix}_external_reference_comparison.json"
    summary_path = report_dir / f"{output_prefix}_external_reference_comparison.md"
    result = ExternalReferenceReplayResult(
        baseline_input_path=str(baseline_input_path),
        enriched_input_path=str(enriched_input_path),
        coverage=coverage,
        baseline=_scenario_summary(baseline),
        guardrail=_scenario_summary(guardrail),
        guardrail_plus_confidence=_scenario_summary(guardrail_plus_confidence),
        report_path=str(report_path),
        summary_path=str(summary_path),
    )
    report_path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    summary_path.write_text(_render_summary(result), encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare baseline Pod A/C replay with multi-exchange external reference guardrails.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/live_snapshots")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--output-prefix", default="external_reference_guardrail")
    parser.add_argument("--replay-input-dir", default="server-data/replay_inputs")
    parser.add_argument("--report-dir", default="server-data/replay_reports")
    parser.add_argument("--cache-dir", default="server-data/external_reference")
    parser.add_argument(
        "--sources",
        default="binance,okx,bybit,coinbase,kraken",
        help="Comma-separated off-Hyperliquid reference sources. Defaults to Pod B external venues.",
    )
    parser.add_argument(
        "--binance-base-url",
        default="https://data-api.binance.vision",
        help="Binance market-data base URL. Defaults to the data API host.",
    )
    parser.add_argument("--okx-base-url", default="https://www.okx.com")
    parser.add_argument("--bybit-base-url", default="https://api.bybit.com")
    parser.add_argument("--coinbase-base-url", default="https://api.coinbase.com")
    parser.add_argument("--kraken-base-url", default="https://api.kraken.com")
    parser.add_argument("--max-quote-age-seconds", type=float, default=120.0)
    parser.add_argument("--max-source-deviation-bps", type=float, default=50.0)
    parser.add_argument("--fetch-workers", type=int, default=4)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = run_external_reference_replay(
        config_path=args.config,
        input_path=Path(args.input),
        start_date=args.start_date,
        end_date=args.end_date,
        output_prefix=args.output_prefix,
        replay_input_dir=Path(args.replay_input_dir),
        report_dir=Path(args.report_dir),
        cache_dir=Path(args.cache_dir),
        sources=[item.strip() for item in args.sources.split(",") if item.strip()],
        binance_base_url=args.binance_base_url,
        okx_base_url=args.okx_base_url,
        bybit_base_url=args.bybit_base_url,
        coinbase_base_url=args.coinbase_base_url,
        kraken_base_url=args.kraken_base_url,
        max_quote_age_seconds=args.max_quote_age_seconds,
        max_source_deviation_bps=args.max_source_deviation_bps,
        fetch_workers=args.fetch_workers,
    )
    print(f"records={result.coverage.records}")
    print(f"coverage_pct={result.coverage.coverage_pct:.2f}")
    print(f"baseline_total_pnl={result.baseline['total_realized_pnl_usd']}")
    print(f"guardrail_total_pnl={result.guardrail['total_realized_pnl_usd']}")
    print(
        "guardrail_plus_confidence_total_pnl="
        f"{result.guardrail_plus_confidence['total_realized_pnl_usd']}"
    )
    print(f"summary_path={result.summary_path}")


if __name__ == "__main__":
    main()
