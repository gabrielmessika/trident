from __future__ import annotations

import argparse
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.backtest.pod_a_executor import PodAExecutor, PodAStopGracePortfolioState
from app.backtest.pod_report import PodABacktestReport
from app.execution.directional_executor import DirectionalExecutor
from app.portfolio.directional_state import OpenPosition, parse_timestamp
from app.settings import AppConfig, load_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SymbolMarketSnapshot,
    symbol_market_snapshot_from_mapping,
    TradePlan,
)


BASELINE_VARIANT = "baseline"
DEFAULT_VARIANTS = [
    BASELINE_VARIANT,
    "evo1_adaptive_exit",
    "evo2_fee_aware_be",
    "evo3_trend_health_sizing",
    "evo4_symbol_health",
    "evo8_a_grade_boost",
    "evo9_wider_winner_exits",
    "evo10_context_guardrail",
]


@dataclass(slots=True)
class PodAEvolutionSpec:
    name: str
    description: str
    adaptive_exit_enabled: bool = False
    adaptive_exit_minutes: int = 120
    adaptive_min_best_bps: float = 35.0
    adaptive_max_current_bps: float = 12.0
    adaptive_weak_current_bps: float = 20.0
    fee_aware_be_enabled: bool = False
    fee_aware_be_buffer_bps: float = 12.0
    trend_health_sizing_enabled: bool = False
    trend_health_min_scale: float = 0.60
    trend_health_max_scale: float = 1.12
    symbol_health_enabled: bool = False
    symbol_health_scale: float = 0.50
    symbol_health_cooldown_minutes: int = 720
    symbol_health_lookback: int = 3
    symbol_health_loss_trigger: int = 2
    symbol_health_loss_sum_trigger_usd: float = -10.0
    a_grade_boost_enabled: bool = False
    a_grade_exit_enabled: bool = False
    a_grade_min_score: int = 6
    a_grade_boost_scale: float = 1.25
    a_grade_strong_score: int = 8
    a_grade_strong_boost_scale: float = 1.40
    a_grade_break_even_multiplier: float = 1.20
    a_grade_trailing_activation_multiplier: float = 1.15
    a_grade_trailing_distance_multiplier: float = 1.35
    context_guardrail_enabled: bool = False
    context_guardrail_cooldown_minutes: int = 720
    context_guardrail_lookback: int = 3
    context_guardrail_loss_trigger: int = 2
    context_guardrail_loss_sum_trigger_usd: float = -10.0


def spec_for_variant(name: str) -> PodAEvolutionSpec:
    normalized = name.strip().lower()
    if normalized == BASELINE_VARIANT:
        return PodAEvolutionSpec(
            name=BASELINE_VARIANT,
            description="Current Pod A baseline. Pod C is copied unchanged from the source baseline report.",
        )
    if normalized == "evo1_adaptive_exit":
        return PodAEvolutionSpec(
            name=normalized,
            description=(
                "Close stale Pod A crypto trend_pullback_long positions after about two hours "
                "when they have not produced enough favorable excursion or current tape is weak."
            ),
            adaptive_exit_enabled=True,
        )
    if normalized == "evo2_fee_aware_be":
        return PodAEvolutionSpec(
            name=normalized,
            description=(
                "Move break-even logic above zero gross so round-trip fees and close impact "
                "are covered before a BE exit fires."
            ),
            fee_aware_be_enabled=True,
        )
    if normalized == "evo3_trend_health_sizing":
        return PodAEvolutionSpec(
            name=normalized,
            description=(
                "Scale Pod A notional by entry health instead of adding hard filters: "
                "smaller weak entries, slight boost to the cleanest ones."
            ),
            trend_health_sizing_enabled=True,
        )
    if normalized == "evo4_symbol_health":
        return PodAEvolutionSpec(
            name=normalized,
            description=(
                "Temporarily reduce Pod A size on symbols whose latest closed trades show "
                "rolling weakness."
            ),
            symbol_health_enabled=True,
        )
    if normalized == "evo8_a_grade_boost":
        return PodAEvolutionSpec(
            name=normalized,
            description=(
                "Increase Pod A notional only for A-grade crypto trend_pullback_long entries; "
                "do not downsize the rest."
            ),
            a_grade_boost_enabled=True,
        )
    if normalized == "evo9_wider_winner_exits":
        return PodAEvolutionSpec(
            name=normalized,
            description=(
                "Let A-grade Pod A winners breathe by delaying break-even/trailing activation "
                "and widening the trailing distance."
            ),
            a_grade_exit_enabled=True,
        )
    if normalized == "evo10_context_guardrail":
        return PodAEvolutionSpec(
            name=normalized,
            description=(
                "Block only the precise symbol/setup/regime context after repeated recent losses, "
                "instead of throttling the whole symbol."
            ),
            context_guardrail_enabled=True,
        )
    if normalized == "evo11_a_grade_boost_wider_exits":
        return PodAEvolutionSpec(
            name=normalized,
            description=(
                "Combine A-grade size boost with wider winner exits for the same A-grade entries."
            ),
            a_grade_boost_enabled=True,
            a_grade_exit_enabled=True,
        )
    raise ValueError(f"Unknown Pod A evolution variant: {name}")


