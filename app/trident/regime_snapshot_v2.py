from __future__ import annotations

from collections.abc import Mapping, Sequence


DEFAULT_CLUSTER_LEADERS: dict[str, tuple[str, ...]] = {
    "crypto": ("BTC", "ETH"),
    "index": ("XYZ:SP500", "SPY", "QQQ", "XYZ:XYZ100"),
    "gold": ("XYZ:GOLD", "PAXG", "GLD"),
    "silver": ("XYZ:SILVER", "SLV"),
    "oil": ("XYZ:CL", "XYZ:BRENTOIL"),
    "fx": ("XYZ:JPY"),
    "equity": ("XYZ:TSLA", "TSLA", "XYZ:NVDA", "NVDA", "XYZ:CRCL", "CRCL"),
}

KNOWN_SYMBOL_CLUSTERS: dict[str, str] = {
    "PAXG": "gold",
    "SPX": "index",
    "SPY": "index",
    "GLD": "gold",
    "SLV": "silver",
    "QQQ": "index",
    "USPYX": "index",
    "TSLA": "equity",
    "AMZN": "equity",
    "GOOGL": "equity",
    "META": "equity",
    "MSFT": "equity",
    "HOOD": "equity",
    "CRCL": "equity",
    "SNDK": "equity",
    "XYZ:CL": "oil",
    "XYZ:BRENTOIL": "oil",
    "XYZ:SP500": "index",
    "XYZ:XYZ100": "index",
    "XYZ:SILVER": "silver",
    "XYZ:GOLD": "gold",
    "XYZ:JPY": "fx",
    "XYZ:TSLA": "equity",
    "XYZ:NVDA": "equity",
    "XYZ:CRCL": "equity",
}


def enrich_regime_snapshot(
    snapshot: Mapping[str, object] | None,
    symbols: Sequence[Mapping[str, object] | dict[str, object]],
    *,
    leader_candidates: Sequence[str] | None = None,
    market_cluster: str | None = "crypto",
    cluster_by_symbol: Mapping[str, str] | None = None,
) -> dict[str, object]:
    base = dict(snapshot or {})
    normalized_symbols = [
        dict(item) for item in symbols if isinstance(item, Mapping)
    ]
    if market_cluster is not None:
        filtered_symbols = [
            item
            for item in normalized_symbols
            if _market_cluster(item, cluster_by_symbol=cluster_by_symbol) == market_cluster
        ]
        if not filtered_symbols and any(
            _market_cluster(item, cluster_by_symbol=cluster_by_symbol) != market_cluster
            for item in normalized_symbols
        ):
            return _with_empty_v2_metrics(base)
    else:
        filtered_symbols = normalized_symbols
    if not filtered_symbols:
        filtered_symbols = normalized_symbols
    if not filtered_symbols:
        return _with_empty_v2_metrics(base)

    leader = _select_leader(
        filtered_symbols,
        leader_candidates or DEFAULT_CLUSTER_LEADERS.get(market_cluster or "", ("BTC", "ETH")),
    )
    leader_symbol = str(leader.get("symbol", "")).upper()
    leader_direction = _leader_direction(leader, filtered_symbols)
    active_symbols = [item for item in filtered_symbols if _is_active_symbol(item)]
    if not active_symbols:
        active_symbols = filtered_symbols

    active_count = len(active_symbols)
    aligned_count = sum(
        1
        for item in active_symbols
        if _is_aligned(item, leader_direction=leader_direction, leader_symbol=leader_symbol)
    )
    disagreement_count = max(0, active_count - aligned_count)
    leader_in_active = any(str(item.get("symbol", "")).upper() == leader_symbol for item in active_symbols)
    aligned_ex_leader = max(0, aligned_count - (1 if leader_in_active else 0))
    alt_candidate_count = max(0, active_count - (1 if leader_in_active else 0))

    breadth_pct = aligned_count / active_count if active_count else 0.0
    alt_participation_pct = (
        aligned_ex_leader / alt_candidate_count if alt_candidate_count else breadth_pct
    )
    dispersion_pct = disagreement_count / active_count if active_count else 0.0
    leader_trend_score = _leader_trend_score(leader)
    coherence_score = _clamp(
        alt_participation_pct * 0.45
        + leader_trend_score * 0.35
        + (1.0 - dispersion_pct) * 0.20,
        0.0,
        1.0,
    )

    base.update(
        {
            "leader_symbol": leader_symbol,
            "symbol_count": len(filtered_symbols),
            "active_symbol_count": active_count,
            "aligned_symbol_count": aligned_count,
            "breadth_pct": round(breadth_pct, 4),
            "alt_participation_pct": round(alt_participation_pct, 4),
            "dispersion_pct": round(dispersion_pct, 4),
            "leader_trend_score": round(leader_trend_score, 4),
            "coherence_score": round(coherence_score, 4),
        }
    )
    return _with_empty_v2_metrics(base)


