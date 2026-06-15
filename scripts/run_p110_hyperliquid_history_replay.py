#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import shutil
import subprocess
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.research.hyperliquid_top30_research import (
    INTERVAL_TO_MS,
    CandleRecord,
    HyperliquidTop30DatasetBuilder,
    _dt_to_ms,
    _iso_from_ms,
)


DEFAULT_SYMBOLS = (
    "BTC",
    "ETH",
    "SOL",
    "NEAR",
    "TON",
    "INJ",
    "ZEC",
    "HYPE",
    "ONDO",
    "VVV",
    "SAGA",
    "PENDLE",
    "TIA",
    "ZRO",
    "STRK",
    "DYM",
    "ICP",
    "PENGU",
)

TOP_SYMBOL_DOW_RULES: dict[tuple[str, int], str] = {
    ("TON", 0): "long",
    ("NEAR", 0): "long",
    ("INJ", 0): "long",
    ("ZEC", 6): "long",
    ("HYPE", 6): "long",
    ("ONDO", 4): "short",
    ("VVV", 2): "short",
    ("SAGA", 4): "short",
    ("PENDLE", 2): "short",
    ("TIA", 4): "short",
}

CLUSTER_DOW_RULES: tuple[tuple[int, str], ...] = (
    (2, "short"),
    (4, "short"),
    (6, "long"),
)


@dataclass(frozen=True, slots=True)
class CalendarRule:
    name: str
    description: str
    interval: str
    hold_bars: int
    mode: str
    symbol_side_by_dow: dict[tuple[str, int], str] | None = None
    cluster_side_by_dow: dict[int, str] | None = None


@dataclass(frozen=True, slots=True)
class HistoryTrade:
    rule: str
    timestamp: str
    exit_timestamp: str
    symbol: str
    interval: str
    side: str
    dow_utc: int
    hour_utc: int
    entry_price: float
    exit_price: float
    gross_bps: float
    net_bps: float
    notional_usd: float
    net_pnl_usd: float
    period: str


