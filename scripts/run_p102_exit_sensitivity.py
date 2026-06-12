#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_RECENT_REPLAY_REPORT = (
    "server-data/replay_reports/p101_recent_full_bot_livecap_20260612T170415Z/"
    "full_bot_replay_current_config.json"
)
DEFAULT_RECENT_SNAPSHOTS = "server-data/live_snapshots"


@dataclass(slots=True)
class TradeSpec:
    trade_id: str
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    target_notional_usd: float
    stop_bps: float
    time_stop_hours: int
    take_profit_bps: float
    break_even_trigger_bps: float
    trailing_activation_bps: float
    trailing_distance_bps: float
    opened_at: datetime
    original_closed_at: datetime | None
    original_pnl_usd: float
    original_close_reason: str
    setup_details: dict[str, Any]


@dataclass(slots=True)
class SnapshotPoint:
    timestamp: datetime
    price: float
    spread_bps: float
    structure_score: float
    vwap_distance_bps: float
    btc_aligned: bool


@dataclass(frozen=True, slots=True)
class VariantSpec:
    grace_minutes: int
    cat_stop_max_bps: float
    early_failure_enabled: bool

    @property
    def name(self) -> str:
        efe = "efe_on" if self.early_failure_enabled else "efe_off"
        return f"grace{self.grace_minutes}_cat{_fmt_bps(self.cat_stop_max_bps)}_{efe}"


@dataclass(slots=True)
class SimulatedTrade:
    variant: str
    trade_id: str
    symbol: str
    side: str
    setup: str
    opened_at: str
    closed_at: str | None
    close_reason: str
    entry_price: float
    exit_price: float | None
    target_notional_usd: float
    stop_bps: float
    planned_loss_usd: float
    gross_pnl_usd: float
    fees_usd: float
    pnl_usd: float
    original_pnl_usd: float
    delta_vs_original_usd: float
    mfe_bps: float
    mae_bps: float
    excess_loss_vs_stop_usd: float
    grace_minutes: int
    cat_stop_max_bps: float
    early_failure_enabled: bool


@dataclass(slots=True)
class VariantSummary:
    variant: str
    grace_minutes: int
    cat_stop_max_bps: float
    early_failure_enabled: bool
    trade_count: int
    pnl_usd: float
    original_pnl_usd: float
    delta_vs_original_usd: float
    win_rate: float | None
    profit_factor: float | None
    fees_usd: float
    excess_loss_vs_stop_usd: float
    avg_mfe_bps: float
    avg_mae_bps: float
    close_reasons: dict[str, int]


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


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_pod_a_trades(report_path: Path) -> list[TradeSpec]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    pod_a = payload.get("pod_a")
    if not isinstance(pod_a, dict):
        raise ValueError(f"{report_path}: missing pod_a payload")
    trades: list[TradeSpec] = []
    for index, row in enumerate(pod_a.get("closed_trade_log", []) or []):
        if not isinstance(row, dict):
            continue
        opened_at = parse_timestamp(row.get("opened_at"))
        if opened_at is None:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        setup = str(row.get("setup") or "").strip()
        if setup != "trend_pullback_long":
            continue
        closed_at = parse_timestamp(row.get("closed_at"))
        trade_id = "|".join(
            [
                symbol,
                str(row.get("side") or ""),
                opened_at.isoformat(),
                str(row.get("close_reason") or ""),
                str(index),
            ]
        )
        trades.append(
            TradeSpec(
                trade_id=trade_id,
                symbol=symbol,
                side=str(row.get("side") or "long"),
                setup=setup,
                confidence=_float(row.get("confidence")),
                entry_price=_float(row.get("entry_price")),
                target_notional_usd=_float(row.get("target_notional_usd")),
                stop_bps=_float(row.get("stop_bps")),
                time_stop_hours=int(_float(row.get("time_stop_hours"))),
                take_profit_bps=_float(row.get("take_profit_bps")),
                break_even_trigger_bps=_float(row.get("break_even_trigger_bps")),
                trailing_activation_bps=_float(row.get("trailing_activation_bps")),
                trailing_distance_bps=_float(row.get("trailing_distance_bps")),
                opened_at=opened_at,
                original_closed_at=closed_at,
                original_pnl_usd=_float(row.get("pnl_usd")),
                original_close_reason=str(row.get("close_reason") or ""),
                setup_details=dict(row.get("setup_details") or {}),
            )
        )
    return trades