def enrich_cluster_regime_snapshots(
    cluster_snapshots: Mapping[str, Mapping[str, object]] | None,
    symbols: Sequence[Mapping[str, object] | dict[str, object]],
    *,
    cluster_by_symbol: Mapping[str, str] | None = None,
    cluster_leaders: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, dict[str, object]]:
    normalized_snapshots = {
        str(cluster).lower(): dict(snapshot)
        for cluster, snapshot in (cluster_snapshots or {}).items()
        if isinstance(snapshot, Mapping)
    }
    leader_map = {
        str(cluster).lower(): tuple(str(item).upper() for item in leaders)
        for cluster, leaders in (cluster_leaders or {}).items()
        if isinstance(leaders, Sequence)
    }
    cluster_names = set(normalized_snapshots) | set(leader_map)
    if not cluster_names:
        return {}

    normalized_symbols = [dict(item) for item in symbols if isinstance(item, Mapping)]
    result: dict[str, dict[str, object]] = {}
    for cluster in sorted(cluster_names):
        cluster_symbols = [
            item
            for item in normalized_symbols
            if _market_cluster(item, cluster_by_symbol=cluster_by_symbol) == cluster
        ]
        base_snapshot = normalized_snapshots.get(cluster)
        if not cluster_symbols and base_snapshot is None:
            continue
        result[cluster] = enrich_regime_snapshot(
            base_snapshot,
            cluster_symbols,
            leader_candidates=leader_map.get(cluster) or DEFAULT_CLUSTER_LEADERS.get(cluster),
            market_cluster=None,
            cluster_by_symbol=cluster_by_symbol,
        )
    return result


def _with_empty_v2_metrics(snapshot: dict[str, object]) -> dict[str, object]:
    snapshot.setdefault("leader_symbol", "")
    snapshot.setdefault("symbol_count", 0)
    snapshot.setdefault("active_symbol_count", 0)
    snapshot.setdefault("aligned_symbol_count", 0)
    snapshot.setdefault("breadth_pct", 0.0)
    snapshot.setdefault("alt_participation_pct", 0.0)
    snapshot.setdefault("dispersion_pct", 1.0 if snapshot.get("symbol_count", 0) else 0.0)
    snapshot.setdefault("leader_trend_score", 0.0)
    snapshot.setdefault("coherence_score", 0.0)
    return snapshot


def _select_leader(
    symbols: list[dict[str, object]],
    leader_candidates: Sequence[str] | None,
) -> dict[str, object]:
    normalized_candidates = [str(item).upper() for item in (leader_candidates or ())]
    by_symbol = {str(item.get("symbol", "")).upper(): item for item in symbols}
    candidate_items = [by_symbol[candidate] for candidate in normalized_candidates if candidate in by_symbol]
    if candidate_items:
        return max(candidate_items, key=_leader_priority)
    return max(symbols, key=_leader_priority)


def _leader_priority(item: Mapping[str, object]) -> float:
    structure = abs(float(item.get("structure_score", 0.0) or 0.0))
    vwap_distance = min(abs(float(item.get("vwap_distance_bps", 0.0) or 0.0)) / 25.0, 1.0)
    bucket_range = min(abs(float(item.get("bucket_range_bps", 0.0) or 0.0)) / 140.0, 1.0)
    return structure * 0.65 + vwap_distance * 0.20 + bucket_range * 0.15


def _leader_direction(
    leader: Mapping[str, object],
    symbols: Sequence[Mapping[str, object]],
) -> int:
    leader_score = _alignment_score(leader)
    if abs(leader_score) >= 0.06:
        return 1 if leader_score > 0 else -1
    mean_score = sum(_alignment_score(item) for item in symbols) / max(len(symbols), 1)
    if abs(mean_score) >= 0.04:
        return 1 if mean_score > 0 else -1
    return 0


def _alignment_score(item: Mapping[str, object]) -> float:
    structure = float(item.get("structure_score", 0.0) or 0.0)
    vwap_component = _clamp(
        float(item.get("vwap_distance_bps", 0.0) or 0.0) / 45.0,
        -0.30,
        0.30,
    )
    return structure + vwap_component


def _is_active_symbol(item: Mapping[str, object]) -> bool:
    return (
        abs(float(item.get("structure_score", 0.0) or 0.0)) >= 0.12
        or abs(float(item.get("vwap_distance_bps", 0.0) or 0.0)) >= 8.0
        or float(item.get("bucket_range_bps", 0.0) or 0.0) >= 60.0
        or int(item.get("bucket_trade_count", 0) or 0) >= 12
    )


def _is_aligned(
    item: Mapping[str, object],
    *,
    leader_direction: int,
    leader_symbol: str,
) -> bool:
    if leader_direction == 0:
        return bool(item.get("btc_aligned", True))
    score = _alignment_score(item)
    if abs(score) >= 0.06:
        return (score > 0) == (leader_direction > 0)
    if leader_symbol in {"BTC", "ETH"}:
        return bool(item.get("btc_aligned", False))
    return False


def _leader_trend_score(item: Mapping[str, object]) -> float:
    structure = min(abs(float(item.get("structure_score", 0.0) or 0.0)), 1.0)
    vwap_distance = min(abs(float(item.get("vwap_distance_bps", 0.0) or 0.0)) / 25.0, 1.0)
    bucket_range = min(abs(float(item.get("bucket_range_bps", 0.0) or 0.0)) / 160.0, 1.0)
    return _clamp(structure * 0.70 + vwap_distance * 0.20 + bucket_range * 0.10, 0.0, 1.0)


def _market_cluster(
    item: Mapping[str, object],
    *,
    cluster_by_symbol: Mapping[str, str] | None,
) -> str:
    explicit_cluster = str(item.get("market_cluster", "")).strip().lower()
    if explicit_cluster:
        return explicit_cluster
    symbol = str(item.get("symbol", "")).strip().upper()
    if cluster_by_symbol and symbol in cluster_by_symbol:
        return str(cluster_by_symbol[symbol]).strip().lower() or "crypto"
    if symbol in KNOWN_SYMBOL_CLUSTERS:
        return KNOWN_SYMBOL_CLUSTERS[symbol]
    if symbol.startswith("XYZ:"):
        return KNOWN_SYMBOL_CLUSTERS.get(symbol, "macro")
    return "crypto"


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))
