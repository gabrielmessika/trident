from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_report import PodABacktestReport
from app.backtest.routing_replay import RoutingReplayRunner
from app.execution.directional_executor import DirectionalExecutor
from app.portfolio.directional_state import ClosedTrade, parse_timestamp
from app.settings import AppConfig, load_config
from app.trident.market_clusters import cluster_for_symbol
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, RiskDecision, SymbolMarketSnapshot


INPUT_PATH = "server-data/replay_inputs/full_bot_latest_fetch.jsonl"
CONFIG_PATH = "config/trident.toml"
OUTPUT_DIR = Path(
    "/workspaces/trident/server-data/replay_reports/pod_a_opposite_signal_candidates_20260422"
)


@dataclass(slots=True)
class CandidateSummary:
    scenario: str
    total_realized_pnl_usd: float
    pod_a_realized_pnl_usd: float
    pod_a_closed_trade_count: int
    pod_a_loss_count: int
    pod_a_average_hold_hours: float
    pod_a_close_reasons: dict[str, int]
    opposite_signal_count: int
    opposite_signal_pnl_usd: float
    stop_hit_count: int
    stop_hit_pnl_usd: float
    trailing_stop_count: int
    trailing_stop_pnl_usd: float
    break_even_stop_count: int
    break_even_stop_pnl_usd: float
    daily_pnl_by_date: dict[str, float]
    delta_total_vs_baseline: float = 0.0
    delta_pod_a_vs_baseline: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _reason_stats(pod_a: dict[str, object], reason: str) -> tuple[int, float]:
    trades = pod_a.get("closed_trade_log", [])
    if not isinstance(trades, list):
        return 0, 0.0
    matches = [trade for trade in trades if str(trade.get("close_reason")) == reason]
    return len(matches), round(sum(float(trade.get("pnl_usd") or 0.0) for trade in matches), 2)


class PodACandidateExecutor(DirectionalExecutor):
    """Pod A executor variants that only modify opposite-signal behavior."""

    def __init__(self, config: AppConfig) -> None:
        from app.backtest.pod_a_executor import PodAExecutor

        base = PodAExecutor(config)
        # Reuse the Pod A-specific portfolio with stop_grace already wired in.
        self.__dict__.update(base.__dict__)
        self._current_regime = ""

    def process_record(
        self,
        *,
        snapshots: list[SymbolMarketSnapshot],
        risk_decisions: list[RiskDecision],
        signal_sides_by_symbol: dict[str, str],
        timestamp: str | None,
        entry_allowed_symbols=None,
        managed_symbols=None,
        allowed_symbols=None,
    ):
        self.portfolio._current_timestamp = timestamp
        try:
            filtered_signal_sides = self._filter_opposite_signal_sides(
                signal_sides_by_symbol=signal_sides_by_symbol,
                risk_decisions=risk_decisions,
                timestamp=timestamp,
            )
            return super().process_record(
                snapshots=snapshots,
                risk_decisions=risk_decisions,
                signal_sides_by_symbol=filtered_signal_sides,
                timestamp=timestamp,
                entry_allowed_symbols=entry_allowed_symbols,
                managed_symbols=managed_symbols,
                allowed_symbols=allowed_symbols,
            )
        finally:
            self.portfolio._current_timestamp = None
            self._post_process_cleanup(signal_sides_by_symbol)

    def _filter_opposite_signal_sides(
        self,
        *,
        signal_sides_by_symbol: dict[str, str],
        risk_decisions: list[RiskDecision],
        timestamp: str | None,
    ) -> dict[str, str]:
        return dict(signal_sides_by_symbol)

    def _post_process_cleanup(self, signal_sides_by_symbol: dict[str, str]) -> None:
        return None


class OppositeSignalDebounce15mExecutor(PodACandidateExecutor):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._debounce_minutes = 15
        self._opposite_signal_since_by_symbol: dict[str, str] = {}

    def _filter_opposite_signal_sides(
        self,
        *,
        signal_sides_by_symbol: dict[str, str],
        risk_decisions: list[RiskDecision],
        timestamp: str | None,
    ) -> dict[str, str]:
        filtered = dict(signal_sides_by_symbol)
        current = parse_timestamp(timestamp)
        for symbol, position in self.portfolio.open_positions.items():
            preview_side = signal_sides_by_symbol.get(symbol)
            if preview_side is None or preview_side == position.side:
                self._opposite_signal_since_by_symbol.pop(symbol, None)
                continue
            since_raw = self._opposite_signal_since_by_symbol.get(symbol)
            since = parse_timestamp(since_raw)
            if since is None:
                self._opposite_signal_since_by_symbol[symbol] = timestamp or ""
                filtered.pop(symbol, None)
                continue
            if current is None:
                filtered.pop(symbol, None)
                continue
            age_seconds = (current - since).total_seconds()
            if age_seconds < self._debounce_minutes * 60:
                filtered.pop(symbol, None)
        return filtered

    def _post_process_cleanup(self, signal_sides_by_symbol: dict[str, str]) -> None:
        open_symbols = set(self.portfolio.open_positions.keys())
        stale = [
            symbol
            for symbol in self._opposite_signal_since_by_symbol
            if symbol not in open_symbols
            or signal_sides_by_symbol.get(symbol) in (None, self.portfolio.open_positions.get(symbol).side if symbol in open_symbols else None)
        ]
        for symbol in stale:
            self._opposite_signal_since_by_symbol.pop(symbol, None)


