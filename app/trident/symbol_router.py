from __future__ import annotations

from dataclasses import dataclass

from app.settings import AppConfig
from app.trident.types import PodName, Regime, SymbolMarketSnapshot, SymbolRoutingDecision


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


@dataclass(slots=True)
class SymbolRouter:
    config: AppConfig
    min_assign_score: float = 0.45
    min_hold_score: float = 0.35
    hysteresis_margin: float = 0.12

    def route(
        self,
        *,
        regime: Regime,
        desired_symbols_by_pod: dict[PodName, list[str]],
        snapshots: list[SymbolMarketSnapshot],
        previous_owners: dict[str, PodName | None] | None = None,
    ) -> list[SymbolRoutingDecision]:
        snapshot_by_symbol = {snapshot.symbol.upper(): snapshot for snapshot in snapshots}
        previous = {symbol.upper(): owner for symbol, owner in (previous_owners or {}).items()}
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
            pod_scores = {
                pod: (
                    self._score_pod(
                        pod=pod,
                        symbol=symbol,
                        regime=regime,
                        snapshot=snapshot,
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
                    previous_owner=previous_owner,
                    snapshot=snapshot,
                )
            )
        return decisions

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
        previous_owner: PodName | None,
        snapshot: SymbolMarketSnapshot | None,
    ) -> SymbolRoutingDecision:
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
        )

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
        symbol: str,
        regime: Regime,
        snapshot: SymbolMarketSnapshot | None,
    ) -> float:
        if snapshot is None:
            return 0.0
        if pod == PodName.POD_A:
            return round(self._score_pod_a(regime=regime, snapshot=snapshot), 4)
        if pod == PodName.POD_B:
            return round(self._score_pod_b(regime=regime, snapshot=snapshot), 4)
        if symbol.upper() in {item.upper() for item in self.config.pod_c.leader_symbols}:
            return 0.0
        return round(self._score_pod_c(regime=regime, snapshot=snapshot), 4)

    def _score_pod_a(self, *, regime: Regime, snapshot: SymbolMarketSnapshot) -> float:
        regime_quality = {
            Regime.TREND_EXPANSION: 1.0,
            Regime.PANIC_SQUEEZE: 0.75,
            Regime.RANGE_AUCTION: 0.45,
            Regime.DEAD_ZONE: 0.15,
            Regime.CASH: 0.0,
        }[regime]
        trend_bps = abs(snapshot.ema_fast - snapshot.ema_slow) / max(snapshot.price, 1e-9) * 10_000.0
        trend_shape = _clamp(trend_bps / 160.0)
        structure_quality = _clamp(abs(snapshot.structure_score))
        reclaim_quality = _clamp(1.0 - abs(snapshot.vwap_distance_bps) / 30.0)
        cluster_quality = 1.0 if snapshot.cluster_aligned else 0.3
        return (
            regime_quality * 0.35
            + trend_shape * 0.25
            + structure_quality * 0.25
            + reclaim_quality * 0.10
            + cluster_quality * 0.05
        )

    def _score_pod_b(self, *, regime: Regime, snapshot: SymbolMarketSnapshot) -> float:
        regime_quality = {
            Regime.RANGE_AUCTION: 1.0,
            Regime.DEAD_ZONE: 0.7,
            Regime.TREND_EXPANSION: 0.15,
            Regime.PANIC_SQUEEZE: 0.05,
            Regime.CASH: 0.0,
        }[regime]
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
            regime_quality * 0.35
            + range_quality * 0.25
            + structure_quality * 0.20
            + toxicity_quality * 0.15
            + spread_quality * 0.05
        )

    def _score_pod_c(self, *, regime: Regime, snapshot: SymbolMarketSnapshot) -> float:
        regime_quality = {
            Regime.PANIC_SQUEEZE: 1.0,
            Regime.TREND_EXPANSION: 0.8,
            Regime.RANGE_AUCTION: 0.35,
            Regime.DEAD_ZONE: 0.1,
            Regime.CASH: 0.0,
        }[regime]
        impulse_quality = _clamp(max(abs(snapshot.trade_flow_bias), abs(snapshot.book_imbalance)) * 1.6)
        range_quality = _clamp(snapshot.bucket_range_bps / 120.0)
        structure_quality = _clamp(abs(snapshot.structure_score))
        cluster_quality = 1.0 if snapshot.cluster_aligned else 0.2
        spread_quality = _clamp(1.0 - snapshot.spread_bps / max(self.config.pod_c.max_spread_bps * 1.5, 1.0))
        return (
            regime_quality * 0.30
            + impulse_quality * 0.25
            + range_quality * 0.15
            + structure_quality * 0.15
            + cluster_quality * 0.10
            + spread_quality * 0.05
        )