def load_snapshot_index(
    snapshot_input: Path,
    *,
    symbols: set[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[SnapshotPoint]]:
    index: dict[str, list[SnapshotPoint]] = {symbol: [] for symbol in sorted(symbols)}
    for file_path in _input_files(snapshot_input):
        with file_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                timestamp = parse_timestamp(payload.get("timestamp"))
                if timestamp is None or timestamp < start or timestamp > end:
                    continue
                raw_symbols = payload.get("symbols", [])
                if not isinstance(raw_symbols, list):
                    continue
                for item in raw_symbols:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol") or "").strip().upper()
                    if symbol not in index:
                        continue
                    price = _float(item.get("price"))
                    if price <= 0:
                        continue
                    index[symbol].append(
                        SnapshotPoint(
                            timestamp=timestamp,
                            price=price,
                            spread_bps=max(_float(item.get("spread_bps")), 0.0),
                            structure_score=_float(item.get("structure_score")),
                            vwap_distance_bps=_float(item.get("vwap_distance_bps")),
                            btc_aligned=bool(item.get("btc_aligned", True)),
                        )
                    )
    for points in index.values():
        points.sort(key=lambda point: point.timestamp)
    return index


def run_sensitivity(
    *,
    trades: list[TradeSpec],
    snapshot_index: dict[str, list[SnapshotPoint]],
    variants: list[VariantSpec],
    taker_fee_bps: float,
    dry_run_slippage_bps: float,
    dry_run_spread_multiplier: float,
) -> tuple[list[VariantSummary], list[SimulatedTrade]]:
    all_rows: list[SimulatedTrade] = []
    summaries: list[VariantSummary] = []
    original_total = round(sum(trade.original_pnl_usd for trade in trades), 6)
    for variant in variants:
        rows = [
            simulate_trade(
                trade,
                snapshot_index.get(trade.symbol, []),
                variant,
                taker_fee_bps=taker_fee_bps,
                dry_run_slippage_bps=dry_run_slippage_bps,
                dry_run_spread_multiplier=dry_run_spread_multiplier,
            )
            for trade in trades
        ]
        all_rows.extend(rows)
        summaries.append(summarize_variant(variant, rows, original_total=original_total))
    summaries.sort(key=lambda item: item.pnl_usd, reverse=True)
    return summaries, all_rows


def simulate_trade(
    trade: TradeSpec,
    points: list[SnapshotPoint],
    variant: VariantSpec,
    *,
    taker_fee_bps: float,
    dry_run_slippage_bps: float,
    dry_run_spread_multiplier: float,
) -> SimulatedTrade:
    planned_loss_usd = trade.target_notional_usd * max(trade.stop_bps, 0.0) / 10_000.0
    best_price = trade.entry_price
    mfe_bps = 0.0
    mae_bps = 0.0
    last_seen: SnapshotPoint | None = None
    for point in points:
        if point.timestamp < trade.opened_at:
            continue
        last_seen = point
        favorable_bps = _favorable_bps(trade.side, trade.entry_price, point.price)
        mfe_bps = max(mfe_bps, favorable_bps)
        mae_bps = max(mae_bps, -favorable_bps)
        best_price = _best_price(trade.side, best_price, point.price)
        best_favorable_bps = _favorable_bps(trade.side, trade.entry_price, best_price)
        age_minutes = (point.timestamp - trade.opened_at).total_seconds() / 60.0
        in_grace = 0.0 <= age_minutes < variant.grace_minutes

        reason = _protective_exit_reason(
            trade=trade,
            point=point,
            favorable_bps=favorable_bps,
            best_favorable_bps=best_favorable_bps,
            age_minutes=age_minutes,
            in_grace=in_grace,
            variant=variant,
        )
        if reason is not None:
            return _close_trade(
                trade=trade,
                point=point,
                variant=variant,
                reason=reason,
                planned_loss_usd=planned_loss_usd,
                mfe_bps=mfe_bps,
                mae_bps=mae_bps,
                taker_fee_bps=taker_fee_bps,
                dry_run_slippage_bps=dry_run_slippage_bps,
                dry_run_spread_multiplier=dry_run_spread_multiplier,
            )

    if last_seen is None:
        return SimulatedTrade(
            variant=variant.name,
            trade_id=trade.trade_id,
            symbol=trade.symbol,
            side=trade.side,
            setup=trade.setup,
            opened_at=trade.opened_at.isoformat(),
            closed_at=None,
            close_reason="missing_snapshot_path",
            entry_price=trade.entry_price,
            exit_price=None,
            target_notional_usd=trade.target_notional_usd,
            stop_bps=trade.stop_bps,
            planned_loss_usd=round(planned_loss_usd, 6),
            gross_pnl_usd=0.0,
            fees_usd=0.0,
            pnl_usd=0.0,
            original_pnl_usd=trade.original_pnl_usd,
            delta_vs_original_usd=round(-trade.original_pnl_usd, 6),
            mfe_bps=0.0,
            mae_bps=0.0,
            excess_loss_vs_stop_usd=0.0,
            grace_minutes=variant.grace_minutes,
            cat_stop_max_bps=variant.cat_stop_max_bps,
            early_failure_enabled=variant.early_failure_enabled,
        )

    return _close_trade(
        trade=trade,
        point=last_seen,
        variant=variant,
        reason="end_of_window",
        planned_loss_usd=planned_loss_usd,
        mfe_bps=mfe_bps,
        mae_bps=mae_bps,
        taker_fee_bps=taker_fee_bps,
        dry_run_slippage_bps=dry_run_slippage_bps,
        dry_run_spread_multiplier=dry_run_spread_multiplier,
    )


def _protective_exit_reason(
    *,
    trade: TradeSpec,
    point: SnapshotPoint,
    favorable_bps: float,
    best_favorable_bps: float,
    age_minutes: float,
    in_grace: bool,
    variant: VariantSpec,
) -> str | None:
    if trade.take_profit_bps > 0 and favorable_bps >= trade.take_profit_bps:
        return "take_profit_hit"
    if (
        trade.trailing_activation_bps > 0
        and trade.trailing_distance_bps > 0
        and best_favorable_bps >= trade.trailing_activation_bps
        and favorable_bps <= best_favorable_bps - trade.trailing_distance_bps
    ):
        return "trailing_stop"
    if (
        trade.break_even_trigger_bps > 0
        and best_favorable_bps >= trade.break_even_trigger_bps
        and favorable_bps <= 0.0
    ):
        return "break_even_stop"

    if variant.early_failure_enabled and in_grace and _early_failure_hit(
        trade=trade,
        point=point,
        favorable_bps=favorable_bps,
        age_minutes=age_minutes,
    ):
        return "early_failure_exit"

    stop_threshold_bps = (
        _catastrophic_stop_bps(trade.stop_bps, variant.cat_stop_max_bps)
        if in_grace
        else trade.stop_bps
    )
    if _adverse_bps(favorable_bps) >= max(stop_threshold_bps, 0.0):
        return "catastrophic_stop" if in_grace else "stop_hit"
    if age_minutes >= max(trade.time_stop_hours, 0) * 60:
        return "time_stop"
    return None


def _early_failure_hit(
    *,
    trade: TradeSpec,
    point: SnapshotPoint,
    favorable_bps: float,
    age_minutes: float,
) -> bool:
    if age_minutes < 10 or age_minutes > 90:
        return False
    adverse_bps = _adverse_bps(favorable_bps)
    threshold_bps = max(trade.stop_bps * 0.55, 25.0)
    if adverse_bps < threshold_bps:
        return False
    if point.structure_score <= 0.20:
        return True
    if point.vwap_distance_bps <= -8.0:
        return True
    return not point.btc_aligned


def _catastrophic_stop_bps(stop_bps: float, max_bps: float) -> float:
    planned = max(stop_bps, 0.0)
    dynamic = max(planned * 2.0, planned + 35.0)
    if max_bps > 0:
        dynamic = min(dynamic, max_bps)
    return round(max(planned, dynamic), 4)


def _close_trade(
    *,
    trade: TradeSpec,
    point: SnapshotPoint,
    variant: VariantSpec,
    reason: str,
    planned_loss_usd: float,
    mfe_bps: float,
    mae_bps: float,
    taker_fee_bps: float,
    dry_run_slippage_bps: float,
    dry_run_spread_multiplier: float,
) -> SimulatedTrade:
    exit_price = _exit_fill_price(
        side=trade.side,
        mid_price=point.price,
        spread_bps=point.spread_bps,
        dry_run_slippage_bps=dry_run_slippage_bps,
        dry_run_spread_multiplier=dry_run_spread_multiplier,
    )
    gross = _gross_pnl_usd(trade, exit_price)
    fees = round(trade.target_notional_usd * taker_fee_bps / 10_000.0 * 2.0, 6)
    pnl = round(gross - fees, 2)
    excess = max(abs(min(pnl, 0.0)) - planned_loss_usd, 0.0)
    return SimulatedTrade(
        variant=variant.name,
        trade_id=trade.trade_id,
        symbol=trade.symbol,
        side=trade.side,
        setup=trade.setup,
        opened_at=trade.opened_at.isoformat(),
        closed_at=point.timestamp.isoformat(),
        close_reason=reason,
        entry_price=round(trade.entry_price, 8),
        exit_price=round(exit_price, 8),
        target_notional_usd=round(trade.target_notional_usd, 6),
        stop_bps=round(trade.stop_bps, 4),
        planned_loss_usd=round(planned_loss_usd, 6),
        gross_pnl_usd=round(gross, 6),
        fees_usd=fees,
        pnl_usd=pnl,
        original_pnl_usd=round(trade.original_pnl_usd, 6),
        delta_vs_original_usd=round(pnl - trade.original_pnl_usd, 6),
        mfe_bps=round(mfe_bps, 4),
        mae_bps=round(mae_bps, 4),
        excess_loss_vs_stop_usd=round(excess, 6),
        grace_minutes=variant.grace_minutes,
        cat_stop_max_bps=variant.cat_stop_max_bps,
        early_failure_enabled=variant.early_failure_enabled,
    )


def summarize_variant(
    variant: VariantSpec,
    rows: list[SimulatedTrade],
    *,
    original_total: float,
) -> VariantSummary:
    pnl = round(sum(row.pnl_usd for row in rows), 6)
    wins = sum(1 for row in rows if row.pnl_usd >= 0)
    gross_positive = sum(row.pnl_usd for row in rows if row.pnl_usd > 0)
    gross_negative = abs(sum(row.pnl_usd for row in rows if row.pnl_usd < 0))
    return VariantSummary(
        variant=variant.name,
        grace_minutes=variant.grace_minutes,
        cat_stop_max_bps=variant.cat_stop_max_bps,
        early_failure_enabled=variant.early_failure_enabled,
        trade_count=len(rows),
        pnl_usd=pnl,
        original_pnl_usd=round(original_total, 6),
        delta_vs_original_usd=round(pnl - original_total, 6),
        win_rate=round(wins / len(rows), 4) if rows else None,
        profit_factor=round(gross_positive / gross_negative, 4) if gross_negative > 0 else None,
        fees_usd=round(sum(row.fees_usd for row in rows), 6),
        excess_loss_vs_stop_usd=round(sum(row.excess_loss_vs_stop_usd for row in rows), 6),
        avg_mfe_bps=round(_avg(row.mfe_bps for row in rows), 4),
        avg_mae_bps=round(_avg(row.mae_bps for row in rows), 4),
        close_reasons=dict(sorted(Counter(row.close_reason for row in rows).items())),
    )


def write_outputs(
    *,
    output_dir: Path,
    report_path: Path,
    snapshot_input: Path,
    summaries: list[VariantSummary],
    rows: list[SimulatedTrade],
    trade_count: int,
    snapshot_index: dict[str, list[SnapshotPoint]],
) -> tuple[Path, Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "variant_summary.csv"
    trades_csv = output_dir / "simulated_trades.csv"
    report_json = output_dir / "p102_exit_sensitivity_report.json"
    report_md = output_dir / "p102_exit_sensitivity_report.md"

    with summary_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "variant",
            "grace_minutes",
            "cat_stop_max_bps",
            "early_failure_enabled",
            "trade_count",
            "pnl_usd",
            "original_pnl_usd",
            "delta_vs_original_usd",
            "win_rate",
            "profit_factor",
            "fees_usd",
            "excess_loss_vs_stop_usd",
            "avg_mfe_bps",
            "avg_mae_bps",
            "close_reasons",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in summaries:
            payload = asdict(item)
            payload["close_reasons"] = json.dumps(item.close_reasons, sort_keys=True)
            writer.writerow(payload)

    with trades_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(asdict(rows[0]).keys()) if rows else list(SimulatedTrade.__slots__)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    payload = {
        "kind": "p102_exit_sensitivity",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inputs": {
            "replay_report": str(report_path),
            "snapshot_input": str(snapshot_input),
            "trade_count": trade_count,
            "symbols": {
                symbol: len(points)
                for symbol, points in sorted(snapshot_index.items())
            },
        },
        "summaries": [asdict(item) for item in summaries],
        "outputs": {
            "variant_summary_csv": str(summary_csv),
            "simulated_trades_csv": str(trades_csv),
            "report_json": str(report_json),
            "report_md": str(report_md),
        },
        "limits": [
            "Replay de sortie post-trade: les entrees sont celles du replay full-bot fourni.",
            "Les fills intra-minute ne sont pas observes; fermeture au prix snapshot minute avec modele dry-run.",
            "La simulation isole Pod A trend_pullback_long et ne rejoue pas le routing ni les nouvelles entrees apres une sortie modifiee.",
        ],
    }
    report_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    report_md.write_text(render_markdown(payload), encoding="utf-8")
    return summary_csv, trades_csv, report_json, report_md


def render_markdown(payload: dict[str, Any]) -> str:
    summaries = payload["summaries"]
    top = summaries[:8]
    current_like = next(
        (
            item
            for item in summaries
            if item["grace_minutes"] == 60
            and math.isclose(float(item["cat_stop_max_bps"]), 160.0)
            and bool(item["early_failure_enabled"])
        ),
        None,
    )
    no_efe_current = next(
        (
            item
            for item in summaries
            if item["grace_minutes"] == 60
            and math.isclose(float(item["cat_stop_max_bps"]), 160.0)
            and not bool(item["early_failure_enabled"])
        ),
        None,
    )
    lines = [
        "# P1-02 - Sensibilite exits Pod A",
        "",
        f"- Genere le: `{payload['generated_at']}`",
        f"- Replay source: `{payload['inputs']['replay_report']}`",
        f"- Snapshots source: `{payload['inputs']['snapshot_input']}`",
        f"- Trades Pod A trend_pullback_long: `{payload['inputs']['trade_count']}`",
        "",
    ]
    if current_like:
        lines.extend(
            [
                "## Variante proche config courante",
                "",
                _variant_sentence(current_like),
                "",
            ]
        )
    if current_like and no_efe_current:
        delta = float(current_like["pnl_usd"]) - float(no_efe_current["pnl_usd"])
        lines.extend(
            [
                f"- Effet EFE a grace 60 / cat 160: `{delta:.2f}` USD vs EFE off.",
                "",
            ]
        )
    lines.extend(
        [
            "## Top variantes",
            "",
            "| Variante | PnL | Delta vs original | WR | PF | Excess stop | Close reasons |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in top:
        lines.append(
            "| {variant} | {pnl:.2f} | {delta:.2f} | {wr} | {pf} | {excess:.2f} | `{reasons}` |".format(
                variant=item["variant"],
                pnl=float(item["pnl_usd"]),
                delta=float(item["delta_vs_original_usd"]),
                wr=_fmt_optional(item.get("win_rate")),
                pf=_fmt_optional(item.get("profit_factor")),
                excess=float(item["excess_loss_vs_stop_usd"]),
                reasons=json.dumps(item["close_reasons"], sort_keys=True),
            )
        )
    lines.extend(
        [
            "",
            "## Artefacts",
            "",
            f"- Summary CSV: `{payload['outputs']['variant_summary_csv']}`",
            f"- Trades simules CSV: `{payload['outputs']['simulated_trades_csv']}`",
            f"- JSON: `{payload['outputs']['report_json']}`",
            "",
            "## Limites",
            "",
            *[f"- {item}" for item in payload["limits"]],
            "",
        ]
    )
    return "\n".join(lines)


def _variant_sentence(item: dict[str, Any]) -> str:
    return (
        f"`{item['variant']}`: PnL `{float(item['pnl_usd']):.2f}` USD, "
        f"delta vs original `{float(item['delta_vs_original_usd']):.2f}`, "
        f"excess stop `{float(item['excess_loss_vs_stop_usd']):.2f}`."
    )


def _input_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.glob("*.jsonl"))


def _best_price(side: str, current_best: float, price: float) -> float:
    if current_best <= 0:
        return price
    if side == "long":
        return max(current_best, price)
    return min(current_best, price)


def _favorable_bps(side: str, entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    if side == "long":
        return (price - entry) / entry * 10_000.0
    return (entry - price) / entry * 10_000.0


def _adverse_bps(favorable_bps: float) -> float:
    return max(-favorable_bps, 0.0)


def _exit_fill_price(
    *,
    side: str,
    mid_price: float,
    spread_bps: float,
    dry_run_slippage_bps: float,
    dry_run_spread_multiplier: float,
) -> float:
    impact_bps = max(spread_bps, 0.0) * dry_run_spread_multiplier + dry_run_slippage_bps
    signed = -impact_bps if side == "long" else impact_bps
    return mid_price * (1.0 + signed / 10_000.0)


def _gross_pnl_usd(trade: TradeSpec, exit_price: float) -> float:
    if trade.entry_price <= 0:
        return 0.0
    if trade.side == "long":
        ret = (exit_price - trade.entry_price) / trade.entry_price
    else:
        ret = (trade.entry_price - exit_price) / trade.entry_price
    return round(trade.target_notional_usd * ret, 6)


def _avg(values: Iterable[float]) -> float:
    rows = list(values)
    if not rows:
        return 0.0
    return sum(rows) / len(rows)


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_bps(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value).replace(".", "p")


def _fmt_optional(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _parse_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _parse_floats(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def build_variants(args: argparse.Namespace) -> list[VariantSpec]:
    grace_values = _parse_ints(args.grace_minutes)
    cat_values = _parse_floats(args.cat_stop_max_bps)
    modes = {item.strip().lower() for item in args.early_failure_modes.split(",") if item.strip()}
    efe_values = []
    if "on" in modes:
        efe_values.append(True)
    if "off" in modes:
        efe_values.append(False)
    return [
        VariantSpec(
            grace_minutes=grace,
            cat_stop_max_bps=cat,
            early_failure_enabled=efe,
        )
        for grace in grace_values
        for cat in cat_values
        for efe in efe_values
    ]


def run(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    report_path = Path(args.replay_report)
    snapshot_input = Path(args.snapshot_input)
    output_dir = Path(args.output_dir) if args.output_dir else (
        Path(args.source_root) / "replay_reports" / f"p102_exit_sensitivity_{utc_stamp()}"
    )
    trades = load_pod_a_trades(report_path)
    if not trades:
        raise ValueError(f"{report_path}: no Pod A trend_pullback_long trades found")
    start = min(trade.opened_at for trade in trades)
    end_candidates = [trade.original_closed_at for trade in trades if trade.original_closed_at]
    end = max(end_candidates) if end_candidates else datetime.now(timezone.utc)
    snapshot_index = load_snapshot_index(
        snapshot_input,
        symbols={trade.symbol for trade in trades},
        start=start,
        end=end,
    )
    summaries, simulated = run_sensitivity(
        trades=trades,
        snapshot_index=snapshot_index,
        variants=build_variants(args),
        taker_fee_bps=float(args.taker_fee_bps),
        dry_run_slippage_bps=float(args.dry_run_slippage_bps),
        dry_run_spread_multiplier=float(args.dry_run_spread_multiplier),
    )
    return write_outputs(
        output_dir=output_dir,
        report_path=report_path,
        snapshot_input=snapshot_input,
        summaries=summaries,
        rows=simulated,
        trade_count=len(trades),
        snapshot_index=snapshot_index,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run P1-02 Pod A exit sensitivity on a full-bot replay trade log.",
    )
    parser.add_argument("--source-root", default="server-data")
    parser.add_argument("--replay-report", default=DEFAULT_RECENT_REPLAY_REPORT)
    parser.add_argument("--snapshot-input", default=DEFAULT_RECENT_SNAPSHOTS)
    parser.add_argument("--output-dir")
    parser.add_argument("--grace-minutes", default="0,60,120,165")
    parser.add_argument("--cat-stop-max-bps", default="120,160,220,300")
    parser.add_argument("--early-failure-modes", default="on,off")
    parser.add_argument("--taker-fee-bps", type=float, default=3.5)
    parser.add_argument("--dry-run-slippage-bps", type=float, default=0.5)
    parser.add_argument("--dry-run-spread-multiplier", type=float, default=0.5)
    return parser


def main() -> None:
    summary_csv, trades_csv, report_json, report_md = run(build_parser().parse_args())
    print(f"summary_csv={summary_csv}")
    print(f"simulated_trades_csv={trades_csv}")
    print(f"report_json={report_json}")
    print(f"report_md={report_md}")


if __name__ == "__main__":
    main()
