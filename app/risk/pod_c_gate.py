from __future__ import annotations

from app.risk.plan_gate import TradePlanRiskGate
from app.settings import AppConfig
from app.trident.pod_a.leverage import LeveragePolicy


class PodCRiskGate(TradePlanRiskGate):
    """Pod C reuses the same deterministic trade-plan rules as Pod A."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._leverage_policy = LeveragePolicy(config.pod_c)
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
        reason = super()._decision_reason(
            plan=plan,
            accepted_count=accepted_count,
            seen_symbols=seen_symbols,
        )
        if reason != "accepted":
            return reason
        limits = self._config.trident.risk
        min_notional = max(
            limits.min_trade_notional_usd,
            self._config.pod_c.min_notional_usd,
        )
        if plan.target_notional_usd < min_notional:
            return "notional_below_min"
        if plan.margin_usd < self._config.pod_c.min_margin_usd:
            return "margin_below_min"
        global_limit = self._leverage_policy.max_allowed()
        symbol_limit = self._leverage_policy.max_allowed(plan.symbol)
        if plan.effective_leverage > symbol_limit:
            if symbol_limit < global_limit:
                return "leverage_above_asset_limit"
            return "leverage_above_limit"
        if plan.expected_loss_usd > max(plan.risk_budget_usd, 0.0):
            return "risk_budget_exceeded"
        return "accepted"
