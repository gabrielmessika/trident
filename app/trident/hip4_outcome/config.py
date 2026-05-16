from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


def _str_list(raw: object, *, upper: bool = False) -> list[str]:
    if isinstance(raw, str):
        raw = [item for item in raw.split(",")]
    if not isinstance(raw, list):
        return []
    parsed: list[str] = []
    for item in raw:
        value = str(item).strip()
        if not value:
            continue
        parsed.append(value.upper() if upper else value)
    return parsed


def _slice_list(raw: object) -> list[str]:
    parsed: list[str] = []
    for item in _str_list(raw, upper=False):
        parts = [part.strip().upper() for part in item.replace("/", ":").split(":")]
        if len(parts) != 3 or not all(parts):
            continue
        parsed.append(":".join(parts))
    return parsed


def _str_list_map(raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, list[str]] = {}
    for key, value in raw.items():
        items = _str_list(value)
        if items:
            parsed[str(key).strip().upper()] = items
    return parsed


def _int_list(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    parsed: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            parsed.append(value)
    return parsed


def _float_map(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {str(key).strip().upper(): float(value) for key, value in raw.items()}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class Hip4OutcomeConfig:
    """Configuration for the HIP-4 outcome Pod B replacement.

    This pod is intentionally independent from the directional Trident Pod B
    pipeline because outcome tokens use spot-like assets and binary settlement.
    """

    mode: str = "observer"  # observer | paper | testnet
    info_url: str = "https://api.hyperliquid-testnet.xyz/info"
    ws_url: str = "wss://api.hyperliquid-testnet.xyz/ws"
    rate_limit_state_path: str = "./runtime/hip4_outcome_rate_limits.json"
    logs_dir: str = "./logs/hip4_outcome"
    state_path: str = "./runtime/hip4_outcome_state.json"
    status_path: str = "./logs/hip4_outcome_status.json"
    write_pod_b_alias_status: bool = True
    pod_b_alias_status_path: str = "./logs/pod_b_live_status.json"
    loop_interval_seconds: float = 15.0
    request_timeout_seconds: float = 10.0
    info_requests_per_minute: int = 90
    include_underlyings: list[str] = field(default_factory=lambda: ["BTC", "ETH", "HYPE"])
    reference_price_sources: list[str] = field(
        default_factory=lambda: ["binance", "okx", "bybit", "coinbase", "kraken", "hyperliquid"]
    )
    reference_price_sources_by_underlying: dict[str, list[str]] = field(default_factory=dict)
    anchor_reference_to_hyperliquid: bool = True
    max_source_deviation_bps: float = 50.0
    min_reference_sources: int = 1
    external_price_timeout_seconds: float = 5.0
    external_price_user_agent: str = "trident-hip4-outcome/0.1"
    max_markets_per_loop: int = 20
    max_opportunities_per_loop: int = 2
    min_time_to_expiry_seconds: int = 20
    max_time_to_expiry_minutes: int = 1440
    estimated_fees: float = 0.002
    outcome_open_fee_rate: float = 0.0
    outcome_settlement_fee_rate: float = 0.002
    estimated_slippage: float = 0.005
    safety_margin: float = 0.01
    min_gross_edge: float = 0.025
    min_net_edge: float = 0.015
    late_expiry_window_minutes: int = 20
    strike_buffer_bps: float = 8.0
    max_late_yes_price: float = 0.97
    max_late_no_price: float = 0.97
    enable_late_expiry: bool = True
    enable_parity: bool = True
    enable_model: bool = True
    enable_short_expiry: bool = True
    enable_price_bucket: bool = True
    enable_named_outcome_basket: bool = False
    named_outcome_basket_min_count: int = 2
    enable_market_observation: bool = True
    observe_unsupported_books: bool = True
    max_observation_markets_per_loop: int = 40
    max_observation_books_per_loop: int = 20
    enable_embedded_observers: bool = False
    embedded_observer_config_paths: list[str] = field(default_factory=list)
    embedded_observer_once_timeout_seconds: float = 20.0
    blocked_opportunity_slices: list[str] = field(default_factory=list)
    block_reference_divergence: bool = False
    reference_divergence_max_bps: float = 50.0
    reference_divergence_min_rejected_sources: int = 1
    reference_divergence_underlyings: list[str] = field(default_factory=list)
    reference_divergence_sides: list[str] = field(default_factory=list)
    reference_divergence_edge_types: list[str] = field(default_factory=list)
    short_expiry_window_minutes: int = 6
    short_expiry_periods: list[str] = field(default_factory=lambda: ["5m", "15m"])
    short_expiry_history_seconds: int = 900
    short_expiry_min_history_seconds: int = 45
    short_expiry_price_history_limit: int = 720
    short_expiry_momentum_windows_seconds: list[int] = field(default_factory=lambda: [30, 60, 180])
    short_expiry_primary_momentum_seconds: int = 60
    short_expiry_min_abs_momentum_bps: float = 1.0
    short_expiry_momentum_scale_bps: float = 12.0
    short_expiry_distance_scale_bps: float = 30.0
    short_expiry_distance_weight: float = 0.22
    short_expiry_momentum_weight: float = 0.22
    short_expiry_microstructure_weight: float = 0.16
    short_expiry_book_imbalance_weight: float = 0.08
    short_expiry_static_model_weight: float = 0.32
    short_expiry_min_confidence: float = 0.55
    short_expiry_max_yes_price: float = 0.92
    short_expiry_max_no_price: float = 0.92
    short_expiry_require_momentum_alignment: bool = True
    short_expiry_watchlist_limit: int = 8
    price_bucket_max_yes_price: float = 0.98
    price_bucket_max_no_price: float = 0.98
    default_annualized_vol: float = 0.85
    annualized_vol_by_underlying: dict[str, float] = field(
        default_factory=lambda: {"BTC": 0.65, "ETH": 0.75, "HYPE": 1.25}
    )
    max_position_usdc: float = 5.0
    pod_b_budget_usdc: float = 25.0
    max_total_outcome_exposure_usdc: float = 25.0
    max_per_underlying_outcome_exposure_usdc: float = 10.0
    max_outcome_markets_open: int = 3
    min_yes_depth_usdc: float = 2.0
    min_no_depth_usdc: float = 2.0
    max_spread: float = 0.60
    max_order_slippage: float = 0.03
    min_order_value_usdc: float = 10.0
    outcome_size_decimals: int = 0
    order_tif: str = "Ioc"
    allow_testnet_orders: bool = False
    enforce_testnet_balance_check: bool = True
    testnet_balance_coin: str = "USDH"
    testnet_balance_buffer_usdc: float = 1.0
    auto_transfer_testnet_spot_usdc: bool = False
    testnet_spot_transfer_target_usdc: float = 0.0
    require_testnet_url: bool = True
    settlement_grace_seconds: int = 300
    fills_lookback_hours: float = 24.0
    reconcile_after_execution: bool = True
    reconcile_every_loops: int = 1
    enable_latency_log: bool = True
    enable_edge_decay_log: bool = True
    enable_daily_summary: bool = True
    edge_decay_state_limit: int = 1000

    @property
    def max_time_to_expiry_seconds(self) -> int:
        return int(max(self.max_time_to_expiry_minutes, 0) * 60)

    @property
    def late_expiry_window_seconds(self) -> int:
        return int(max(self.late_expiry_window_minutes, 0) * 60)

    @property
    def short_expiry_window_seconds(self) -> int:
        return int(max(self.short_expiry_window_minutes, 0) * 60)

    def with_mode(self, mode: str | None) -> "Hip4OutcomeConfig":
        if not mode:
            return self
        return replace(self, mode=mode.strip().lower())


def load_hip4_outcome_config(path: str | Path | None = None, *, apply_env: bool = True) -> Hip4OutcomeConfig:
    config_path = Path(path or (os.getenv("HIP4_OUTCOME_CONFIG") if apply_env else None) or "config/hip4_outcome_mainnet_paper.toml")
    data: dict[str, object] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)

    section = data.get("hip4_outcome", {})
    if not isinstance(section, dict):
        section = {}
    hyperliquid = data.get("hyperliquid", {})
    if not isinstance(hyperliquid, dict):
        hyperliquid = {}

    configured_settlement_fee = float(
        section.get("outcome_settlement_fee_rate", section.get("estimated_fees", 0.002))
    )

    def env_str(name: str, default: object) -> str:
        if not apply_env:
            return str(default)
        return str(os.getenv(name, default))

    def env_bool(name: str, default: bool) -> bool:
        return _env_bool(name, default) if apply_env else bool(default)

    def env_float(name: str, default: float) -> float:
        return _env_float(name, default) if apply_env else float(default)

    def env_int(name: str, default: int) -> int:
        return _env_int(name, default) if apply_env else int(default)

    def env_value(name: str, default: object) -> object:
        if not apply_env:
            return default
        return os.getenv(name, default)

    return Hip4OutcomeConfig(
        mode=env_str("HIP4_OUTCOME_MODE", section.get("mode", "observer")).strip().lower(),
        info_url=env_str(
            "HIP4_OUTCOME_INFO_URL",
            hyperliquid.get("info_url", section.get("info_url", "https://api.hyperliquid-testnet.xyz/info")),
        ),
        ws_url=env_str(
            "HIP4_OUTCOME_WS_URL",
            hyperliquid.get("ws_url", section.get("ws_url", "wss://api.hyperliquid-testnet.xyz/ws")),
        ),
        rate_limit_state_path=str(
            section.get("rate_limit_state_path", "./runtime/hip4_outcome_rate_limits.json")
        ),
        logs_dir=str(section.get("logs_dir", "./logs/hip4_outcome")),
        state_path=str(section.get("state_path", "./runtime/hip4_outcome_state.json")),
        status_path=str(section.get("status_path", "./logs/hip4_outcome_status.json")),
        write_pod_b_alias_status=env_bool(
            "HIP4_OUTCOME_WRITE_POD_B_ALIAS_STATUS",
            bool(section.get("write_pod_b_alias_status", True)),
        ),
        pod_b_alias_status_path=str(
            section.get("pod_b_alias_status_path", "./logs/pod_b_live_status.json")
        ),
        loop_interval_seconds=float(section.get("loop_interval_seconds", 15.0)),
        request_timeout_seconds=float(section.get("request_timeout_seconds", 10.0)),
        info_requests_per_minute=int(section.get("info_requests_per_minute", 90)),
        include_underlyings=_str_list(
            section.get("include_underlyings", ["BTC", "ETH", "HYPE"]),
            upper=True,
        ),
        reference_price_sources=_str_list(
            section.get(
                "reference_price_sources",
                ["binance", "okx", "bybit", "coinbase", "kraken", "hyperliquid"],
            ),
            upper=False,
        ),
        reference_price_sources_by_underlying=_str_list_map(
            section.get("reference_price_sources_by_underlying", {})
        ),
        anchor_reference_to_hyperliquid=env_bool(
            "HIP4_OUTCOME_ANCHOR_REFERENCE_TO_HYPERLIQUID",
            bool(section.get("anchor_reference_to_hyperliquid", True)),
        ),
        max_source_deviation_bps=float(section.get("max_source_deviation_bps", 50.0)),
        min_reference_sources=int(section.get("min_reference_sources", 1)),
        external_price_timeout_seconds=float(section.get("external_price_timeout_seconds", 5.0)),
        external_price_user_agent=str(
            section.get("external_price_user_agent", "trident-hip4-outcome/0.1")
        ),
        max_markets_per_loop=int(section.get("max_markets_per_loop", 20)),
        max_opportunities_per_loop=int(section.get("max_opportunities_per_loop", 2)),
        min_time_to_expiry_seconds=int(section.get("min_time_to_expiry_seconds", 20)),
        max_time_to_expiry_minutes=int(section.get("max_time_to_expiry_minutes", 1440)),
        estimated_fees=float(section.get("estimated_fees", configured_settlement_fee)),
        outcome_open_fee_rate=env_float(
            "HIP4_OUTCOME_OPEN_FEE_RATE",
            float(section.get("outcome_open_fee_rate", 0.0)),
        ),
        outcome_settlement_fee_rate=env_float(
            "HIP4_OUTCOME_SETTLEMENT_FEE_RATE",
            configured_settlement_fee,
        ),
        estimated_slippage=float(section.get("estimated_slippage", 0.005)),
        safety_margin=float(section.get("safety_margin", 0.01)),
        min_gross_edge=float(section.get("min_gross_edge", 0.025)),
        min_net_edge=float(section.get("min_net_edge", 0.015)),
        late_expiry_window_minutes=int(section.get("late_expiry_window_minutes", 20)),
        strike_buffer_bps=float(section.get("strike_buffer_bps", 8.0)),
        max_late_yes_price=float(section.get("max_late_yes_price", 0.97)),
        max_late_no_price=float(section.get("max_late_no_price", 0.97)),
        enable_late_expiry=env_bool(
            "HIP4_OUTCOME_ENABLE_LATE_EXPIRY",
            bool(section.get("enable_late_expiry", True)),
        ),
        enable_parity=env_bool(
            "HIP4_OUTCOME_ENABLE_PARITY",
            bool(section.get("enable_parity", True)),
        ),
        enable_model=env_bool(
            "HIP4_OUTCOME_ENABLE_MODEL",
            bool(section.get("enable_model", True)),
        ),
        enable_short_expiry=env_bool(
            "HIP4_OUTCOME_ENABLE_SHORT_EXPIRY",
            bool(section.get("enable_short_expiry", True)),
        ),
        enable_price_bucket=env_bool(
            "HIP4_OUTCOME_ENABLE_PRICE_BUCKET",
            bool(section.get("enable_price_bucket", True)),
        ),
        enable_named_outcome_basket=env_bool(
            "HIP4_OUTCOME_ENABLE_NAMED_OUTCOME_BASKET",
            bool(section.get("enable_named_outcome_basket", False)),
        ),
        named_outcome_basket_min_count=env_int(
            "HIP4_OUTCOME_NAMED_OUTCOME_BASKET_MIN_COUNT",
            int(section.get("named_outcome_basket_min_count", 2)),
        ),
        enable_market_observation=env_bool(
            "HIP4_OUTCOME_ENABLE_MARKET_OBSERVATION",
            bool(section.get("enable_market_observation", True)),
        ),
        observe_unsupported_books=env_bool(
            "HIP4_OUTCOME_OBSERVE_UNSUPPORTED_BOOKS",
            bool(section.get("observe_unsupported_books", True)),
        ),
        max_observation_markets_per_loop=int(section.get("max_observation_markets_per_loop", 40)),
        max_observation_books_per_loop=int(section.get("max_observation_books_per_loop", 20)),
        enable_embedded_observers=env_bool(
            "HIP4_OUTCOME_ENABLE_EMBEDDED_OBSERVERS",
            bool(section.get("enable_embedded_observers", False)),
        ),
        embedded_observer_config_paths=_str_list(
            env_value(
                "HIP4_OUTCOME_EMBEDDED_OBSERVER_CONFIGS",
                section.get("embedded_observer_config_paths", []),
            )
        ),
        embedded_observer_once_timeout_seconds=float(
            section.get("embedded_observer_once_timeout_seconds", 20.0)
        ),
        blocked_opportunity_slices=_slice_list(
            env_value(
                "HIP4_OUTCOME_BLOCKED_OPPORTUNITY_SLICES",
                section.get("blocked_opportunity_slices", []),
            )
        ),
        block_reference_divergence=env_bool(
            "HIP4_OUTCOME_BLOCK_REFERENCE_DIVERGENCE",
            bool(section.get("block_reference_divergence", False)),
        ),
        reference_divergence_max_bps=env_float(
            "HIP4_OUTCOME_REFERENCE_DIVERGENCE_MAX_BPS",
            float(section.get("reference_divergence_max_bps", 50.0)),
        ),
        reference_divergence_min_rejected_sources=env_int(
            "HIP4_OUTCOME_REFERENCE_DIVERGENCE_MIN_REJECTED_SOURCES",
            int(section.get("reference_divergence_min_rejected_sources", 1)),
        ),
        reference_divergence_underlyings=_str_list(
            env_value(
                "HIP4_OUTCOME_REFERENCE_DIVERGENCE_UNDERLYINGS",
                section.get("reference_divergence_underlyings", []),
            ),
            upper=True,
        ),
        reference_divergence_sides=_str_list(
            env_value(
                "HIP4_OUTCOME_REFERENCE_DIVERGENCE_SIDES",
                section.get("reference_divergence_sides", []),
            ),
            upper=True,
        ),
        reference_divergence_edge_types=_str_list(
            env_value(
                "HIP4_OUTCOME_REFERENCE_DIVERGENCE_EDGE_TYPES",
                section.get("reference_divergence_edge_types", []),
            ),
            upper=True,
        ),
        short_expiry_window_minutes=int(section.get("short_expiry_window_minutes", 6)),
        short_expiry_periods=_str_list(section.get("short_expiry_periods", ["5m", "15m"])),
        short_expiry_history_seconds=int(section.get("short_expiry_history_seconds", 900)),
        short_expiry_min_history_seconds=int(section.get("short_expiry_min_history_seconds", 45)),
        short_expiry_price_history_limit=int(section.get("short_expiry_price_history_limit", 720)),
        short_expiry_momentum_windows_seconds=_int_list(
            section.get("short_expiry_momentum_windows_seconds", [30, 60, 180])
        ),
        short_expiry_primary_momentum_seconds=int(
            section.get("short_expiry_primary_momentum_seconds", 60)
        ),
        short_expiry_min_abs_momentum_bps=float(
            section.get("short_expiry_min_abs_momentum_bps", 1.0)
        ),
        short_expiry_momentum_scale_bps=float(
            section.get("short_expiry_momentum_scale_bps", 12.0)
        ),
        short_expiry_distance_scale_bps=float(
            section.get("short_expiry_distance_scale_bps", 30.0)
        ),
        short_expiry_distance_weight=float(section.get("short_expiry_distance_weight", 0.22)),
        short_expiry_momentum_weight=float(section.get("short_expiry_momentum_weight", 0.22)),
        short_expiry_microstructure_weight=float(
            section.get("short_expiry_microstructure_weight", 0.16)
        ),
        short_expiry_book_imbalance_weight=float(
            section.get("short_expiry_book_imbalance_weight", 0.08)
        ),
        short_expiry_static_model_weight=float(
            section.get("short_expiry_static_model_weight", 0.32)
        ),
        short_expiry_min_confidence=float(section.get("short_expiry_min_confidence", 0.55)),
        short_expiry_max_yes_price=float(section.get("short_expiry_max_yes_price", 0.92)),
        short_expiry_max_no_price=float(section.get("short_expiry_max_no_price", 0.92)),
        short_expiry_require_momentum_alignment=env_bool(
            "HIP4_OUTCOME_SHORT_EXPIRY_REQUIRE_MOMENTUM_ALIGNMENT",
            bool(section.get("short_expiry_require_momentum_alignment", True)),
        ),
        short_expiry_watchlist_limit=int(section.get("short_expiry_watchlist_limit", 8)),
        price_bucket_max_yes_price=float(section.get("price_bucket_max_yes_price", 0.98)),
        price_bucket_max_no_price=float(section.get("price_bucket_max_no_price", 0.98)),
        default_annualized_vol=float(section.get("default_annualized_vol", 0.85)),
        annualized_vol_by_underlying=_float_map(
            section.get(
                "annualized_vol_by_underlying",
                {"BTC": 0.65, "ETH": 0.75, "HYPE": 1.25},
            )
        ),
        max_position_usdc=float(section.get("max_position_usdc", 5.0)),
        pod_b_budget_usdc=env_float(
            "HIP4_OUTCOME_POD_B_BUDGET_USDC",
            float(
                section.get(
                    "pod_b_budget_usdc",
                    section.get("max_total_outcome_exposure_usdc", 25.0),
                )
            ),
        ),
        max_total_outcome_exposure_usdc=float(
            section.get("max_total_outcome_exposure_usdc", 25.0)
        ),
        max_per_underlying_outcome_exposure_usdc=float(
            section.get("max_per_underlying_outcome_exposure_usdc", 10.0)
        ),
        max_outcome_markets_open=int(section.get("max_outcome_markets_open", 3)),
        min_yes_depth_usdc=float(section.get("min_yes_depth_usdc", 2.0)),
        min_no_depth_usdc=float(section.get("min_no_depth_usdc", 2.0)),
        max_spread=float(section.get("max_spread", 0.60)),
        max_order_slippage=float(section.get("max_order_slippage", 0.03)),
        min_order_value_usdc=float(section.get("min_order_value_usdc", 10.0)),
        outcome_size_decimals=int(section.get("outcome_size_decimals", 0)),
        order_tif=str(section.get("order_tif", "Ioc")),
        allow_testnet_orders=env_bool(
            "HIP4_OUTCOME_ALLOW_TESTNET_ORDERS",
            bool(section.get("allow_testnet_orders", False)),
        ),
        enforce_testnet_balance_check=env_bool(
            "HIP4_OUTCOME_ENFORCE_TESTNET_BALANCE_CHECK",
            bool(section.get("enforce_testnet_balance_check", True)),
        ),
        testnet_balance_coin=env_str(
            "HIP4_OUTCOME_TESTNET_BALANCE_COIN",
            section.get("testnet_balance_coin", "USDH"),
        ).strip().upper(),
        testnet_balance_buffer_usdc=env_float(
            "HIP4_OUTCOME_TESTNET_BALANCE_BUFFER_USDC",
            float(section.get("testnet_balance_buffer_usdc", 1.0)),
        ),
        auto_transfer_testnet_spot_usdc=env_bool(
            "HIP4_OUTCOME_AUTO_TRANSFER_TESTNET_SPOT_USDC",
            bool(section.get("auto_transfer_testnet_spot_usdc", False)),
        ),
        testnet_spot_transfer_target_usdc=env_float(
            "HIP4_OUTCOME_TESTNET_SPOT_TRANSFER_TARGET_USDC",
            float(section.get("testnet_spot_transfer_target_usdc", 0.0)),
        ),
        require_testnet_url=bool(section.get("require_testnet_url", True)),
        settlement_grace_seconds=int(section.get("settlement_grace_seconds", 300)),
        fills_lookback_hours=float(section.get("fills_lookback_hours", 24.0)),
        reconcile_after_execution=env_bool(
            "HIP4_OUTCOME_RECONCILE_AFTER_EXECUTION",
            bool(section.get("reconcile_after_execution", True)),
        ),
        reconcile_every_loops=int(section.get("reconcile_every_loops", 1)),
        enable_latency_log=bool(section.get("enable_latency_log", True)),
        enable_edge_decay_log=bool(section.get("enable_edge_decay_log", True)),
        enable_daily_summary=bool(section.get("enable_daily_summary", True)),
        edge_decay_state_limit=int(section.get("edge_decay_state_limit", 1000)),
    )
