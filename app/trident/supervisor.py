from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from app.live.runtime_status import (
    load_runtime_status,
    runtime_status_is_fresh,
    sanitize_runtime_status_payload,
)
from app.settings import AppConfig
from app.trident.capital_allocator import CapitalAllocator
from app.trident.kill_switch import KillSwitch
from app.trident.market_clusters import (
    cluster_for_symbol,
    enrich_snapshots,
    normalize_cluster_names,
    observation_universe_symbols,
)
from app.trident.pod_a import AnchorTrendPlanner, AnchorTrendService, MarketContextService
from app.trident.pod_b import BreakoutContext, BreakoutPlanner, BreakoutService
from app.trident.pod_c import TradfiTrendContextService, TradfiTrendPlanner, TradfiTrendService
from app.trident.routing_overrides import (
    load_runtime_symbol_pod_override_payload,
    write_runtime_symbol_pod_override_payload,
)
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
    Regime,
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
        self._compact_backtest_logs = self._should_compact_backtest_logs()
        self._compact_log_flush_every = 250
        self._compact_tradable_pool_log = self._new_compact_tradable_pool_log()
        self._compact_routing_log = self._new_compact_routing_log()
        self._compact_pod_b_sync_log = self._new_compact_pod_b_sync_log()
        self.registry = SymbolRegistry()
        self.kill_switch = KillSwitch()
        self.regime_allocator = RegimeAllocator(config)
        self.capital_allocator = CapitalAllocator(config)
        self.symbol_router = SymbolRouter(config)
        self.pod_a_context_service = MarketContextService(config)
        self.pod_a_service = AnchorTrendService()
        self.pod_a_planner = AnchorTrendPlanner(config)
        self.pod_b_service = BreakoutService(config)
        self.pod_b_planner = BreakoutPlanner(config)
        self.pod_c_service = TradfiTrendService(config.pod_c)
        self.pod_c_context_service = TradfiTrendContextService(config, self.pod_c_service)
        self.pod_c_planner = TradfiTrendPlanner(config)
        self._latest_snapshots: list[SymbolMarketSnapshot] = []
        self.state = SupervisorState(
            regime=self.regime_allocator.current_regime(),
            mode=mode,
            profile=profile,
            enabled_pods=self._enabled_pods(),
        )
        self.pods = self._build_pods()
        self.reload_runtime_routing_overrides()
        self.sync_symbol_ownership()
        self.capital_plan = self._build_capital_plan()
        self.sync_pod_b()

    def _should_compact_backtest_logs(self) -> bool:
        profile = self.profile.strip().lower()
        return any(
            token in profile
            for token in ("backtest", "replay", "cohabitation")
        )

    def _new_compact_tradable_pool_log(self) -> dict[str, object]:
        return {
            "event_count": 0,
            "added_total": 0,
            "removed_total": 0,
            "reason_change_total": 0,
            "current_tradable_total": 0,
            "peak_tradable_pool": 0,
            "last_regime": "",
            "last_added": [],
            "last_removed": [],
            "last_reason_changes": [],
        }

    def _new_compact_routing_log(self) -> dict[str, object]:
        return {
            "event_count": 0,
            "symbol_change_total": 0,
            "assignment_total": 0,
            "deassignment_total": 0,
            "reassignment_total": 0,
            "pod_b_to_none_total": 0,
            "capacity_trim_total": 0,
            "unknown_reason_total": 0,
            "conflict_peak": 0,
            "last_regime": "",
            "last_changes": [],
        }

    def _new_compact_pod_b_sync_log(self) -> dict[str, object]:
        return {
            "event_count": 0,
            "managed_symbol_total": 0,
            "peak_managed_symbols": 0,
            "target_change_count": 0,
            "process_state_change_count": 0,
            "reason_change_count": 0,
            "last_regime": "",
            "last_process_state": "",
            "last_reason": "",
            "last_target_usd": 0.0,
            "last_symbols": [],
        }

    def flush_compact_logs(self) -> None:
        self._flush_compact_tradable_pool_log(force=True)
        self._flush_compact_routing_log(force=True)
        self._flush_compact_pod_b_sync_log(force=True)

    def _flush_compact_tradable_pool_log(self, *, force: bool = False) -> None:
        summary = self._compact_tradable_pool_log
        event_count = int(summary["event_count"])
        if event_count == 0:
            return
        if not force and event_count < self._compact_log_flush_every:
            return
        average_tradable_pool = float(summary["current_tradable_total"]) / event_count
        logger.info(
            "Supervisor tradable pool summary; profile=%s regime=%s events=%s added_total=%s removed_total=%s reason_change_total=%s avg_tradable_pool=%.2f peak_tradable_pool=%s last_added=%s last_removed=%s last_reason_changes=%s",
            self.profile,
            summary["last_regime"],
            event_count,
            summary["added_total"],
            summary["removed_total"],
            summary["reason_change_total"],
            average_tradable_pool,
            summary["peak_tradable_pool"],
            summary["last_added"],
            summary["last_removed"],
            summary["last_reason_changes"],
        )
        self._compact_tradable_pool_log = self._new_compact_tradable_pool_log()

    def _flush_compact_routing_log(self, *, force: bool = False) -> None:
        summary = self._compact_routing_log
        event_count = int(summary["event_count"])
        if event_count == 0:
            return
        if not force and event_count < self._compact_log_flush_every:
            return
        logger.info(
            "Supervisor routing summary; profile=%s regime=%s events=%s symbol_changes=%s assignments=%s deassignments=%s reassignments=%s pod_b_to_none=%s capacity_trim=%s unknown_reason=%s conflict_peak=%s last_changes=%s",
            self.profile,
            summary["last_regime"],
            event_count,
            summary["symbol_change_total"],
            summary["assignment_total"],
            summary["deassignment_total"],
            summary["reassignment_total"],
            summary["pod_b_to_none_total"],
            summary["capacity_trim_total"],
            summary["unknown_reason_total"],
            summary["conflict_peak"],
            summary["last_changes"],
        )
        self._compact_routing_log = self._new_compact_routing_log()

    def _flush_compact_pod_b_sync_log(self, *, force: bool = False) -> None:
        summary = self._compact_pod_b_sync_log
        event_count = int(summary["event_count"])
        if event_count == 0:
            return
        if not force and event_count < self._compact_log_flush_every:
            return
        average_managed_symbols = float(summary["managed_symbol_total"]) / event_count
        logger.info(
            "Supervisor Pod B sync summary; profile=%s regime=%s events=%s avg_managed_symbols=%.2f peak_managed_symbols=%s target_change_count=%s process_state_change_count=%s reason_change_count=%s last_process_state=%s last_reason=%s last_target_usd=%.2f last_symbols=%s",
            self.profile,
            summary["last_regime"],
            event_count,
            average_managed_symbols,
            summary["peak_managed_symbols"],
            summary["target_change_count"],
            summary["process_state_change_count"],
            summary["reason_change_count"],
            summary["last_process_state"],
            summary["last_reason"],
            float(summary["last_target_usd"]),
            summary["last_symbols"],
        )
        self._compact_pod_b_sync_log = self._new_compact_pod_b_sync_log()

    def _observed_symbols(self) -> list[str]:
        return observation_universe_symbols(self.config)

    def _symbol_matches_pod_clusters(
        self,
        snapshot: SymbolMarketSnapshot | None,
        allowed_clusters: list[str],
    ) -> bool:
        if snapshot is None:
            return False
        cluster_scope = normalize_cluster_names(allowed_clusters)
        if not cluster_scope:
            return False
        return str(snapshot.market_cluster).strip().lower() in cluster_scope

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
        self._latest_snapshots = list(snapshots)
        self.reload_runtime_routing_overrides()
        previous_status_by_symbol = {
            item.symbol: item for item in self.state.observed_symbol_status
        }
        previous_local_regimes = {
            item.symbol: item.local_regime for item in self.state.local_regime_by_symbol
        }
        reassignment_age_seconds_by_symbol = self._reassignment_age_seconds_by_symbol()
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
            reassignment_age_seconds_by_symbol=reassignment_age_seconds_by_symbol,
            cluster_regimes=self.state.cluster_regimes or None,
        )
        self._apply_symbol_routing(
            decisions,
            previous_owners=previous_owners,
            previous_local_regimes=previous_local_regimes,
        )

    def runtime_routing_override_path(self) -> Path:
        return Path(self.config.trident.routing.runtime_override_path)

    def effective_symbol_pod_overrides(self) -> dict[str, str]:
        merged = dict(self.config.trident.routing.symbol_pod_overrides)
        merged.update(self.state.runtime_symbol_pod_overrides)
        return {
            str(symbol).strip().upper(): str(owner).strip().lower()
            for symbol, owner in merged.items()
            if str(symbol).strip() and str(owner).strip()
        }

    def reload_runtime_routing_overrides(self) -> None:
        previous_runtime = dict(self.state.runtime_symbol_pod_overrides)
        previous_updated_at = self.state.runtime_symbol_pod_overrides_updated_at
        payload = load_runtime_symbol_pod_override_payload(
            self.runtime_routing_override_path()
        )
        runtime_overrides = payload.get("symbol_pod_overrides", {})
        updated_at = payload.get("updated_at")
        self.state.runtime_symbol_pod_overrides = (
            dict(runtime_overrides) if isinstance(runtime_overrides, dict) else {}
        )
        self.state.runtime_symbol_pod_overrides_updated_at = (
            str(updated_at) if isinstance(updated_at, str) else None
        )
        self.symbol_router.set_symbol_pod_overrides(self.effective_symbol_pod_overrides())
        if (
            previous_runtime == self.state.runtime_symbol_pod_overrides
            and previous_updated_at == self.state.runtime_symbol_pod_overrides_updated_at
        ):
            return
        logger.info(
            "Supervisor runtime routing overrides reloaded; runtime_overrides=%s updated_at=%s effective_overrides=%s path=%s",
            self.state.runtime_symbol_pod_overrides,
            self.state.runtime_symbol_pod_overrides_updated_at,
            self.effective_symbol_pod_overrides(),
            self.runtime_routing_override_path(),
        )

    def set_runtime_symbol_override(self, symbol: str, owner: PodName) -> None:
        runtime_overrides = dict(self.state.runtime_symbol_pod_overrides)
        runtime_overrides[str(symbol).strip().upper()] = owner.value
        write_runtime_symbol_pod_override_payload(
            self.runtime_routing_override_path(),
            runtime_overrides,
        )
        self.reload_runtime_routing_overrides()
        self._reroute_with_cached_snapshots()

    def clear_runtime_symbol_override(self, symbol: str) -> None:
        runtime_overrides = dict(self.state.runtime_symbol_pod_overrides)
        runtime_overrides.pop(str(symbol).strip().upper(), None)
        write_runtime_symbol_pod_override_payload(
            self.runtime_routing_override_path(),
            runtime_overrides,
        )
        self.reload_runtime_routing_overrides()
        self._reroute_with_cached_snapshots()

    def _reroute_with_cached_snapshots(self) -> None:
        if self._latest_snapshots:
            self.refresh_symbol_routing(self._latest_snapshots)
            return
        self.sync_symbol_ownership()

    def _candidate_symbols_by_pod(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> dict[PodName, list[str]]:
        status_by_symbol = self._observed_symbol_statuses(snapshots)
        self.state.observed_symbol_status = sorted(
            status_by_symbol.values(),
            key=lambda item: item.symbol,
        )
        tradable_symbols = sorted(
            symbol
            for symbol, status in status_by_symbol.items()
            if status.tradable
        )
        snapshot_by_symbol = {snapshot.symbol.upper(): snapshot for snapshot in snapshots}
        if not tradable_symbols:
            return {
                pod_name: []
                for pod_name, pod in self.pods.items()
                if pod.enabled
            }
        candidates = {
            PodName.POD_A: (
                [
                    symbol
                    for symbol in tradable_symbols
                    if self._symbol_matches_pod_clusters(
                        snapshot_by_symbol.get(symbol),
                        self.config.pod_a.allowed_market_clusters,
                    )
                ]
                if self.config.pod_a.enabled
                else []
            ),
            PodName.POD_B: (
                [
                    symbol
                    for symbol in tradable_symbols
                    if self._symbol_matches_pod_clusters(
                        snapshot_by_symbol.get(symbol),
                        self.config.pod_b.allowed_market_clusters,
                    )
                ]
                if self.config.pod_b.enabled
                else []
            ),
            PodName.POD_C: (
                [
                    symbol
                    for symbol in tradable_symbols
                    if (
                        snapshot_by_symbol.get(symbol) is not None
                        and self.pod_c_service.is_eligible_symbol(
                            symbol,
                            snapshot_by_symbol[symbol].market_cluster,
                        )
                        and self._pod_c_cluster_budget_pct(snapshot_by_symbol[symbol]) > 0
                    )
                ]
                if self.config.pod_c.enabled
                else []
            ),
        }
        override_symbols = self._routing_override_symbols_by_pod(tradable_symbols)
        for pod_name, symbols in override_symbols.items():
            merged = {item.upper() for item in candidates.get(pod_name, [])}
            for symbol in symbols:
                if symbol in merged:
                    continue
                candidates.setdefault(pod_name, []).append(symbol)
                merged.add(symbol)
        return {
            pod_name: sorted(symbols)
            for pod_name, symbols in candidates.items()
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

    def apply_regime_snapshot(
        self,
        snapshot: RegimeSnapshot,
        cluster_regime_snapshots: dict[str, RegimeSnapshot] | None = None,
    ) -> RegimeSnapshot:
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
        self._apply_cluster_regime_snapshots(cluster_regime_snapshots or {})
        self.capital_plan = self._build_capital_plan()
        self.sync_pod_b()
        return snapshot

    def _apply_cluster_regime_snapshots(
        self,
        cluster_snapshots: dict[str, RegimeSnapshot],
    ) -> None:
        for cluster, snap in cluster_snapshots.items():
            self.state.cluster_regime_snapshots[cluster] = snap
            current = self.state.cluster_regimes.get(cluster, Regime.CASH)
            decision = self.regime_allocator.resolve_cluster(
                snapshot=snap,
                current_regime=current,
                pending_regime=self.state.cluster_pending_regimes.get(cluster),
                pending_count=self.state.cluster_pending_counts.get(cluster, 0),
            )
            self.state.cluster_pending_regimes[cluster] = decision.pending_regime
            self.state.cluster_pending_counts[cluster] = decision.pending_count
            self.state.cluster_regimes[cluster] = decision.effective_regime

    @property
    def tradfi_summary_regime(self) -> Regime:
        non_crypto = {
            cluster: regime
            for cluster, regime in self.state.cluster_regimes.items()
            if cluster != "crypto"
        }
        if not non_crypto:
            return Regime.CASH
        regime_counts: dict[Regime, int] = {}
        for regime in non_crypto.values():
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
        # Conservative tie-break: most cautious regime wins
        _conservatism = {
            Regime.DEAD_ZONE: 0,
            Regime.PANIC_SQUEEZE: 1,
            Regime.RANGE_AUCTION: 2,
            Regime.CASH: 3,
            Regime.TREND_EXPANSION: 4,
        }
        return max(
            regime_counts,
            key=lambda r: (regime_counts[r], -_conservatism.get(r, 99)),
        )

    @property
    def tradfi_regime(self) -> Regime:
        return self.tradfi_summary_regime

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

    def preview_pod_b_signals(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SignalPreview]:
        snapshots = self._prepare_snapshots(snapshots)
        self.refresh_symbol_routing(snapshots)
        signals = self.pod_b_service.evaluate_many(self._pod_b_contexts(snapshots))
        previews = [
            SignalPreview(
                symbol=signal.symbol,
                side=signal.side,
                setup=signal.setup,
                confidence=signal.confidence,
            )
            for signal in signals
        ]
        self.state.pod_b_signal_preview = previews
        return previews

    def build_pod_b_trade_plans(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[TradePlan]:
        snapshots = self._prepare_snapshots(snapshots)
        self.refresh_symbol_routing(snapshots)
        signals = self.pod_b_service.evaluate_many(self._pod_b_contexts(snapshots))
        pod_allocation = self._pod_b_planning_allocation(signals)
        plans: list[TradePlan] = []
        for signal in signals:
            plan = self.pod_b_planner.build_trade_plan(signal, pod_allocation)
            if plan is not None:
                plans.append(plan)
        return plans

    def _pod_b_contexts(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[BreakoutContext]:
        opening_symbols = self.opening_symbols_for(PodName.POD_B)
        return [
            BreakoutContext(
                symbol=snapshot.symbol,
                regime=self.state.regime.value,
                price=snapshot.price,
                ema_fast=snapshot.ema_fast,
                ema_slow=snapshot.ema_slow,
                vwap_distance_bps=snapshot.vwap_distance_bps,
                structure_score=snapshot.structure_score,
                funding_rate=snapshot.funding_rate,
                spread_bps=snapshot.spread_bps,
                btc_aligned=snapshot.btc_aligned,
                market_cluster=snapshot.market_cluster,
                cluster_leader=snapshot.cluster_leader,
                book_imbalance=snapshot.book_imbalance,
                trade_flow_bias=snapshot.trade_flow_bias,
                bucket_trade_count=snapshot.bucket_trade_count,
                bucket_notional_usd=(
                    snapshot.bucket_notional_usd
                    if snapshot.bucket_notional_usd > 0
                    else snapshot.bucket_volume * snapshot.price
                ),
                bucket_range_bps=snapshot.bucket_range_bps,
                delta_book_imbalance=snapshot.delta_book_imbalance,
                delta_trade_flow_bias=snapshot.delta_trade_flow_bias,
                volume_ratio=snapshot.volume_ratio,
                trade_count_ratio=snapshot.trade_count_ratio,
                realized_vol_short_bps=snapshot.realized_vol_short_bps,
                realized_vol_long_bps=snapshot.realized_vol_long_bps,
                compression_score=snapshot.compression_score,
                microprice_dislocation_bps=snapshot.microprice_dislocation_bps,
            )
            for snapshot in snapshots
            if snapshot.symbol in opening_symbols
        ]

    def _pod_b_planning_allocation(self, signals: list[object]) -> PodAllocation:
        base = self.capital_plan.pod_allocations[PodName.POD_B]
        if not signals:
            return base
        signal_symbols = list(dict.fromkeys(str(signal.symbol) for signal in signals))
        total_equity = max(self.capital_plan.total_equity_usd, 1e-9)
        target_usd = min(base.target_usd, base.target_pct * total_equity)
        if target_usd <= 0:
            return base
        per_symbol_usd = min(
            target_usd / len(signal_symbols),
            self.config.trident.capital.max_allocation_per_symbol_pct * total_equity,
        )
        if per_symbol_usd <= 0:
            return base
        allocated_usd = round(per_symbol_usd * len(signal_symbols), 2)
        return PodAllocation(
            pod=base.pod,
            target_pct=round(allocated_usd / total_equity, 6),
            target_usd=allocated_usd,
            capped_by_pod_limit=base.capped_by_pod_limit,
            symbols=[
                SymbolAllocation(
                    symbol=symbol,
                    target_pct=round(per_symbol_usd / total_equity, 6),
                    target_usd=round(per_symbol_usd, 2),
                )
                for symbol in signal_symbols
            ],
        )

    def preview_pod_c_signals(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SignalPreview]:
        snapshots = self._prepare_snapshots(snapshots)
        self.refresh_symbol_routing(snapshots)
        owned_symbols = set(self.registry.symbols_for(PodName.POD_C))
        contexts = self.pod_c_context_service.build_contexts(
            self.state.regime,
            snapshots,
            owned_symbols=owned_symbols,
            cluster_regimes=self.state.cluster_regimes or None,
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
        owned_symbols = set(self.registry.symbols_for(PodName.POD_C))
        contexts = self.pod_c_context_service.build_contexts(
            self.state.regime,
            snapshots,
            owned_symbols=owned_symbols,
            cluster_regimes=self.state.cluster_regimes or None,
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
        symbol_clusters_by_pod = {
            pod_name: {
                symbol: self._cluster_for_owned_symbol(symbol)
                for symbol in self.registry.symbols_for(pod_name)
            }
            for pod_name in self.pods
        }
        return self.capital_allocator.build_plan(
            regime=self.state.regime,
            owned_symbols_by_pod={
                pod_name: self.registry.symbols_for(pod_name) for pod_name in self.pods
            },
            cluster_regimes=self.state.cluster_regimes or None,
            symbol_clusters_by_pod=symbol_clusters_by_pod,
        )

    def cluster_target_allocations(self) -> dict[str, float]:
        return self.capital_allocator.cluster_target_pcts(
            self.state.regime,
            self.state.cluster_regimes or None,
        )

    def _cluster_for_owned_symbol(self, symbol: str) -> str:
        normalized = str(symbol).strip().upper()
        snapshot = next(
            (item for item in self._latest_snapshots if item.symbol.upper() == normalized),
            None,
        )
        if snapshot is not None and snapshot.market_cluster:
            return str(snapshot.market_cluster).strip().lower()
        return cluster_for_symbol(self.config, normalized)

    def _pod_c_cluster_budget_pct(self, snapshot: SymbolMarketSnapshot) -> float:
        cluster = str(snapshot.market_cluster).strip().lower()
        if not cluster or cluster == "crypto":
            return 0.0
        return self.cluster_target_allocations().get(cluster, 0.0)

    def _owned_snapshots(
        self,
        pod_name: PodName,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SymbolMarketSnapshot]:
        owned_symbols = set(self.registry.symbols_for(pod_name))
        if not owned_symbols:
            return []
        return [snapshot for snapshot in snapshots if snapshot.symbol.upper() in owned_symbols]

    def _prepare_snapshots(
        self,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[SymbolMarketSnapshot]:
        if not snapshots:
            return []
        return enrich_snapshots(self.config, snapshots)

    def sync_pod_b(self) -> None:
        previous_status = (
            dict(self.state.pod_b_status)
            if isinstance(self.state.pod_b_status, dict)
            else {}
        )
        self.state.pod_b_status = self._load_or_build_pod_b_status(previous_status)
        self._log_pod_b_sync_changes(previous_status=previous_status, current_status=self.state.pod_b_status)

    def refresh_pod_b_status(self) -> None:
        self.state.pod_b_status = self._load_or_build_pod_b_status(self.state.pod_b_status)

    def opening_symbols_for(self, pod_name: PodName) -> set[str]:
        allocation = self.capital_plan.pod_allocations[pod_name]
        return {
            symbol.symbol.upper()
            for symbol in allocation.symbols
            if symbol.target_usd > 0
        }

    def allowed_symbols_for(self, pod_name: PodName) -> set[str]:
        return self.opening_symbols_for(pod_name)

    def owner_for_symbol(self, symbol: str) -> PodName | None:
        normalized = str(symbol).strip().upper()
        for decision in self.state.symbol_routing:
            if decision.symbol.upper() == normalized:
                return decision.owner
        return None

    def managed_symbols_for(
        self,
        pod_name: PodName,
        active_symbols: set[str] | None = None,
    ) -> set[str]:
        managed = set(self.opening_symbols_for(pod_name))
        for symbol in active_symbols or set():
            normalized = str(symbol).strip().upper()
            owner = self.owner_for_symbol(normalized)
            if owner in {None, pod_name}:
                managed.add(normalized)
        return managed

    def _pod_b_active_symbols_from_status(self, status_payload: object) -> set[str]:
        if not isinstance(status_payload, dict):
            return set()
        active_symbols = {
            str(item.get("symbol", "")).strip().upper()
            for item in status_payload.get("open_positions", [])
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        }
        active_symbols.update(
            str(symbol).strip().upper()
            for symbol in status_payload.get("managed_symbols", [])
            if str(symbol).strip()
        )
        return active_symbols

    def _pod_b_runtime_status_path(self) -> Path:
        return Path("logs/pod_b_live_status.json")

    def _build_pod_b_fallback_status(
        self,
        previous_status: object | None = None,
    ) -> dict[str, object]:
        allocation = self.capital_plan.pod_allocations[PodName.POD_B]
        previous_payload = previous_status if isinstance(previous_status, dict) else {}
        active_symbols = self._pod_b_active_symbols_from_status(previous_payload)
        managed_symbols = sorted(self.managed_symbols_for(PodName.POD_B, active_symbols))
        opening_symbols = sorted(self.opening_symbols_for(PodName.POD_B))
        previous_report = (
            dict(previous_payload.get("report", {}))
            if isinstance(previous_payload.get("report"), dict)
            else {}
        )
        open_positions = (
            list(previous_payload.get("open_positions", []))
            if isinstance(previous_payload.get("open_positions"), list)
            else []
        )
        return {
            "pod": "pod_b",
            "process_state": "planned",
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "last_sync_reason": "supervisor_planned_state",
            "managed_symbols": managed_symbols,
            "opening_symbols": opening_symbols,
            "target_usd": round(allocation.target_usd, 2),
            "target_pct": round(allocation.target_pct, 6),
            "total_position_count": len(open_positions),
            "total_open_order_count": 0,
            "total_fill_count": int(previous_report.get("closed_trade_count", 0)),
            "realized_pnl_usd": float(previous_report.get("realized_pnl_usd", 0.0)),
            "total_unrealized_pnl_usd": round(
                sum(float(item.get("unrealized_pnl_usd", 0.0)) for item in open_positions if isinstance(item, dict)),
                4,
            ),
            "open_positions": open_positions,
            "report": previous_report,
            "status_path": str(self._pod_b_runtime_status_path()),
        }

    def _load_or_build_pod_b_status(
        self,
        previous_status: object | None = None,
    ) -> dict[str, object]:
        runtime_path = self._pod_b_runtime_status_path()
        payload = load_runtime_status(runtime_path)
        if runtime_status_is_fresh(payload):
            merged = sanitize_runtime_status_payload(payload, include_supervisor=False)
            merged.setdefault("status_path", str(runtime_path))
            return merged
        return self._build_pod_b_fallback_status(previous_status)

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
            "tradfi_summary_regime": self.tradfi_summary_regime.value,
            "tradfi_regime": self.tradfi_summary_regime.value,
            "cluster_regimes": {
                cluster: regime.value
                for cluster, regime in self.state.cluster_regimes.items()
            },
            "cluster_regime_snapshots": {
                cluster: {
                    "ready": snap.ready,
                    "adx": snap.adx,
                    "atr_ratio": snap.atr_ratio,
                    "range_width_bps": snap.range_width_bps,
                    "structure_score": snap.structure_score,
                    "btc_impulse": snap.btc_impulse,
                }
                for cluster, snap in self.state.cluster_regime_snapshots.items()
            },
            "cluster_target_allocations": self.cluster_target_allocations(),
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
                    "override_active": next(
                        (
                            decision.override_active
                            for decision in self.state.symbol_routing
                            if decision.symbol == item.symbol
                        ),
                        False,
                    ),
                    "override_owner": next(
                        (
                            decision.override_owner.value
                            for decision in self.state.symbol_routing
                            if decision.symbol == item.symbol and decision.override_owner is not None
                        ),
                        None,
                    ),
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
            "allocations": self.capital_allocator.allocations_for(
                self.state.regime,
                cluster_regimes=self.state.cluster_regimes or None,
            ),
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
                    "override_active": item.override_active,
                    "override_owner": (
                        item.override_owner.value if item.override_owner is not None else None
                    ),
                    "global_alignment": item.global_alignment,
                    "pod_scores": {
                        pod.value: score for pod, score in item.pod_scores.items()
                    },
                    "reassignment_count": self.state.symbol_reassignment_count_by_symbol.get(
                        item.symbol,
                        0,
                    ),
                    "last_reassigned_at": self.state.symbol_last_reassignment_at.get(
                        item.symbol
                    ),
                    "reassignment_age_seconds": self._reassignment_age_seconds(item.symbol),
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
            "routing_overrides": {
                "config": dict(self.config.trident.routing.symbol_pod_overrides),
                "runtime": dict(self.state.runtime_symbol_pod_overrides),
                "effective": self.effective_symbol_pod_overrides(),
                "runtime_updated_at": self.state.runtime_symbol_pod_overrides_updated_at,
                "runtime_path": str(self.runtime_routing_override_path()),
            },
            "pod_a_signal_preview": [
                {
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "setup": signal.setup,
                    "confidence": signal.confidence,
                }
                for signal in self.state.pod_a_signal_preview
            ],
            "pod_b_signal_preview": [
                {
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "setup": signal.setup,
                    "confidence": signal.confidence,
                }
                for signal in self.state.pod_b_signal_preview
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
                    "pod_reasoning": {
                        pod.value: reason
                        for pod, reason in decision.pod_reasoning.items()
                    },
                    "reassignment_cooldown_active": decision.reassignment_cooldown_active,
                    "reassignment_cooldown_remaining_seconds": round(
                        decision.reassignment_cooldown_remaining_seconds,
                        2,
                    ),
                    "override_active": decision.override_active,
                    "override_owner": (
                        decision.override_owner.value
                        if decision.override_owner is not None
                        else None
                    ),
                    "last_reassigned_at": self.state.symbol_last_reassignment_at.get(
                        decision.symbol
                    ),
                    "reassignment_age_seconds": self._reassignment_age_seconds(
                        decision.symbol
                    ),
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
            if previous_owner != decision.owner and (
                previous_owner is not None
                or decision.symbol in self.state.symbol_reassignment_count_by_symbol
            ):
                self.state.symbol_reassignment_count_by_symbol[decision.symbol] = (
                    self.state.symbol_reassignment_count_by_symbol.get(decision.symbol, 0)
                    + 1
                )
                self.state.symbol_last_reassignment_at[decision.symbol] = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )
            local_states.append(
                LocalSymbolState(
                    symbol=decision.symbol,
                    local_regime=decision.local_regime,
                    reason=decision.local_regime_reason,
                    owner=decision.owner,
                    previous_owner=decision.previous_owner,
                    override_active=decision.override_active,
                    override_owner=decision.override_owner,
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

    def _reassignment_age_seconds_by_symbol(self) -> dict[str, float]:
        ages: dict[str, float] = {}
        now = datetime.now(timezone.utc)
        for symbol, recorded_at in self.state.symbol_last_reassignment_at.items():
            age = self._parse_age_seconds(now=now, recorded_at=recorded_at)
            if age is not None:
                ages[symbol] = age
        return ages

    def _reassignment_age_seconds(self, symbol: str) -> float | None:
        recorded_at = self.state.symbol_last_reassignment_at.get(symbol)
        if recorded_at is None:
            return None
        return self._parse_age_seconds(
            now=datetime.now(timezone.utc),
            recorded_at=recorded_at,
        )

    def _parse_age_seconds(self, *, now: datetime, recorded_at: str) -> float | None:
        normalized = recorded_at.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max((now - parsed.astimezone(timezone.utc)).total_seconds(), 0.0)

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
        if self._compact_backtest_logs:
            summary = self._compact_tradable_pool_log
            summary["event_count"] = int(summary["event_count"]) + 1
            summary["added_total"] = int(summary["added_total"]) + len(added)
            summary["removed_total"] = int(summary["removed_total"]) + len(removed)
            summary["reason_change_total"] = int(summary["reason_change_total"]) + len(
                reason_changes
            )
            summary["current_tradable_total"] = int(summary["current_tradable_total"]) + len(
                current_tradable
            )
            summary["peak_tradable_pool"] = max(
                int(summary["peak_tradable_pool"]),
                len(current_tradable),
            )
            summary["last_regime"] = self.state.regime.value
            summary["last_added"] = added[:5]
            summary["last_removed"] = removed[:5]
            summary["last_reason_changes"] = reason_changes[:3]
            self._flush_compact_tradable_pool_log()
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
        decision_by_symbol = {
            decision.symbol: decision for decision in decisions
        }
        changes: list[str] = []
        assignment_count = 0
        deassignment_count = 0
        reassignment_count = 0
        pod_b_to_none_count = 0
        capacity_trim_count = 0
        unknown_reason_count = 0
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
            if previous_owner is None and current_owner is not None:
                assignment_count += 1
            elif previous_owner is not None and current_owner is None:
                deassignment_count += 1
            elif previous_owner is not None and current_owner is not None:
                reassignment_count += 1
            if previous_owner == PodName.POD_B and current_owner is None:
                pod_b_to_none_count += 1
            decision = decision_by_symbol.get(symbol)
            decision_mode = decision.mode if decision is not None else "routing_snapshot_missing"
            if decision is None:
                decision_reason = "routing_decision_missing_after_candidate_drop"
                unknown_reason_count += 1
            else:
                decision_reason = str(decision.reason or "routing_reason_missing")
                if decision_reason == "unknown":
                    unknown_reason_count += 1
            if str(decision_reason).startswith("capacity_trim:"):
                capacity_trim_count += 1
            changes.append(
                f"{symbol}:{previous_owner.value if previous_owner else 'none'}->{current_owner.value if current_owner else 'none'}"
                f" mode={decision_mode}"
                f" reason={decision_reason}"
            )
        conflict_count = len(self.state.ownership_conflicts)
        if not changes and conflict_count == previous_conflict_count:
            return
        if self._compact_backtest_logs:
            summary = self._compact_routing_log
            summary["event_count"] = int(summary["event_count"]) + 1
            summary["symbol_change_total"] = int(summary["symbol_change_total"]) + len(changes)
            summary["assignment_total"] = int(summary["assignment_total"]) + assignment_count
            summary["deassignment_total"] = (
                int(summary["deassignment_total"]) + deassignment_count
            )
            summary["reassignment_total"] = (
                int(summary["reassignment_total"]) + reassignment_count
            )
            summary["pod_b_to_none_total"] = (
                int(summary["pod_b_to_none_total"]) + pod_b_to_none_count
            )
            summary["capacity_trim_total"] = (
                int(summary["capacity_trim_total"]) + capacity_trim_count
            )
            summary["unknown_reason_total"] = (
                int(summary["unknown_reason_total"]) + unknown_reason_count
            )
            summary["conflict_peak"] = max(int(summary["conflict_peak"]), conflict_count)
            summary["last_regime"] = self.state.regime.value
            summary["last_changes"] = changes[:5]
            self._flush_compact_routing_log()
            return
        logger.info(
            "Supervisor symbol routing changed; regime=%s changes=%s conflicts=%s",
            self.state.regime.value,
            changes,
            conflict_count,
        )

    def _routing_override_symbols_by_pod(
        self,
        tradable_symbols: list[str],
    ) -> dict[PodName, list[str]]:
        tradable = {symbol.upper() for symbol in tradable_symbols}
        overrides = self.effective_symbol_pod_overrides()
        valid_pods = {pod.value: pod for pod in PodName}
        symbols_by_pod: dict[PodName, list[str]] = {}
        for raw_symbol, raw_pod in overrides.items():
            symbol = str(raw_symbol).strip().upper()
            if symbol not in tradable:
                continue
            pod = valid_pods.get(str(raw_pod).strip().lower())
            if pod is None:
                continue
            if not self.pods[pod].enabled:
                logger.warning(
                    "Ignoring routing override for disabled pod; symbol=%s target=%s",
                    symbol,
                    pod.value,
                )
                continue
            symbols_by_pod.setdefault(pod, []).append(symbol)
        return {
            pod: sorted(symbols)
            for pod, symbols in symbols_by_pod.items()
        }

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
        if self._compact_backtest_logs:
            summary = self._compact_pod_b_sync_log
            summary["event_count"] = int(summary["event_count"]) + 1
            summary["managed_symbol_total"] = int(summary["managed_symbol_total"]) + len(
                current_symbols
            )
            summary["peak_managed_symbols"] = max(
                int(summary["peak_managed_symbols"]),
                len(current_symbols),
            )
            if previous_target != current_target:
                summary["target_change_count"] = int(summary["target_change_count"]) + 1
            if previous_state != current_state:
                summary["process_state_change_count"] = (
                    int(summary["process_state_change_count"]) + 1
                )
            if previous_reason != current_reason:
                summary["reason_change_count"] = int(summary["reason_change_count"]) + 1
            summary["last_regime"] = self.state.regime.value
            summary["last_process_state"] = current_state
            summary["last_reason"] = current_reason
            summary["last_target_usd"] = current_target
            summary["last_symbols"] = current_symbols[:5]
            self._flush_compact_pod_b_sync_log()
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
