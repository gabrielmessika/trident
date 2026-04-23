from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_b_pattern_experiment import summarize_backtest
from app.backtest.pod_b_runner import PodBBacktestRunner
from app.settings import AllocationConfig, AppConfig, RegimeAllocations, load_config
from app.trident.pod_b import BreakoutContext, BreakoutService
from app.trident.pod_b.signals import BreakoutSignal


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    name: str
    trigger_kind: str
    description: str
    top_n: int
    min_interest_score: float
    max_spread_bps: float
    min_bucket_notional_usd: float
    allowed_regimes: tuple[str, ...] = ()
    allowed_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    description: str
    trigger_specs: tuple[TriggerSpec, ...] = ()
    include_base: bool = False


@dataclass(frozen=True, slots=True)
class AllocationProfile:
    name: str
    description: str
    allocations: dict[str, dict[str, float]]
    pod_a_enabled: bool = True
    pod_b_enabled: bool = True
    pod_c_enabled: bool = True


@dataclass(slots=True)
class SuiteScenarioResult:
    suite: str
    profile: str
    scenario: str
    description: str
    summary: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class MemecoinPnlExperimentResult:
    official_input: str
    special_input: str
    official_standalone: list[dict[str, object]]
    official_full_bot: list[dict[str, object]]
    special_standalone: list[dict[str, object]]
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ExperimentalMemecoinService(BreakoutService):
    """Backtest-only memecoin ranking + trigger service."""

    def __init__(
        self,
        config: AppConfig,
        *,
        trigger_specs: tuple[TriggerSpec, ...],
        include_base: bool = False,
        label: str = "memecoin",
    ) -> None:
        super().__init__(config)
        self._trigger_specs = tuple(trigger_specs)
        self._include_base = include_base
        self._label = label

    def evaluate(self, context: BreakoutContext) -> BreakoutSignal | None:
        signals = self.evaluate_many([context])
        return signals[0] if signals else None

    def evaluate_many(self, contexts: list[BreakoutContext]) -> list[BreakoutSignal]:
        candidates_by_symbol: dict[str, BreakoutSignal] = {}
        if self._include_base:
            for context in contexts:
                signal = super().evaluate(context)
                if signal is not None:
                    candidates_by_symbol[signal.symbol] = signal
        ranked_contexts = self._ranked_contexts(contexts)
        for rank, context, metrics in ranked_contexts:
            for spec in self._trigger_specs:
                signal = self._evaluate_trigger(context, metrics, spec=spec, rank=rank)
                if signal is None:
                    continue
                existing = candidates_by_symbol.get(signal.symbol)
                if existing is None or signal.confidence > existing.confidence:
                    candidates_by_symbol[signal.symbol] = signal
        return sorted(candidates_by_symbol.values(), key=lambda item: item.confidence, reverse=True)

    def _ranked_contexts(
        self,
        contexts: list[BreakoutContext],
    ) -> list[tuple[int, BreakoutContext, dict[str, object]]]:
        scored: list[tuple[BreakoutContext, dict[str, object]]] = []
        for context in contexts:
            if str(context.market_cluster).strip().lower() != "crypto":
                continue
            if context.price <= 0:
                continue
            metrics = self._metrics(context)
            scored.append((context, metrics))
        scored.sort(
            key=lambda item: (
                float(item[1]["interest_score"]),
                item[0].bucket_notional_usd,
                item[0].symbol,
            ),
            reverse=True,
        )
        return [
            (rank, context, metrics)
            for rank, (context, metrics) in enumerate(scored, start=1)
        ]

    def _metrics(self, context: BreakoutContext) -> dict[str, object]:
        event_score = min(
            1.0,
            abs(context.delta_trade_flow_bias) * 0.35
            + abs(context.delta_book_imbalance) * 0.20
            + _clamp(max(context.delta_spread_bps, 0.0) / 2.0) * 0.15
            + _clamp(context.volume_ratio / 3.0) * 0.15
            + _clamp(context.trade_count_ratio / 3.0) * 0.15,
        )
        liquidity_pull_score_raw, liquidity_pull_direction = self._liquidity_pull_signal(context)
        touch_liquidity_pull_score_raw, touch_liquidity_pull_direction = self._touch_liquidity_pull_signal(
            context
        )
        depth_refill_score_raw, depth_refill_direction = self._depth_refill_signal(context)
        touch_refill_score_raw, touch_refill_direction = self._touch_refill_signal(context)
        book_churn_score = self._book_churn_signal(context)
        liquidity_pull_score = max(
            liquidity_pull_score_raw if liquidity_pull_direction == "long" else 0.0,
            touch_liquidity_pull_score_raw if touch_liquidity_pull_direction == "long" else 0.0,
        )
        depth_refill_score = max(
            depth_refill_score_raw if depth_refill_direction == "long" else 0.0,
            touch_refill_score_raw if touch_refill_direction == "long" else 0.0,
        )
        volume_accel = _clamp((context.volume_ratio - 1.0) / 2.5)
        trade_accel = _clamp((context.trade_count_ratio - 1.0) / 2.5)
        notional_presence = _clamp(
            0.0
            if context.bucket_notional_usd <= 0
            else math.log10(max(context.bucket_notional_usd, 1.0)) / 4.0
        )
        volatility = _clamp(context.realized_vol_short_bps / 35.0)
        spread_quality = 1.0 - _clamp(context.spread_bps / 8.0)
        stability = 1.0 - _clamp(book_churn_score / 1.0)
        directional_pressure = _clamp(
            abs(context.trade_flow_bias) * 0.60
            + abs(context.book_imbalance) * 0.40
        )
        interest_score = _clamp(
            event_score * 0.24
            + volume_accel * 0.16
            + trade_accel * 0.12
            + notional_presence * 0.12
            + volatility * 0.10
            + spread_quality * 0.12
            + stability * 0.06
            + directional_pressure * 0.08
        )
        return {
            "event_score": round(event_score, 4),
            "liquidity_pull_score": round(liquidity_pull_score, 4),
            "depth_refill_score": round(depth_refill_score, 4),
            "book_churn_score": round(book_churn_score, 4),
            "interest_score": round(interest_score, 4),
            "liquidity_pull_score_raw": round(liquidity_pull_score_raw, 4),
            "touch_liquidity_pull_score_raw": round(touch_liquidity_pull_score_raw, 4),
            "depth_refill_score_raw": round(depth_refill_score_raw, 4),
            "touch_refill_score_raw": round(touch_refill_score_raw, 4),
        }

    def _evaluate_trigger(
        self,
        context: BreakoutContext,
        metrics: dict[str, object],
        *,
        spec: TriggerSpec,
        rank: int,
    ) -> BreakoutSignal | None:
        if not self._direction_enabled("long"):
            return None
        if rank > spec.top_n:
            return None
        if spec.allowed_regimes and context.regime not in set(spec.allowed_regimes):
            return None
        if spec.allowed_symbols and context.symbol.upper() not in {item.upper() for item in spec.allowed_symbols}:
            return None
        if float(metrics["interest_score"]) < spec.min_interest_score:
            return None
        if context.spread_bps > spec.max_spread_bps:
            return None
        if context.bucket_notional_usd < spec.min_bucket_notional_usd:
            return None
        if context.bucket_trade_count < 3:
            return None
        if context.price <= 0:
            return None
        event_score = float(metrics["event_score"])
        liquidity_pull_score = float(metrics["liquidity_pull_score"])
        depth_refill_score = float(metrics["depth_refill_score"])
        interest_score = float(metrics["interest_score"])
        if spec.trigger_kind == "event_momentum":
            if not (
                event_score >= 0.55
                and context.volume_ratio >= 1.40
                and context.trade_count_ratio >= 1.15
                and context.price_move_bps >= 0.25
                and max(liquidity_pull_score, depth_refill_score) >= 0.60
                and context.trade_flow_bias >= 0.05
                and context.book_imbalance >= -0.05
                and context.microprice_dislocation_bps >= 0.0
            ):
                return None
            trigger_quality = _clamp(
                event_score * 0.40
                + max(liquidity_pull_score, depth_refill_score) * 0.25
                + _clamp(context.price_move_bps / 4.0) * 0.15
                + _clamp(context.volume_ratio / 3.0) * 0.10
                + _clamp(context.trade_count_ratio / 3.0) * 0.10
            )
            stop_bps = self._expansion_stop_bps(context)
            setup = "memecoin_event_momentum_long"
            setup_bonus = 0.08
        elif spec.trigger_kind == "flow_following":
            if not (
                context.trade_flow_bias >= 0.12
                and context.delta_trade_flow_bias >= 0.05
                and context.book_imbalance >= 0.05
                and context.delta_book_imbalance >= -0.05
                and context.microprice_dislocation_bps >= 0.0
                and max(liquidity_pull_score, event_score) >= 0.50
            ):
                return None
            trigger_quality = _clamp(
                self._flow_support_quality(context, "long") * 0.35
                + self._money_flow_quality(context, "long") * 0.25
                + interest_score * 0.20
                + liquidity_pull_score * 0.10
                + event_score * 0.10
            )
            stop_bps = self._expansion_stop_bps(context)
            setup = "memecoin_flow_following_long"
            setup_bonus = 0.09
        elif spec.trigger_kind == "pullback_reclaim":
            if not (
                event_score >= 0.42
                and context.volume_ratio >= 1.20
                and context.trade_count_ratio >= 1.05
                and -0.75 <= context.price_move_bps <= 2.50
                and context.trade_flow_bias >= 0.06
                and context.book_imbalance >= -0.02
                and depth_refill_score >= 0.60
                and context.microprice_dislocation_bps >= 0.0
            ):
                return None
            trigger_quality = _clamp(
                event_score * 0.25
                + depth_refill_score * 0.25
                + self._vwap_reclaim_quality(context, "long") * 0.20
                + self._money_flow_quality(context, "long") * 0.15
                + interest_score * 0.15
            )
            stop_bps = self._pullback_stop_bps(context)
            setup = "memecoin_pullback_reclaim_long"
            setup_bonus = 0.10
        else:
            raise ValueError(f"Unsupported memecoin trigger: {spec.trigger_kind}")

        components = {
            "compression_quality": round(max(interest_score, 0.35), 4),
            "activity_quality": round(max(event_score, _clamp(max(context.volume_ratio, context.trade_count_ratio) / 3.0)), 4),
            "breakout_quality": round(trigger_quality, 4),
            "spread_quality": round(_clamp(1.0 - context.spread_bps / max(spec.max_spread_bps, 1.0)), 4),
            "alignment_quality": round(self._flow_support_quality(context, "long"), 4),
            "setup_bonus": round(setup_bonus, 4),
            "vwap_reclaim_quality": round(self._vwap_reclaim_quality(context, "long"), 4),
            "money_flow_quality": round(self._money_flow_quality(context, "long"), 4),
        }
        confidence = round(min(0.99, self._aggregate_confidence(components)), 3)
        signal = BreakoutSignal(
            symbol=context.symbol,
            side="long",
            setup=setup,
            confidence=confidence,
            entry_price=context.price,
            stop_bps_hint=stop_bps,
            market_cluster=context.market_cluster,
            cluster_leader=context.cluster_leader,
            setup_details={
                "memecoin_label": self._label,
                "memecoin_trigger": spec.trigger_kind,
                "rank": rank,
                "interest_score": round(interest_score, 4),
                "event_score": round(event_score, 4),
                "flow_support_quality": round(self._flow_support_quality(context, "long"), 4),
                "vwap_reclaim_quality": round(self._vwap_reclaim_quality(context, "long"), 4),
                "money_flow_quality": round(self._money_flow_quality(context, "long"), 4),
                "liquidity_pull_score": round(liquidity_pull_score, 4),
                "depth_refill_score": round(depth_refill_score, 4),
                "bucket_notional_usd": round(context.bucket_notional_usd, 4),
                "spread_bps": round(context.spread_bps, 4),
                "price_move_bps": round(context.price_move_bps, 4),
                "trade_count_ratio": round(context.trade_count_ratio, 4),
                "volume_ratio": round(context.volume_ratio, 4),
                "regime": context.regime,
            },
            confidence_components=components,
        )
        return self._with_microstructure_watch_details(signal, context)

    def _pullback_stop_bps(self, context: BreakoutContext) -> float:
        floor = self._config.pod_b.bis_stop_floor_bps
        ceiling = self._config.pod_b.bis_stop_ceiling_bps
        base = max(context.bucket_range_bps * 1.05, context.realized_vol_short_bps * 1.20, floor)
        return round(min(base, ceiling), 4)

    def _touch_liquidity_pull_signal(
        self,
        context: BreakoutContext,
    ) -> tuple[float, str]:
        bullish_pull = (
            max(-context.best_ask_size_velocity, 0.0) * 0.75
            + max(context.best_bid_size_velocity, 0.0) * 0.25
        )
        bearish_pull = (
            max(-context.best_bid_size_velocity, 0.0) * 0.75
            + max(context.best_ask_size_velocity, 0.0) * 0.25
        )
        direction = "long" if bullish_pull >= bearish_pull else "short"
        dominant_pull = bullish_pull if direction == "long" else bearish_pull
        flow_support = self._positive_for_direction(
            context.trade_flow_bias * 0.50
            + context.delta_trade_flow_bias * 0.20
            + context.book_imbalance * 0.20
            + context.delta_book_imbalance * 0.10,
            direction,
            scale=0.45,
        )
        micro_support = self._positive_for_direction(
            context.microprice_dislocation_bps,
            direction,
            scale=1.25,
        )
        spread_widening = _clamp(max(context.delta_spread_bps, 0.0) / 1.5)
        score = (
            _clamp(dominant_pull / 1.10) * 0.45
            + flow_support * 0.25
            + micro_support * 0.15
            + spread_widening * 0.15
        )
        return round(score, 4), direction

    def _book_churn_signal(self, context: BreakoutContext) -> float:
        broad_instability = _clamp(
            (
                abs(context.bid_depth_velocity)
                + abs(context.ask_depth_velocity)
                + abs(context.best_bid_size_velocity)
                + abs(context.best_ask_size_velocity)
            )
            / 3.0
        )
        two_sided_touch = _clamp(
            min(abs(context.best_bid_size_velocity), abs(context.best_ask_size_velocity)) / 0.35
        )
        two_sided_depth = _clamp(
            min(abs(context.bid_depth_velocity), abs(context.ask_depth_velocity)) / 0.35
        )
        spread_instability = _clamp(abs(context.delta_spread_bps) / 2.0)
        micro_noise = _clamp(abs(context.microprice_dislocation_bps) / 1.25)
        activity = _clamp(max(context.volume_ratio, context.trade_count_ratio) / 3.0)
        score = (
            broad_instability * 0.25
            + two_sided_touch * 0.20
            + two_sided_depth * 0.20
            + spread_instability * 0.15
            + micro_noise * 0.10
            + activity * 0.10
        )
        return round(score, 4)


