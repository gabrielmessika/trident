from __future__ import annotations

from dataclasses import replace

from app.settings import AppConfig
from app.trident.types import SymbolMarketSnapshot


DEFAULT_CLUSTER = "crypto"
DEFAULT_CLUSTER_LEADERS: dict[str, list[str]] = {
    "crypto": ["BTC", "ETH"],
    "index": ["SPX"],
    "gold": ["PAXG"],
}
DEFAULT_CLUSTER_OVERRIDES: dict[str, str] = {
    "SPX": "index",
    "PAXG": "gold",
}


def cluster_for_symbol(config: AppConfig, symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not normalized:
        return DEFAULT_CLUSTER
    overrides = {
        **DEFAULT_CLUSTER_OVERRIDES,
        **config.hyperliquid.market_cluster_overrides,
    }
    return overrides.get(normalized, DEFAULT_CLUSTER)


def leaders_for_cluster(config: AppConfig, cluster: str) -> list[str]:
    normalized = str(cluster).strip().lower() or DEFAULT_CLUSTER
    leaders = {
        **DEFAULT_CLUSTER_LEADERS,
        **config.hyperliquid.cluster_leaders,
    }
    selected = leaders.get(normalized)
    if selected:
        return [symbol.upper() for symbol in selected]
    return list(DEFAULT_CLUSTER_LEADERS.get(DEFAULT_CLUSTER, []))


def all_cluster_leaders(config: AppConfig) -> set[str]:
    leaders = {
        symbol.upper()
        for group in {**DEFAULT_CLUSTER_LEADERS, **config.hyperliquid.cluster_leaders}.values()
        for symbol in group
    }
    return leaders


def enrich_snapshots(
    config: AppConfig,
    snapshots: list[SymbolMarketSnapshot],
) -> list[SymbolMarketSnapshot]:
    snapshot_by_symbol = {snapshot.symbol.upper(): snapshot for snapshot in snapshots}
    impulse_by_symbol = {
        symbol: _impulse_bps(snapshot)
        for symbol, snapshot in snapshot_by_symbol.items()
    }
    enriched: list[SymbolMarketSnapshot] = []
    for snapshot in snapshots:
        symbol = snapshot.symbol.upper()
        cluster = cluster_for_symbol(config, symbol)
        leader_symbol = _select_cluster_leader(
            config=config,
            cluster=cluster,
            symbol=symbol,
            snapshot_by_symbol=snapshot_by_symbol,
            impulse_by_symbol=impulse_by_symbol,
        )
        leader_impulse = impulse_by_symbol.get(leader_symbol, 0.0) if leader_symbol else 0.0
        symbol_impulse = impulse_by_symbol.get(symbol, 0.0)
        cluster_aligned = snapshot.btc_aligned
        if leader_symbol:
            cluster_aligned = (
                symbol == leader_symbol
                or symbol_impulse == 0.0
                or leader_impulse == 0.0
                or (symbol_impulse > 0.0) == (leader_impulse > 0.0)
            )
        enriched.append(
            replace(
                snapshot,
                market_cluster=cluster,
                cluster_aligned=cluster_aligned,
                cluster_leader=leader_symbol or "",
            )
        )
    return enriched


def _select_cluster_leader(
    *,
    config: AppConfig,
    cluster: str,
    symbol: str,
    snapshot_by_symbol: dict[str, SymbolMarketSnapshot],
    impulse_by_symbol: dict[str, float],
) -> str:
    candidates = [
        leader
        for leader in leaders_for_cluster(config, cluster)
        if leader in snapshot_by_symbol and snapshot_by_symbol[leader].price > 0
    ]
    if symbol in candidates:
        return symbol
    if candidates:
        return max(candidates, key=lambda item: abs(impulse_by_symbol.get(item, 0.0)))
    return symbol if symbol in all_cluster_leaders(config) else ""


def _impulse_bps(snapshot: SymbolMarketSnapshot) -> float:
    if snapshot.price <= 0:
        return 0.0
    return ((snapshot.price - snapshot.ema_slow) / snapshot.price) * 10_000.0