def _is_pod_a_crypto_pullback(position: OpenPosition) -> bool:
    if str(position.setup or "") != "trend_pullback_long":
        return False
    details = dict(position.setup_details or {})
    return str(details.get("market_cluster", "") or "").lower() == "crypto"


class EvolutionPodAPortfolioState(PodAStopGracePortfolioState):
    def __init__(self, stop_grace_minutes: int, spec: PodAEvolutionSpec) -> None:
        super().__init__(stop_grace_minutes)
        self._spec = spec
        self._current_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}

    def set_current_snapshots(self, snapshots: Iterable[SymbolMarketSnapshot]) -> None:
        self._current_snapshots_by_symbol = {
            snapshot.symbol.upper(): snapshot for snapshot in snapshots
        }

    def clear_current_snapshots(self) -> None:
        self._current_snapshots_by_symbol = {}

    def protective_exit_reason(self, position: OpenPosition, price: float) -> str | None:
        self._update_best_price(position, price)
        favorable_bps = self._favorable_move_bps(position, price)
        best_favorable_bps = self._best_favorable_move_bps(position)

        if position.take_profit_bps > 0 and favorable_bps >= position.take_profit_bps:
            return "take_profit_hit"
        if (
            position.trailing_activation_bps > 0
            and position.trailing_distance_bps > 0
            and best_favorable_bps >= position.trailing_activation_bps
            and favorable_bps <= best_favorable_bps - position.trailing_distance_bps
        ):
            return "trailing_stop"
        if (
            position.break_even_trigger_bps > 0
            and best_favorable_bps >= position.break_even_trigger_bps
        ):
            if self._spec.fee_aware_be_enabled:
                if favorable_bps <= self._fee_aware_break_even_buffer_bps(position):
                    return "fee_aware_break_even_stop"
            elif favorable_bps <= 0.0:
                return "break_even_stop"
        if self._adaptive_exit_hit(position, favorable_bps, best_favorable_bps):
            return "adaptive_stale_exit"
        if self._stop_hit(position, price):
            return "stop_hit"
        return None

    def _fee_aware_break_even_buffer_bps(self, position: OpenPosition) -> float:
        if position.target_notional_usd <= 0:
            return self._spec.fee_aware_be_buffer_bps
        entry_fee_bps = position.entry_fee_usd / position.target_notional_usd * 10_000.0
        estimated_round_trip_fee_bps = entry_fee_bps * 2.0
        return max(self._spec.fee_aware_be_buffer_bps, estimated_round_trip_fee_bps + 2.0)

    def _adaptive_exit_hit(
        self,
        position: OpenPosition,
        favorable_bps: float,
        best_favorable_bps: float,
    ) -> bool:
        if not self._spec.adaptive_exit_enabled or not _is_pod_a_crypto_pullback(position):
            return False
        age_minutes = self._age_minutes(position)
        if age_minutes is None:
            return False
        if age_minutes < self._spec.adaptive_exit_minutes:
            return False
        if (
            best_favorable_bps < self._spec.adaptive_min_best_bps
            and favorable_bps <= self._spec.adaptive_max_current_bps
        ):
            return True
        if (
            favorable_bps <= self._spec.adaptive_weak_current_bps
            and self._current_snapshot_is_weak(position)
        ):
            return True
        if age_minutes >= self._spec.adaptive_exit_minutes + 60 and favorable_bps < 15.0:
            return True
        return False

    def _age_minutes(self, position: OpenPosition) -> float | None:
        if position.opened_at is None or self._current_timestamp is None:
            return None
        current = parse_timestamp(self._current_timestamp)
        if current is None:
            return None
        return (current - position.opened_at).total_seconds() / 60.0

    def _current_snapshot_is_weak(self, position: OpenPosition) -> bool:
        snapshot = self._current_snapshots_by_symbol.get(position.symbol.upper())
        if snapshot is None:
            return False
        return (
            snapshot.price < snapshot.ema_fast
            or snapshot.vwap_distance_bps < -5.0
            or snapshot.structure_score < 0.25
            or snapshot.trade_flow_bias < -0.20
        )


