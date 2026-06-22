#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.backtest import full_bot_replay as full_bot_replay_module
from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import AppConfig, load_config
from app.trident.types import PodName, RiskDecision, SymbolMarketSnapshot, TradePlan
from app.trident_ai.market_regime import (
    build_market_micro_regime,
    market_micro_regime_labels,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PARENT = ROOT / "server-data" / "replay_reports"
DEFAULT_RECENT_INPUT = (
    ROOT
    / "server-data"
    / "replay_inputs"
    / "full_bot_live_window_20260524T1605_20260611_no_external_reference.jsonl"
)


class _NoopRoutingReplayRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run_jsonl(self, *, input_path: str | Path, dedupe_by_timestamp: bool = True):
        class _NoopRoutingResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "skipped": True,
                    "reason": "omitted_by_p111_micro_regime_full_replay",
                }

        return _NoopRoutingResult()


def _install_fast_routing_replay() -> None:
    full_bot_replay_module.RoutingReplayRunner = _NoopRoutingReplayRunner


@dataclass(frozen=True, slots=True)
class MicroRegimeProfile:
    name: str
    description: str
    veto_labels: tuple[str, ...] = ()
    size_scales: tuple[tuple[str, float], ...] = ()
    promotion_class: str = "research_candidate_requires_full_validation"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PolicyPlanResult:
    plan: TradePlan
    veto_reason: str | None = None


@dataclass(slots=True)
class ReplaySummaryRow:
    input_label: str
    profile: str
    description: str
    promotion_class: str
    runtime_seconds: float
    records_processed: int
    dates_covered: list[str]
    ac_realized_pnl_usd: float
    ac_delta_usd: float | None
    ac_closed_trade_count: int
    ac_trade_delta: int | None
    ac_profit_factor: float | None
    ac_win_rate: float | None
    ac_max_drawdown_usd: float
    pod_a_realized_pnl_usd: float
    pod_a_delta_usd: float | None
    pod_a_closed_trade_count: int
    pod_c_realized_pnl_usd: float
    pod_c_delta_usd: float | None
    pod_c_closed_trade_count: int
    micro_rejections: dict[str, int]
    micro_scaled_closed_trades: int
    report_path: str
    summary_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def default_profiles() -> list[MicroRegimeProfile]:
    return [
        MicroRegimeProfile(
            name="baseline",
            description="Current A/C replay with no micro-regime gate.",
            promotion_class="reference",
        ),
        MicroRegimeProfile(
            name="veto_range_mid_vol_high",
            description="Veto entries whose entry range_vol_regime is range_mid|vol_high.",
            veto_labels=("range_vol_regime::range_mid|vol_high",),
        ),
        MicroRegimeProfile(
            name="half_size_micro_adverse",
            description="Apply 0.50 notional scale when entry microprice_bucket is micro_adverse.",
            size_scales=(("microprice_bucket::micro_adverse", 0.50),),
        ),
        MicroRegimeProfile(
            name="veto_mid_high_half_adverse",
            description="Veto range_mid|vol_high and apply 0.50 notional scale on micro_adverse.",
            veto_labels=("range_vol_regime::range_mid|vol_high",),
            size_scales=(("microprice_bucket::micro_adverse", 0.50),),
        ),
    ]


