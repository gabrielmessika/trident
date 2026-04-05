from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
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


@dataclass(slots=True)
class ExecutionConfig:
    dry_run_taker_fee_bps: float = 3.5
    dry_run_slippage_bps: float = 0.5
    dry_run_spread_multiplier: float = 0.5


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
    bucket_ms: int = 60_000
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


@dataclass(slots=True)
class TridentConfigSection:
    enabled: bool
    regime: RegimeThresholds
    capital: CapitalLimits
    risk: RiskLimits
    execution: ExecutionConfig
    allocations: RegimeAllocations


@dataclass(slots=True)
class PodAConfig:
    enabled: bool
    symbols: list[str]
    max_allocation_pct: float


@dataclass(slots=True)
class PodBConfig:
    enabled: bool
    symbols: list[str]
    passivbot_config_path: str
    launch_command: list[str]
    launch_workdir: str
    max_allocation_pct: float
    paper_quote_width_bps: float
    paper_order_size_pct: float
    paper_max_inventory_skew_pct: float
    paper_maker_fee_bps: float
    paper_recent_fills_limit: int


@dataclass(slots=True)
class PodCConfig:
    enabled: bool
    leader_symbols: list[str]
    follower_symbols: list[str]
    max_allocation_pct: float
    impulse_threshold_bps: float
    min_lag_bps: float
    max_spread_bps: float
    time_stop_hours: int


@dataclass(slots=True)
class AppConfig:
    general: GeneralConfig
    hyperliquid: HyperliquidConfig
    trident: TridentConfigSection
    pod_a: PodAConfig
    pod_b: PodBConfig
    pod_c: PodCConfig


def _allocations(data: dict[str, object], name: str) -> AllocationConfig:
    section = data[name]
    return AllocationConfig(
        pod_a=float(section.get("pod_a", 0.0)),
        pod_b=float(section.get("pod_b", 0.0)),
        pod_c=float(section.get("pod_c", 0.0)),
        cash=float(section.get("cash", 0.0)),
    )


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
            bucket_ms=int(hyperliquid_data.get("bucket_ms", 60_000)),
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
            ),
            allocations=RegimeAllocations(
                trend_expansion=_allocations(allocations_data, "trend_expansion"),
                range_auction=_allocations(allocations_data, "range_auction"),
                panic_squeeze=_allocations(allocations_data, "panic_squeeze"),
                dead_zone=_allocations(allocations_data, "dead_zone"),
            ),
        ),
        pod_a=PodAConfig(
            enabled=bool(pod_a_data.get("enabled", True)),
            symbols=list(pod_a_data.get("symbols", [])),
            max_allocation_pct=float(pod_a_data.get("max_allocation_pct", 1.0)),
        ),
        pod_b=PodBConfig(
            enabled=bool(pod_b_data.get("enabled", False)),
            symbols=list(pod_b_data.get("symbols", [])),
            passivbot_config_path=str(
                pod_b_data.get("passivbot_config_path", "./runtime/passivbot/live.json")
            ),
            launch_command=list(pod_b_data.get("launch_command", [])),
            launch_workdir=str(pod_b_data.get("launch_workdir", "")),
            max_allocation_pct=float(pod_b_data.get("max_allocation_pct", 1.0)),
            paper_quote_width_bps=float(pod_b_data.get("paper_quote_width_bps", 6.0)),
            paper_order_size_pct=float(pod_b_data.get("paper_order_size_pct", 0.25)),
            paper_max_inventory_skew_pct=float(
                pod_b_data.get("paper_max_inventory_skew_pct", 1.0)
            ),
            paper_maker_fee_bps=float(pod_b_data.get("paper_maker_fee_bps", 0.0)),
            paper_recent_fills_limit=int(pod_b_data.get("paper_recent_fills_limit", 20)),
        ),
        pod_c=PodCConfig(
            enabled=bool(pod_c_data.get("enabled", False)),
            leader_symbols=list(pod_c_data.get("leader_symbols", [])),
            follower_symbols=list(pod_c_data.get("follower_symbols", [])),
            max_allocation_pct=float(pod_c_data.get("max_allocation_pct", 1.0)),
            impulse_threshold_bps=float(pod_c_data.get("impulse_threshold_bps", 10.0)),
            min_lag_bps=float(pod_c_data.get("min_lag_bps", 4.0)),
            max_spread_bps=float(pod_c_data.get("max_spread_bps", 6.0)),
            time_stop_hours=int(pod_c_data.get("time_stop_hours", 4)),
        ),
    )