class EvolutionPodAExecutor(PodAExecutor):
    def __init__(self, config: AppConfig, spec: PodAEvolutionSpec) -> None:
        super().__init__(config)
        self.portfolio = EvolutionPodAPortfolioState(config.pod_a.stop_grace_minutes, spec)

    def process_record(self, *, snapshots, **kwargs):
        self.portfolio.set_current_snapshots(snapshots)
        try:
            return super().process_record(snapshots=snapshots, **kwargs)
        finally:
            self.portfolio.clear_current_snapshots()


class SymbolHealthTracker:
    def __init__(self, spec: PodAEvolutionSpec) -> None:
        self._spec = spec
        self._recent_pnls_by_symbol: dict[str, deque[float]] = {}
        self._cooldown_until_by_symbol: dict[str, datetime] = {}

    def scale_for(self, symbol: str, timestamp: str | None) -> tuple[float, str]:
        if not self._spec.symbol_health_enabled:
            return 1.0, ""
        current = parse_timestamp(timestamp)
        until = self._cooldown_until_by_symbol.get(symbol.upper())
        if current is None or until is None or current >= until:
            return 1.0, ""
        return self._spec.symbol_health_scale, f"cooldown_until={until.isoformat()}"

    def record_closed_trades(self, trades: Iterable[object]) -> None:
        if not self._spec.symbol_health_enabled:
            return
        for trade in trades:
            symbol = str(getattr(trade, "symbol", "")).upper()
            closed_at = getattr(trade, "closed_at", None)
            if not symbol or closed_at is None:
                continue
            bucket = self._recent_pnls_by_symbol.setdefault(
                symbol,
                deque(maxlen=max(self._spec.symbol_health_lookback, 1)),
            )
            bucket.append(float(getattr(trade, "pnl_usd", 0.0) or 0.0))
            recent = list(bucket)
            loss_count = sum(1 for value in recent if value < 0.0)
            if (
                len(recent) >= self._spec.symbol_health_lookback
                and (
                    loss_count >= self._spec.symbol_health_loss_trigger
                    or sum(recent) <= self._spec.symbol_health_loss_sum_trigger_usd
                )
            ):
                self._cooldown_until_by_symbol[symbol] = closed_at + timedelta(
                    minutes=self._spec.symbol_health_cooldown_minutes,
                )