class MicroRegimeFullBotBacktestRunner(FullBotBacktestRunner):
    def __init__(
        self,
        config: AppConfig,
        *,
        micro_profile: MicroRegimeProfile,
        force_enable_all_pods: bool = True,
        apply_live_notional_caps: bool = True,
    ) -> None:
        super().__init__(
            config,
            force_enable_all_pods=force_enable_all_pods,
            apply_live_notional_caps=apply_live_notional_caps,
        )
        self.micro_profile = micro_profile

    def _process_pod_a(
        self,
        *,
        supervisor,
        report,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
        source_file: str,
        previous_regime: str,
        current_regime: str,
    ) -> None:
        self._add_regime_record(
            report=report,
            timestamp=timestamp,
            source_file=source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        previews = supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp)
        trade_plans = supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp)
        date_key = self._date_key(timestamp, source_file)
        for plan in trade_plans:
            plan.setup_details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
        trade_plans = self._apply_live_notional_caps(PodName.POD_A, trade_plans)
        policy_results = apply_micro_regime_policy_to_plans(
            self.micro_profile,
            trade_plans,
            snapshots,
            pod_name=PodName.POD_A,
        )
        gate_plans = [item.plan for item in policy_results if item.veto_reason is None]
        if self.external_reference_policy is not None:
            self.external_reference_policy.adjust_plans(PodName.POD_A, gate_plans)
        risk_decisions = self.pod_a_risk_gate.evaluate_many(gate_plans)
        if self.external_reference_policy is not None:
            risk_decisions = self.external_reference_policy.apply_decisions(
                PodName.POD_A,
                risk_decisions,
            )
        risk_decisions = combine_policy_and_gate_decisions(policy_results, risk_decisions)
        opening_symbols = supervisor.opening_symbols_for(PodName.POD_A)
        managed_symbols = supervisor.managed_symbols_for(
            PodName.POD_A,
            active_symbols=self.pod_a_executor.portfolio.open_positions.keys(),
        )
        execution = self.pod_a_executor.process_record(
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            entry_allowed_symbols=opening_symbols,
            managed_symbols=managed_symbols,
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=risk_decisions,
            execution=execution,
            executor=self.pod_a_executor,
            closed_trade_recorder=self._record_pod_a_closed_trade,
        )

    def _process_pod_c(
        self,
        *,
        supervisor,
        report,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
        source_file: str,
        previous_regime: str,
        current_regime: str,
    ) -> None:
        self._add_regime_record(
            report=report,
            timestamp=timestamp,
            source_file=source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        previews = supervisor.preview_pod_c_signals(snapshots)
        trade_plans = supervisor.build_pod_c_trade_plans(snapshots)
        date_key = self._date_key(timestamp, source_file)
        for plan in trade_plans:
            plan.setup_details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
        trade_plans = self._apply_live_notional_caps(PodName.POD_C, trade_plans)
        policy_results = apply_micro_regime_policy_to_plans(
            self.micro_profile,
            trade_plans,
            snapshots,
            pod_name=PodName.POD_C,
        )
        gate_plans = [item.plan for item in policy_results if item.veto_reason is None]
        if self.external_reference_policy is not None:
            self.external_reference_policy.adjust_plans(PodName.POD_C, gate_plans)
        risk_decisions = self.pod_c_risk_gate.evaluate_many(gate_plans)
        if self.external_reference_policy is not None:
            risk_decisions = self.external_reference_policy.apply_decisions(
                PodName.POD_C,
                risk_decisions,
            )
        risk_decisions = combine_policy_and_gate_decisions(policy_results, risk_decisions)
        opening_symbols = supervisor.opening_symbols_for(PodName.POD_C)
        managed_symbols = supervisor.managed_symbols_for(
            PodName.POD_C,
            active_symbols=self.pod_c_executor.portfolio.open_positions.keys(),
        )
        execution = self.pod_c_executor.process_record(
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            entry_allowed_symbols=opening_symbols,
            managed_symbols=managed_symbols,
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=risk_decisions,
            execution=execution,
            executor=self.pod_c_executor,
        )


def apply_micro_regime_policy_to_plans(
    profile: MicroRegimeProfile,
    plans: Sequence[TradePlan],
    snapshots: Sequence[SymbolMarketSnapshot],
    *,
    pod_name: PodName,
) -> list[PolicyPlanResult]:
    snapshot_by_symbol = {snapshot.symbol.upper(): snapshot for snapshot in snapshots}
    results: list[PolicyPlanResult] = []
    veto_labels = set(profile.veto_labels)
    for plan in plans:
        snapshot = snapshot_by_symbol.get(plan.symbol.upper())
        adjusted = plan
        if snapshot is None:
            results.append(PolicyPlanResult(plan=adjusted))
            continue
        micro_regime = build_market_micro_regime(
            _snapshot_feature_mapping(snapshot),
            symbol=plan.symbol,
            side=plan.side,
        )
        labels = set(market_micro_regime_labels(micro_regime))
        adjusted = _attach_micro_regime_details(
            adjusted,
            profile=profile,
            pod_name=pod_name,
            micro_regime=micro_regime,
            labels=labels,
        )
        matched_vetoes = sorted(label for label in veto_labels if label in labels)
        if matched_vetoes:
            reason = f"micro_regime_veto_{_safe_reason_label(matched_vetoes[0])}"
            results.append(PolicyPlanResult(plan=adjusted, veto_reason=reason))
            continue
        scale = _notional_scale_for_labels(profile, labels)
        if scale < 1.0:
            adjusted = _scale_plan_notional(adjusted, scale=scale, profile=profile)
        results.append(PolicyPlanResult(plan=adjusted))
    return results


def combine_policy_and_gate_decisions(
    policy_results: Sequence[PolicyPlanResult],
    gate_decisions: Sequence[RiskDecision],
) -> list[RiskDecision]:
    gate_iter = iter(gate_decisions)
    combined: list[RiskDecision] = []
    for item in policy_results:
        if item.veto_reason is not None:
            combined.append(
                RiskDecision(
                    accepted=False,
                    reason=item.veto_reason,
                    trade_plan=item.plan,
                )
            )
            continue
        combined.append(next(gate_iter))
    return combined


def run_full_replay_suite(
    *,
    config_path: Path,
    input_sources: Sequence[str],
    profile_names: Sequence[str] | None = None,
    output_dir: Path | None = None,
    apply_live_notional_caps: bool = True,
    skip_routing_replay: bool = True,
) -> dict[str, object]:
    if skip_routing_replay:
        _install_fast_routing_replay()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = output_dir or DEFAULT_OUTPUT_PARENT / f"p111_micro_regime_full_replay_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    profiles = _selected_profiles(profile_names)
    inputs = [_parse_labeled_path(item) for item in input_sources]

    rows: list[ReplaySummaryRow] = []
    baselines: dict[str, ReplaySummaryRow] = {}
    for input_label, input_path in inputs:
        for profile in profiles:
            start = time.perf_counter()
            report_path = out_dir / f"{input_label}_{profile.name}.json"
            summary_path = out_dir / f"{input_label}_{profile.name}.md"
            runner = MicroRegimeFullBotBacktestRunner(
                load_config(str(config_path)),
                micro_profile=profile,
                apply_live_notional_caps=apply_live_notional_caps,
            )
            result = runner.run_jsonl(
                input_path=input_path,
                report_output=report_path,
                summary_output=summary_path,
            )
            runtime = time.perf_counter() - start
            baseline = baselines.get(input_label)
            row = summarize_replay_result(
                input_label=input_label,
                profile=profile,
                result=result,
                runtime_seconds=runtime,
                report_path=report_path,
                summary_path=summary_path,
                baseline=baseline,
            )
            rows.append(row)
            if profile.name == "baseline":
                baselines[input_label] = row

    payload = {
        "kind": "p111_micro_regime_full_replay",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "research_only_no_live_change",
        "config_path": str(config_path),
        "apply_live_notional_caps": apply_live_notional_caps,
        "skip_routing_replay": skip_routing_replay,
        "inputs": [{"label": label, "path": str(path)} for label, path in inputs],
        "profiles": [profile.to_dict() for profile in profiles],
        "rows": [row.to_dict() for row in rows],
        "decision": build_promotion_decision(rows),
    }
    report_json = out_dir / "p111_micro_regime_full_replay.json"
    report_md = out_dir / "p111_micro_regime_full_replay.md"
    report_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_md.write_text(render_markdown_report(payload), encoding="utf-8")
    payload["output_dir"] = str(out_dir)
    payload["report_json"] = str(report_json)
    payload["report_md"] = str(report_md)
    return payload


def summarize_replay_result(
    *,
    input_label: str,
    profile: MicroRegimeProfile,
    result: FullBotBacktestResult,
    runtime_seconds: float,
    report_path: Path,
    summary_path: Path,
    baseline: ReplaySummaryRow | None,
) -> ReplaySummaryRow:
    pod_a = result.pod_a
    pod_c = result.pod_c
    ac_pnl = round(_float(pod_a.get("realized_pnl_usd")) + _float(pod_c.get("realized_pnl_usd")), 4)
    ac_trades = _int(pod_a.get("closed_trade_count")) + _int(pod_c.get("closed_trade_count"))
    pod_a_pnl = _float(pod_a.get("realized_pnl_usd"))
    pod_c_pnl = _float(pod_c.get("realized_pnl_usd"))
    all_trades = list(pod_a.get("closed_trade_log") or []) + list(pod_c.get("closed_trade_log") or [])
    return ReplaySummaryRow(
        input_label=input_label,
        profile=profile.name,
        description=profile.description,
        promotion_class=profile.promotion_class,
        runtime_seconds=round(runtime_seconds, 3),
        records_processed=result.records_processed,
        dates_covered=list(result.dates_covered),
        ac_realized_pnl_usd=ac_pnl,
        ac_delta_usd=(round(ac_pnl - baseline.ac_realized_pnl_usd, 4) if baseline else None),
        ac_closed_trade_count=ac_trades,
        ac_trade_delta=(ac_trades - baseline.ac_closed_trade_count if baseline else None),
        ac_profit_factor=_profit_factor(all_trades),
        ac_win_rate=_win_rate(all_trades),
        ac_max_drawdown_usd=round(
            max(_float(pod_a.get("max_drawdown_usd")), _float(pod_c.get("max_drawdown_usd"))),
            4,
        ),
        pod_a_realized_pnl_usd=pod_a_pnl,
        pod_a_delta_usd=(
            round(pod_a_pnl - baseline.pod_a_realized_pnl_usd, 4) if baseline else None
        ),
        pod_a_closed_trade_count=_int(pod_a.get("closed_trade_count")),
        pod_c_realized_pnl_usd=pod_c_pnl,
        pod_c_delta_usd=(
            round(pod_c_pnl - baseline.pod_c_realized_pnl_usd, 4) if baseline else None
        ),
        pod_c_closed_trade_count=_int(pod_c.get("closed_trade_count")),
        micro_rejections=_micro_rejections(pod_a, pod_c),
        micro_scaled_closed_trades=_micro_scaled_closed_trades(all_trades),
        report_path=str(report_path),
        summary_path=str(summary_path),
    )


def build_promotion_decision(rows: Sequence[ReplaySummaryRow]) -> dict[str, object]:
    by_profile: dict[str, list[ReplaySummaryRow]] = {}
    for row in rows:
        if row.profile == "baseline":
            continue
        by_profile.setdefault(row.profile, []).append(row)
    candidates: list[dict[str, object]] = []
    for profile, profile_rows in by_profile.items():
        deltas = [row.ac_delta_usd for row in profile_rows if row.ac_delta_usd is not None]
        improved = bool(deltas) and all(delta > 0.0 for delta in deltas)
        min_delta = min(deltas) if deltas else None
        total_delta = round(sum(deltas), 4) if deltas else None
        trade_delta = sum(row.ac_trade_delta or 0 for row in profile_rows)
        if improved and len(profile_rows) >= 2:
            verdict = "candidate_shadow_promotion_requires_operator_confirmation"
        elif improved:
            verdict = "improved_in_sample_needs_baseline_or_walk_forward"
        else:
            verdict = "do_not_promote"
        candidates.append(
            {
                "profile": profile,
                "verdict": verdict,
                "input_count": len(profile_rows),
                "min_delta_usd": min_delta,
                "total_delta_usd": total_delta,
                "trade_delta": trade_delta,
            }
        )
    best = sorted(
        candidates,
        key=lambda item: (
            _sort_delta(item["total_delta_usd"]),
            _sort_delta(item["min_delta_usd"]),
        ),
        reverse=True,
    )
    return {
        "live_change": "none",
        "reason": "research replay only; live/mainnet activation still requires explicit operator preflight/deploy",
        "profiles": candidates,
        "best_profile": best[0] if best else None,
    }


def render_markdown_report(payload: dict[str, object]) -> str:
    rows = [ReplaySummaryRow(**row) for row in payload["rows"]]  # type: ignore[index]
    lines = [
        "# P1-11 Micro-Regime Full Replay",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        f"- Apply live notional caps: `{payload['apply_live_notional_caps']}`",
        f"- Routing replay: `{'skipped' if payload['skip_routing_replay'] else 'included'}`",
        "",
        "## Results",
        "",
        (
            "| Input | Profile | A/C PnL | Delta | Trades | Trade Delta | PF | WR | "
            "Max DD | Pod A | Pod C | Micro rejects | Scaled closed |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| "
            f"`{row.input_label}` | `{row.profile}` | "
            f"${row.ac_realized_pnl_usd:.2f} | {_fmt_delta(row.ac_delta_usd)} | "
            f"{row.ac_closed_trade_count} | {_fmt_int_delta(row.ac_trade_delta)} | "
            f"{_fmt_optional(row.ac_profit_factor)} | {_fmt_pct(row.ac_win_rate)} | "
            f"${row.ac_max_drawdown_usd:.2f} | ${row.pod_a_realized_pnl_usd:.2f} | "
            f"${row.pod_c_realized_pnl_usd:.2f} | {sum(row.micro_rejections.values())} | "
            f"{row.micro_scaled_closed_trades} |"
        )
    lines.extend(["", "## Decision", ""])
    decision = payload["decision"]  # type: ignore[index]
    if isinstance(decision, dict):
        lines.append(f"- Live change: `{decision.get('live_change')}`")
        lines.append(f"- Reason: {decision.get('reason')}")
        best = decision.get("best_profile")
        if isinstance(best, dict):
            lines.append(
                "- Best profile: "
                f"`{best.get('profile')}` / `{best.get('verdict')}` / "
                f"total delta `{best.get('total_delta_usd')}`"
            )
        profiles = decision.get("profiles")
        if isinstance(profiles, list):
            for item in profiles:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"- `{item.get('profile')}`: `{item.get('verdict')}`, "
                    f"min delta `{item.get('min_delta_usd')}`, "
                    f"total delta `{item.get('total_delta_usd')}`, "
                    f"trade delta `{item.get('trade_delta')}`"
                )
    lines.extend(["", "## Guardrails", ""])
    lines.append("- Full replay changes are applied before entry, but remain research-only.")
    lines.append("- No live config, deploy script, fetch script, or mainnet setting is modified.")
    lines.append("- Promotion to live still requires explicit operator confirmation and preflight.")
    return "\n".join(lines) + "\n"


def _selected_profiles(profile_names: Sequence[str] | None) -> list[MicroRegimeProfile]:
    profiles = default_profiles()
    if not profile_names:
        return profiles
    wanted = {item.strip() for item in profile_names if item.strip()}
    selected = [profile for profile in profiles if profile.name in wanted]
    missing = sorted(wanted - {profile.name for profile in selected})
    if missing:
        raise ValueError(f"unknown_micro_regime_profiles: {', '.join(missing)}")
    return selected


def _attach_micro_regime_details(
    plan: TradePlan,
    *,
    profile: MicroRegimeProfile,
    pod_name: PodName,
    micro_regime: dict[str, object],
    labels: set[str],
) -> TradePlan:
    details: dict[str, float | str | bool] = dict(plan.setup_details or {})
    details.update(
        {
            "micro_regime_profile": profile.name,
            "micro_regime_pod": pod_name.value,
            "market_micro_regime_schema": str(micro_regime.get("schema_version", "")),
            "range_bucket": str(micro_regime.get("range_bucket", "")),
            "short_vol_bucket": str(micro_regime.get("short_vol_bucket", "")),
            "volume_ratio_bucket": str(micro_regime.get("volume_ratio_bucket", "")),
            "vwap_bucket": str(micro_regime.get("vwap_bucket", "")),
            "microprice_bucket": str(micro_regime.get("microprice_bucket", "")),
            "range_vol_regime": str(micro_regime.get("range_vol_regime", "")),
            "flow_regime": str(micro_regime.get("flow_regime", "")),
            "micro_regime": str(micro_regime.get("micro_regime", "")),
            "symbol_range_vol": str(micro_regime.get("symbol_range_vol", "")),
            "symbol_micro_regime": str(micro_regime.get("symbol_micro_regime", "")),
            "micro_regime_labels": ",".join(sorted(labels)),
        }
    )
    return replace(plan, setup_details=details)


def _scale_plan_notional(
    plan: TradePlan,
    *,
    scale: float,
    profile: MicroRegimeProfile,
) -> TradePlan:
    bounded = max(min(float(scale), 1.0), 0.0)
    details: dict[str, float | str | bool] = dict(plan.setup_details or {})
    details["micro_regime_notional_scale"] = round(bounded, 6)
    details["micro_regime_scale_profile"] = profile.name
    details["micro_regime_original_target_notional_usd"] = round(
        float(plan.target_notional_usd or 0.0),
        6,
    )
    for key in (
        "campaign_base_target_notional_usd",
        "campaign_base_margin_usd",
        "campaign_base_risk_budget_usd",
        "campaign_base_expected_loss_usd",
    ):
        if key in details:
            details[key] = round(_float(details[key]) * bounded, 6)
    return replace(
        plan,
        target_notional_usd=round(float(plan.target_notional_usd or 0.0) * bounded, 6),
        margin_usd=round(float(plan.margin_usd or 0.0) * bounded, 6),
        risk_budget_usd=round(float(plan.risk_budget_usd or 0.0) * bounded, 6),
        expected_loss_usd=round(float(plan.expected_loss_usd or 0.0) * bounded, 6),
        setup_details=details,
    )


def _notional_scale_for_labels(profile: MicroRegimeProfile, labels: set[str]) -> float:
    scale = 1.0
    for label, value in profile.size_scales:
        if label in labels:
            scale *= max(min(float(value), 1.0), 0.0)
    return scale


def _snapshot_feature_mapping(snapshot: SymbolMarketSnapshot) -> dict[str, object]:
    return {
        "bucket_range_bps": snapshot.bucket_range_bps,
        "realized_vol_short_bps": snapshot.realized_vol_short_bps,
        "volume_ratio": snapshot.volume_ratio,
        "vwap_distance_bps": snapshot.vwap_distance_bps,
        "microprice_dislocation_bps": snapshot.microprice_dislocation_bps,
    }


def _micro_rejections(*pods: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pod in pods:
        for reason, count in dict(pod.get("rejections_by_reason", {}) or {}).items():
            if str(reason).startswith("micro_regime_"):
                counts[str(reason)] = counts.get(str(reason), 0) + _int(count)
    return counts


def _micro_scaled_closed_trades(trades: Sequence[object]) -> int:
    count = 0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        details = trade.get("setup_details")
        if not isinstance(details, dict):
            continue
        if _float(details.get("micro_regime_notional_scale"), default=1.0) < 1.0:
            count += 1
    return count


def _profit_factor(trades: Sequence[object]) -> float | None:
    gross_win = 0.0
    gross_loss = 0.0
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        pnl = _float(trade.get("pnl_usd"))
        if pnl >= 0:
            gross_win += pnl
        else:
            gross_loss += abs(pnl)
    if gross_loss <= 0.0:
        return None if gross_win <= 0.0 else float("inf")
    return round(gross_win / gross_loss, 4)


def _win_rate(trades: Sequence[object]) -> float | None:
    samples = [trade for trade in trades if isinstance(trade, dict)]
    if not samples:
        return None
    wins = sum(1 for trade in samples if _float(trade.get("pnl_usd")) >= 0.0)
    return round(wins / len(samples), 4)


def _parse_labeled_path(value: str) -> tuple[str, Path]:
    if "=" in value:
        label, raw_path = value.split("=", 1)
        label = _safe_path_label(label)
        path = Path(raw_path)
    else:
        path = Path(value)
        label = _safe_path_label(path.stem)
    if not path.is_absolute():
        path = ROOT / path
    return label, path


def _default_inputs() -> list[str]:
    return [f"recent_live_window={DEFAULT_RECENT_INPUT}"]


def _safe_path_label(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value.strip())
    return cleaned or "input"


def _safe_reason_label(value: str) -> str:
    return _safe_path_label(value.replace("::", "_").replace("|", "_")).lower()


def _float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _sort_delta(value: object) -> float:
    if value is None:
        return -1e18
    return _float(value, default=-1e18)


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "`baseline`"
    return f"${value:+.2f}"


def _fmt_int_delta(value: int | None) -> str:
    if value is None:
        return "`baseline`"
    return f"{value:+d}"


def _fmt_optional(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run P1-11 full A/C replay with micro-regime entry gates.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument(
        "--input",
        action="append",
        help="Replay input as LABEL=PATH. Defaults to the recent A/C live-window full-bot input.",
    )
    parser.add_argument(
        "--profile",
        action="append",
        help="Profile name to run. Defaults to all P1-11 profiles.",
    )
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--no-live-notional-caps",
        action="store_true",
        help="Do not apply current A/C live notional cap before replay execution.",
    )
    parser.add_argument(
        "--include-routing-replay",
        action="store_true",
        help="Also run the routing summary replay for every scenario.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = run_full_replay_suite(
        config_path=Path(args.config),
        input_sources=args.input or _default_inputs(),
        profile_names=args.profile,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        apply_live_notional_caps=not args.no_live_notional_caps,
        skip_routing_replay=not args.include_routing_replay,
    )
    print(f"output_dir={payload['output_dir']}")
    print(f"report_md={payload['report_md']}")
    decision = payload.get("decision", {})
    if isinstance(decision, dict):
        print(f"best_profile={decision.get('best_profile')}")


if __name__ == "__main__":
    main()
