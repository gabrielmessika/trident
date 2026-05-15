from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum


class Mode(StrEnum):
    OBSERVATION = "observation"
    DRY_RUN = "dry-run"
    LIVE = "live"


class Regime(StrEnum):
    TREND_EXPANSION = "TrendExpansion"
    RANGE_AUCTION = "RangeAuction"
    PANIC_SQUEEZE = "PanicSqueeze"
    DEAD_ZONE = "DeadZone"
    CASH = "Cash"


class SymbolLocalRegime(StrEnum):
    TREND_STRUCTURE = "TrendStructure"
    RANGE_STRUCTURE = "RangeStructure"
    EVENT_IMPULSE = "EventImpulse"
    NEUTRAL = "Neutral"


class PodName(StrEnum):
    POD_A = "pod_a"
    POD_B = "pod_b"
    POD_C = "pod_c"


@dataclass(slots=True)
class PodHealth:
    pod: PodName
    healthy: bool = True
    message: str = "not_started"


@dataclass(slots=True)
class PodPosition:
    pod: PodName
    symbol: str
    size: Decimal = Decimal("0")
    side: str = "flat"


@dataclass(slots=True)
class PodIntent:
    pod: PodName
    action: str
    symbol: str | None = None
    reason: str = ""


@dataclass(slots=True)
class OwnershipConflict:
    symbol: str
    requested_by: PodName
    owner: PodName


@dataclass(slots=True)
class SymbolRoutingDecision:
    symbol: str
    owner: PodName | None
    mode: str
    reason: str
    previous_owner: PodName | None = None
    candidate_pods: list[PodName] = field(default_factory=list)
    pod_scores: dict[PodName, float] = field(default_factory=dict)
    local_regime: SymbolLocalRegime | None = None
    local_regime_reason: str = ""
    pod_reasoning: dict[PodName, str] = field(default_factory=dict)
    reassignment_cooldown_active: bool = False
    reassignment_cooldown_remaining_seconds: float = 0.0
    override_active: bool = False
    override_owner: PodName | None = None


@dataclass(slots=True)
class ObservedSymbolStatus:
    symbol: str
    tradable: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RegimeSnapshot:
    ready: bool = False
    adx: float = 0.0
    atr_ratio: float = 0.0
    range_width_bps: float = 0.0
    structure_score: float = 0.0
    btc_impulse: bool = False
    leader_symbol: str = ""
    symbol_count: int = 0
    active_symbol_count: int = 0
    aligned_symbol_count: int = 0
    breadth_pct: float = 0.0
    alt_participation_pct: float = 0.0
    dispersion_pct: float = 0.0
    leader_trend_score: float = 0.0
    coherence_score: float = 0.0


@dataclass(slots=True)
class RegimeTransition:
    recorded_at: str
    previous_regime: Regime
    new_regime: Regime
    snapshot: RegimeSnapshot


@dataclass(slots=True)
class LocalSymbolState:
    symbol: str
    local_regime: SymbolLocalRegime
    reason: str
    owner: PodName | None = None
    previous_owner: PodName | None = None
    override_active: bool = False
    override_owner: PodName | None = None
    global_alignment: str = "unknown"
    pod_scores: dict[PodName, float] = field(default_factory=dict)


@dataclass(slots=True)
class LocalSymbolTransition:
    recorded_at: str
    symbol: str
    previous_local_regime: SymbolLocalRegime | None
    new_local_regime: SymbolLocalRegime
    reason: str


@dataclass(slots=True)
class RegimeDecision:
    raw_regime: Regime
    effective_regime: Regime
    pending_regime: Regime | None
    pending_count: int
    switched: bool = False


@dataclass(slots=True)
class SymbolAllocation:
    symbol: str
    target_pct: float
    target_usd: float
    reason_summary: str = ""
    correlation_group: str = ""
    correlation_density_factor: float = 1.0
    capped_by_correlation: bool = False


@dataclass(slots=True)
class SymbolMarketSnapshot:
    symbol: str
    price: float
    ema_fast: float
    ema_slow: float
    vwap_distance_bps: float
    structure_score: float
    funding_rate: float
    spread_bps: float
    btc_aligned: bool
    market_cluster: str = "crypto"
    cluster_aligned: bool = True
    cluster_leader: str = ""
    book_imbalance: float = 0.0
    trade_flow_bias: float = 0.0
    bucket_volume: float = 0.0
    bucket_trade_count: int = 0
    bucket_range_bps: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    best_bid_size: float = 0.0
    best_ask_size: float = 0.0
    bid_depth_10bps: float = 0.0
    ask_depth_10bps: float = 0.0
    bid_depth_velocity: float = 0.0
    ask_depth_velocity: float = 0.0
    best_bid_size_velocity: float = 0.0
    best_ask_size_velocity: float = 0.0
    microprice: float = 0.0
    microprice_dislocation_bps: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    vwap: float | None = None
    bucket_notional_usd: float = 0.0
    signed_trade_delta: float = 0.0
    delta_spread_bps: float = 0.0
    delta_book_imbalance: float = 0.0
    delta_trade_flow_bias: float = 0.0
    volume_ratio: float = 1.0
    trade_count_ratio: float = 1.0
    realized_vol_short_bps: float = 0.0
    realized_vol_long_bps: float = 0.0
    compression_score: float = 0.0
    open_interest: float | None = None
    mark_px: float | None = None
    oracle_px: float | None = None
    premium: float | None = None
    day_ntl_vlm: float | None = None
    day_base_vlm: float | None = None
    asset_ctx_observation_age_seconds: float | None = None
    external_reference_price: float | None = None
    external_reference_source_count: int = 0
    external_reference_sources: str = ""
    external_reference_symbol: str = ""
    external_reference_time: str = ""
    external_reference_age_seconds: float | None = None
    external_reference_max_deviation_bps: float = 0.0
    external_premium_bps: float = 0.0
    external_momentum_60s_bps: float = 0.0
    external_momentum_300s_bps: float = 0.0
    external_alignment_score: float = 0.0
    source: str = ""


