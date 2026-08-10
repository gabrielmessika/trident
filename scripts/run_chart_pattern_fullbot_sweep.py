#!/usr/bin/env python3
"""Promotion-grade full-bot target/SL sweep for chart-pattern candidates.

Research-only: this script does not change live config, deploy anything, or send
orders. It reuses the comparable full-bot replay harness and evaluates one
target/stop profile per independent overlay executor in a single pass.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.execution.directional_executor import DirectionalExecutor
from app.settings import load_config
from scripts.run_chart_pattern_fullbot_comparable_replay import (
    DEFAULT_BASELINE_INPUT,
    DEFAULT_CASES_CSV,
    EvoSpec,
    EvoState,
    load_cases,
    run_fullbot_comparable,
)
from scripts import run_cup_handle_pattern_scan as cup


DEFAULT_OUTPUT_DIR = (
    "server-data/replay_reports/chart_pattern_fullbot_sweep_20260706T000000Z"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--cases-csv", default=DEFAULT_CASES_CSV)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--max-open-positions", type=int, default=1)
    parser.add_argument("--max-new-positions-per-bar", type=int, default=1)
    parser.add_argument("--max-spread-bps", type=float, default=10.0)
    parser.add_argument("--max-signal-lag-hours", type=float, default=12.0)
    parser.add_argument("--include-blocked-symbols", action="store_true")
    parser.add_argument("--apply-live-caps", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    blocked_symbols = set() if args.include_blocked_symbols else {
        str(symbol).upper()
        for symbol in getattr(config.hyperliquid, "tradable_blocked_symbols", [])
    }
    specs = sweep_specs()
    cases = load_cases(Path(args.cases_csv), specs=specs, blocked_symbols=blocked_symbols)
    states = [
        EvoState(
            spec=spec,
            executor=DirectionalExecutor(config),
            cases=sorted(cases.get(spec.name, []), key=lambda item: (cup.iso_ms(item.signal_time), item.symbol)),
            pending=deque(),
        )
        for spec in specs
    ]
    results = run_fullbot_comparable(
        config=config,
        input_path=Path(args.baseline_input),
        states=states,
        notional_usd=float(args.notional_usd),
        max_open_positions=int(args.max_open_positions),
        max_new_positions_per_bar=int(args.max_new_positions_per_bar),
        max_spread_bps=float(args.max_spread_bps),
        max_signal_lag_hours=float(args.max_signal_lag_hours),
        apply_live_caps=bool(args.apply_live_caps),
    )
    scenario_rows = [asdict(row) for row in results]
    ranked = rank_rows(scenario_rows)
    payload = {
        "kind": "chart_pattern_fullbot_sweep",
        "generated_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "status": "research_only_no_live_change",
        "inputs": {
            "config": str(args.config),
            "baseline_input": str(args.baseline_input),
            "cases_csv": str(args.cases_csv),
        },
        "method": {
            "blocked_symbols_excluded": sorted(blocked_symbols),
            "notional_usd": float(args.notional_usd),
            "max_open_positions": int(args.max_open_positions),
            "max_new_positions_per_bar": int(args.max_new_positions_per_bar),
            "max_spread_bps": float(args.max_spread_bps),
            "max_signal_lag_hours": float(args.max_signal_lag_hours),
            "live_caps": bool(args.apply_live_caps),
            "criteria": promotion_criteria(),
            "limits": [
                "Synthetic overlay sleeve, not live production code.",
                "One independent executor per target/SL profile.",
                "Signal enters on first replay snapshot after 4h candle close.",
            ],
        },
        "results": scenario_rows,
        "ranked": ranked,
    }
    write_csv(output_dir / "sweep_summary.csv", ranked)
    write_csv(output_dir / "scenario_summary.csv", scenario_rows)
    (output_dir / "fullbot_sweep.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "fullbot_sweep.md", payload)
    print(output_dir)


def sweep_specs() -> list[EvoSpec]:
    specs: list[EvoSpec] = []
    for target in (50.0, 75.0, 100.0):
        for stop in (4.0, 5.0, 6.0, 8.0):
            specs.append(
                EvoSpec(
                    name=f"double_bottom_t{fmt_grid(target)}_sl{fmt_grid(stop)}",
                    pattern="double_bottom",
                    filter_name="4h_target_low_q50_score_high_q50_volume_high_q50",
                    target_fraction_pct=target,
                    stop_loss_pct=stop,
                    description=f"Double bottom 4h filtre, target {target:g}%, SL {stop:g}%.",
                )
            )
    for target in (50.0, 66.0, 75.0):
        for stop in (8.0, 10.0, 12.0, 15.0):
            specs.append(
                EvoSpec(
                    name=f"triangle_breakout_t{fmt_grid(target)}_sl{fmt_grid(stop)}",
                    pattern="triangle_breakout",
                    filter_name="4h_target_low_q50_score_high_q50_volume_high_q50",
                    target_fraction_pct=target,
                    stop_loss_pct=stop,
                    description=f"Triangle breakout 4h filtre, target {target:g}%, SL {stop:g}%.",
                )
            )
    return specs


def rank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for row in rows:
        scenario = str(row.get("scenario", ""))
        if scenario == "current_ac":
            continue
        parsed = parse_scenario(scenario)
        overlay_pnl = float(row.get("overlay_pnl_usd", 0.0) or 0.0)
        trades = int(row.get("overlay_trades", 0) or 0)
        pf = as_float(row.get("overlay_profit_factor"))
        max_dd = float(row.get("overlay_max_drawdown_usd", 0.0) or 0.0)
        pass_gate = (
            overlay_pnl > 0.0
            and trades >= 10
            and pf is not None
            and pf >= 1.5
            and max_dd <= 25.0
            and not is_single_symbol_dependent(row)
        )
        ranked.append(
            {
                **parsed,
                "scenario": scenario,
                "pass_gate": pass_gate,
                "reason": gate_reason(row, pass_gate),
                "overlay_pnl_usd": overlay_pnl,
                "overlay_trades": trades,
                "win_rate_pct": as_float(row.get("overlay_win_rate_pct")),
                "profit_factor": pf,
                "max_drawdown_usd": max_dd,
                "baseline_fullbot_pnl_usd": float(row.get("baseline_ac_pnl_usd", 0.0) or 0.0),
                "total_fullbot_plus_overlay_pnl_usd": float(row.get("total_ac_plus_overlay_pnl_usd", 0.0) or 0.0),
                "signal_count": int(row.get("signal_count", 0) or 0),
                "opened_count": int(row.get("opened_count", 0) or 0),
                "skipped_baseline_overlap_count": int(row.get("skipped_baseline_overlap_count", 0) or 0),
                "close_reasons": row.get("close_reasons", {}),
                "avg_mfe_bps": as_float(row.get("avg_mfe_bps")),
                "avg_mae_bps": as_float(row.get("avg_mae_bps")),
                "top_symbol_share_pct": top_symbol_share_pct(row),
            }
        )
    return sorted(
        ranked,
        key=lambda item: (
            bool(item["pass_gate"]),
            float(item["overlay_pnl_usd"]),
            float(item["profit_factor"] or 0.0),
            -float(item["max_drawdown_usd"]),
        ),
        reverse=True,
    )


def promotion_criteria() -> dict[str, Any]:
    return {
        "overlay_pnl_usd": "> 0",
        "min_trades": 10,
        "min_profit_factor": 1.5,
        "max_drawdown_usd": 25.0,
        "single_symbol_dependency": "reject if one symbol carries >= 60% of positive PnL or trades",
    }


def gate_reason(row: dict[str, Any], pass_gate: bool) -> str:
    if pass_gate:
        return "passes_research_gate"
    reasons: list[str] = []
    pnl = float(row.get("overlay_pnl_usd", 0.0) or 0.0)
    trades = int(row.get("overlay_trades", 0) or 0)
    pf = as_float(row.get("overlay_profit_factor"))
    max_dd = float(row.get("overlay_max_drawdown_usd", 0.0) or 0.0)
    if pnl <= 0:
        reasons.append("pnl_non_positive")
    if trades < 10:
        reasons.append("sample_lt_10")
    if pf is None or pf < 1.5:
        reasons.append("pf_lt_1_5")
    if max_dd > 25:
        reasons.append("max_dd_gt_25")
    if is_single_symbol_dependent(row):
        reasons.append("single_symbol_dependent")
    return ",".join(reasons)


def is_single_symbol_dependent(row: dict[str, Any]) -> bool:
    top_share = top_symbol_share_pct(row)
    return top_share is not None and top_share >= 60.0


def top_symbol_share_pct(row: dict[str, Any]) -> float | None:
    trades_by_symbol = row.get("trades_by_symbol") or {}
    if isinstance(trades_by_symbol, str):
        return None
    total_trades = sum(int(value or 0) for value in trades_by_symbol.values())
    if total_trades <= 0:
        return None
    trade_share = 100.0 * max(int(value or 0) for value in trades_by_symbol.values()) / total_trades
    pnl_by_symbol = row.get("pnl_by_symbol") or {}
    pnl_share = 0.0
    if isinstance(pnl_by_symbol, dict):
        positive = [float(value or 0.0) for value in pnl_by_symbol.values() if float(value or 0.0) > 0.0]
        if positive and sum(positive) > 0.0:
            pnl_share = 100.0 * max(positive) / sum(positive)
    return round(max(trade_share, pnl_share), 6)


def parse_scenario(scenario: str) -> dict[str, Any]:
    if scenario.startswith("double_bottom_"):
        pattern = "double_bottom"
    elif scenario.startswith("triangle_breakout_"):
        pattern = "triangle_breakout"
    else:
        pattern = ""
    parts = scenario.split("_")
    target = next((part[1:] for part in parts if part.startswith("t") and part[1:].isdigit()), "")
    stop = next((part[2:] for part in parts if part.startswith("sl") and part[2:].isdigit()), "")
    return {
        "pattern": pattern,
        "target_fraction_pct": float(target) if target else None,
        "stop_loss_pct": float(stop) if stop else None,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    ranked = payload["ranked"]
    lines = [
        "# Chart pattern full-bot target/SL sweep",
        "",
        "Statut: `research_only_no_live_change`.",
        "",
        "## Methode",
        "",
        f"- Live caps: `{payload['method']['live_caps']}`.",
        f"- Max open overlay: `{payload['method']['max_open_positions']}`.",
        f"- Max new positions per bar: `{payload['method']['max_new_positions_per_bar']}`.",
        "- Symboles bloques live exclus par defaut.",
        "- Signal 4h au premier snapshot apres cloture de bougie.",
        "",
        "## Top profils",
        "",
        "| Rank | Pass | Pattern | Target | SL | Trades | PnL | Total | Win | PF | Max DD | Top symbol share | Reason |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for rank, row in enumerate(ranked[:20], start=1):
        lines.append(
            "| {rank} | {pass_gate} | `{pattern}` | {target:.0f}% | {stop:.0f}% | {trades} | {pnl:.2f} | {total:.2f} | {win} | {pf} | {dd:.2f} | {share} | `{reason}` |".format(
                rank=rank,
                pass_gate="O" if row["pass_gate"] else "N",
                pattern=row["pattern"],
                target=row["target_fraction_pct"],
                stop=row["stop_loss_pct"],
                trades=row["overlay_trades"],
                pnl=row["overlay_pnl_usd"],
                total=row["total_fullbot_plus_overlay_pnl_usd"],
                win=fmt_pct(row["win_rate_pct"]),
                pf=fmt_num(row["profit_factor"]),
                dd=row["max_drawdown_usd"],
                share=fmt_pct(row["top_symbol_share_pct"]),
                reason=row["reason"],
            )
        )
    pass_rows = [row for row in ranked if row["pass_gate"]]
    lines.extend([
        "",
        "## Decision gate",
        "",
        f"- Profils qui passent le gate recherche: `{len(pass_rows)}`.",
    ])
    if pass_rows:
        best = pass_rows[0]
        lines.append(
            "- Meilleur profil: `{pattern}` target `{target:.0f}%`, SL `{stop:.0f}%`, PnL `+{pnl:.2f}`.".format(
                pattern=best["pattern"],
                target=best["target_fraction_pct"],
                stop=best["stop_loss_pct"],
                pnl=best["overlay_pnl_usd"],
            )
        )
    else:
        lines.append("- Aucun profil ne passe tous les criteres de promotion recherche.")
    lines.extend([
        "",
        "## Fichiers",
        "",
        "- `sweep_summary.csv`",
        "- `scenario_summary.csv`",
        "- `fullbot_sweep.json`",
        "- `fullbot_sweep.md`",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def fmt_grid(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value).replace(".", "p")


def fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.3f}"


if __name__ == "__main__":
    main()