OFFICIAL_SCENARIOS = (
    ScenarioSpec(
        name="baseline_breakout",
        description="Current Pod B breakout engine.",
    ),
    ScenarioSpec(
        name="memecoin_event_only",
        description="Ranked event-momentum only.",
        trigger_specs=(
            TriggerSpec(
                name="event_official",
                trigger_kind="event_momentum",
                description="Official comparable event momentum",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=5.0,
                min_bucket_notional_usd=100.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
        ),
    ),
    ScenarioSpec(
        name="memecoin_flow_only",
        description="Ranked flow-following only.",
        trigger_specs=(
            TriggerSpec(
                name="flow_official",
                trigger_kind="flow_following",
                description="Official comparable flow following",
                top_n=10,
                min_interest_score=0.50,
                max_spread_bps=4.5,
                min_bucket_notional_usd=125.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
        ),
    ),
    ScenarioSpec(
        name="memecoin_pullback_all",
        description="Robust broad pullback-reclaim variant.",
        trigger_specs=(
            TriggerSpec(
                name="pullback_official_all",
                trigger_kind="pullback_reclaim",
                description="Official comparable pullback reclaim across all regimes",
                top_n=5,
                min_interest_score=0.50,
                max_spread_bps=4.0,
                min_bucket_notional_usd=100.0,
            ),
        ),
    ),
    ScenarioSpec(
        name="memecoin_pullback_trend_panic",
        description="Aggressive trend/panic pullback-reclaim winner.",
        trigger_specs=(
            TriggerSpec(
                name="pullback_official_tp",
                trigger_kind="pullback_reclaim",
                description="Official comparable pullback reclaim in trend/panic only",
                top_n=5,
                min_interest_score=0.60,
                max_spread_bps=4.0,
                min_bucket_notional_usd=100.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
        ),
    ),
    ScenarioSpec(
        name="memecoin_combo",
        description="Event + flow + robust pullback memecoin pack.",
        trigger_specs=(
            TriggerSpec(
                name="event_official",
                trigger_kind="event_momentum",
                description="Official comparable event momentum",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=5.0,
                min_bucket_notional_usd=100.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
            TriggerSpec(
                name="flow_official",
                trigger_kind="flow_following",
                description="Official comparable flow following",
                top_n=10,
                min_interest_score=0.50,
                max_spread_bps=4.5,
                min_bucket_notional_usd=125.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
            TriggerSpec(
                name="pullback_official_all",
                trigger_kind="pullback_reclaim",
                description="Official comparable pullback reclaim across all regimes",
                top_n=5,
                min_interest_score=0.50,
                max_spread_bps=4.0,
                min_bucket_notional_usd=100.0,
            ),
        ),
    ),
    ScenarioSpec(
        name="hybrid_breakout_plus_combo",
        description="Current breakout families plus memecoin combo.",
        trigger_specs=(
            TriggerSpec(
                name="event_official",
                trigger_kind="event_momentum",
                description="Official comparable event momentum",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=5.0,
                min_bucket_notional_usd=100.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
            TriggerSpec(
                name="flow_official",
                trigger_kind="flow_following",
                description="Official comparable flow following",
                top_n=10,
                min_interest_score=0.50,
                max_spread_bps=4.5,
                min_bucket_notional_usd=125.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
            TriggerSpec(
                name="pullback_official_all",
                trigger_kind="pullback_reclaim",
                description="Official comparable pullback reclaim across all regimes",
                top_n=5,
                min_interest_score=0.50,
                max_spread_bps=4.0,
                min_bucket_notional_usd=100.0,
            ),
        ),
        include_base=True,
    ),
)

SPECIAL_SCENARIOS = (
    ScenarioSpec(
        name="baseline_breakout",
        description="Current Pod B breakout engine on the special-symbol sleeve.",
    ),
    ScenarioSpec(
        name="memecoin_event_only",
        description="Ranked event-momentum only on the sleeve.",
        trigger_specs=(
            TriggerSpec(
                name="event_special",
                trigger_kind="event_momentum",
                description="Special-symbol event momentum",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=3.5,
                min_bucket_notional_usd=50.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
        ),
    ),
    ScenarioSpec(
        name="memecoin_flow_only",
        description="Ranked flow-following only on the sleeve.",
        trigger_specs=(
            TriggerSpec(
                name="flow_special",
                trigger_kind="flow_following",
                description="Special-symbol flow following",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=3.0,
                min_bucket_notional_usd=50.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
        ),
    ),
    ScenarioSpec(
        name="memecoin_pullback_all",
        description="Robust broad pullback-reclaim variant on the sleeve.",
        trigger_specs=(
            TriggerSpec(
                name="pullback_special",
                trigger_kind="pullback_reclaim",
                description="Special-symbol pullback reclaim",
                top_n=5,
                min_interest_score=0.50,
                max_spread_bps=4.0,
                min_bucket_notional_usd=50.0,
            ),
        ),
    ),
    ScenarioSpec(
        name="memecoin_combo",
        description="Event + flow + pullback memecoin pack on the sleeve.",
        trigger_specs=(
            TriggerSpec(
                name="event_special",
                trigger_kind="event_momentum",
                description="Special-symbol event momentum",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=3.5,
                min_bucket_notional_usd=50.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
            TriggerSpec(
                name="flow_special",
                trigger_kind="flow_following",
                description="Special-symbol flow following",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=3.0,
                min_bucket_notional_usd=50.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
            TriggerSpec(
                name="pullback_special",
                trigger_kind="pullback_reclaim",
                description="Special-symbol pullback reclaim",
                top_n=5,
                min_interest_score=0.50,
                max_spread_bps=4.0,
                min_bucket_notional_usd=50.0,
            ),
        ),
    ),
    ScenarioSpec(
        name="hybrid_breakout_plus_combo",
        description="Current breakout families plus memecoin combo on the sleeve.",
        trigger_specs=(
            TriggerSpec(
                name="event_special",
                trigger_kind="event_momentum",
                description="Special-symbol event momentum",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=3.5,
                min_bucket_notional_usd=50.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
            TriggerSpec(
                name="flow_special",
                trigger_kind="flow_following",
                description="Special-symbol flow following",
                top_n=5,
                min_interest_score=0.55,
                max_spread_bps=3.0,
                min_bucket_notional_usd=50.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
            TriggerSpec(
                name="pullback_special",
                trigger_kind="pullback_reclaim",
                description="Special-symbol pullback reclaim",
                top_n=5,
                min_interest_score=0.50,
                max_spread_bps=4.0,
                min_bucket_notional_usd=50.0,
            ),
        ),
        include_base=True,
    ),
)

OFFICIAL_FULL_BOT_PROFILES = (
    AllocationProfile(
        name="trend_panic_diag",
        description="Diagnostic slot: active only in trend/panic.",
        allocations={
            "trend_expansion": {"pod_a": 0.70, "pod_b": 0.10, "pod_c": 0.20, "cash": 0.00},
            "range_auction": {"pod_a": 0.10, "pod_b": 0.00, "pod_c": 0.15, "cash": 0.75},
            "panic_squeeze": {"pod_a": 0.10, "pod_b": 0.10, "pod_c": 0.05, "cash": 0.75},
            "dead_zone": {"pod_a": 0.00, "pod_b": 0.00, "pod_c": 0.05, "cash": 0.95},
        },
    ),
    AllocationProfile(
        name="all_regime_scout",
        description="Scout sleeve: small capital across all crypto regimes.",
        allocations={
            "trend_expansion": {"pod_a": 0.75, "pod_b": 0.05, "pod_c": 0.20, "cash": 0.00},
            "range_auction": {"pod_a": 0.10, "pod_b": 0.05, "pod_c": 0.15, "cash": 0.70},
            "panic_squeeze": {"pod_a": 0.10, "pod_b": 0.10, "pod_c": 0.05, "cash": 0.75},
            "dead_zone": {"pod_a": 0.00, "pod_b": 0.05, "pod_c": 0.05, "cash": 0.90},
        },
    ),
)

POD_B_ONLY_PROFILE = AllocationProfile(
    name="pod_b_only_balanced",
    description="Standalone Pod B profile with deployable budgets across all regimes.",
    allocations={
        "trend_expansion": {"pod_a": 0.00, "pod_b": 0.20, "pod_c": 0.00, "cash": 0.80},
        "range_auction": {"pod_a": 0.00, "pod_b": 0.10, "pod_c": 0.00, "cash": 0.90},
        "panic_squeeze": {"pod_a": 0.00, "pod_b": 0.20, "pod_c": 0.00, "cash": 0.80},
        "dead_zone": {"pod_a": 0.00, "pod_b": 0.10, "pod_c": 0.00, "cash": 0.90},
    },
    pod_a_enabled=False,
    pod_b_enabled=True,
    pod_c_enabled=False,
)


def _allocation_config(section: dict[str, float]) -> AllocationConfig:
    return AllocationConfig(
        pod_a=float(section.get("pod_a", 0.0)),
        pod_b=float(section.get("pod_b", 0.0)),
        pod_c=float(section.get("pod_c", 0.0)),
        cash=float(section.get("cash", 0.0)),
    )


def _with_profile(config: AppConfig, profile: AllocationProfile) -> AppConfig:
    allocations = RegimeAllocations(
        trend_expansion=_allocation_config(profile.allocations["trend_expansion"]),
        range_auction=_allocation_config(profile.allocations["range_auction"]),
        panic_squeeze=_allocation_config(profile.allocations["panic_squeeze"]),
        dead_zone=_allocation_config(profile.allocations["dead_zone"]),
    )
    return replace(
        config,
        trident=replace(config.trident, allocations=allocations),
        pod_a=replace(config.pod_a, enabled=profile.pod_a_enabled),
        pod_b=replace(config.pod_b, enabled=profile.pod_b_enabled),
        pod_c=replace(config.pod_c, enabled=profile.pod_c_enabled),
    )


def _special_runtime_config(config: AppConfig) -> AppConfig:
    blocked = [
        symbol
        for symbol in config.hyperliquid.tradable_blocked_symbols
        if str(symbol).strip().upper() != "PENGU"
    ]
    return replace(
        config,
        hyperliquid=replace(
            config.hyperliquid,
            tradable_blocked_symbols=blocked,
        ),
    )


def _standalone_summary(name: str, description: str, backtest: dict[str, object]) -> dict[str, object]:
    return {
        "scenario": name,
        "description": description,
        **summarize_backtest(backtest),
    }


def _full_bot_summary(name: str, description: str, result) -> dict[str, object]:
    return {
        "scenario": name,
        "description": description,
        "total_realized_pnl_usd": round(result.total_realized_pnl_usd, 4),
        "pod_a_realized_pnl_usd": round(float(result.pod_a.get("realized_pnl_usd", 0.0) or 0.0), 4),
        "pod_b_realized_pnl_usd": round(float(result.pod_b.get("realized_pnl_usd", 0.0) or 0.0), 4),
        "pod_c_realized_pnl_usd": round(float(result.pod_c.get("realized_pnl_usd", 0.0) or 0.0), 4),
        "pod_b_signal_count": int(result.pod_b.get("signal_count", 0) or 0),
        "pod_b_accepted_count": int(result.pod_b.get("accepted_count", 0) or 0),
        "pod_b_closed_trade_count": int(result.pod_b.get("closed_trade_count", 0) or 0),
        "routing_reassignment_event_count": int(result.routing.get("reassignment_event_count", 0) or 0),
        "total_activity_count": int(result.total_activity_count or 0),
    }


def _build_service(config: AppConfig, scenario: ScenarioSpec) -> ExperimentalMemecoinService:
    return ExperimentalMemecoinService(
        config,
        trigger_specs=scenario.trigger_specs,
        include_base=scenario.include_base,
        label=scenario.name,
    )


def run_experiments(
    *,
    official_input: str | Path,
    special_input: str | Path,
    official_config: str | Path = "config/trident.toml",
    special_config: str | Path = "config/trident_special_symbols_core_shadow.toml",
    output_json: str | Path | None = None,
    output_md: str | Path | None = None,
) -> MemecoinPnlExperimentResult:
    base_official = load_config(official_config)
    base_special = _special_runtime_config(load_config(special_config))

    official_standalone: list[SuiteScenarioResult] = []
    standalone_config = _with_profile(base_official, POD_B_ONLY_PROFILE)
    for scenario in OFFICIAL_SCENARIOS:
        runner = PodBBacktestRunner(standalone_config)
        if scenario.trigger_specs or scenario.include_base:
            runner.service = _build_service(standalone_config, scenario)
        backtest = runner.run_jsonl(official_input).backtest
        official_standalone.append(
            SuiteScenarioResult(
                suite="official_standalone",
                profile=POD_B_ONLY_PROFILE.name,
                scenario=scenario.name,
                description=scenario.description,
                summary=_standalone_summary(scenario.name, scenario.description, backtest),
            )
        )

    official_full_bot: list[SuiteScenarioResult] = []
    for profile in OFFICIAL_FULL_BOT_PROFILES:
        runtime_config = _with_profile(base_official, profile)
        for scenario in OFFICIAL_SCENARIOS:
            runner = FullBotBacktestRunner(runtime_config, force_enable_all_pods=False)
            if scenario.trigger_specs or scenario.include_base:
                runner.pod_b_service = _build_service(runtime_config, scenario)
            result = runner.run_jsonl(official_input)
            official_full_bot.append(
                SuiteScenarioResult(
                    suite="official_full_bot",
                    profile=profile.name,
                    scenario=scenario.name,
                    description=scenario.description,
                    summary=_full_bot_summary(scenario.name, scenario.description, result),
                )
            )

    special_standalone: list[SuiteScenarioResult] = []
    special_standalone_config = _with_profile(base_special, POD_B_ONLY_PROFILE)
    for scenario in SPECIAL_SCENARIOS:
        runner = PodBBacktestRunner(special_standalone_config)
        if scenario.trigger_specs or scenario.include_base:
            runner.service = _build_service(special_standalone_config, scenario)
        backtest = runner.run_jsonl(special_input).backtest
        special_standalone.append(
            SuiteScenarioResult(
                suite="special_standalone",
                profile=POD_B_ONLY_PROFILE.name,
                scenario=scenario.name,
                description=scenario.description,
                summary=_standalone_summary(scenario.name, scenario.description, backtest),
            )
        )

    result = MemecoinPnlExperimentResult(
        official_input=str(official_input),
        special_input=str(special_input),
        official_standalone=[item.to_dict() for item in official_standalone],
        official_full_bot=[item.to_dict() for item in official_full_bot],
        special_standalone=[item.to_dict() for item in special_standalone],
        notes=[
            "Official standalone isolates signal quality using a balanced Pod B-only capital profile.",
            "Official full-bot tests two deployment styles: trend/panic diagnostic and all-regime scout.",
            "Special standalone unblocks PENGU to measure the sleeve fairly against the research winner.",
            "All memecoin scenarios remain backtest-only; live Pod B logic is unchanged.",
        ],
    )
    if output_json is not None:
        path = Path(output_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    if output_md is not None:
        path = Path(output_md)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(result), encoding="utf-8")
    return result


def _render_markdown(result: MemecoinPnlExperimentResult) -> str:
    lines = [
        "# Memecoin PnL Experiments",
        "",
        f"- Official input: `{result.official_input}`",
        f"- Special input: `{result.special_input}`",
        "",
        "## Official Standalone",
        "",
        "| Scenario | Realized PnL USD | Closed trades | Signals | Accepted | Win rate |",
        "|----------|-----------------:|--------------:|--------:|---------:|---------:|",
    ]
    for item in result.official_standalone:
        summary = item["summary"]
        lines.append(
            "| "
            f"{summary['scenario']} | "
            f"{summary['realized_pnl_usd']:.4f} | "
            f"{summary['closed_trade_count']} | "
            f"{summary['signal_count']} | "
            f"{summary['accepted_count']} | "
            f"{summary['win_rate']:.4f} |"
        )
    lines.extend(
        [
            "",
            "## Official Full Bot",
            "",
            "| Profile | Scenario | Total PnL USD | Pod B PnL USD | Pod A PnL USD | Pod C PnL USD | Pod B trades | Routing reassignments |",
            "|---------|----------|--------------:|--------------:|--------------:|--------------:|-------------:|----------------------:|",
        ]
    )
    for item in result.official_full_bot:
        summary = item["summary"]
        lines.append(
            "| "
            f"{item['profile']} | "
            f"{summary['scenario']} | "
            f"{summary['total_realized_pnl_usd']:.4f} | "
            f"{summary['pod_b_realized_pnl_usd']:.4f} | "
            f"{summary['pod_a_realized_pnl_usd']:.4f} | "
            f"{summary['pod_c_realized_pnl_usd']:.4f} | "
            f"{summary['pod_b_closed_trade_count']} | "
            f"{summary['routing_reassignment_event_count']} |"
        )
    lines.extend(
        [
            "",
            "## Special Standalone",
            "",
            "| Scenario | Realized PnL USD | Closed trades | Signals | Accepted | Win rate |",
            "|----------|-----------------:|--------------:|--------:|---------:|---------:|",
        ]
    )
    for item in result.special_standalone:
        summary = item["summary"]
        lines.append(
            "| "
            f"{summary['scenario']} | "
            f"{summary['realized_pnl_usd']:.4f} | "
            f"{summary['closed_trade_count']} | "
            f"{summary['signal_count']} | "
            f"{summary['accepted_count']} | "
            f"{summary['win_rate']:.4f} |"
        )
    lines.extend(["", "## Notes", ""])
    for note in result.notes:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run concrete memecoin PnL experiments for Pod B and sleeve replacements.")
    parser.add_argument("--official-input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument("--special-input", default="server-data/replay_inputs/special_symbols_hl_15m_30d_20260419.jsonl")
    parser.add_argument("--official-config", default="config/trident.toml")
    parser.add_argument("--special-config", default="config/trident_special_symbols_core_shadow.toml")
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = run_experiments(
        official_input=args.official_input,
        special_input=args.special_input,
        official_config=args.official_config,
        special_config=args.special_config,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(f"official_standalone_cases={len(result.official_standalone)}")
    print(f"official_full_bot_cases={len(result.official_full_bot)}")
    print(f"special_standalone_cases={len(result.special_standalone)}")


if __name__ == "__main__":
    main()