_SYMBOL_MARKET_SNAPSHOT_FIELD_NAMES = {item.name for item in fields(SymbolMarketSnapshot)}


def symbol_market_snapshot_from_mapping(payload: dict[str, object]) -> SymbolMarketSnapshot:
    return SymbolMarketSnapshot(
        **{
            key: value
            for key, value in payload.items()
            if key in _SYMBOL_MARKET_SNAPSHOT_FIELD_NAMES
        }
    )


@dataclass(slots=True)
class SignalPreview:
    symbol: str
    side: str
    setup: str
    confidence: float
    reason_summary: str = ""
    setup_details: dict[str, float | str | bool] = field(default_factory=dict)
    confidence_components: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class TradePlan:
    symbol: str
    side: str
    setup: str
    confidence: float
    target_notional_usd: float
    stop_bps: float
    time_stop_hours: int
    take_profit_bps: float = 0.0
    break_even_trigger_bps: float = 0.0
    trailing_activation_bps: float = 0.0
    trailing_distance_bps: float = 0.0
    reentry_cooldown_minutes: int = 0
    confidence_components: dict[str, float] = field(default_factory=dict)
    margin_usd: float = 0.0
    requested_leverage: float = 1.0
    effective_leverage: float = 1.0
    risk_budget_usd: float = 0.0
    expected_loss_usd: float = 0.0
    invalidation_price: float | None = None
    isolated: bool = True
    setup_details: dict[str, float | str | bool] = field(default_factory=dict)


@dataclass(slots=True)
class RiskDecision:
    accepted: bool
    reason: str
    trade_plan: TradePlan


@dataclass(slots=True)
class PodAllocation:
    pod: PodName
    target_pct: float
    target_usd: float
    capped_by_pod_limit: bool = False
    symbols: list[SymbolAllocation] = field(default_factory=list)


@dataclass(slots=True)
class CapitalPlan:
    regime: Regime
    total_equity_usd: float
    cash_pct: float
    cash_usd: float
    pod_allocations: dict[PodName, PodAllocation] = field(default_factory=dict)


@dataclass(slots=True)
class SupervisorState:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    regime: Regime = Regime.CASH
    raw_regime: Regime = Regime.CASH
    pending_regime: Regime | None = None
    pending_regime_count: int = 0
    mode: str = Mode.OBSERVATION.value
    profile: str = "trident"
    enabled_pods: list[PodName] = field(default_factory=list)
    ownership_conflicts: list[OwnershipConflict] = field(default_factory=list)
    symbol_routing: list[SymbolRoutingDecision] = field(default_factory=list)
    observed_symbol_status: list[ObservedSymbolStatus] = field(default_factory=list)
    regime_snapshot: RegimeSnapshot = field(default_factory=RegimeSnapshot)
    cluster_regimes: dict[str, Regime] = field(default_factory=dict)
    cluster_regime_snapshots: dict[str, RegimeSnapshot] = field(default_factory=dict)
    cluster_pending_regimes: dict[str, Regime | None] = field(default_factory=dict)
    cluster_pending_counts: dict[str, int] = field(default_factory=dict)
    regime_history: list[RegimeTransition] = field(default_factory=list)
    regime_evaluation_count: int = 0
    regime_transition_count: int = 0
    local_regime_by_symbol: list[LocalSymbolState] = field(default_factory=list)
    local_regime_transitions: list[LocalSymbolTransition] = field(default_factory=list)
    symbol_reassignment_count_by_symbol: dict[str, int] = field(default_factory=dict)
    symbol_last_reassignment_at: dict[str, str] = field(default_factory=dict)
    runtime_symbol_pod_overrides: dict[str, str] = field(default_factory=dict)
    runtime_symbol_pod_overrides_updated_at: str | None = None
    pod_a_signal_preview: list[SignalPreview] = field(default_factory=list)
    pod_b_signal_preview: list[SignalPreview] = field(default_factory=list)
    pod_c_signal_preview: list[SignalPreview] = field(default_factory=list)
    pod_a_signal_review: list[dict[str, object]] = field(default_factory=list)
    pod_b_signal_review: list[dict[str, object]] = field(default_factory=list)
    pod_c_signal_review: list[dict[str, object]] = field(default_factory=list)
    pod_b_status: dict[str, object] = field(default_factory=dict)
