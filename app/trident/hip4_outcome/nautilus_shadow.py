from __future__ import annotations

import csv
import asyncio
import importlib
import importlib.metadata
import json
import os
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.trident.hip4_outcome.book import depth_usdc_within_slippage, parse_side_book
from app.trident.hip4_outcome.client import HIP4OutcomeInfoClient
from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import BookLevel, OutcomeMarket, outcome_coin
from app.trident.hip4_outcome.parser import parse_outcome_markets


INSTRUMENT_FIELDS = [
    "ts",
    "instrument_id",
    "raw_symbol",
    "product_type",
    "underlying",
    "expiry",
    "quote_currency",
    "tick_size",
    "lot_size",
    "source",
]
BOOK_FIELDS = [
    "ts_event",
    "ts_init",
    "coin",
    "instrument_id",
    "market_id",
    "side_name",
    "best_bid",
    "best_ask",
    "bid_size",
    "ask_size",
    "bid_depth_10",
    "ask_depth_10",
    "spread",
    "source_latency_ms",
]
QUALITY_FIELDS = [
    "ts",
    "market_id",
    "underlying",
    "yes_coin",
    "no_coin",
    "yes_book_age_ms",
    "no_book_age_ms",
    "max_book_age_ms",
    "book_pair_skew_ms",
    "book_update_count_5s",
    "book_update_count_15s",
    "unique_book_count_5s",
    "unique_book_count_15s",
    "reference_age_ms",
    "reference_divergence_bps",
    "empty_book",
    "crossed_book",
    "quality_score",
    "tradable_window",
    "quality_reasons",
]
PARITY_FIELDS = [
    "ts",
    "market_id",
    "coin",
    "trident_bid",
    "trident_ask",
    "nautilus_bid",
    "nautilus_ask",
    "bid_diff",
    "ask_diff",
    "trident_age_ms",
    "nautilus_age_ms",
    "verdict",
]


@dataclass(frozen=True, slots=True)
class NautilusShadowConfig:
    enabled: bool = False
    mode: str = "shadow"
    environment: str = "mainnet"
    logs_dir: str = "./logs/hip4_nautilus_shadow"
    state_path: str = "./runtime/hip4_nautilus_shadow_state.json"
    loop_interval_seconds: float = 1.0
    max_markets: int = 4
    include_underlyings: tuple[str, ...] = ("BTC", "ETH", "SOL", "HYPE")
    include_outcome_products: bool = True
    include_hip3_products: bool = False
    subscribe_all_mids: bool = True
    subscribe_order_books: bool = True
    book_depth_levels: int = 10
    warmup_seconds: float = 8.0
    stagger_subscriptions_ms: int = 1000
    max_ws_connects_per_minute: int = 6
    write_shadow_books: bool = True
    write_shadow_quality: bool = True
    write_shadow_instruments: bool = True
    allow_orders: bool = False
    allow_private_user_stream: bool = False

    @property
    def status_path(self) -> str:
        return str(Path(self.logs_dir) / "status.json")


@dataclass(frozen=True, slots=True)
class NautilusBookSnapshot:
    instrument_id: str
    coin: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    ts_event_ns: int
    ts_init_ns: int
    received_ns: int
    update_count: int = 0
    is_snapshot: bool = True

    @property
    def bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def bid_size(self) -> float:
        return self.bids[0].size if self.bids else 0.0

    @property
    def ask_size(self) -> float:
        return self.asks[0].size if self.asks else 0.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return round(float(self.ask) - float(self.bid), 8)

    def age_ms(self, now_ns: int) -> int:
        if self.ts_event_ns <= 0:
            return 999_999_999
        return max(int((now_ns - self.ts_event_ns) / 1_000_000), 0)

    def latency_ms(self) -> float:
        if self.ts_event_ns <= 0 or self.ts_init_ns <= 0:
            return 0.0
        return round(max((self.ts_init_ns - self.ts_event_ns) / 1_000_000, 0.0), 3)

    def bid_depth_usdc(self, *, max_slippage: float) -> float:
        return depth_usdc_within_slippage(
            list(self.bids),
            is_ask=False,
            slippage=max_slippage,
        )

    def ask_depth_usdc(self, *, max_slippage: float) -> float:
        return depth_usdc_within_slippage(
            list(self.asks),
            is_ask=True,
            slippage=max_slippage,
        )


NautilusBookSource = Callable[
    [list[OutcomeMarket], NautilusShadowConfig, Hip4OutcomeConfig],
    tuple[dict[str, NautilusBookSnapshot], dict[str, Any]],
]


