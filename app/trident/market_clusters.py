from __future__ import annotations

from dataclasses import replace

from app.settings import AppConfig
from app.trident.types import SymbolMarketSnapshot


DEFAULT_CLUSTER = "crypto"
DEFAULT_CLUSTER_LEADERS: dict[str, list[str]] = {
    "crypto": ["BTC", "ETH"],
    "index": ["SPY"],
    "gold": ["GLD", "PAXG"],
    "silver": ["SLV"],
    "equity": ["TSLA"],
}
DEFAULT_CLUSTER_OVERRIDES: dict[str, str] = {
    "PAXG": "gold",
    "SPY": "index",
    "GLD": "gold",
    "SLV": "silver",
    "QQQ": "index",
    "TSLA": "equity",
    "CRCL": "equity",
    "SNDK": "equity",
}
DEFAULT_CRYPTO_CORRELATION_GROUPS: dict[str, str] = {
    "BTC": "core_beta",
    "ETH": "core_beta",
    "SOL": "core_beta",
    "LINK": "core_beta",
    "AVAX": "core_beta",
    "ADA": "core_beta",
    "DOGE": "core_beta",
    "PNUT": "meme_beta",
    "FARTCOIN": "meme_beta",
    "PUMP": "meme_beta",
    "KPEPE": "meme_beta",
    "PEPE": "meme_beta",
    "XPL": "meme_beta",
}


def normalize_symbols(symbols: list[str] | None) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for symbol in symbols or []:
        name = str(symbol).strip().upper()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return normalized


def observation_universe_symbols(config: AppConfig) -> list[str]:
    source = (
        config.hyperliquid.observation_universe
        or config.hyperliquid.default_coins
        or []
    )
    return normalize_symbols(source)


def normalize_cluster_names(clusters: list[str] | None) -> set[str]:
    return {
        str(cluster).strip().lower()
        for cluster in clusters or []
        if str(cluster).strip()
    }


def symbols_in_allowed_clusters(
    config: AppConfig,
    symbols: list[str] | None,
    allowed_clusters: list[str] | None,
) -> list[str]:
    normalized_symbols = normalize_symbols(symbols)
    cluster_scope = normalize_cluster_names(allowed_clusters)
    if not cluster_scope:
        return []
    return [
        symbol
        for symbol in normalized_symbols
        if cluster_for_symbol(config, symbol) in cluster_scope
    ]


def cluster_for_symbol(config: AppConfig, symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not normalized:
        return DEFAULT_CLUSTER
    overrides = {
        **DEFAULT_CLUSTER_OVERRIDES,
        **config.hyperliquid.market_cluster_overrides,
    }
    return overrides.get(normalized, DEFAULT_CLUSTER)


def correlation_group_for_symbol(config: AppConfig, symbol: str) -> str | None:
    normalized = str(symbol).strip().upper()
    if not normalized:
        return None
    if cluster_for_symbol(config, normalized) != DEFAULT_CLUSTER:
        return None
    return DEFAULT_CRYPTO_CORRELATION_GROUPS.get(normalized)


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
    if cluster != DEFAULT_CLUSTER and symbol in snapshot_by_symbol and snapshot_by_symbol[symbol].price > 0:
        return symbol
    return symbol if symbol in all_cluster_leaders(config) else ""


def _impulse_bps(snapshot: SymbolMarketSnapshot) -> float:
    if snapshot.price <= 0:
        return 0.0
    return ((snapshot.price - snapshot.ema_slow) / snapshot.price) * 10_000.0
