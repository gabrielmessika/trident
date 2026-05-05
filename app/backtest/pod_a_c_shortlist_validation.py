from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backtest import full_bot_replay as full_bot_replay_module
from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import AppConfig, PodAPatternVetoConfig, load_config
from app.trident.market_clusters import cluster_for_symbol


POD_A_HYPE_VETO = "hype_trend_pullback_long_targeted"
POD_A_MTF_VETOES = {
    "mtf_4h_rsi14_weakness",
    "mtf_4h_close_below_ema50",
    "mtf_1h_chop_ema20_under_ema50_rsi40_50",
    "mtf_1h_overextension_chase",
}
POD_A_TARGETED_OVEREXTENSION_VETOES = {
    "btc_overextension_4h",
    "xrp_overextension_4h_targeted",
}
POD_C_SILVER_VETO = "silver_strong_extension_veto"


class _NoopRoutingReplayRunner:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def run_jsonl(self, *_args: object, **_kwargs: object) -> "_NoopRoutingReplayRunner":
        return self

    def to_dict(self) -> dict[str, object]:
        return {"skipped": True, "reason": "pod_a_c_shortlist_validation"}


@dataclass(slots=True)
class TradeStats:
    trades: int = 0
    pnl_usd: float = 0.0
    wins: int = 0
    losses: int = 0

    @property
    def win_rate(self) -> float:
        return round(self.wins / self.trades, 4) if self.trades else 0.0

    def add(self, pnl_usd: float) -> None:
        self.trades += 1
        self.pnl_usd = round(self.pnl_usd + pnl_usd, 2)
        if pnl_usd >= 0.0:
            self.wins += 1
        else:
            self.losses += 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"win_rate": self.win_rate}


@dataclass(slots=True)
class ScenarioResult:
    step: str
    name: str
    verdict: str
    before_total_pnl_usd: float
    after_total_pnl_usd: float
    total_delta_usd: float
    before_pod_a_pnl_usd: float
    after_pod_a_pnl_usd: float
    pod_a_delta_usd: float
    before_pod_c_pnl_usd: float
    after_pod_c_pnl_usd: float
    pod_c_delta_usd: float
    before_target: TradeStats
    after_target: TradeStats
    veto_rejections: int
    note: str
    runtime_seconds: float

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["before_target"] = self.before_target.to_dict()
        payload["after_target"] = self.after_target.to_dict()
        return payload


def _install_fast_routing_replay() -> None:
    full_bot_replay_module.RoutingReplayRunner = _NoopRoutingReplayRunner


def _normalize_name(name: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "_" for character in name)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


def _veto_reason(name: str) -> str:
    return f"pattern_veto_{_normalize_name(name)}"


def _remove_pod_a_vetoes(config: AppConfig, names: set[str]) -> AppConfig:
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            pattern_vetoes=[
                rule for rule in config.pod_a.pattern_vetoes if rule.name not in names
            ],
        ),
    )


def _remove_pod_c_vetoes(config: AppConfig, names: set[str]) -> AppConfig:
    return replace(
        config,
        pod_c=replace(
            config.pod_c,
            pattern_vetoes=[
                rule for rule in config.pod_c.pattern_vetoes if rule.name not in names
            ],
        ),
    )


def _add_pod_c_veto(config: AppConfig, rule: PodAPatternVetoConfig) -> AppConfig:
    config = _remove_pod_c_vetoes(config, {rule.name})
    return replace(
        config,
        pod_c=replace(config.pod_c, pattern_vetoes=list(config.pod_c.pattern_vetoes) + [rule]),
    )


def _relaxed_pod_c_config(config: AppConfig) -> AppConfig:
    return replace(
        config,
        pod_c=replace(
            config.pod_c,
            cluster_aware_v2_enabled=False,
            pattern_vetoes=[],
        ),
    )