def load_nautilus_shadow_config(
    path: str | Path | None = None,
    *,
    apply_env: bool = True,
) -> NautilusShadowConfig:
    config_path = Path(
        path
        or (
            os.getenv("HIP4_NAUTILUS_SHADOW_CONFIG")
            if apply_env
            else None
        )
        or "config/hip4_nautilus_shadow.toml"
    )
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            loaded = tomllib.load(handle)
        if isinstance(loaded, dict):
            data = loaded
    section = data.get("hip4_nautilus_shadow", {})
    if not isinstance(section, dict):
        section = {}

    def value(name: str, default: object) -> object:
        return section.get(name, default)

    enabled = _bool(value("enabled", False))
    logs_dir = str(value("logs_dir", "./logs/hip4_nautilus_shadow"))
    state_path = str(value("state_path", "./runtime/hip4_nautilus_shadow_state.json"))
    max_markets = _int(value("max_markets", 4), 4)
    if apply_env:
        enabled = _env_bool("HIP4_NAUTILUS_SHADOW_ENABLED", enabled)
        logs_dir = os.getenv("HIP4_NAUTILUS_SHADOW_LOGS_DIR", logs_dir)
        state_path = os.getenv("HIP4_NAUTILUS_SHADOW_STATE_PATH", state_path)
        max_markets = _env_int("HIP4_NAUTILUS_SHADOW_MAX_MARKETS", max_markets)

    return NautilusShadowConfig(
        enabled=enabled,
        mode=str(value("mode", "shadow")),
        environment=str(value("environment", "mainnet")),
        logs_dir=logs_dir,
        state_path=state_path,
        loop_interval_seconds=_float(value("loop_interval_seconds", 1), 1.0),
        max_markets=max(max_markets, 0),
        include_underlyings=tuple(
            str(item).strip().upper()
            for item in value("include_underlyings", ["BTC", "ETH", "SOL", "HYPE"])
            if str(item).strip()
        ),
        include_outcome_products=_bool(value("include_outcome_products", True)),
        include_hip3_products=_bool(value("include_hip3_products", False)),
        subscribe_all_mids=_bool(value("subscribe_all_mids", True)),
        subscribe_order_books=_bool(value("subscribe_order_books", True)),
        book_depth_levels=_int(value("book_depth_levels", 10), 10),
        warmup_seconds=_float(value("warmup_seconds", 8), 8.0),
        stagger_subscriptions_ms=_int(value("stagger_subscriptions_ms", 1000), 1000),
        max_ws_connects_per_minute=_int(value("max_ws_connects_per_minute", 6), 6),
        write_shadow_books=_bool(value("write_shadow_books", True)),
        write_shadow_quality=_bool(value("write_shadow_quality", True)),
        write_shadow_instruments=_bool(value("write_shadow_instruments", True)),
        allow_orders=_bool(value("allow_orders", False)),
        allow_private_user_stream=_bool(value("allow_private_user_stream", False)),
    )


def outcome_coin_to_nautilus_instrument_id(coin: str) -> str:
    normalized = str(coin).strip()
    if not normalized:
        raise ValueError("empty HIP-4 outcome coin")
    if normalized.startswith("#") or normalized.startswith("+"):
        encoding = normalized[1:]
    else:
        encoding = normalized
    if not encoding.isdigit():
        raise ValueError(f"invalid HIP-4 outcome coin: {coin}")
    return f"+{encoding}.HYPERLIQUID"


def nautilus_instrument_id_to_outcome_coin(instrument_id: object) -> str:
    raw = str(instrument_id).strip()
    if raw.endswith(".HYPERLIQUID"):
        raw = raw[: -len(".HYPERLIQUID")]
    if raw.startswith("+") or raw.startswith("#"):
        raw = raw[1:]
    if not raw.isdigit():
        raise ValueError(f"invalid Nautilus HIP-4 instrument_id: {instrument_id}")
    return f"#{raw}"


