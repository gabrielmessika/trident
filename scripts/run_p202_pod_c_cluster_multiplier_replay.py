#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest.pod_c_runner import PodCBacktestRunner
from app.settings import AppConfig, load_config
from app.trident.pod_c import TradfiTrendPlanner
from app.trident.pod_c.signals import TradfiTrendSignal
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodAllocation, TradePlan


DEFAULT_EXTENDED_INPUT = (
    "server-data/replay_inputs/full_bot_live_window_20260524T1605_20260611_no_external_reference.jsonl"
)
DEFAULT_HISTORICAL_GLOBAL_JSON = (
    "server-data/replay_reports/pod_c_cluster_multiplier_global_20260526/"
    "pod_c_cluster_multiplier_compare.json"
)
DEFAULT_HISTORICAL_RECENT_JSON = (
    "server-data/replay_reports/pod_c_cluster_multiplier_recent_20260526/"
    "pod_c_cluster_multiplier_compare.json"
)
BASELINE_MULTIPLIER = 0.55
PROMOTION_ACTIVITY_RATIO_CAP = 1.20
MIN_CLUSTER_TRADES_FOR_PROMOTION = 3


@dataclass(slots=True)
class Scenario:
    name: str
    description: str
    global_multiplier: float
    cluster_multipliers: dict[str, float]
    blocked_symbols: list[str]


@dataclass(slots=True)
class ScenarioResult:
    scenario: str
    description: str
    input_path: str
    global_multiplier: float
    cluster_multipliers: dict[str, float]
    blocked_symbols: list[str]
    runtime_seconds: float
    date_start: str | None
    date_end: str | None
    date_count: int
    records_processed: int
    signal_count: int
    accepted_count: int
    rejected_count: int
    opened_count: int
    skipped_open_count: int
    closed_trade_count: int
    win_rate: float | None
    profit_factor: float | None
    realized_pnl_usd: float
    gross_pnl_usd: float
    fees_usd: float
    max_drawdown_usd: float
    delta_vs_extended_baseline_usd: float | None
    trade_ratio_vs_extended_baseline: float | None
    fees_ratio_vs_extended_baseline: float | None
    rejections_by_reason: dict[str, int]
    close_reasons: dict[str, int]
    pnl_by_cluster: dict[str, dict[str, float | int]]
    pnl_by_symbol: dict[str, dict[str, float | int]]
    pnl_by_date: dict[str, float]


class ClusterMultiplierTradfiPlanner(TradfiTrendPlanner):
    """Research-only planner that can override size_multiplier by cluster."""

    def __init__(
        self,
        config: AppConfig,
        *,
        cluster_multipliers: dict[str, float],
    ) -> None:
        super().__init__(config)
        self._cluster_multipliers = {
            str(cluster).strip().lower(): float(multiplier)
            for cluster, multiplier in cluster_multipliers.items()
        }

    def build_trade_plan(
        self,
        signal: TradfiTrendSignal,
        pod_allocation: PodAllocation,
    ) -> TradePlan | None:
        cluster = str(signal.market_cluster).strip().lower()
        if cluster not in self._cluster_multipliers:
            return super().build_trade_plan(signal, pod_allocation)
        original_multiplier = self.config.size_multiplier
        self.config.size_multiplier = self._cluster_multipliers[cluster]
        try:
            return super().build_trade_plan(signal, pod_allocation)
        finally:
            self.config.size_multiplier = original_multiplier


class ClusterMultiplierPodCBacktestRunner(PodCBacktestRunner):
    def __init__(
        self,
        config: AppConfig,
        *,
        cluster_multipliers: dict[str, float],
    ) -> None:
        super().__init__(config)
        self._cluster_multipliers = cluster_multipliers

    def _build_supervisor(self) -> TridentSupervisor:
        supervisor = super()._build_supervisor()
        supervisor.pod_c_planner = ClusterMultiplierTradfiPlanner(
            self.config,
            cluster_multipliers=self._cluster_multipliers,
        )
        return supervisor