def _run(config: AppConfig, input_path: Path) -> tuple[FullBotBacktestResult, float]:
    started = time.perf_counter()
    result = FullBotBacktestRunner(config, force_enable_all_pods=False).run_jsonl(
        input_path=input_path,
        dedupe_by_timestamp=True,
    )
    return result, time.perf_counter() - started


def _pod_pnl(result: FullBotBacktestResult, pod: str) -> float:
    payload = getattr(result, pod)
    return float(payload.get("realized_pnl_usd", 0.0) or 0.0)


def _pod_a_target_stats(
    result: FullBotBacktestResult,
    *,
    symbols: set[str] | None = None,
    setup: str = "trend_pullback_long",
) -> TradeStats:
    stats = TradeStats()
    for trade in result.pod_a.get("closed_trade_log", []) or []:
        if not isinstance(trade, dict):
            continue
        if symbols is not None and str(trade.get("symbol", "")).upper() not in symbols:
            continue
        if str(trade.get("setup", "")) != setup:
            continue
        stats.add(float(trade.get("pnl_usd", 0.0) or 0.0))
    return stats


def _string_set(values: list[str]) -> set[str]:
    return {str(item).strip() for item in values if str(item).strip()}


def _cluster_set(values: list[str]) -> set[str]:
    return {str(item).strip().lower() for item in values if str(item).strip()}


def _trade_matches_rule(trade: dict[str, object], rule: PodAPatternVetoConfig) -> bool:
    details = trade.get("setup_details", {})
    if not isinstance(details, dict):
        details = {}
    if rule.setups and str(trade.get("setup", "")).strip() not in _string_set(rule.setups):
        return False
    if rule.sides and str(trade.get("side", "")).strip() not in _string_set(rule.sides):
        return False
    if rule.symbols and str(trade.get("symbol", "")).strip().upper() not in {
        item.upper() for item in _string_set(rule.symbols)
    }:
        return False
    market_cluster = str(details.get("market_cluster", trade.get("market_cluster", ""))).strip().lower()
    if rule.market_clusters and market_cluster not in _cluster_set(rule.market_clusters):
        return False
    exact_fields = (
        ("cluster_strategies", "cluster_strategy"),
        ("trend_buckets", "trend_bucket"),
        ("structure_buckets", "structure_bucket"),
        ("vwap_buckets", "vwap_bucket"),
        ("activity_buckets", "activity_bucket"),
        ("trade_count_buckets", "trade_count_bucket"),
        ("flow_buckets", "flow_bucket"),
        ("flow_alignments", "flow_alignment"),
    )
    for attr, detail_key in exact_fields:
        expected = _string_set(getattr(rule, attr))
        if expected and str(details.get(detail_key, "")).strip() not in expected:
            return False
    return True


def _pod_c_rule_target_stats(result: FullBotBacktestResult, rule: PodAPatternVetoConfig) -> TradeStats:
    stats = TradeStats()
    for trade in result.pod_c.get("closed_trade_log", []) or []:
        if isinstance(trade, dict) and _trade_matches_rule(trade, rule):
            stats.add(float(trade.get("pnl_usd", 0.0) or 0.0))
    return stats


def _pod_c_all_stats(result: FullBotBacktestResult) -> TradeStats:
    stats = TradeStats()
    for trade in result.pod_c.get("closed_trade_log", []) or []:
        if isinstance(trade, dict):
            stats.add(float(trade.get("pnl_usd", 0.0) or 0.0))
    return stats


def _sum_rejections(result: FullBotBacktestResult, *, pod: str, veto_names: set[str]) -> int:
    payload = getattr(result, pod)
    rejections = payload.get("rejections_by_reason", {}) or {}
    return sum(int(rejections.get(_veto_reason(name), 0) or 0) for name in veto_names)


