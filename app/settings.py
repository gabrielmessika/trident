from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(slots=True)
class AllocationConfig:
    pod_a: float = 0.0
    pod_b: float = 0.0
    pod_c: float = 0.0
    cash: float = 0.0


@dataclass(slots=True)
class RegimeThresholds:
    adx_trend_threshold: float = 22.0
    trend_structure_threshold: float = 0.30
    atr_ratio_panic_threshold: float = 1.8
    dead_zone_atr_threshold: float = 0.45
    dead_zone_range_threshold: float = 80.0
    switch_confirmation_bars: int = 3
    trend_confirmation_bars: int = 2
    panic_confirmation_bars: int = 1


@dataclass(slots=True)
class CapitalLimits:
    reference_equity_usd: float = 1000.0
    max_allocation_per_symbol_pct: float = 0.25
    min_symbol_allocation_usd: float = 25.0


@dataclass(slots=True)
class RiskLimits:
    min_confidence: float = 0.50
    max_trade_plans_per_batch: int = 2
    min_trade_notional_usd: float = 50.0
    max_risk_per_trade_pct: float = 0.01
    max_total_open_risk_pct: float = 0.03


@dataclass(slots=True)
class ExecutionConfig:
    dry_run_taker_fee_bps: float = 3.5
    dry_run_slippage_bps: float = 0.5
    dry_run_spread_multiplier: float = 0.5
    routing_revoke_grace_minutes: int = 0
    routing_revoke_grace_minutes_by_symbol: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class RoutingConfig:
    min_assign_score: float = 0.45
    min_hold_score: float = 0.35
    hysteresis_margin: float = 0.15
    reassignment_cooldown_seconds: int = 900
    reassignment_debounce_min_score: float = 0.15
    reassignment_debounce_seconds_by_symbol: dict[str, int] = field(default_factory=dict)
    runtime_override_path: str = "./runtime/trident/symbol_routing_overrides.json"
    symbol_pod_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TradfiAllocationConfig:
    pod_c: float = 0.0

    @property
    def total(self) -> float:
        return self.pod_c


@dataclass(slots=True)
class TradfiRegimeAllocations:
    trend_expansion: TradfiAllocationConfig = field(default_factory=TradfiAllocationConfig)
    range_auction: TradfiAllocationConfig = field(default_factory=TradfiAllocationConfig)
    panic_squeeze: TradfiAllocationConfig = field(default_factory=TradfiAllocationConfig)
    dead_zone: TradfiAllocationConfig = field(default_factory=TradfiAllocationConfig)


@dataclass(slots=True)
class ClusterAllocationConfig:
    target_pct: float = 0.0


@dataclass(slots=True)
class ClusterRegimeAllocationTable:
    trend_expansion: ClusterAllocationConfig = field(default_factory=ClusterAllocationConfig)
    range_auction: ClusterAllocationConfig = field(default_factory=ClusterAllocationConfig)
    panic_squeeze: ClusterAllocationConfig = field(default_factory=ClusterAllocationConfig)
    dead_zone: ClusterAllocationConfig = field(default_factory=ClusterAllocationConfig)


@dataclass(slots=True)
class ClusterAllocationsConfig:
    clusters: dict[str, ClusterRegimeAllocationTable] = field(default_factory=dict)


@dataclass(slots=True)
class RegimeAllocations:
    trend_expansion: AllocationConfig
    range_auction: AllocationConfig
    panic_squeeze: AllocationConfig
    dead_zone: AllocationConfig


@dataclass(slots=True)
class GeneralConfig:
    mode: str = "observation"
    host: str = "127.0.0.1"
    port: int = 3000
    log_level: str = "info"