@dataclass(frozen=True, slots=True)
class RuleSummary:
    rule: str
    description: str
    status: str
    reason: str
    interval: str
    hold_bars: int
    trade_count: int
    net_pnl_usd: float
    avg_net_bps: float
    hit_rate: float | None
    profit_factor: float | None
    max_drawdown_usd: float
    first_timestamp: str | None
    last_timestamp: str | None
    pre_local_pnl_usd: float
    local_window_pnl_usd: float
    recent_half_pnl_usd: float
    top_symbol: str
    top_symbol_pnl_usd: float
    worst_symbol: str
    worst_symbol_pnl_usd: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-10 Hyperliquid API/S3 long-history calendar replay.")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--days", type=int, default=900)
    parser.add_argument("--intervals", default="1h,4h,1d")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--round-trip-cost-bps", type=float, default=16.0)
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--skip-s3", action="store_true")
    parser.add_argument("--s3-dates", default="20230916")
    parser.add_argument("--s3-hours", default="9")
    parser.add_argument("--s3-symbols", default="SOL,BTC")
    parser.add_argument("--s3-timeout-seconds", type=int, default=90)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p110_hyperliquid_history_{generated_at}")
    output_dir.mkdir(parents=True, exist_ok=True)
    symbols = _parse_csv(args.symbols, upper=True)
    intervals = _parse_csv(args.intervals, upper=False)
    start = datetime.now(tz=UTC) - timedelta(days=max(args.days, 1))
    end = datetime.now(tz=UTC)
    start_ms = _dt_to_ms(start)
    end_ms = _dt_to_ms(end)

    api_manifest = {
        "status": "skipped",
        "symbols": symbols,
        "intervals": intervals,
        "requested_start": _iso_from_ms(start_ms),
        "requested_end": _iso_from_ms(end_ms),
        "coverage": {},
    }
    if not args.skip_api:
        api_manifest = collect_api_candles(
            config=args.config,
            output_dir=output_dir,
            symbols=symbols,
            intervals=intervals,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    s3_manifest = {"status": "skipped"}
    if not args.skip_s3:
        s3_manifest = attempt_s3_archive_probe(
            output_dir=output_dir,
            dates=_parse_csv(args.s3_dates, upper=False),
            hours=_parse_csv(args.s3_hours, upper=False),
            symbols=_parse_csv(args.s3_symbols, upper=True),
            timeout_seconds=args.s3_timeout_seconds,
        )

    rules = default_rules(intervals)
    trades = replay_calendar_rules(
        output_dir=output_dir,
        symbols=symbols,
        rules=rules,
        notional_usd=args.notional_usd,
        cost_bps=args.round_trip_cost_bps,
    )
    summaries = summarize_rules(rules, trades)
    write_csv(output_dir / "p110_rule_trades.csv", [asdict(trade) for trade in trades])
    write_csv(output_dir / "p110_rule_summary.csv", [asdict(summary) for summary in summaries])
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "method": {
            "api": "Hyperliquid info/candleSnapshot candles; fixed cost because historical candles do not expose live spread/microstructure.",
            "s3": "Best-effort requester-pays archive probe for market_data/asset_ctxs; no dependency on S3 success for API replay.",
            "rules": "P1-09 calendar/symbol hypotheses are frozen before this replay; no parameter search in P1-10.",
        },
        "api_manifest": api_manifest,
        "s3_manifest": s3_manifest,
        "rules": [rule_to_dict(rule) for rule in rules],
        "summaries": [asdict(summary) for summary in summaries],
    }
    (output_dir / "p110_hyperliquid_history_replay.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_report(output_dir / "p110_hyperliquid_history_replay.md", payload)
    print(output_dir)


def collect_api_candles(
    *,
    config: str,
    output_dir: Path,
    symbols: list[str],
    intervals: list[str],
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    builder = HyperliquidTop30DatasetBuilder(config_path=config)
    raw_dir = output_dir / "raw" / "api_candles"
    coverage: dict[str, dict[str, Any]] = {}
    fetches = 0
    started = time.perf_counter()
    for interval in intervals:
        if interval not in INTERVAL_TO_MS:
            continue
        coverage[interval] = {}
        interval_dir = raw_dir / interval
        interval_dir.mkdir(parents=True, exist_ok=True)
        for index, symbol in enumerate(symbols, start=1):
            print(f"[api candles] {interval} {index}/{len(symbols)} {symbol}", flush=True)
            candles = builder._fetch_candles(
                symbol=symbol,
                interval=interval,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            fetches += 1
            path = interval_dir / f"{symbol}.json.gz"
            _write_gzip_json(path, [record.to_dict() for record in candles])
            coverage[interval][symbol] = coverage_dict(candles, interval, start_ms=start_ms, end_ms=end_ms)
    manifest = {
        "status": "completed",
        "config": config,
        "symbols": symbols,
        "intervals": [interval for interval in intervals if interval in INTERVAL_TO_MS],
        "requested_start": _iso_from_ms(start_ms),
        "requested_end": _iso_from_ms(end_ms),
        "fetches": fetches,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "coverage": coverage,
        "notes": [
            "candleSnapshot returns at most the most recent 5000 candles per interval.",
            "1h history is therefore capped near 208 days even when --days is larger.",
        ],
    }
    (output_dir / "api_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def attempt_s3_archive_probe(
    *,
    output_dir: Path,
    dates: list[str],
    hours: list[str],
    symbols: list[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    s3_dir = output_dir / "raw" / "s3_probe"
    s3_dir.mkdir(parents=True, exist_ok=True)
    aws = shutil.which("aws")
    lz4 = shutil.which("lz4") or shutil.which("unlz4")
    manifest: dict[str, Any] = {
        "status": "unavailable",
        "aws_path": aws,
        "lz4_path": lz4,
        "dates": dates,
        "hours": hours,
        "symbols": symbols,
        "attempts": [],
        "notes": [
            "Official S3 archive is requester-pays and may require AWS tooling/credentials.",
            "Expected paths: s3://hyperliquid-archive/market_data/[date]/[hour]/[datatype]/[coin].lz4 and s3://hyperliquid-archive/asset_ctxs/[date].csv.lz4.",
        ],
    }
    if aws is None:
        manifest["reason"] = "aws CLI not installed in this workspace"
        (s3_dir / "s3_probe_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return manifest

    for date in dates:
        asset_target = s3_dir / f"asset_ctxs_{date}.csv.lz4"
        manifest["attempts"].append(
            run_s3_copy(
                source=f"s3://hyperliquid-archive/asset_ctxs/{date}.csv.lz4",
                target=asset_target,
                timeout_seconds=timeout_seconds,
            )
        )
        for hour in hours:
            for symbol in symbols:
                target = s3_dir / f"market_data_{date}_{hour}_l2Book_{symbol}.lz4"
                manifest["attempts"].append(
                    run_s3_copy(
                        source=f"s3://hyperliquid-archive/market_data/{date}/{hour}/l2Book/{symbol}.lz4",
                        target=target,
                        timeout_seconds=timeout_seconds,
                    )
                )
    success_count = sum(1 for item in manifest["attempts"] if item.get("status") == "downloaded")
    manifest["status"] = "completed" if success_count else "attempted_no_download"
    manifest["downloaded_count"] = success_count
    (s3_dir / "s3_probe_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def run_s3_copy(*, source: str, target: Path, timeout_seconds: int) -> dict[str, Any]:
    cmd = [
        "aws",
        "s3",
        "cp",
        source,
        str(target),
        "--request-payer",
        "requester",
        "--no-progress",
    ]
    started = time.perf_counter()
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "source": source,
            "target": str(target),
            "status": "timeout",
            "runtime_seconds": round(time.perf_counter() - started, 3),
            "stderr": str(exc),
        }
    status = "downloaded" if result.returncode == 0 and target.exists() and target.stat().st_size > 0 else "failed"
    return {
        "source": source,
        "target": str(target),
        "status": status,
        "returncode": result.returncode,
        "runtime_seconds": round(time.perf_counter() - started, 3),
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
        "bytes": target.stat().st_size if target.exists() else 0,
    }


def default_rules(intervals: list[str]) -> list[CalendarRule]:
    rules: list[CalendarRule] = []
    if "1h" in intervals:
        rules.append(
            CalendarRule(
                name="symbol_dow_top_hits_1h_daily_open_8h",
                description="P1-09 symbol/day top hits, one 00:00 UTC entry per matching day, 8h hold.",
                interval="1h",
                hold_bars=8,
                mode="daily_open",
                symbol_side_by_dow=dict(TOP_SYMBOL_DOW_RULES),
            )
        )
        rules.append(
            CalendarRule(
                name="calendar_cluster_1h_daily_open_8h",
                description="P1-09 cluster day rules across selected crypto symbols, one 00:00 UTC entry per matching day, 8h hold.",
                interval="1h",
                hold_bars=8,
                mode="daily_open",
                cluster_side_by_dow=dict(CLUSTER_DOW_RULES),
            )
        )
    if "4h" in intervals:
        rules.append(
            CalendarRule(
                name="symbol_dow_top_hits_4h_every_bar_8h",
                description="P1-09 symbol/day top hits on 4h candles, 8h hold.",
                interval="4h",
                hold_bars=2,
                mode="every_bar",
                symbol_side_by_dow=dict(TOP_SYMBOL_DOW_RULES),
            )
        )
        rules.append(
            CalendarRule(
                name="calendar_cluster_4h_every_bar_8h",
                description="P1-09 cluster day rules across selected crypto symbols on 4h candles, 8h hold.",
                interval="4h",
                hold_bars=2,
                mode="every_bar",
                cluster_side_by_dow=dict(CLUSTER_DOW_RULES),
            )
        )
    if "1d" in intervals:
        rules.append(
            CalendarRule(
                name="symbol_dow_top_hits_1d_1d_hold",
                description="P1-09 symbol/day top hits on daily candles, 1d hold.",
                interval="1d",
                hold_bars=1,
                mode="every_bar",
                symbol_side_by_dow=dict(TOP_SYMBOL_DOW_RULES),
            )
        )
        rules.append(
            CalendarRule(
                name="calendar_cluster_1d_1d_hold",
                description="P1-09 cluster day rules across selected crypto symbols on daily candles, 1d hold.",
                interval="1d",
                hold_bars=1,
                mode="every_bar",
                cluster_side_by_dow=dict(CLUSTER_DOW_RULES),
            )
        )
    return rules


def rule_to_dict(rule: CalendarRule) -> dict[str, Any]:
    payload = asdict(rule)
    payload["symbol_side_by_dow"] = [
        {"symbol": symbol, "dow": dow, "side": side}
        for (symbol, dow), side in sorted((rule.symbol_side_by_dow or {}).items())
    ]
    payload["cluster_side_by_dow"] = [
        {"dow": dow, "side": side}
        for dow, side in sorted((rule.cluster_side_by_dow or {}).items())
    ]
    return payload


def replay_calendar_rules(
    *,
    output_dir: Path,
    symbols: list[str],
    rules: list[CalendarRule],
    notional_usd: float,
    cost_bps: float,
) -> list[HistoryTrade]:
    candles_by_interval_symbol: dict[tuple[str, str], list[CandleRecord]] = {}
    for rule in rules:
        for symbol in symbols:
            key = (rule.interval, symbol)
            if key in candles_by_interval_symbol:
                continue
            path = output_dir / "raw" / "api_candles" / rule.interval / f"{symbol}.json.gz"
            candles_by_interval_symbol[key] = read_candles(path)
    trades: list[HistoryTrade] = []
    for rule in rules:
        for symbol in symbols:
            candles = candles_by_interval_symbol.get((rule.interval, symbol), [])
            if not candles:
                continue
            for index, candle in enumerate(candles):
                exit_index = index + rule.hold_bars
                if exit_index >= len(candles):
                    continue
                dt = datetime.fromtimestamp(candle.start_time / 1000.0, tz=UTC)
                if rule.mode == "daily_open" and dt.hour != 0:
                    continue
                side = side_for_rule(rule, symbol=symbol, dow=dt.weekday())
                if side is None:
                    continue
                exit_candle = candles[exit_index]
                if candle.close <= 0 or exit_candle.close <= 0:
                    continue
                raw_bps = (exit_candle.close / candle.close - 1.0) * 10_000.0
                gross_bps = raw_bps if side == "long" else -raw_bps
                net_bps = gross_bps - cost_bps
                trades.append(
                    HistoryTrade(
                        rule=rule.name,
                        timestamp=_iso_from_ms(candle.start_time),
                        exit_timestamp=_iso_from_ms(exit_candle.start_time),
                        symbol=symbol,
                        interval=rule.interval,
                        side=side,
                        dow_utc=dt.weekday(),
                        hour_utc=dt.hour,
                        entry_price=round(candle.close, 8),
                        exit_price=round(exit_candle.close, 8),
                        gross_bps=round(gross_bps, 6),
                        net_bps=round(net_bps, 6),
                        notional_usd=notional_usd,
                        net_pnl_usd=round(net_bps / 10_000.0 * notional_usd, 6),
                        period=period_label(candle.start_time),
                    )
                )
    trades.sort(key=lambda item: (item.timestamp, item.rule, item.symbol))
    return trades


def side_for_rule(rule: CalendarRule, *, symbol: str, dow: int) -> str | None:
    if rule.symbol_side_by_dow is not None:
        return rule.symbol_side_by_dow.get((symbol, dow))
    if rule.cluster_side_by_dow is not None and symbol not in {"BTC", "ETH"}:
        return rule.cluster_side_by_dow.get(dow)
    return None


def summarize_rules(rules: list[CalendarRule], trades: list[HistoryTrade]) -> list[RuleSummary]:
    by_rule: dict[str, list[HistoryTrade]] = defaultdict(list)
    for trade in trades:
        by_rule[trade.rule].append(trade)
    summaries: list[RuleSummary] = []
    rule_by_name = {rule.name: rule for rule in rules}
    for rule_name in sorted(rule_by_name):
        rule = rule_by_name[rule_name]
        rows = by_rule.get(rule_name, [])
        pnls = [row.net_pnl_usd for row in rows]
        by_symbol = defaultdict(float)
        for row in rows:
            by_symbol[row.symbol] += row.net_pnl_usd
        top_symbol, top_symbol_pnl = max(by_symbol.items(), key=lambda item: item[1], default=("", 0.0))
        worst_symbol, worst_symbol_pnl = min(by_symbol.items(), key=lambda item: item[1], default=("", 0.0))
        pre_local = sum(row.net_pnl_usd for row in rows if row.period == "pre_local_window")
        local = sum(row.net_pnl_usd for row in rows if row.period == "local_p109_window")
        midpoint = rows[len(rows) // 2].timestamp if rows else ""
        recent_half = sum(row.net_pnl_usd for row in rows if row.timestamp >= midpoint)
        summary = RuleSummary(
            rule=rule_name,
            description=rule.description,
            status="",
            reason="",
            interval=rule.interval,
            hold_bars=rule.hold_bars,
            trade_count=len(rows),
            net_pnl_usd=round(sum(pnls), 6),
            avg_net_bps=round(sum(row.net_bps for row in rows) / len(rows), 6) if rows else 0.0,
            hit_rate=win_rate(pnls),
            profit_factor=profit_factor(pnls),
            max_drawdown_usd=round(max_drawdown(pnls), 6),
            first_timestamp=rows[0].timestamp if rows else None,
            last_timestamp=rows[-1].timestamp if rows else None,
            pre_local_pnl_usd=round(pre_local, 6),
            local_window_pnl_usd=round(local, 6),
            recent_half_pnl_usd=round(recent_half, 6),
            top_symbol=top_symbol,
            top_symbol_pnl_usd=round(top_symbol_pnl, 6),
            worst_symbol=worst_symbol,
            worst_symbol_pnl_usd=round(worst_symbol_pnl, 6),
        )
        status, reason = classify_summary(summary)
        summaries.append(
            RuleSummary(
                **{
                    **asdict(summary),
                    "status": status,
                    "reason": reason,
                }
            )
        )
    return summaries


def classify_summary(summary: RuleSummary) -> tuple[str, str]:
    pf = summary.profit_factor or 0.0
    if summary.trade_count < 30:
        return "research_only", "sample long-historique trop faible"
    if summary.net_pnl_usd <= 0:
        return "rejetee", "PnL net long-historique negatif"
    if summary.pre_local_pnl_usd <= 0:
        return "research_only", "positif total mais pas confirme avant la fenetre locale P1-09"
    if summary.local_window_pnl_usd <= 0:
        return "research_only", "positif historique mais ne confirme pas la fenetre locale recente"
    if pf >= 1.15 and summary.recent_half_pnl_usd > 0:
        return "candidate_walk_forward", "positif pre-local et local; walk-forward requis avant shadow"
    return "research_only", "positif mais PF/recence insuffisants"


def coverage_dict(candles: list[CandleRecord], interval: str, *, start_ms: int, end_ms: int) -> dict[str, Any]:
    if not candles:
        return {"available": False, "bar_count": 0, "interval": interval}
    interval_ms = INTERVAL_TO_MS[interval]
    expected = max(1, math.floor((end_ms - start_ms) / interval_ms))
    return {
        "available": True,
        "bar_count": len(candles),
        "interval": interval,
        "actual_start": _iso_from_ms(candles[0].start_time),
        "actual_end": _iso_from_ms(candles[-1].end_time),
        "coverage_days": round((candles[-1].end_time - candles[0].start_time) / 86_400_000.0, 2),
        "coverage_ratio_vs_request": round(min(len(candles) / expected, 1.0), 4),
        "full_requested_window": candles[0].start_time <= start_ms + interval_ms,
    }


def period_label(start_time_ms: int) -> str:
    dt = datetime.fromtimestamp(start_time_ms / 1000.0, tz=UTC)
    if dt < datetime(2026, 4, 5, tzinfo=UTC):
        return "pre_local_window"
    if dt <= datetime(2026, 6, 15, 23, 59, 59, tzinfo=UTC):
        return "local_p109_window"
    return "post_local_window"


def read_candles(path: Path) -> list[CandleRecord]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        return []
    rows: list[CandleRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append(
            CandleRecord(
                start_time=int(item.get("start_time", item.get("t", 0)) or 0),
                end_time=int(item.get("end_time", item.get("T", 0)) or 0),
                interval=str(item.get("interval", item.get("i", "")) or ""),
                symbol=str(item.get("symbol", item.get("s", "")) or ""),
                open=_float(item.get("open", item.get("o"))),
                high=_float(item.get("high", item.get("h"))),
                low=_float(item.get("low", item.get("l"))),
                close=_float(item.get("close", item.get("c"))),
                volume=_float(item.get("volume", item.get("v"))),
                trade_count=int(_float(item.get("trade_count", item.get("n")))),
            )
        )
    rows.sort(key=lambda item: item.start_time)
    return rows


def write_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# P1-10 - Hyperliquid long history API/S3",
        "",
        f"- Genere le: `{payload['generated_at']}`",
        "- Statut: `research_only_no_live_change`",
        "- API: `candleSnapshot` Hyperliquid; S3: probe requester-pays best-effort.",
        "- Important: les candles historiques ne contiennent pas les features live microstructure (`spread`, `book_imbalance`, `trade_flow_bias`).",
        "",
        "## Couverture API",
    ]
    coverage = payload.get("api_manifest", {}).get("coverage", {})
    if isinstance(coverage, dict):
        for interval, symbols in coverage.items():
            if not isinstance(symbols, dict):
                continue
            available = [item for item in symbols.values() if isinstance(item, dict) and item.get("available")]
            min_days = min((float(item.get("coverage_days", 0.0)) for item in available), default=0.0)
            max_days = max((float(item.get("coverage_days", 0.0)) for item in available), default=0.0)
            lines.append(f"- `{interval}`: `{len(available)}` symboles disponibles, couverture `{min_days:.1f}` -> `{max_days:.1f}` jours.")
    s3 = payload.get("s3_manifest", {})
    lines.extend(
        [
            "",
            "## S3",
            f"- Statut: `{s3.get('status')}`",
            f"- Raison/outillage: `{s3.get('reason', '')}`; aws=`{s3.get('aws_path')}`, lz4=`{s3.get('lz4_path')}`.",
            "",
            "## Decisions",
            "| Regle | Statut | Trades | Net USD | PF | WR | DD | Pre-local | Local P1-09 | Raison |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for summary in payload.get("summaries", []):
        lines.append(
            f"| `{summary['rule']}` | `{summary['status']}` | {summary['trade_count']} | "
            f"{summary['net_pnl_usd']:.2f} | {_fmt(summary['profit_factor'])} | {_pct(summary['hit_rate'])} | "
            f"{summary['max_drawdown_usd']:.2f} | {summary['pre_local_pnl_usd']:.2f} | "
            f"{summary['local_window_pnl_usd']:.2f} | {summary['reason']} |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "- `candidate_walk_forward` ne veut pas dire promouvable live: la regle doit encore survivre a une collecte future figee.",
            "- Les resultats S3 sont separes de l'API; si S3 est indisponible, le verdict porte seulement sur OHLCV public.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_gzip_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _parse_csv(value: str, *, upper: bool) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return [item.upper() for item in items] if upper else items


def _float(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return parsed


def win_rate(pnls: list[float]) -> float | None:
    return sum(1 for pnl in pnls if pnl > 0) / len(pnls) if pnls else None


def profit_factor(pnls: list[float]) -> float | None:
    gains = sum(pnl for pnl in pnls if pnl > 0)
    losses = -sum(pnl for pnl in pnls if pnl < 0)
    return gains / losses if losses > 0 else None


def max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def _fmt(value: Any) -> str:
    if value in (None, ""):
        return "na"
    return f"{float(value):.2f}"


def _pct(value: float | None) -> str:
    return "na" if value is None else f"{value:.1%}"


if __name__ == "__main__":
    main()
