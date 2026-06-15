from __future__ import annotations

import argparse
import bisect
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib import parse, request


DEFAULT_REPORTS = [
    Path("server-data/replay_reports/external_reference_multisource_20260405_20260513_baseline.json"),
    Path(
        "server-data/replay_reports/"
        "full_bot_live_window_20260524T1605_20260611_current_config_no_external_reference_20260612.json"
    ),
]

REFERENCE_SYMBOLS = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "GLD": "GLD",
    "SLV": "SLV",
    "PAXG": "PAXG-USD",
    "TSLA": "TSLA",
    "NVDA": "NVDA",
    "CRCL": "CRCL",
    "XYZ:CL": "CL=F",
    "XYZ:BRENTOIL": "BZ=F",
    "XYZ:SP500": "ES=F",
    "XYZ:XYZ100": "NQ=F",
    "XYZ:SILVER": "SI=F",
    "XYZ:GOLD": "GC=F",
    "XYZ:JPY": "JPY=X",
    "XYZ:TSLA": "TSLA",
    "XYZ:NVDA": "NVDA",
    "XYZ:CRCL": "CRCL",
}


@dataclass(frozen=True, slots=True)
class QuotePoint:
    timestamp: datetime
    price: float


@dataclass(frozen=True, slots=True)
class EnrichedTrade:
    window: str
    symbol: str
    side: str
    opened_at: str
    pnl_usd: float
    entry_price: float
    reference_symbol: str
    reference_price: float | None
    reference_time: str | None
    reference_age_seconds: float | None
    external_premium_bps: float | None
    external_momentum_300s_bps: float | None
    reference_available: bool


@dataclass(frozen=True, slots=True)
class GateOutcome:
    window: str
    gate: str
    base_pnl_usd: float
    kept_pnl_usd: float
    delta_usd: float
    total_trades: int
    blocked_trades: int
    blocked_pnl_usd: float
    blocked_winners: int
    blocked_losers: int
    missing_reference_blocks: int
    stale_blocks: int
    premium_blocks: int
    momentum_blocks: int
    blocked_symbols: dict[str, int]


@dataclass(frozen=True, slots=True)
class WindowSummary:
    window: str
    input_report: str
    trade_count: int
    base_pnl_usd: float
    reference_coverage_pct: float
    reference_available_count: int
    missing_reference_count: int
    median_age_seconds: float | None
    max_age_seconds: float | None
    max_abs_premium_bps: float | None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    generated_at: str
    interval: str
    input_reports: list[str]
    window_summaries: list[WindowSummary]
    gate_outcomes: list[GateOutcome]
    recommendation: str
    notes: list[str]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _cache_name(symbol: str, start: datetime, end: datetime, interval: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)
    return f"{safe}_{interval}_{int(start.timestamp())}_{int(end.timestamp())}.json"


def _fetch_yahoo_points(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    interval: str,
    cache_dir: Path,
    timeout_seconds: float,
) -> list[QuotePoint]:
    if end - start > timedelta(days=45):
        merged: dict[datetime, QuotePoint] = {}
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=45), end)
            try:
                points = _fetch_yahoo_points(
                    symbol,
                    start=cursor,
                    end=chunk_end,
                    interval=interval,
                    cache_dir=cache_dir,
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                points = []
            for point in points:
                merged[point.timestamp] = point
            cursor = chunk_end + timedelta(seconds=1)
        return [merged[key] for key in sorted(merged)]

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / _cache_name(symbol, start, end, interval)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        encoded = parse.quote(symbol, safe="")
        query = parse.urlencode(
            {
                "period1": int(start.timestamp()),
                "period2": int(end.timestamp()),
                "interval": interval,
                "includePrePost": "true",
            }
        )
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?{query}"
        req = request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "trident-p103-pod-c-validation/0.1",
            },
        )
        with request.urlopen(req, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
    return _points_from_payload(payload)


def _points_from_payload(payload: object) -> list[QuotePoint]:
    if not isinstance(payload, dict):
        return []
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        return []
    timestamps = result.get("timestamp")
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0])
    closes = quote.get("close") if isinstance(quote, dict) else None
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        return []
    points: list[QuotePoint] = []
    for timestamp, close in zip(timestamps, closes):
        try:
            price = float(close)
            ts = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        if price > 0:
            points.append(QuotePoint(timestamp=ts, price=price))
    deduped = {point.timestamp: point for point in points}
    return [deduped[key] for key in sorted(deduped)]