def probe_nautilus_capabilities() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available": False,
        "version": None,
        "outcome_supported": False,
        "modules": {},
        "module_errors": {},
        "product_types": [],
        "error": None,
    }
    try:
        payload["version"] = importlib.metadata.version("nautilus-trader")
    except importlib.metadata.PackageNotFoundError:
        try:
            payload["version"] = importlib.metadata.version("nautilus_trader")
        except importlib.metadata.PackageNotFoundError:
            payload["error"] = "nautilus_trader package is not installed"
            return payload

    modules = {
        "adapters.hyperliquid": "nautilus_trader.adapters.hyperliquid",
        "adapters.hyperliquid.enums": "nautilus_trader.adapters.hyperliquid.enums",
        "model.identifiers": "nautilus_trader.model.identifiers",
    }
    imported: dict[str, bool] = {}
    module_errors: dict[str, str] = {}
    imported_modules: dict[str, Any] = {}
    for label, module_name in modules.items():
        try:
            imported_modules[label] = importlib.import_module(module_name)
            imported[label] = True
        except Exception as exc:
            imported[label] = False
            module_errors[label] = f"{type(exc).__name__}: {exc}"
    payload["modules"] = imported
    payload["module_errors"] = module_errors
    payload["available"] = all(imported.values())

    product_type = None
    for module in imported_modules.values():
        candidate = getattr(module, "HyperliquidProductType", None)
        if candidate is not None:
            product_type = candidate
            break
    if product_type is not None:
        members = [str(item).split(".")[-1] for item in product_type]
        payload["product_types"] = members
        payload["outcome_supported"] = "OUTCOME" in members

    if not payload["available"]:
        payload["error"] = "required Nautilus Hyperliquid modules are unavailable"
    elif not payload["outcome_supported"]:
        payload["error"] = "Nautilus Hyperliquid OUTCOME product type is unavailable"
    return payload


