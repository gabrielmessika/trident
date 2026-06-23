#!/usr/bin/env python3
"""P120 / C-PNL-03 CL/BRENTOIL relative-value audit.

Research-only. Reads Pod C live logs, extracts P109 oil shadow observations, and
tests whether requiring both CL and BRENTOIL to confirm the shadow improves a
fixed-horizon short proxy.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_p105_a_grade_replay import parse_timestamp, utc_stamp


OIL_SYMBOLS = {"XYZ:CL", "XYZ:BRENTOIL"}


@dataclass(slots=True)
class OilObservation:
    observation_id: str
    timestamp: datetime
    symbol: str
    price: float
    would_open: bool
    research_regime: str
    hour_utc: int | None
    score: float
    reason: str
    external_premium_bps: float | None
    external_momentum_300s_bps: float | None


@dataclass(slots=True)
class CandidateRow:
    observation_id: str
    timestamp: str
    symbol: str
    cohort: str
    deduped_240m: bool
    entry_price: float
    exit_price_240m: float | None
    short_return_240m_bps: float | None
    proxy_pnl_usd: float | None
    pair_symbols_present: int
    pair_would_open_count: int
    pair_confirmed: bool
    research_regime: str
    external_premium_bps: float | None
    external_momentum_300s_bps: float | None


@dataclass(slots=True)
class SummaryRow:
    cohort: str
    deduped_only: bool
    candidates: int
    maturated: int
    proxy_pnl_usd: float
    win_rate: float | None
    profit_factor: float | None
    avg_return_bps: float | None
    by_symbol: dict[str, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod-c-log", default="server-data/logs/pod_c_live.jsonl")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--horizon-min", type=int, default=240)
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--roundtrip-fee-bps", type=float, default=7.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p120_oil_relative_value_{utc_stamp()}")
    output_dir.mkdir(parents=True, exist_ok=True)
    observations = load_observations(Path(args.pod_c_log))
    candidates = build_candidate_rows(
        observations,
        horizon=timedelta(minutes=int(args.horizon_min)),
        notional_usd=float(args.notional_usd),
        roundtrip_fee_bps=float(args.roundtrip_fee_bps),
    )
    summaries = summarize_candidates(candidates)
    payload = {
        "generated_at": utc_stamp(),
        "decision": "research_only_no_live_change",
        "pod_c_log": str(args.pod_c_log),
        "parameters": {
            "horizon_min": int(args.horizon_min),
            "notional_usd": float(args.notional_usd),
            "roundtrip_fee_bps": float(args.roundtrip_fee_bps),
        },
        "observations": len(observations),
        "summary": [asdict(row) for row in summaries],
    }
    write_csv(output_dir / "oil_relative_value_candidates.csv", candidates)
    write_csv(output_dir / "scenario_summary.csv", summaries)
    write_json(output_dir / "p120_oil_relative_value_audit.json", payload)
    write_markdown(output_dir / "p120_oil_relative_value_audit.md", payload, summaries)
    print(output_dir)


def load_observations(path: Path) -> list[OilObservation]:
    by_key: dict[tuple[str, str], OilObservation] = {}
    for record in jsonl_records(path):
        event_type = str(record.get("event_type") or "")
        if event_type not in {"signal", "signal_review"}:
            continue
        item = record.get("signal") if event_type == "signal" else record.get("review")
        if not isinstance(item, dict):
            continue
        timestamp = parse_timestamp(str(record.get("timestamp") or ""))
        snapshot = record.get("symbol_snapshot") if isinstance(record.get("symbol_snapshot"), dict) else {}
        details = combine_details(item.get("setup_details"), item.get("p109_oil_shadow"))
        if timestamp is None or details.get("p109_oil_shadow_mode") != "observation_only":
            continue
        symbol = str(item.get("symbol") or snapshot.get("symbol") or details.get("p109_oil_symbol") or "").upper()
        if symbol not in OIL_SYMBOLS:
            continue
        price = float(snapshot.get("price") or 0.0)
        if price <= 0.0:
            continue
        key = (timestamp.isoformat(), symbol)
        by_key[key] = OilObservation(
            observation_id=f"obs_{len(by_key) + 1:06d}",
            timestamp=timestamp,
            symbol=symbol,
            price=price,
            would_open=details.get("would_open_p109_oil_short_shadow") is True,
            research_regime=str(details.get("p109_oil_shadow_research_regime") or ""),
            hour_utc=optional_int(details.get("p109_oil_shadow_hour_utc")),
            score=float(details.get("p109_oil_shadow_score") or 0.0),
            reason=str(details.get("p109_oil_shadow_reason") or ""),
            external_premium_bps=optional_float(snapshot.get("external_premium_bps")),
            external_momentum_300s_bps=optional_float(snapshot.get("external_momentum_300s_bps")),
        )
    return sorted(by_key.values(), key=lambda row: (row.timestamp, row.symbol))


def build_candidate_rows(
    observations: list[OilObservation],
    *,
    horizon: timedelta,
    notional_usd: float,
    roundtrip_fee_bps: float,
) -> list[CandidateRow]:
    series = price_series(observations)
    by_timestamp: dict[datetime, list[OilObservation]] = defaultdict(list)
    for row in observations:
        by_timestamp[row.timestamp].append(row)
    rows: list[CandidateRow] = []
    last_deduped_by_symbol: dict[str, datetime] = {}
    for row in observations:
        if not row.would_open:
            continue
        pair = by_timestamp.get(row.timestamp, [])
        pair_would_open = sum(1 for item in pair if item.would_open)
        pair_confirmed = pair_would_open >= 2
        cohort = "pair_confirmed" if pair_confirmed else "solo_confirmed"
        deduped = row.timestamp >= last_deduped_by_symbol.get(row.symbol, datetime.min.replace(tzinfo=timezone.utc))
        if deduped:
            last_deduped_by_symbol[row.symbol] = row.timestamp + horizon
        exit_price = future_price(series.get(row.symbol, []), row.timestamp + horizon)
        ret_bps = short_return_bps(row.price, exit_price) if exit_price is not None else None
        proxy_pnl = (
            notional_usd * ((ret_bps or 0.0) - roundtrip_fee_bps) / 10_000.0
            if ret_bps is not None
            else None
        )
        rows.append(
            CandidateRow(
                observation_id=row.observation_id,
                timestamp=isoformat(row.timestamp),
                symbol=row.symbol,
                cohort=cohort,
                deduped_240m=deduped,
                entry_price=round(row.price, 8),
                exit_price_240m=round(exit_price, 8) if exit_price is not None else None,
                short_return_240m_bps=round(ret_bps, 6) if ret_bps is not None else None,
                proxy_pnl_usd=round(proxy_pnl, 6) if proxy_pnl is not None else None,
                pair_symbols_present=len({item.symbol for item in pair}),
                pair_would_open_count=pair_would_open,
                pair_confirmed=pair_confirmed,
                research_regime=row.research_regime,
                external_premium_bps=row.external_premium_bps,
                external_momentum_300s_bps=row.external_momentum_300s_bps,
            )
        )
    return rows


def summarize_candidates(rows: list[CandidateRow]) -> list[SummaryRow]:
    summaries: list[SummaryRow] = []
    cohorts = ["all_would_open", "pair_confirmed", "solo_confirmed"]
    for deduped_only in (False, True):
        scoped = [row for row in rows if row.deduped_240m or not deduped_only]
        for cohort in cohorts:
            if cohort == "all_would_open":
                selected = scoped
            else:
                selected = [row for row in scoped if row.cohort == cohort]
            summaries.append(summarize(cohort, deduped_only, selected))
    return summaries


def summarize(cohort: str, deduped_only: bool, rows: list[CandidateRow]) -> SummaryRow:
    matured = [row for row in rows if row.proxy_pnl_usd is not None]
    pnls = [float(row.proxy_pnl_usd or 0.0) for row in matured]
    returns = [float(row.short_return_240m_bps or 0.0) for row in matured if row.short_return_240m_bps is not None]
    by_symbol: dict[str, int] = {}
    for row in rows:
        by_symbol[row.symbol] = by_symbol.get(row.symbol, 0) + 1
    return SummaryRow(
        cohort=cohort,
        deduped_only=deduped_only,
        candidates=len(rows),
        maturated=len(matured),
        proxy_pnl_usd=round(sum(pnls), 6),
        win_rate=round(sum(1 for pnl in pnls if pnl > 0.0) / len(pnls), 6) if pnls else None,
        profit_factor=profit_factor(pnls),
        avg_return_bps=round(sum(returns) / len(returns), 6) if returns else None,
        by_symbol=dict(sorted(by_symbol.items())),
    )


def price_series(observations: list[OilObservation]) -> dict[str, list[tuple[datetime, float]]]:
    series: dict[str, dict[datetime, float]] = defaultdict(dict)
    for row in observations:
        series[row.symbol][row.timestamp] = row.price
    return {
        symbol: sorted(points.items(), key=lambda item: item[0])
        for symbol, points in series.items()
    }


def future_price(series: list[tuple[datetime, float]], target: datetime) -> float | None:
    timestamps = [row[0] for row in series]
    index = bisect.bisect_left(timestamps, target)
    if index >= len(series):
        return None
    return series[index][1]


def short_return_bps(entry: float, exit_price: float | None) -> float | None:
    if exit_price is None or entry <= 0.0:
        return None
    return (entry - exit_price) / entry * 10_000.0


def combine_details(*sources: object) -> dict[str, Any]:
    combined: dict[str, Any] = {}
    for source in sources:
        if isinstance(source, dict):
            combined.update(source)
    return combined


def profit_factor(pnls: list[float]) -> float | None:
    gains = sum(pnl for pnl in pnls if pnl > 0.0)
    losses = -sum(pnl for pnl in pnls if pnl < 0.0)
    if losses <= 0.0:
        return None
    return round(gains / losses, 6)


def optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def write_csv(path: Path, rows: list[Any]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any], summaries: list[SummaryRow]) -> None:
    lines = [
        "# P120 oil relative-value audit",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        "- decision: `research_only_no_live_change`",
        f"- pod_c_log: `{payload.get('pod_c_log')}`",
        f"- observations: `{payload.get('observations')}`",
        f"- parameters: `{payload.get('parameters')}`",
        "",
        "## Summary",
        "",
        "| Cohort | Deduped | Candidates | Matured | Proxy PnL | PF | WR | Avg ret bps | By symbol |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summaries:
        lines.append(
            f"| `{row.cohort}` | `{row.deduped_only}` | {row.candidates} | {row.maturated} | "
            f"{row.proxy_pnl_usd:.2f} | {fmt(row.profit_factor)} | {fmt(row.win_rate)} | "
            f"{fmt(row.avg_return_bps)} | `{row.by_symbol}` |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- Proxy short 240m research-only avec fees roundtrip fixes; aucun ordre live.",
            "- `pair_confirmed` exige que CL et BRENTOIL confirment le shadow au meme timestamp.",
            "- `deduped=True` garde au plus un candidat par symbole et horizon 240m.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    main()