SCENARIOS = [
    Scenario(
        name="current_live_blocked",
        description="Current production-shape config: size_multiplier=0.70, XYZ:SILVER blocked.",
        global_multiplier=0.70,
        cluster_multipliers={},
        blocked_symbols=["XYZ:SILVER"],
    ),
    Scenario(
        name="baseline_055",
        description="Official historical baseline shape: global Pod C size_multiplier=0.55, silver unblocked.",
        global_multiplier=BASELINE_MULTIPLIER,
        cluster_multipliers={},
        blocked_symbols=[],
    ),
    Scenario(
        name="global_065",
        description="Global Pod C size_multiplier=0.65, silver unblocked.",
        global_multiplier=0.65,
        cluster_multipliers={},
        blocked_symbols=[],
    ),
    Scenario(
        name="global_070",
        description="Global Pod C size_multiplier=0.70, silver unblocked.",
        global_multiplier=0.70,
        cluster_multipliers={},
        blocked_symbols=[],
    ),
    Scenario(
        name="gold_070",
        description="Only gold uses size_multiplier=0.70; other clusters stay at 0.55.",
        global_multiplier=BASELINE_MULTIPLIER,
        cluster_multipliers={"gold": 0.70},
        blocked_symbols=[],
    ),
    Scenario(
        name="silver_070",
        description="Only silver uses size_multiplier=0.70; other clusters stay at 0.55.",
        global_multiplier=BASELINE_MULTIPLIER,
        cluster_multipliers={"silver": 0.70},
        blocked_symbols=[],
    ),
    Scenario(
        name="metals_070",
        description="Gold and silver use size_multiplier=0.70; other clusters stay at 0.55.",
        global_multiplier=BASELINE_MULTIPLIER,
        cluster_multipliers={"gold": 0.70, "silver": 0.70},
        blocked_symbols=[],
    ),
]


def scenario_config(base: AppConfig, scenario: Scenario) -> AppConfig:
    return replace(
        base,
        trident=replace(
            base.trident,
            routing=replace(
                base.trident.routing,
                symbol_pod_overrides={},
                runtime_override_path="tmp/p202_empty_symbol_routing_overrides.json",
            ),
        ),
        pod_c=replace(
            base.pod_c,
            enabled=True,
            size_multiplier=scenario.global_multiplier,
            blocked_symbols=list(scenario.blocked_symbols),
        ),
    )


