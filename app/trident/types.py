from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(slots=True)
class RegimeTransition:
    recorded_at: str
    previous_regime: Regime
    new_regime: Regime
    snapshot: RegimeSnapshot


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
    source: str = ""


@dataclass(slots=True)
class SignalPreview:
    symbol: str
    side: str
    setup: str
    confidence: float


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
    regime_history: list[RegimeTransition] = field(default_factory=list)
    regime_evaluation_count: int = 0
    regime_transition_count: int = 0
    pod_a_signal_preview: list[SignalPreview] = field(default_factory=list)
    pod_c_signal_preview: list[SignalPreview] = field(default_factory=list)
    pod_b_status: dict[str, object] = field(default_factory=dict)