@dataclass(slots=True)
class HyperliquidConfig:
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    info_url: str = "https://api.hyperliquid.xyz/info"
    rate_limit_state_path: str = "./runtime/hyperliquid_rate_limits.json"
    snapshot_output_dir: str = "./data/live_snapshots"
    pod_b_feature_output_dir: str = "./data/live_features/pod_b"
    observation_universe: list[str] | None = None
    max_coins_per_connection: int = 10
    subscription_pacing_ms: int = 250
    bucket_ms: int = 60_000
    pod_b_feature_bucket_ms: int = 10_000
    reconnect_delay_seconds: float = 5.0
    max_reconnect_delay_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    message_timeout_seconds: float = 20.0
    heartbeat_interval_seconds: float = 25.0
    max_idle_heartbeats: int = 2
    info_requests_per_minute: int = 60
    ws_connects_per_minute: int = 6
    ws_messages_per_second: int = 8
    shared_rate_limit_jitter_seconds: float = 0.1
    circuit_breaker_threshold: int = 3
    circuit_breaker_seconds: float = 30.0
    default_coins: list[str] | None = None
    tradable_max_spread_bps: float = 10.0
    tradable_min_bucket_notional_usd: float = 100.0
    tradable_min_bucket_trade_count: int = 3
    tradable_max_abs_funding_rate: float = 0.01
    tradable_blocked_symbols: list[str] = field(default_factory=list)
    market_cluster_overrides: dict[str, str] = field(default_factory=dict)
    cluster_leaders: dict[str, list[str]] = field(default_factory=dict)
    spot_coin_ids: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class TridentConfigSection:
    enabled: bool
    regime: RegimeThresholds
    capital: CapitalLimits
    risk: RiskLimits
    execution: ExecutionConfig
    routing: RoutingConfig
    allocations: RegimeAllocations
    allocations_tradfi: TradfiRegimeAllocations = field(default_factory=TradfiRegimeAllocations)
    allocations_cluster: ClusterAllocationsConfig = field(default_factory=ClusterAllocationsConfig)


@dataclass(slots=True)
class PodAConfig:
    enabled: bool
    allowed_market_clusters: list[str]
    max_allocation_pct: float
    default_leverage: float
    max_leverage: float
    max_leverage_by_symbol: dict[str, float]
    prefer_isolated: bool
    sizing_mode: str
    risk_per_trade_pct: float
    min_margin_usd: float
    min_notional_usd: float
    allow_partial_take_profit: bool
    allow_break_even: bool
    disabled_setups: list[str]
    blocked_regimes: list[str]
    allowed_setups_in_blocked_regimes: list[str]
    symbol_modes: dict[str, PodASymbolModeConfig] = field(default_factory=dict)


@dataclass(slots=True)
class PodASymbolModeConfig:
    enabled: bool = False
    allowed_setups: list[str] = field(default_factory=list)
    allowed_regimes: list[str] = field(default_factory=list)
    min_confidence: float = 0.0
    risk_per_trade_pct_multiplier: float = 1.0
    stop_bps_multiplier: float = 1.0
    stop_bps_floor: float = 0.0
    time_stop_hours: int = 24
    take_profit_multiplier: float = 1.0
    break_even_multiplier: float = 1.0
    trailing_activation_multiplier: float = 1.0
    trailing_distance_multiplier: float = 1.0
    max_leverage: float = 0.0


@dataclass(slots=True)
class PodBConfig:
    enabled: bool
    allowed_market_clusters: list[str]
    max_allocation_pct: float
    bis_min_confidence: float
    bis_default_leverage: float
    bis_max_leverage: float
    bis_max_leverage_by_symbol: dict[str, float]
    bis_risk_per_trade_pct: float
    bis_min_margin_usd: float
    bis_min_notional_usd: float
    bis_reentry_cooldown_minutes: int
    bis_time_stop_hours: int
    bis_max_spread_bps: float
    bis_min_bucket_notional_usd: float
    bis_min_bucket_trade_count: int
    bis_min_compression_score: float
    bis_min_activity_score: float
    bis_min_breakout_score: float
    bis_min_volume_ratio: float
    bis_min_trade_count_ratio: float
    bis_max_chase_distance_bps: float
    bis_allowed_regimes: list[str]
    bis_min_abs_structure_score: float
    bis_min_trend_quality_bps: float
    bis_min_realized_vol_short_bps: float
    bis_min_directional_vwap_distance_bps: float
    bis_stop_floor_bps: float
    bis_stop_ceiling_bps: float
    bis_enable_longs: bool
    bis_enable_shorts: bool
    bis_strict_continuation_filter_enabled: bool
    bis_enabled_setups: list[str]
    bis_max_concurrent_positions: int
    bis_max_total_open_risk_pct: float


@dataclass(slots=True)
class PodCConfig:
    enabled: bool
    allowed_market_clusters: list[str]
    cluster_aware_v2_enabled: bool
    max_allocation_pct: float
    default_leverage: float
    max_leverage: float
    max_leverage_by_symbol: dict[str, float]
    max_spread_bps: float
    max_abs_funding_rate: float
    min_confidence: float
    size_multiplier: float
    risk_per_trade_pct: float
    min_margin_usd: float
    min_notional_usd: float
    reentry_cooldown_minutes: int
    time_stop_hours: int
    blocked_symbols: list[str]
    min_bucket_notional_usd: float
    min_bucket_trade_count: int
    min_trend_bps: float
    min_structure_score: float
    max_vwap_distance_bps: float
    min_reclaim_distance_bps: float
    min_activity_ratio: float
    activity_lookback: int


