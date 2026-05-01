from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def outcome_encoding(outcome: int, side: int) -> int:
    if side not in {0, 1}:
        raise ValueError("HIP-4 outcome side must be 0 or 1")
    return int(outcome) * 10 + int(side)


def outcome_coin(outcome: int, side: int) -> str:
    return f"#{outcome_encoding(outcome, side)}"


def outcome_token_name(outcome: int, side: int) -> str:
    return f"+{outcome_encoding(outcome, side)}"


def outcome_asset_id(outcome: int, side: int) -> int:
    return 100_000_000 + outcome_encoding(outcome, side)


@dataclass(slots=True)
class OutcomeMarket:
    market_id: str
    outcome: int
    name: str
    description: str
    underlying: str
    strike: float
    expiry_ts: int
    period: str = ""
    class_name: str = "priceBinary"
    settlement_source: str = "hyperliquid_outcome"
    side_names: tuple[str, str] = ("Yes", "No")
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def yes_coin(self) -> str:
        return outcome_coin(self.outcome, 0)

    @property
    def no_coin(self) -> str:
        return outcome_coin(self.outcome, 1)

    @property
    def yes_asset_id(self) -> int:
        return outcome_asset_id(self.outcome, 0)

    @property
    def no_asset_id(self) -> int:
        return outcome_asset_id(self.outcome, 1)

    @property
    def expiry_iso(self) -> str:
        return datetime.fromtimestamp(self.expiry_ts, timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class BookLevel:
    price: float
    size: float
    order_count: int = 0

    @property
    def notional_usdc(self) -> float:
        return self.price * self.size


@dataclass(slots=True)
class OutcomeSideBook:
    coin: str
    bid: float | None
    ask: float | None
    bid_size: float = 0.0
    ask_size: float = 0.0
    bid_depth_usdc: float = 0.0
    ask_depth_usdc: float = 0.0
    time_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return max(self.ask - self.bid, 0.0)


@dataclass(slots=True)
class OutcomeOrderBook:
    market_id: str
    yes: OutcomeSideBook
    no: OutcomeSideBook
    observed_at: str = field(default_factory=utc_now_iso)

    @property
    def yes_ask(self) -> float | None:
        return self.yes.ask

    @property
    def no_ask(self) -> float | None:
        return self.no.ask


@dataclass(slots=True)
class ProbabilityEstimate:
    market_id: str
    probability_yes: float
    model_name: str
    confidence: float
    inputs: dict[str, float | str]


@dataclass(slots=True)
class ShortHorizonFeatures:
    underlying: str
    reference_price: float
    strike: float
    seconds_left: int
    sample_count: int
    history_span_seconds: int
    distance_to_strike_bps: float
    momentum_bps_by_window: dict[int, float | None] = field(default_factory=dict)
    realized_vol_bps_60s: float | None = None
    velocity_bps_per_minute: float | None = None
    has_min_history: bool = False

    def momentum_bps(self, window_seconds: int) -> float | None:
        return self.momentum_bps_by_window.get(int(window_seconds))

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "short_reference_price": self.reference_price,
            "short_strike": self.strike,
            "short_seconds_left": self.seconds_left,
            "short_sample_count": self.sample_count,
            "short_history_span_seconds": self.history_span_seconds,
            "short_distance_to_strike_bps": self.distance_to_strike_bps,
            "short_realized_vol_bps_60s": self.realized_vol_bps_60s,
            "short_velocity_bps_per_minute": self.velocity_bps_per_minute,
            "short_has_min_history": self.has_min_history,
        }
        for window, value in sorted(self.momentum_bps_by_window.items()):
            payload[f"short_momentum_bps_{int(window)}s"] = value
        return payload


