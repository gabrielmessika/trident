from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable

from app.backtest import full_bot_replay as full_replay
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, PodAPatternVetoConfig, load_config
from app.trident.types import TradePlan


PROMOTED_MTF_VETO_NAMES = {
    "mtf_4h_rsi14_weakness",
    "mtf_4h_close_below_ema50",
    "mtf_1h_chop_ema20_under_ema50_rsi40_50",
    "mtf_1h_overextension_chase",
}


@dataclass(slots=True)
class ScenarioSummary:
    name: str
    total_pnl_usd: float
    total_delta_usd: float
    pod_a_pnl_usd: float
    pod_a_delta_usd: float
    pod_b_pnl_usd: float
    pod_c_pnl_usd: float
    pod_c_delta_usd: float
    pod_a_trades: int
    pod_c_trades: int
    pod_a_rejections: dict[str, int]


class _NoopRoutingReplay:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def run_jsonl(self, *_args: object, **_kwargs: object) -> "_NoopRoutingReplay":
        return self

    def to_dict(self) -> dict[str, object]:
        return {"skipped": True, "reason": "pod_a_mtf_candidate_validation"}


def _base_config(path: str | Path, *, strip_promoted_mtf_vetoes: bool) -> AppConfig:
    config = load_config(path)
    if strip_promoted_mtf_vetoes:
        config = replace(
            config,
            pod_a=replace(
                config.pod_a,
                pattern_vetoes=[
                    rule
                    for rule in config.pod_a.pattern_vetoes
                    if rule.name not in PROMOTED_MTF_VETO_NAMES
                ],
            ),
        )
    return replace(config, pod_b=replace(config.pod_b, enabled=False))


def _append_pod_a_vetoes(
    config: AppConfig,
    vetoes: Iterable[PodAPatternVetoConfig],
) -> AppConfig:
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            pattern_vetoes=list(config.pod_a.pattern_vetoes) + list(vetoes),
        ),
    )


def _with_4h_weakness_veto(config: AppConfig) -> AppConfig:
    return _append_pod_a_vetoes(
        config,
        [
            PodAPatternVetoConfig(
                name="candidate_4h_rsi14_weakness",
                setups=["trend_pullback_long"],
                market_clusters=["crypto"],
                sides=["long"],
                max_prev_rsi14_4h=40.0,
            ),
            PodAPatternVetoConfig(
                name="candidate_4h_close_below_ema50",
                setups=["trend_pullback_long"],
                market_clusters=["crypto"],
                sides=["long"],
                require_prev_ema50_ready_4h=True,
                max_prev_ema50_distance_4h_pct=0.0,
            ),
        ],
    )


def _with_1h_chop_veto(config: AppConfig) -> AppConfig:
    return _append_pod_a_vetoes(
        config,
        [
            PodAPatternVetoConfig(
                name="candidate_1h_chop_ema20_under_ema50_rsi40_50",
                setups=["trend_pullback_long"],
                market_clusters=["crypto"],
                sides=["long"],
                require_prev_ema50_ready_1h=True,
                min_prev_rsi14_1h=40.0,
                max_prev_rsi14_1h=50.0,
                max_prev_ema20_distance_ema50_1h_pct=0.0,
            )
        ],
    )


def _with_1h_overextension_veto(config: AppConfig) -> AppConfig:
    return _append_pod_a_vetoes(
        config,
        [
            PodAPatternVetoConfig(
                name="candidate_1h_overextension_chase",
                setups=["trend_pullback_long"],
                market_clusters=["crypto"],
                sides=["long"],
                min_prev_rsi14_1h=70.0,
                min_entry_vs_open_1h_bps=50.0,
            )
        ],
    )


