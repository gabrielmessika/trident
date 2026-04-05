from __future__ import annotations

from app.settings import AppConfig
from app.trident.types import RiskDecision, TradePlan


class TradePlanRiskGate:
    """Shared deterministic gate for directional trade plans."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def evaluate_many(self, plans: list[TradePlan]) -> list[RiskDecision]:
        decisions: list[RiskDecision] = []
        seen_symbols: set[str] = set()
        accepted_count = 0

        for plan in plans:
            reason = self._decision_reason(
                plan=plan,
                accepted_count=accepted_count,
                seen_symbols=seen_symbols,
            )
            accepted = reason == "accepted"
            decisions.append(RiskDecision(accepted=accepted, reason=reason, trade_plan=plan))
            if accepted:
                accepted_count += 1
                seen_symbols.add(plan.symbol)

        return decisions

    def _decision_reason(
        self,
        *,
        plan: TradePlan,
        accepted_count: int,
        seen_symbols: set[str],
    ) -> str:
        limits = self._config.trident.risk
        if plan.confidence < limits.min_confidence:
            return "confidence_below_min"
        if plan.target_notional_usd < limits.min_trade_notional_usd:
            return "notional_below_min"
        if accepted_count >= limits.max_trade_plans_per_batch:
            return "batch_limit_reached"
        if plan.symbol in seen_symbols:
            return "duplicate_symbol"
        return "accepted"