class ContextGuardrailTracker:
    def __init__(self, spec: PodAEvolutionSpec) -> None:
        self._spec = spec
        self._recent_pnls_by_context: dict[tuple[str, str, str], deque[float]] = {}
        self._cooldown_until_by_context: dict[tuple[str, str, str], datetime] = {}

    def active_reason(self, plan: TradePlan, timestamp: str | None) -> str | None:
        if not self._spec.context_guardrail_enabled:
            return None
        current = parse_timestamp(timestamp)
        key = self._key_from_plan(plan)
        if current is None or key is None:
            return None
        until = self._cooldown_until_by_context.get(key)
        if until is None or current >= until:
            return None
        symbol, setup, regime = key
        return (
            f"context_guardrail:{symbol}/{setup}/{regime}:"
            f"cooldown_until={until.isoformat()}"
        )

    def record_closed_trades(self, trades: Iterable[object]) -> None:
        if not self._spec.context_guardrail_enabled:
            return
        for trade in trades:
            key = self._key_from_trade(trade)
            closed_at = getattr(trade, "closed_at", None)
            if key is None or closed_at is None:
                continue
            bucket = self._recent_pnls_by_context.setdefault(
                key,
                deque(maxlen=max(self._spec.context_guardrail_lookback, 1)),
            )
            bucket.append(float(getattr(trade, "pnl_usd", 0.0) or 0.0))
            recent = list(bucket)
            loss_count = sum(1 for value in recent if value < 0.0)
            if (
                len(recent) >= self._spec.context_guardrail_lookback
                and (
                    loss_count >= self._spec.context_guardrail_loss_trigger
                    or sum(recent) <= self._spec.context_guardrail_loss_sum_trigger_usd
                )
            ):
                self._cooldown_until_by_context[key] = closed_at + timedelta(
                    minutes=self._spec.context_guardrail_cooldown_minutes,
                )

    def _key_from_plan(self, plan: TradePlan) -> tuple[str, str, str] | None:
        symbol = str(plan.symbol or "").strip().upper()
        setup = str(plan.setup or "").strip()
        regime = str((plan.setup_details or {}).get("regime", "") or "").strip()
        if not symbol or not setup or not regime:
            return None
        return symbol, setup, regime

    def _key_from_trade(self, trade: object) -> tuple[str, str, str] | None:
        symbol = str(getattr(trade, "symbol", "") or "").strip().upper()
        setup = str(getattr(trade, "setup", "") or "").strip()
        setup_details = dict(getattr(trade, "setup_details", {}) or {})
        regime = str(setup_details.get("regime", "") or "").strip()
        if not symbol or not setup or not regime:
            return None
        return symbol, setup, regime