def _scenario_result(
    *,
    step: str,
    name: str,
    before: FullBotBacktestResult,
    after: FullBotBacktestResult,
    before_target: TradeStats,
    after_target: TradeStats,
    veto_rejections: int,
    runtime_seconds: float,
    positive_pod: str,
    note_keep: str,
    note_reject: str,
    note_no_effect: str,
) -> ScenarioResult:
    total_delta = round(after.total_realized_pnl_usd - before.total_realized_pnl_usd, 4)
    pod_a_delta = round(_pod_pnl(after, "pod_a") - _pod_pnl(before, "pod_a"), 4)
    pod_c_delta = round(_pod_pnl(after, "pod_c") - _pod_pnl(before, "pod_c"), 4)
    positive_delta = pod_a_delta if positive_pod == "pod_a" else pod_c_delta
    if abs(total_delta) < 1e-9 and veto_rejections <= 0 and before_target.trades <= 0:
        verdict = "no_effect"
        note = note_no_effect
    elif total_delta > 0.0 and positive_delta > 0.0:
        verdict = "keep"
        note = note_keep
    elif abs(total_delta) < 1e-9:
        verdict = "no_effect"
        note = note_no_effect
    else:
        verdict = "reject"
        note = note_reject
    return ScenarioResult(
        step=step,
        name=name,
        verdict=verdict,
        before_total_pnl_usd=float(before.total_realized_pnl_usd),
        after_total_pnl_usd=float(after.total_realized_pnl_usd),
        total_delta_usd=total_delta,
        before_pod_a_pnl_usd=_pod_pnl(before, "pod_a"),
        after_pod_a_pnl_usd=_pod_pnl(after, "pod_a"),
        pod_a_delta_usd=pod_a_delta,
        before_pod_c_pnl_usd=_pod_pnl(before, "pod_c"),
        after_pod_c_pnl_usd=_pod_pnl(after, "pod_c"),
        pod_c_delta_usd=pod_c_delta,
        before_target=before_target,
        after_target=after_target,
        veto_rejections=veto_rejections,
        note=note,
        runtime_seconds=round(runtime_seconds, 3),
    )


def _gold_candidates() -> list[PodAPatternVetoConfig]:
    return [
        PodAPatternVetoConfig(
            name="gold_soft_extension_veto",
            enabled=True,
            setups=["tradfi_continuation_long"],
            sides=["long"],
            market_clusters=["gold"],
            trend_buckets=["soft"],
            structure_buckets=["strong"],
            vwap_buckets=["extension"],
        ),
        PodAPatternVetoConfig(
            name="gold_strong_neutral_veto",
            enabled=True,
            setups=["tradfi_continuation_long"],
            sides=["long"],
            market_clusters=["gold"],
            trend_buckets=["strong"],
            structure_buckets=["strong"],
            vwap_buckets=["neutral"],
        ),
        PodAPatternVetoConfig(
            name="gold_medium_neutral_veto",
            enabled=True,
            setups=["tradfi_continuation_long"],
            sides=["long"],
            market_clusters=["gold"],
            trend_buckets=["medium"],
            structure_buckets=["strong"],
            vwap_buckets=["neutral"],
        ),
    ]


def _silver_rule() -> PodAPatternVetoConfig:
    return PodAPatternVetoConfig(
        name=POD_C_SILVER_VETO,
        enabled=True,
        setups=["tradfi_continuation_long"],
        sides=["long"],
        market_clusters=["silver"],
        trend_buckets=["strong"],
        structure_buckets=["strong"],
        vwap_buckets=["extension"],
    )