def run_scenarios(
    *,
    config_path: Path,
    input_path: Path,
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    extended_baseline: ScenarioResult | None = None
    for scenario in SCENARIOS:
        config = scenario_config(load_config(str(config_path)), scenario)
        started = time.perf_counter()
        backtest = ClusterMultiplierPodCBacktestRunner(
            config,
            cluster_multipliers=scenario.cluster_multipliers,
        ).run_jsonl(input_path).backtest
        runtime = time.perf_counter() - started
        result = build_result(
            scenario=scenario,
            input_path=input_path,
            backtest=backtest,
            extended_baseline=extended_baseline,
            runtime=runtime,
        )
        if scenario.name == "baseline_055":
            extended_baseline = result
            result.delta_vs_extended_baseline_usd = 0.0
            result.trade_ratio_vs_extended_baseline = 1.0
            result.fees_ratio_vs_extended_baseline = 1.0
        results.append(result)
        print(
            f"scenario={scenario.name} pnl={result.realized_pnl_usd:.2f} "
            f"trades={result.closed_trade_count} fees={result.fees_usd:.4f} "
            f"seconds={runtime:.1f}",
            flush=True,
        )
    return _fill_baseline_deltas(results)


def build_result(
    *,
    scenario: Scenario,
    input_path: Path,
    backtest: dict[str, object],
    extended_baseline: ScenarioResult | None,
    runtime: float,
) -> ScenarioResult:
    dates = sorted(str(item) for item in dict(backtest.get("records_by_date", {}) or {}).keys())
    realized_pnl = _float(backtest.get("realized_pnl_usd"))
    fees = _float(backtest.get("fees_usd"))
    trades = _int(backtest.get("closed_trade_count"))
    baseline_trades = extended_baseline.closed_trade_count if extended_baseline is not None else 0
    baseline_fees = extended_baseline.fees_usd if extended_baseline is not None else 0.0
    return ScenarioResult(
        scenario=scenario.name,
        description=scenario.description,
        input_path=str(input_path),
        global_multiplier=scenario.global_multiplier,
        cluster_multipliers=dict(scenario.cluster_multipliers),
        blocked_symbols=list(scenario.blocked_symbols),
        runtime_seconds=round(runtime, 3),
        date_start=dates[0] if dates else None,
        date_end=dates[-1] if dates else None,
        date_count=len(dates),
        records_processed=_int(backtest.get("records_processed")),
        signal_count=_int(backtest.get("signal_count")),
        accepted_count=_int(backtest.get("accepted_count")),
        rejected_count=_int(backtest.get("rejected_count")),
        opened_count=_int(backtest.get("opened_count")),
        skipped_open_count=_int(backtest.get("skipped_open_count")),
        closed_trade_count=trades,
        win_rate=_optional_float(backtest.get("win_rate")),
        profit_factor=_profit_factor(backtest),
        realized_pnl_usd=realized_pnl,
        gross_pnl_usd=_float(backtest.get("gross_pnl_usd")),
        fees_usd=fees,
        max_drawdown_usd=_float(backtest.get("max_drawdown_usd")),
        delta_vs_extended_baseline_usd=(
            round(realized_pnl - extended_baseline.realized_pnl_usd, 4)
            if extended_baseline is not None
            else None
        ),
        trade_ratio_vs_extended_baseline=(
            round(trades / baseline_trades, 4) if baseline_trades > 0 else None
        ),
        fees_ratio_vs_extended_baseline=(
            round(fees / baseline_fees, 4) if baseline_fees > 0 else None
        ),
        rejections_by_reason=_int_dict(backtest.get("rejections_by_reason")),
        close_reasons=_int_dict(backtest.get("close_reasons")),
        pnl_by_cluster=_closed_trade_breakdown(backtest, key="market_cluster"),
        pnl_by_symbol=_closed_trade_breakdown(backtest, key="symbol"),
        pnl_by_date={str(k): _float(v) for k, v in dict(backtest.get("pnl_by_date", {}) or {}).items()},
    )


def _fill_baseline_deltas(results: list[ScenarioResult]) -> list[ScenarioResult]:
    baseline = next((item for item in results if item.scenario == "baseline_055"), None)
    if baseline is None:
        return results
    for item in results:
        item.delta_vs_extended_baseline_usd = round(
            item.realized_pnl_usd - baseline.realized_pnl_usd,
            4,
        )
        item.trade_ratio_vs_extended_baseline = (
            round(item.closed_trade_count / baseline.closed_trade_count, 4)
            if baseline.closed_trade_count > 0
            else None
        )
        item.fees_ratio_vs_extended_baseline = (
            round(item.fees_usd / baseline.fees_usd, 4) if baseline.fees_usd > 0 else None
        )
    return results


def build_cluster_statuses(results: list[ScenarioResult]) -> dict[str, dict[str, object]]:
    baseline = next((item for item in results if item.scenario == "baseline_055"), None)
    if baseline is None:
        return {}
    result_by_name = {item.scenario: item for item in results}
    mapping = {
        "gold": "gold_070",
        "silver": "silver_070",
        "index": "global_070",
        "oil": "global_070",
        "equity": "global_070",
        "fx": "global_070",
    }
    statuses: dict[str, dict[str, object]] = {}
    for cluster, scenario_name in mapping.items():
        candidate = result_by_name.get(scenario_name)
        base_cluster = baseline.pnl_by_cluster.get(cluster, {})
        candidate_cluster = candidate.pnl_by_cluster.get(cluster, {}) if candidate else {}
        base_trades = _int(base_cluster.get("trades"))
        candidate_trades = _int(candidate_cluster.get("trades"))
        base_pnl = _float(base_cluster.get("pnl_usd"))
        candidate_pnl = _float(candidate_cluster.get("pnl_usd"))
        delta = round(candidate_pnl - base_pnl, 4)
        total_delta = candidate.delta_vs_extended_baseline_usd if candidate else None
        trade_ratio = (
            round(candidate.closed_trade_count / baseline.closed_trade_count, 4)
            if candidate and baseline.closed_trade_count > 0
            else None
        )
        status, reason = _cluster_status(
            cluster=cluster,
            candidate=candidate,
            base_trades=base_trades,
            candidate_trades=candidate_trades,
            cluster_delta=delta,
            total_delta=total_delta,
            trade_ratio=trade_ratio,
        )
        statuses[cluster] = {
            "status": status,
            "scenario": scenario_name,
            "baseline_trades": base_trades,
            "candidate_trades": candidate_trades,
            "baseline_pnl_usd": round(base_pnl, 4),
            "candidate_pnl_usd": round(candidate_pnl, 4),
            "cluster_delta_usd": delta,
            "scenario_total_delta_usd": total_delta,
            "scenario_trade_ratio": trade_ratio,
            "reason": reason,
        }
    return statuses


def _cluster_status(
    *,
    cluster: str,
    candidate: ScenarioResult | None,
    base_trades: int,
    candidate_trades: int,
    cluster_delta: float,
    total_delta: float | None,
    trade_ratio: float | None,
) -> tuple[str, str]:
    if candidate is None:
        return "blocked", "missing candidate scenario"
    if candidate_trades < MIN_CLUSTER_TRADES_FOR_PROMOTION:
        return "watch", "too few candidate trades for promotion"
    if total_delta is None or total_delta <= 0:
        return "blocked", "scenario does not improve net PnL vs extended baseline"
    if cluster_delta <= 0:
        return "blocked", "cluster contribution does not improve vs extended baseline"
    if trade_ratio is not None and trade_ratio > PROMOTION_ACTIVITY_RATIO_CAP:
        return "watch", "improvement requires higher total activity"
    if cluster == "silver":
        return "watch", "silver needs manual review before any unblock"
    if base_trades < MIN_CLUSTER_TRADES_FOR_PROMOTION:
        return "watch", "baseline cluster sample is too small"
    return "promotable", "positive net delta with comparable activity"


def historical_context(paths: list[Path]) -> dict[str, object]:
    context: dict[str, object] = {}
    for path in paths:
        if not path.exists():
            context[str(path)] = {"missing": True}
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        context[str(path)] = {
            "input_path": payload.get("input_path"),
            "scenarios": {
                str(item.get("scenario", {}).get("name")): {
                    "realized_pnl_usd": item.get("realized_pnl_usd"),
                    "closed_trade_count": item.get("closed_trade_count"),
                    "fees_usd": item.get("fees_usd"),
                    "pnl_by_cluster": item.get("pnl_by_cluster"),
                }
                for item in payload.get("scenarios", [])
                if isinstance(item, dict)
            },
        }
    return context


def build_payload(
    *,
    generated_at: str,
    config_path: Path,
    input_path: Path,
    results: list[ScenarioResult],
    historical: dict[str, object],
) -> dict[str, object]:
    statuses = build_cluster_statuses(results)
    return {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "config_path": str(config_path),
        "input_path": str(input_path),
        "silver_live_block_kept": True,
        "promotion_activity_ratio_cap": PROMOTION_ACTIVITY_RATIO_CAP,
        "min_cluster_trades_for_promotion": MIN_CLUSTER_TRADES_FOR_PROMOTION,
        "cluster_statuses": statuses,
        "results": [asdict(item) for item in results],
        "historical_context": historical,
    }


def write_markdown(path: Path, payload: dict[str, object]) -> None:
    results = [ScenarioResult(**item) for item in payload["results"]]  # type: ignore[arg-type]
    cluster_statuses = dict(payload.get("cluster_statuses", {}) or {})
    lines = [
        "# P2-02 Pod C cluster multiplier replay",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Input: `{payload['input_path']}`",
        f"- Status: `{payload['status']}`",
        "- Live change: none; `XYZ:SILVER` remains blocked in `config/trident.toml`.",
        "",
        "## Scenario summary",
        "",
        (
            "| Scenario | Net PnL | Delta vs 0.55 | Trades | Trade ratio | "
            "Fees | PF | Max DD | Dates |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in results:
        lines.append(
            "| "
            f"{item.scenario} | {_fmt_float(item.realized_pnl_usd)} | "
            f"{_fmt_float(item.delta_vs_extended_baseline_usd)} | "
            f"{item.closed_trade_count} | {_fmt_float(item.trade_ratio_vs_extended_baseline)} | "
            f"{_fmt_float(item.fees_usd)} | {_fmt_float(item.profit_factor)} | "
            f"{_fmt_float(item.max_drawdown_usd)} | {item.date_start} -> {item.date_end} |"
        )
    lines.extend(
        [
            "",
            "## Cluster status",
            "",
            (
                "| Cluster | Status | Scenario | Base trades/PnL | Candidate trades/PnL | "
                "Cluster delta | Reason |"
            ),
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for cluster, details in sorted(cluster_statuses.items()):
        if not isinstance(details, dict):
            continue
        lines.append(
            "| "
            f"{cluster} | {details.get('status')} | {details.get('scenario')} | "
            f"{details.get('baseline_trades')} / {_fmt_float(details.get('baseline_pnl_usd'))} | "
            f"{details.get('candidate_trades')} / {_fmt_float(details.get('candidate_pnl_usd'))} | "
            f"{_fmt_float(details.get('cluster_delta_usd'))} | {details.get('reason')} |"
        )
    lines.extend(["", "## Cluster breakdown", ""])
    for item in results:
        lines.extend(
            [
                f"### {item.scenario}",
                "",
                "| Cluster | Trades | Net PnL | Gross PnL | Fees |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for cluster, values in sorted(item.pnl_by_cluster.items()):
            lines.append(
                "| "
                f"{cluster} | {values.get('trades')} | {_fmt_float(values.get('pnl_usd'))} | "
                f"{_fmt_float(values.get('gross_pnl_usd'))} | {_fmt_float(values.get('fees_usd'))} |"
            )
        if not item.pnl_by_cluster:
            lines.append("| - | 0 | 0.00 | 0.00 | 0.00 |")
        lines.append("")
    lines.extend(
        [
            "## Historical context",
            "",
            (
                "Historical 2026-05-26 artifacts are included in the JSON payload for comparison. "
                "They are context only; promotion decisions above use the fresh extended replay."
            ),
            "",
            "## Notes",
            "",
            "- This is Pod C research-only replay, not a live config change.",
            "- All scenario PnL numbers are net of recorded fees.",
            "- `silver_070` and `metals_070` unblock silver only inside the replay scenario.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _closed_trade_breakdown(
    backtest: dict[str, object],
    *,
    key: str,
) -> dict[str, dict[str, float | int]]:
    breakdown: dict[str, dict[str, float | int]] = {}
    for trade in backtest.get("closed_trade_log", []) or []:
        if not isinstance(trade, dict):
            continue
        name = str(trade.get(key) or "unknown")
        bucket = breakdown.setdefault(
            name,
            {"trades": 0, "pnl_usd": 0.0, "gross_pnl_usd": 0.0, "fees_usd": 0.0},
        )
        bucket["trades"] = int(bucket["trades"]) + 1
        bucket["pnl_usd"] = round(float(bucket["pnl_usd"]) + _float(trade.get("pnl_usd")), 4)
        bucket["gross_pnl_usd"] = round(
            float(bucket["gross_pnl_usd"]) + _float(trade.get("gross_pnl_usd")),
            4,
        )
        bucket["fees_usd"] = round(float(bucket["fees_usd"]) + _float(trade.get("fees_usd")), 6)
    return breakdown


def _profit_factor(backtest: dict[str, object]) -> float | None:
    wins = 0.0
    losses = 0.0
    for trade in backtest.get("closed_trade_log", []) or []:
        if not isinstance(trade, dict):
            continue
        pnl = _float(trade.get("pnl_usd"))
        if pnl >= 0:
            wins += pnl
        else:
            losses += abs(pnl)
    if losses == 0:
        return round(wins, 4) if wins > 0 else None
    return round(wins / losses, 4)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default=DEFAULT_EXTENDED_INPUT)
    parser.add_argument("--historical-global-json", default=DEFAULT_HISTORICAL_GLOBAL_JSON)
    parser.add_argument("--historical-recent-json", default=DEFAULT_HISTORICAL_RECENT_JSON)
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir) if args.output_dir else Path(
        f"server-data/replay_reports/p202_pod_c_cluster_multiplier_{generated_at}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config)
    input_path = Path(args.input)
    results = run_scenarios(config_path=config_path, input_path=input_path)
    payload = build_payload(
        generated_at=generated_at,
        config_path=config_path,
        input_path=input_path,
        results=results,
        historical=historical_context(
            [Path(args.historical_global_json), Path(args.historical_recent_json)]
        ),
    )
    json_path = output_dir / "pod_c_cluster_multiplier_compare.json"
    md_path = output_dir / "pod_c_cluster_multiplier_compare.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(md_path, payload)
    print(output_dir)


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return round(float(value or 0.0), 6)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _float(value)


def _int_dict(value: object) -> dict[str, int]:
    return {str(k): _int(v) for k, v in dict(value or {}).items()}


def _fmt_float(value: object) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


if __name__ == "__main__":
    main()