@dataclass(slots=True)
class AppConfig:
    general: GeneralConfig
    hyperliquid: HyperliquidConfig
    trident: TridentConfigSection
    pod_a: PodAConfig
    pod_b: PodBConfig
    pod_c: PodCConfig


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _allocations(data: dict[str, object], name: str) -> AllocationConfig:
    section = data[name]
    return AllocationConfig(
        pod_a=float(section.get("pod_a", 0.0)),
        pod_b=float(section.get("pod_b", 0.0)),
        pod_c=float(section.get("pod_c", 0.0)),
        cash=float(section.get("cash", 0.0)),
    )


def _tradfi_allocations(trident_data: dict[str, object]) -> TradfiRegimeAllocations:
    data = trident_data.get("allocations_tradfi", {})
    if not isinstance(data, dict):
        return TradfiRegimeAllocations()

    def _parse(name: str) -> TradfiAllocationConfig:
        section = data.get(name, {})
        if not isinstance(section, dict):
            return TradfiAllocationConfig()
        return TradfiAllocationConfig(
            pod_c=float(section.get("pod_c", 0.0)),
        )

    return TradfiRegimeAllocations(
        trend_expansion=_parse("trend_expansion"),
        range_auction=_parse("range_auction"),
        panic_squeeze=_parse("panic_squeeze"),
        dead_zone=_parse("dead_zone"),
    )


def _cluster_allocations(trident_data: dict[str, object]) -> ClusterAllocationsConfig:
    data = trident_data.get("allocations_cluster", {})
    if not isinstance(data, dict):
        return ClusterAllocationsConfig()

    def _parse_table(cluster_data: object) -> ClusterRegimeAllocationTable:
        if not isinstance(cluster_data, dict):
            return ClusterRegimeAllocationTable()

        def _parse_section(name: str) -> ClusterAllocationConfig:
            section = cluster_data.get(name, {})
            if not isinstance(section, dict):
                return ClusterAllocationConfig()
            return ClusterAllocationConfig(
                target_pct=float(section.get("target_pct", 0.0)),
            )

        return ClusterRegimeAllocationTable(
            trend_expansion=_parse_section("trend_expansion"),
            range_auction=_parse_section("range_auction"),
            panic_squeeze=_parse_section("panic_squeeze"),
            dead_zone=_parse_section("dead_zone"),
        )

    return ClusterAllocationsConfig(
        clusters={
            str(cluster).strip().lower(): _parse_table(cluster_data)
            for cluster, cluster_data in data.items()
            if str(cluster).strip()
        }
    )


def _float_map(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, float] = {}
    for key, value in raw.items():
        parsed[str(key).upper()] = float(value)
    return parsed


def _str_map(raw: object, *, upper_keys: bool = True, lower_values: bool = False) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, str] = {}
    for key, value in raw.items():
        normalized_key = str(key).upper() if upper_keys else str(key)
        normalized_value = str(value)
        if lower_values:
            normalized_value = normalized_value.lower()
        parsed[normalized_key] = normalized_value
    return parsed


def _str_list_map(raw: object, *, lower_keys: bool = True, upper_values: bool = True) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(value, list):
            continue
        normalized_key = str(key).lower() if lower_keys else str(key)
        normalized_values: list[str] = []
        for item in value:
            name = str(item).strip()
            if not name:
                continue
            normalized_values.append(name.upper() if upper_values else name)
        parsed[normalized_key] = normalized_values
    return parsed


def _str_list(raw: object, *, upper_values: bool = False) -> list[str]:
    if not isinstance(raw, list):
        return []
    parsed: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not name:
            continue
        parsed.append(name.upper() if upper_values else name)
    return parsed


