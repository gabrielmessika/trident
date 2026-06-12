#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backtest.pod_a_runner import PodABacktestResult, PodABacktestRunner
from app.settings import AppConfig, load_config


DEFAULT_BASELINE_INPUT = (
    "server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl"
)
DEFAULT_RECENT_INPUT = "server-data/replay_inputs/full_bot_live_window_20260524T1605_20260611_no_external_reference.jsonl"


@dataclass(slots=True)
class Scenario:
    name: str
    description: str
    allowed_setups: list[str]
    disabled_setups: list[str] | None = None


@dataclass(slots=True)
class ScenarioResult:
    window: str
    scenario: str
    description: str
    input_path: str
    records_processed: int
    signal_count: int
    accepted_count: int
    rejected_count: int
    closed_trade_count: int
    realized_pnl_usd: float
    fees_usd: float
    win_rate: float | None
    profit_factor: float | None
    delta_vs_current_usd: float | None
    signals_by_setup: dict[str, int]
    accepted_by_setup: dict[str, int]
    trades_by_setup: dict[str, int]
    pnl_by_setup: dict[str, float]
    trades_by_side: dict[str, int]
    pnl_by_side: dict[str, float]
    close_reasons: dict[str, int]
    runtime_seconds: float


SCENARIOS = [
    Scenario(
        name="current_long_only",
        description="Config Pod A courante, long-only.",
        allowed_setups=["trend_pullback_long"],
    ),
    Scenario(
        name="trend_pullback_short_on",
        description="Config courante + reactivation du miroir trend_pullback_short.",
        allowed_setups=["trend_pullback_long", "trend_pullback_short"],
    ),
    Scenario(
        name="short_only_experimental",
        description="Research only: trend_pullback_long coupe, trend_pullback_short seul.",
        allowed_setups=["trend_pullback_short"],
        disabled_setups=[
            "bos_retest_short",
            "bos_retest_long",
            "trend_pullback_long",
            "liquidity_sweep_reclaim_short",
            "liquidity_sweep_reclaim_long",
            "vwap_reclaim_long",
        ],
    ),
]


def scenario_config(base: AppConfig, scenario: Scenario) -> AppConfig:
    allowed = _dedupe(list(base.pod_a.allowed_setups) + list(scenario.allowed_setups))
    if scenario.name == "short_only_experimental":
        allowed = _dedupe(scenario.allowed_setups)
    disabled = (
        list(scenario.disabled_setups)
        if scenario.disabled_setups is not None
        else [item for item in base.pod_a.disabled_setups if item not in set(scenario.allowed_setups)]
    )
    return replace(
        base,
        pod_a=replace(
            base.pod_a,
            allowed_setups=allowed,
            disabled_setups=disabled,
        ),
    )


