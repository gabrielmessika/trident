from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from app.backtest.full_bot_oldb_vs_special_integrated_compare import (
    FullBotSpecialReplacementRunner,
)
from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class WindowSpec:
    name: str
    start_ts: str | None = None
    end_ts: str | None = None


@dataclass(slots=True)
class ScenarioResult:
    total_realized_pnl_usd: float
    directional_fees_usd: float
    pod_a_pnl_usd: float
    pod_b_pnl_usd: float
    pod_c_pnl_usd: float
    pod_special_pnl_usd: float
    pod_a_closed_trade_count: int
    pod_b_closed_trade_count: int
    pod_c_closed_trade_count: int
    pod_special_closed_trade_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "total_realized_pnl_usd": self.total_realized_pnl_usd,
            "directional_fees_usd": self.directional_fees_usd,
            "pod_a_pnl_usd": self.pod_a_pnl_usd,
            "pod_b_pnl_usd": self.pod_b_pnl_usd,
            "pod_c_pnl_usd": self.pod_c_pnl_usd,
            "pod_special_pnl_usd": self.pod_special_pnl_usd,
            "pod_a_closed_trade_count": self.pod_a_closed_trade_count,
            "pod_b_closed_trade_count": self.pod_b_closed_trade_count,
            "pod_c_closed_trade_count": self.pod_c_closed_trade_count,
            "pod_special_closed_trade_count": self.pod_special_closed_trade_count,
        }


@dataclass(slots=True)
class ScenarioDefinition:
    name: str
    description: str
    kind: str
    main_config_path: str | None = None
    special_config_path: str | None = None
    reserved_symbols: list[str] | None = None


def _build_baseline_config(base: AppConfig) -> AppConfig:
    return base


def _build_pod_a_structural_targets(base: AppConfig) -> AppConfig:
    return replace(
        base,
        pod_a=replace(
            base.pod_a,
            structural_targets=replace(base.pod_a.structural_targets, enabled=True),
        ),
    )


def _build_pod_a_reversal_fade(base: AppConfig) -> AppConfig:
    allowed_setups = list(base.pod_a.allowed_setups)
    if "reversal_fade_short" not in allowed_setups:
        allowed_setups.append("reversal_fade_short")
    return replace(
        base,
        pod_a=replace(
            base.pod_a,
            allowed_setups=allowed_setups,
            reversal_fade=replace(base.pod_a.reversal_fade, enabled=True),
        ),
    )


def _build_pod_a_structural_plus_reversal(base: AppConfig) -> AppConfig:
    config = _build_pod_a_reversal_fade(base)
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            structural_targets=replace(config.pod_a.structural_targets, enabled=True),
        ),
    )


def _build_pod_a_campaign_addon(base: AppConfig) -> AppConfig:
    return replace(
        base,
        pod_a=replace(
            base.pod_a,
            campaign=replace(
                base.pod_a.campaign,
                enabled=True,
                initial_entry_fraction=0.70,
                add_on_enabled=True,
                add_on_fraction=0.30,
                add_on_trigger_bps=35.0,
                add_on_min_confidence=0.72,
                max_add_ons_per_position=1,
            ),
        ),
    )


def _build_pod_a_guardrails(base: AppConfig) -> AppConfig:
    return replace(
        base,
        pod_a=replace(
            base.pod_a,
            guardrail_enabled=True,
            setup_guardrail_enabled=True,
            intraday_setup_guardrail_enabled=True,
        ),
    )


def _build_crypto_regime_v2_range_only(base: AppConfig) -> AppConfig:
    regime = replace(
        base.trident.regime,
        crypto_v2_enabled=True,
        crypto_v2_mode="hybrid_upgrade_only",
        crypto_v2_allow_range_to_trend_upgrade=True,
        crypto_v2_allow_dead_zone_to_trend_upgrade=False,
    )
    return replace(base, trident=replace(base.trident, regime=regime))


def _build_pod_c_block_equity_only(base: AppConfig) -> AppConfig:
    blocked = list(base.pod_c.blocked_symbols)
    for symbol in ["XYZ:TSLA", "XYZ:NVDA", "XYZ:CRCL"]:
        if symbol not in blocked:
            blocked.append(symbol)
    return replace(base, pod_c=replace(base.pod_c, blocked_symbols=blocked))


