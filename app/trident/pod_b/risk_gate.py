from __future__ import annotations

from collections import deque

from app.risk.plan_gate import TradePlanRiskGate
from app.settings import AppConfig


class PodBRiskGate(TradePlanRiskGate):
    """Directional risk gate for Pod B breakout."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._config = config
        self._guardrail_lookback = max(config.pod_b.bis_guardrail_lookback_trades, 1)
        self._closed_trade_pnl_by_key: dict[tuple[str, str], deque[float]] = {}

    def record_closed_trade(
        self,
        *,
        symbol: str,
        setup: str | None,
        pnl_usd: float | None,
    ) -> None:
        key = self._guardrail_key(symbol, setup)
        if key is None or pnl_usd is None:
            return
        history = self._closed_trade_pnl_by_key.get(key)
        if history is None:
            history = deque(maxlen=self._guardrail_lookback)
            self._closed_trade_pnl_by_key[key] = history
        history.append(float(pnl_usd))

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
        if self._rolling_guardrail_triggered(plan.symbol, plan.setup):
            return "rolling_guardrail_symbol_setup"
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

    def _rolling_guardrail_triggered(self, symbol: str, setup: str | None) -> bool:
        if not self._config.pod_b.bis_guardrail_enabled:
            return False
        key = self._guardrail_key(symbol, setup)
        if key is None:
            return False
        history = self._closed_trade_pnl_by_key.get(key)
        if history is None:
            return False
        if len(history) < max(self._config.pod_b.bis_guardrail_min_closed_trades, 1):
            return False
        cumulative_pnl = sum(history)
        return cumulative_pnl <= self._config.pod_b.bis_guardrail_max_cumulative_loss_usd

    def _guardrail_key(self, symbol: str, setup: str | None) -> tuple[str, str] | None:
        normalized_symbol = str(symbol).strip().upper()
        normalized_setup = str(setup or "").strip().lower()
        if not normalized_symbol or not normalized_setup:
            return None
        return normalized_symbol, normalized_setup
