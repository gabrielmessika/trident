from __future__ import annotations

from app.trident.pod_a.signals import AnchorTrendContext


MAX_SPREAD_BPS = 8.0
MAX_ABS_FUNDING_RATE = 0.0005


def passes_anchor_filters(context: AnchorTrendContext) -> bool:
    if context.regime != "TrendExpansion":
        return False
    if not context.btc_aligned:
        return False
    if context.spread_bps > MAX_SPREAD_BPS:
        return False
    if abs(context.funding_rate) > MAX_ABS_FUNDING_RATE:
        return False
    return True