class OppositeExecutablePersistent2SnapExecutor(PodACandidateExecutor):
    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._required_consecutive_snapshots = 2
        self._opposite_executable_streak_by_symbol: dict[str, int] = {}

    def _filter_opposite_signal_sides(
        self,
        *,
        signal_sides_by_symbol: dict[str, str],
        risk_decisions: list[RiskDecision],
        timestamp: str | None,
    ) -> dict[str, str]:
        filtered = dict(signal_sides_by_symbol)
        accepted_opposite_side_by_symbol: dict[str, str] = {}
        for decision in risk_decisions:
            if not decision.accepted:
                continue
            accepted_opposite_side_by_symbol[decision.trade_plan.symbol] = decision.trade_plan.side

        for symbol, position in self.portfolio.open_positions.items():
            preview_side = signal_sides_by_symbol.get(symbol)
            accepted_side = accepted_opposite_side_by_symbol.get(symbol)
            if (
                preview_side is None
                or preview_side == position.side
                or accepted_side is None
                or accepted_side != preview_side
                or accepted_side == position.side
            ):
                self._opposite_executable_streak_by_symbol.pop(symbol, None)
                continue
            streak = self._opposite_executable_streak_by_symbol.get(symbol, 0) + 1
            self._opposite_executable_streak_by_symbol[symbol] = streak
            if streak < self._required_consecutive_snapshots:
                filtered.pop(symbol, None)
        return filtered

    def _post_process_cleanup(self, signal_sides_by_symbol: dict[str, str]) -> None:
        open_symbols = set(self.portfolio.open_positions.keys())
        for symbol in list(self._opposite_executable_streak_by_symbol):
            if symbol not in open_symbols:
                self._opposite_executable_streak_by_symbol.pop(symbol, None)


class OppositeBlockedDuringStopGraceTrendExecutor(PodACandidateExecutor):
    def _filter_opposite_signal_sides(
        self,
        *,
        signal_sides_by_symbol: dict[str, str],
        risk_decisions: list[RiskDecision],
        timestamp: str | None,
    ) -> dict[str, str]:
        filtered = dict(signal_sides_by_symbol)
        for symbol, position in self.portfolio.open_positions.items():
            preview_side = signal_sides_by_symbol.get(symbol)
            if preview_side is None or preview_side == position.side:
                continue
            if (
                getattr(self, "_current_regime", "") == "TrendExpansion"
                and self.portfolio._stop_grace_active(position)
            ):
                filtered.pop(symbol, None)
        return filtered


