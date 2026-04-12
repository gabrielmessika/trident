from __future__ import annotations

from app.risk.plan_gate import TradePlanRiskGate
from app.settings import AppConfig


class PodBRiskGate(TradePlanRiskGate):
    """Directional risk gate for Pod B breakout."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._config = config

    def evaluate_many(
        self,
        plans: list,
        *,
        current_open_expected_loss_usd: float = 0.0,
        current_open_position_count: int = 0,
    ) -> list:
        decisions: list = []
        seen_symbols: set[str] = set()
        accepted_count = 0
        accepted_expected_loss_usd = 0.0

        for plan in plans:
            reason = self._decision_reason(
                plan=plan,
                accepted_count=accepted_count,
                seen_symbols=seen_symbols,
                current_open_expected_loss_usd=current_open_expected_loss_usd,
                current_open_position_count=current_open_position_count,
                accepted_expected_loss_usd=accepted_expected_loss_usd,
            )
            accepted = reason == "accepted"
            from app.trident.types import RiskDecision

            decisions.append(RiskDecision(accepted=accepted, reason=reason, trade_plan=plan))
            if accepted:
                accepted_count += 1
                seen_symbols.add(plan.symbol)
                accepted_expected_loss_usd += max(plan.expected_loss_usd, 0.0)
        return decisions

    def _decision_reason(
        self,
        *,
        plan,
        accepted_count: int,
        seen_symbols: set[str],
        current_open_expected_loss_usd: float = 0.0,
        current_open_position_count: int = 0,
        accepted_expected_loss_usd: float = 0.0,
    ) -> str:
        if plan.confidence < self._config.pod_b.bis_min_confidence:
            return "confidence_below_min"
        if accepted_count + current_open_position_count >= self._config.pod_b.bis_max_concurrent_positions:
            return "max_open_positions_reached"
        reason = super()._decision_reason(
            plan=plan,
            accepted_count=accepted_count,
            seen_symbols=seen_symbols,
        )
        if reason != "accepted":
            return reason
        min_notional = max(
            self._config.trident.risk.min_trade_notional_usd,
            self._config.pod_b.bis_min_notional_usd,
        )
        if plan.target_notional_usd < min_notional:
            return "notional_below_min"
        if plan.margin_usd < self._config.pod_b.bis_min_margin_usd:
            return "margin_below_min"
        symbol_limit = self._config.pod_b.bis_max_leverage_by_symbol.get(
            plan.symbol.upper(),
            self._config.pod_b.bis_max_leverage,
        )
        if plan.effective_leverage > min(symbol_limit, self._config.pod_b.bis_max_leverage):
            return "leverage_above_asset_limit"
        if plan.expected_loss_usd > max(plan.risk_budget_usd, 0.0):
            return "risk_budget_exceeded"
        max_total_open_risk_usd = (
            self._config.trident.capital.reference_equity_usd
            * max(self._config.pod_b.bis_max_total_open_risk_pct, 0.0)
        )
        if (
            current_open_expected_loss_usd
            + accepted_expected_loss_usd
            + max(plan.expected_loss_usd, 0.0)
            > max_total_open_risk_usd
        ):
            return "total_open_risk_exceeded"
        return "accepted"
