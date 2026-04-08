from __future__ import annotations

from dataclasses import dataclass
import logging
import math

from app.settings import AppConfig
from app.trident.types import (
    PodName,
    Regime,
    SymbolLocalRegime,
    SymbolMarketSnapshot,
    SymbolRoutingDecision,
)

logger = logging.getLogger(__name__)


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


@dataclass(slots=True)
class SymbolRouter:
    config: AppConfig
    min_assign_score: float = 0.45
    min_hold_score: float = 0.35
    hysteresis_margin: float = 0.15
    reassignment_cooldown_seconds: int = 900
    reassignment_debounce_min_score: float = 0.15
    reassignment_debounce_seconds_by_symbol: dict[str, int] | None = None
    symbol_pod_overrides: dict[str, PodName] | None = None

    def __post_init__(self) -> None:
        routing = self.config.trident.routing
        self.min_assign_score = float(routing.min_assign_score)
        self.min_hold_score = float(routing.min_hold_score)
        self.hysteresis_margin = float(routing.hysteresis_margin)
        self.reassignment_cooldown_seconds = int(routing.reassignment_cooldown_seconds)
        self.reassignment_debounce_min_score = float(routing.reassignment_debounce_min_score)
        self.reassignment_debounce_seconds_by_symbol = {
            str(symbol).upper(): int(seconds)
            for symbol, seconds in routing.reassignment_debounce_seconds_by_symbol.items()
        }
        self.set_symbol_pod_overrides(routing.symbol_pod_overrides)

    def set_symbol_pod_overrides(self, raw_overrides: dict[str, str]) -> None:
        self.symbol_pod_overrides = self._normalize_symbol_pod_overrides(raw_overrides)

    def route(
        self,
        *,
        regime: Regime,
        desired_symbols_by_pod: dict[PodName, list[str]],
        snapshots: list[SymbolMarketSnapshot],
        previous_owners: dict[str, PodName | None] | None = None,
        reassignment_age_seconds_by_symbol: dict[str, float] | None = None,
    ) -> list[SymbolRoutingDecision]:
        snapshot_by_symbol = {snapshot.symbol.upper(): snapshot for snapshot in snapshots}
        previous = {symbol.upper(): owner for symbol, owner in (previous_owners or {}).items()}
        reassignment_ages = {
            symbol.upper(): age
            for symbol, age in (reassignment_age_seconds_by_symbol or {}).items()
        }
        candidate_symbols = sorted(
            {
                symbol.upper()
                for symbols in desired_symbols_by_pod.values()
                for symbol in symbols
            }
        )
        decisions: list[SymbolRoutingDecision] = []
        for symbol in candidate_symbols:
            candidates = self._candidate_pods(symbol, desired_symbols_by_pod)
            if not candidates:
                continue
            snapshot = snapshot_by_symbol.get(symbol)
            previous_owner = previous.get(symbol)
            local_regime, local_regime_reason = self._classify_local_regime(snapshot)
            override_owner = self._override_owner(symbol)
            pod_scores = {
                pod: (
                    self._score_pod(
                        pod=pod,
                        regime=regime,
                        snapshot=snapshot,
                        local_regime=local_regime,
                    )
                    if snapshot is not None
                    else 0.0
                )
                for pod in candidates
            }
            decisions.append(
                self._pick_owner(
                    symbol=symbol,
                    candidates=candidates,
                    pod_scores=pod_scores,
                    regime=regime,
                    previous_owner=previous_owner,
                    snapshot=snapshot,
                    local_regime=local_regime,
                    local_regime_reason=local_regime_reason,
                    reassignment_age_seconds=reassignment_ages.get(symbol),
                    override_owner=override_owner,
                )
            )
        return self._enforce_capacity_limits(decisions, regime=regime)

    def _enforce_capacity_limits(
        self,
        decisions: list[SymbolRoutingDecision],
        *,
        regime: Regime,
    ) -> list[SymbolRoutingDecision]:
        decisions_by_symbol = {decision.symbol: decision for decision in decisions}
        capacity_by_pod = self._capacity_limit_by_pod(regime)

        while True:
            overflowing_pod = next(
                (
                    pod
                    for pod in (PodName.POD_A, PodName.POD_B, PodName.POD_C)
                    if len(
                        [
                            item
                            for item in decisions_by_symbol.values()
                            if item.owner == pod
                        ]
                    )
                    > capacity_by_pod.get(pod, math.inf)
                ),
                None,
            )
            if overflowing_pod is None:
                break

            owned = [
                item
                for item in decisions_by_symbol.values()
                if item.owner == overflowing_pod
            ]
            capacity = int(capacity_by_pod.get(overflowing_pod, math.inf))
            ranked = sorted(
                owned,
                key=lambda item: (
                    -item.pod_scores.get(overflowing_pod, 0.0),
                    -(1 if item.previous_owner == overflowing_pod else 0),
                    item.symbol,
                ),
            )
            keep_symbols = {item.symbol for item in ranked[:capacity]}
            overflow = [item for item in ranked if item.symbol not in keep_symbols]
            for decision in overflow:
                decisions_by_symbol[decision.symbol] = self._reassign_after_capacity_trim(
                    decision,
                    trimmed_pod=overflowing_pod,
                    capacity_by_pod=capacity_by_pod,
                    decisions_by_symbol=decisions_by_symbol,
                )

        return sorted(decisions_by_symbol.values(), key=lambda item: item.symbol)

    def _reassign_after_capacity_trim(
        self,
        decision: SymbolRoutingDecision,
        *,
        trimmed_pod: PodName,
        capacity_by_pod: dict[PodName, int | float],
        decisions_by_symbol: dict[str, SymbolRoutingDecision],
    ) -> SymbolRoutingDecision:
        alternative_candidates = [
            pod
            for pod in decision.candidate_pods
            if pod != trimmed_pod and self._pod_has_capacity(
                pod,
                capacity_by_pod=capacity_by_pod,
                decisions_by_symbol=decisions_by_symbol,
            )
        ]
        if not alternative_candidates:
            return SymbolRoutingDecision(
                symbol=decision.symbol,
                owner=None,
                mode="allocation_capacity",
                reason=f"capacity_trim:{trimmed_pod.value}",
                previous_owner=decision.previous_owner,
                candidate_pods=list(decision.candidate_pods),
                pod_scores=dict(decision.pod_scores),
                local_regime=decision.local_regime,
                local_regime_reason=decision.local_regime_reason,
                pod_reasoning=dict(decision.pod_reasoning),
                reassignment_cooldown_active=decision.reassignment_cooldown_active,
                reassignment_cooldown_remaining_seconds=decision.reassignment_cooldown_remaining_seconds,
                override_active=decision.override_active,
                override_owner=decision.override_owner,
            )

        best_owner = sorted(
            alternative_candidates,
            key=lambda pod: (
                decision.pod_scores.get(pod, 0.0),
                -self._priority_rank(pod),
            ),
            reverse=True,
        )[0]
        best_score = decision.pod_scores.get(best_owner, 0.0)
        if best_score < self.min_assign_score:
            return SymbolRoutingDecision(
                symbol=decision.symbol,
                owner=None,
                mode="allocation_capacity",
                reason=f"capacity_trim:{trimmed_pod.value}",
                previous_owner=decision.previous_owner,
                candidate_pods=list(decision.candidate_pods),
                pod_scores=dict(decision.pod_scores),
                local_regime=decision.local_regime,
                local_regime_reason=decision.local_regime_reason,
                pod_reasoning=dict(decision.pod_reasoning),
                reassignment_cooldown_active=decision.reassignment_cooldown_active,
                reassignment_cooldown_remaining_seconds=decision.reassignment_cooldown_remaining_seconds,
                override_active=decision.override_active,
                override_owner=decision.override_owner,
            )

        return SymbolRoutingDecision(
            symbol=decision.symbol,
            owner=best_owner,
            mode="allocation_capacity",
            reason=(
                f"capacity_rebalance:{trimmed_pod.value}->{best_owner.value}"
                f" ({best_score:.2f})"
            ),
            previous_owner=decision.previous_owner,
            candidate_pods=list(decision.candidate_pods),
            pod_scores=dict(decision.pod_scores),
            local_regime=decision.local_regime,
            local_regime_reason=decision.local_regime_reason,
            pod_reasoning=dict(decision.pod_reasoning),
            reassignment_cooldown_active=decision.reassignment_cooldown_active,
            reassignment_cooldown_remaining_seconds=decision.reassignment_cooldown_remaining_seconds,
            override_active=decision.override_active,
            override_owner=decision.override_owner,
        )

    def _pod_has_capacity(
        self,
        pod: PodName,
        *,
        capacity_by_pod: dict[PodName, int | float],
        decisions_by_symbol: dict[str, SymbolRoutingDecision],
    ) -> bool:
        capacity = capacity_by_pod.get(pod, math.inf)
        if math.isinf(capacity):
            return True
        current = len([item for item in decisions_by_symbol.values() if item.owner == pod])
        return current < capacity

    def _capacity_limit_by_pod(self, regime: Regime) -> dict[PodName, int | float]:
        base = self._base_allocations(regime)
        total_equity = max(self.config.trident.capital.reference_equity_usd, 0.0)
        min_symbol_usd = self.config.trident.capital.min_symbol_allocation_usd
        if min_symbol_usd <= 0:
            return {
                PodName.POD_A: math.inf,
                PodName.POD_B: math.inf,
                PodName.POD_C: math.inf,
            }

        return {
            PodName.POD_A: self._pod_symbol_capacity(
                target_pct=base.get("pod_a", 0.0),
                pod_cap=self.config.pod_a.max_allocation_pct,
                enabled=self.config.pod_a.enabled,
                total_equity=total_equity,
                min_symbol_usd=min_symbol_usd,
            ),
            PodName.POD_B: self._pod_symbol_capacity(
                target_pct=base.get("pod_b", 0.0),
                pod_cap=self.config.pod_b.max_allocation_pct,
                enabled=self.config.pod_b.enabled,
                total_equity=total_equity,
                min_symbol_usd=min_symbol_usd,
            ),
            PodName.POD_C: self._pod_symbol_capacity(
                target_pct=base.get("pod_c", 0.0),
                pod_cap=self.config.pod_c.max_allocation_pct,
                enabled=self.config.pod_c.enabled,
                total_equity=total_equity,
                min_symbol_usd=min_symbol_usd,
            ),
        }

    def _pod_symbol_capacity(
        self,
        *,
        target_pct: float,
        pod_cap: float,
        enabled: bool,
        total_equity: float,
        min_symbol_usd: float,
    ) -> int:
        if not enabled:
            return 0
        effective_target_pct = min(max(target_pct, 0.0), max(pod_cap, 0.0))
        target_usd = effective_target_pct * total_equity
        return int(target_usd // min_symbol_usd) if target_usd >= min_symbol_usd else 0

    def _base_allocations(self, regime: Regime) -> dict[str, float]:
        if regime == Regime.TREND_EXPANSION:
            section = self.config.trident.allocations.trend_expansion
        elif regime == Regime.RANGE_AUCTION:
            section = self.config.trident.allocations.range_auction
        elif regime == Regime.PANIC_SQUEEZE:
            section = self.config.trident.allocations.panic_squeeze
        else:
            section = self.config.trident.allocations.dead_zone
        return {
            "pod_a": section.pod_a,
            "pod_b": section.pod_b,
            "pod_c": section.pod_c,
            "cash": section.cash,
        }

    def _candidate_pods(
        self,
        symbol: str,
        desired_symbols_by_pod: dict[PodName, list[str]],
    ) -> list[PodName]:
        normalized = symbol.upper()
        candidates: list[PodName] = []
        for pod in (PodName.POD_A, PodName.POD_B, PodName.POD_C):
            desired = desired_symbols_by_pod.get(pod, [])
            if normalized in {item.upper() for item in desired}:
                candidates.append(pod)
        return candidates

    def _pick_owner(
        self,
        *,
        symbol: str,
        candidates: list[PodName],
        pod_scores: dict[PodName, float],
        regime: Regime,
        previous_owner: PodName | None,
        snapshot: SymbolMarketSnapshot | None,
        local_regime: SymbolLocalRegime | None,
        local_regime_reason: str,
        reassignment_age_seconds: float | None,
        override_owner: PodName | None,
    ) -> SymbolRoutingDecision:
        pod_reasoning = self._build_pod_reasoning(
            candidates=candidates,
            pod_scores=pod_scores,
            local_regime=local_regime,
            regime=regime,
            override_owner=override_owner,
        )
        if override_owner is not None and override_owner in candidates:
            override_score = pod_scores.get(override_owner, 0.0)
            return SymbolRoutingDecision(
                symbol=symbol,
                owner=override_owner,
                mode="manual_override",
                reason=f"manual_override:{override_owner.value} ({override_score:.2f})",
                previous_owner=previous_owner,
                candidate_pods=list(candidates),
                pod_scores=dict(pod_scores),
                local_regime=local_regime,
                local_regime_reason=local_regime_reason,
                pod_reasoning=pod_reasoning,
                override_active=True,
                override_owner=override_owner,
            )
        if snapshot is None:
            owner = previous_owner if previous_owner in candidates else self._priority_pick(candidates)
            reason = (
                f"keep_previous:{previous_owner.value}"
                if previous_owner in candidates and previous_owner is not None
                else f"fallback_priority:{owner.value if owner is not None else 'none'}"
            )
            return SymbolRoutingDecision(
                symbol=symbol,
                owner=owner,
                mode="fallback_priority",
                reason=reason,
                previous_owner=previous_owner,
                candidate_pods=list(candidates),
                pod_scores=dict(pod_scores),
                local_regime=local_regime,
                local_regime_reason=local_regime_reason,
                pod_reasoning=pod_reasoning,
                override_owner=override_owner,
            )

        ranked = sorted(
            candidates,
            key=lambda pod: (pod_scores.get(pod, 0.0), -self._priority_rank(pod)),
            reverse=True,
        )
        best_owner = ranked[0]
        best_score = pod_scores.get(best_owner, 0.0)
        previous_score = (
            pod_scores.get(previous_owner, 0.0)
            if previous_owner is not None and previous_owner in candidates
            else 0.0
        )
        cooldown_active = (
            previous_owner is not None
            and previous_owner in candidates
            and best_owner != previous_owner
            and reassignment_age_seconds is not None
            and reassignment_age_seconds < self.reassignment_cooldown_seconds
            and previous_score >= self.min_hold_score
        )

        if cooldown_active:
            remaining = max(
                float(self.reassignment_cooldown_seconds) - float(reassignment_age_seconds),
                0.0,
            )
            return SymbolRoutingDecision(
                symbol=symbol,
                owner=previous_owner,
                mode="dynamic_cooldown",
                reason=(
                    f"reassignment_cooldown_hold:{previous_owner.value}"
                    f" ({remaining:.0f}s remaining, {previous_score:.2f} vs {best_owner.value} {best_score:.2f})"
                ),
                previous_owner=previous_owner,
                candidate_pods=list(candidates),
                pod_scores=dict(pod_scores),
                local_regime=local_regime,
                local_regime_reason=local_regime_reason,
                pod_reasoning=pod_reasoning,
                reassignment_cooldown_active=True,
                reassignment_cooldown_remaining_seconds=remaining,
                override_owner=override_owner,
            )

        debounce_seconds = self._symbol_debounce_seconds(symbol)
        debounce_active = (
            previous_owner is not None
            and previous_owner in candidates
            and best_owner != previous_owner
            and reassignment_age_seconds is not None
            and debounce_seconds > 0
            and reassignment_age_seconds < debounce_seconds
            and previous_score >= self.reassignment_debounce_min_score
        )

        if debounce_active:
            remaining = max(
                float(debounce_seconds) - float(reassignment_age_seconds),
                0.0,
            )
            return SymbolRoutingDecision(
                symbol=symbol,
                owner=previous_owner,
                mode="dynamic_debounce",
                reason=(
                    f"reassignment_debounce_hold:{previous_owner.value}"
                    f" ({remaining:.0f}s remaining, {previous_score:.2f} vs {best_owner.value} {best_score:.2f})"
                ),
                previous_owner=previous_owner,
                candidate_pods=list(candidates),
                pod_scores=dict(pod_scores),
                local_regime=local_regime,
                local_regime_reason=local_regime_reason,
                pod_reasoning=pod_reasoning,
                reassignment_cooldown_active=True,
                reassignment_cooldown_remaining_seconds=remaining,
                override_owner=override_owner,
            )

        if (
            previous_owner is not None
            and previous_owner in candidates
            and previous_score >= self.min_hold_score
            and previous_score + self.hysteresis_margin >= best_score
        ):
            return SymbolRoutingDecision(
                symbol=symbol,
                owner=previous_owner,
                mode="dynamic_hysteresis",
                reason=(
                    f"hysteresis_hold:{previous_owner.value}"
                    f" ({previous_score:.2f} vs {best_owner.value} {best_score:.2f})"
                ),
                previous_owner=previous_owner,
                candidate_pods=list(candidates),
                pod_scores=dict(pod_scores),
                local_regime=local_regime,
                local_regime_reason=local_regime_reason,
                pod_reasoning=pod_reasoning,
                override_owner=override_owner,
            )

        if best_score >= self.min_assign_score:
            return SymbolRoutingDecision(
                symbol=symbol,
                owner=best_owner,
                mode="dynamic_affinity",
                reason=f"best_affinity:{best_owner.value} ({best_score:.2f})",
                previous_owner=previous_owner,
                candidate_pods=list(candidates),
                pod_scores=dict(pod_scores),
                local_regime=local_regime,
                local_regime_reason=local_regime_reason,
                pod_reasoning=pod_reasoning,
                override_owner=override_owner,
            )

        owner = previous_owner if previous_owner in candidates else self._priority_pick(candidates)
        return SymbolRoutingDecision(
            symbol=symbol,
            owner=owner,
            mode="fallback_priority",
            reason=(
                f"weak_signal_keep:{owner.value}"
                if owner is not None and owner == previous_owner
                else f"fallback_priority:{owner.value if owner is not None else 'none'}"
            ),
            previous_owner=previous_owner,
            candidate_pods=list(candidates),
            pod_scores=dict(pod_scores),
            local_regime=local_regime,
            local_regime_reason=local_regime_reason,
            pod_reasoning=pod_reasoning,
            override_owner=override_owner,
        )

    def _symbol_debounce_seconds(self, symbol: str) -> int:
        if not self.reassignment_debounce_seconds_by_symbol:
            return 0
        return max(
            int(self.reassignment_debounce_seconds_by_symbol.get(symbol.upper(), 0)),
            0,
        )

    def _build_pod_reasoning(
        self,
        *,
        candidates: list[PodName],
        pod_scores: dict[PodName, float],
        local_regime: SymbolLocalRegime | None,
        regime: Regime,
        override_owner: PodName | None,
    ) -> dict[PodName, str]:
        reasoning: dict[PodName, str] = {}
        best_score = max((pod_scores.get(pod, 0.0) for pod in candidates), default=0.0)
        best_pods = {
            pod for pod in candidates if abs(pod_scores.get(pod, 0.0) - best_score) < 1e-9
        }
        for pod in candidates:
            score = pod_scores.get(pod, 0.0)
            affinity = self._local_regime_affinity(pod, local_regime)
            override_note = (
                f" override={override_owner.value}"
                if override_owner is not None
                else ""
            )
            if override_owner == pod:
                reasoning[pod] = (
                    f"manual_override score={score:.2f} local_affinity={affinity:.2f}"
                    f" global_regime={regime.value}{override_note}"
                )
                continue
            if score < self.min_assign_score:
                reasoning[pod] = (
                    f"below_assign_threshold score={score:.2f} local_affinity={affinity:.2f}"
                    f" global_regime={regime.value}{override_note}"
                )
            elif pod in best_pods:
                reasoning[pod] = (
                    f"best_candidate score={score:.2f} local_affinity={affinity:.2f}"
                    f" global_regime={regime.value}{override_note}"
                )
            else:
                reasoning[pod] = (
                    f"outscored score={score:.2f} best_score={best_score:.2f}"
                    f" local_affinity={affinity:.2f} global_regime={regime.value}{override_note}"
                )
        return reasoning

    def _override_owner(self, symbol: str) -> PodName | None:
        if not self.symbol_pod_overrides:
            return None
        return self.symbol_pod_overrides.get(symbol.upper())

    def _normalize_symbol_pod_overrides(
        self,
        raw_overrides: dict[str, str],
    ) -> dict[str, PodName]:
        normalized: dict[str, PodName] = {}
        valid_pods = {pod.value: pod for pod in PodName}
        for symbol, raw_pod in raw_overrides.items():
            pod = valid_pods.get(str(raw_pod).strip().lower())
            if pod is None:
                logger.warning(
                    "Ignoring invalid symbol routing override; symbol=%s target=%s",
                    symbol,
                    raw_pod,
                )
                continue
            normalized[str(symbol).strip().upper()] = pod
        return normalized

    def _priority_pick(self, candidates: list[PodName]) -> PodName | None:
        if not candidates:
            return None
        return sorted(candidates, key=self._priority_rank)[0]

    def _priority_rank(self, pod: PodName) -> int:
        order = {
            PodName.POD_C: 0,
            PodName.POD_A: 1,
            PodName.POD_B: 2,
        }
        return order[pod]

    def _score_pod(
        self,
        *,
        pod: PodName,
        regime: Regime,
        snapshot: SymbolMarketSnapshot | None,
        local_regime: SymbolLocalRegime | None,
    ) -> float:
        if snapshot is None:
            return 0.0
        if pod == PodName.POD_A:
            return round(
                self._score_pod_a(
                    regime=regime,
                    snapshot=snapshot,
                    local_regime=local_regime,
                ),
                4,
            )
        if pod == PodName.POD_B:
            return round(
                self._score_pod_b(
                    regime=regime,
                    snapshot=snapshot,
                    local_regime=local_regime,
                ),
                4,
            )
        return round(
            self._score_pod_c(
                regime=regime,
                snapshot=snapshot,
                local_regime=local_regime,
            ),
            4,
        )

    def _score_pod_a(
        self,
        *,
        regime: Regime,
        snapshot: SymbolMarketSnapshot,
        local_regime: SymbolLocalRegime | None,
    ) -> float:
        global_quality = {
            Regime.TREND_EXPANSION: 1.0,
            Regime.PANIC_SQUEEZE: 0.75,
            Regime.RANGE_AUCTION: 0.45,
            Regime.DEAD_ZONE: 0.15,
            Regime.CASH: 0.0,
        }[regime]
        local_quality = self._local_regime_affinity(PodName.POD_A, local_regime)
        trend_bps = abs(snapshot.ema_fast - snapshot.ema_slow) / max(snapshot.price, 1e-9) * 10_000.0
        trend_shape = _clamp(trend_bps / 160.0)
        structure_quality = _clamp(abs(snapshot.structure_score))
        reclaim_quality = _clamp(1.0 - abs(snapshot.vwap_distance_bps) / 30.0)
        cluster_quality = 1.0 if snapshot.cluster_aligned else 0.3
        return (
            local_quality * 0.15
            + global_quality * 0.25
            + trend_shape * 0.25
            + structure_quality * 0.20
            + reclaim_quality * 0.10
            + cluster_quality * 0.05
        )

    def _score_pod_b(
        self,
        *,
        regime: Regime,
        snapshot: SymbolMarketSnapshot,
        local_regime: SymbolLocalRegime | None,
    ) -> float:
        global_quality = {
            Regime.RANGE_AUCTION: 1.0,
            Regime.DEAD_ZONE: 0.7,
            Regime.TREND_EXPANSION: 0.15,
            Regime.PANIC_SQUEEZE: 0.05,
            Regime.CASH: 0.0,
        }[regime]
        local_quality = self._local_regime_affinity(PodName.POD_B, local_regime)
        range_limit = max(self.config.pod_b.paper_guard_max_range_width_bps, 1.0)
        range_quality = _clamp(1.0 - snapshot.bucket_range_bps / (range_limit * 1.5))
        structure_quality = _clamp(
            1.0
            - abs(snapshot.structure_score)
            / max(self.config.pod_b.paper_guard_max_abs_structure_score * 3.0, 0.6)
        )
        toxicity = max(abs(snapshot.trade_flow_bias), abs(snapshot.book_imbalance))
        toxicity_quality = _clamp(
            1.0
            - toxicity / max(self.config.pod_b.paper_flow_toxicity_threshold * 4.0, 0.8)
        )
        spread_quality = _clamp(1.0 - snapshot.spread_bps / 8.0)
        return (
            local_quality * 0.15
            + global_quality * 0.25
            + range_quality * 0.25
            + structure_quality * 0.20
            + toxicity_quality * 0.10
            + spread_quality * 0.05
        )

    def _score_pod_c(
        self,
        *,
        regime: Regime,
        snapshot: SymbolMarketSnapshot,
        local_regime: SymbolLocalRegime | None,
    ) -> float:
        global_quality = {
            Regime.RANGE_AUCTION: 1.0,
            Regime.DEAD_ZONE: 0.8,
            Regime.TREND_EXPANSION: 0.4,
            Regime.PANIC_SQUEEZE: 0.2,
            Regime.CASH: 0.0,
        }[regime]
        local_quality = self._local_regime_affinity(PodName.POD_C, local_regime)
        range_limit = max(self.config.pod_b.paper_guard_max_range_width_bps, 1.0)
        squeeze_quality = _clamp(1.0 - snapshot.bucket_range_bps / (range_limit * 0.8))
        spread_quality = _clamp(1.0 - snapshot.spread_bps / max(self.config.pod_c.max_spread_bps * 1.5, 1.0))
        volume_quality = _clamp(snapshot.bucket_trade_count / 10.0)
        structure_quality = _clamp(1.0 - abs(snapshot.structure_score) * 0.8)
        return (
            local_quality * 0.10
            + global_quality * 0.20
            + squeeze_quality * 0.30
            + spread_quality * 0.15
            + volume_quality * 0.15
            + structure_quality * 0.10
        )

    def _classify_local_regime(
        self,
        snapshot: SymbolMarketSnapshot | None,
    ) -> tuple[SymbolLocalRegime | None, str]:
        if snapshot is None:
            return None, "missing_snapshot"

        trend_bps = abs(snapshot.ema_fast - snapshot.ema_slow) / max(snapshot.price, 1e-9) * 10_000.0
        structure_abs = abs(snapshot.structure_score)
        toxicity = max(abs(snapshot.trade_flow_bias), abs(snapshot.book_imbalance))
        range_limit = max(self.config.pod_b.paper_guard_max_range_width_bps, 1.0)
        event_score = (
            _clamp(toxicity / max(self.config.pod_b.paper_flow_toxicity_threshold, 0.2))
            * 0.45
            + _clamp(snapshot.bucket_range_bps / max(range_limit, 40.0)) * 0.30
            + _clamp(structure_abs / 0.35) * 0.25
        )
        trend_score = (
            _clamp(trend_bps / 60.0) * 0.40
            + _clamp(structure_abs / 0.25) * 0.30
            + _clamp(1.0 - abs(snapshot.vwap_distance_bps) / 25.0) * 0.20
            + (0.10 if snapshot.cluster_aligned else 0.03)
        )
        range_score = (
            _clamp(1.0 - snapshot.bucket_range_bps / max(range_limit, 25.0)) * 0.35
            + _clamp(
                1.0
                - structure_abs
                / max(self.config.pod_b.paper_guard_max_abs_structure_score * 2.5, 0.5)
            )
            * 0.25
            + _clamp(
                1.0
                - toxicity
                / max(self.config.pod_b.paper_flow_toxicity_threshold * 3.0, 0.8)
            )
            * 0.25
            + _clamp(1.0 - trend_bps / 45.0) * 0.15
        )

        ranked = sorted(
            [
                (SymbolLocalRegime.EVENT_IMPULSE, event_score),
                (SymbolLocalRegime.TREND_STRUCTURE, trend_score),
                (SymbolLocalRegime.RANGE_STRUCTURE, range_score),
            ],
            key=lambda item: item[1],
            reverse=True,
        )
        best_regime, best_score = ranked[0]
        if best_score < 0.50:
            return (
                SymbolLocalRegime.NEUTRAL,
                (
                    "neutral_local_state"
                    f" (trend={trend_score:.2f}, range={range_score:.2f}, event={event_score:.2f})"
                ),
            )
        return (
            best_regime,
            (
                f"{best_regime.value}"
                f" (trend={trend_score:.2f}, range={range_score:.2f}, event={event_score:.2f})"
            ),
        )

    def _local_regime_affinity(
        self,
        pod: PodName,
        local_regime: SymbolLocalRegime | None,
    ) -> float:
        if local_regime is None:
            return 0.0
        affinities = {
            PodName.POD_A: {
                SymbolLocalRegime.TREND_STRUCTURE: 1.0,
                SymbolLocalRegime.EVENT_IMPULSE: 0.65,
                SymbolLocalRegime.RANGE_STRUCTURE: 0.30,
                SymbolLocalRegime.NEUTRAL: 0.45,
            },
            PodName.POD_B: {
                SymbolLocalRegime.TREND_STRUCTURE: 0.20,
                SymbolLocalRegime.EVENT_IMPULSE: 0.10,
                SymbolLocalRegime.RANGE_STRUCTURE: 1.0,
                SymbolLocalRegime.NEUTRAL: 0.55,
            },
            PodName.POD_C: {
                SymbolLocalRegime.TREND_STRUCTURE: 0.75,
                SymbolLocalRegime.EVENT_IMPULSE: 1.0,
                SymbolLocalRegime.RANGE_STRUCTURE: 0.25,
                SymbolLocalRegime.NEUTRAL: 0.35,
            },
        }
        return affinities[pod][local_regime]
