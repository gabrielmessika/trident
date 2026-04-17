from __future__ import annotations

from app.settings import AppConfig
from app.trident.market_clusters import correlation_group_for_symbol
from app.trident.types import CapitalPlan, PodAllocation, PodName, Regime, SymbolAllocation

CORRELATION_DENSITY_PENALTY = 0.25


class CapitalAllocator:
    """Produces a deterministic capital plan with pod and symbol caps."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def allocations_for(
        self,
        regime: Regime,
        tradfi_regime: Regime | None = None,
        cluster_regimes: dict[str, Regime] | None = None,
    ) -> dict[str, float]:
        section = self._allocation_section(regime)
        cluster_targets = self.cluster_target_pcts(regime, cluster_regimes)
        if self._uses_cluster_allocations():
            tradfi_pct = round(sum(cluster_targets.values()), 6)
        elif tradfi_regime is not None:
            tradfi_pct = self._tradfi_pod_c_pct(tradfi_regime)
        else:
            tradfi_pct = section.pod_c
        cash_pct = max(0.0, round(1.0 - section.pod_a - section.pod_b - tradfi_pct, 6))
        return {
            "pod_a": section.pod_a,
            "pod_b": section.pod_b,
            "pod_c": tradfi_pct,
            "cash": cash_pct,
        }

    def _tradfi_pod_c_pct(self, regime: Regime) -> float:
        tradfi = self._config.trident.allocations_tradfi
        if regime == Regime.TREND_EXPANSION:
            return tradfi.trend_expansion.pod_c
        elif regime == Regime.RANGE_AUCTION:
            return tradfi.range_auction.pod_c
        elif regime == Regime.PANIC_SQUEEZE:
            return tradfi.panic_squeeze.pod_c
        else:
            return tradfi.dead_zone.pod_c

    def cluster_target_pcts(
        self,
        regime: Regime,
        cluster_regimes: dict[str, Regime] | None,
    ) -> dict[str, float]:
        if not self._uses_cluster_allocations() or not cluster_regimes:
            return {}
        raw_targets: dict[str, float] = {}
        for cluster, cluster_regime in cluster_regimes.items():
            normalized_cluster = str(cluster).strip().lower()
            if not normalized_cluster or normalized_cluster == "crypto":
                continue
            target_pct = self._cluster_target_pct(normalized_cluster, cluster_regime)
            if target_pct > 0:
                raw_targets[normalized_cluster] = target_pct
        if not raw_targets:
            return {}
        total_target = sum(raw_targets.values())
        available_pct = self._available_tradfi_pct(regime)
        if available_pct <= 0:
            return {}
        scale = min(1.0, available_pct / total_target) if total_target > 0 else 0.0
        return {
            cluster: round(target_pct * scale, 6)
            for cluster, target_pct in raw_targets.items()
            if target_pct > 0
        }

    def _cluster_target_pct(self, cluster: str, regime: Regime) -> float:
        table = self._config.trident.allocations_cluster.clusters.get(cluster)
        if table is None:
            return 0.0
        if regime == Regime.TREND_EXPANSION:
            return table.trend_expansion.target_pct
        elif regime == Regime.RANGE_AUCTION:
            return table.range_auction.target_pct
        elif regime == Regime.PANIC_SQUEEZE:
            return table.panic_squeeze.target_pct
        else:
            return table.dead_zone.target_pct

    def _available_tradfi_pct(self, regime: Regime) -> float:
        section = self._allocation_section(regime)
        residual_pct = max(0.0, 1.0 - section.pod_a - section.pod_b)
        return min(residual_pct, max(self._config.pod_c.max_allocation_pct, 0.0))

    def _uses_cluster_allocations(self) -> bool:
        return bool(self._config.trident.allocations_cluster.clusters)

    def _allocation_section(self, regime: Regime) -> object:
        if regime == Regime.TREND_EXPANSION:
            return self._config.trident.allocations.trend_expansion
        elif regime == Regime.RANGE_AUCTION:
            return self._config.trident.allocations.range_auction
        elif regime == Regime.PANIC_SQUEEZE:
            return self._config.trident.allocations.panic_squeeze
        else:
            return self._config.trident.allocations.dead_zone

    def build_plan(
        self,
        regime: Regime,
        owned_symbols_by_pod: dict[PodName, list[str]],
        tradfi_regime: Regime | None = None,
        cluster_regimes: dict[str, Regime] | None = None,
        symbol_clusters_by_pod: dict[PodName, dict[str, str]] | None = None,
    ) -> CapitalPlan:
        base = self.allocations_for(
            regime,
            tradfi_regime=tradfi_regime,
            cluster_regimes=cluster_regimes,
        )
        cluster_targets = self.cluster_target_pcts(regime, cluster_regimes)
        total_equity = self._config.trident.capital.reference_equity_usd
        max_symbol_pct = self._config.trident.capital.max_allocation_per_symbol_pct
        min_symbol_usd = self._config.trident.capital.min_symbol_allocation_usd

        pod_caps = {
            PodName.POD_A: self._config.pod_a.max_allocation_pct,
            PodName.POD_B: self._config.pod_b.max_allocation_pct,
            PodName.POD_C: self._config.pod_c.max_allocation_pct,
        }
        pod_enabled = {
            PodName.POD_A: self._config.pod_a.enabled,
            PodName.POD_B: self._config.pod_b.enabled,
            PodName.POD_C: self._config.pod_c.enabled,
        }

        cash_pct = base.get("cash", 0.0)
        pod_allocations: dict[PodName, PodAllocation] = {}

        for pod_name, base_key in (
            (PodName.POD_A, "pod_a"),
            (PodName.POD_B, "pod_b"),
            (PodName.POD_C, "pod_c"),
        ):
            target_pct = base.get(base_key, 0.0)
            capped = False

            if not pod_enabled[pod_name]:
                cash_pct += target_pct
                target_pct = 0.0
            elif target_pct > pod_caps[pod_name]:
                cash_pct += target_pct - pod_caps[pod_name]
                target_pct = pod_caps[pod_name]
                capped = True

            owned_symbols = owned_symbols_by_pod.get(pod_name, [])
            symbol_allocations: list[SymbolAllocation] = []

            if target_pct > 0 and not owned_symbols:
                cash_pct += target_pct
                target_pct = 0.0
            elif owned_symbols and target_pct > 0:
                if pod_name == PodName.POD_C and cluster_targets:
                    symbol_allocations = self._build_pod_c_symbol_allocations(
                        owned_symbols=owned_symbols,
                        cluster_targets=cluster_targets,
                        symbol_clusters=(symbol_clusters_by_pod or {}).get(PodName.POD_C, {}),
                        total_equity=total_equity,
                        max_symbol_pct=max_symbol_pct,
                        min_symbol_usd=min_symbol_usd,
                    )
                else:
                    symbol_allocations = self._build_uniform_symbol_allocations(
                        owned_symbols=owned_symbols,
                        target_pct=target_pct,
                        total_equity=total_equity,
                        max_symbol_pct=max_symbol_pct,
                        min_symbol_usd=min_symbol_usd,
                        apply_correlation_cap=pod_name == PodName.POD_B,
                    )
                allocated_symbol_pct = sum(item.target_pct for item in symbol_allocations)
                if allocated_symbol_pct < target_pct:
                    cash_pct += target_pct - allocated_symbol_pct
                    target_pct = allocated_symbol_pct

            pod_allocations[pod_name] = PodAllocation(
                pod=pod_name,
                target_pct=round(target_pct, 6),
                target_usd=round(target_pct * total_equity, 2),
                capped_by_pod_limit=capped,
                symbols=symbol_allocations,
            )

        return CapitalPlan(
            regime=regime,
            total_equity_usd=total_equity,
            cash_pct=round(cash_pct, 6),
            cash_usd=round(cash_pct * total_equity, 2),
            pod_allocations=pod_allocations,
        )

    def _build_uniform_symbol_allocations(
        self,
        *,
        owned_symbols: list[str],
        target_pct: float,
        total_equity: float,
        max_symbol_pct: float,
        min_symbol_usd: float,
        apply_correlation_cap: bool = False,
    ) -> list[SymbolAllocation]:
        allocations: list[SymbolAllocation] = []
        per_symbol_pct = target_pct / len(owned_symbols)
        group_sizes = self._correlation_group_sizes(owned_symbols)
        for symbol in owned_symbols:
            density_factor = 1.0
            correlation_group = ""
            if apply_correlation_cap and per_symbol_pct * total_equity > (min_symbol_usd * 1.5):
                correlation_group = correlation_group_for_symbol(self._config, symbol) or ""
                density_factor = self._correlation_density_factor(symbol, group_sizes)
            symbol_pct = min(per_symbol_pct * density_factor, max_symbol_pct)
            symbol_usd = round(symbol_pct * total_equity, 2)
            if symbol_usd < min_symbol_usd:
                continue
            capped_by_correlation = density_factor < 0.9999
            reason_summary = "uniform_allocation"
            if capped_by_correlation:
                reason_summary = (
                    f"correlation_cap:{correlation_group or 'crypto'} "
                    f"x{density_factor:.3f}"
                )
            allocations.append(
                SymbolAllocation(
                    symbol=symbol,
                    target_pct=round(symbol_pct, 6),
                    target_usd=symbol_usd,
                    reason_summary=reason_summary,
                    correlation_group=correlation_group,
                    correlation_density_factor=round(density_factor, 6),
                    capped_by_correlation=capped_by_correlation,
                )
            )
        return allocations

    def _correlation_group_sizes(self, symbols: list[str]) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for symbol in symbols:
            group = correlation_group_for_symbol(self._config, symbol)
            if group is None:
                continue
            sizes[group] = sizes.get(group, 0) + 1
        return sizes

    def _correlation_density_factor(
        self,
        symbol: str,
        group_sizes: dict[str, int],
    ) -> float:
        group = correlation_group_for_symbol(self._config, symbol)
        if group is None:
            return 1.0
        size = group_sizes.get(group, 0)
        if size <= 1:
            return 1.0
        return 1.0 / (1.0 + CORRELATION_DENSITY_PENALTY * (size - 1))

    def _build_pod_c_symbol_allocations(
        self,
        *,
        owned_symbols: list[str],
        cluster_targets: dict[str, float],
        symbol_clusters: dict[str, str],
        total_equity: float,
        max_symbol_pct: float,
        min_symbol_usd: float,
    ) -> list[SymbolAllocation]:
        symbols_by_cluster: dict[str, list[str]] = {}
        for symbol in owned_symbols:
            cluster = str(symbol_clusters.get(symbol, "")).strip().lower()
            if not cluster:
                continue
            symbols_by_cluster.setdefault(cluster, []).append(symbol)

        allocations: list[SymbolAllocation] = []
        for cluster, cluster_target_pct in cluster_targets.items():
            cluster_symbols = symbols_by_cluster.get(cluster, [])
            if not cluster_symbols or cluster_target_pct <= 0:
                continue
            per_symbol_pct = cluster_target_pct / len(cluster_symbols)
            for symbol in cluster_symbols:
                symbol_pct = min(per_symbol_pct, max_symbol_pct)
                symbol_usd = round(symbol_pct * total_equity, 2)
                if symbol_usd < min_symbol_usd:
                    continue
                allocations.append(
                    SymbolAllocation(
                        symbol=symbol,
                        target_pct=round(symbol_pct, 6),
                        target_usd=symbol_usd,
                        reason_summary=f"cluster_budget:{cluster}",
                    )
                )
        return allocations