def _build_pod_c_block_fx_only(base: AppConfig) -> AppConfig:
    blocked = list(base.pod_c.blocked_symbols)
    if "XYZ:JPY" not in blocked:
        blocked.append("XYZ:JPY")
    return replace(base, pod_c=replace(base.pod_c, blocked_symbols=blocked))


def _build_pod_c_block_equity_fx(base: AppConfig) -> AppConfig:
    return _build_pod_c_block_fx_only(_build_pod_c_block_equity_only(base))


SCENARIO_BUILDERS: dict[str, callable] = {
    "baseline_current": _build_baseline_config,
    "pod_a_structural_targets": _build_pod_a_structural_targets,
    "pod_a_reversal_fade": _build_pod_a_reversal_fade,
    "pod_a_structural_plus_reversal": _build_pod_a_structural_plus_reversal,
    "pod_a_campaign_addon": _build_pod_a_campaign_addon,
    "pod_a_guardrails": _build_pod_a_guardrails,
    "crypto_regime_v2_range_only": _build_crypto_regime_v2_range_only,
    "pod_c_block_equity_only": _build_pod_c_block_equity_only,
    "pod_c_block_fx_only": _build_pod_c_block_fx_only,
    "pod_c_block_equity_fx": _build_pod_c_block_equity_fx,
}


SCENARIOS: list[ScenarioDefinition] = [
    ScenarioDefinition(
        name="baseline_current",
        description="Current prod baseline.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="pod_a_structural_targets",
        description="Enable structural targets on Pod A trend pullbacks.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="pod_a_reversal_fade",
        description="Enable Pod A rejection-confirmed reversal fade shorts.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="pod_a_structural_plus_reversal",
        description="Enable both structural targets and reversal fade on Pod A.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="pod_a_campaign_addon",
        description="Enable one conservative campaign add-on on Pod A.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="pod_a_guardrails",
        description="Enable the generic Pod A guardrails together.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="crypto_regime_v2_hybrid_moderate_a",
        description="Aggressive crypto regime V2 hybrid shadow profile.",
        kind="config_file",
        main_config_path="config/trident_hybrid_moderate_a_shadow.toml",
    ),
    ScenarioDefinition(
        name="crypto_regime_v2_range_only",
        description="Range-only crypto regime V2 upgrade candidate.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="pod_b_reenabled_validated_slot",
        description="Re-enable legacy Pod B with the validated vetoes and a 10% slot.",
        kind="config_file",
        main_config_path="config/trident_compare_pod_b_slot.toml",
    ),
    ScenarioDefinition(
        name="pod_c_block_equity_only",
        description="Block unproven Pod C equity symbols only.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="pod_c_block_fx_only",
        description="Block unproven Pod C FX symbols only.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="pod_c_block_equity_fx",
        description="Block all unproven Pod C equity and FX symbols.",
        kind="builder",
    ),
    ScenarioDefinition(
        name="special_symbols_taoxpl_slot",
        description="Replace the Pod B slot with the TAO/XPL special-symbols pod.",
        kind="special_slot",
        main_config_path="config/trident_compare_pod_b_slot.toml",
        special_config_path="config/trident_special_symbols_taoxpl_shadow.toml",
        reserved_symbols=["TAO", "XPL"],
    ),
]


WINDOWS: list[WindowSpec] = [
    WindowSpec(name="full_latest_fetch"),
    WindowSpec(
        name="window_0405_0412",
        start_ts="2026-04-05T00:00:00Z",
        end_ts="2026-04-12T23:59:59Z",
    ),
    WindowSpec(
        name="window_0413_0418",
        start_ts="2026-04-13T00:00:00Z",
        end_ts=None,
    ),
]


def _filter_jsonl(source: Path, destination: Path, window: WindowSpec) -> int:
    count = 0
    with source.open("r", encoding="utf-8") as src, destination.open("w", encoding="utf-8") as dst:
        for raw in src:
            if not raw.strip():
                continue
            payload = json.loads(raw)
            timestamp = str(payload.get("timestamp") or "")
            if not timestamp:
                continue
            if window.start_ts is not None and timestamp < window.start_ts:
                continue
            if window.end_ts is not None and timestamp > window.end_ts:
                continue
            dst.write(raw)
            count += 1
    return count


