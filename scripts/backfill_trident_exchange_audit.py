#!/usr/bin/env python3
"""Backfill TRIDENT A/C exchange fills and funding into audit CSVs.

The script is read-only against Hyperliquid. It enriches fetched append-only
`trade_close` journals with `userFillsByTime` and `userFunding` data.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.hyperliquid.private_state import HyperliquidCredentials, sdk_base_url_from_info_url
from app.hyperliquid.symbols import normalize_hl_symbol
from app.settings import load_config


ROOT = Path(__file__).resolve().parents[1]
MATCH_GRACE_BEFORE_MS = 60_000
MATCH_GRACE_AFTER_MS = 10 * 60_000
DEFAULT_START = "2026-05-24T00:00:00Z"


@dataclass(slots=True)
class TradeCloseRecord:
    pod: str
    source_file: str
    source_line: int
    event_ts: str | None
    symbol: str
    side: str
    setup: str | None
    close_reason: str | None
    opened_at: str | None
    closed_at: str | None
    opened_ms: int | None
    closed_ms: int | None
    entry_price: float | None
    exit_price: float | None
    target_notional_usd: float | None
    pnl_usd: float | None
    gross_pnl_usd: float | None
    fees_usd: float | None
    existing_close_fill_oids: set[int] = field(default_factory=set)
    raw_trade: dict[str, Any] = field(default_factory=dict)

    @property
    def normalized_symbol(self) -> str:
        return normalize_hl_symbol(self.symbol)

    @property
    def identity(self) -> str:
        return "|".join(
            [
                self.pod,
                self.symbol,
                self.side,
                str(self.opened_at or ""),
                str(self.closed_at or self.event_ts or ""),
                str(self.close_reason or ""),
            ]
        )

    @property
    def expected_size(self) -> Decimal | None:
        if not self.target_notional_usd or not self.entry_price or self.entry_price <= 0:
            return None
        return _decimal(self.target_notional_usd) / _decimal(self.entry_price)


@dataclass(slots=True)
class ExchangeFillRow:
    symbol: str
    oid: int | None
    side: str
    direction: str
    size: Decimal
    price: float
    closed_pnl_usd: float
    fee_usd: float
    timestamp_ms: int
    hash: str | None
    raw: dict[str, Any]

    @property
    def normalized_symbol(self) -> str:
        return normalize_hl_symbol(self.symbol)

    @property
    def identity(self) -> tuple[str, int | None, int, str, str, str]:
        return (
            self.normalized_symbol,
            self.oid,
            self.timestamp_ms,
            self.direction,
            str(self.size),
            self.hash or "",
        )

    @property
    def notional_usd(self) -> float:
        return round(float(self.size) * self.price, 8)


@dataclass(slots=True)
class FundingPaymentRow:
    symbol: str
    amount_usd: float
    funding_rate: float | None
    size: Decimal
    timestamp_ms: int
    hash: str | None
    raw: dict[str, Any]

    @property
    def normalized_symbol(self) -> str:
        return normalize_hl_symbol(self.symbol)

    @property
    def identity(self) -> tuple[str, int, str]:
        return (self.normalized_symbol, self.timestamp_ms, self.hash or str(self.amount_usd))


@dataclass(slots=True)
class TradeEnrichment:
    trade: TradeCloseRecord
    close_fills: list[ExchangeFillRow]
    funding_payments: list[FundingPaymentRow]
    match_source: str

    @property
    def exchange_fee_usd(self) -> float:
        return round(sum(fill.fee_usd for fill in self.close_fills), 8)

    @property
    def exchange_closed_pnl_usd(self) -> float:
        return round(sum(fill.closed_pnl_usd for fill in self.close_fills), 8)

    @property
    def funding_usd(self) -> float:
        return round(sum(payment.amount_usd for payment in self.funding_payments), 8)

    @property
    def exchange_net_pnl_usd(self) -> float | None:
        if not self.close_fills:
            return None
        return round(self.exchange_closed_pnl_usd - self.exchange_fee_usd + self.funding_usd, 8)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_time_ms(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                yield line_number, json.loads(text)
            except json.JSONDecodeError:
                continue


def load_trade_closes(source_root: Path) -> list[TradeCloseRecord]:
    sources = {
        "pod_a": source_root / "logs" / "pod_a_live.jsonl",
        "pod_c": source_root / "logs" / "pod_c_live.jsonl",
    }
    trades: list[TradeCloseRecord] = []
    seen: set[str] = set()
    for pod, path in sources.items():
        if not path.exists():
            continue
        source_file = str(path.relative_to(source_root))
        for line_number, record in read_jsonl(path):
            if record.get("event_type") != "trade_close":
                continue
            trade = record.get("trade") or {}
            if not isinstance(trade, dict):
                continue
            item = _trade_close_from_record(
                pod=pod,
                source_file=source_file,
                source_line=line_number,
                event_ts=record.get("timestamp"),
                trade=trade,
            )
            if not item.symbol:
                continue
            if item.identity in seen:
                continue
            seen.add(item.identity)
            trades.append(item)
    return sorted(
        trades,
        key=lambda trade: (
            trade.closed_ms if trade.closed_ms is not None else 0,
            trade.source_file,
            trade.source_line,
        ),
    )


def _trade_close_from_record(
    *,
    pod: str,
    source_file: str,
    source_line: int,
    event_ts: object,
    trade: dict[str, Any],
) -> TradeCloseRecord:
    opened_at = _str_or_none(trade.get("opened_at"))
    closed_at = _str_or_none(trade.get("closed_at") or event_ts)
    close_oids: set[int] = set()
    close_fills = trade.get("close_fills") or []
    if isinstance(close_fills, list):
        for fill in close_fills:
            if not isinstance(fill, dict):
                continue
            oid = _int_or_none(fill.get("oid"))
            if oid is not None:
                close_oids.add(oid)
    return TradeCloseRecord(
        pod=pod,
        source_file=source_file,
        source_line=source_line,
        event_ts=_str_or_none(event_ts),
        symbol=str(trade.get("symbol") or ""),
        side=str(trade.get("side") or ""),
        setup=_str_or_none(trade.get("setup") or trade.get("open_reason")),
        close_reason=_str_or_none(trade.get("close_reason")),
        opened_at=opened_at,
        closed_at=closed_at,
        opened_ms=parse_time_ms(opened_at),
        closed_ms=parse_time_ms(closed_at),
        entry_price=_float_or_none(trade.get("entry_price")),
        exit_price=_float_or_none(trade.get("exit_price")),
        target_notional_usd=_float_or_none(trade.get("target_notional_usd")),
        pnl_usd=_float_or_none(trade.get("pnl_usd")),
        gross_pnl_usd=_float_or_none(trade.get("gross_pnl_usd")),
        fees_usd=_float_or_none(trade.get("fees_usd")),
        existing_close_fill_oids=close_oids,
        raw_trade=trade,
    )


def _str_or_none(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def parse_exchange_fills(payload: list[object]) -> list[ExchangeFillRow]:
    fills: list[ExchangeFillRow] = []
    seen: set[tuple[str, int | None, int, str, str, str]] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = normalize_hl_symbol(str(item.get("coin", "")))
        if not symbol:
            continue
        side_raw = str(item.get("side", ""))
        fill = ExchangeFillRow(
            symbol=symbol,
            oid=_int_or_none(item.get("oid")),
            side="buy" if side_raw == "B" else "sell" if side_raw == "A" else side_raw.lower(),
            direction=str(item.get("dir", "")),
            size=_decimal(item.get("sz")),
            price=float(_decimal(item.get("px"))),
            closed_pnl_usd=float(_decimal(item.get("closedPnl"))),
            fee_usd=abs(float(_decimal(item.get("fee")))),
            timestamp_ms=int(_decimal(item.get("time"))),
            hash=_str_or_none(item.get("hash")),
            raw=dict(item),
        )
        if fill.identity in seen:
            continue
        seen.add(fill.identity)
        fills.append(fill)
    return sorted(fills, key=lambda fill: (fill.timestamp_ms, fill.normalized_symbol, fill.oid or 0))


def parse_funding_payments(payload: list[object]) -> list[FundingPaymentRow]:
    payments: list[FundingPaymentRow] = []
    seen: set[tuple[str, int, str]] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        delta = item.get("delta")
        details = delta if isinstance(delta, dict) else item
        symbol = normalize_hl_symbol(str(details.get("coin", item.get("coin", ""))))
        if not symbol:
            continue
        amount_raw = (
            details.get("usdc")
            if details.get("usdc") not in (None, "")
            else details.get("amount", details.get("funding"))
        )
        payment = FundingPaymentRow(
            symbol=symbol,
            amount_usd=float(_decimal(amount_raw)),
            funding_rate=(
                float(_decimal(details.get("fundingRate", details.get("funding_rate"))))
                if details.get("fundingRate", details.get("funding_rate")) not in (None, "")
                else None
            ),
            size=_decimal(details.get("szi", details.get("sz"))),
            timestamp_ms=int(_decimal(item.get("time", details.get("time")))),
            hash=_str_or_none(item.get("hash")),
            raw=dict(item),
        )
        if payment.identity in seen:
            continue
        seen.add(payment.identity)
        payments.append(payment)
    return sorted(payments, key=lambda payment: (payment.timestamp_ms, payment.normalized_symbol))


def fetch_exchange_history(
    *,
    config_path: Path,
    start_ms: int,
    end_ms: int,
    window_hours: int,
    sleep_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    config = load_config(config_path)
    credentials = HyperliquidCredentials.from_env()
    errors = credentials.validate_for_readonly()
    if errors:
        raise RuntimeError("; ".join(errors))
    try:
        from hyperliquid.info import Info
    except Exception as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("hyperliquid-python-sdk is required") from exc

    info = Info(
        sdk_base_url_from_info_url(config.hyperliquid.info_url),
        skip_ws=True,
        timeout=config.hyperliquid.connect_timeout_seconds,
    )
    fills: list[dict[str, Any]] = []
    funding: list[dict[str, Any]] = []
    for chunk_start, chunk_end in _time_windows(start_ms, end_ms, window_hours=window_hours):
        fills_payload = info.user_fills_by_time(
            credentials.account_address,
            chunk_start,
            chunk_end,
            aggregate_by_time=False,
        )
        if isinstance(fills_payload, list):
            fills.extend(item for item in fills_payload if isinstance(item, dict))
        funding_payload = info.user_funding_history(
            credentials.account_address,
            chunk_start,
            chunk_end,
        )
        if isinstance(funding_payload, list):
            funding.extend(item for item in funding_payload if isinstance(item, dict))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return fills, funding


def _time_windows(start_ms: int, end_ms: int, *, window_hours: int):
    step = max(int(window_hours), 1) * 3600_000
    current = start_ms
    while current <= end_ms:
        chunk_end = min(current + step - 1, end_ms)
        yield current, chunk_end
        current = chunk_end + 1


def enrich_trades(
    trades: list[TradeCloseRecord],
    fills: list[ExchangeFillRow],
    funding: list[FundingPaymentRow],
) -> tuple[list[TradeEnrichment], dict[tuple[str, int | None, int, str, str, str], TradeEnrichment]]:
    fills_by_symbol: dict[str, list[ExchangeFillRow]] = defaultdict(list)
    funding_by_symbol: dict[str, list[FundingPaymentRow]] = defaultdict(list)
    for fill in fills:
        fills_by_symbol[fill.normalized_symbol].append(fill)
    for payment in funding:
        funding_by_symbol[payment.normalized_symbol].append(payment)

    used_close_fills: set[tuple[str, int | None, int, str, str, str]] = set()
    matched_fill_owner: dict[tuple[str, int | None, int, str, str, str], TradeEnrichment] = {}
    enrichments: list[TradeEnrichment] = []

    for trade in trades:
        close_fills, match_source = _match_close_fills(
            trade,
            fills_by_symbol.get(trade.normalized_symbol, []),
            used_close_fills,
        )
        for fill in close_fills:
            used_close_fills.add(fill.identity)
        funding_payments = _funding_for_trade(
            trade,
            funding_by_symbol.get(trade.normalized_symbol, []),
        )
        enrichment = TradeEnrichment(
            trade=trade,
            close_fills=close_fills,
            funding_payments=funding_payments,
            match_source=match_source,
        )
        for fill in close_fills:
            matched_fill_owner[fill.identity] = enrichment
        enrichments.append(enrichment)
    return enrichments, matched_fill_owner


def _match_close_fills(
    trade: TradeCloseRecord,
    fills: list[ExchangeFillRow],
    used: set[tuple[str, int | None, int, str, str, str]],
) -> tuple[list[ExchangeFillRow], str]:
    if trade.existing_close_fill_oids:
        exact = [
            fill
            for fill in fills
            if fill.identity not in used
            and fill.oid in trade.existing_close_fill_oids
            and _looks_like_close_fill(fill, trade)
        ]
        if exact:
            return sorted(exact, key=lambda fill: fill.timestamp_ms), "journal_close_fill_oid"

    if trade.opened_ms is None or trade.closed_ms is None:
        return [], "missing_trade_timestamps"
    start_ms = max(trade.opened_ms - MATCH_GRACE_BEFORE_MS, 0)
    end_ms = trade.closed_ms + MATCH_GRACE_AFTER_MS
    candidates = [
        fill
        for fill in fills
        if fill.identity not in used
        and start_ms <= fill.timestamp_ms <= end_ms
        and _looks_like_close_fill(fill, trade)
    ]
    if not candidates:
        return [], "no_exchange_close_fill_match"
    candidates.sort(
        key=lambda fill: (
            abs(fill.timestamp_ms - trade.closed_ms),
            0 if "close" in fill.direction.lower() else 1,
            -abs(fill.closed_pnl_usd),
        )
    )
    expected = trade.expected_size
    if expected is None or expected <= 0:
        return [candidates[0]], "nearest_exchange_close_fill"

    selected: list[ExchangeFillRow] = []
    total = Decimal("0")
    for fill in candidates:
        selected.append(fill)
        total += abs(fill.size)
        if total >= expected * Decimal("0.80"):
            break
    return sorted(selected, key=lambda fill: fill.timestamp_ms), "exchange_time_window_size"


def _looks_like_close_fill(fill: ExchangeFillRow, trade: TradeCloseRecord) -> bool:
    direction = fill.direction.lower()
    if "close" in direction or abs(fill.closed_pnl_usd) > 0:
        return True
    side = trade.side.lower()
    if side == "long":
        return fill.side == "sell"
    if side == "short":
        return fill.side == "buy"
    return False


def _funding_for_trade(
    trade: TradeCloseRecord,
    payments: list[FundingPaymentRow],
) -> list[FundingPaymentRow]:
    if trade.opened_ms is None or trade.closed_ms is None:
        return []
    return [
        payment
        for payment in payments
        if trade.opened_ms <= payment.timestamp_ms <= trade.closed_ms
    ]


def write_outputs(
    *,
    output_dir: Path,
    source_root: Path,
    trades: list[TradeCloseRecord],
    fills: list[ExchangeFillRow],
    funding: list[FundingPaymentRow],
    enrichments: list[TradeEnrichment],
    matched_fill_owner: dict[tuple[str, int | None, int, str, str, str], TradeEnrichment],
    start_ms: int,
    end_ms: int,
    raw_fills_payload: list[dict[str, Any]],
    raw_funding_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "raw_user_fills.json", raw_fills_payload)
    write_json(output_dir / "raw_user_funding.json", raw_funding_payload)
    _write_exchange_fills_csv(output_dir / "trident_ac_exchange_fills.csv", fills, matched_fill_owner)
    _write_closed_trades_csv(output_dir / "trident_ac_closed_trades_full.csv", enrichments)
    _write_funding_csv(output_dir / "trident_ac_funding_payments.csv", funding, enrichments)
    summary = _build_summary(
        source_root=source_root,
        trades=trades,
        fills=fills,
        funding=funding,
        enrichments=enrichments,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    write_json(output_dir / "summary.json", summary)
    (output_dir / "summary.md").write_text(_render_summary_md(summary), encoding="utf-8")
    return summary


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_exchange_fills_csv(
    path: Path,
    fills: list[ExchangeFillRow],
    matched_fill_owner: dict[tuple[str, int | None, int, str, str, str], TradeEnrichment],
) -> None:
    fields = [
        "exchange_ts",
        "exchange_timestamp_ms",
        "symbol",
        "side",
        "direction",
        "oid",
        "price",
        "size",
        "notional_usd",
        "closed_pnl_usd",
        "fee_usd",
        "hash",
        "matched_pod",
        "matched_action",
        "matched_trade_id",
        "matched_close_reason",
        "matched_source",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for fill in fills:
            owner = matched_fill_owner.get(fill.identity)
            writer.writerow(
                {
                    "exchange_ts": iso_from_ms(fill.timestamp_ms),
                    "exchange_timestamp_ms": fill.timestamp_ms,
                    "symbol": fill.symbol,
                    "side": fill.side,
                    "direction": fill.direction,
                    "oid": fill.oid,
                    "price": fill.price,
                    "size": str(fill.size),
                    "notional_usd": fill.notional_usd,
                    "closed_pnl_usd": fill.closed_pnl_usd,
                    "fee_usd": fill.fee_usd,
                    "hash": fill.hash,
                    "matched_pod": owner.trade.pod if owner else "",
                    "matched_action": "close" if owner else "",
                    "matched_trade_id": owner.trade.identity if owner else "",
                    "matched_close_reason": owner.trade.close_reason if owner else "",
                    "matched_source": owner.match_source if owner else "",
                }
            )


def _write_closed_trades_csv(path: Path, enrichments: list[TradeEnrichment]) -> None:
    fields = [
        "pod",
        "symbol",
        "side",
        "setup",
        "close_reason",
        "opened_at",
        "closed_at",
        "event_ts",
        "entry_price",
        "exit_price",
        "target_notional_usd",
        "journal_pnl_usd",
        "journal_gross_pnl_usd",
        "journal_fees_usd",
        "exchange_close_fill_count",
        "exchange_close_fill_oids",
        "exchange_fee_usd",
        "exchange_closed_pnl_usd",
        "funding_usd",
        "funding_payment_count",
        "exchange_net_pnl_usd",
        "journal_vs_exchange_net_diff_usd",
        "match_source",
        "source_file",
        "source_line",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for enrichment in enrichments:
            trade = enrichment.trade
            exchange_net = enrichment.exchange_net_pnl_usd
            diff = None
            if exchange_net is not None and trade.pnl_usd is not None:
                diff = round(trade.pnl_usd - exchange_net, 8)
            writer.writerow(
                {
                    "pod": trade.pod,
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "setup": trade.setup,
                    "close_reason": trade.close_reason,
                    "opened_at": trade.opened_at,
                    "closed_at": trade.closed_at,
                    "event_ts": trade.event_ts,
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "target_notional_usd": trade.target_notional_usd,
                    "journal_pnl_usd": trade.pnl_usd,
                    "journal_gross_pnl_usd": trade.gross_pnl_usd,
                    "journal_fees_usd": trade.fees_usd,
                    "exchange_close_fill_count": len(enrichment.close_fills),
                    "exchange_close_fill_oids": ",".join(
                        str(fill.oid) for fill in enrichment.close_fills if fill.oid is not None
                    ),
                    "exchange_fee_usd": enrichment.exchange_fee_usd if enrichment.close_fills else "",
                    "exchange_closed_pnl_usd": (
                        enrichment.exchange_closed_pnl_usd if enrichment.close_fills else ""
                    ),
                    "funding_usd": enrichment.funding_usd,
                    "funding_payment_count": len(enrichment.funding_payments),
                    "exchange_net_pnl_usd": exchange_net if exchange_net is not None else "",
                    "journal_vs_exchange_net_diff_usd": diff if diff is not None else "",
                    "match_source": enrichment.match_source,
                    "source_file": trade.source_file,
                    "source_line": trade.source_line,
                }
            )


def _write_funding_csv(
    path: Path,
    funding: list[FundingPaymentRow],
    enrichments: list[TradeEnrichment],
) -> None:
    owner_by_payment: dict[tuple[str, int, str], list[TradeEnrichment]] = defaultdict(list)
    for enrichment in enrichments:
        for payment in enrichment.funding_payments:
            owner_by_payment[payment.identity].append(enrichment)
    fields = [
        "exchange_ts",
        "exchange_timestamp_ms",
        "symbol",
        "amount_usd",
        "funding_rate",
        "size",
        "hash",
        "matched_trade_count",
        "matched_trade_ids",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for payment in funding:
            owners = owner_by_payment.get(payment.identity, [])
            writer.writerow(
                {
                    "exchange_ts": iso_from_ms(payment.timestamp_ms),
                    "exchange_timestamp_ms": payment.timestamp_ms,
                    "symbol": payment.symbol,
                    "amount_usd": payment.amount_usd,
                    "funding_rate": payment.funding_rate,
                    "size": str(payment.size),
                    "hash": payment.hash,
                    "matched_trade_count": len(owners),
                    "matched_trade_ids": " || ".join(owner.trade.identity for owner in owners),
                }
            )


def _build_summary(
    *,
    source_root: Path,
    trades: list[TradeCloseRecord],
    fills: list[ExchangeFillRow],
    funding: list[FundingPaymentRow],
    enrichments: list[TradeEnrichment],
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    by_pod = Counter(enrichment.trade.pod for enrichment in enrichments)
    matched_by_pod = Counter(
        enrichment.trade.pod for enrichment in enrichments if enrichment.close_fills
    )
    fills_by_pod = Counter()
    journal_pnl = Counter()
    exchange_net = Counter()
    exchange_closed_pnl = Counter()
    exchange_fee = Counter()
    funding_sum = Counter()
    match_sources = Counter(enrichment.match_source for enrichment in enrichments)
    unmatched_examples: list[dict[str, Any]] = []
    for enrichment in enrichments:
        pod = enrichment.trade.pod
        fills_by_pod[pod] += len(enrichment.close_fills)
        if enrichment.trade.pnl_usd is not None:
            journal_pnl[pod] += enrichment.trade.pnl_usd
        if enrichment.exchange_net_pnl_usd is not None:
            exchange_net[pod] += enrichment.exchange_net_pnl_usd
        exchange_closed_pnl[pod] += enrichment.exchange_closed_pnl_usd
        exchange_fee[pod] += enrichment.exchange_fee_usd
        funding_sum[pod] += enrichment.funding_usd
        if not enrichment.close_fills and len(unmatched_examples) < 12:
            unmatched_examples.append(
                {
                    "pod": pod,
                    "symbol": enrichment.trade.symbol,
                    "opened_at": enrichment.trade.opened_at,
                    "closed_at": enrichment.trade.closed_at,
                    "close_reason": enrichment.trade.close_reason,
                    "journal_pnl_usd": enrichment.trade.pnl_usd,
                    "source_file": enrichment.trade.source_file,
                    "source_line": enrichment.trade.source_line,
                    "match_source": enrichment.match_source,
                }
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_root": str(source_root),
        "window_start": iso_from_ms(start_ms),
        "window_end": iso_from_ms(end_ms),
        "trade_close_rows": len(trades),
        "trade_close_rows_by_pod": dict(by_pod),
        "exchange_user_fill_rows": len(fills),
        "exchange_funding_payment_rows": len(funding),
        "matched_trade_rows": sum(1 for enrichment in enrichments if enrichment.close_fills),
        "matched_trade_rows_by_pod": dict(matched_by_pod),
        "exchange_close_fill_count_by_pod": dict(fills_by_pod),
        "match_sources": match_sources.most_common(),
        "journal_pnl_usd_by_pod": {key: round(value, 8) for key, value in journal_pnl.items()},
        "exchange_closed_pnl_usd_by_pod": {
            key: round(value, 8) for key, value in exchange_closed_pnl.items()
        },
        "exchange_fee_usd_by_pod": {key: round(value, 8) for key, value in exchange_fee.items()},
        "funding_usd_by_pod": {key: round(value, 8) for key, value in funding_sum.items()},
        "exchange_net_pnl_usd_by_pod": {
            key: round(value, 8) for key, value in exchange_net.items()
        },
        "unmatched_trade_examples": unmatched_examples,
    }


def _render_summary_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Backfill exchange TRIDENT A/C",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- window: `{summary['window_start']}` -> `{summary['window_end']}`",
        f"- trade_close_rows: `{summary['trade_close_rows']}`",
        f"- exchange_user_fill_rows: `{summary['exchange_user_fill_rows']}`",
        f"- exchange_funding_payment_rows: `{summary['exchange_funding_payment_rows']}`",
        f"- matched_trade_rows: `{summary['matched_trade_rows']}`",
        "",
        "## Par pod",
        "",
        "| Pod | Trades | Trades matches | Close fills | Journal PnL | Exchange closed PnL | Exchange fees | Funding | Exchange net |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    pods = sorted(set(summary.get("trade_close_rows_by_pod", {})) | set(summary.get("matched_trade_rows_by_pod", {})))
    for pod in pods:
        lines.append(
            "| {pod} | {trades} | {matched} | {fills} | {journal} | {closed} | {fees} | {funding} | {net} |".format(
                pod=pod,
                trades=summary.get("trade_close_rows_by_pod", {}).get(pod, 0),
                matched=summary.get("matched_trade_rows_by_pod", {}).get(pod, 0),
                fills=summary.get("exchange_close_fill_count_by_pod", {}).get(pod, 0),
                journal=summary.get("journal_pnl_usd_by_pod", {}).get(pod, 0),
                closed=summary.get("exchange_closed_pnl_usd_by_pod", {}).get(pod, 0),
                fees=summary.get("exchange_fee_usd_by_pod", {}).get(pod, 0),
                funding=summary.get("funding_usd_by_pod", {}).get(pod, 0),
                net=summary.get("exchange_net_pnl_usd_by_pod", {}).get(pod, 0),
            )
        )
    lines.extend(["", "## Match sources", ""])
    for source, count in summary.get("match_sources", []):
        lines.append(f"- `{source}`: `{count}`")
    if summary.get("unmatched_trade_examples"):
        lines.extend(["", "## Exemples non matches", ""])
        for item in summary["unmatched_trade_examples"]:
            lines.append(
                "- `{pod}` `{symbol}` closed `{closed_at}` reason `{reason}` pnl `{pnl}` line `{line}`".format(
                    pod=item.get("pod"),
                    symbol=item.get("symbol"),
                    closed_at=item.get("closed_at"),
                    reason=item.get("close_reason"),
                    pnl=item.get("journal_pnl_usd"),
                    line=item.get("source_line"),
                )
            )
    lines.append("")
    return "\n".join(lines)


def load_payload(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [item for item in payload if isinstance(item, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(ROOT / "server-data"))
    parser.add_argument("--output", default=None)
    parser.add_argument("--config", default=str(ROOT / "config" / "trident.toml"))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--sleep-seconds", type=float, default=0.15)
    parser.add_argument("--fills-json", default=None, help="Use an existing raw user fills JSON file")
    parser.add_argument("--funding-json", default=None, help="Use an existing raw user funding JSON file")
    args = parser.parse_args()

    source_root = Path(args.source).resolve()
    output_dir = (
        Path(args.output).resolve()
        if args.output
        else source_root / "audit_backfills" / utc_stamp()
    )
    start_ms = parse_time_ms(args.start)
    if start_ms is None:
        raise SystemExit(f"Invalid --start: {args.start}")
    end_ms = parse_time_ms(args.end) if args.end else int(time.time() * 1000)
    if end_ms is None:
        raise SystemExit(f"Invalid --end: {args.end}")

    raw_fills = load_payload(Path(args.fills_json)) if args.fills_json else []
    raw_funding = load_payload(Path(args.funding_json)) if args.funding_json else []
    if not args.fills_json and not args.funding_json:
        raw_fills, raw_funding = fetch_exchange_history(
            config_path=Path(args.config),
            start_ms=start_ms,
            end_ms=end_ms,
            window_hours=args.window_hours,
            sleep_seconds=args.sleep_seconds,
        )

    trades = load_trade_closes(source_root)
    fills = parse_exchange_fills(raw_fills)
    funding = parse_funding_payments(raw_funding)
    enrichments, matched_fill_owner = enrich_trades(trades, fills, funding)
    summary = write_outputs(
        output_dir=output_dir,
        source_root=source_root,
        trades=trades,
        fills=fills,
        funding=funding,
        enrichments=enrichments,
        matched_fill_owner=matched_fill_owner,
        start_ms=start_ms,
        end_ms=end_ms,
        raw_fills_payload=raw_fills,
        raw_funding_payload=raw_funding,
    )
    print(output_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
