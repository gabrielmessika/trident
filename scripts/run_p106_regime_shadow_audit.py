#!/usr/bin/env python3
"""Audit live Pod A P1-06 regime shadow fields.

This is research-only. It reads local fetched logs/snapshots, measures the
observation-only regime gate, and estimates defensive-short candidates with a
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


DEFAULT_START = "2026-06-13T12:59:00Z"


@dataclass(frozen=True, slots=True)
class ShadowEvent:
    event_type: str
    timestamp: datetime
    timestamp_text: str
    symbol: str
    side: str
    setup: str
    status: str
    source: str
    price: float | None
    bull_score: float | None
    bear_score: float | None
    regime_gate: str
    would_block_long: bool
    would_open_defensive_short_shadow: bool
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
    bull_score: float | None
    bear_score: float | None
    regime_gate: str
    would_block_long: bool
    would_open_defensive_short_shadow: bool
    live_action_unchanged: bool


@dataclass(frozen=True, slots=True)
class ForwardReturn:
    event_type: str
    timestamp: str
    symbol: str
    side: str
    setup: str
    regime_gate: str
    entry_price: float
    exit_timestamp: str
    exit_price: float
    gross_short_return_bps: float
    net_short_return_bps: float
    net_pnl_usd: float


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
        args.output_dir or f"server-data/replay_reports/p106_regime_shadow_audit_{stamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    events, trades = load_shadow_events(Path(args.pod_a_log), start=start, end=end)
    price_index = load_price_index(Path(args.snapshots_dir), start=start, end=end)
    raw_candidates = [
        event for event in events if event.would_open_defensive_short_shadow
    ]
    deduped_candidates = dedupe_candidates(
        raw_candidates,
        cooldown=timedelta(minutes=max(args.dedupe_minutes, 0)),
    )
    raw_forwards = forward_returns(
        raw_candidates,
        price_index=price_index,
        horizon=timedelta(minutes=max(args.horizon_minutes, 1)),
        cost_bps=float(args.cost_bps),
        notional_usd=float(args.notional_usd),
    )
    deduped_forwards = forward_returns(
        deduped_candidates,
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
        "defensive_short_proxy_raw": forward_summary(raw_forwards),
        "defensive_short_proxy_deduped": forward_summary(deduped_forwards),
        "top_symbols_raw": top_symbol_summary(raw_forwards),
        "top_symbols_deduped": top_symbol_summary(deduped_forwards),
    }
    (output_dir / "p106_regime_shadow_audit.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_forward_csv(output_dir / "defensive_short_forward_returns_raw.csv", raw_forwards)
    write_forward_csv(output_dir / "defensive_short_forward_returns_deduped.csv", deduped_forwards)
    write_report(
        output_dir / "p106_regime_shadow_audit.md",
        payload=payload,
        events=events,
        trades=trades,
    )
    print(output_dir)


def load_shadow_events(
    path: Path,
    *,
    start: datetime,
    end: datetime | None,
) -> tuple[list[ShadowEvent], list[ClosedTrade]]:
    events: list[ShadowEvent] = []
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


def event_from_signal(record: dict[str, Any]) -> ShadowEvent | None:
    signal = record.get("signal") or {}
    if not isinstance(signal, dict):
        return None
    details = signal.get("setup_details") or signal.get("regime_shadow") or {}
    if not isinstance(details, dict):
        return None
    timestamp = parse_timestamp(record.get("timestamp") or record.get("event_ts"))
    if timestamp is None:
        return None
    symbol_snapshot = record.get("symbol_snapshot") or {}
    price = optional_float(symbol_snapshot.get("price")) if isinstance(symbol_snapshot, dict) else None
    return build_shadow_event(
        event_type="signal",
        timestamp=timestamp,
        symbol=str(signal.get("symbol") or ""),
        side=str(signal.get("side") or ""),
        setup=str(signal.get("setup") or ""),
        status=str((signal.get("risk") or {}).get("accepted") if isinstance(signal.get("risk"), dict) else ""),
        source=str(record.get("source") or ""),
        price=price,
        details=details,
    )


def event_from_review(record: dict[str, Any]) -> ShadowEvent | None:
    review = record.get("review") or {}
    if not isinstance(review, dict):
        return None
    details = review.get("regime_shadow") or {}
    if not isinstance(details, dict):
        return None
    timestamp = parse_timestamp(record.get("timestamp") or record.get("event_ts"))
    if timestamp is None:
        return None
    symbol_snapshot = record.get("symbol_snapshot") or {}
    price = optional_float(symbol_snapshot.get("price")) if isinstance(symbol_snapshot, dict) else None
    setups = review.get("candidate_setups") or []
    setup = ",".join(str(item) for item in setups) if isinstance(setups, list) else str(setups or "")
    return build_shadow_event(
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


def build_shadow_event(
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
) -> ShadowEvent | None:
    if details.get("regime_shadow_mode") != "observation_only":
        return None
    return ShadowEvent(
        event_type=event_type,
        timestamp=timestamp,
        timestamp_text=isoformat(timestamp),
        symbol=symbol.upper(),
        side=side.lower(),
        setup=setup,
        status=status,
        source=source,
        price=price,
        bull_score=optional_float(details.get("bull_regime_score")),
        bear_score=optional_float(details.get("bear_regime_score")),
        regime_gate=str(details.get("regime_gate_decision") or ""),
        would_block_long=details.get("would_block_long") is True,
        would_open_defensive_short_shadow=details.get("would_open_defensive_short_shadow") is True,
        live_action_unchanged=details.get("live_action_unchanged") is True,
    )


def trade_from_record(record: dict[str, Any]) -> ClosedTrade | None:
    trade = record.get("trade") or {}
    if not isinstance(trade, dict):
        return None
    details = trade.get("setup_details") or {}
    if not isinstance(details, dict) or details.get("regime_shadow_mode") != "observation_only":
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
        bull_score=optional_float(details.get("bull_regime_score")),
        bear_score=optional_float(details.get("bear_regime_score")),
        regime_gate=str(details.get("regime_gate_decision") or ""),
        would_block_long=details.get("would_block_long") is True,
        would_open_defensive_short_shadow=details.get("would_open_defensive_short_shadow") is True,
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
    candidates: Iterable[ShadowEvent],
    *,
    cooldown: timedelta,
) -> list[ShadowEvent]:
    if cooldown.total_seconds() <= 0:
        return sorted(candidates, key=lambda item: item.timestamp)
    selected: list[ShadowEvent] = []
    last_by_key: dict[tuple[str, str], datetime] = {}
    for event in sorted(candidates, key=lambda item: item.timestamp):
        key = (event.event_type, event.symbol)
        last = last_by_key.get(key)
        if last is not None and event.timestamp < last + cooldown:
            continue
        selected.append(event)
        last_by_key[key] = event.timestamp
    return selected


def forward_returns(
    candidates: Iterable[ShadowEvent],
    *,
    price_index: PriceIndex,
    horizon: timedelta,
    cost_bps: float,
    notional_usd: float,
) -> list[ForwardReturn]:
    rows: list[ForwardReturn] = []
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
        gross_bps = ((entry - exit_price) / entry) * 10_000.0
        net_bps = gross_bps - cost_bps
        rows.append(
            ForwardReturn(
                event_type=event.event_type,
                timestamp=event.timestamp_text,
                symbol=event.symbol,
                side=event.side,
                setup=event.setup,
                regime_gate=event.regime_gate,
                entry_price=round(entry, 10),
                exit_timestamp=isoformat(exit_timestamp),
                exit_price=round(exit_price, 10),
                gross_short_return_bps=round(gross_bps, 6),
                net_short_return_bps=round(net_bps, 6),
                net_pnl_usd=round(notional_usd * net_bps / 10_000.0, 6),
            )
        )
    return rows


def shadow_summary(events: list[ShadowEvent]) -> dict[str, Any]:
    by_event = Counter(event.event_type for event in events)
    by_gate = Counter(event.regime_gate for event in events)
    by_event_gate: dict[str, dict[str, int]] = {}
    for event_type in sorted(by_event):
        filtered = [event for event in events if event.event_type == event_type]
        by_event_gate[event_type] = dict(Counter(event.regime_gate for event in filtered))
    return {
        "records": len(events),
        "by_event_type": dict(by_event),
        "by_gate": dict(by_gate),
        "by_event_gate": by_event_gate,
        "would_block_long": sum(1 for event in events if event.would_block_long),
        "would_open_defensive_short_shadow": sum(
            1 for event in events if event.would_open_defensive_short_shadow
        ),
        "live_action_unchanged_false": sum(
            1 for event in events if not event.live_action_unchanged
        ),
        "top_block_long_symbols": top_event_symbols(events, field="would_block_long"),
        "top_defensive_short_symbols": top_event_symbols(
            events,
            field="would_open_defensive_short_shadow",
        ),
    }


def closed_trade_summary(trades: list[ClosedTrade]) -> dict[str, Any]:
    long_trades = [trade for trade in trades if trade.side == "long"]
    blocked_longs = [trade for trade in long_trades if trade.would_block_long]
    defensive_short_marks = [
        trade for trade in trades if trade.would_open_defensive_short_shadow
    ]
    return {
        "closed_trades_with_shadow": len(trades),
        "total_pnl_usd": round(sum(trade.pnl_usd for trade in trades), 6),
        "long_trades": len(long_trades),
        "long_pnl_usd": round(sum(trade.pnl_usd for trade in long_trades), 6),
        "would_block_long_trades": len(blocked_longs),
        "would_block_long_pnl_usd": round(sum(trade.pnl_usd for trade in blocked_longs), 6),
        "would_open_defensive_short_marked_trades": len(defensive_short_marks),
        "would_open_defensive_short_marked_pnl_usd": round(
            sum(trade.pnl_usd for trade in defensive_short_marks),
            6,
        ),
        "by_gate": {
            gate: {
                "count": len(rows),
                "pnl_usd": round(sum(row.pnl_usd for row in rows), 6),
            }
            for gate, rows in group_trades_by_gate(trades).items()
        },
        "by_symbol": top_trade_symbols(trades),
    }


def forward_summary(rows: list[ForwardReturn]) -> dict[str, Any]:
    wins = [row for row in rows if row.net_short_return_bps > 0]
    losses = [row for row in rows if row.net_short_return_bps <= 0]
    gross_win = sum(row.net_pnl_usd for row in wins)
    gross_loss = abs(sum(row.net_pnl_usd for row in losses))
    return {
        "count": len(rows),
        "net_pnl_usd": round(sum(row.net_pnl_usd for row in rows), 6),
        "avg_net_return_bps": round(
            sum(row.net_short_return_bps for row in rows) / len(rows),
            6,
        )
        if rows
        else None,
        "hit_rate": round(len(wins) / len(rows), 4) if rows else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "gross_win_usd": round(gross_win, 6),
        "gross_loss_usd": round(gross_loss, 6),
    }


def top_symbol_summary(rows: list[ForwardReturn], limit: int = 12) -> list[dict[str, Any]]:
    grouped: dict[str, list[ForwardReturn]] = defaultdict(list)
    for row in rows:
        grouped[row.symbol].append(row)
    summary = []
    for symbol, symbol_rows in grouped.items():
        summary.append(
            {
                "symbol": symbol,
                "count": len(symbol_rows),
                "net_pnl_usd": round(sum(row.net_pnl_usd for row in symbol_rows), 6),
                "avg_net_return_bps": round(
                    sum(row.net_short_return_bps for row in symbol_rows) / len(symbol_rows),
                    6,
                ),
            }
        )
    return sorted(summary, key=lambda item: float(item["net_pnl_usd"]), reverse=True)[:limit]


def top_event_symbols(events: list[ShadowEvent], *, field: str, limit: int = 12) -> list[tuple[str, int]]:
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


def write_forward_csv(path: Path, rows: list[ForwardReturn]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(asdict(rows[0]).keys()) if rows else [
            "event_type",
            "timestamp",
            "symbol",
            "side",
            "setup",
            "regime_gate",
            "entry_price",
            "exit_timestamp",
            "exit_price",
            "gross_short_return_bps",
            "net_short_return_bps",
            "net_pnl_usd",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_report(
    path: Path,
    *,
    payload: dict[str, Any],
    events: list[ShadowEvent],
    trades: list[ClosedTrade],
) -> None:
    shadow = payload["shadow_summary"]
    closed = payload["closed_trade_summary"]
    raw = payload["defensive_short_proxy_raw"]
    deduped = payload["defensive_short_proxy_deduped"]
    params = payload["parameters"]
    lines = [
        "# P1-06 regime shadow audit",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        "- status: `research_only_no_live_change`",
        f"- source_start: `{payload['source']['start']}`",
        f"- source_end: `{payload['source']['end'] or 'latest local log'}`",
        f"- horizon: `{params['horizon_minutes']}m`, cost: `{params['cost_bps']} bps`, notional: `{params['notional_usd']} USD`, dedupe: `{params['dedupe_minutes']}m`",
        "",
        "## Shadow coverage",
        "",
        f"- Records with regime shadow: `{shadow['records']}`",
        f"- By event type: `{shadow['by_event_type']}`",
        f"- By gate: `{shadow['by_gate']}`",
        f"- would_block_long: `{shadow['would_block_long']}`",
        f"- would_open_defensive_short_shadow: `{shadow['would_open_defensive_short_shadow']}`",
        f"- live_action_unchanged_false: `{shadow['live_action_unchanged_false']}`",
        "",
        "## Closed trades",
        "",
        f"- Closed trades with shadow: `{closed['closed_trades_with_shadow']}`",
        f"- Total PnL: `{closed['total_pnl_usd']}`",
        f"- Long trades: `{closed['long_trades']}`, PnL: `{closed['long_pnl_usd']}`",
        f"- Longs that would have been blocked: `{closed['would_block_long_trades']}`, PnL: `{closed['would_block_long_pnl_usd']}`",
        f"- Trades marked defensive short shadow: `{closed['would_open_defensive_short_marked_trades']}`, PnL: `{closed['would_open_defensive_short_marked_pnl_usd']}`",
        f"- By gate: `{closed['by_gate']}`",
        "",
        "## Defensive short proxy",
        "",
        "| Scope | Count | Net PnL | Avg bps | Hit | PF |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Raw candidates | {raw['count']} | {fmt(raw['net_pnl_usd'])} | {fmt(raw['avg_net_return_bps'])} | {fmt(raw['hit_rate'])} | {fmt(raw['profit_factor'])} |",
        f"| Deduped candidates | {deduped['count']} | {fmt(deduped['net_pnl_usd'])} | {fmt(deduped['avg_net_return_bps'])} | {fmt(deduped['hit_rate'])} | {fmt(deduped['profit_factor'])} |",
        "",
        "## Top symbols, deduped defensive shorts",
        "",
        "| Symbol | Count | Net PnL | Avg bps |",
        "|---|---:|---:|---:|",
    ]
    for row in payload["top_symbols_deduped"]:
        lines.append(
            f"| `{row['symbol']}` | {row['count']} | {fmt(row['net_pnl_usd'])} | {fmt(row['avg_net_return_bps'])} |"
        )
    lines.extend(
        [
            "",
            "## Decision note",
            "",
            decision_note(events=events, trades=trades, deduped_summary=deduped),
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def decision_note(
    *,
    events: list[ShadowEvent],
    trades: list[ClosedTrade],
    deduped_summary: dict[str, Any],
) -> str:
    blocked_longs = [trade for trade in trades if trade.side == "long" and trade.would_block_long]
    candidate_count = int(deduped_summary.get("count") or 0)
    net_pnl = float(deduped_summary.get("net_pnl_usd") or 0.0)
    if not trades:
        return "Pas assez de trades fermés avec shadow pour décider; continuer la collecte."
    if blocked_longs:
        return (
            "Le gate aurait bloqué des longs réels; comparer pertes évitées et winners manqués "
            "avant toute promotion."
        )
    if candidate_count < 10:
        return "Échantillon defensive_short dédupliqué trop faible; garder P1-06 en shadow."
    if net_pnl > 0:
        return (
            "Le proxy defensive_short est positif mais reste un proxy fixed-horizon; "
            "faire un replay full-bot/shadow avant toute activation."
        )
    return "Le proxy defensive_short n'est pas positif; garder en research/shadow et ne rien promouvoir."


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
