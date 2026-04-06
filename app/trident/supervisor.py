from __future__ import annotations

from datetime import datetime, timezone

from app.settings import AppConfig
from app.trident.capital_allocator import CapitalAllocator
from app.trident.kill_switch import KillSwitch
from app.trident.pod_a import AnchorTrendPlanner, AnchorTrendService, MarketContextService
from app.trident.pod_b import PassivbotManager
from app.trident.pod_c import EventContextService, EventRaiderPlanner, EventRaiderService
from app.trident.pod_runtime import ConfiguredPod
from app.trident.regime_allocator import RegimeAllocator
from app.trident.symbol_registry import SymbolRegistry
from app.trident.types import (
    CapitalPlan,
    OwnershipConflict,
    PodHealth,
    PodName,
    PodAllocation,
    RegimeSnapshot,
    RegimeTransition,
    SignalPreview,
    SymbolAllocation,
    SymbolMarketSnapshot,
    SupervisorState,
    TradePlan,
)


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
        self.pod_a_context_service = MarketContextService()
        self.pod_a_service = AnchorTrendService()
        self.pod_a_planner = AnchorTrendPlanner(config)
        self.pod_c_context_service = EventContextService(config.pod_c)
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
                desired_symbols=[symbol.upper() for symbol in self.config.pod_a.symbols],
            ),
            PodName.POD_B: ConfiguredPod(
                name=PodName.POD_B,
                enabled=self.config.pod_b.enabled,
                desired_symbols=[symbol.upper() for symbol in self.config.pod_b.symbols],
            ),
            PodName.POD_C: ConfiguredPod(
                name=PodName.POD_C,
                enabled=self.config.pod_c.enabled,
                desired_symbols=[
                    symbol.upper() for symbol in self.config.pod_c.follower_symbols
                ],
            ),
        }

    def _pod_priority(self) -> list[PodName]:
        return [PodName.POD_C, PodName.POD_A, PodName.POD_B]

    def sync_symbol_ownership(self) -> None:
        self.registry.clear()
        self.state.ownership_conflicts = []
        for pod_name in self._pod_priority():
            pod = self.pods[pod_name]
            if not pod.enabled:
                continue
            for symbol in pod.desired_symbols:
                if self.registry.claim(symbol, pod_name):
                    continue
                owner = self.registry.owner_of(symbol)
                if owner is None:
                    continue
                self.state.ownership_conflicts.append(
                    OwnershipConflict(
                        symbol=symbol,
                        requested_by=pod_name,
                        owner=owner,
                    )
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
        leader_symbols = {symbol.upper() for symbol in self.config.pod_c.leader_symbols}
        allowed_symbols = owned_followers | leader_symbols
        if not allowed_symbols:
            return []
        return [snapshot for snapshot in snapshots if snapshot.symbol.upper() in allowed_symbols]

    def sync_pod_b(self) -> None:
        allocation = self.capital_plan.pod_allocations[PodName.POD_B]
        status = self.pod_b_manager.sync(
            allocation=allocation,
            owned_symbols=self.registry.symbols_for(PodName.POD_B),
        )
        self.state.pod_b_status = status.as_dict()

    def pod_health(self) -> list[PodHealth]:
        return [pod.health() for pod in self.pods.values() if pod.enabled]

    def snapshot(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "mode": self.mode,
            "regime": self.state.regime.value,
            "raw_regime": self.state.raw_regime.value,
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
            "pod_b_status": self.state.pod_b_status,
        }