def run_scenarios(
    *,
    config_path: Path,
    input_path: Path,
    window: str,
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    current_pnl: float | None = None
    for scenario in SCENARIOS:
        config = scenario_config(load_config(str(config_path)), scenario)
        started = time.perf_counter()
        result = PodABacktestRunner(config).run_jsonl(input_path)
        runtime = time.perf_counter() - started
        if scenario.name == "current_long_only":
            current_pnl = float(result.realized_pnl_usd)
        results.append(
            build_result(
                window=window,
                scenario=scenario,
                input_path=input_path,
                result=result,
                current_pnl=current_pnl,
                runtime=runtime,
            )
        )
        print(
            f"window={window} scenario={scenario.name} pnl={result.realized_pnl_usd:.4f} "
            f"trades={result.closed_trade_count} seconds={runtime:.1f}",
            flush=True,
        )
    return results


def build_result(
    *,
    window: str,
    scenario: Scenario,
    input_path: Path,
    result: PodABacktestResult,
    current_pnl: float | None,
    runtime: float,
) -> ScenarioResult:
    wins = int(result.win_count)
    losses = int(result.loss_count)
    gross_wins = sum(
        float(row.get("pnl_usd", 0.0) or 0.0)
        for row in result.closed_trade_log
        if float(row.get("pnl_usd", 0.0) or 0.0) > 0
    )
    gross_losses = -sum(
        float(row.get("pnl_usd", 0.0) or 0.0)
        for row in result.closed_trade_log
        if float(row.get("pnl_usd", 0.0) or 0.0) < 0
    )
    pnl_by_side: dict[str, float] = {}
    trades_by_side: dict[str, int] = {}
    for row in result.closed_trade_log:
        side = str(row.get("side") or "unknown")
        trades_by_side[side] = trades_by_side.get(side, 0) + 1
        pnl_by_side[side] = pnl_by_side.get(side, 0.0) + float(row.get("pnl_usd", 0.0) or 0.0)
    return ScenarioResult(
        window=window,
        scenario=scenario.name,
        description=scenario.description,
        input_path=str(input_path),
        records_processed=int(result.records_processed),
        signal_count=int(result.signal_count),
        accepted_count=int(result.accepted_count),
        rejected_count=int(result.rejected_count),
        closed_trade_count=int(result.closed_trade_count),
        realized_pnl_usd=round(float(result.realized_pnl_usd), 6),
        fees_usd=round(float(result.fees_usd), 6),
        win_rate=(wins / (wins + losses) if wins + losses > 0 else None),
        profit_factor=(gross_wins / gross_losses if gross_losses > 0 else None),
        delta_vs_current_usd=(
            round(float(result.realized_pnl_usd) - current_pnl, 6)
            if current_pnl is not None
            else None
        ),
        signals_by_setup=dict(result.signals_by_setup),
        accepted_by_setup=dict(result.accepted_by_setup),
        trades_by_setup=dict(result.trades_by_setup),
        pnl_by_setup={key: round(float(value), 6) for key, value in result.pnl_by_setup.items()},
        trades_by_side=trades_by_side,
        pnl_by_side={key: round(value, 6) for key, value in pnl_by_side.items()},
        close_reasons=dict(result.close_reasons),
        runtime_seconds=round(runtime, 3),
    )


def write_markdown(path: Path, *, generated_at: str, rows: list[ScenarioResult]) -> None:
    lines = [
        "# P1-06 Pod A Short Replay",
        "",
        f"- Generated at: `{generated_at}`",
        "- Status: `research_only_no_live_change`",
        "",
        "| Window | Scenario | Trades | PnL | Delta vs current | WR | PF | Trades by setup | PnL by setup | Side PnL |",
        "|---|---|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.window}`",
                    f"`{row.scenario}`",
                    str(row.closed_trade_count),
                    f"{row.realized_pnl_usd:.2f}",
                    _fmt_float(row.delta_vs_current_usd),
                    _fmt_pct(row.win_rate),
                    _fmt_float(row.profit_factor),
                    _fmt_dict(row.trades_by_setup),
                    _fmt_dict(row.pnl_by_setup),
                    _fmt_dict(row.pnl_by_side),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Notes:",
            "",
            "- Pod A only: this does not replay Pod C or routing portfolio interactions.",
            "- The replay keeps current risk gates/blocklists unless explicitly changed by the scenario.",
            "- No live setup, side, cap, stop, or sizing is changed.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--recent-input", default=DEFAULT_RECENT_INPUT)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--skip-recent", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir) if args.output_dir else Path(
        f"server-data/replay_reports/p106_pod_a_short_replay_{generated_at}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[ScenarioResult] = []
    if not args.skip_baseline:
        rows.extend(
            run_scenarios(
                config_path=Path(args.config),
                input_path=Path(args.baseline_input),
                window="baseline_apr_may",
            )
        )
    if not args.skip_recent:
        rows.extend(
            run_scenarios(
                config_path=Path(args.config),
                input_path=Path(args.recent_input),
                window="recent_may_jun",
            )
        )
    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "results": [asdict(row) for row in rows],
    }
    (output_dir / "pod_a_short_replay.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "pod_a_short_replay.md", generated_at=generated_at, rows=rows)
    print(output_dir)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def _fmt_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _fmt_dict(values: dict[str, object]) -> str:
    if not values:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in values.items())


if __name__ == "__main__":
    main()