def collect_nautilus_shadow_once(
    config: NautilusShadowConfig,
    hip4_config: Hip4OutcomeConfig,
    *,
    info_client: HIP4OutcomeInfoClient | None = None,
    now_fn: Callable[[], str] | None = None,
    force: bool = False,
    capabilities: dict[str, Any] | None = None,
    nautilus_book_source: NautilusBookSource | None = None,
) -> dict[str, Any]:
    _assert_shadow_safe(config)
    logs_dir = Path(config.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    state_path = Path(config.state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if not config.enabled and not force:
        payload = _status_payload(
            config=config,
            ts=(now_fn or _utc_now_iso)(),
            shadow_ready=False,
            reason="disabled",
            capabilities=capabilities or probe_nautilus_capabilities(),
        )
        _write_status(config, payload)
        return payload

    nautilus = capabilities or probe_nautilus_capabilities()
    if not bool(nautilus.get("available")) or not bool(nautilus.get("outcome_supported")):
        payload = _status_payload(
            config=config,
            ts=(now_fn or _utc_now_iso)(),
            shadow_ready=False,
            reason=str(nautilus.get("error") or "nautilus_unavailable"),
            capabilities=nautilus,
        )
        _write_status(config, payload)
        return payload

    client = info_client or HIP4OutcomeInfoClient(hip4_config)
    ts = (now_fn or _utc_now_iso)()
    meta = client.fetch_outcome_meta()
    markets = parse_outcome_markets(
        meta,
        include_underlyings=list(config.include_underlyings),
    )[: config.max_markets]

    instrument_rows = _instrument_rows(markets, ts)
    book_rows: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    parity_rows: list[dict[str, Any]] = []
    last_update_by_coin: dict[str, str] = {}
    errors: list[str] = []
    nautilus_books: dict[str, NautilusBookSnapshot] | None = None
    nautilus_summary: dict[str, Any] = {}

    if markets and config.subscribe_order_books:
        try:
            source = nautilus_book_source or collect_nautilus_order_books
            nautilus_books, nautilus_summary = source(markets, config, hip4_config)
        except Exception as exc:
            errors.append(f"nautilus_order_books: {type(exc).__name__}: {exc}")
            nautilus_books = {}
        missing_coins = [
            coin
            for market in markets
            for coin in (market.yes_coin, market.no_coin)
            if coin not in (nautilus_books or {})
        ]
        if missing_coins:
            errors.append("nautilus_missing_books: " + ",".join(missing_coins[:12]))

    if config.write_shadow_books or config.write_shadow_quality:
        for market in markets:
            try:
                quality, books, parity = _collect_market_books(
                    market=market,
                    client=client,
                    ts=ts,
                    max_slippage=max(hip4_config.max_order_slippage, 0.0),
                    nautilus_books=nautilus_books,
                )
            except Exception as exc:
                errors.append(f"{market.market_id}: {type(exc).__name__}: {exc}")
                continue
            quality_rows.append(quality)
            book_rows.extend(books)
            parity_rows.extend(parity)
            for row in books:
                coin = str(row.get("coin", ""))
                if coin:
                    last_update_by_coin[coin] = str(row.get("ts_event", ts))

    if config.write_shadow_instruments:
        _append_jsonl_rows(logs_dir / "instruments.jsonl", instrument_rows)
    if config.write_shadow_books:
        _append_jsonl_rows(logs_dir / "book_snapshots.jsonl", book_rows)
        _append_csv_rows(logs_dir / "parity_compare.csv", PARITY_FIELDS, parity_rows)
    if config.write_shadow_quality:
        _append_csv_rows(logs_dir / "data_quality.csv", QUALITY_FIELDS, quality_rows)

    payload = _status_payload(
        config=config,
        ts=ts,
        shadow_ready=bool(markets) and not errors,
        reason="ok" if markets and not errors else "partial_or_no_markets",
        capabilities=nautilus,
    )
    payload.update(
        {
            "instrument_count": len(instrument_rows),
            "market_count": len(markets),
            "book_snapshot_count": len(book_rows),
            "data_quality_count": len(quality_rows),
            "parity_compare_count": len(parity_rows),
            "last_update_by_coin": last_update_by_coin,
            "errors": errors,
            "selected_markets": [market.market_id for market in markets],
            "nautilus_book_source": nautilus_summary,
        }
    )
    _write_status(config, payload)
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def collect_nautilus_order_books(
    markets: list[OutcomeMarket],
    config: NautilusShadowConfig,
    hip4_config: Hip4OutcomeConfig,
) -> tuple[dict[str, NautilusBookSnapshot], dict[str, Any]]:
    return asyncio.run(_collect_nautilus_order_books_async(markets, config, hip4_config))


async def _collect_nautilus_order_books_async(
    markets: list[OutcomeMarket],
    config: NautilusShadowConfig,
    hip4_config: Hip4OutcomeConfig,
) -> tuple[dict[str, NautilusBookSnapshot], dict[str, Any]]:
    from nautilus_trader.adapters.hyperliquid.enums import HyperliquidProductType
    from nautilus_trader.adapters.hyperliquid.providers import HyperliquidInstrumentProvider
    from nautilus_trader.core import nautilus_pyo3
    from nautilus_trader.model.data import capsule_to_data

    environment = nautilus_pyo3.HyperliquidEnvironment.from_str(config.environment)
    http_client = nautilus_pyo3.HyperliquidHttpClient(
        environment=environment,
        timeout_secs=max(int(hip4_config.request_timeout_seconds), 1),
    )
    provider = HyperliquidInstrumentProvider(
        http_client,
        product_types=[HyperliquidProductType.OUTCOME],
    )
    await provider.load_all_async(filters={"market_types": ["outcome"]})
    loaded_instruments = provider.list_all()
    loaded_ids = {str(instrument.id): instrument for instrument in loaded_instruments}

    target_ids = {
        outcome_coin_to_nautilus_instrument_id(coin): coin
        for market in markets
        for coin in (market.yes_coin, market.no_coin)
    }
    missing_instruments = sorted(
        instrument_id for instrument_id in target_ids if instrument_id not in loaded_ids
    )
    subscribed_ids = [instrument_id for instrument_id in target_ids if instrument_id in loaded_ids]
    snapshots_by_coin: dict[str, NautilusBookSnapshot] = {}
    message_counts: Counter[str] = Counter()
    callback_errors: list[str] = []

    def handle_message(message: object) -> None:
        try:
            data = capsule_to_data(message) if nautilus_pyo3.is_pycapsule(message) else message
            instrument_id = str(getattr(data, "instrument_id", ""))
            if instrument_id not in target_ids or not hasattr(data, "deltas"):
                return
            message_counts[instrument_id] += 1
            snapshot = _snapshot_from_nautilus_deltas(
                data,
                depth_levels=max(config.book_depth_levels, 1),
                update_count=message_counts[instrument_id],
            )
            snapshots_by_coin[target_ids[instrument_id]] = snapshot
        except Exception as exc:  # pragma: no cover - depends on Rust callback payloads
            callback_errors.append(f"{type(exc).__name__}: {exc}")

    ws_client = nautilus_pyo3.HyperliquidWebSocketClient(
        url=hip4_config.ws_url or None,
        environment=environment,
    )
    await ws_client.connect(
        asyncio.get_running_loop(),
        provider.instruments_pyo3(),
        handle_message,
    )
    try:
        for instrument_id in subscribed_ids:
            pyo3_instrument_id = nautilus_pyo3.InstrumentId.from_str(instrument_id)
            await ws_client.subscribe_book(pyo3_instrument_id)
            stagger = max(config.stagger_subscriptions_ms, 0) / 1000.0
            if stagger:
                await asyncio.sleep(stagger)
        warmup = max(float(config.warmup_seconds), 0.1)
        await asyncio.sleep(warmup)
    finally:
        await ws_client.close()

    summary = {
        "source": "nautilus_hyperliquid_ws",
        "environment": config.environment,
        "ws_url": getattr(ws_client, "url", None),
        "loaded_instrument_count": len(loaded_instruments),
        "target_instrument_count": len(target_ids),
        "subscribed_instrument_count": len(subscribed_ids),
        "snapshot_count": len(snapshots_by_coin),
        "message_counts": dict(message_counts),
        "missing_instruments": missing_instruments,
        "callback_errors": callback_errors[:20],
    }
    return snapshots_by_coin, summary


def _snapshot_from_nautilus_deltas(
    data: object,
    *,
    depth_levels: int,
    update_count: int,
) -> NautilusBookSnapshot:
    instrument_id = str(getattr(data, "instrument_id", ""))
    coin = nautilus_instrument_id_to_outcome_coin(instrument_id)
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}

    for delta in getattr(data, "deltas", []):
        action = _enum_name(getattr(delta, "action", ""))
        order = getattr(delta, "order", None)
        if action == "CLEAR":
            bids.clear()
            asks.clear()
            continue
        if order is None:
            continue
        side = _enum_name(getattr(order, "side", ""))
        price = _float(getattr(order, "price", None), 0.0)
        size = _float(getattr(order, "size", None), 0.0)
        if price <= 0:
            continue
        if action in {"DELETE", "REMOVE"} or size <= 0:
            if side == "BUY":
                bids.pop(price, None)
            elif side == "SELL":
                asks.pop(price, None)
            continue
        if action not in {"ADD", "UPDATE"}:
            continue
        if side == "BUY":
            bids[price] = bids.get(price, 0.0) + size
        elif side == "SELL":
            asks[price] = asks.get(price, 0.0) + size

    bid_levels = tuple(
        BookLevel(price=price, size=size)
        for price, size in sorted(bids.items(), reverse=True)[:depth_levels]
    )
    ask_levels = tuple(
        BookLevel(price=price, size=size)
        for price, size in sorted(asks.items())[:depth_levels]
    )
    return NautilusBookSnapshot(
        instrument_id=instrument_id,
        coin=coin,
        bids=bid_levels,
        asks=ask_levels,
        ts_event_ns=_int(getattr(data, "ts_event", 0), 0),
        ts_init_ns=_int(getattr(data, "ts_init", 0), 0),
        received_ns=time.time_ns(),
        update_count=update_count,
        is_snapshot=bool(getattr(data, "is_snapshot", True)),
    )


def _collect_market_books(
    *,
    market: OutcomeMarket,
    client: HIP4OutcomeInfoClient,
    ts: str,
    max_slippage: float,
    nautilus_books: dict[str, NautilusBookSnapshot] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    yes_started = time.monotonic()
    yes_payload = client.fetch_l2_book(market.yes_coin)
    yes_latency_ms = round((time.monotonic() - yes_started) * 1000.0, 3)
    no_started = time.monotonic()
    no_payload = client.fetch_l2_book(market.no_coin)
    no_latency_ms = round((time.monotonic() - no_started) * 1000.0, 3)
    yes = parse_side_book(yes_payload, max_slippage=max_slippage)
    no = parse_side_book(no_payload, max_slippage=max_slippage)
    now_ns = time.time_ns()
    now_ms = int(now_ns / 1_000_000)
    nautilus_yes = (nautilus_books or {}).get(market.yes_coin)
    nautilus_no = (nautilus_books or {}).get(market.no_coin)
    use_nautilus = nautilus_books is not None
    yes_age_ms = (
        nautilus_yes.age_ms(now_ns)
        if use_nautilus and nautilus_yes is not None
        else _book_age(now_ms, yes.time_ms)
    )
    no_age_ms = (
        nautilus_no.age_ms(now_ns)
        if use_nautilus and nautilus_no is not None
        else _book_age(now_ms, no.time_ms)
    )
    max_age = max(yes_age_ms, no_age_ms)
    skew = abs(yes_age_ms - no_age_ms)
    empty_book = (
        not _nautilus_book_is_usable(nautilus_yes)
        or not _nautilus_book_is_usable(nautilus_no)
        if use_nautilus
        else not _book_is_usable(yes) or not _book_is_usable(no)
    )
    crossed = (
        _nautilus_book_is_crossed(nautilus_yes) or _nautilus_book_is_crossed(nautilus_no)
        if use_nautilus
        else _book_is_crossed(yes) or _book_is_crossed(no)
    )
    reference_age = max(_book_age(now_ms, yes.time_ms), _book_age(now_ms, no.time_ms))
    divergence = _market_reference_divergence_bps(
        [(yes, nautilus_yes), (no, nautilus_no)]
    ) if use_nautilus else None
    score, reasons = _quality_score(
        yes_age_ms=yes_age_ms,
        no_age_ms=no_age_ms,
        empty_book=empty_book,
        crossed=crossed,
        source_missing=use_nautilus and (nautilus_yes is None or nautilus_no is None),
        reference_divergence_bps=divergence,
    )
    tradable = score >= 0.60 and not empty_book and not crossed
    quality = {
        "ts": ts,
        "market_id": market.market_id,
        "underlying": market.underlying,
        "yes_coin": market.yes_coin,
        "no_coin": market.no_coin,
        "yes_book_age_ms": yes_age_ms,
        "no_book_age_ms": no_age_ms,
        "max_book_age_ms": max_age,
        "book_pair_skew_ms": skew,
        "book_update_count_5s": _book_update_count(nautilus_yes, nautilus_no) if use_nautilus else "",
        "book_update_count_15s": _book_update_count(nautilus_yes, nautilus_no) if use_nautilus else "",
        "unique_book_count_5s": _unique_book_count(nautilus_yes, nautilus_no) if use_nautilus else "",
        "unique_book_count_15s": _unique_book_count(nautilus_yes, nautilus_no) if use_nautilus else "",
        "reference_age_ms": reference_age if use_nautilus else "",
        "reference_divergence_bps": "" if divergence is None else divergence,
        "empty_book": str(empty_book).lower(),
        "crossed_book": str(crossed).lower(),
        "quality_score": score,
        "tradable_window": str(tradable).lower(),
        "quality_reasons": ";".join(reasons),
    }
    if use_nautilus:
        books = [
            _nautilus_book_row(
                ts=ts,
                market=market,
                side_name="YES",
                coin=market.yes_coin,
                snapshot=nautilus_yes,
                max_slippage=max_slippage,
            ),
            _nautilus_book_row(
                ts=ts,
                market=market,
                side_name="NO",
                coin=market.no_coin,
                snapshot=nautilus_no,
                max_slippage=max_slippage,
            ),
        ]
    else:
        books = [
            _book_row(
                ts=ts,
                market=market,
                side_name="YES",
                coin=market.yes_coin,
                side_book=yes,
                source_latency_ms=yes_latency_ms,
            ),
            _book_row(
                ts=ts,
                market=market,
                side_name="NO",
                coin=market.no_coin,
                side_book=no,
                source_latency_ms=no_latency_ms,
            ),
        ]
    parity = [
        _parity_row(
            ts=ts,
            market=market,
            coin=market.yes_coin,
            trident_book=yes,
            trident_latency_ms=yes_latency_ms,
            nautilus_snapshot=nautilus_yes,
            now_ns=now_ns,
        )
    ]
    parity.append(
        _parity_row(
            ts=ts,
            market=market,
            coin=market.no_coin,
            trident_book=no,
            trident_latency_ms=no_latency_ms,
            nautilus_snapshot=nautilus_no,
            now_ns=now_ns,
        )
    )
    return quality, books, parity


def _instrument_rows(markets: list[OutcomeMarket], ts: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market in markets:
        for side in (0, 1):
            raw_symbol = outcome_coin(market.outcome, side)
            rows.append(
                {
                    "ts": ts,
                    "instrument_id": outcome_coin_to_nautilus_instrument_id(raw_symbol),
                    "raw_symbol": raw_symbol,
                    "product_type": "OUTCOME",
                    "underlying": market.underlying,
                    "expiry": market.expiry_iso,
                    "quote_currency": "USDH",
                    "tick_size": "0.0001",
                    "lot_size": "0.01",
                    "source": "nautilus_hyperliquid_symbol_shadow",
                }
            )
    return rows


def _book_row(
    *,
    ts: str,
    market: OutcomeMarket,
    side_name: str,
    coin: str,
    side_book: Any,
    source_latency_ms: float,
) -> dict[str, Any]:
    return {
        "ts_event": ts,
        "ts_init": ts,
        "coin": coin,
        "instrument_id": outcome_coin_to_nautilus_instrument_id(coin),
        "market_id": market.market_id,
        "side_name": side_name,
        "best_bid": _empty_if_none(side_book.bid),
        "best_ask": _empty_if_none(side_book.ask),
        "bid_size": side_book.bid_size,
        "ask_size": side_book.ask_size,
        "bid_depth_10": side_book.bid_depth_usdc,
        "ask_depth_10": side_book.ask_depth_usdc,
        "spread": _empty_if_none(side_book.spread),
        "source_latency_ms": source_latency_ms,
    }


def _nautilus_book_row(
    *,
    ts: str,
    market: OutcomeMarket,
    side_name: str,
    coin: str,
    snapshot: NautilusBookSnapshot | None,
    max_slippage: float,
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "ts_event": ts,
            "ts_init": ts,
            "coin": coin,
            "instrument_id": outcome_coin_to_nautilus_instrument_id(coin),
            "market_id": market.market_id,
            "side_name": side_name,
            "best_bid": "",
            "best_ask": "",
            "bid_size": 0.0,
            "ask_size": 0.0,
            "bid_depth_10": 0.0,
            "ask_depth_10": 0.0,
            "spread": "",
            "source_latency_ms": "",
            "source": "nautilus_hyperliquid_ws_missing",
        }
    return {
        "ts_event": _ns_to_iso(snapshot.ts_event_ns),
        "ts_init": _ns_to_iso(snapshot.ts_init_ns),
        "coin": coin,
        "instrument_id": snapshot.instrument_id,
        "market_id": market.market_id,
        "side_name": side_name,
        "best_bid": _empty_if_none(snapshot.bid),
        "best_ask": _empty_if_none(snapshot.ask),
        "bid_size": snapshot.bid_size,
        "ask_size": snapshot.ask_size,
        "bid_depth_10": snapshot.bid_depth_usdc(max_slippage=max_slippage),
        "ask_depth_10": snapshot.ask_depth_usdc(max_slippage=max_slippage),
        "spread": _empty_if_none(snapshot.spread),
        "source_latency_ms": snapshot.latency_ms(),
        "source": "nautilus_hyperliquid_ws",
        "update_count": snapshot.update_count,
        "is_snapshot": snapshot.is_snapshot,
    }


def _parity_row(
    *,
    ts: str,
    market: OutcomeMarket,
    coin: str,
    trident_book: Any,
    trident_latency_ms: float,
    nautilus_snapshot: NautilusBookSnapshot | None,
    now_ns: int,
) -> dict[str, Any]:
    trident_bid = trident_book.bid
    trident_ask = trident_book.ask
    nautilus_bid = nautilus_snapshot.bid if nautilus_snapshot is not None else None
    nautilus_ask = nautilus_snapshot.ask if nautilus_snapshot is not None else None
    bid_diff = _diff_or_empty(trident_bid, nautilus_bid)
    ask_diff = _diff_or_empty(trident_ask, nautilus_ask)
    trident_age = _book_age(int(now_ns / 1_000_000), trident_book.time_ms)
    nautilus_age = nautilus_snapshot.age_ms(now_ns) if nautilus_snapshot is not None else ""
    return {
        "ts": ts,
        "market_id": market.market_id,
        "coin": coin,
        "trident_bid": _empty_if_none(trident_bid),
        "trident_ask": _empty_if_none(trident_ask),
        "nautilus_bid": _empty_if_none(nautilus_bid),
        "nautilus_ask": _empty_if_none(nautilus_ask),
        "bid_diff": bid_diff,
        "ask_diff": ask_diff,
        "trident_age_ms": trident_age,
        "nautilus_age_ms": nautilus_age,
        "verdict": _parity_verdict(
            trident_bid=trident_bid,
            trident_ask=trident_ask,
            nautilus_bid=nautilus_bid,
            nautilus_ask=nautilus_ask,
        ),
        "trident_latency_ms": trident_latency_ms,
        "nautilus_latency_ms": (
            nautilus_snapshot.latency_ms() if nautilus_snapshot is not None else ""
        ),
    }


def _status_payload(
    *,
    config: NautilusShadowConfig,
    ts: str,
    shadow_ready: bool,
    reason: str,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    return {
        "pod": "hip4_nautilus_shadow",
        "mode": config.mode,
        "environment": config.environment,
        "read_only": True,
        "enabled": config.enabled,
        "shadow_ready": shadow_ready,
        "reason": reason,
        "updated_at": ts,
        "logs_dir": config.logs_dir,
        "state_path": config.state_path,
        "status_path": config.status_path,
        "allow_orders": config.allow_orders,
        "allow_private_user_stream": config.allow_private_user_stream,
        "nautilus": capabilities,
        "operator_note": "shadow/read-only; no orders, no HIP-4 active state writes",
    }


def _assert_shadow_safe(config: NautilusShadowConfig) -> None:
    if config.mode != "shadow":
        raise ValueError("Nautilus HIP-4 adapter only supports mode=shadow")
    if config.allow_orders:
        raise ValueError("Nautilus shadow must keep allow_orders=false")
    if config.allow_private_user_stream:
        raise ValueError("Nautilus shadow must keep allow_private_user_stream=false")


def _write_status(config: NautilusShadowConfig, payload: dict[str, Any]) -> None:
    path = Path(config.status_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _append_jsonl_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _book_age(now_ms: int, book_time_ms: int) -> int:
    if book_time_ms <= 0:
        return 999_999_999
    return max(now_ms - int(book_time_ms), 0)


def _book_is_usable(side_book: Any) -> bool:
    return side_book.bid is not None and side_book.ask is not None


def _book_is_crossed(side_book: Any) -> bool:
    return (
        side_book.bid is not None
        and side_book.ask is not None
        and float(side_book.bid) > float(side_book.ask)
    )


def _nautilus_book_is_usable(snapshot: NautilusBookSnapshot | None) -> bool:
    return snapshot is not None and snapshot.bid is not None and snapshot.ask is not None


def _nautilus_book_is_crossed(snapshot: NautilusBookSnapshot | None) -> bool:
    return (
        snapshot is not None
        and snapshot.bid is not None
        and snapshot.ask is not None
        and float(snapshot.bid) > float(snapshot.ask)
    )


def _book_update_count(
    yes: NautilusBookSnapshot | None,
    no: NautilusBookSnapshot | None,
) -> int:
    return int((yes.update_count if yes else 0) + (no.update_count if no else 0))


def _unique_book_count(
    yes: NautilusBookSnapshot | None,
    no: NautilusBookSnapshot | None,
) -> int:
    return int(bool(yes)) + int(bool(no))


def _market_reference_divergence_bps(pairs: list[tuple[Any, NautilusBookSnapshot | None]]) -> float | None:
    values: list[float] = []
    for trident_book, nautilus_snapshot in pairs:
        if nautilus_snapshot is None:
            continue
        for left, right in (
            (getattr(trident_book, "bid", None), nautilus_snapshot.bid),
            (getattr(trident_book, "ask", None), nautilus_snapshot.ask),
        ):
            diff = _diff_bps(left, right)
            if diff is not None:
                values.append(diff)
    if not values:
        return None
    return round(max(values), 4)


def _diff_or_empty(left: object, right: object) -> object:
    if left is None or right is None or left == "" or right == "":
        return ""
    try:
        return round(float(right) - float(left), 8)
    except (TypeError, ValueError):
        return ""


def _diff_bps(left: object, right: object) -> float | None:
    if left is None or right is None or left == "" or right == "":
        return None
    try:
        left_f = float(left)
        right_f = float(right)
    except (TypeError, ValueError):
        return None
    denominator = max((abs(left_f) + abs(right_f)) / 2.0, 1e-9)
    return abs(right_f - left_f) / denominator * 10_000.0


def _parity_verdict(
    *,
    trident_bid: object,
    trident_ask: object,
    nautilus_bid: object,
    nautilus_ask: object,
) -> str:
    if nautilus_bid is None or nautilus_ask is None:
        return "missing_nautilus_book"
    if trident_bid is None or trident_ask is None:
        return "missing_trident_book"
    bid_bps = _diff_bps(trident_bid, nautilus_bid)
    ask_bps = _diff_bps(trident_ask, nautilus_ask)
    values = [value for value in (bid_bps, ask_bps) if value is not None]
    if not values:
        return "invalid_price_compare"
    max_bps = max(values)
    if max_bps <= 1:
        return "match_lt_1bp"
    if max_bps <= 10:
        return "match_lt_10bps"
    if max_bps <= 50:
        return "diverged_lt_50bps"
    return "diverged_gt_50bps"


def _quality_score(
    *,
    yes_age_ms: int,
    no_age_ms: int,
    empty_book: bool,
    crossed: bool,
    source_missing: bool = False,
    reference_divergence_bps: float | None = None,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 1.0
    max_age = max(yes_age_ms, no_age_ms)
    skew = abs(yes_age_ms - no_age_ms)
    if source_missing:
        score -= 0.70
        reasons.append("nautilus_missing_book")
    if empty_book:
        score -= 0.60
        reasons.append("empty_book")
    if crossed:
        score -= 0.50
        reasons.append("crossed_book")
    if max_age > 3_000:
        score -= 0.25
        reasons.append("stale_book_gt_3000ms")
    elif max_age > 1_000:
        score -= 0.10
        reasons.append("book_age_gt_1000ms")
    if skew > 1_000:
        score -= 0.20
        reasons.append("pair_skew_gt_1000ms")
    elif skew > 250:
        score -= 0.08
        reasons.append("pair_skew_gt_250ms")
    if reference_divergence_bps is not None:
        if reference_divergence_bps > 50:
            score -= 0.20
            reasons.append("reference_divergence_gt_50bps")
        elif reference_divergence_bps > 10:
            score -= 0.08
            reasons.append("reference_divergence_gt_10bps")
    if not reasons:
        reasons.append("ok")
    return round(max(min(score, 1.0), 0.0), 4), reasons


def _enum_name(value: object) -> str:
    name = getattr(value, "name", None)
    if name:
        return str(name).upper()
    return str(value).split(".")[-1].upper()


def _empty_if_none(value: object) -> object:
    return "" if value is None else value


def _ns_to_iso(value: int) -> str:
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else _bool(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else _int(raw, default)


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
