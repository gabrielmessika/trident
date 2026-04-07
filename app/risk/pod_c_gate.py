from __future__ import annotations

from app.settings import AppConfig
from app.risk.plan_gate import TradePlanRiskGate


class PodCRiskGate(TradePlanRiskGate):
    """Pod C reuses the same deterministic trade-plan rules as Pod A."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._pod_c_min_confidence = config.pod_c.min_confidence
        self._blocked_symbols = {
            symbol.strip().upper() for symbol in config.pod_c.blocked_symbols if symbol.strip()
        }

    def _decision_reason(
        self,
        *,
        plan,
        accepted_count: int,
        seen_symbols: set[str],
    ) -> str:
        if str(plan.symbol).upper() in self._blocked_symbols:
            return "symbol_blocked"
        if plan.confidence < self._pod_c_min_confidence:
            return "confidence_below_min"
        return super()._decision_reason(
            plan=plan,
            accepted_count=accepted_count,
            seen_symbols=seen_symbols,
        )
