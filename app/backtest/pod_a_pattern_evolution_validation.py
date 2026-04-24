from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class PatternEvolution:
    name: str
    description: str
    no_short_setups: list[str]
    shorts_on_setups: list[str]


@dataclass(slots=True)
class ScenarioSummary:
    name: str
    description: str
    mode: str
    setups: list[str]
    total_pnl_usd: float
    pod_a_pnl_usd: float
    pod_b_pnl_usd: float
    pod_c_pnl_usd: float
    total_delta_usd: float
    pod_a_delta_usd: float
    closed_trade_count: int
    pod_a_closed_trade_count: int
    setup_trades: dict[str, int]
    setup_pnl: dict[str, float]
    setup_signals: dict[str, int]
    runtime_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class EvolutionDecision:
    name: str
    verdict: str
    best_mode: str
    no_short_delta_usd: float
    shorts_on_delta_usd: float
    note: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


ACTIONABLE_EVOLUTIONS = [
    PatternEvolution(
        name="trend_pullback",
        description="Pod A trend pullback actuel; test de reintroduction du short miroir.",
        no_short_setups=["trend_pullback_long"],
        shorts_on_setups=["trend_pullback_long", "trend_pullback_short"],
    ),
    PatternEvolution(
        name="vwap_reclaim",
        description="Famille VWAP reclaim reperee dans la matrice coin-par-coin.",
        no_short_setups=["trend_pullback_long", "vwap_reclaim_long"],
        shorts_on_setups=[
            "trend_pullback_long",
            "vwap_reclaim_long",
            "vwap_reclaim_short",
        ],
    ),
    PatternEvolution(
        name="ichimoku_continuation",
        description="Famille Ichimoku continuation reperee surtout sur XRP/ARB/BNB/LTC.",
        no_short_setups=["trend_pullback_long", "ichimoku_continuation_long"],
        shorts_on_setups=[
            "trend_pullback_long",
            "ichimoku_continuation_long",
            "ichimoku_continuation_short",
        ],
    ),
]

NON_EXECUTABLE_PATTERN_FAMILIES = {
    "ema50_overextension_reversion": "actuellement implemente en veto BTC-only, pas en entree directionnelle Pod A generique",
    "funding_reversion": "pas de setup executable Pod A; necessite un moteur funding/reversion",
    "squeeze_breakout": "famille plutot Pod B; pas de setup Pod A equivalent strict",
    "ttm_squeeze_release": "famille plutot Pod B; pas de setup Pod A equivalent strict",
    "range_mean_reversion": "pas de setup mean-reversion Pod A",
    "stoch_cci_reversion": "pas de setup mean-reversion Pod A",
    "trend_breakout": "pas de setup Pod A equivalent strict sans redefinir la logique BOS/breakout",
}


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _scenario_config(base: AppConfig, setups: list[str]) -> AppConfig:
    enabled_setups = set(setups)
    allowed_setups = _dedupe(list(base.pod_a.allowed_setups) + list(setups))
    disabled_setups = [
        item for item in base.pod_a.disabled_setups if item not in enabled_setups
    ]
    return replace(
        base,
        pod_a=replace(
            base.pod_a,
            allowed_setups=allowed_setups,
            disabled_setups=disabled_setups,
        ),
    )