class PodAEvolutionPolicy:
    def __init__(self, spec: PodAEvolutionSpec) -> None:
        self.spec = spec
        self.symbol_health = SymbolHealthTracker(spec)
        self.context_guardrail = ContextGuardrailTracker(spec)

    def adjust_plans(self, plans: list[TradePlan], *, timestamp: str | None) -> None:
        kept_plans: list[TradePlan] = []
        for plan in plans:
            details = dict(plan.setup_details or {})
            if details.get("market_cluster") != "crypto" or plan.setup != "trend_pullback_long":
                kept_plans.append(plan)
                continue
            context_guardrail_reason = self.context_guardrail.active_reason(plan, timestamp)
            if context_guardrail_reason is not None:
                continue
            scale = 1.0
            reasons: list[str] = []
            if self.spec.trend_health_sizing_enabled:
                trend_scale, trend_reason = self._trend_health_scale(plan)
                scale *= trend_scale
                reasons.append(trend_reason)
            if self.spec.a_grade_boost_enabled:
                boost_scale, boost_reason = self._a_grade_boost_scale(plan)
                scale *= boost_scale
                if boost_scale != 1.0:
                    reasons.append(boost_reason)
            health_scale, health_reason = self.symbol_health.scale_for(plan.symbol, timestamp)
            if health_scale != 1.0:
                scale *= health_scale
                reasons.append(f"symbol_health:{health_reason}")
            exit_reason = self._adjust_a_grade_exit(plan)
            if exit_reason:
                reasons.append(exit_reason)
            if scale != 1.0:
                self._scale_plan(plan, scale)
            if scale != 1.0 or exit_reason:
                plan.setup_details = {
                    **dict(plan.setup_details or {}),
                    "pod_a_evolution": self.spec.name,
                    "pod_a_evolution_size_scale": round(scale, 4),
                    "pod_a_evolution_reason": "; ".join(part for part in reasons if part),
                }
            kept_plans.append(plan)
        plans[:] = kept_plans

    def record_closed_trades(self, trades: Iterable[object]) -> None:
        self.symbol_health.record_closed_trades(trades)
        self.context_guardrail.record_closed_trades(trades)

    def _a_grade_boost_scale(self, plan: TradePlan) -> tuple[float, str]:
        score, reason = self._a_grade_score(plan)
        if score >= self.spec.a_grade_strong_score:
            return self.spec.a_grade_strong_boost_scale, f"a_grade_strong:{reason}"
        if score >= self.spec.a_grade_min_score:
            return self.spec.a_grade_boost_scale, f"a_grade:{reason}"
        return 1.0, ""

    def _adjust_a_grade_exit(self, plan: TradePlan) -> str:
        if not self.spec.a_grade_exit_enabled:
            return ""
        score, reason = self._a_grade_score(plan)
        if score < self.spec.a_grade_min_score:
            return ""
        plan.break_even_trigger_bps = round(
            plan.break_even_trigger_bps * self.spec.a_grade_break_even_multiplier,
            4,
        )
        plan.trailing_activation_bps = round(
            plan.trailing_activation_bps * self.spec.a_grade_trailing_activation_multiplier,
            4,
        )
        plan.trailing_distance_bps = round(
            plan.trailing_distance_bps * self.spec.a_grade_trailing_distance_multiplier,
            4,
        )
        return f"a_grade_wider_exit:{reason}"

    def _a_grade_score(self, plan: TradePlan) -> tuple[int, str]:
        details = dict(plan.setup_details or {})
        score = 0
        hits: list[str] = []
        regime = str(details.get("regime", "") or "")
        structure_score = abs(_float_detail(details, "structure_score"))
        trend_1h = _float_detail(details, "trend_1h_bps")
        trend_4h = _float_detail(details, "trend_4h_bps")
        stoch = _float_detail(details, "stoch_rsi_k")
        cci20 = _float_detail(details, "cci20")
        vwap = _float_detail(details, "vwap_reclaim_score")
        btc_overextension = _float_detail(details, "btc_overextension_score")
        pattern_watch_count = _int_detail(details, "pattern_watch_count")

        if float(plan.confidence or 0.0) >= 0.62:
            score += 1
            hits.append("confidence")
        if regime in {"TrendExpansion", "PanicSqueeze"}:
            score += 1
            hits.append("regime")
        if bool(details.get("candles_ready", False)):
            score += 1
            hits.append("candles")
        if structure_score >= 0.45:
            score += 1
            hits.append("structure")
        if 8.0 <= trend_1h <= 180.0:
            score += 1
            hits.append("trend_1h")
        if -25.0 <= trend_4h <= 110.0:
            score += 1
            hits.append("trend_4h")
        if vwap >= 0.45:
            score += 1
            hits.append("vwap")
        if 0.32 <= stoch <= 0.78:
            score += 1
            hits.append("stoch")
        if cci20 <= 110.0:
            score += 1
            hits.append("cci")
        if btc_overextension <= 0.60:
            score += 1
            hits.append("btc_ok")
        if pattern_watch_count <= 1:
            score += 1
            hits.append("few_watchers")
        return score, f"score={score} hits={','.join(hits)}"

    def _trend_health_scale(self, plan: TradePlan) -> tuple[float, str]:
        details = dict(plan.setup_details or {})
        score = 0
        confidence = float(plan.confidence or 0.0)
        trend_1h = _float_detail(details, "trend_1h_bps")
        trend_4h = _float_detail(details, "trend_4h_bps")
        stoch = _float_detail(details, "stoch_rsi_k")
        cci20 = _float_detail(details, "cci20")
        vwap = _float_detail(details, "vwap_reclaim_score")

        if confidence >= 0.65:
            score += 1
        if trend_1h >= 20.0:
            score += 1
        if -10.0 <= trend_4h <= 60.0:
            score += 1
        if 0.35 <= stoch <= 0.85:
            score += 1
        if cci20 <= 120.0:
            score += 1
        if vwap >= 0.45:
            score += 1

        if score <= 2:
            scale = self.spec.trend_health_min_scale
        elif score == 3:
            scale = 0.80
        elif score == 4:
            scale = 1.0
        elif score == 5:
            scale = 1.05
        else:
            scale = self.spec.trend_health_max_scale
        return scale, f"trend_health_score={score}"

    def _scale_plan(self, plan: TradePlan, scale: float) -> None:
        bounded = max(0.0, min(float(scale), 2.0))
        plan.target_notional_usd = round(plan.target_notional_usd * bounded, 6)
        plan.margin_usd = round(plan.margin_usd * bounded, 6)
        plan.risk_budget_usd = round(plan.risk_budget_usd * bounded, 6)
        plan.expected_loss_usd = round(plan.expected_loss_usd * bounded, 6)


