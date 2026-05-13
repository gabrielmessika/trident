from __future__ import annotations

from dataclasses import dataclass

from app.trident.types import PodName, RiskDecision, TradePlan


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


@dataclass(slots=True)
class ExternalReferencePolicyConfig:
    min_source_count: int = 1
    pass_when_missing: bool = True
    max_reference_age_seconds: float = 120.0
    max_abs_premium_bps: float = 50.0
    max_adverse_premium_bps: float = 25.0
    max_source_deviation_bps: float = 35.0
    counter_momentum_60s_bps: float = 6.0
    counter_momentum_300s_bps: float = 12.0
    confidence_adjustment_enabled: bool = False
    aligned_confidence_bonus: float = 0.025
    adverse_confidence_penalty: float = 0.04
    max_confidence_adjustment: float = 0.05


class ExternalReferenceDecisionPolicy:
    """Backtest-only policy for testing external venue references on Pod A/C plans."""

    def __init__(self, config: ExternalReferencePolicyConfig | None = None) -> None:
        self.config = config or ExternalReferencePolicyConfig()

    def adjust_plans(self, pod: PodName, plans: list[TradePlan]) -> None:
        if pod not in {PodName.POD_A, PodName.POD_C}:
            return
        if not self.config.confidence_adjustment_enabled:
            return
        for plan in plans:
            adjustment = self._confidence_adjustment(plan)
            if adjustment == 0.0:
                continue
            previous = float(plan.confidence)
            plan.confidence = round(_clamp(previous + adjustment, 0.0, 1.0), 3)
            details = dict(plan.setup_details or {})
            details["external_confidence_adjustment"] = round(adjustment, 4)
            details["external_confidence_before_adjustment"] = round(previous, 4)
            plan.setup_details = details
            components = dict(plan.confidence_components or {})
            components["external_reference_adjustment"] = round(adjustment, 4)
            plan.confidence_components = components

    def apply_decisions(
        self,
        pod: PodName,
        decisions: list[RiskDecision],
    ) -> list[RiskDecision]:
        if pod not in {PodName.POD_A, PodName.POD_C}:
            return decisions
        filtered: list[RiskDecision] = []
        for decision in decisions:
            if not decision.accepted:
                filtered.append(decision)
                continue
            reason = self.veto_reason(decision.trade_plan)
            if reason is None:
                filtered.append(decision)
                continue
            filtered.append(
                RiskDecision(
                    accepted=False,
                    reason=reason,
                    trade_plan=decision.trade_plan,
                )
            )
        return filtered

    def veto_reason(self, plan: TradePlan) -> str | None:
        details = dict(plan.setup_details or {})
        source_count = int(float(details.get("external_reference_source_count", 0.0) or 0.0))
        if source_count < max(self.config.min_source_count, 1):
            return None if self.config.pass_when_missing else "external_reference_missing"

        age = float(details.get("external_reference_age_seconds", 0.0) or 0.0)
        if age > self.config.max_reference_age_seconds:
            return "external_reference_stale"

        source_deviation = float(
            details.get("external_reference_max_deviation_bps", 0.0) or 0.0
        )
        if source_deviation > self.config.max_source_deviation_bps:
            return "external_reference_source_dispersion"

        premium = float(details.get("external_premium_bps", 0.0) or 0.0)
        if abs(premium) > self.config.max_abs_premium_bps:
            return "external_reference_dislocation"

        side = str(plan.side).strip().lower()
        if side == "long" and premium > self.config.max_adverse_premium_bps:
            return "external_reference_long_chase_premium"
        if side == "short" and premium < -self.config.max_adverse_premium_bps:
            return "external_reference_short_chase_discount"

        momentum_60s = float(details.get("external_momentum_60s_bps", 0.0) or 0.0)
        if side == "long" and momentum_60s <= -self.config.counter_momentum_60s_bps:
            return "external_reference_counter_momentum_60s"
        if side == "short" and momentum_60s >= self.config.counter_momentum_60s_bps:
            return "external_reference_counter_momentum_60s"

        momentum_300s = float(details.get("external_momentum_300s_bps", 0.0) or 0.0)
        if side == "long" and momentum_300s <= -self.config.counter_momentum_300s_bps:
            return "external_reference_counter_momentum_300s"
        if side == "short" and momentum_300s >= self.config.counter_momentum_300s_bps:
            return "external_reference_counter_momentum_300s"

        return None

    def _confidence_adjustment(self, plan: TradePlan) -> float:
        details = dict(plan.setup_details or {})
        source_count = int(float(details.get("external_reference_source_count", 0.0) or 0.0))
        if source_count < max(self.config.min_source_count, 1):
            return 0.0
        side = str(plan.side).strip().lower()
        side_sign = 1.0 if side == "long" else -1.0 if side == "short" else 0.0
        if side_sign == 0.0:
            return 0.0
        premium = float(details.get("external_premium_bps", 0.0) or 0.0)
        momentum_60s = float(details.get("external_momentum_60s_bps", 0.0) or 0.0)
        momentum_300s = float(details.get("external_momentum_300s_bps", 0.0) or 0.0)
        momentum_score = (
            _clamp(momentum_60s / max(self.config.counter_momentum_60s_bps * 2.0, 1e-9), -1.0, 1.0)
            * 0.65
            + _clamp(momentum_300s / max(self.config.counter_momentum_300s_bps * 2.0, 1e-9), -1.0, 1.0)
            * 0.35
        )
        directional_score = momentum_score * side_sign
        adverse_premium = (
            premium > self.config.max_adverse_premium_bps * 0.6
            if side == "long"
            else premium < -self.config.max_adverse_premium_bps * 0.6
        )
        if directional_score >= 0.35 and not adverse_premium:
            return min(self.config.aligned_confidence_bonus, self.config.max_confidence_adjustment)
        if directional_score <= -0.35 or adverse_premium:
            return -min(
                self.config.adverse_confidence_penalty,
                self.config.max_confidence_adjustment,
            )
        return 0.0
