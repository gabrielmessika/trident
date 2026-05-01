from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from urllib import error, parse, request

from app.trident.hip4_outcome.config import Hip4OutcomeConfig


@dataclass(slots=True)
class ReferencePriceQuote:
    source: str
    symbol: str
    price: float
    raw: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ReferencePrice:
    underlying: str
    price: float
    source_count: int
    quotes: list[ReferencePriceQuote] = field(default_factory=list)
    rejected_quotes: list[ReferencePriceQuote] = field(default_factory=list)
    max_deviation_bps: float = 0.0

    def to_metadata(self) -> dict[str, object]:
        return {
            "reference_price": self.price,
            "reference_source_count": self.source_count,
            "reference_sources": [quote.to_dict() for quote in self.quotes],
            "reference_rejected_sources": [
                quote.to_dict() for quote in self.rejected_quotes
            ],
            "reference_max_deviation_bps": self.max_deviation_bps,
        }


class ExternalPriceAggregator:
    def __init__(self, config: Hip4OutcomeConfig) -> None:
        self.config = config

    def fetch_many(
        self,
        underlyings: list[str],
        *,
        hyperliquid_mids: dict[str, float],
    ) -> dict[str, ReferencePrice]:
        result: dict[str, ReferencePrice] = {}
        for underlying in dict.fromkeys(item.upper() for item in underlyings if item):
            reference = self.fetch_one(underlying, hyperliquid_mids=hyperliquid_mids)
            if reference is not None:
                result[underlying] = reference
        return result

    def fetch_one(
        self,
        underlying: str,
        *,
        hyperliquid_mids: dict[str, float],
    ) -> ReferencePrice | None:
        quotes: list[ReferencePriceQuote] = []
        sources = self.config.reference_price_sources_by_underlying.get(
            underlying.upper(),
            self.config.reference_price_sources,
        )
        for source in sources:
            normalized = source.strip().lower()
            quote: ReferencePriceQuote | None = None
            if normalized == "binance":
                quote = self._fetch_binance(underlying)
            elif normalized == "okx":
                quote = self._fetch_okx(underlying)
            elif normalized == "bybit":
                quote = self._fetch_bybit(underlying)
            elif normalized == "coinbase":
                quote = self._fetch_coinbase(underlying)
            elif normalized == "kraken":
                quote = self._fetch_kraken(underlying)
            elif normalized == "hyperliquid":
                price = hyperliquid_mids.get(underlying.upper())
                if price is not None and price > 0:
                    quote = ReferencePriceQuote(
                        source="hyperliquid",
                        symbol=underlying.upper(),
                        price=price,
                    )
            if quote is not None and quote.price > 0:
                quotes.append(quote)
        if len(quotes) < max(self.config.min_reference_sources, 1):
            return None
        return self._select_reference(underlying, quotes)

    def _select_reference(
        self,
        underlying: str,
        quotes: list[ReferencePriceQuote],
    ) -> ReferencePrice | None:
        median = statistics.median([quote.price for quote in quotes])
        if median <= 0:
            return None
        kept: list[ReferencePriceQuote] = []
        rejected: list[ReferencePriceQuote] = []
        max_deviation = 0.0
        for quote in quotes:
            deviation = abs(quote.price - median) / median * 10_000.0
            max_deviation = max(max_deviation, deviation)
            if deviation <= self.config.max_source_deviation_bps:
                kept.append(quote)
            else:
                rejected.append(quote)
        if len(kept) < max(self.config.min_reference_sources, 1):
            return None
        price = float(statistics.median([quote.price for quote in kept]))
        return ReferencePrice(
            underlying=underlying.upper(),
            price=price,
            source_count=len(kept),
            quotes=kept,
            rejected_quotes=rejected,
            max_deviation_bps=round(max_deviation, 4),
        )

    def _fetch_binance(self, underlying: str) -> ReferencePriceQuote | None:
        symbol = f"{underlying.upper()}USDT"
        url = "https://api.binance.com/api/v3/ticker/price?" + parse.urlencode(
            {"symbol": symbol}
        )
        payload = self._get_json(url)
        if not isinstance(payload, dict):
            return None
        try:
            price = float(payload.get("price"))
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return ReferencePriceQuote(
            source="binance",
            symbol=str(payload.get("symbol", symbol)),
            price=price,
            raw=dict(payload),
        )

    def _fetch_okx(self, underlying: str) -> ReferencePriceQuote | None:
        for suffix in ("USDT", "USDT-SWAP"):
            inst_id = f"{underlying.upper()}-{suffix}"
            url = "https://www.okx.com/api/v5/market/ticker?" + parse.urlencode(
                {"instId": inst_id}
            )
            payload = self._get_json(url)
            if not isinstance(payload, dict):
                continue
            data = payload.get("data", [])
            if not isinstance(data, list) or not data:
                continue
            first = data[0] if isinstance(data[0], dict) else {}
            try:
                price = float(first.get("last"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            return ReferencePriceQuote(
                source="okx",
                symbol=str(first.get("instId", inst_id)),
                price=price,
                raw=dict(first),
            )
        return None

    def _fetch_bybit(self, underlying: str) -> ReferencePriceQuote | None:
        symbol = f"{underlying.upper()}USDT"
        for category in ("spot", "linear"):
            url = "https://api.bybit.com/v5/market/tickers?" + parse.urlencode(
                {"category": category, "symbol": symbol}
            )
            payload = self._get_json(url)
            if not isinstance(payload, dict):
                continue
            result = payload.get("result", {})
            if not isinstance(result, dict):
                continue
            items = result.get("list", [])
            if not isinstance(items, list) or not items:
                continue
            first = items[0] if isinstance(items[0], dict) else {}
            try:
                price = float(first.get("lastPrice"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            return ReferencePriceQuote(
                source="bybit",
                symbol=str(first.get("symbol", symbol)),
                price=price,
                raw=dict(first),
            )
        return None

    def _fetch_coinbase(self, underlying: str) -> ReferencePriceQuote | None:
        for quote in ("USD", "USDT"):
            product_id = f"{underlying.upper()}-{quote}"
            url = f"https://api.exchange.coinbase.com/products/{parse.quote(product_id)}/ticker"
            payload = self._get_json(url)
            if not isinstance(payload, dict):
                continue
            try:
                price = float(payload.get("price"))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            return ReferencePriceQuote(
                source="coinbase",
                symbol=product_id,
                price=price,
                raw=dict(payload),
            )
        return None

    def _fetch_kraken(self, underlying: str) -> ReferencePriceQuote | None:
        base = "XBT" if underlying.upper() == "BTC" else underlying.upper()
        for quote in ("USD", "USDT"):
            pair = f"{base}{quote}"
            url = "https://api.kraken.com/0/public/Ticker?" + parse.urlencode(
                {"pair": pair}
            )
            payload = self._get_json(url)
            if not isinstance(payload, dict):
                continue
            errors = payload.get("error", [])
            if isinstance(errors, list) and errors:
                continue
            result = payload.get("result", {})
            if not isinstance(result, dict) or not result:
                continue
            first_key = next(iter(result))
            first = result.get(first_key, {})
            if not isinstance(first, dict):
                continue
            close = first.get("c", [])
            try:
                price = float(close[0] if isinstance(close, list) and close else None)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            return ReferencePriceQuote(
                source="kraken",
                symbol=str(first_key),
                price=price,
                raw=dict(first),
            )
        return None

    def _get_json(self, url: str) -> object | None:
        req = request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self.config.external_price_user_agent,
            },
        )
        try:
            with request.urlopen(
                req,
                timeout=self.config.external_price_timeout_seconds,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, OSError, error.HTTPError, error.URLError, json.JSONDecodeError):
            return None