def _pod_a_symbol_modes(raw: object) -> dict[str, PodASymbolModeConfig]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, PodASymbolModeConfig] = {}
    for symbol, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            continue
        parsed[normalized_symbol] = PodASymbolModeConfig(
            enabled=bool(payload.get("enabled", False)),
            allowed_setups=_str_list(payload.get("allowed_setups", [])),
            allowed_regimes=_str_list(payload.get("allowed_regimes", [])),
            min_confidence=float(payload.get("min_confidence", 0.0)),
            risk_per_trade_pct_multiplier=float(
                payload.get("risk_per_trade_pct_multiplier", 1.0)
            ),
            stop_bps_multiplier=float(payload.get("stop_bps_multiplier", 1.0)),
            stop_bps_floor=float(payload.get("stop_bps_floor", 0.0)),
            time_stop_hours=int(payload.get("time_stop_hours", 24)),
            take_profit_multiplier=float(payload.get("take_profit_multiplier", 1.0)),
            break_even_multiplier=float(payload.get("break_even_multiplier", 1.0)),
            trailing_activation_multiplier=float(
                payload.get("trailing_activation_multiplier", 1.0)
            ),
            trailing_distance_multiplier=float(
                payload.get("trailing_distance_multiplier", 1.0)
            ),
            max_leverage=float(payload.get("max_leverage", 0.0)),
        )
    return parsed


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.getenv("TRIDENT_CONFIG_PATH", "config/trident.toml"))
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    general_data = data.get("general", {})
    hyperliquid_data = data.get("hyperliquid", {})
    trident_data = data.get("trident", {})
    allocations_data = trident_data.get("allocations", {})
    regime_data = trident_data.get("regime", {})
    capital_data = trident_data.get("capital", {})
    risk_data = trident_data.get("risk", {})
    execution_data = trident_data.get("execution", {})
    routing_data = trident_data.get("routing", {})
    pod_a_data = data.get("pod_a", {})
    pod_b_data = data.get("pod_b", {})
    pod_c_data = data.get("pod_c", {})

    return AppConfig(
        general=GeneralConfig(
            mode=str(general_data.get("mode", "observation")),
            host=str(general_data.get("host", "127.0.0.1")),
            port=int(general_data.get("port", 3000)),
            log_level=str(general_data.get("log_level", "info")),
        ),
        hyperliquid=HyperliquidConfig(
            ws_url=str(
                hyperliquid_data.get("ws_url", "wss://api.hyperliquid.xyz/ws")
            ),
            info_url=str(
                hyperliquid_data.get("info_url", "https://api.hyperliquid.xyz/info")
            ),
            rate_limit_state_path=str(
                hyperliquid_data.get(
                    "rate_limit_state_path",
                    "./runtime/hyperliquid_rate_limits.json",
                )
            ),
            snapshot_output_dir=str(
                hyperliquid_data.get("snapshot_output_dir", "./data/live_snapshots")
            ),
            pod_b_feature_output_dir=str(
                hyperliquid_data.get(
                    "pod_b_feature_output_dir",
                    "./data/live_features/pod_b",
                )
            ),
            observation_universe=list(
                hyperliquid_data.get(
                    "observation_universe",
                    hyperliquid_data.get("default_coins", []),
                )
            ),
            max_coins_per_connection=int(
                hyperliquid_data.get("max_coins_per_connection", 10)
            ),
            subscription_pacing_ms=int(
                hyperliquid_data.get("subscription_pacing_ms", 250)
            ),
            bucket_ms=int(hyperliquid_data.get("bucket_ms", 60_000)),
            pod_b_feature_bucket_ms=int(
                hyperliquid_data.get("pod_b_feature_bucket_ms", 10_000)
            ),
            reconnect_delay_seconds=float(
                hyperliquid_data.get("reconnect_delay_seconds", 5.0)
            ),
            max_reconnect_delay_seconds=float(
                hyperliquid_data.get("max_reconnect_delay_seconds", 30.0)
            ),
            connect_timeout_seconds=float(
                hyperliquid_data.get("connect_timeout_seconds", 10.0)
            ),
            message_timeout_seconds=float(
                hyperliquid_data.get("message_timeout_seconds", 20.0)
            ),
            heartbeat_interval_seconds=float(
                hyperliquid_data.get("heartbeat_interval_seconds", 25.0)
            ),
            max_idle_heartbeats=int(hyperliquid_data.get("max_idle_heartbeats", 2)),
            info_requests_per_minute=int(
                hyperliquid_data.get("info_requests_per_minute", 60)
            ),
            ws_connects_per_minute=int(
                hyperliquid_data.get("ws_connects_per_minute", 6)
            ),
            ws_messages_per_second=int(
                hyperliquid_data.get("ws_messages_per_second", 8)
            ),
            shared_rate_limit_jitter_seconds=float(
                hyperliquid_data.get("shared_rate_limit_jitter_seconds", 0.1)
            ),
            circuit_breaker_threshold=int(
                hyperliquid_data.get("circuit_breaker_threshold", 3)
            ),
            circuit_breaker_seconds=float(
                hyperliquid_data.get("circuit_breaker_seconds", 30.0)
            ),
            default_coins=list(hyperliquid_data.get("default_coins", [])),
            tradable_max_spread_bps=float(
                hyperliquid_data.get("tradable_max_spread_bps", 10.0)
            ),
            tradable_min_bucket_notional_usd=float(
                hyperliquid_data.get("tradable_min_bucket_notional_usd", 100.0)
            ),
            tradable_min_bucket_trade_count=int(
                hyperliquid_data.get("tradable_min_bucket_trade_count", 3)
            ),
            tradable_max_abs_funding_rate=float(
                hyperliquid_data.get("tradable_max_abs_funding_rate", 0.01)
            ),
            tradable_blocked_symbols=_str_list(
                hyperliquid_data.get("tradable_blocked_symbols", []),
            ),
            market_cluster_overrides=_str_map(
                hyperliquid_data.get("market_cluster_overrides", {}),
                upper_keys=True,
                lower_values=True,
            ),
            cluster_leaders=_str_list_map(
                hyperliquid_data.get("cluster_leaders", {}),
                lower_keys=True,
                upper_values=True,
            ),
            spot_coin_ids=_str_map(
                hyperliquid_data.get("spot_coin_ids", {}),
                upper_keys=True,
                lower_values=False,
            ),
        ),
        trident=TridentConfigSection(
            enabled=bool(trident_data.get("enabled", True)),
            regime=RegimeThresholds(
                adx_trend_threshold=float(regime_data.get("adx_trend_threshold", 22.0)),
                trend_structure_threshold=float(
                    regime_data.get("trend_structure_threshold", 0.30)
                ),
                atr_ratio_panic_threshold=float(
                    regime_data.get("atr_ratio_panic_threshold", 1.8)
                ),
                dead_zone_atr_threshold=float(
                    regime_data.get("dead_zone_atr_threshold", 0.45)
                ),
                dead_zone_range_threshold=float(
                    regime_data.get("dead_zone_range_threshold", 80.0)
                ),
                switch_confirmation_bars=int(
                    regime_data.get("switch_confirmation_bars", 3)
                ),
                trend_confirmation_bars=int(
                    regime_data.get("trend_confirmation_bars", 2)
                ),
                panic_confirmation_bars=int(
                    regime_data.get("panic_confirmation_bars", 1)
                ),
            ),
            capital=CapitalLimits(
                reference_equity_usd=float(capital_data.get("reference_equity_usd", 1000.0)),
                max_allocation_per_symbol_pct=float(
                    capital_data.get("max_allocation_per_symbol_pct", 0.25)
                ),
                min_symbol_allocation_usd=float(
                    capital_data.get("min_symbol_allocation_usd", 25.0)
                ),
            ),
            risk=RiskLimits(
                min_confidence=float(risk_data.get("min_confidence", 0.50)),
                max_trade_plans_per_batch=int(risk_data.get("max_trade_plans_per_batch", 2)),
                min_trade_notional_usd=float(
                    risk_data.get("min_trade_notional_usd", 50.0)
                ),
                max_risk_per_trade_pct=float(
                    risk_data.get("max_risk_per_trade_pct", 0.01)
                ),
                max_total_open_risk_pct=float(
                    risk_data.get("max_total_open_risk_pct", 0.03)
                ),
            ),
            execution=ExecutionConfig(
                dry_run_taker_fee_bps=float(
                    execution_data.get("dry_run_taker_fee_bps", 3.5)
                ),
                dry_run_slippage_bps=float(
                    execution_data.get("dry_run_slippage_bps", 0.5)
                ),
                dry_run_spread_multiplier=float(
                    execution_data.get("dry_run_spread_multiplier", 0.5)
                ),
                routing_revoke_grace_minutes=int(
                    execution_data.get("routing_revoke_grace_minutes", 0)
                ),
                routing_revoke_grace_minutes_by_symbol={
                    symbol: int(value)
                    for symbol, value in _float_map(
                        execution_data.get("routing_revoke_grace_minutes_by_symbol", {})
                    ).items()
                },
            ),
            routing=RoutingConfig(
                min_assign_score=float(routing_data.get("min_assign_score", 0.45)),
                min_hold_score=float(routing_data.get("min_hold_score", 0.35)),
                hysteresis_margin=float(routing_data.get("hysteresis_margin", 0.15)),
                reassignment_cooldown_seconds=int(
                    routing_data.get("reassignment_cooldown_seconds", 900)
                ),
                reassignment_debounce_min_score=float(
                    routing_data.get("reassignment_debounce_min_score", 0.15)
                ),
                reassignment_debounce_seconds_by_symbol={
                    symbol: int(value)
                    for symbol, value in _float_map(
                        routing_data.get("reassignment_debounce_seconds_by_symbol", {})
                    ).items()
                },
                runtime_override_path=str(
                    routing_data.get(
                        "runtime_override_path",
                        "./runtime/trident/symbol_routing_overrides.json",
                    )
                ),
                symbol_pod_overrides=_str_map(
                    routing_data.get("symbol_pod_overrides", {}),
                    upper_keys=True,
                    lower_values=True,
                ),
            ),
            allocations=RegimeAllocations(
                trend_expansion=_allocations(allocations_data, "trend_expansion"),
                range_auction=_allocations(allocations_data, "range_auction"),
                panic_squeeze=_allocations(allocations_data, "panic_squeeze"),
                dead_zone=_allocations(allocations_data, "dead_zone"),
            ),
            allocations_tradfi=_tradfi_allocations(trident_data),
            allocations_cluster=_cluster_allocations(trident_data),
        ),
        pod_a=PodAConfig(
            enabled=_env_bool("TRIDENT_ENABLE_POD_A", bool(pod_a_data.get("enabled", True))),
            allowed_market_clusters=_str_list(
                pod_a_data.get("allowed_market_clusters", ["crypto"]),
                upper_values=False,
            ),
            max_allocation_pct=float(pod_a_data.get("max_allocation_pct", 1.0)),
            default_leverage=float(pod_a_data.get("default_leverage", 1.0)),
            max_leverage=float(pod_a_data.get("max_leverage", 1.0)),
            max_leverage_by_symbol=_float_map(pod_a_data.get("max_leverage_by_symbol", {})),
            prefer_isolated=bool(pod_a_data.get("prefer_isolated", True)),
            sizing_mode=str(pod_a_data.get("sizing_mode", "allocation_only")),
            risk_per_trade_pct=float(pod_a_data.get("risk_per_trade_pct", 0.01)),
            min_margin_usd=float(pod_a_data.get("min_margin_usd", 25.0)),
            min_notional_usd=float(pod_a_data.get("min_notional_usd", 50.0)),
            allow_partial_take_profit=bool(
                pod_a_data.get("allow_partial_take_profit", False)
            ),
            allow_break_even=bool(pod_a_data.get("allow_break_even", False)),
            disabled_setups=_str_list(pod_a_data.get("disabled_setups", [])),
            blocked_regimes=_str_list(pod_a_data.get("blocked_regimes", [])),
            allowed_setups_in_blocked_regimes=_str_list(
                pod_a_data.get("allowed_setups_in_blocked_regimes", [])
            ),
            symbol_modes=_pod_a_symbol_modes(pod_a_data.get("symbol_modes", {})),
        ),
        pod_b=PodBConfig(
            enabled=_env_bool("TRIDENT_ENABLE_POD_B", bool(pod_b_data.get("enabled", False))),
            allowed_market_clusters=_str_list(
                pod_b_data.get("allowed_market_clusters", ["crypto"]),
                upper_values=False,
            ),
            max_allocation_pct=float(pod_b_data.get("max_allocation_pct", 1.0)),
            bis_min_confidence=float(pod_b_data.get("bis_min_confidence", 0.58)),
            bis_default_leverage=float(pod_b_data.get("bis_default_leverage", 2.0)),
            bis_max_leverage=float(pod_b_data.get("bis_max_leverage", 20.0)),
            bis_max_leverage_by_symbol=_float_map(
                pod_b_data.get("bis_max_leverage_by_symbol", {})
            ),
            bis_risk_per_trade_pct=float(
                pod_b_data.get("bis_risk_per_trade_pct", 0.01)
            ),
            bis_min_margin_usd=float(pod_b_data.get("bis_min_margin_usd", 20.0)),
            bis_min_notional_usd=float(pod_b_data.get("bis_min_notional_usd", 10.0)),
            bis_reentry_cooldown_minutes=int(
                pod_b_data.get("bis_reentry_cooldown_minutes", 45)
            ),
            bis_time_stop_hours=int(pod_b_data.get("bis_time_stop_hours", 2)),
            bis_max_spread_bps=float(pod_b_data.get("bis_max_spread_bps", 8.0)),
            bis_min_bucket_notional_usd=float(
                pod_b_data.get("bis_min_bucket_notional_usd", 100.0)
            ),
            bis_min_bucket_trade_count=int(
                pod_b_data.get("bis_min_bucket_trade_count", 3)
            ),
            bis_min_compression_score=float(
                pod_b_data.get("bis_min_compression_score", 0.55)
            ),
            bis_min_activity_score=float(
                pod_b_data.get("bis_min_activity_score", 0.55)
            ),
            bis_min_breakout_score=float(
                pod_b_data.get("bis_min_breakout_score", 0.45)
            ),
            bis_min_volume_ratio=float(pod_b_data.get("bis_min_volume_ratio", 1.2)),
            bis_min_trade_count_ratio=float(
                pod_b_data.get("bis_min_trade_count_ratio", 1.1)
            ),
            bis_max_chase_distance_bps=float(
                pod_b_data.get("bis_max_chase_distance_bps", 35.0)
            ),
            bis_allowed_regimes=_str_list(
                pod_b_data.get(
                    "bis_allowed_regimes",
                    ["TrendExpansion", "PanicSqueeze"],
                )
            ),
            bis_min_abs_structure_score=float(
                pod_b_data.get("bis_min_abs_structure_score", 0.15)
            ),
            bis_min_trend_quality_bps=float(
                pod_b_data.get("bis_min_trend_quality_bps", 6.0)
            ),
            bis_min_realized_vol_short_bps=float(
                pod_b_data.get("bis_min_realized_vol_short_bps", 6.0)
            ),
            bis_min_directional_vwap_distance_bps=float(
                pod_b_data.get("bis_min_directional_vwap_distance_bps", 4.0)
            ),
            bis_stop_floor_bps=float(pod_b_data.get("bis_stop_floor_bps", 18.0)),
            bis_stop_ceiling_bps=float(pod_b_data.get("bis_stop_ceiling_bps", 80.0)),
            bis_enable_longs=bool(pod_b_data.get("bis_enable_longs", True)),
            bis_enable_shorts=bool(pod_b_data.get("bis_enable_shorts", False)),
            bis_strict_continuation_filter_enabled=bool(
                pod_b_data.get("bis_strict_continuation_filter_enabled", False)
            ),
            bis_enabled_setups=_str_list(
                pod_b_data.get("bis_enabled_setups", ["vol_expansion_long"])
            ),
            bis_max_concurrent_positions=int(
                pod_b_data.get("bis_max_concurrent_positions", 4)
            ),
            bis_max_total_open_risk_pct=float(
                pod_b_data.get("bis_max_total_open_risk_pct", 0.02)
            ),
        ),
        pod_c=PodCConfig(
            enabled=_env_bool("TRIDENT_ENABLE_POD_C", bool(pod_c_data.get("enabled", False))),
            allowed_market_clusters=_str_list(
                pod_c_data.get("allowed_market_clusters", ["index", "gold", "silver", "equity"]),
                upper_values=False,
            ),
            cluster_aware_v2_enabled=bool(
                pod_c_data.get("cluster_aware_v2_enabled", False)
            ),
            max_allocation_pct=float(pod_c_data.get("max_allocation_pct", 0.90)),
            default_leverage=float(pod_c_data.get("default_leverage", 1.0)),
            max_leverage=float(pod_c_data.get("max_leverage", 1.0)),
            max_leverage_by_symbol=_float_map(pod_c_data.get("max_leverage_by_symbol", {})),
            max_spread_bps=float(pod_c_data.get("max_spread_bps", 6.0)),
            max_abs_funding_rate=float(pod_c_data.get("max_abs_funding_rate", 0.01)),
            min_confidence=float(pod_c_data.get("min_confidence", 0.62)),
            size_multiplier=float(pod_c_data.get("size_multiplier", 0.55)),
            risk_per_trade_pct=float(pod_c_data.get("risk_per_trade_pct", 0.01)),
            min_margin_usd=float(pod_c_data.get("min_margin_usd", 25.0)),
            min_notional_usd=float(pod_c_data.get("min_notional_usd", 50.0)),
            reentry_cooldown_minutes=int(pod_c_data.get("reentry_cooldown_minutes", 90)),
            time_stop_hours=int(pod_c_data.get("time_stop_hours", 3)),
            blocked_symbols=_str_list(
                pod_c_data.get("blocked_symbols", []),
                upper_values=True,
            ),
            min_bucket_notional_usd=float(
                pod_c_data.get("min_bucket_notional_usd", 100.0)
            ),
            min_bucket_trade_count=int(
                pod_c_data.get("min_bucket_trade_count", 3)
            ),
            min_trend_bps=float(pod_c_data.get("min_trend_bps", 10.0)),
            min_structure_score=float(
                pod_c_data.get("min_structure_score", 0.20)
            ),
            max_vwap_distance_bps=float(
                pod_c_data.get("max_vwap_distance_bps", 30.0)
            ),
            min_reclaim_distance_bps=float(
                pod_c_data.get("min_reclaim_distance_bps", 6.0)
            ),
            min_activity_ratio=float(
                pod_c_data.get("min_activity_ratio", 0.75)
            ),
            activity_lookback=int(pod_c_data.get("activity_lookback", pod_c_data.get("squeeze_lookback", 20))),
        ),
    )