def _pod_c_symbol_rows(config: AppConfig, result: FullBotBacktestResult) -> list[dict[str, object]]:
    stats_by_symbol: dict[str, TradeStats] = {}
    strategies_by_symbol: dict[str, dict[str, TradeStats]] = {}
    reasons_by_symbol: dict[str, dict[str, int]] = {}
    for trade in result.pod_c.get("closed_trade_log", []) or []:
        if not isinstance(trade, dict):
            continue
        symbol = str(trade.get("symbol", "")).upper()
        if not symbol:
            continue
        pnl = float(trade.get("pnl_usd", 0.0) or 0.0)
        stats_by_symbol.setdefault(symbol, TradeStats()).add(pnl)
        details = trade.get("setup_details", {})
        if not isinstance(details, dict):
            details = {}
        strategy = str(details.get("cluster_strategy", trade.get("setup", "unknown")) or "unknown")
        strategies_by_symbol.setdefault(symbol, {}).setdefault(strategy, TradeStats()).add(pnl)
        reason = str(trade.get("close_reason", "unknown") or "unknown")
        reasons = reasons_by_symbol.setdefault(symbol, {})
        reasons[reason] = reasons.get(reason, 0) + 1

    rows: list[dict[str, object]] = []
    for symbol in config.hyperliquid.observation_universe:
        if ":" not in symbol:
            continue
        cluster = cluster_for_symbol(config, symbol)
        symbol_stats = stats_by_symbol.get(symbol.upper(), TradeStats())
        strategies = strategies_by_symbol.get(symbol.upper(), {})
        best_strategy = "-"
        if strategies:
            best_name, best_stats = max(
                strategies.items(),
                key=lambda item: (item[1].pnl_usd, item[1].trades),
            )
            best_strategy = f"{best_name} ({best_stats.pnl_usd:+.2f}/{best_stats.trades}t)"
        note = "active_positive" if symbol_stats.pnl_usd > 0 else "no_current_trade"
        if cluster in {"equity", "fx"}:
            note = "observation_only"
        elif symbol_stats.trades > 0 and symbol_stats.pnl_usd <= 0:
            note = "watch_or_filter"
        rows.append(
            {
                "symbol": symbol,
                "cluster": cluster,
                "trades": symbol_stats.trades,
                "pnl_usd": symbol_stats.pnl_usd,
                "win_rate": symbol_stats.win_rate,
                "best_pattern": best_strategy,
                "close_reasons": reasons_by_symbol.get(symbol.upper(), {}),
                "note": note,
            }
        )
    return rows


def _pod_c_strategy_rows(result: FullBotBacktestResult) -> list[dict[str, object]]:
    stats: dict[str, TradeStats] = {}
    for trade in result.pod_c.get("closed_trade_log", []) or []:
        if not isinstance(trade, dict):
            continue
        details = trade.get("setup_details", {})
        if not isinstance(details, dict):
            details = {}
        cluster = str(details.get("market_cluster", trade.get("market_cluster", "unknown")))
        strategy = str(details.get("cluster_strategy", trade.get("setup", "unknown")))
        stats.setdefault(f"{cluster}|{strategy}", TradeStats()).add(
            float(trade.get("pnl_usd", 0.0) or 0.0)
        )
    return [
        {"pattern": key, **item.to_dict()}
        for key, item in sorted(stats.items(), key=lambda pair: pair[1].pnl_usd, reverse=True)
    ]


def _pod_c_daily_rows(result: FullBotBacktestResult) -> list[dict[str, object]]:
    pod_c = result.pod_c
    dates = sorted(
        set(pod_c.get("records_by_date", {}) or {})
        | set(pod_c.get("signals_by_date", {}) or {})
        | set(pod_c.get("accepted_by_date", {}) or {})
        | set(pod_c.get("rejected_by_date", {}) or {})
        | set(pod_c.get("pnl_by_date", {}) or {})
    )
    rows = []
    for date_key in dates:
        rows.append(
            {
                "date": date_key,
                "records": int((pod_c.get("records_by_date", {}) or {}).get(date_key, 0) or 0),
                "signals": int((pod_c.get("signals_by_date", {}) or {}).get(date_key, 0) or 0),
                "accepted": int((pod_c.get("accepted_by_date", {}) or {}).get(date_key, 0) or 0),
                "rejected": int((pod_c.get("rejected_by_date", {}) or {}).get(date_key, 0) or 0),
                "trades": int((pod_c.get("trades_by_date", {}) or {}).get(date_key, 0) or 0),
                "pnl_usd": float((pod_c.get("pnl_by_date", {}) or {}).get(date_key, 0.0) or 0.0),
            }
        )
    return rows