@dataclass(slots=True)
class ShortExpiryAssessment:
    market_id: str
    underlying: str
    period: str
    probability_yes: float
    book_probability_yes: float
    book_imbalance_yes: float
    best_side: str
    best_gross_edge: float
    best_net_edge: float
    confidence: float
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutcomeOpportunity:
    market_id: str
    outcome: int
    underlying: str
    side: str  # BUY_YES | BUY_NO | BUY_BOTH
    edge_type: str  # LATE_EXPIRY | PARITY | MODEL | SHORT_EXPIRY
    gross_edge: float
    estimated_fees: float
    estimated_slippage: float
    net_edge: float
    confidence: float
    requested_size_usdc: float
    max_loss_usdc: float
    expiry_ts: int
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_signal(self) -> dict[str, Any]:
        return {
            "pod_name": "HIP4OutcomeEdgePod",
            "strategy_type": "OUTCOME_ARBITRAGE",
            "market_id": self.market_id,
            "underlying": self.underlying,
            "instrument_type": "HIP4_OUTCOME",
            "side": self.side,
            "edge_type": self.edge_type,
            "confidence": self.confidence,
            "gross_edge": self.gross_edge,
            "net_edge": self.net_edge,
            "requested_size_usdc": self.requested_size_usdc,
            "max_loss_usdc": self.max_loss_usdc,
            "expiry_ts": self.expiry_ts,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class SupervisorDecision:
    approved: bool
    approved_size_usdc: float
    reason: str
    execution_mode: str
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutcomeFill:
    coin: str
    side_name: str
    token_qty: Decimal
    avg_price: float
    cost_usdc: float
    status: str
    oid: int | None = None
    cloid: str | None = None
    raw: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["token_qty"] = str(self.token_qty)
        return payload


@dataclass(slots=True)
class OutcomeExecutionResult:
    status: str
    fills: list[OutcomeFill] = field(default_factory=list)
    error: str | None = None
    raw: Any | None = None

    @property
    def filled(self) -> bool:
        return any(fill.token_qty > 0 and fill.avg_price > 0 for fill in self.fills)

    @property
    def total_cost_usdc(self) -> float:
        return round(sum(fill.cost_usdc for fill in self.fills), 8)


@dataclass(slots=True)
class OutcomePosition:
    position_id: str
    market_id: str
    outcome: int
    underlying: str
    edge_type: str
    side: str
    opened_at: str
    expiry_ts: int
    cost_usdc: float
    max_loss_usdc: float
    net_edge: float
    confidence: float
    fills: list[OutcomeFill] = field(default_factory=list)
    status: str = "open"
    settled_at: str | None = None
    estimated_payout_usdc: float = 0.0
    estimated_pnl_usdc: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["fills"] = [fill.to_dict() for fill in self.fills]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OutcomePosition":
        fills: list[OutcomeFill] = []
        for item in payload.get("fills", []):
            if not isinstance(item, dict):
                continue
            fills.append(
                OutcomeFill(
                    coin=str(item.get("coin", "")),
                    side_name=str(item.get("side_name", "")),
                    token_qty=Decimal(str(item.get("token_qty", "0"))),
                    avg_price=float(item.get("avg_price", 0.0)),
                    cost_usdc=float(item.get("cost_usdc", 0.0)),
                    status=str(item.get("status", "")),
                    oid=(None if item.get("oid") is None else int(item.get("oid"))),
                    cloid=(None if item.get("cloid") is None else str(item.get("cloid"))),
                    raw=item.get("raw"),
                )
            )
        return cls(
            position_id=str(payload.get("position_id", "")),
            market_id=str(payload.get("market_id", "")),
            outcome=int(payload.get("outcome", 0)),
            underlying=str(payload.get("underlying", "")),
            edge_type=str(payload.get("edge_type", "")),
            side=str(payload.get("side", "")),
            opened_at=str(payload.get("opened_at", "")),
            expiry_ts=int(payload.get("expiry_ts", 0)),
            cost_usdc=float(payload.get("cost_usdc", 0.0)),
            max_loss_usdc=float(payload.get("max_loss_usdc", 0.0)),
            net_edge=float(payload.get("net_edge", 0.0)),
            confidence=float(payload.get("confidence", 0.0)),
            fills=fills,
            status=str(payload.get("status", "open")),
            settled_at=(
                None if payload.get("settled_at") in (None, "") else str(payload.get("settled_at"))
            ),
            estimated_payout_usdc=float(payload.get("estimated_payout_usdc", 0.0)),
            estimated_pnl_usdc=float(payload.get("estimated_pnl_usdc", 0.0)),
            metadata=dict(payload.get("metadata", {})) if isinstance(payload.get("metadata"), dict) else {},
        )
