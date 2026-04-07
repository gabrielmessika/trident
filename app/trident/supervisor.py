from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.settings import AppConfig
from app.trident.capital_allocator import CapitalAllocator
from app.trident.kill_switch import KillSwitch
from app.trident.market_clusters import all_cluster_leaders, enrich_snapshots
from app.trident.pod_a import AnchorTrendPlanner, AnchorTrendService, MarketContextService
from app.trident.pod_b import PassivbotManager
from app.trident.pod_c import EventContextService, EventRaiderPlanner, EventRaiderService
from app.trident.pod_runtime import ConfiguredPod
from app.trident.regime_allocator import RegimeAllocator
from app.trident.symbol_router import SymbolRouter
from app.trident.symbol_registry import SymbolRegistry
from app.trident.types import (
    CapitalPlan,
    LocalSymbolState,
    LocalSymbolTransition,
    ObservedSymbolStatus,
    OwnershipConflict,
    PodHealth,
    PodName,
    PodAllocation,
    RegimeSnapshot,
    RegimeTransition,
    SignalPreview,
    SymbolAllocation,
    SymbolLocalRegime,
    SymbolMarketSnapshot,
    SymbolRoutingDecision,
    SupervisorState,
    TradePlan,
)

logger = logging.getLogger(__name__)


