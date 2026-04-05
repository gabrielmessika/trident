from __future__ import annotations

from app.risk.plan_gate import TradePlanRiskGate


class PodCRiskGate(TradePlanRiskGate):
    """Pod C reuses the same deterministic trade-plan rules as Pod A."""