def _is_overextension_chase(plan: TradePlan) -> bool:
    if plan.side != "long" or plan.setup != "trend_pullback_long":
        return False
    if str(plan.setup_details.get("market_cluster", "")).strip().lower() != "crypto":
        return False
    try:
        rsi14_1h = float(plan.setup_details.get("prev_rsi14_1h", 0.0) or 0.0)
        entry_vs_open = float(plan.setup_details.get("entry_vs_open_1h_bps", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    return rsi14_1h >= 70.0 and entry_vs_open >= 50.0


class OverextensionThrottleRiskGate(PodARiskGate):
    def evaluate_many(self, plans: list[TradePlan]):
        for plan in plans:
            if not _is_overextension_chase(plan):
                continue
            plan.target_notional_usd = round(plan.target_notional_usd * 0.5, 6)
            plan.margin_usd = round(plan.margin_usd * 0.5, 6)
            plan.risk_budget_usd = round(plan.risk_budget_usd * 0.5, 6)
            plan.expected_loss_usd = round(plan.expected_loss_usd * 0.5, 6)
            details = dict(plan.setup_details or {})
            details["candidate_1h_overextension_throttle"] = True
            details["candidate_1h_overextension_size_multiplier"] = 0.5
            plan.setup_details = details
        return super().evaluate_many(plans)


def _run(
    *,
    config: AppConfig,
    input_path: str | Path,
    throttle_overextension: bool = False,
) -> full_replay.FullBotBacktestResult:
    runner = full_replay.FullBotBacktestRunner(config, force_enable_all_pods=False)
    if throttle_overextension:
        runner.pod_a_risk_gate = OverextensionThrottleRiskGate(runner.config)
    return runner.run_jsonl(input_path=input_path, dedupe_by_timestamp=True)


def _summarize(
    *,
    name: str,
    result: full_replay.FullBotBacktestResult,
    baseline: full_replay.FullBotBacktestResult,
) -> ScenarioSummary:
    pod_a = result.pod_a
    pod_c = result.pod_c
    baseline_pod_a = baseline.pod_a
    baseline_pod_c = baseline.pod_c
    return ScenarioSummary(
        name=name,
        total_pnl_usd=round(float(result.total_realized_pnl_usd), 4),
        total_delta_usd=round(
            float(result.total_realized_pnl_usd) - float(baseline.total_realized_pnl_usd),
            4,
        ),
        pod_a_pnl_usd=round(float(pod_a.get("realized_pnl_usd", 0.0) or 0.0), 4),
        pod_a_delta_usd=round(
            float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
            - float(baseline_pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            4,
        ),
        pod_b_pnl_usd=round(float(result.pod_b.get("realized_pnl_usd", 0.0) or 0.0), 4),
        pod_c_pnl_usd=round(float(pod_c.get("realized_pnl_usd", 0.0) or 0.0), 4),
        pod_c_delta_usd=round(
            float(pod_c.get("realized_pnl_usd", 0.0) or 0.0)
            - float(baseline_pod_c.get("realized_pnl_usd", 0.0) or 0.0),
            4,
        ),
        pod_a_trades=int(pod_a.get("closed_trade_count", 0) or 0),
        pod_c_trades=int(pod_c.get("closed_trade_count", 0) or 0),
        pod_a_rejections={
            str(key): int(value)
            for key, value in (pod_a.get("rejections_by_reason", {}) or {}).items()
        },
    )


def _render_markdown(rows: list[ScenarioSummary]) -> str:
    lines = [
        "# Pod A MTF Candidate Validation",
        "",
        (
            "Pod B is disabled. Final routing replay is skipped in this report; "
            "directional Pod A/Pod C replay is full stream."
        ),
        "",
        (
            "| scenario | total pnl | delta | pod A pnl | pod A delta | "
            "pod A trades | pod C pnl | pod C delta | pod C trades |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| `{row.name}` | {row.total_pnl_usd:.2f} | {row.total_delta_usd:+.2f} | "
            f"{row.pod_a_pnl_usd:.2f} | {row.pod_a_delta_usd:+.2f} | "
            f"{row.pod_a_trades} | {row.pod_c_pnl_usd:.2f} | "
            f"{row.pod_c_delta_usd:+.2f} | {row.pod_c_trades} |"
        )
    lines.extend(["", "## Pod A Rejections", ""])
    for row in rows:
        candidate_rejections = {
            key: value
            for key, value in row.pod_a_rejections.items()
            if key.startswith("pattern_veto_candidate")
        }
        if not candidate_rejections:
            continue
        rendered = ", ".join(
            f"`{key}`={value}" for key, value in sorted(candidate_rejections.items())
        )
        lines.append(f"- `{row.name}`: {rendered}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Pod A MTF candidate veto/throttle ideas on full replay.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument(
        "--json-output",
        default="server-data/replay_reports/pod_a_mtf_candidate_validation_20260427.json",
    )
    parser.add_argument(
        "--markdown-output",
        default="server-data/replay_reports/pod_a_mtf_candidate_validation_20260427.md",
    )
    parser.add_argument(
        "--include-routing-replay",
        action="store_true",
        help="Run the extra routing replay summary after each directional full replay.",
    )
    parser.add_argument(
        "--keep-config-mtf-vetoes",
        action="store_true",
        help="Keep already-promoted MTF vetoes in the baseline scenario.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.include_routing_replay:
        full_replay.RoutingReplayRunner = _NoopRoutingReplay

    base_config = _base_config(
        args.config,
        strip_promoted_mtf_vetoes=not args.keep_config_mtf_vetoes,
    )
    scenarios: list[tuple[str, AppConfig, bool]] = [
        ("baseline", base_config, False),
        ("mtf_4h_weakness_veto", _with_4h_weakness_veto(base_config), False),
        ("mtf_1h_chop_veto", _with_1h_chop_veto(base_config), False),
        ("mtf_1h_overextension_veto", _with_1h_overextension_veto(base_config), False),
        ("mtf_1h_overextension_throttle_50pct", base_config, True),
        (
            "mtf_combo_4h_weakness_1h_chop_overextension",
            _with_1h_overextension_veto(
                _with_1h_chop_veto(_with_4h_weakness_veto(base_config))
            ),
            False,
        ),
    ]

    results: dict[str, full_replay.FullBotBacktestResult] = {}
    summaries: list[ScenarioSummary] = []
    for name, config, throttle in scenarios:
        print(f"running={name}", flush=True)
        result = _run(
            config=config,
            input_path=args.input,
            throttle_overextension=throttle,
        )
        results[name] = result
        baseline = results["baseline"]
        summary = _summarize(name=name, result=result, baseline=baseline)
        summaries.append(summary)
        print(
            f"finished={name} total={summary.total_pnl_usd:.2f} "
            f"delta={summary.total_delta_usd:+.2f} "
            f"pod_a={summary.pod_a_pnl_usd:.2f}",
            flush=True,
        )

    payload = {
        "input_path": str(args.input),
        "config_path": str(args.config),
        "routing_replay_included": bool(args.include_routing_replay),
        "promoted_mtf_vetoes_stripped_from_baseline": not bool(args.keep_config_mtf_vetoes),
        "summaries": [asdict(row) for row in summaries],
    }
    json_path = Path(args.json_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    markdown_path = Path(args.markdown_output)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_render_markdown(summaries), encoding="utf-8")

    print(f"json_output={json_path}", flush=True)
    print(f"markdown_output={markdown_path}", flush=True)


if __name__ == "__main__":
    main()
