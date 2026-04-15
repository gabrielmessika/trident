from __future__ import annotations

from app.risk.plan_gate import TradePlanRiskGate
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.pod_a.symbol_mode import active_symbol_mode
from app.trident.types import RiskDecision, TradePlan


class PodARiskGate(TradePlanRiskGate):
    """Pod A extends the shared gate with risk-budget and leverage checks."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._leverage_policy = LeveragePolicy(config.pod_a)
        self._disabled_setups = {item.strip() for item in config.pod_a.disabled_setups if item.strip()}
        self._blocked_regimes = {
            item.strip().lower() for item in config.pod_a.blocked_regimes if item.strip()
        }
        self._allowed_setups_in_blocked_regimes = {
            item.strip()
            for item in config.pod_a.allowed_setups_in_blocked_regimes
            if item.strip()
        }

    def evaluate_many(self, plans: list[TradePlan]) -> list[RiskDecision]:
        decisions: list[RiskDecision] = []
        seen_symbols: set[str] = set()
        accepted_count = 0
        accepted_expected_loss_usd = 0.0

        for plan in plans:
            reason = self._decision_reason(
                plan=plan,
                accepted_count=accepted_count,
                seen_symbols=seen_symbols,
                accepted_expected_loss_usd=accepted_expected_loss_usd,
            )
            accepted = reason == "accepted"
            decisions.append(RiskDecision(accepted=accepted, reason=reason, trade_plan=plan))
            if accepted:
                accepted_count += 1
                seen_symbols.add(plan.symbol)
                accepted_expected_loss_usd += max(plan.expected_loss_usd, 0.0)
        return decisions

    def _decision_reason(
        self,
        *,
        plan: TradePlan,
        accepted_count: int,
        seen_symbols: set[str],
        accepted_expected_loss_usd: float = 0.0,
    ) -> str:
        reason = super()._decision_reason(
            plan=plan,
            accepted_count=accepted_count,
            seen_symbols=seen_symbols,
        )
        if reason != "accepted":
            return reason
        symbol_mode = active_symbol_mode(self._config.pod_a, plan.symbol)
        symbol_mode_allowed_setups = (
            {item.strip() for item in symbol_mode.allowed_setups if item.strip()}
            if symbol_mode is not None
            else set()
        )
        if plan.setup in self._disabled_setups and plan.setup not in symbol_mode_allowed_setups:
            return "setup_disabled"

        current_regime = str(plan.setup_details.get("regime", "")).strip().lower()
        if (
            current_regime in self._blocked_regimes
            and plan.setup not in self._allowed_setups_in_blocked_regimes
        ):
            return "regime_filtered"
        if symbol_mode is not None:
            if symbol_mode_allowed_setups and plan.setup not in symbol_mode_allowed_setups:
                return "symbol_mode_setup_filtered"
            allowed_regimes = {
                item.strip().lower() for item in symbol_mode.allowed_regimes if item.strip()
            }
            if allowed_regimes and current_regime not in allowed_regimes:
                return "symbol_mode_regime_filtered"
            if plan.confidence < max(symbol_mode.min_confidence, 0.0):
                return "symbol_mode_confidence_below_min"

        limits = self._config.trident.risk
        min_notional = max(
            limits.min_trade_notional_usd,
            self._config.pod_a.min_notional_usd,
        )
        if plan.target_notional_usd < min_notional:
            return "notional_below_min"
        if plan.margin_usd < self._config.pod_a.min_margin_usd:
            return "margin_below_min"
        global_limit = self._leverage_policy.max_allowed()
        symbol_limit = self._leverage_policy.max_allowed(plan.symbol)
        if plan.effective_leverage > symbol_limit:
            if symbol_limit < global_limit:
                return "leverage_above_asset_limit"
            return "leverage_above_limit"
        if plan.expected_loss_usd > max(plan.risk_budget_usd, 0.0):
            return "risk_budget_exceeded"

        max_total_open_risk_usd = (
            self._config.trident.capital.reference_equity_usd
            * max(limits.max_total_open_risk_pct, 0.0)
        )
        if accepted_expected_loss_usd + max(plan.expected_loss_usd, 0.0) > max_total_open_risk_usd:
            return "total_open_risk_exceeded"
        return "accepted"