def override_app_config(
    config: AppConfig,
    *,
    reference_equity_usd: float | None = None,
    pod_a_default_leverage: float | None = None,
    pod_a_max_leverage: float | None = None,
    pod_a_max_leverage_by_symbol: dict[str, float] | None = None,
    pod_a_risk_per_trade_pct: float | None = None,
    pod_a_min_margin_usd: float | None = None,
    pod_a_min_notional_usd: float | None = None,
    pod_b_bis_default_leverage: float | None = None,
    pod_b_bis_max_leverage: float | None = None,
    pod_b_bis_max_leverage_by_symbol: dict[str, float] | None = None,
    pod_b_bis_risk_per_trade_pct: float | None = None,
    pod_b_bis_min_margin_usd: float | None = None,
    pod_b_bis_min_notional_usd: float | None = None,
    pod_c_default_leverage: float | None = None,
    pod_c_max_leverage: float | None = None,
    pod_c_max_leverage_by_symbol: dict[str, float] | None = None,
    pod_c_risk_per_trade_pct: float | None = None,
    pod_c_min_margin_usd: float | None = None,
    pod_c_min_notional_usd: float | None = None,
) -> AppConfig:
    capital = replace(
        config.trident.capital,
        reference_equity_usd=(
            config.trident.capital.reference_equity_usd
            if reference_equity_usd is None
            else float(reference_equity_usd)
        ),
    )
    trident = replace(config.trident, capital=capital)
    pod_a = replace(
        config.pod_a,
        default_leverage=(
            config.pod_a.default_leverage
            if pod_a_default_leverage is None
            else float(pod_a_default_leverage)
        ),
        max_leverage=(
            config.pod_a.max_leverage
            if pod_a_max_leverage is None
            else float(pod_a_max_leverage)
        ),
        max_leverage_by_symbol=(
            dict(config.pod_a.max_leverage_by_symbol)
            if pod_a_max_leverage_by_symbol is None
            else _float_map(pod_a_max_leverage_by_symbol)
        ),
        risk_per_trade_pct=(
            config.pod_a.risk_per_trade_pct
            if pod_a_risk_per_trade_pct is None
            else float(pod_a_risk_per_trade_pct)
        ),
        min_margin_usd=(
            config.pod_a.min_margin_usd
            if pod_a_min_margin_usd is None
            else float(pod_a_min_margin_usd)
        ),
        min_notional_usd=(
            config.pod_a.min_notional_usd
            if pod_a_min_notional_usd is None
            else float(pod_a_min_notional_usd)
        ),
    )
    pod_b = replace(
        config.pod_b,
        bis_default_leverage=(
            config.pod_b.bis_default_leverage
            if pod_b_bis_default_leverage is None
            else float(pod_b_bis_default_leverage)
        ),
        bis_max_leverage=(
            config.pod_b.bis_max_leverage
            if pod_b_bis_max_leverage is None
            else float(pod_b_bis_max_leverage)
        ),
        bis_max_leverage_by_symbol=(
            dict(config.pod_b.bis_max_leverage_by_symbol)
            if pod_b_bis_max_leverage_by_symbol is None
            else _float_map(pod_b_bis_max_leverage_by_symbol)
        ),
        bis_risk_per_trade_pct=(
            config.pod_b.bis_risk_per_trade_pct
            if pod_b_bis_risk_per_trade_pct is None
            else float(pod_b_bis_risk_per_trade_pct)
        ),
        bis_min_margin_usd=(
            config.pod_b.bis_min_margin_usd
            if pod_b_bis_min_margin_usd is None
            else float(pod_b_bis_min_margin_usd)
        ),
        bis_min_notional_usd=(
            config.pod_b.bis_min_notional_usd
            if pod_b_bis_min_notional_usd is None
            else float(pod_b_bis_min_notional_usd)
        ),
    )
    pod_c = replace(
        config.pod_c,
        default_leverage=(
            config.pod_c.default_leverage
            if pod_c_default_leverage is None
            else float(pod_c_default_leverage)
        ),
        max_leverage=(
            config.pod_c.max_leverage
            if pod_c_max_leverage is None
            else float(pod_c_max_leverage)
        ),
        max_leverage_by_symbol=(
            dict(config.pod_c.max_leverage_by_symbol)
            if pod_c_max_leverage_by_symbol is None
            else _float_map(pod_c_max_leverage_by_symbol)
        ),
        risk_per_trade_pct=(
            config.pod_c.risk_per_trade_pct
            if pod_c_risk_per_trade_pct is None
            else float(pod_c_risk_per_trade_pct)
        ),
        min_margin_usd=(
            config.pod_c.min_margin_usd
            if pod_c_min_margin_usd is None
            else float(pod_c_min_margin_usd)
        ),
        min_notional_usd=(
            config.pod_c.min_notional_usd
            if pod_c_min_notional_usd is None
            else float(pod_c_min_notional_usd)
        ),
    )
    return replace(config, trident=trident, pod_a=pod_a, pod_b=pod_b, pod_c=pod_c)
