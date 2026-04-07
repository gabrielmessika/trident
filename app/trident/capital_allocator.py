from __future__ import annotations

from app.settings import AppConfig
from app.trident.types import CapitalPlan, PodAllocation, PodName, Regime, SymbolAllocation


class CapitalAllocator:
    """Produces a deterministic capital plan with pod and symbol caps."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def allocations_for(self, regime: Regime) -> dict[str, float]:
        if regime == Regime.TREND_EXPANSION:
            section = self._config.trident.allocations.trend_expansion
        elif regime == Regime.RANGE_AUCTION:
            section = self._config.trident.allocations.range_auction
        elif regime == Regime.PANIC_SQUEEZE:
            section = self._config.trident.allocations.panic_squeeze
        else:
            section = self._config.trident.allocations.dead_zone
        return {
            "pod_a": section.pod_a,
            "pod_b": section.pod_b,
            "pod_c": section.pod_c,
            "cash": section.cash,
        }

    def build_plan(
        self,
        regime: Regime,
        owned_symbols_by_pod: dict[PodName, list[str]],
    ) -> CapitalPlan:
        base = self.allocations_for(regime)
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
                per_symbol_pct = target_pct / len(owned_symbols)
                for symbol in owned_symbols:
                    symbol_pct = min(per_symbol_pct, max_symbol_pct)
                    symbol_usd = round(symbol_pct * total_equity, 2)
                    if symbol_usd < min_symbol_usd:
                        continue
                    symbol_allocations.append(
                        SymbolAllocation(
                            symbol=symbol,
                            target_pct=round(symbol_pct, 6),
                            target_usd=symbol_usd,
                        )
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