def _baseline_payload(result: FullBotBacktestResult, runtime_seconds: float) -> dict[str, object]:
    return {
        "total_pnl_usd": float(result.total_realized_pnl_usd),
        "pod_a_pnl_usd": _pod_pnl(result, "pod_a"),
        "pod_b_pnl_usd": _pod_pnl(result, "pod_b"),
        "pod_c_pnl_usd": _pod_pnl(result, "pod_c"),
        "pod_a_trades": int(result.pod_a.get("closed_trade_count", 0) or 0),
        "pod_c_trades": int(result.pod_c.get("closed_trade_count", 0) or 0),
        "records_processed": int(result.records_processed),
        "duplicates_skipped": int(result.duplicate_timestamps_skipped),
        "first_timestamp": result.first_timestamp,
        "last_timestamp": result.last_timestamp,
        "dates_covered": result.dates_covered,
        "runtime_seconds": round(runtime_seconds, 3),
    }


def _write_outputs(payload: dict[str, object], *, output_json: Path, output_md: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_render_markdown(payload), encoding="utf-8")


def _render_markdown(payload: dict[str, object]) -> str:
    baseline = payload["baseline"]
    lines = [
        "# Pod A / Pod C Shortlist Validation",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Input: `{payload['input_path']}`",
        f"- Config baseline: `{payload['config_path']}`",
        "- Routing replay: `skipped` for speed; directional Pod A/Pod C replay uses the full stream.",
        f"- Baseline total PnL: `{baseline['total_pnl_usd']:.2f}` "
        f"(Pod A `{baseline['pod_a_pnl_usd']:.2f}`, Pod C `{baseline['pod_c_pnl_usd']:.2f}`)",
        f"- Window: `{baseline['first_timestamp']}` -> `{baseline['last_timestamp']}`, "
        f"records `{baseline['records_processed']}`",
        "",
        "## Scenario Results",
        "",
        "| Step | Scenario | Verdict | Before | After | Delta | Pod A delta | Pod C delta | Target before | Target after | Vetoes | Note |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in payload["scenario_results"]:
        before = item["before_target"]
        after = item["after_target"]
        lines.append(
            f"| {item['step']} | `{item['name']}` | `{item['verdict']}` | "
            f"{item['before_total_pnl_usd']:.2f} | {item['after_total_pnl_usd']:.2f} | "
            f"{item['total_delta_usd']:+.2f} | {item['pod_a_delta_usd']:+.2f} | "
            f"{item['pod_c_delta_usd']:+.2f} | {before['pnl_usd']:.2f}/{before['trades']}t | "
            f"{after['pnl_usd']:.2f}/{after['trades']}t | {item['veto_rejections']} | "
            f"{item['note']} |"
        )

    lines.extend(
        [
            "",
            "## Pod C Daily Freshness",
            "",
            "| Date | Records | Signals | Accepted | Rejected | Closed trades | PnL |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload["pod_c_daily_rows"][-8:]:
        lines.append(
            f"| {row['date']} | {row['records']} | {row['signals']} | {row['accepted']} | "
            f"{row['rejected']} | {row['trades']} | {row['pnl_usd']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## Pod C Cluster Drilldown",
            "",
            "| Symbol | Cluster | Best pattern | Trades | PnL | Win rate | Note |",
            "|---|---|---|---:|---:|---:|---|",
        ]
    )
    for row in payload["pod_c_symbol_rows"]:
        lines.append(
            f"| {row['symbol']} | {row['cluster']} | {row['best_pattern']} | "
            f"{row['trades']} | {row['pnl_usd']:.2f} | {row['win_rate']:.2f} | {row['note']} |"
        )

    lines.extend(
        [
            "",
            "## Pod C Strategy Summary",
            "",
            "| Pattern | Trades | PnL | Win rate |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in payload["pod_c_strategy_rows"]:
        lines.append(
            f"| {row['pattern']} | {row['trades']} | {row['pnl_usd']:.2f} | "
            f"{row['win_rate']:.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def run_validation(
    *,
    config_path: Path,
    input_path: Path,
    output_json: Path,
    output_md: Path,
) -> dict[str, object]:
    _install_fast_routing_replay()
    base_config = load_config(str(config_path))

    print("baseline_current status=running", flush=True)
    baseline, baseline_runtime = _run(base_config, input_path)
    print(
        f"baseline_current total={baseline.total_realized_pnl_usd:.2f} "
        f"pod_a={_pod_pnl(baseline, 'pod_a'):.2f} pod_c={_pod_pnl(baseline, 'pod_c'):.2f} "
        f"seconds={baseline_runtime:.1f}",
        flush=True,
    )

    scenario_results: list[ScenarioResult] = []
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "input_path": str(input_path),
        "config_path": str(config_path),
        "baseline": _baseline_payload(baseline, baseline_runtime),
        "scenario_results": [],
        "pod_c_daily_rows": _pod_c_daily_rows(baseline),
        "pod_c_symbol_rows": _pod_c_symbol_rows(base_config, baseline),
        "pod_c_strategy_rows": _pod_c_strategy_rows(baseline),
    }
    _write_outputs(payload, output_json=output_json, output_md=output_md)

    def add_result(result: ScenarioResult) -> None:
        scenario_results.append(result)
        payload["scenario_results"] = [item.to_dict() for item in scenario_results]
        _write_outputs(payload, output_json=output_json, output_md=output_md)

    print("pod_a_hype_control_without_veto status=running", flush=True)
    control_config = _remove_pod_a_vetoes(base_config, {POD_A_HYPE_VETO})
    control, runtime = _run(control_config, input_path)
    add_result(
        _scenario_result(
            step="1_pod_a_hype",
            name="current_hype_veto_vs_without_hype_veto",
            before=control,
            after=baseline,
            before_target=_pod_a_target_stats(control, symbols={"HYPE"}),
            after_target=_pod_a_target_stats(baseline, symbols={"HYPE"}),
            veto_rejections=_sum_rejections(baseline, pod="pod_a", veto_names={POD_A_HYPE_VETO}),
            runtime_seconds=runtime,
            positive_pod="pod_a",
            note_keep="HYPE trend_pullback veto improves the current full replay.",
            note_reject="HYPE trend_pullback veto hurts the current full replay.",
            note_no_effect="No measurable HYPE trend_pullback effect on this window.",
        )
    )

    print("pod_a_mtf_control_without_vetoes status=running", flush=True)
    control_config = _remove_pod_a_vetoes(base_config, POD_A_MTF_VETOES)
    control, runtime = _run(control_config, input_path)
    add_result(
        _scenario_result(
            step="2_pod_a_mtf",
            name="current_mtf_vetoes_vs_without_mtf_vetoes",
            before=control,
            after=baseline,
            before_target=_pod_a_target_stats(control),
            after_target=_pod_a_target_stats(baseline),
            veto_rejections=_sum_rejections(baseline, pod="pod_a", veto_names=POD_A_MTF_VETOES),
            runtime_seconds=runtime,
            positive_pod="pod_a",
            note_keep="Promoted MTF vetoes still improve the current full replay.",
            note_reject="Promoted MTF vetoes no longer improve the current full replay.",
            note_no_effect="No MTF veto effect on this window.",
        )
    )

    print("pod_a_overextension_control_without_targeted_vetoes status=running", flush=True)
    control_config = _remove_pod_a_vetoes(base_config, POD_A_TARGETED_OVEREXTENSION_VETOES)
    control, runtime = _run(control_config, input_path)
    add_result(
        _scenario_result(
            step="2_pod_a_overextension",
            name="current_btc_xrp_overextension_vs_without_targeted_overextension",
            before=control,
            after=baseline,
            before_target=_pod_a_target_stats(control, symbols={"BTC", "XRP"}),
            after_target=_pod_a_target_stats(baseline, symbols={"BTC", "XRP"}),
            veto_rejections=_sum_rejections(
                baseline,
                pod="pod_a",
                veto_names=POD_A_TARGETED_OVEREXTENSION_VETOES,
            ),
            runtime_seconds=runtime,
            positive_pod="pod_a",
            note_keep="BTC/XRP targeted overextension vetoes improve the current full replay.",
            note_reject="BTC/XRP targeted overextension vetoes hurt the current full replay.",
            note_no_effect="No BTC/XRP targeted overextension effect on this window.",
        )
    )

    print("pod_c_relaxed_cluster_probe status=running", flush=True)
    relaxed, runtime = _run(_relaxed_pod_c_config(base_config), input_path)
    add_result(
        _scenario_result(
            step="3_pod_c_relaxed",
            name="current_pod_c_vs_relaxed_cluster_aware_off",
            before=baseline,
            after=relaxed,
            before_target=_pod_c_all_stats(baseline),
            after_target=_pod_c_all_stats(relaxed),
            veto_rejections=0,
            runtime_seconds=runtime,
            positive_pod="pod_c",
            note_keep="Relaxing Pod C improves this replay; investigate broader branch.",
            note_reject="Relaxing Pod C degrades this replay; keep current selectivity.",
            note_no_effect="Relaxing Pod C has no measurable effect on this window.",
        )
    )

    print("pod_c_silver_control_without_veto status=running", flush=True)
    silver_control, runtime = _run(_remove_pod_c_vetoes(base_config, {POD_C_SILVER_VETO}), input_path)
    silver_rule = _silver_rule()
    add_result(
        _scenario_result(
            step="4_pod_c_silver",
            name="current_silver_veto_vs_without_silver_veto",
            before=silver_control,
            after=baseline,
            before_target=_pod_c_rule_target_stats(silver_control, silver_rule),
            after_target=_pod_c_rule_target_stats(baseline, silver_rule),
            veto_rejections=_sum_rejections(baseline, pod="pod_c", veto_names={POD_C_SILVER_VETO}),
            runtime_seconds=runtime,
            positive_pod="pod_c",
            note_keep="Silver strong extension veto remains additive.",
            note_reject="Silver strong extension veto is not additive on this replay.",
            note_no_effect="Silver veto has no measurable effect on this window.",
        )
    )

    for rule in _gold_candidates():
        print(f"pod_c_gold_candidate {rule.name} status=running", flush=True)
        scenario, runtime = _run(_add_pod_c_veto(base_config, rule), input_path)
        add_result(
            _scenario_result(
                step="5_pod_c_gold",
                name=f"add_{rule.name}",
                before=baseline,
                after=scenario,
                before_target=_pod_c_rule_target_stats(baseline, rule),
                after_target=_pod_c_rule_target_stats(scenario, rule),
                veto_rejections=_sum_rejections(scenario, pod="pod_c", veto_names={rule.name}),
                runtime_seconds=runtime,
                positive_pod="pod_c",
                note_keep=f"{rule.name} is additive on this replay.",
                note_reject=f"{rule.name} is not additive on this replay.",
                note_no_effect=f"{rule.name} has no measurable effect on this window.",
            )
        )

    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the current Pod A / Pod C shortlist validations on a full replay.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument(
        "--output-json",
        default="server-data/replay_reports/pod_a_c_shortlist_validation_20260505.json",
    )
    parser.add_argument(
        "--output-md",
        default="server-data/replay_reports/pod_a_c_shortlist_validation_20260505.md",
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
    print(f"output_json={args.output_json}", flush=True)
    print(f"output_md={args.output_md}", flush=True)
    print(json.dumps(payload["scenario_results"], indent=2), flush=True)


if __name__ == "__main__":
    main()
