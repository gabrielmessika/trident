#!/usr/bin/env python3
"""Audit live Pod A P1-07 order-block shadow fields.

Research-only. Reads local fetched logs/snapshots, measures the observation-only
order-block shadow, and estimates long-veto / defensive-short candidates with a
simple fixed-horizon proxy. It never changes live config and never places
orders.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_START = "2026-06-14T21:15:00Z"


@dataclass(frozen=True, slots=True)
class OrderBlockEvent:
    event_type: str
    timestamp: datetime
    timestamp_text: str
    symbol: str
    side: str
    setup: str
    status: str
    source: str
    price: float | None
    regime_gate: str
    bullish_order_blocks: str
    bearish_order_blocks: str
    has_bullish_order_block: bool
    has_bearish_order_block: bool
    would_block_long: bool
    would_open_defensive_short: bool
    live_action_unchanged: bool


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    timestamp: datetime
    symbol: str
    side: str
    setup: str
    pnl_usd: float
    close_reason: str
    opened_at: str
    closed_at: str
    regime_gate: str
    has_bearish_order_block: bool
    would_block_long: bool
    would_open_defensive_short: bool
    live_action_unchanged: bool


@dataclass(frozen=True, slots=True)
class ForwardProxy:
    kind: str
    event_type: str
    timestamp: str
    symbol: str
    side: str
    setup: str
    regime_gate: str
    entry_price: float
    exit_timestamp: str
    exit_price: float
    gross_return_bps: float
    net_return_bps: float
    net_pnl_usd: float
    decision_value_usd: float


class PriceIndex:
    def __init__(self) -> None:
        self._times: dict[str, list[datetime]] = defaultdict(list)
        self._prices: dict[str, list[float]] = defaultdict(list)

    def add(self, symbol: str, timestamp: datetime, price: float) -> None:
        key = symbol.upper()
        self._times[key].append(timestamp)
        self._prices[key].append(price)

    def sort(self) -> None:
        for symbol, times in list(self._times.items()):
            prices = self._prices[symbol]
            pairs = sorted(zip(times, prices), key=lambda item: item[0])
            self._times[symbol] = [item[0] for item in pairs]
            self._prices[symbol] = [item[1] for item in pairs]

    def at_or_after(self, symbol: str, timestamp: datetime) -> tuple[datetime, float] | None:
        key = symbol.upper()
        times = self._times.get(key) or []
        prices = self._prices.get(key) or []
        index = bisect.bisect_left(times, timestamp)
        if index >= len(times):
            return None
        return times[index], prices[index]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-a-log", default="server-data/logs/pod_a_live.jsonl")
    parser.add_argument("--snapshots-dir", default="server-data/live_snapshots")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default="")
    parser.add_argument("--horizon-minutes", type=int, default=180)
    parser.add_argument("--cost-bps", type=float, default=16.0)
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--dedupe-minutes", type=int, default=180)
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start = parse_timestamp(args.start)
    end = parse_timestamp(args.end) if args.end else None
    if start is None:
        raise ValueError("--start must be a valid UTC timestamp")
    if end is not None and end <= start:
        raise ValueError("--end must be after --start")
    stamp = utc_stamp()
    output_dir = Path(
        args.output_dir or f"server-data/replay_reports/p107_order_block_shadow_audit_{stamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    events, trades = load_order_block_events(Path(args.pod_a_log), start=start, end=end)
    price_index = load_price_index(Path(args.snapshots_dir), start=start, end=end)
    long_veto_events = [event for event in events if event.would_block_long]
    defensive_short_events = [event for event in events if event.would_open_defensive_short]
    cooldown = timedelta(minutes=max(args.dedupe_minutes, 0))
    long_veto_raw = forward_proxies(
        long_veto_events,
        kind="long_veto",
        price_index=price_index,
        horizon=timedelta(minutes=max(args.horizon_minutes, 1)),
        cost_bps=float(args.cost_bps),
        notional_usd=float(args.notional_usd),
    )
    long_veto_deduped = forward_proxies(
        dedupe_candidates(long_veto_events, cooldown=cooldown),
        kind="long_veto",
        price_index=price_index,
        horizon=timedelta(minutes=max(args.horizon_minutes, 1)),
        cost_bps=float(args.cost_bps),
        notional_usd=float(args.notional_usd),
    )
    defensive_short_raw = forward_proxies(
        defensive_short_events,
        kind="defensive_short",
        price_index=price_index,
        horizon=timedelta(minutes=max(args.horizon_minutes, 1)),
        cost_bps=float(args.cost_bps),
        notional_usd=float(args.notional_usd),
    )
    defensive_short_deduped = forward_proxies(
        dedupe_candidates(defensive_short_events, cooldown=cooldown),
        kind="defensive_short",
        price_index=price_index,
        horizon=timedelta(minutes=max(args.horizon_minutes, 1)),
        cost_bps=float(args.cost_bps),
        notional_usd=float(args.notional_usd),
    )

    payload = {
        "generated_at": stamp,
        "status": "research_only_no_live_change",
        "source": {
            "pod_a_log": args.pod_a_log,
            "snapshots_dir": args.snapshots_dir,
            "start": isoformat(start),
            "end": isoformat(end) if end else None,
        },
        "parameters": {
            "horizon_minutes": args.horizon_minutes,
            "cost_bps": args.cost_bps,
            "notional_usd": args.notional_usd,
            "dedupe_minutes": args.dedupe_minutes,
        },
        "shadow_summary": shadow_summary(events),
        "closed_trade_summary": closed_trade_summary(trades),
        "long_veto_proxy_raw": proxy_summary(
            long_veto_raw,
            notional_usd=float(args.notional_usd),
        ),
        "long_veto_proxy_deduped": proxy_summary(
            long_veto_deduped,
            notional_usd=float(args.notional_usd),
        ),
        "defensive_short_proxy_raw": proxy_summary(
            defensive_short_raw,
            notional_usd=float(args.notional_usd),
        ),
        "defensive_short_proxy_deduped": proxy_summary(
            defensive_short_deduped,
            notional_usd=float(args.notional_usd),
        ),
        "top_long_veto_symbols_deduped": top_symbol_summary(
            long_veto_deduped,
            notional_usd=float(args.notional_usd),
        ),
        "top_defensive_short_symbols_deduped": top_symbol_summary(
            defensive_short_deduped,
            notional_usd=float(args.notional_usd),
        ),
    }
    (output_dir / "p107_order_block_shadow_audit.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_proxy_csv(output_dir / "long_veto_forward_returns_raw.csv", long_veto_raw)
    write_proxy_csv(output_dir / "long_veto_forward_returns_deduped.csv", long_veto_deduped)
    write_proxy_csv(output_dir / "defensive_short_forward_returns_raw.csv", defensive_short_raw)
    write_proxy_csv(output_dir / "defensive_short_forward_returns_deduped.csv", defensive_short_deduped)
    write_report(output_dir / "p107_order_block_shadow_audit.md", payload=payload)
    print(output_dir)


def load_order_block_events(
    path: Path,
    *,
    start: datetime,
    end: datetime | None,
) -> tuple[list[OrderBlockEvent], list[ClosedTrade]]:
    events: list[OrderBlockEvent] = []
    trades: list[ClosedTrade] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            event_type = str(record.get("event_type") or "")
            if event_type == "signal":
                event = event_from_signal(record)
                if event and in_window(event.timestamp, start, end):
                    events.append(event)
            elif event_type == "signal_review":
                event = event_from_review(record)
                if event and in_window(event.timestamp, start, end):
                    events.append(event)
            elif event_type == "trade_close":
                trade = trade_from_record(record)
                if trade and in_window(trade.timestamp, start, end):
                    trades.append(trade)
    return events, trades


def event_from_signal(record: dict[str, Any]) -> OrderBlockEvent | None:
    signal = record.get("signal") or {}
    if not isinstance(signal, dict):
        return None
    details = signal.get("order_block_shadow") or signal.get("setup_details") or {}
    if not isinstance(details, dict):
        return None
    timestamp = parse_timestamp(record.get("timestamp") or record.get("event_ts"))
    if timestamp is None:
        return None
    symbol_snapshot = record.get("symbol_snapshot") or {}
    price = optional_float(symbol_snapshot.get("price")) if isinstance(symbol_snapshot, dict) else None
    risk = signal.get("risk") or {}
    status = str(risk.get("accepted")) if isinstance(risk, dict) else ""
    return build_event(
        event_type="signal",
        timestamp=timestamp,
        symbol=str(signal.get("symbol") or ""),
        side=str(signal.get("side") or ""),
        setup=str(signal.get("setup") or ""),
        status=status,
        source=str(record.get("source") or ""),
        price=price,
        details=details,
    )


def event_from_review(record: dict[str, Any]) -> OrderBlockEvent | None:
    review = record.get("review") or {}
    if not isinstance(review, dict):
        return None
    details = review.get("order_block_shadow") or {}
    if not isinstance(details, dict):
        return None
    timestamp = parse_timestamp(record.get("timestamp") or record.get("event_ts"))
    if timestamp is None:
        return None
    symbol_snapshot = record.get("symbol_snapshot") or {}
    price = optional_float(symbol_snapshot.get("price")) if isinstance(symbol_snapshot, dict) else None
    setups = review.get("candidate_setups") or []
    setup = ",".join(str(item) for item in setups) if isinstance(setups, list) else str(setups or "")
    return build_event(
        event_type="signal_review",
        timestamp=timestamp,
        symbol=str(review.get("symbol") or ""),
        side=str(review.get("preferred_side") or ""),
        setup=setup,
        status=str(review.get("status") or ""),
        source=str(record.get("source") or ""),
        price=price,
        details=details,
    )


def build_event(
    *,
    event_type: str,
    timestamp: datetime,
    symbol: str,
    side: str,
    setup: str,
    status: str,
    source: str,
    price: float | None,
    details: dict[str, Any],
) -> OrderBlockEvent | None:
    if details.get("order_block_shadow_mode") != "observation_only":
        return None
    return OrderBlockEvent(
        event_type=event_type,
        timestamp=timestamp,
        timestamp_text=isoformat(timestamp),
        symbol=symbol.upper(),
        side=side.lower(),
        setup=setup,
        status=status,
        source=source,
        price=price,
        regime_gate=str(details.get("regime_gate_decision") or ""),
        bullish_order_blocks=str(details.get("bullish_order_blocks_1h4h") or ""),
        bearish_order_blocks=str(details.get("bearish_order_blocks_1h4h") or ""),
        has_bullish_order_block=details.get("has_bullish_order_block_1h4h") is True,
        has_bearish_order_block=details.get("has_bearish_order_block_1h4h") is True,
        would_block_long=details.get("would_block_long_order_block_shadow") is True,
        would_open_defensive_short=details.get("would_open_defensive_short_order_block_shadow") is True,
        live_action_unchanged=details.get("live_action_unchanged") is True,
    )


def trade_from_record(record: dict[str, Any]) -> ClosedTrade | None:
    trade = record.get("trade") or {}
    if not isinstance(trade, dict):
        return None
    details = trade.get("setup_details") or {}
    if not isinstance(details, dict) or details.get("order_block_shadow_mode") != "observation_only":
        return None
    timestamp = parse_timestamp(record.get("timestamp") or trade.get("closed_at"))
    if timestamp is None:
        return None
    return ClosedTrade(
        timestamp=timestamp,
        symbol=str(trade.get("symbol") or "").upper(),
        side=str(trade.get("side") or "").lower(),
        setup=str(trade.get("setup") or ""),
        pnl_usd=float(optional_float(trade.get("pnl_usd")) or 0.0),
        close_reason=str(trade.get("close_reason") or ""),
        opened_at=str(trade.get("opened_at") or ""),
        closed_at=str(trade.get("closed_at") or ""),
        regime_gate=str(details.get("regime_gate_decision") or ""),
        has_bearish_order_block=details.get("has_bearish_order_block_1h4h") is True,
        would_block_long=details.get("would_block_long_order_block_shadow") is True,
        would_open_defensive_short=details.get("would_open_defensive_short_order_block_shadow") is True,
        live_action_unchanged=details.get("live_action_unchanged") is True,
    )


def load_price_index(
    snapshots_dir: Path,
    *,
    start: datetime,
    end: datetime | None,
) -> PriceIndex:
    index = PriceIndex()
    horizon_padding = timedelta(hours=8)
    for path in sorted(snapshots_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                timestamp = parse_timestamp(record.get("timestamp"))
                if timestamp is None:
                    continue
                if timestamp < start - horizon_padding:
                    continue
                if end is not None and timestamp > end + horizon_padding:
                    continue
                for item in record.get("symbols") or []:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol") or "").upper()
                    price = optional_float(item.get("price"))
                    if symbol and price and price > 0:
                        index.add(symbol, timestamp, price)
    index.sort()
    return index


def dedupe_candidates(
    candidates: Iterable[OrderBlockEvent],
    *,
    cooldown: timedelta,
) -> list[OrderBlockEvent]:
    if cooldown.total_seconds() <= 0:
        return sorted(candidates, key=lambda item: item.timestamp)
    selected: list[OrderBlockEvent] = []
    last_by_key: dict[tuple[str, str], datetime] = {}
    for event in sorted(candidates, key=lambda item: item.timestamp):
        key = (event.symbol, "short" if event.would_open_defensive_short else "long_veto")
        last = last_by_key.get(key)
        if last is not None and event.timestamp < last + cooldown:
            continue
        selected.append(event)
        last_by_key[key] = event.timestamp
    return selected


def forward_proxies(
    candidates: Iterable[OrderBlockEvent],
    *,
    kind: str,
    price_index: PriceIndex,
    horizon: timedelta,
    cost_bps: float,
    notional_usd: float,
) -> list[ForwardProxy]:
    rows: list[ForwardProxy] = []
    for event in candidates:
        entry = event.price
        if entry is None or entry <= 0.0:
            indexed_entry = price_index.at_or_after(event.symbol, event.timestamp)
            if indexed_entry is None:
                continue
            _, entry = indexed_entry
        exit_point = price_index.at_or_after(event.symbol, event.timestamp + horizon)
        if exit_point is None:
            continue
        exit_timestamp, exit_price = exit_point
        if kind == "defensive_short":
            gross_bps = ((entry - exit_price) / entry) * 10_000.0
            net_bps = gross_bps - cost_bps
            net_pnl = notional_usd * net_bps / 10_000.0
            decision_value = net_pnl
        else:
            gross_bps = ((exit_price - entry) / entry) * 10_000.0
            net_bps = gross_bps - cost_bps
            net_pnl = notional_usd * net_bps / 10_000.0
            decision_value = -net_pnl
        rows.append(
            ForwardProxy(
                kind=kind,
                event_type=event.event_type,
                timestamp=event.timestamp_text,
                symbol=event.symbol,
                side=event.side,
                setup=event.setup,
                regime_gate=event.regime_gate,
                entry_price=round(entry, 10),
                exit_timestamp=isoformat(exit_timestamp),
                exit_price=round(exit_price, 10),
                gross_return_bps=round(gross_bps, 6),
                net_return_bps=round(net_bps, 6),
                net_pnl_usd=round(net_pnl, 6),
                decision_value_usd=round(decision_value, 6),
            )
        )
    return rows


def shadow_summary(events: list[OrderBlockEvent]) -> dict[str, Any]:
    by_event = Counter(event.event_type for event in events)
    by_gate = Counter(event.regime_gate for event in events)
    return {
        "records": len(events),
        "by_event_type": dict(by_event),
        "by_gate": dict(by_gate),
        "has_bullish_order_block": sum(1 for event in events if event.has_bullish_order_block),
        "has_bearish_order_block": sum(1 for event in events if event.has_bearish_order_block),
        "would_block_long_order_block_shadow": sum(1 for event in events if event.would_block_long),
        "would_open_defensive_short_order_block_shadow": sum(
            1 for event in events if event.would_open_defensive_short
        ),
        "live_action_unchanged_false": sum(
            1 for event in events if not event.live_action_unchanged
        ),
        "top_block_long_symbols": top_event_symbols(events, field="would_block_long"),
        "top_defensive_short_symbols": top_event_symbols(events, field="would_open_defensive_short"),
    }


def closed_trade_summary(trades: list[ClosedTrade]) -> dict[str, Any]:
    blocked = [trade for trade in trades if trade.would_block_long]
    defensive_short = [trade for trade in trades if trade.would_open_defensive_short]
    return {
        "closed_trades_with_shadow": len(trades),
        "total_pnl_usd": round(sum(trade.pnl_usd for trade in trades), 6),
        "would_block_long_trades": len(blocked),
        "would_block_long_pnl_usd": round(sum(trade.pnl_usd for trade in blocked), 6),
        "would_open_defensive_short_marked_trades": len(defensive_short),
        "would_open_defensive_short_marked_pnl_usd": round(
            sum(trade.pnl_usd for trade in defensive_short),
            6,
        ),
        "by_gate": {
            gate: {"count": len(rows), "pnl_usd": round(sum(row.pnl_usd for row in rows), 6)}
            for gate, rows in group_trades_by_gate(trades).items()
        },
        "by_symbol": top_trade_symbols(trades),
    }


def proxy_summary(rows: list[ForwardProxy], *, notional_usd: float) -> dict[str, Any]:
    positive = [row for row in rows if row.decision_value_usd > 0]
    negative = [row for row in rows if row.decision_value_usd <= 0]
    gross_positive = sum(row.decision_value_usd for row in positive)
    gross_negative = abs(sum(row.decision_value_usd for row in negative))
    return {
        "count": len(rows),
        "decision_value_usd": round(sum(row.decision_value_usd for row in rows), 6),
        "net_pnl_usd": round(sum(row.net_pnl_usd for row in rows), 6),
        "avg_decision_value_bps": round(
            sum((row.decision_value_usd / notional_usd) * 10_000.0 for row in rows) / len(rows),
            6,
        )
        if rows
        else None,
        "hit_rate": round(len(positive) / len(rows), 4) if rows else None,
        "profit_factor": round(gross_positive / gross_negative, 4) if gross_negative > 0 else None,
        "gross_positive_usd": round(gross_positive, 6),
        "gross_negative_usd": round(gross_negative, 6),
    }


def top_symbol_summary(
    rows: list[ForwardProxy],
    *,
    notional_usd: float,
    limit: int = 12,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[ForwardProxy]] = defaultdict(list)
    for row in rows:
        grouped[row.symbol].append(row)
    summary = []
    for symbol, symbol_rows in grouped.items():
        summary.append(
            {
                "symbol": symbol,
                "count": len(symbol_rows),
                "decision_value_usd": round(
                    sum(row.decision_value_usd for row in symbol_rows),
                    6,
                ),
                "avg_decision_value_bps": round(
                    sum(
                        (row.decision_value_usd / notional_usd) * 10_000.0
                        for row in symbol_rows
                    )
                    / len(symbol_rows),
                    6,
                ),
            }
        )
    return sorted(summary, key=lambda item: float(item["decision_value_usd"]), reverse=True)[:limit]


def top_event_symbols(events: list[OrderBlockEvent], *, field: str, limit: int = 12) -> list[tuple[str, int]]:
    counter = Counter()
    for event in events:
        if getattr(event, field):
            counter[event.symbol] += 1
    return counter.most_common(limit)


def top_trade_symbols(trades: list[ClosedTrade], limit: int = 12) -> list[dict[str, Any]]:
    grouped: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.symbol].append(trade)
    rows = []
    for symbol, symbol_trades in grouped.items():
        rows.append(
            {
                "symbol": symbol,
                "count": len(symbol_trades),
                "pnl_usd": round(sum(trade.pnl_usd for trade in symbol_trades), 6),
            }
        )
    return sorted(rows, key=lambda item: float(item["pnl_usd"]))[:limit]


def group_trades_by_gate(trades: list[ClosedTrade]) -> dict[str, list[ClosedTrade]]:
    grouped: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        grouped[trade.regime_gate or "unknown"].append(trade)
    return dict(grouped)


def write_proxy_csv(path: Path, rows: list[ForwardProxy]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(asdict(rows[0]).keys()) if rows else [
            "kind",
            "event_type",
            "timestamp",
            "symbol",
            "side",
            "setup",
            "regime_gate",
            "entry_price",
            "exit_timestamp",
            "exit_price",
            "gross_return_bps",
            "net_return_bps",
            "net_pnl_usd",
            "decision_value_usd",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_report(path: Path, *, payload: dict[str, Any]) -> None:
    shadow = payload["shadow_summary"]
    closed = payload["closed_trade_summary"]
    long_raw = payload["long_veto_proxy_raw"]
    long_deduped = payload["long_veto_proxy_deduped"]
    short_raw = payload["defensive_short_proxy_raw"]
    short_deduped = payload["defensive_short_proxy_deduped"]
    params = payload["parameters"]
    lines = [
        "# P1-07 order-block shadow audit",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        "- status: `research_only_no_live_change`",
        f"- source_start: `{payload['source']['start']}`",
        f"- source_end: `{payload['source']['end'] or 'latest local log'}`",
        f"- horizon: `{params['horizon_minutes']}m`, cost: `{params['cost_bps']} bps`, notional: `{params['notional_usd']} USD`, dedupe: `{params['dedupe_minutes']}m`",
        "",
        "## Shadow coverage",
        "",
        f"- Records with order-block shadow: `{shadow['records']}`",
        f"- By event type: `{shadow['by_event_type']}`",
        f"- By gate: `{shadow['by_gate']}`",
        f"- has_bearish_order_block_1h4h: `{shadow['has_bearish_order_block']}`",
        f"- would_block_long_order_block_shadow: `{shadow['would_block_long_order_block_shadow']}`",
        f"- would_open_defensive_short_order_block_shadow: `{shadow['would_open_defensive_short_order_block_shadow']}`",
        f"- live_action_unchanged_false: `{shadow['live_action_unchanged_false']}`",
        "",
        "## Closed trades",
        "",
        f"- Closed trades with shadow: `{closed['closed_trades_with_shadow']}`",
        f"- Total PnL: `{closed['total_pnl_usd']}`",
        f"- Trades that would have been blocked: `{closed['would_block_long_trades']}`, PnL: `{closed['would_block_long_pnl_usd']}`",
        f"- Trades marked defensive short shadow: `{closed['would_open_defensive_short_marked_trades']}`, PnL: `{closed['would_open_defensive_short_marked_pnl_usd']}`",
        f"- By gate: `{closed['by_gate']}`",
        "",
        "## Forward proxies",
        "",
        "| Scope | Count | Decision value | Net PnL | Avg value bps | Hit | PF |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| Long veto raw | {long_raw['count']} | {fmt(long_raw['decision_value_usd'])} | {fmt(long_raw['net_pnl_usd'])} | {fmt(long_raw['avg_decision_value_bps'])} | {fmt(long_raw['hit_rate'])} | {fmt(long_raw['profit_factor'])} |",
        f"| Long veto deduped | {long_deduped['count']} | {fmt(long_deduped['decision_value_usd'])} | {fmt(long_deduped['net_pnl_usd'])} | {fmt(long_deduped['avg_decision_value_bps'])} | {fmt(long_deduped['hit_rate'])} | {fmt(long_deduped['profit_factor'])} |",
        f"| Defensive short raw | {short_raw['count']} | {fmt(short_raw['decision_value_usd'])} | {fmt(short_raw['net_pnl_usd'])} | {fmt(short_raw['avg_decision_value_bps'])} | {fmt(short_raw['hit_rate'])} | {fmt(short_raw['profit_factor'])} |",
        f"| Defensive short deduped | {short_deduped['count']} | {fmt(short_deduped['decision_value_usd'])} | {fmt(short_deduped['net_pnl_usd'])} | {fmt(short_deduped['avg_decision_value_bps'])} | {fmt(short_deduped['hit_rate'])} | {fmt(short_deduped['profit_factor'])} |",
        "",
        "## Top long-veto symbols, deduped",
        "",
        "| Symbol | Count | Decision value | Avg value bps |",
        "|---|---:|---:|---:|",
    ]
    for row in payload["top_long_veto_symbols_deduped"]:
        lines.append(
            f"| `{row['symbol']}` | {row['count']} | {fmt(row['decision_value_usd'])} | {fmt(row['avg_decision_value_bps'])} |"
        )
    lines.extend(
        [
            "",
            "## Decision note",
            "",
            decision_note(payload),
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def decision_note(payload: dict[str, Any]) -> str:
    closed = payload["closed_trade_summary"]
    long_deduped = payload["long_veto_proxy_deduped"]
    short_deduped = payload["defensive_short_proxy_deduped"]
    if int(closed.get("would_block_long_trades") or 0) > 0:
        return "Le shadow a touché des trades réels; comparer pertes évitées et winners manqués avant promotion."
    if int(short_deduped.get("count") or 0) > 0 and float(short_deduped.get("decision_value_usd") or 0.0) > 0:
        return "Les shorts order-block sont positifs en proxy; refaire un replay full-bot avant toute activation."
    if int(long_deduped.get("count") or 0) > 0 and float(long_deduped.get("decision_value_usd") or 0.0) > 0:
        return "Le veto long order-block est positif en proxy mais sans trade réel bloqué; garder en shadow."
    return "Pas de preuve live suffisante; garder P1-07 en shadow/research sans changement live."


def in_window(timestamp: datetime, start: datetime, end: datetime | None) -> bool:
    return timestamp >= start and (end is None or timestamp <= end)


def parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def fmt(value: object) -> str:
    numeric = optional_float(value)
    if numeric is None:
        return ""
    return f"{numeric:.4f}"


if __name__ == "__main__":
    main()