class CandidateFullBotBacktestRunner(FullBotBacktestRunner):
    def _process_pod_a(
        self,
        *,
        supervisor: TridentSupervisor,
        report: PodABacktestReport,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
        source_file: str,
        previous_regime: str,
        current_regime: str,
    ) -> None:
        executor = self.pod_a_executor
        setattr(executor, "_current_regime", current_regime)
        try:
            super()._process_pod_a(
                supervisor=supervisor,
                report=report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
        finally:
            setattr(executor, "_current_regime", "")


def summarize_result(scenario: str, result: object) -> CandidateSummary:
    pod_a = result.pod_a
    opposite_count, opposite_pnl = _reason_stats(pod_a, "opposite_signal")
    stop_hit_count, stop_hit_pnl = _reason_stats(pod_a, "stop_hit")
    trailing_count, trailing_pnl = _reason_stats(pod_a, "trailing_stop")
    break_even_count, break_even_pnl = _reason_stats(pod_a, "break_even_stop")
    return CandidateSummary(
        scenario=scenario,
        total_realized_pnl_usd=round(float(result.total_realized_pnl_usd), 2),
        pod_a_realized_pnl_usd=round(float(pod_a.get("realized_pnl_usd", 0.0)), 2),
        pod_a_closed_trade_count=int(pod_a.get("closed_trade_count", 0) or 0),
        pod_a_loss_count=int(pod_a.get("loss_count", 0) or 0),
        pod_a_average_hold_hours=round(float(pod_a.get("average_hold_hours", 0.0) or 0.0), 4),
        pod_a_close_reasons={
            str(key): int(value)
            for key, value in dict(pod_a.get("close_reasons", {})).items()
        },
        opposite_signal_count=opposite_count,
        opposite_signal_pnl_usd=opposite_pnl,
        stop_hit_count=stop_hit_count,
        stop_hit_pnl_usd=stop_hit_pnl,
        trailing_stop_count=trailing_count,
        trailing_stop_pnl_usd=trailing_pnl,
        break_even_stop_count=break_even_count,
        break_even_stop_pnl_usd=break_even_pnl,
        daily_pnl_by_date={
            str(key): round(float(value), 2)
            for key, value in dict(pod_a.get("pnl_by_date", {})).items()
        },
    )


def run_scenario(
    scenario: str,
    executor_factory,
) -> CandidateSummary:
    config = load_config(CONFIG_PATH)
    runner = CandidateFullBotBacktestRunner(config)
    runner.pod_a_executor = executor_factory(config)
    result = runner.run_jsonl(INPUT_PATH)
    summary = summarize_result(scenario, result)
    return summary


def write_outputs(summaries: list[CandidateSummary]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = next(item for item in summaries if item.scenario == "baseline_current")
    for summary in summaries:
        summary.delta_total_vs_baseline = round(
            summary.total_realized_pnl_usd - baseline.total_realized_pnl_usd,
            2,
        )
        summary.delta_pod_a_vs_baseline = round(
            summary.pod_a_realized_pnl_usd - baseline.pod_a_realized_pnl_usd,
            2,
        )

    summary_json = OUTPUT_DIR / "scenario_summary.json"
    summary_json.write_text(
        json.dumps([item.to_dict() for item in summaries], indent=2) + "\n",
        encoding="utf-8",
    )

    table_lines = [
        "# Pod A Opposite-Signal Candidate Sweep",
        "",
        f"- input: `{INPUT_PATH}`",
        f"- config: `{CONFIG_PATH}`",
        "- note: les `3 candidats` testes ici sont interpretes comme:",
        "  - `debounce_15m`",
        "  - `opposite_executable_persistent_2snap`",
        "  - `block_opposite_during_stop_grace_trend`",
        "",
        "| Scenario | Delta total | Delta Pod A | Pod A PnL | Closed | Losses | Opposite count | Opposite pnl | Stop hits | Stop hit pnl | Trailing count | Trailing pnl | Break-even count | Break-even pnl |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        table_lines.append(
            "| `{scenario}` | {delta_total:+.2f} | {delta_pod_a:+.2f} | {pod_a:.2f} | {closed} | {losses} | {opp_count} | {opp_pnl:.2f} | {stop_count} | {stop_pnl:.2f} | {trail_count} | {trail_pnl:.2f} | {be_count} | {be_pnl:.2f} |".format(
                scenario=item.scenario,
                delta_total=item.delta_total_vs_baseline,
                delta_pod_a=item.delta_pod_a_vs_baseline,
                pod_a=item.pod_a_realized_pnl_usd,
                closed=item.pod_a_closed_trade_count,
                losses=item.pod_a_loss_count,
                opp_count=item.opposite_signal_count,
                opp_pnl=item.opposite_signal_pnl_usd,
                stop_count=item.stop_hit_count,
                stop_pnl=item.stop_hit_pnl_usd,
                trail_count=item.trailing_stop_count,
                trail_pnl=item.trailing_stop_pnl_usd,
                be_count=item.break_even_stop_count,
                be_pnl=item.break_even_stop_pnl_usd,
            )
        )

    summary_md = OUTPUT_DIR / "scenario_summary.md"
    summary_md.write_text("\n".join(table_lines) + "\n", encoding="utf-8")


def main() -> None:
    scenarios = [
        ("baseline_current", lambda config: __import__("app.backtest.pod_a_executor", fromlist=["PodAExecutor"]).PodAExecutor(config)),
        ("debounce_15m", OppositeSignalDebounce15mExecutor),
        ("opposite_executable_persistent_2snap", OppositeExecutablePersistent2SnapExecutor),
        ("block_opposite_during_stop_grace_trend", OppositeBlockedDuringStopGraceTrendExecutor),
    ]
    summaries: list[CandidateSummary] = []
    for scenario, factory in scenarios:
        summary = run_scenario(scenario, factory)
        summaries.append(summary)
        print(json.dumps(summary.to_dict()))
    write_outputs(summaries)


if __name__ == "__main__":
    main()
