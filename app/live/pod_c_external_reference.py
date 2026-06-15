from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib import error, parse, request

from app.settings import AppConfig, PodCExternalReferenceConfig


DEFAULT_REFERENCE_SPECS: dict[str, list[str]] = {
    "XYZ:CL": ["yahoo:CL=F"],
    "XYZ:BRENTOIL": ["yahoo:BZ=F"],
    "XYZ:SP500": ["yahoo:ES=F"],
    "XYZ:XYZ100": ["yahoo:NQ=F"],
    "XYZ:SILVER": ["yahoo:SI=F"],
    "XYZ:GOLD": ["yahoo:GC=F"],
    "XYZ:JPY": ["yahoo:JPY=X"],
    "XYZ:TSLA": ["yahoo:TSLA"],
    "XYZ:NVDA": ["yahoo:NVDA"],
    "XYZ:CRCL": ["yahoo:CRCL"],
}


@dataclass(frozen=True, slots=True)
class ReferenceSpec:
    source: str
    symbol: str


@dataclass(slots=True)
class ReferenceQuote:
    source: str
    source_symbol: str
    price: float
    time_ms: int
    momentum_60s_bps: float = 0.0
    momentum_300s_bps: float = 0.0


@dataclass(slots=True)
class _QuoteCacheEntry:
    quote: ReferenceQuote
    fetched_at: float


ReferenceFetcher = Callable[[ReferenceSpec, float], ReferenceQuote | None]


class PodCExternalReferenceEnricher:
    """Adds read-only external reference fields to Pod C live snapshots."""

    def __init__(
        self,
        config: AppConfig | PodCExternalReferenceConfig,
        *,
        fetcher: ReferenceFetcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = (
            config.pod_c.external_reference
            if isinstance(config, AppConfig)
            else config
        )
        self._fetcher = fetcher or fetch_reference_quote
        self._monotonic = monotonic
        configured_symbols = {
            symbol.upper(): list(specs)
            for symbol, specs in self.config.symbols.items()
            if symbol.strip() and specs
        }
        self._specs_by_symbol = {
            symbol: [_parse_spec(item) for item in specs]
            for symbol, specs in {**DEFAULT_REFERENCE_SPECS, **configured_symbols}.items()
        }
        self._quote_cache: dict[ReferenceSpec, _QuoteCacheEntry] = {}
        self.records_seen = 0
        self.symbols_seen = 0
        self.symbols_enriched = 0
        self.symbols_missing_reference = 0
        self.fetch_error_count = 0
        self.last_error: str | None = None

    def enrich_record(self, record: dict[str, object]) -> dict[str, object]:
        self.records_seen += 1
        if not self.config.enabled:
            return record
        raw_symbols = record.get("symbols", [])
        if not isinstance(raw_symbols, list):
            return record
        timestamp_ms = _timestamp_ms(str(record.get("timestamp") or ""))
        payload = dict(record)
        enriched_symbols: list[object] = []
        for item in raw_symbols:
            if not isinstance(item, dict):
                enriched_symbols.append(item)
                continue
            enriched = dict(item)
            self.symbols_seen += 1
            symbol = str(enriched.get("symbol", "")).strip().upper()
            price = _optional_float(enriched.get("price")) or 0.0
            if symbol and price > 0:
                reference = self.reference_for_symbol(
                    symbol,
                    hl_price=price,
                    timestamp_ms=timestamp_ms,
                )
                if reference is not None:
                    enriched.update(reference)
                    self.symbols_enriched += 1
                elif symbol in self._specs_by_symbol:
                    self.symbols_missing_reference += 1
            enriched_symbols.append(enriched)
        payload["symbols"] = enriched_symbols
        return payload

    def reference_for_symbol(
        self,
        symbol: str,
        *,
        hl_price: float,
        timestamp_ms: int,
    ) -> dict[str, object] | None:
        specs = self._specs_by_symbol.get(symbol.upper())
        if not specs:
            return None
        quotes = [
            quote
            for spec in specs
            if (quote := self._quote_for_spec(spec)) is not None
        ]
        kept, max_deviation = _select_reference_quotes(
            quotes,
            max_source_deviation_bps=max(self.config.max_source_deviation_bps, 0.0),
        )
        if len(kept) < max(int(self.config.min_sources), 1):
            return None
        reference_price = _median(quote.price for quote in kept)
        if reference_price <= 0:
            return None
        reference_time_ms = max(quote.time_ms for quote in kept)
        momentum_60s = _median(quote.momentum_60s_bps for quote in kept)
        momentum_300s = _median(quote.momentum_300s_bps for quote in kept)
        premium_bps = (
            (hl_price - reference_price) / reference_price * 10_000.0
            if hl_price > 0
            else 0.0
        )
        return {
            "external_reference_price": round(reference_price, 8),
            "external_reference_source_count": len(kept),
            "external_reference_sources": ",".join(quote.source for quote in kept),
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
        }

    def stats_payload(self) -> dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "configured_symbols": sorted(self._specs_by_symbol),
            "records_seen": self.records_seen,
            "symbols_seen": self.symbols_seen,
            "symbols_enriched": self.symbols_enriched,
            "symbols_missing_reference": self.symbols_missing_reference,
            "fetch_error_count": self.fetch_error_count,
            "last_error": self.last_error,
        }

    def _quote_for_spec(self, spec: ReferenceSpec) -> ReferenceQuote | None:
        now = self._monotonic()
        ttl = max(float(self.config.cache_ttl_seconds), 0.0)
        cached = self._quote_cache.get(spec)
        if cached is not None and (ttl <= 0.0 or now - cached.fetched_at <= ttl):
            return cached.quote
        try:
            quote = self._fetcher(spec, max(float(self.config.timeout_seconds), 0.1))
        except Exception as exc:  # pragma: no cover - defensive around injected fetchers.
            self.fetch_error_count += 1
            self.last_error = str(exc)
            return cached.quote if cached is not None else None
        if quote is None:
            return cached.quote if cached is not None else None
        self._quote_cache[spec] = _QuoteCacheEntry(quote=quote, fetched_at=now)
        return quote