def _summarize_full_bot(result: object) -> ScenarioResult:
    payload = result.to_dict()
    pod_a = payload.get("pod_a", {}) or {}
    pod_b = payload.get("pod_b", {}) or {}
    pod_c = payload.get("pod_c", {}) or {}
    return ScenarioResult(
        total_realized_pnl_usd=float(payload.get("total_realized_pnl_usd", 0.0) or 0.0),
        directional_fees_usd=float(payload.get("directional_fees_usd", 0.0) or 0.0),
        pod_a_pnl_usd=float(pod_a.get("realized_pnl_usd", 0.0) or 0.0),
        pod_b_pnl_usd=float(pod_b.get("realized_pnl_usd", 0.0) or 0.0),
        pod_c_pnl_usd=float(pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        pod_special_pnl_usd=0.0,
        pod_a_closed_trade_count=int(pod_a.get("closed_trade_count", 0) or 0),
        pod_b_closed_trade_count=int(pod_b.get("closed_trade_count", 0) or 0),
        pod_c_closed_trade_count=int(pod_c.get("closed_trade_count", 0) or 0),
        pod_special_closed_trade_count=0,
    )


def _summarize_special_slot(result: object) -> ScenarioResult:
    payload = result.to_dict()
    pod_a = payload.get("pod_a", {}) or {}
    pod_special = payload.get("pod_special", {}) or {}
    pod_c = payload.get("pod_c", {}) or {}
    return ScenarioResult(
        total_realized_pnl_usd=float(payload.get("total_realized_pnl_usd", 0.0) or 0.0),
        directional_fees_usd=float(payload.get("directional_fees_usd", 0.0) or 0.0),
        pod_a_pnl_usd=float(pod_a.get("realized_pnl_usd", 0.0) or 0.0),
        pod_b_pnl_usd=0.0,
        pod_c_pnl_usd=float(pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        pod_special_pnl_usd=float(pod_special.get("realized_pnl_usd", 0.0) or 0.0),
        pod_a_closed_trade_count=int(pod_a.get("closed_trade_count", 0) or 0),
        pod_b_closed_trade_count=0,
        pod_c_closed_trade_count=int(pod_c.get("closed_trade_count", 0) or 0),
        pod_special_closed_trade_count=int(pod_special.get("closed_trade_count", 0) or 0),
    )


def _judgement(
    baseline: dict[str, ScenarioResult],
    scenario: dict[str, ScenarioResult | None],
    *,
    kind: str,
) -> tuple[str, str]:
    full_payload = scenario["full_latest_fetch"]
    if full_payload is None:
        return ("reject", "Scenario failed before producing a full-window result.")
    full_delta = round(
        full_payload.total_realized_pnl_usd
        - baseline["full_latest_fetch"].total_realized_pnl_usd,
        2,
    )
    if abs(full_delta) < 0.01:
        return ("no_effect", "No measurable effect on the full window; split windows skipped.")
    if full_delta < 0:
        return ("reject", "Worse than baseline on the full window; split windows skipped.")
    early_payload = scenario.get("window_0405_0412")
    recent_payload = scenario.get("window_0413_0418")
    if early_payload is None or recent_payload is None:
        return ("shadow_only", "Improves the full window; split validation was not completed.")
    early_delta = round(
        early_payload.total_realized_pnl_usd
        - baseline["window_0405_0412"].total_realized_pnl_usd,
        2,
    )
    recent_delta = round(
        recent_payload.total_realized_pnl_usd
        - baseline["window_0413_0418"].total_realized_pnl_usd,
        2,
    )
    if kind == "special_slot":
        if full_delta > 0 and early_delta > 0 and recent_delta > 0:
            return ("candidate", "Improves all windows, but still changes capital sharing.")
        if full_delta > 0:
            return ("shadow_only", "Improves only part of the sample and still needs real slot validation.")
        return ("reject", "Does not beat the current baseline on the split windows.")
    if full_delta > 0 and early_delta >= 0 and recent_delta >= 0:
        return ("candidate", "Improves full, early, and recent windows.")
    if full_delta > 0 and recent_delta >= -5.0:
        return ("shadow_only", "Adds upside on the full window but weakens at least one split.")
    return ("reject", "Fails the durability gate across the split windows.")


def _run_scenario(
    scenario: ScenarioDefinition,
    baseline_config: AppConfig,
    input_path: Path,
) -> ScenarioResult:
    if scenario.kind == "builder":
        config = SCENARIO_BUILDERS[scenario.name](baseline_config)
        result = FullBotBacktestRunner(config, force_enable_all_pods=False).run_jsonl(
            input_path=input_path,
            dedupe_by_timestamp=True,
        )
        return _summarize_full_bot(result)
    if scenario.kind == "config_file":
        config = load_config(str(scenario.main_config_path))
        result = FullBotBacktestRunner(config, force_enable_all_pods=False).run_jsonl(
            input_path=input_path,
            dedupe_by_timestamp=True,
        )
        return _summarize_full_bot(result)
    if scenario.kind == "special_slot":
        main_config = load_config(str(scenario.main_config_path))
        special_config = load_config(str(scenario.special_config_path))
        result = FullBotSpecialReplacementRunner(
            main_config,
            special_config,
            reserved_symbols=list(scenario.reserved_symbols or []),
        ).run_jsonl(
            input_path=input_path,
            dedupe_by_timestamp=True,
        )
        return _summarize_special_slot(result)
    raise ValueError(f"Unsupported scenario kind: {scenario.kind}")


def _render_markdown(
    input_path: Path,
    windows: list[WindowSpec],
    filtered_counts: dict[str, int],
    results: dict[str, dict[str, ScenarioResult | None]],
    judgements: dict[str, tuple[str, str]],
) -> str:
    baseline = results["baseline_current"]
    lines = [
        "# Remaining Evolution Validation",
        "",
        f"- Input: `{input_path}`",
        "- Baseline: `config/trident.toml`",
        "- Decision rule: prefer changes that hold on full, early, and recent windows.",
        "",
        "## Windows",
        "",
        "| Window | Records | Start | End |",
        "|---|---:|---|---|",
    ]
    for window in windows:
        lines.append(
            f"| {window.name} | {filtered_counts[window.name]} | "
            f"{window.start_ts or 'input start'} | {window.end_ts or 'input end'} |"
        )
    lines.extend(
        [
            "",
            "## Baseline",
            "",
            "| Window | Total | Pod A | Pod B | Pod C | Fees | Trades A | Trades B | Trades C |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for window in windows:
        payload = baseline[window.name]
        assert payload is not None
        lines.append(
            f"| {window.name} | {payload.total_realized_pnl_usd:.2f} | "
            f"{payload.pod_a_pnl_usd:.2f} | {payload.pod_b_pnl_usd:.2f} | "
            f"{payload.pod_c_pnl_usd:.2f} | {payload.directional_fees_usd:.2f} | "
            f"{payload.pod_a_closed_trade_count} | {payload.pod_b_closed_trade_count} | "
            f"{payload.pod_c_closed_trade_count} |"
        )
    lines.extend(
        [
            "",
            "## Scenarios",
            "",
            "| Scenario | Verdict | Full delta | Early delta | Recent delta | Note |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    for scenario in SCENARIOS[1:]:
        scenario_results = results[scenario.name]
        full_delta = (
            (scenario_results["full_latest_fetch"].total_realized_pnl_usd if scenario_results["full_latest_fetch"] is not None else 0.0)
            - baseline["full_latest_fetch"].total_realized_pnl_usd
        )
        early_delta = (
            scenario_results["window_0405_0412"].total_realized_pnl_usd
            - baseline["window_0405_0412"].total_realized_pnl_usd
            if scenario_results["window_0405_0412"] is not None
            else float("nan")
        )
        recent_delta = (
            scenario_results["window_0413_0418"].total_realized_pnl_usd
            - baseline["window_0413_0418"].total_realized_pnl_usd
            if scenario_results["window_0413_0418"] is not None
            else float("nan")
        )
        verdict, note = judgements[scenario.name]
        early_delta_str = f"{early_delta:.2f}" if scenario_results["window_0405_0412"] is not None else "skipped"
        recent_delta_str = f"{recent_delta:.2f}" if scenario_results["window_0413_0418"] is not None else "skipped"
        lines.append(
            f"| {scenario.name} | {verdict} | {full_delta:.2f} | {early_delta_str} | "
            f"{recent_delta_str} | {note} |"
        )
    lines.append("")
    for scenario in SCENARIOS[1:]:
        lines.extend(
            [
                f"## {scenario.name}",
                "",
                f"- Description: {scenario.description}",
                f"- Verdict: `{judgements[scenario.name][0]}`",
                f"- Note: {judgements[scenario.name][1]}",
                "",
                "| Window | Total | Delta vs baseline | Pod A | Pod B | Pod C | Pod special | Fees |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for window in windows:
            payload = results[scenario.name][window.name]
            if payload is None:
                lines.append(
                    f"| {window.name} | skipped | skipped | skipped | skipped | skipped | skipped | skipped |"
                )
                continue
            delta = payload.total_realized_pnl_usd - baseline[window.name].total_realized_pnl_usd
            lines.append(
                f"| {window.name} | {payload.total_realized_pnl_usd:.2f} | {delta:.2f} | "
                f"{payload.pod_a_pnl_usd:.2f} | {payload.pod_b_pnl_usd:.2f} | "
                f"{payload.pod_c_pnl_usd:.2f} | {payload.pod_special_pnl_usd:.2f} | "
                f"{payload.directional_fees_usd:.2f} |"
            )
        lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the remaining strategy evolutions against the current baseline."
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--json-output")
    parser.add_argument("--md-output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    baseline_config = load_config(args.config)
    input_path = Path(args.input)
    results: dict[str, dict[str, ScenarioResult | None]] = {scenario.name: {} for scenario in SCENARIOS}
    filtered_counts: dict[str, int] = {}

    with TemporaryDirectory(prefix="trident_evolution_windows_") as tmpdir:
        tmp_root = Path(tmpdir)
        for window in WINDOWS:
            window_path = tmp_root / f"{window.name}.jsonl"
            filtered_counts[window.name] = _filter_jsonl(input_path, window_path, window)
            if window.name == "full_latest_fetch":
                for scenario in SCENARIOS:
                    results[scenario.name][window.name] = _run_scenario(
                        scenario,
                        baseline_config,
                        window_path,
                    )
                continue

            for scenario in SCENARIOS[:1]:
                results[scenario.name][window.name] = _run_scenario(
                    scenario,
                    baseline_config,
                    window_path,
                )

            baseline_full = results["baseline_current"]["full_latest_fetch"]
            assert baseline_full is not None
            for scenario in SCENARIOS[1:]:
                scenario_full = results[scenario.name]["full_latest_fetch"]
                assert scenario_full is not None
                full_delta = (
                    scenario_full.total_realized_pnl_usd
                    - baseline_full.total_realized_pnl_usd
                )
                if full_delta <= 0.0:
                    results[scenario.name][window.name] = None
                    continue
                results[scenario.name][window.name] = _run_scenario(
                    scenario,
                    baseline_config,
                    window_path,
                )

    baseline = results["baseline_current"]
    judgements = {
        scenario.name: _judgement(
            baseline,
            results[scenario.name],
            kind=scenario.kind,
        )
        for scenario in SCENARIOS[1:]
    }
    payload = {
        "input_path": str(input_path),
        "baseline_config": args.config,
        "windows": [
            {
                "name": window.name,
                "start_ts": window.start_ts,
                "end_ts": window.end_ts,
                "records": filtered_counts[window.name],
            }
            for window in WINDOWS
        ],
        "baseline": {
            window.name: baseline[window.name].to_dict() if baseline[window.name] is not None else None
            for window in WINDOWS
        },
        "scenarios": {
            scenario.name: {
                "description": scenario.description,
                "kind": scenario.kind,
                "judgement": {
                    "verdict": judgements[scenario.name][0],
                    "note": judgements[scenario.name][1],
                },
                "windows": {
                    window.name: (
                        results[scenario.name][window.name].to_dict()
                        if results[scenario.name][window.name] is not None
                        else None
                    )
                    for window in WINDOWS
                },
            }
            for scenario in SCENARIOS[1:]
        },
    }
    if args.json_output:
        output_path = Path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.md_output:
        output_path = Path(args.md_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            _render_markdown(input_path, WINDOWS, filtered_counts, results, judgements),
            encoding="utf-8",
        )
    print(json.dumps({name: value for name, value in judgements.items()}, indent=2))


if __name__ == "__main__":
    main()
