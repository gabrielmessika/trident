from __future__ import annotations

from app.trident.pod_a.signals import AnchorTrendContext


MAX_SPREAD_BPS_BY_CLUSTER = {
    "crypto": 8.0,
    "index": 5.0,
    "gold": 6.0,
}
MAX_ABS_FUNDING_RATE_BY_CLUSTER = {
    "crypto": 0.0005,
    "index": 0.0015,
    "gold": 0.001,
}


def max_spread_bps_for_cluster(cluster: str) -> float:
    return MAX_SPREAD_BPS_BY_CLUSTER.get(cluster, MAX_SPREAD_BPS_BY_CLUSTER["crypto"])


def max_abs_funding_rate_for_cluster(cluster: str) -> float:
    return MAX_ABS_FUNDING_RATE_BY_CLUSTER.get(
        cluster,
        MAX_ABS_FUNDING_RATE_BY_CLUSTER["crypto"],
    )


def passes_anchor_filters(context: AnchorTrendContext) -> bool:
    if not context.cluster_aligned:
        return False
    if context.spread_bps > max_spread_bps_for_cluster(context.market_cluster):
        return False
    if abs(context.funding_rate) > max_abs_funding_rate_for_cluster(context.market_cluster):
        return False
    return True
