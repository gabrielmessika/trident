from __future__ import annotations

from dataclasses import asdict, dataclass

from app.settings import TriggerLiquidityConfig
from app.trident.types import SymbolMarketSnapshot, TradePlan


@dataclass(frozen=True, slots=True)
class TriggerLiquidityDecision:
    symbol: str
    side: str
    action: str
    proposed_action: str
    reason: str
    shadow_only: bool
    veto: bool = False
    size_multiplier: float = 1.0
    confidence_delta: float = 0.0
    adverse_cascade_risk: float = 0.0
    supportive_cascade_risk: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TriggerLiquidityOverlay:
    """Turns compact trigger-liquidity features into Pod A / Supervisor guidance."""

    def __init__(self, config: TriggerLiquidityConfig) -> None:
        self.config = config

    def evaluate(
        self,
        snapshot: SymbolMarketSnapshot,
        *,
        side: str,
        setup: str = "",
        confidence: float = 0.0,
    ) -> TriggerLiquidityDecision:
        normalized_side = side.strip().lower()
        if not self.config.enabled:
            return self._decision(snapshot, normalized_side, "allow", "disabled")
        if not snapshot.trigger_liquidity_available:
            return self._decision(snapshot, normalized_side, "allow", "no_trigger_liquidity")
        if self._is_stale(snapshot):
            return self._decision(snapshot, normalized_side, "allow", "trigger_liquidity_stale")

        adverse, supportive = self._cascade_risks(snapshot, normalized_side)
        proposed_action = "allow"
        reason = "trigger_liquidity_neutral"
        size_multiplier = 1.0
        confidence_delta = 0.0
        veto = False

        if self.config.veto_enabled and adverse >= self.config.veto_min_cascade_risk:
            proposed_action = "veto_entry"
            reason = "adverse_trigger_cascade_risk"
            veto = True
        elif self.config.sizing_enabled and adverse >= self.config.reduce_min_cascade_risk:
            proposed_action = "reduce_size"
            reason = "adverse_trigger_cascade_watch"
            size_multiplier = self._size_multiplier()
        elif (
            self.config.confidence_boost_enabled
            and supportive >= self.config.confidence_boost_min_cascade_risk
        ):
            proposed_action = "boost_confidence"
            reason = "supportive_trigger_cascade_risk"
            confidence_delta = max(float(self.config.confidence_boost_delta), 0.0)

        action = proposed_action
        if self.config.shadow_only and proposed_action != "allow":
            action = "watch"
            veto = False
            size_multiplier = 1.0
            confidence_delta = 0.0

        return TriggerLiquidityDecision(
            symbol=snapshot.symbol,
            side=normalized_side,
            action=action,
            proposed_action=proposed_action,
            reason=reason,
            shadow_only=bool(self.config.shadow_only),
            veto=veto,
            size_multiplier=size_multiplier,
            confidence_delta=confidence_delta,
            adverse_cascade_risk=round(adverse, 4),
            supportive_cascade_risk=round(supportive, 4),
        )

    def apply_to_plan(
        self,
        plan: TradePlan,
        snapshot: SymbolMarketSnapshot,
    ) -> TradePlan | None:
        decision = self.evaluate(
            snapshot,
            side=plan.side,
            setup=plan.setup,
            confidence=plan.confidence,
        )
        plan.setup_details = {
            **dict(plan.setup_details or {}),
            **self.details_for_snapshot(snapshot, decision),
        }
        if decision.veto:
            return None
        if decision.action == "reduce_size":
            multiplier = decision.size_multiplier
            plan.target_notional_usd = round(plan.target_notional_usd * multiplier, 6)
            plan.margin_usd = round(plan.margin_usd * multiplier, 6)
            plan.risk_budget_usd = round(plan.risk_budget_usd * multiplier, 6)
            plan.expected_loss_usd = round(plan.expected_loss_usd * multiplier, 6)
        if decision.action == "boost_confidence":
            plan.confidence = round(min(plan.confidence + decision.confidence_delta, 1.0), 3)
        return plan

    def details_for_snapshot(
        self,
        snapshot: SymbolMarketSnapshot,
        decision: TriggerLiquidityDecision,
    ) -> dict[str, object]:
        return {
            "trigger_liquidity_action": decision.action,
            "trigger_liquidity_proposed_action": decision.proposed_action,
            "trigger_liquidity_reason": decision.reason,
            "trigger_liquidity_shadow_only": decision.shadow_only,
            "trigger_adverse_cascade_risk": decision.adverse_cascade_risk,
            "trigger_supportive_cascade_risk": decision.supportive_cascade_risk,
            "trigger_nearest_stop_cluster_bps": snapshot.nearest_stop_cluster_bps,
            "trigger_nearest_tp_cluster_bps": snapshot.nearest_tp_cluster_bps,
            "trigger_stop_pressure_above": snapshot.stop_pressure_above,
            "trigger_stop_pressure_below": snapshot.stop_pressure_below,
            "trigger_tp_pressure_above": snapshot.tp_pressure_above,
            "trigger_tp_pressure_below": snapshot.tp_pressure_below,
            "trigger_asymmetry": snapshot.trigger_asymmetry,
            "trigger_data_age_seconds": snapshot.trigger_data_age_seconds,
        }

    def _decision(
        self,
        snapshot: SymbolMarketSnapshot,
        side: str,
        action: str,
        reason: str,
    ) -> TriggerLiquidityDecision:
        return TriggerLiquidityDecision(
            symbol=snapshot.symbol,
            side=side,
            action=action,
            proposed_action=action,
            reason=reason,
            shadow_only=bool(self.config.shadow_only),
        )

    def _is_stale(self, snapshot: SymbolMarketSnapshot) -> bool:
        age = snapshot.trigger_data_age_seconds
        if age is None:
            return False
        return age > max(float(self.config.max_data_age_seconds), 0.0)

    def _cascade_risks(
        self,
        snapshot: SymbolMarketSnapshot,
        side: str,
    ) -> tuple[float, float]:
        if side == "long":
            return snapshot.cascade_risk_down, snapshot.cascade_risk_up
        if side == "short":
            return snapshot.cascade_risk_up, snapshot.cascade_risk_down
        return 0.0, 0.0

    def _size_multiplier(self) -> float:
        return min(max(float(self.config.size_reduction_multiplier), 0.0), 1.0)