def _summary_from_result(
    *,
    name: str,
    description: str,
    mode: str,
    setups: list[str],
    result: FullBotBacktestResult,
    baseline: FullBotBacktestResult,
    runtime_seconds: float,
) -> ScenarioSummary:
    pod_a = result.pod_a
    pod_b = result.pod_b
    pod_c = result.pod_c
    baseline_pod_a = baseline.pod_a
    setup_trades = {
        setup: int((pod_a.get("trades_by_setup", {}) or {}).get(setup, 0) or 0)
        for setup in setups
    }
    setup_pnl = {
        setup: float((pod_a.get("pnl_by_setup", {}) or {}).get(setup, 0.0) or 0.0)
        for setup in setups
    }
    setup_signals = {
        setup: int((pod_a.get("signals_by_setup", {}) or {}).get(setup, 0) or 0)
        for setup in setups
    }
    return ScenarioSummary(
        name=name,
        description=description,
        mode=mode,
        setups=list(setups),
        total_pnl_usd=float(result.total_realized_pnl_usd),
        pod_a_pnl_usd=float(pod_a.get("realized_pnl_usd", 0.0) or 0.0),
        pod_b_pnl_usd=float(pod_b.get("realized_pnl_usd", 0.0) or 0.0),
        pod_c_pnl_usd=float(pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        total_delta_usd=round(
            float(result.total_realized_pnl_usd) - float(baseline.total_realized_pnl_usd),
            4,
        ),
        pod_a_delta_usd=round(
            float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
            - float(baseline_pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            4,
        ),
        closed_trade_count=int(result.total_activity_count),
        pod_a_closed_trade_count=int(pod_a.get("closed_trade_count", 0) or 0),
        setup_trades=setup_trades,
        setup_pnl=setup_pnl,
        setup_signals=setup_signals,
        runtime_seconds=round(runtime_seconds, 3),
    )


def _run_full_bot(
    config: AppConfig,
    *,
    input_path: Path,
) -> tuple[FullBotBacktestResult, float]:
    started = time.perf_counter()
    result = FullBotBacktestRunner(
        config,
        force_enable_all_pods=False,
    ).run_jsonl(
        input_path=input_path,
        dedupe_by_timestamp=True,
    )
    return result, time.perf_counter() - started


def _decision(
    no_short: ScenarioSummary,
    shorts_on: ScenarioSummary,
) -> EvolutionDecision:
    no_short_delta = no_short.total_delta_usd
    shorts_delta = shorts_on.total_delta_usd
    best = no_short if no_short_delta >= shorts_delta else shorts_on
    if max(no_short_delta, shorts_delta) <= 0.0:
        return EvolutionDecision(
            name=no_short.name,
            verdict="reject",
            best_mode=best.mode,
            no_short_delta_usd=no_short_delta,
            shorts_on_delta_usd=shorts_delta,
            note="Aucune variante ne bat la baseline full-bot.",
        )
    if shorts_delta > no_short_delta and shorts_delta > 0.0:
        short_setups = [setup for setup in shorts_on.setups if setup.endswith("_short")]
        short_trades = sum(shorts_on.setup_trades.get(setup, 0) for setup in short_setups)
        verdict = "keep_shorts_shadow" if short_trades > 0 else "keep_no_short"
        note = (
            "La variante shorts_on bat la baseline et la version sans short."
            if short_trades > 0
            else "La variante shorts_on bat par effet indirect; aucun trade short cloture sur les setups testes."
        )
        return EvolutionDecision(
            name=no_short.name,
            verdict=verdict,
            best_mode=shorts_on.mode,
            no_short_delta_usd=no_short_delta,
            shorts_on_delta_usd=shorts_delta,
            note=note,
        )
    return EvolutionDecision(
        name=no_short.name,
        verdict="keep_no_short",
        best_mode=no_short.mode,
        no_short_delta_usd=no_short_delta,
        shorts_on_delta_usd=shorts_delta,
        note="La version sans short est la meilleure variante positive.",
    )


def _render_markdown(payload: dict[str, object]) -> str:
    baseline = payload["baseline"]
    lines = [
        "# Pod A Pattern Evolution Validation",
        "",
        f"- Input: `{payload['input_path']}`",
        f"- Config baseline: `{payload['config_path']}`",
        f"- Baseline total PnL: `{baseline['total_pnl_usd']:.2f}`",
        f"- Baseline Pod A PnL: `{baseline['pod_a_pnl_usd']:.2f}`",
        f"- Records processed: `{baseline['records_processed']}`",
        f"- Duplicate timestamps skipped: `{baseline['duplicate_timestamps_skipped']}`",
        "",
        "## Decisions",
        "",
        "| Evolution | Verdict | Best mode | No-short delta | Shorts-on delta | Note |",
        "|---|---|---|---:|---:|---|",
    ]
    for item in payload["decisions"]:
        lines.append(
            f"| {item['name']} | {item['verdict']} | {item['best_mode']} | "
            f"{item['no_short_delta_usd']:.2f} | {item['shorts_on_delta_usd']:.2f} | "
            f"{item['note']} |"
        )

    lines.extend(
        [
            "",
            "## Scenario Detail",
            "",
            "| Evolution | Mode | Total PnL | Total delta | Pod A PnL | Pod A delta | Pod A trades | Setup trades | Setup PnL |",
            "|---|---|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for item in payload["scenarios"]:
        setup_trades = ", ".join(
            f"{setup}={count}" for setup, count in item["setup_trades"].items()
        )
        setup_pnl = ", ".join(
            f"{setup}={value:.2f}" for setup, value in item["setup_pnl"].items()
        )
        lines.append(
            f"| {item['name']} | {item['mode']} | {item['total_pnl_usd']:.2f} | "
            f"{item['total_delta_usd']:.2f} | {item['pod_a_pnl_usd']:.2f} | "
            f"{item['pod_a_delta_usd']:.2f} | {item['pod_a_closed_trade_count']} | "
            f"{setup_trades or '-'} | {setup_pnl or '-'} |"
        )

    lines.extend(
        [
            "",
            "## Non-Executable From Candle Matrix",
            "",
            "| Pattern family | Status |",
            "|---|---|",
        ]
    )
    for family, reason in payload["non_executable_pattern_families"].items():
        lines.append(f"| {family} | {reason} |")
    lines.append("")
    return "\n".join(lines)


def run_validation(
    *,
    config_path: Path,
    input_path: Path,
    output_json: Path,
    output_md: Path,
) -> dict[str, object]:
    base_config = load_config(str(config_path))
    print("scenario=baseline_current status=running", flush=True)
    baseline_result, baseline_runtime = _run_full_bot(base_config, input_path=input_path)
    print(
        "scenario=baseline_current "
        f"total={baseline_result.total_realized_pnl_usd} "
        f"pod_a={baseline_result.pod_a.get('realized_pnl_usd', 0.0)} "
        f"seconds={baseline_runtime:.1f}",
        flush=True,
    )
    summaries: list[ScenarioSummary] = []
    decisions: list[EvolutionDecision] = []
    for evolution in ACTIONABLE_EVOLUTIONS:
        mode_results: dict[str, ScenarioSummary] = {}
        for mode, setups in (
            ("no_short", evolution.no_short_setups),
            ("shorts_on", evolution.shorts_on_setups),
        ):
            scenario_name = f"{evolution.name}_{mode}"
            fresh_base_config = load_config(str(config_path))
            scenario_config = _scenario_config(fresh_base_config, setups)
            if scenario_config == fresh_base_config:
                print(f"scenario={scenario_name} status=reused_baseline", flush=True)
                result = baseline_result
                runtime = 0.0
            else:
                print(f"scenario={scenario_name} status=running", flush=True)
                result, runtime = _run_full_bot(scenario_config, input_path=input_path)
            summary = _summary_from_result(
                name=evolution.name,
                description=evolution.description,
                mode=mode,
                setups=setups,
                result=result,
                baseline=baseline_result,
                runtime_seconds=runtime,
            )
            summaries.append(summary)
            mode_results[mode] = summary
            print(
                f"scenario={scenario_name} total={summary.total_pnl_usd} "
                f"delta={summary.total_delta_usd} pod_a={summary.pod_a_pnl_usd} "
                f"pod_a_delta={summary.pod_a_delta_usd} seconds={runtime:.1f}",
                flush=True,
            )
        decisions.append(_decision(mode_results["no_short"], mode_results["shorts_on"]))

    payload = {
        "input_path": str(input_path),
        "config_path": str(config_path),
        "baseline": {
            "total_pnl_usd": float(baseline_result.total_realized_pnl_usd),
            "pod_a_pnl_usd": float(baseline_result.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            "pod_b_pnl_usd": float(baseline_result.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
            "pod_c_pnl_usd": float(baseline_result.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
            "records_processed": int(baseline_result.records_processed),
            "duplicate_timestamps_skipped": int(baseline_result.duplicate_timestamps_skipped),
            "runtime_seconds": round(baseline_runtime, 3),
        },
        "scenarios": [item.to_dict() for item in summaries],
        "decisions": [item.to_dict() for item in decisions],
        "non_executable_pattern_families": NON_EXECUTABLE_PATTERN_FAMILIES,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_render_markdown(payload), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate executable Pod A pattern evolutions with and without shorts.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument(
        "--output-json",
        default="server-data/replay_reports/pod_a_pattern_evolution_validation_20260424.json",
    )
    parser.add_argument(
        "--output-md",
        default="server-data/replay_reports/pod_a_pattern_evolution_validation_20260424.md",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = run_validation(
        config_path=Path(args.config),
        input_path=Path(args.input),
        output_json=Path(args.output_json),
        output_md=Path(args.output_md),
    )
    print(f"output_json={args.output_json}")
    print(f"output_md={args.output_md}")
    print(json.dumps(payload["decisions"], indent=2))


if __name__ == "__main__":
    main()