class TridentSupervisor:
    """Phase-0 supervisor with config, registry, and health state only."""

    def __init__(self, config: AppConfig, profile: str, mode: str) -> None:
        self.config = config
        self.profile = profile
        self.mode = mode
        self.registry = SymbolRegistry()
        self.kill_switch = KillSwitch()
        self.regime_allocator = RegimeAllocator(config)
        self.capital_allocator = CapitalAllocator(config)
        self.symbol_router = SymbolRouter(config)
        self.pod_a_context_service = MarketContextService(config)
        self.pod_a_service = AnchorTrendService()
        self.pod_a_planner = AnchorTrendPlanner(config)
        self.pod_c_context_service = EventContextService(config)
        self.pod_c_service = EventRaiderService(config.pod_c)
        self.pod_c_planner = EventRaiderPlanner(config.pod_c)
        self.pod_b_manager = PassivbotManager(config)
        self.state = SupervisorState(
            regime=self.regime_allocator.current_regime(),
            mode=mode,
            profile=profile,
            enabled_pods=self._enabled_pods(),
        )
        self.pods = self._build_pods()
        self.sync_symbol_ownership()
        self.capital_plan = self._build_capital_plan()
        self.sync_pod_b()

    def _observed_symbols(self) -> list[str]:
        source = (
            self.config.hyperliquid.observation_universe
            or self.config.hyperliquid.default_coins
            or self.config.pod_a.symbols
        )
        seen: set[str] = set()
        normalized: list[str] = []
        for symbol in source:
            name = str(symbol).strip().upper()
            if not name or name in seen:
                continue
            seen.add(name)
            normalized.append(name)
        return normalized

    def _enabled_pods(self) -> list[PodName]:
        enabled: list[PodName] = []
        if self.config.pod_a.enabled:
            enabled.append(PodName.POD_A)
        if self.config.pod_b.enabled:
            enabled.append(PodName.POD_B)
        if self.config.pod_c.enabled:
            enabled.append(PodName.POD_C)
        return enabled

    def _build_pods(self) -> dict[PodName, ConfiguredPod]:
        return {
            PodName.POD_A: ConfiguredPod(
                name=PodName.POD_A,
                enabled=self.config.pod_a.enabled,
            ),
            PodName.POD_B: ConfiguredPod(
                name=PodName.POD_B,
                enabled=self.config.pod_b.enabled,
            ),
            PodName.POD_C: ConfiguredPod(
                name=PodName.POD_C,
                enabled=self.config.pod_c.enabled,
            ),
        }

    def _pod_priority(self) -> list[PodName]:
        return [PodName.POD_C, PodName.POD_A, PodName.POD_B]

    def sync_symbol_ownership(self) -> None:
        self.refresh_symbol_routing([])

    def refresh_symbol_routing(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> None:
        snapshots = self._prepare_snapshots(snapshots)
        previous_status_by_symbol = {
            item.symbol: item for item in self.state.observed_symbol_status
        }
        previous_local_regimes = {
            item.symbol: item.local_regime for item in self.state.local_regime_by_symbol
        }
        candidate_symbols_by_pod = self._candidate_symbols_by_pod(snapshots)
        current_status_by_symbol = {
            item.symbol: item for item in self.state.observed_symbol_status
        }
        self._log_tradable_pool_changes(
            previous_status_by_symbol=previous_status_by_symbol,
            current_status_by_symbol=current_status_by_symbol,
        )
        for pod_name, pod in self.pods.items():
            pod.desired_symbols = list(candidate_symbols_by_pod.get(pod_name, []))
        previous_owners = {
            item.symbol: item.owner for item in self.registry.snapshot()
        }
        decisions = self.symbol_router.route(
            regime=self.state.regime,
            desired_symbols_by_pod=candidate_symbols_by_pod,
            snapshots=snapshots,
            previous_owners=previous_owners,
        )
        self._apply_symbol_routing(
            decisions,
            previous_owners=previous_owners,
            previous_local_regimes=previous_local_regimes,
        )

    def _candidate_symbols_by_pod(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> dict[PodName, list[str]]:
        status_by_symbol = self._observed_symbol_statuses(snapshots)
        self.state.observed_symbol_status = sorted(
            status_by_symbol.values(),
            key=lambda item: item.symbol,
        )
        leaders = all_cluster_leaders(self.config)
        tradable_symbols = sorted(
            symbol
            for symbol, status in status_by_symbol.items()
            if status.tradable
        )
        if not tradable_symbols:
            return {
                pod_name: []
                for pod_name, pod in self.pods.items()
                if pod.enabled
            }
        return {
            PodName.POD_A: tradable_symbols if self.config.pod_a.enabled else [],
            PodName.POD_B: tradable_symbols if self.config.pod_b.enabled else [],
            PodName.POD_C: (
                [symbol for symbol in tradable_symbols if symbol not in leaders]
                if self.config.pod_c.enabled
                else []
            ),
        }

    def _is_tradable_snapshot(self, snapshot: SymbolMarketSnapshot) -> bool:
        return not self._tradability_reasons(snapshot)

    def _observed_symbol_statuses(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> dict[str, ObservedSymbolStatus]:
        observed_symbols = set(self._observed_symbols())
        statuses: dict[str, ObservedSymbolStatus] = {}
        for snapshot in snapshots:
            symbol = snapshot.symbol.upper()
            if symbol not in observed_symbols:
                continue
            statuses[symbol] = ObservedSymbolStatus(
                symbol=symbol,
                tradable=False,
                reasons=self._tradability_reasons(snapshot),
            )
            statuses[symbol].tradable = not statuses[symbol].reasons
        return statuses

    def _tradability_reasons(self, snapshot: SymbolMarketSnapshot) -> list[str]:
        reasons: list[str] = []
        if snapshot.price <= 0:
            reasons.append("price_non_positive")
        max_spread_bps = max(self.config.hyperliquid.tradable_max_spread_bps, 0.0)
        if snapshot.spread_bps <= 0:
            reasons.append("spread_non_positive")
        elif max_spread_bps > 0 and snapshot.spread_bps > max_spread_bps:
            reasons.append("spread_above_max")

        if self._enforce_live_microstructure_gate(snapshot):
            min_notional_usd = max(
                self.config.hyperliquid.tradable_min_bucket_notional_usd,
                0.0,
            )
            min_trade_count = max(self.config.hyperliquid.tradable_min_bucket_trade_count, 0)
            max_abs_funding = max(self.config.hyperliquid.tradable_max_abs_funding_rate, 0.0)
            bucket_notional_usd = max(snapshot.bucket_volume, 0.0) * max(snapshot.price, 0.0)
            if bucket_notional_usd < min_notional_usd:
                reasons.append("bucket_notional_below_min")
            if snapshot.bucket_trade_count < min_trade_count:
                reasons.append("bucket_trade_count_below_min")
            if max_abs_funding > 0 and abs(snapshot.funding_rate) > max_abs_funding:
                reasons.append("funding_outlier")

        return reasons

    def _enforce_live_microstructure_gate(self, snapshot: SymbolMarketSnapshot) -> bool:
        source = snapshot.source.strip().lower()
        return "live" in source

    def _apply_symbol_routing(
        self,
        decisions: list[SymbolRoutingDecision],
        *,
        previous_owners: dict[str, PodName],
        previous_local_regimes: dict[str, SymbolLocalRegime],
    ) -> None:
        self.registry.clear()
        previous_conflict_count = len(self.state.ownership_conflicts)
        self.state.ownership_conflicts = []
        self.state.symbol_routing = decisions
        for decision in decisions:
            if decision.owner is None:
                if (
                    decision.mode != "allocation_capacity"
                    and len(decision.candidate_pods) > 1
                ):
                    self.state.ownership_conflicts.append(
                        OwnershipConflict(
                            symbol=decision.symbol,
                            requested_by=decision.candidate_pods[0],
                            owner=decision.candidate_pods[1],
                        )
                    )
                continue
            self.registry.claim(decision.symbol, decision.owner)
        self._update_local_regime_state(
            decisions=decisions,
            previous_owners=previous_owners,
            previous_local_regimes=previous_local_regimes,
        )
        self._log_symbol_routing_changes(
            previous_owners=previous_owners,
            decisions=decisions,
            previous_conflict_count=previous_conflict_count,
        )
        self.capital_plan = self._build_capital_plan()
        self.sync_pod_b()

    def apply_regime_snapshot(self, snapshot: RegimeSnapshot) -> RegimeSnapshot:
        previous_regime = self.state.regime
        self.state.regime_snapshot = snapshot
        self.state.regime_evaluation_count += 1
        decision = self.regime_allocator.resolve(
            snapshot=snapshot,
            current_regime=self.state.regime,
            pending_regime=self.state.pending_regime,
            pending_count=self.state.pending_regime_count,
        )
        self.state.raw_regime = decision.raw_regime
        self.state.pending_regime = decision.pending_regime
        self.state.pending_regime_count = decision.pending_count
        new_regime = decision.effective_regime
        if new_regime != previous_regime:
            self.state.regime_transition_count += 1
            self.state.regime_history.append(
                RegimeTransition(
                    recorded_at=datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    previous_regime=previous_regime,
                    new_regime=new_regime,
                    snapshot=snapshot,
                )
            )
            self.state.regime_history = self.state.regime_history[-25:]
        self.state.regime = new_regime
        self.capital_plan = self._build_capital_plan()
        self.sync_pod_b()
        return snapshot

    def preview_pod_a_signals(
        self,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None = None,
    ) -> list[SignalPreview]:
        snapshots = self._prepare_snapshots(snapshots)
        self.refresh_symbol_routing(snapshots)
        contexts = self.pod_a_context_service.build_contexts(
            self.state.regime,
            self._owned_snapshots(PodName.POD_A, snapshots),
            timestamp=timestamp,
        )
        signals = self.pod_a_service.evaluate_many(contexts)
        previews = [
            SignalPreview(
                symbol=signal.symbol,
                side=signal.side,
                setup=signal.setup,
                confidence=signal.confidence,
            )
            for signal in signals
        ]
        self.state.pod_a_signal_preview = previews
        return previews

    def build_pod_a_trade_plans(
        self,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None = None,
    ) -> list[TradePlan]:
        snapshots = self._prepare_snapshots(snapshots)
        self.refresh_symbol_routing(snapshots)
        contexts = self.pod_a_context_service.build_contexts(
            self.state.regime,
            self._owned_snapshots(PodName.POD_A, snapshots),
            timestamp=timestamp,
        )
        signals = self.pod_a_service.evaluate_many(contexts)
        pod_allocation = self._pod_a_planning_allocation(signals)
        plans: list[TradePlan] = []
        for signal in signals:
            plan = self.pod_a_planner.build_trade_plan(signal, pod_allocation)
            if plan is not None:
                plans.append(plan)
        return plans

    def _pod_a_planning_allocation(self, signals: list[object]) -> PodAllocation:
        base = self.capital_plan.pod_allocations[PodName.POD_A]
        if not signals:
            return base

        signal_symbols = list(dict.fromkeys(str(signal.symbol) for signal in signals))
        if self.state.regime.value == "TrendExpansion":
            return base
        base_symbols = [item.symbol for item in base.symbols]
        if base_symbols == signal_symbols:
            return base

        total_equity = max(self.capital_plan.total_equity_usd, 1e-9)
        target_pct = min(
            self.capital_allocator.allocations_for(self.state.regime).get("pod_a", 0.0),
            self.config.pod_a.max_allocation_pct,
        )
        target_usd = round(target_pct * total_equity, 2)
        if target_usd <= 0:
            return base
        max_symbol_usd = (
            self.config.trident.capital.max_allocation_per_symbol_pct * total_equity
        )
        per_symbol_usd = min(target_usd / len(signal_symbols), max_symbol_usd)
        if per_symbol_usd <= 0:
            return base

        symbols = [
            SymbolAllocation(
                symbol=symbol,
                target_pct=round(per_symbol_usd / total_equity, 6),
                target_usd=round(per_symbol_usd, 2),
            )
            for symbol in signal_symbols
        ]
        allocated_usd = round(sum(item.target_usd for item in symbols), 2)
        return PodAllocation(
            pod=base.pod,
            target_pct=round(allocated_usd / total_equity, 6),
            target_usd=allocated_usd,
            capped_by_pod_limit=target_pct < self.capital_allocator.allocations_for(self.state.regime).get("pod_a", 0.0),
            symbols=symbols,
        )

    def preview_pod_c_signals(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SignalPreview]:
        snapshots = self._prepare_snapshots(snapshots)
        self.refresh_symbol_routing(snapshots)
        contexts = self.pod_c_context_service.build_contexts(
            self.state.regime,
            self._pod_c_relevant_snapshots(snapshots),
        )
        signals = self.pod_c_service.evaluate_many(contexts)
        previews = [
            SignalPreview(
                symbol=signal.symbol,
                side=signal.side,
                setup=signal.setup,
                confidence=signal.confidence,
            )
            for signal in signals
        ]
        self.state.pod_c_signal_preview = previews
        return previews

    def build_pod_c_trade_plans(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[TradePlan]:
        snapshots = self._prepare_snapshots(snapshots)
        self.refresh_symbol_routing(snapshots)
        contexts = self.pod_c_context_service.build_contexts(
            self.state.regime,
            self._pod_c_relevant_snapshots(snapshots),
        )
        signals = self.pod_c_service.evaluate_many(contexts)
        pod_allocation = self.capital_plan.pod_allocations[PodName.POD_C]
        plans: list[TradePlan] = []
        for signal in signals:
            plan = self.pod_c_planner.build_trade_plan(signal, pod_allocation)
            if plan is not None:
                plans.append(plan)
        return plans

    def _build_capital_plan(self) -> CapitalPlan:
        return self.capital_allocator.build_plan(
            regime=self.state.regime,
            owned_symbols_by_pod={
                pod_name: self.registry.symbols_for(pod_name) for pod_name in self.pods
            },
        )

    def _owned_snapshots(
        self,
        pod_name: PodName,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SymbolMarketSnapshot]:
        owned_symbols = set(self.registry.symbols_for(pod_name))
        if not owned_symbols:
            return []
        return [snapshot for snapshot in snapshots if snapshot.symbol.upper() in owned_symbols]

    def _pod_c_relevant_snapshots(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SymbolMarketSnapshot]:
        owned_followers = set(self.registry.symbols_for(PodName.POD_C))
        leader_symbols = all_cluster_leaders(self.config)
        allowed_symbols = owned_followers | leader_symbols
        if not allowed_symbols:
            return []
        return [snapshot for snapshot in snapshots if snapshot.symbol.upper() in allowed_symbols]

    def _prepare_snapshots(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SymbolMarketSnapshot]:
        if not snapshots:
            return []
        return enrich_snapshots(self.config, snapshots)

    def sync_pod_b(self) -> None:
        allocation = self.capital_plan.pod_allocations[PodName.POD_B]
        previous_status = (
            dict(self.state.pod_b_status)
            if isinstance(self.state.pod_b_status, dict)
            else {}
        )
        status = self.pod_b_manager.sync(
            allocation=allocation,
            owned_symbols=self.registry.symbols_for(PodName.POD_B),
        )
        self.state.pod_b_status = status.as_dict()
        self._log_pod_b_sync_changes(previous_status=previous_status, current_status=self.state.pod_b_status)

    def refresh_pod_b_status(self) -> None:
        allocation = self.capital_plan.pod_allocations[PodName.POD_B]
        status = self.pod_b_manager.read_status(
            allocation=allocation,
            owned_symbols=self.registry.symbols_for(PodName.POD_B),
        )
        self.state.pod_b_status = status.as_dict()

    def allowed_symbols_for(self, pod_name: PodName) -> set[str]:
        allocation = self.capital_plan.pod_allocations[pod_name]
        return {
            symbol.symbol.upper()
            for symbol in allocation.symbols
            if symbol.target_usd > 0
        }

    def pod_health(self) -> list[PodHealth]:
        return [pod.health() for pod in self.pods.values() if pod.enabled]

    def snapshot(self) -> dict[str, object]:
        self.refresh_pod_b_status()
        return {
            "profile": self.profile,
            "mode": self.mode,
            "started_at": self.state.started_at.isoformat().replace("+00:00", "Z"),
            "regime": self.state.regime.value,
            "raw_regime": self.state.raw_regime.value,
            "observation_universe": self._observed_symbols(),
            "tradable_pool": [
                item.symbol
                for item in self.state.observed_symbol_status
                if item.tradable
            ],
            "observed_symbol_status": [
                {
                    "symbol": item.symbol,
                    "tradable": item.tradable,
                    "reasons": list(item.reasons),
                }
                for item in self.state.observed_symbol_status
            ],
            "kill_switch": {
                "active": self.kill_switch.is_active,
                "reason": self.kill_switch.active_reason,
            },
            "enabled_pods": [pod.value for pod in self.state.enabled_pods],
            "pod_health": [
                {
                    "pod": health.pod.value,
                    "healthy": health.healthy,
                    "message": health.message,
                }
                for health in self.pod_health()
            ],
            "symbol_ownership": [
                {
                    "symbol": item.symbol,
                    "owner": item.owner.value if item.owner else None,
                    "routing_mode": next(
                        (
                            decision.mode
                            for decision in self.state.symbol_routing
                            if decision.symbol == item.symbol
                        ),
                        None,
                    ),
                    "routing_reason": next(
                        (
                            decision.reason
                            for decision in self.state.symbol_routing
                            if decision.symbol == item.symbol
                        ),
                        None,
                    ),
                }
                for item in self.registry.snapshot()
            ],
            "ownership_conflicts": [
                {
                    "symbol": conflict.symbol,
                    "requested_by": conflict.requested_by.value,
                    "owner": conflict.owner.value,
                }
                for conflict in self.state.ownership_conflicts
            ],
            "pods": {
                pod_name.value: {
                    "enabled": pod.enabled,
                    "candidate_symbols": pod.desired_symbols,
                    "desired_symbols": pod.desired_symbols,
                    "owned_symbols": self.registry.symbols_for(pod_name),
                    "target_pct": self.capital_plan.pod_allocations[pod_name].target_pct,
                    "target_usd": self.capital_plan.pod_allocations[pod_name].target_usd,
                    "capped_by_pod_limit": self.capital_plan.pod_allocations[
                        pod_name
                    ].capped_by_pod_limit,
                }
                for pod_name, pod in self.pods.items()
            },
            "allocations": self.capital_allocator.allocations_for(self.state.regime),
            "capital_plan": {
                "regime": self.capital_plan.regime.value,
                "total_equity_usd": self.capital_plan.total_equity_usd,
                "cash_pct": self.capital_plan.cash_pct,
                "cash_usd": self.capital_plan.cash_usd,
                "pods": {
                    pod.value: {
                        "target_pct": allocation.target_pct,
                        "target_usd": allocation.target_usd,
                        "capped_by_pod_limit": allocation.capped_by_pod_limit,
                        "symbols": [
                            {
                                "symbol": symbol.symbol,
                                "target_pct": symbol.target_pct,
                                "target_usd": symbol.target_usd,
                            }
                            for symbol in allocation.symbols
                        ],
                    }
                    for pod, allocation in self.capital_plan.pod_allocations.items()
                },
            },
            "regime_snapshot": {
                "ready": self.state.regime_snapshot.ready,
                "adx": self.state.regime_snapshot.adx,
                "atr_ratio": self.state.regime_snapshot.atr_ratio,
                "range_width_bps": self.state.regime_snapshot.range_width_bps,
                "structure_score": self.state.regime_snapshot.structure_score,
                "btc_impulse": self.state.regime_snapshot.btc_impulse,
            },
            "pending_regime": (
                self.state.pending_regime.value if self.state.pending_regime is not None else None
            ),
            "pending_regime_count": self.state.pending_regime_count,
            "regime_transition_count": self.state.regime_transition_count,
            "regime_evaluation_count": self.state.regime_evaluation_count,
            "regime_history": [
                {
                    "recorded_at": transition.recorded_at,
                    "previous_regime": transition.previous_regime.value,
                    "new_regime": transition.new_regime.value,
                    "snapshot": {
                        "ready": transition.snapshot.ready,
                        "adx": transition.snapshot.adx,
                        "atr_ratio": transition.snapshot.atr_ratio,
                        "range_width_bps": transition.snapshot.range_width_bps,
                        "structure_score": transition.snapshot.structure_score,
                        "btc_impulse": transition.snapshot.btc_impulse,
                    },
                }
                for transition in self.state.regime_history
            ],
            "local_regime_by_symbol": [
                {
                    "symbol": item.symbol,
                    "local_regime": item.local_regime.value,
                    "reason": item.reason,
                    "owner": item.owner.value if item.owner is not None else None,
                    "previous_owner": (
                        item.previous_owner.value if item.previous_owner is not None else None
                    ),
                    "global_alignment": item.global_alignment,
                    "pod_scores": {
                        pod.value: score for pod, score in item.pod_scores.items()
                    },
                    "reassignment_count": self.state.symbol_reassignment_count_by_symbol.get(
                        item.symbol,
                        0,
                    ),
                }
                for item in self.state.local_regime_by_symbol
            ],
            "local_regime_transitions": [
                {
                    "recorded_at": transition.recorded_at,
                    "symbol": transition.symbol,
                    "previous_local_regime": (
                        transition.previous_local_regime.value
                        if transition.previous_local_regime is not None
                        else None
                    ),
                    "new_local_regime": transition.new_local_regime.value,
                    "reason": transition.reason,
                }
                for transition in self.state.local_regime_transitions
            ],
            "symbol_reassignment_count_by_symbol": dict(
                self.state.symbol_reassignment_count_by_symbol
            ),
            "pod_a_signal_preview": [
                {
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "setup": signal.setup,
                    "confidence": signal.confidence,
                }
                for signal in self.state.pod_a_signal_preview
            ],
            "pod_c_signal_preview": [
                {
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "setup": signal.setup,
                    "confidence": signal.confidence,
                }
                for signal in self.state.pod_c_signal_preview
            ],
            "symbol_routing": [
                {
                    "symbol": decision.symbol,
                    "owner": decision.owner.value if decision.owner is not None else None,
                    "previous_owner": (
                        decision.previous_owner.value
                        if decision.previous_owner is not None
                        else None
                    ),
                    "mode": decision.mode,
                    "reason": decision.reason,
                    "candidate_pods": [pod.value for pod in decision.candidate_pods],
                    "pod_scores": {
                        pod.value: score for pod, score in decision.pod_scores.items()
                    },
                    "local_regime": (
                        decision.local_regime.value
                        if decision.local_regime is not None
                        else None
                    ),
                    "local_regime_reason": decision.local_regime_reason,
                }
                for decision in self.state.symbol_routing
            ],
            "pod_b_status": self.state.pod_b_status,
        }

    def _update_local_regime_state(
        self,
        *,
        decisions: list[SymbolRoutingDecision],
        previous_owners: dict[str, PodName],
        previous_local_regimes: dict[str, SymbolLocalRegime],
    ) -> None:
        local_states: list[LocalSymbolState] = []
        transitions: list[LocalSymbolTransition] = []
        for decision in sorted(decisions, key=lambda item: item.symbol):
            if decision.local_regime is None:
                continue
            previous_local_regime = previous_local_regimes.get(decision.symbol)
            if previous_local_regime != decision.local_regime:
                transitions.append(
                    LocalSymbolTransition(
                        recorded_at=datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        symbol=decision.symbol,
                        previous_local_regime=previous_local_regime,
                        new_local_regime=decision.local_regime,
                        reason=decision.local_regime_reason,
                    )
                )
            previous_owner = previous_owners.get(decision.symbol)
            if previous_owner != decision.owner:
                self.state.symbol_reassignment_count_by_symbol[decision.symbol] = (
                    self.state.symbol_reassignment_count_by_symbol.get(decision.symbol, 0)
                    + 1
                )
            local_states.append(
                LocalSymbolState(
                    symbol=decision.symbol,
                    local_regime=decision.local_regime,
                    reason=decision.local_regime_reason,
                    owner=decision.owner,
                    previous_owner=decision.previous_owner,
                    global_alignment=self._local_global_alignment(
                        self.state.regime,
                        decision.local_regime,
                    ),
                    pod_scores=dict(decision.pod_scores),
                )
            )
        self.state.local_regime_by_symbol = local_states
        self.state.local_regime_transitions.extend(transitions)
        self.state.local_regime_transitions = self.state.local_regime_transitions[-100:]

    def _local_global_alignment(
        self,
        global_regime: Regime,
        local_regime: SymbolLocalRegime,
    ) -> str:
        aligned = {
            Regime.TREND_EXPANSION: {
                SymbolLocalRegime.TREND_STRUCTURE,
                SymbolLocalRegime.EVENT_IMPULSE,
            },
            Regime.RANGE_AUCTION: {SymbolLocalRegime.RANGE_STRUCTURE},
            Regime.PANIC_SQUEEZE: {
                SymbolLocalRegime.EVENT_IMPULSE,
                SymbolLocalRegime.TREND_STRUCTURE,
            },
            Regime.DEAD_ZONE: {
                SymbolLocalRegime.RANGE_STRUCTURE,
                SymbolLocalRegime.NEUTRAL,
            },
            Regime.CASH: {SymbolLocalRegime.NEUTRAL},
        }
        return "aligned" if local_regime in aligned[global_regime] else "divergent"

    def _log_tradable_pool_changes(
        self,
        *,
        previous_status_by_symbol: dict[str, ObservedSymbolStatus],
        current_status_by_symbol: dict[str, ObservedSymbolStatus],
    ) -> None:
        previous_tradable = {
            symbol
            for symbol, status in previous_status_by_symbol.items()
            if status.tradable
        }
        current_tradable = {
            symbol
            for symbol, status in current_status_by_symbol.items()
            if status.tradable
        }
        added = sorted(current_tradable - previous_tradable)
        removed = sorted(previous_tradable - current_tradable)
        reason_changes: list[str] = []
        for symbol in sorted(set(previous_status_by_symbol) & set(current_status_by_symbol)):
            previous = previous_status_by_symbol[symbol]
            current = current_status_by_symbol[symbol]
            if previous.tradable == current.tradable and previous.reasons == current.reasons:
                continue
            if symbol in added or symbol in removed:
                continue
            if current.reasons != previous.reasons:
                reason_changes.append(
                    f"{symbol}:{previous.reasons}->{current.reasons}"
                )
        if not added and not removed and not reason_changes:
            return
        logger.info(
            "Supervisor tradable pool changed; regime=%s added=%s removed=%s reason_changes=%s current_tradable=%s",
            self.state.regime.value,
            added,
            removed,
            reason_changes,
            sorted(current_tradable),
        )

    def _log_symbol_routing_changes(
        self,
        *,
        previous_owners: dict[str, PodName],
        decisions: list[SymbolRoutingDecision],
        previous_conflict_count: int,
    ) -> None:
        changes: list[str] = []
        current_owners = {
            decision.symbol: decision.owner
            for decision in decisions
        }
        symbols = sorted(set(previous_owners) | set(current_owners))
        for symbol in symbols:
            previous_owner = previous_owners.get(symbol)
            current_owner = current_owners.get(symbol)
            if previous_owner == current_owner:
                continue
            decision = next((item for item in decisions if item.symbol == symbol), None)
            changes.append(
                f"{symbol}:{previous_owner.value if previous_owner else 'none'}->{current_owner.value if current_owner else 'none'}"
                f" mode={decision.mode if decision else 'unknown'}"
                f" reason={decision.reason if decision else 'unknown'}"
            )
        conflict_count = len(self.state.ownership_conflicts)
        if not changes and conflict_count == previous_conflict_count:
            return
        logger.info(
            "Supervisor symbol routing changed; regime=%s changes=%s conflicts=%s",
            self.state.regime.value,
            changes,
            conflict_count,
        )

    def _log_pod_b_sync_changes(
        self,
        *,
        previous_status: dict[str, object],
        current_status: dict[str, object],
    ) -> None:
        previous_symbols = [str(symbol) for symbol in previous_status.get("managed_symbols", [])]
        current_symbols = [str(symbol) for symbol in current_status.get("managed_symbols", [])]
        previous_target = float(previous_status.get("target_usd", 0.0) or 0.0)
        current_target = float(current_status.get("target_usd", 0.0) or 0.0)
        previous_state = str(previous_status.get("process_state", ""))
        current_state = str(current_status.get("process_state", ""))
        previous_reason = str(previous_status.get("last_sync_reason", ""))
        current_reason = str(current_status.get("last_sync_reason", ""))
        if (
            previous_symbols == current_symbols
            and previous_target == current_target
            and previous_state == current_state
            and previous_reason == current_reason
        ):
            return
        logger.info(
            "Supervisor Pod B sync changed; regime=%s managed_symbols=%s target_usd=%.2f process_state=%s reason=%s previous_symbols=%s previous_target_usd=%.2f previous_process_state=%s previous_reason=%s",
            self.state.regime.value,
            current_symbols,
            current_target,
            current_state,
            current_reason,
            previous_symbols,
            previous_target,
            previous_state,
            previous_reason,
        )