def fetch_reference_quote(
    spec: ReferenceSpec,
    timeout_seconds: float,
) -> ReferenceQuote | None:
    if spec.source == "yahoo":
        return _fetch_yahoo_chart(spec.symbol, timeout_seconds)
    return None


def _fetch_yahoo_chart(symbol: str, timeout_seconds: float) -> ReferenceQuote | None:
    for range_value in ("10m", "5d"):
        quote = _fetch_yahoo_chart_range(symbol, timeout_seconds, range_value)
        if quote is not None:
            return quote
    return None


def _fetch_yahoo_chart_range(
    symbol: str,
    timeout_seconds: float,
    range_value: str,
) -> ReferenceQuote | None:
    encoded_symbol = parse.quote(symbol, safe="")
    query = parse.urlencode(
        {
            "range": range_value,
            "interval": "1m",
            "includePrePost": "true",
        }
    )
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}?{query}"
    req = request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "trident-pod-c-external-reference/0.1",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (TimeoutError, OSError, error.HTTPError, error.URLError, json.JSONDecodeError):
        return None
    result = _first_chart_result(payload)
    if result is None:
        return None
    points = _chart_points(result)
    if not points:
        return None
    time_ms, price = points[-1]
    previous_60s = _price_at_or_before(points, time_ms - 60_000)
    previous_300s = _price_at_or_before(points, time_ms - 300_000)
    return ReferenceQuote(
        source="yahoo",
        source_symbol=symbol,
        price=price,
        time_ms=time_ms,
        momentum_60s_bps=_momentum_bps(price, previous_60s),
        momentum_300s_bps=_momentum_bps(price, previous_300s),
    )


def _first_chart_result(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return None
    result = chart.get("result")
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    return first if isinstance(first, dict) else None


def _chart_points(result: dict[str, object]) -> list[tuple[int, float]]:
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, dict):
        return []
    quote_rows = indicators.get("quote")
    if not isinstance(quote_rows, list) or not quote_rows:
        return []
    quote = quote_rows[0] if isinstance(quote_rows[0], dict) else {}
    closes = quote.get("close")
    if not isinstance(closes, list):
        return []
    points: list[tuple[int, float]] = []
    for raw_ts, raw_close in zip(timestamps, closes):
        try:
            timestamp_ms = int(raw_ts) * 1000
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if timestamp_ms > 0 and close > 0:
            points.append((timestamp_ms, close))
    return points


def _parse_spec(raw: str) -> ReferenceSpec:
    value = str(raw).strip()
    if ":" in value:
        source, symbol = value.split(":", 1)
    else:
        source, symbol = "yahoo", value
    return ReferenceSpec(source=source.strip().lower(), symbol=symbol.strip())


def _select_reference_quotes(
    quotes: list[ReferenceQuote],
    *,
    max_source_deviation_bps: float,
) -> tuple[list[ReferenceQuote], float]:
    if not quotes:
        return [], 0.0
    anchor = _median(quote.price for quote in quotes)
    kept: list[ReferenceQuote] = []
    max_deviation = 0.0
    for quote in quotes:
        deviation = abs(quote.price - anchor) / anchor * 10_000.0 if anchor > 0 else 0.0
        max_deviation = max(max_deviation, deviation)
        if deviation <= max_source_deviation_bps:
            kept.append(quote)
    return kept, round(max_deviation, 4)


def _median(values: object) -> float:
    items = [float(value) for value in values]
    if not items:
        return 0.0
    return float(statistics.median(items))


def _price_at_or_before(points: list[tuple[int, float]], timestamp_ms: int) -> float | None:
    previous: float | None = None
    for point_time_ms, price in points:
        if point_time_ms > timestamp_ms:
            break
        previous = price
    return previous


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


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _timestamp_ms(value: str) -> int:
    if value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _iso_from_ms(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