def _quote_at_or_before(points: list[QuotePoint], opened_at: datetime) -> tuple[QuotePoint | None, QuotePoint | None]:
    timestamps = [point.timestamp for point in points]
    index = bisect.bisect_right(timestamps, opened_at) - 1
    if index < 0:
        return None, None
    current = points[index]
    previous = points[index - 1] if index > 0 else None
    return current, previous


def _load_pod_c_trades(report_path: Path) -> tuple[str, list[dict[str, object]]]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    dates = payload.get("dates_covered") or []
    if isinstance(dates, list) and dates:
        window = f"{dates[0]}_to_{dates[-1]}"
    else:
        first = str(payload.get("first_timestamp") or "")[:10]
        last = str(payload.get("last_timestamp") or "")[:10]
        window = f"{first}_to_{last}".strip("_to_") or report_path.stem
    pod_c = payload.get("pod_c") if isinstance(payload, dict) else {}
    trades = (pod_c or {}).get("closed_trade_log") if isinstance(pod_c, dict) else []
    return window, [trade for trade in trades or [] if isinstance(trade, dict)]


def _build_quote_indexes(
    trades_by_window: dict[str, list[dict[str, object]]],
    *,
    interval: str,
    cache_dir: Path,
    timeout_seconds: float,
) -> dict[str, list[QuotePoint]]:
    symbols: set[str] = set()
    opened_times: list[datetime] = []
    for trades in trades_by_window.values():
        for trade in trades:
            symbol = str(trade.get("symbol") or "").upper()
            if symbol in REFERENCE_SYMBOLS:
                symbols.add(symbol)
            opened_at = _parse_dt(str(trade.get("opened_at") or ""))
            if opened_at is not None:
                opened_times.append(opened_at)
    if not symbols or not opened_times:
        return {}
    start = min(opened_times) - timedelta(days=2)
    end = max(opened_times) + timedelta(days=2)
    indexes: dict[str, list[QuotePoint]] = {}
    for symbol in sorted(symbols):
        reference_symbol = REFERENCE_SYMBOLS[symbol]
        try:
            indexes[symbol] = _fetch_yahoo_points(
                reference_symbol,
                start=start,
                end=end,
                interval=interval,
                cache_dir=cache_dir,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            indexes[symbol] = []
    return indexes


def _enrich_trades(
    trades_by_window: dict[str, list[dict[str, object]]],
    indexes: dict[str, list[QuotePoint]],
) -> list[EnrichedTrade]:
    enriched: list[EnrichedTrade] = []
    for window, trades in trades_by_window.items():
        for trade in trades:
            symbol = str(trade.get("symbol") or "").upper()
            opened_at = _parse_dt(str(trade.get("opened_at") or ""))
            entry_price = _float(trade.get("entry_price"))
            pnl = _float(trade.get("pnl_usd"))
            side = str(trade.get("side") or "long").lower()
            reference_symbol = REFERENCE_SYMBOLS.get(symbol, "")
            quote = previous = None
            if opened_at is not None and entry_price > 0:
                quote, previous = _quote_at_or_before(indexes.get(symbol, []), opened_at)
            reference_price = quote.price if quote is not None else None
            age_seconds = (
                max((opened_at - quote.timestamp).total_seconds(), 0.0)
                if opened_at is not None and quote is not None
                else None
            )
            premium = (
                (entry_price - quote.price) / quote.price * 10_000.0
                if quote is not None and quote.price > 0 and entry_price > 0
                else None
            )
            momentum = (
                (quote.price - previous.price) / previous.price * 10_000.0
                if quote is not None and previous is not None and previous.price > 0
                else None
            )
            enriched.append(
                EnrichedTrade(
                    window=window,
                    symbol=symbol,
                    side=side,
                    opened_at=opened_at.isoformat() if opened_at else str(trade.get("opened_at") or ""),
                    pnl_usd=round(pnl, 6),
                    entry_price=round(entry_price, 8),
                    reference_symbol=reference_symbol,
                    reference_price=_round(reference_price, 8),
                    reference_time=quote.timestamp.isoformat() if quote is not None else None,
                    reference_age_seconds=_round(age_seconds, 4),
                    external_premium_bps=_round(premium, 4),
                    external_momentum_300s_bps=_round(momentum, 4),
                    reference_available=quote is not None,
                )
            )
    return enriched


def _float(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _gate_reason(trade: EnrichedTrade, gate: str) -> str | None:
    missing = not trade.reference_available
    age = trade.reference_age_seconds
    premium = trade.external_premium_bps
    momentum = trade.external_momentum_300s_bps
    side = trade.side

    if gate == "data_only":
        return None
    if gate == "missing_or_stale_15m":
        if missing:
            return "missing"
        if age is not None and age > 900:
            return "stale"
        return None
    if gate == "missing_or_stale_60m":
        if missing:
            return "missing"
        if age is not None and age > 3600:
            return "stale"
        return None
    if gate == "abs_premium_gt_50":
        return "premium" if premium is not None and abs(premium) > 50 else None
    if gate == "abs_premium_gt_100":
        return "premium" if premium is not None and abs(premium) > 100 else None
    if gate == "long_chase_premium_gt_25":
        return "premium" if side == "long" and premium is not None and premium > 25 else None
    if gate == "long_chase_premium_gt_50":
        return "premium" if side == "long" and premium is not None and premium > 50 else None
    if gate == "counter_momentum_5m_6bps":
        if side == "long" and momentum is not None and momentum <= -6:
            return "momentum"
        if side == "short" and momentum is not None and momentum >= 6:
            return "momentum"
        return None
    if gate == "candidate_default_5m":
        if missing:
            return "missing"
        if age is not None and age > 900:
            return "stale"
        if premium is not None and abs(premium) > 50:
            return "premium"
        if side == "long" and premium is not None and premium > 25:
            return "premium"
        if side == "long" and momentum is not None and momentum <= -6:
            return "momentum"
        if side == "short" and momentum is not None and momentum >= 6:
            return "momentum"
        return None
    if gate == "candidate_loose_5m":
        if missing:
            return "missing"
        if age is not None and age > 3600:
            return "stale"
        if premium is not None and abs(premium) > 100:
            return "premium"
        if side == "long" and premium is not None and premium > 50:
            return "premium"
        if side == "long" and momentum is not None and momentum <= -12:
            return "momentum"
        if side == "short" and momentum is not None and momentum >= 12:
            return "momentum"
        return None
    raise ValueError(f"unknown gate: {gate}")


def _evaluate_gate(window: str, trades: list[EnrichedTrade], gate: str) -> GateOutcome:
    base_pnl = sum(trade.pnl_usd for trade in trades)
    blocked: list[tuple[EnrichedTrade, str]] = []
    for trade in trades:
        reason = _gate_reason(trade, gate)
        if reason is not None:
            blocked.append((trade, reason))
    blocked_pnl = sum(trade.pnl_usd for trade, _ in blocked)
    symbols: dict[str, int] = {}
    for trade, _ in blocked:
        symbols[trade.symbol] = symbols.get(trade.symbol, 0) + 1
    return GateOutcome(
        window=window,
        gate=gate,
        base_pnl_usd=round(base_pnl, 4),
        kept_pnl_usd=round(base_pnl - blocked_pnl, 4),
        delta_usd=round(-blocked_pnl, 4),
        total_trades=len(trades),
        blocked_trades=len(blocked),
        blocked_pnl_usd=round(blocked_pnl, 4),
        blocked_winners=sum(1 for trade, _ in blocked if trade.pnl_usd > 0),
        blocked_losers=sum(1 for trade, _ in blocked if trade.pnl_usd < 0),
        missing_reference_blocks=sum(1 for _, reason in blocked if reason == "missing"),
        stale_blocks=sum(1 for _, reason in blocked if reason == "stale"),
        premium_blocks=sum(1 for _, reason in blocked if reason == "premium"),
        momentum_blocks=sum(1 for _, reason in blocked if reason == "momentum"),
        blocked_symbols=dict(sorted(symbols.items())),
    )


def _window_summary(window: str, input_report: Path, trades: list[EnrichedTrade]) -> WindowSummary:
    ages = [trade.reference_age_seconds for trade in trades if trade.reference_age_seconds is not None]
    premiums = [
        abs(trade.external_premium_bps)
        for trade in trades
        if trade.external_premium_bps is not None
    ]
    available = sum(1 for trade in trades if trade.reference_available)
    return WindowSummary(
        window=window,
        input_report=str(input_report),
        trade_count=len(trades),
        base_pnl_usd=round(sum(trade.pnl_usd for trade in trades), 4),
        reference_coverage_pct=round(available / len(trades) * 100.0, 2) if trades else 0.0,
        reference_available_count=available,
        missing_reference_count=len(trades) - available,
        median_age_seconds=_round(_median(ages), 4),
        max_age_seconds=_round(max(ages), 4) if ages else None,
        max_abs_premium_bps=_round(max(premiums), 4) if premiums else None,
    )


def _median(values: Iterable[float | None]) -> float | None:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(value))
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _recommendation(outcomes: list[GateOutcome], summaries: list[WindowSummary]) -> tuple[str, list[str]]:
    notes = [
        "Les references Yahoo 5m servent uniquement au replay/research P1-03.",
        "Aucune regle live n'est modifiee par ce script.",
    ]
    low_coverage_windows = [
        summary.window
        for summary in summaries
        if summary.trade_count > 0 and summary.reference_coverage_pct < 80.0
    ]
    non_data_gates = [outcome for outcome in outcomes if outcome.gate != "data_only"]
    covered_positive = sorted(
        {
            outcome.gate
            for outcome in non_data_gates
            for summary in summaries
            if summary.window == outcome.window
            and summary.reference_coverage_pct >= 80.0
            and outcome.delta_usd > 0.0
        }
    )
    if low_coverage_windows and covered_positive:
        notes.append(
            "Couverture insuffisante sur "
            f"{', '.join(low_coverage_windows)}; les candidats positifs "
            f"{', '.join(covered_positive)} ne sont pas validables out-of-sample."
        )
        return "keep_open_recent_guardrail_candidate_needs_oos_or_shadow", notes
    by_gate: dict[str, list[GateOutcome]] = {}
    for outcome in non_data_gates:
        by_gate.setdefault(outcome.gate, []).append(outcome)
    robust: list[str] = []
    for gate, rows in by_gate.items():
        if len(rows) < len(summaries):
            continue
        recent_delta = sum(row.delta_usd for row in rows if "2026-05-24" in row.window or "2026-06" in row.window)
        min_delta = min(row.delta_usd for row in rows)
        max_block_rate = max(row.blocked_trades / row.total_trades if row.total_trades else 0.0 for row in rows)
        if recent_delta > 0 and min_delta >= 0 and max_block_rate <= 0.5:
            robust.append(gate)
    if robust:
        notes.append(f"Candidats robustes a etudier avant cloture: {', '.join(sorted(robust))}.")
        return "keep_open_promotable_guardrail_candidate", notes
    notes.append(
        "Aucun gate stale/dislocation teste ne justifie une promotion active sur les deux fenetres."
    )
    return "close_p103_data_restored_no_guardrail_promoted", notes


def _render_markdown(report: ValidationReport) -> str:
    lines = [
        "# P1-03 Pod C external reference validation\n\n",
        f"- generated_at: `{report.generated_at}`\n",
        f"- interval: `{report.interval}`\n",
        f"- recommendation: `{report.recommendation}`\n",
        "\n",
        "## Coverage\n\n",
        "| window | trades | base pnl | ref coverage | median age | max age | max abs premium |\n",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |\n",
    ]
    for row in report.window_summaries:
        lines.append(
            f"| {row.window} | {row.trade_count} | {row.base_pnl_usd:.2f} | "
            f"{row.reference_coverage_pct:.2f}% | {row.median_age_seconds} | "
            f"{row.max_age_seconds} | {row.max_abs_premium_bps} |\n"
        )
    lines.extend(
        [
            "\n",
            "## Gate Counterfactuals\n\n",
            "| window | gate | kept pnl | delta | blocked | blocked pnl | winners/losers | reason mix | symbols |\n",
            "| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |\n",
        ]
    )
    for row in report.gate_outcomes:
        reason_mix = (
            f"missing={row.missing_reference_blocks}, stale={row.stale_blocks}, "
            f"premium={row.premium_blocks}, momentum={row.momentum_blocks}"
        )
        lines.append(
            f"| {row.window} | {row.gate} | {row.kept_pnl_usd:.2f} | "
            f"{row.delta_usd:.2f} | {row.blocked_trades}/{row.total_trades} | "
            f"{row.blocked_pnl_usd:.2f} | {row.blocked_winners}/{row.blocked_losers} | "
            f"{reason_mix} | {row.blocked_symbols} |\n"
        )
    lines.extend(["\n", "## Notes\n\n"])
    for note in report.notes:
        lines.append(f"- {note}\n")
    return "".join(lines)


def run_validation(
    *,
    report_paths: list[Path],
    output_dir: Path,
    cache_dir: Path,
    interval: str,
    timeout_seconds: float,
) -> ValidationReport:
    trades_by_window: dict[str, list[dict[str, object]]] = {}
    input_by_window: dict[str, Path] = {}
    for report_path in report_paths:
        window, trades = _load_pod_c_trades(report_path)
        trades_by_window[window] = trades
        input_by_window[window] = report_path
    indexes = _build_quote_indexes(
        trades_by_window,
        interval=interval,
        cache_dir=cache_dir,
        timeout_seconds=timeout_seconds,
    )
    enriched = _enrich_trades(trades_by_window, indexes)
    enriched_by_window: dict[str, list[EnrichedTrade]] = {}
    for trade in enriched:
        enriched_by_window.setdefault(trade.window, []).append(trade)
    gates = [
        "data_only",
        "missing_or_stale_15m",
        "missing_or_stale_60m",
        "abs_premium_gt_50",
        "abs_premium_gt_100",
        "long_chase_premium_gt_25",
        "long_chase_premium_gt_50",
        "counter_momentum_5m_6bps",
        "candidate_default_5m",
        "candidate_loose_5m",
    ]
    summaries = [
        _window_summary(window, input_by_window[window], enriched_by_window.get(window, []))
        for window in trades_by_window
    ]
    outcomes = [
        _evaluate_gate(window, enriched_by_window.get(window, []), gate)
        for window in trades_by_window
        for gate in gates
    ]
    recommendation, notes = _recommendation(outcomes, summaries)
    report = ValidationReport(
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        interval=interval,
        input_reports=[str(path) for path in report_paths],
        window_summaries=summaries,
        gate_outcomes=outcomes,
        recommendation=recommendation,
        notes=notes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "p103_pod_c_external_reference_validation.json"
    md_path = output_dir / "p103_pod_c_external_reference_validation.md"
    json_path.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate whether P1-03 Pod C external reference guardrails are promotable.",
    )
    parser.add_argument(
        "--report",
        action="append",
        dest="reports",
        help="Full-bot replay JSON containing pod_c.closed_trade_log. Can be repeated.",
    )
    parser.add_argument(
        "--output-dir",
        default="server-data/replay_reports/p103_pod_c_external_reference_validation_20260615",
    )
    parser.add_argument("--cache-dir", default="server-data/external_reference_yahoo")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report_paths = [Path(item) for item in args.reports] if args.reports else DEFAULT_REPORTS
    report = run_validation(
        report_paths=report_paths,
        output_dir=Path(args.output_dir),
        cache_dir=Path(args.cache_dir),
        interval=args.interval,
        timeout_seconds=args.timeout_seconds,
    )
    print(f"recommendation={report.recommendation}")
    for summary in report.window_summaries:
        print(
            f"{summary.window}: trades={summary.trade_count} "
            f"base_pnl={summary.base_pnl_usd} coverage={summary.reference_coverage_pct:.2f}%"
        )
    print(f"summary_path={Path(args.output_dir) / 'p103_pod_c_external_reference_validation.md'}")


if __name__ == "__main__":
    main()