def _float_detail(details: dict[str, object], key: str) -> float:
    try:
        return float(details.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int_detail(details: dict[str, object], key: str) -> int:
    try:
        return int(float(details.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


class PodAEvolutionBacktestRunner(FullBotBacktestRunner):
    def __init__(
        self,
        config: AppConfig,
        *,
        spec: PodAEvolutionSpec,
        pod_c_baseline: dict[str, object],
        force_enable_routing_pods: bool = True,
    ) -> None:
        super().__init__(config, force_enable_all_pods=force_enable_routing_pods)
        self.spec = spec
        self.pod_a_evolution_policy = PodAEvolutionPolicy(spec)
        self.pod_a_executor = EvolutionPodAExecutor(self.config, spec)
        self.pod_c_baseline = dict(pod_c_baseline)
        self.pod_b_executor = DirectionalExecutor(self.config)

    def run_jsonl(
        self,
        input_path: str | Path,
        *,
        dedupe_by_timestamp: bool = True,
        report_output: str | Path | None = None,
        summary_output: str | Path | None = None,
        comparison_output: str | Path | None = None,
    ) -> FullBotBacktestResult:
        supervisor = TridentSupervisor(
            config=self.config,
            profile=f"pod-a-evolution-{self.spec.name}",
            mode="dry-run",
        )
        pod_a_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        pod_c_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        pod_b_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        seen_timestamps: set[str] = set()
        latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        dates_covered: set[str] = set()
        records_processed = 0
        duplicate_timestamps_skipped = 0

        for record in self.loader.iter_merged_jsonl(input_path):
            timestamp = record.timestamp
            if dedupe_by_timestamp and timestamp:
                if timestamp in seen_timestamps:
                    duplicate_timestamps_skipped += 1
                    continue
                seen_timestamps.add(timestamp)
            if first_timestamp is None:
                first_timestamp = timestamp
            last_timestamp = timestamp
            if timestamp:
                dates_covered.add(timestamp[:10])

            snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
            previous_snapshots_by_symbol = dict(latest_snapshots_by_symbol)
            latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
            if record.capture_reason == "maintenance_refresh":
                self._process_maintenance_record(
                    supervisor=supervisor,
                    pod_a_report=pod_a_report,
                    pod_b_report=pod_b_report,
                    pod_c_report=pod_c_report,
                    snapshots=snapshots,
                    timestamp=timestamp,
                    source_file=record.source_file,
                    stream_source=record.stream_source,
                )
                continue

            previous_regime = supervisor.state.regime.value
            cluster_regime_snapshots = {
                cluster: RegimeSnapshot(**snap)
                for cluster, snap in (record.cluster_regime_snapshots or {}).items()
                if isinstance(snap, dict)
            }
            supervisor.apply_regime_snapshot(
                RegimeSnapshot(**record.regime_snapshot),
                cluster_regime_snapshots=cluster_regime_snapshots,
            )
            current_regime = supervisor.state.regime.value
            self._process_pod_a(
                supervisor=supervisor,
                report=pod_a_report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
            self._process_pod_c(
                supervisor=supervisor,
                report=pod_c_report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
            self._process_pod_b(
                supervisor=supervisor,
                report=pod_b_report,
                snapshots=snapshots,
                previous_snapshots_by_symbol=previous_snapshots_by_symbol,
                timestamp=timestamp,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
            records_processed += 1

        supervisor.flush_compact_logs()
        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_a_report,
            executor=self.pod_a_executor,
            latest_snapshots=list(latest_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
            closed_trade_recorder=self._record_pod_a_closed_trade,
        )
        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_c_report,
            executor=self.pod_c_executor,
            latest_snapshots=list(latest_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
        )
        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_b_report,
            executor=self.pod_b_executor,
            latest_snapshots=list(latest_snapshots_by_symbol.values()),
            last_timestamp=last_timestamp,
            closed_trade_recorder=self._record_pod_b_closed_trade,
        )

        pod_a = pod_a_report.to_dict()
        pod_b = pod_b_report.to_dict()
        pod_c = pod_c_report.to_dict()
        pod_b_realized = float(pod_b.get("realized_pnl_usd", 0.0) or 0.0)
        pod_b_fees = float(pod_b.get("fees_usd", 0.0) or 0.0)
        pod_b_activity = int(pod_b.get("closed_trade_count", 0) or 0)
        result = FullBotBacktestResult(
            input_path=str(input_path),
            dedupe_by_timestamp=dedupe_by_timestamp,
            records_processed=records_processed,
            duplicate_timestamps_skipped=duplicate_timestamps_skipped,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            dates_covered=sorted(dates_covered),
            pod_a=pod_a,
            pod_b=pod_b,
            pod_c=pod_c,
            routing={
                "reassignment_event_count": 0,
                "max_ownership_conflict_count": 0,
                "notes": ["routing replay skipped; Pod C copied unchanged for Pod A evolution test"],
            },
            total_realized_pnl_usd=round(
                float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
                + pod_b_realized
                + float(pod_c.get("realized_pnl_usd", 0.0) or 0.0),
                4,
            ),
            directional_fees_usd=round(
                float(pod_a.get("fees_usd", 0.0) or 0.0)
                + pod_b_fees
                + float(pod_c.get("fees_usd", 0.0) or 0.0),
                6,
            ),
            total_activity_count=(
                int(pod_a.get("closed_trade_count", 0) or 0)
                + pod_b_activity
                + int(pod_c.get("closed_trade_count", 0) or 0)
            ),
            notes=[
                f"pod_a_evolution_variant={self.spec.name}",
                self.spec.description,
                "Pod B is not modified by this experiment.",
                "Pod routing can still force-enable configured pods to match full-bot baseline ownership.",
                "Pod C is processed with the unchanged baseline logic.",
            ],
            report_path=str(report_output) if report_output is not None else None,
            summary_path=str(summary_output) if summary_output is not None else None,
        )
        self._write_outputs(
            result,
            report_output=report_output,
            summary_output=summary_output,
            comparison_output=comparison_output,
        )
        return result

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
        self.pod_a_evolution_policy.adjust_plans(trade_plans, timestamp=timestamp)
        risk_decisions = self.pod_a_risk_gate.evaluate_many(trade_plans)
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
        self.pod_a_evolution_policy.record_closed_trades(execution.closed_trades)

    def _process_pod_a_maintenance(
        self,
        *,
        supervisor: TridentSupervisor,
        report: PodABacktestReport,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
        source_file: str,
        stream_source: str | None,
    ) -> None:
        source = str(stream_source or "").strip().lower()
        if source and not source.startswith("pod_a"):
            return
        managed_symbols = supervisor.managed_symbols_for(
            PodName.POD_A,
            active_symbols=self.pod_a_executor.portfolio.open_positions.keys(),
        )
        execution = self.pod_a_executor.process_record(
            snapshots=snapshots,
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp=timestamp,
            entry_allowed_symbols=supervisor.opening_symbols_for(PodName.POD_A),
            managed_symbols=managed_symbols,
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=[],
            risk_decisions=[],
            execution=execution,
            executor=self.pod_a_executor,
            closed_trade_recorder=self._record_pod_a_closed_trade,
        )
        self.pod_a_evolution_policy.record_closed_trades(execution.closed_trades)

    def _write_outputs(
        self,
        result: FullBotBacktestResult,
        *,
        report_output: str | Path | None,
        summary_output: str | Path | None,
        comparison_output: str | Path | None,
    ) -> None:
        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        if summary_output is not None:
            summary_path = Path(summary_output)
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(self._render_summary(result), encoding="utf-8")
        if comparison_output is not None:
            comparison_path = Path(comparison_output)
            comparison_path.parent.mkdir(parents=True, exist_ok=True)
            with comparison_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self._comparison_entry(result)) + "\n")


def _load_pod_c_baseline(path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    pod_c = payload.get("pod_c")
    if not isinstance(pod_c, dict):
        raise ValueError(f"pod_c payload missing from {path}")
    return pod_c


def _result_summary(result: FullBotBacktestResult, baseline: FullBotBacktestResult | None) -> dict[str, object]:
    total = float(result.total_realized_pnl_usd)
    pod_a = float(result.pod_a.get("realized_pnl_usd", 0.0) or 0.0)
    base_total = float(baseline.total_realized_pnl_usd) if baseline is not None else total
    base_pod_a = float(baseline.pod_a.get("realized_pnl_usd", 0.0) or 0.0) if baseline is not None else pod_a
    return {
        "variant": _variant_from_notes(result.notes),
        "total_realized_pnl_usd": round(total, 4),
        "delta_total_vs_baseline_usd": round(total - base_total, 4),
        "pod_a_realized_pnl_usd": round(pod_a, 4),
        "delta_pod_a_vs_baseline_usd": round(pod_a - base_pod_a, 4),
        "pod_c_realized_pnl_usd": result.pod_c.get("realized_pnl_usd", 0.0),
        "directional_fees_usd": result.directional_fees_usd,
        "pod_a_closed_trade_count": result.pod_a.get("closed_trade_count", 0),
        "pod_a_close_reasons": result.pod_a.get("close_reasons", {}),
        "report_path": result.report_path,
        "summary_path": result.summary_path,
    }


def _variant_from_notes(notes: list[str]) -> str:
    for note in notes:
        if note.startswith("pod_a_evolution_variant="):
            return note.split("=", 1)[1]
    return ""


def _write_aggregate(
    rows: list[dict[str, object]],
    *,
    json_output: str | Path,
    md_output: str | Path,
) -> None:
    json_path = Path(json_output)
    md_path = Path(md_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rows": rows,
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Pod A evolution backtest comparison",
        "",
        "| Variant | Total PnL | Delta total | Pod A PnL | Delta Pod A | Pod A trades | Fees |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {variant} | {total:.2f} | {delta_total:.2f} | {pod_a:.2f} | {delta_pod_a:.2f} | {trades} | {fees:.2f} |".format(
                variant=row["variant"],
                total=float(row["total_realized_pnl_usd"]),
                delta_total=float(row["delta_total_vs_baseline_usd"]),
                pod_a=float(row["pod_a_realized_pnl_usd"]),
                delta_pod_a=float(row["delta_pod_a_vs_baseline_usd"]),
                trades=int(row["pod_a_closed_trade_count"]),
                fees=float(row["directional_fees_usd"]),
            )
        )
    lines.extend(
        [
            "",
            "Notes:",
            "- Pod B is not modified by this experiment.",
            "- Pod C is processed with the unchanged baseline logic.",
            "- Runtime routing is active; only the separate routing report export is skipped.",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest isolated Pod A evolution variants.")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument(
        "--pod-c-source-report",
        default="server-data/replay_reports/official_baseline_current_cli_20260513.json",
    )
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    parser.add_argument("--report-dir", default="server-data/replay_reports/pod_a_evolutions_20260513")
    parser.add_argument("--aggregate-json")
    parser.add_argument("--aggregate-md")
    parser.add_argument("--no-dedupe-timestamps", action="store_true")
    parser.add_argument(
        "--no-force-enable-routing-pods",
        action="store_true",
        help=(
            "Keep config pod enabled flags as-is. By default this runner force-enables "
            "routing pods to match full_bot_replay baseline ownership."
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    variants = [item.strip().lower() for item in args.variants.split(",") if item.strip()]
    report_dir = Path(args.report_dir)
    aggregate_json = args.aggregate_json or str(report_dir / "comparison.json")
    aggregate_md = args.aggregate_md or str(report_dir / "comparison.md")
    pod_c_baseline = _load_pod_c_baseline(args.pod_c_source_report)
    config = load_config(args.config)

    results: list[FullBotBacktestResult] = []
    baseline_result: FullBotBacktestResult | None = None
    comparison_output = report_dir / "comparison.jsonl"
    if comparison_output.exists():
        comparison_output.unlink()

    for variant in variants:
        spec = spec_for_variant(variant)
        print(f"[variant] {spec.name}", flush=True)
        runner = PodAEvolutionBacktestRunner(
            config,
            spec=spec,
            pod_c_baseline=pod_c_baseline,
            force_enable_routing_pods=not args.no_force_enable_routing_pods,
        )
        result = runner.run_jsonl(
            args.input,
            dedupe_by_timestamp=not args.no_dedupe_timestamps,
            report_output=report_dir / f"{spec.name}.json",
            summary_output=report_dir / f"{spec.name}.md",
            comparison_output=comparison_output,
        )
        results.append(result)
        if spec.name == BASELINE_VARIANT:
            baseline_result = result
        print(
            f"[done] {spec.name} total={result.total_realized_pnl_usd} "
            f"pod_a={result.pod_a.get('realized_pnl_usd', 0.0)}",
            flush=True,
        )

    if baseline_result is None and results:
        baseline_result = results[0]
    rows = [_result_summary(result, baseline_result) for result in results]
    _write_aggregate(rows, json_output=aggregate_json, md_output=aggregate_md)
    print(f"aggregate_json={aggregate_json}")
    print(f"aggregate_md={aggregate_md}")


if __name__ == "__main__":
    main()
