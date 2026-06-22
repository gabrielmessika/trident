from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone


TRIDENT_AI_INITIAL_SYMBOLS: tuple[str, ...] = ("BTC", "ETH", "SOL", "HYPE")
TRIDENT_AI_PROPOSAL_SCHEMA_VERSION = "trident_ai_proposal_v1"
TRIDENT_AI_MARKET_CONTEXT_SCHEMA_VERSION = "trident_ai_market_context_v1"
TRIDENT_AI_INTEL_DIGEST_SCHEMA_VERSION = "trident_ai_intel_digest_v1"

_ALLOWED_ACTIONS = {"hold", "open", "close", "reduce", "close_only_mode"}
_ALLOWED_SIDES = {"long", "short"}
_OPEN_LIKE_ACTIONS = {"open"}
_EXECUTION_ACTIONS = {"open", "close", "reduce"}

FeatureValue = float | int | str | bool | None | dict[str, object] | list[object]


@dataclass(slots=True)
class AgentMarketContext:
    schema_version: str
    context_id: str
    as_of: str
    symbol: str
    price: float
    regime: str
    features: dict[str, FeatureValue] = field(default_factory=dict)
    source: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> AgentMarketContext:
        return cls(
            schema_version=_required_str(payload, "schema_version"),
            context_id=_required_str(payload, "context_id"),
            as_of=_required_str(payload, "as_of"),
            symbol=_normalize_symbol(_required_str(payload, "symbol")),
            price=_required_float(payload, "price"),
            regime=_required_str(payload, "regime"),
            features=_optional_feature_mapping(payload.get("features", {})),
            source=_optional_str(payload.get("source", "")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "as_of": self.as_of,
            "symbol": self.symbol,
            "price": self.price,
            "regime": self.regime,
            "features": dict(self.features),
            "source": self.source,
        }


@dataclass(slots=True)
class AgentIntelDigest:
    schema_version: str
    digest_id: str
    as_of: str
    global_market_impact: str
    items: list[dict[str, object]] = field(default_factory=list)
    source: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> AgentIntelDigest:
        return cls(
            schema_version=_required_str(payload, "schema_version"),
            digest_id=_required_str(payload, "digest_id"),
            as_of=_required_str(payload, "as_of"),
            global_market_impact=_required_str(payload, "global_market_impact"),
            items=_optional_object_list(payload.get("items", []), field_name="items"),
            source=_optional_str(payload.get("source", "")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "digest_id": self.digest_id,
            "as_of": self.as_of,
            "global_market_impact": self.global_market_impact,
            "items": [dict(item) for item in self.items],
            "source": self.source,
        }


@dataclass(slots=True)
class AgentTradeProposal:
    schema_version: str
    decision_id: str
    as_of: str
    valid_until: str
    action: str
    symbol: str
    side: str
    confidence: float
    time_horizon_minutes: int
    max_notional_usd: float
    max_leverage: float
    entry_style: str
    invalidation_price: float
    stop_bps: float
    take_profit_bps: float
    time_stop_minutes: int
    rationale_tags: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> AgentTradeProposal:
        return cls(
            schema_version=_required_str(payload, "schema_version"),
            decision_id=_required_str(payload, "decision_id"),
            as_of=_required_str(payload, "as_of"),
            valid_until=_required_str(payload, "valid_until"),
            action=_required_str(payload, "action").lower(),
            symbol=_normalize_symbol(_required_str(payload, "symbol")),
            side=_required_str(payload, "side").lower(),
            confidence=_required_float(payload, "confidence"),
            time_horizon_minutes=_required_int(payload, "time_horizon_minutes"),
            max_notional_usd=_required_float(payload, "max_notional_usd"),
            max_leverage=_required_float(payload, "max_leverage"),
            entry_style=_required_str(payload, "entry_style").lower(),
            invalidation_price=_required_float(payload, "invalidation_price"),
            stop_bps=_required_float(payload, "stop_bps"),
            take_profit_bps=_required_float(payload, "take_profit_bps"),
            time_stop_minutes=_required_int(payload, "time_stop_minutes"),
            rationale_tags=_optional_str_list(payload.get("rationale_tags", []), "rationale_tags"),
            evidence_ids=_optional_str_list(payload.get("evidence_ids", []), "evidence_ids"),
            risk_notes=_optional_str_list(payload.get("risk_notes", []), "risk_notes"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "as_of": self.as_of,
            "valid_until": self.valid_until,
            "action": self.action,
            "symbol": self.symbol,
            "side": self.side,
            "confidence": self.confidence,
            "time_horizon_minutes": self.time_horizon_minutes,
            "max_notional_usd": self.max_notional_usd,
            "max_leverage": self.max_leverage,
            "entry_style": self.entry_style,
            "invalidation_price": self.invalidation_price,
            "stop_bps": self.stop_bps,
            "take_profit_bps": self.take_profit_bps,
            "time_stop_minutes": self.time_stop_minutes,
            "rationale_tags": list(self.rationale_tags),
            "evidence_ids": list(self.evidence_ids),
            "risk_notes": list(self.risk_notes),
        }


@dataclass(slots=True)
class AgentDecisionBundle:
    bundle_id: str
    market_context: AgentMarketContext
    proposal: AgentTradeProposal
    intel_digest: AgentIntelDigest | None = None
    model: str = ""
    prompt_version: str = ""
    raw_output_hash: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "market_context": self.market_context.to_dict(),
            "proposal": self.proposal.to_dict(),
            "intel_digest": self.intel_digest.to_dict() if self.intel_digest is not None else None,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "raw_output_hash": self.raw_output_hash,
        }


@dataclass(frozen=True, slots=True)
class AgentProposalValidationConfig:
    allowed_symbols: tuple[str, ...] = TRIDENT_AI_INITIAL_SYMBOLS
    min_confidence: float = 0.55
    max_notional_usd: float = 25.0
    max_leverage: float = 1.0
    max_proposal_age_seconds: float = 300.0
    max_market_context_age_seconds: float = 300.0
    max_intel_digest_age_seconds: float = 1800.0
    max_clock_skew_seconds: float = 60.0
    require_stop: bool = True
    require_evidence: bool = True


@dataclass(frozen=True, slots=True)
class AgentProposalValidationResult:
    accepted: bool
    reason: str
    proposal: AgentTradeProposal | None = None


def agent_trade_proposal_json_schema(
    *,
    allowed_symbols: Sequence[str] = TRIDENT_AI_INITIAL_SYMBOLS,
) -> dict[str, object]:
    symbols = sorted({_normalize_symbol(symbol) for symbol in allowed_symbols})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "decision_id",
            "as_of",
            "valid_until",
            "action",
            "symbol",
            "side",
            "confidence",
            "time_horizon_minutes",
            "max_notional_usd",
            "max_leverage",
            "entry_style",
            "invalidation_price",
            "stop_bps",
            "take_profit_bps",
            "time_stop_minutes",
            "rationale_tags",
            "evidence_ids",
            "risk_notes",
        ],
        "properties": {
            "schema_version": {"type": "string", "enum": [TRIDENT_AI_PROPOSAL_SCHEMA_VERSION]},
            "decision_id": {"type": "string"},
            "as_of": {"type": "string"},
            "valid_until": {"type": "string"},
            "action": {"type": "string", "enum": sorted(_ALLOWED_ACTIONS)},
            "symbol": {"type": "string", "enum": symbols},
            "side": {"type": "string", "enum": sorted(_ALLOWED_SIDES)},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "time_horizon_minutes": {"type": "integer", "minimum": 1},
            "max_notional_usd": {"type": "number", "minimum": 0.0},
            "max_leverage": {"type": "number", "minimum": 0.0},
            "entry_style": {"type": "string"},
            "invalidation_price": {"type": "number", "minimum": 0.0},
            "stop_bps": {"type": "number"},
            "take_profit_bps": {"type": "number"},
            "time_stop_minutes": {"type": "integer"},
            "rationale_tags": {"type": "array", "items": {"type": "string"}},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "risk_notes": {"type": "array", "items": {"type": "string"}},
        },
    }


class _SchemaError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_agent_proposal(
    payload: Mapping[str, object] | AgentTradeProposal,
    *,
    market_context: Mapping[str, object] | AgentMarketContext | None = None,
    intel_digest: Mapping[str, object] | AgentIntelDigest | None = None,
    config: AgentProposalValidationConfig | None = None,
    now: datetime | None = None,
) -> AgentProposalValidationResult:
    """Validate one LLM proposal and return a fail-closed decision."""

    config = config or AgentProposalValidationConfig()
    now = _normalize_datetime(now or datetime.now(timezone.utc))
    try:
        proposal = payload if isinstance(payload, AgentTradeProposal) else _proposal_from_payload(payload)
        context = _market_context_from_payload(market_context) if market_context is not None else None
        digest = _intel_digest_from_payload(intel_digest) if intel_digest is not None else None
    except _SchemaError as exc:
        return AgentProposalValidationResult(accepted=False, reason=exc.reason)

    reason = _proposal_decision_reason(
        proposal=proposal,
        market_context=context,
        intel_digest=digest,
        config=config,
        now=now,
    )
    return AgentProposalValidationResult(
        accepted=reason == "accepted",
        reason=reason,
        proposal=proposal,
    )


def _proposal_decision_reason(
    *,
    proposal: AgentTradeProposal,
    market_context: AgentMarketContext | None,
    intel_digest: AgentIntelDigest | None,
    config: AgentProposalValidationConfig,
    now: datetime,
) -> str:
    if proposal.schema_version != TRIDENT_AI_PROPOSAL_SCHEMA_VERSION:
        return "unsupported_schema_version"
    if proposal.action not in _ALLOWED_ACTIONS:
        return "invalid_action"
    if proposal.symbol not in {symbol.upper() for symbol in config.allowed_symbols}:
        return "invalid_symbol"
    if proposal.side not in _ALLOWED_SIDES:
        return "invalid_side"
    if proposal.action in _EXECUTION_ACTIONS and proposal.confidence < config.min_confidence:
        return "confidence_below_min"
    if proposal.max_notional_usd > config.max_notional_usd:
        return "notional_above_cap"
    if proposal.max_leverage > config.max_leverage:
        return "leverage_above_cap"
    if proposal.action in _EXECUTION_ACTIONS and proposal.max_notional_usd <= 0:
        return "invalid_notional"
    if proposal.action not in _EXECUTION_ACTIONS and proposal.max_notional_usd < 0:
        return "invalid_notional"
    if proposal.action in _EXECUTION_ACTIONS and proposal.max_leverage <= 0:
        return "invalid_leverage"
    if proposal.action not in _EXECUTION_ACTIONS and proposal.max_leverage < 0:
        return "invalid_leverage"
    if proposal.action in _OPEN_LIKE_ACTIONS:
        if config.require_stop and proposal.stop_bps <= 0:
            return "stop_required"
        if proposal.take_profit_bps <= proposal.stop_bps:
            return "take_profit_not_above_stop"
        if proposal.invalidation_price <= 0:
            return "invalid_invalidation_price"
        if proposal.time_stop_minutes <= 0:
            return "invalid_time_stop"
    if config.require_evidence and not proposal.evidence_ids:
        return "evidence_required"

    as_of = _parse_timestamp(proposal.as_of, field_name="as_of")
    if as_of is None:
        return "invalid_timestamp:as_of"
    valid_until = _parse_timestamp(proposal.valid_until, field_name="valid_until")
    if valid_until is None:
        return "invalid_timestamp:valid_until"
    if as_of > now and (as_of - now).total_seconds() > config.max_clock_skew_seconds:
        return "proposal_from_future"
    if valid_until <= now:
        return "proposal_expired"
    if (now - as_of).total_seconds() > config.max_proposal_age_seconds:
        return "proposal_stale"

    if market_context is not None:
        if market_context.schema_version != TRIDENT_AI_MARKET_CONTEXT_SCHEMA_VERSION:
            return "unsupported_market_context_schema_version"
        if market_context.symbol != proposal.symbol:
            return "market_context_symbol_mismatch"
        if market_context.price <= 0:
            return "invalid_market_context_price"
        context_as_of = _parse_timestamp(market_context.as_of, field_name="market_context.as_of")
        if context_as_of is None:
            return "invalid_timestamp:market_context.as_of"
        if context_as_of > now and (context_as_of - now).total_seconds() > config.max_clock_skew_seconds:
            return "market_context_from_future"
        if (now - context_as_of).total_seconds() > config.max_market_context_age_seconds:
            return "market_context_stale"

    if intel_digest is not None:
        if intel_digest.schema_version != TRIDENT_AI_INTEL_DIGEST_SCHEMA_VERSION:
            return "unsupported_intel_digest_schema_version"
        digest_as_of = _parse_timestamp(intel_digest.as_of, field_name="intel_digest.as_of")
        if digest_as_of is None:
            return "invalid_timestamp:intel_digest.as_of"
        if digest_as_of > now and (digest_as_of - now).total_seconds() > config.max_clock_skew_seconds:
            return "intel_digest_from_future"
        if (now - digest_as_of).total_seconds() > config.max_intel_digest_age_seconds:
            return "intel_digest_stale"

    return "accepted"


def _proposal_from_payload(payload: Mapping[str, object]) -> AgentTradeProposal:
    if not isinstance(payload, Mapping):
        raise _SchemaError("proposal_not_object")
    try:
        return AgentTradeProposal.from_mapping(payload)
    except _SchemaError:
        raise
    except Exception as exc:
        raise _SchemaError("proposal_schema_invalid") from exc


def _market_context_from_payload(
    payload: Mapping[str, object] | AgentMarketContext,
) -> AgentMarketContext:
    if isinstance(payload, AgentMarketContext):
        return payload
    if not isinstance(payload, Mapping):
        raise _SchemaError("market_context_not_object")
    try:
        return AgentMarketContext.from_mapping(payload)
    except _SchemaError as exc:
        raise _SchemaError(f"market_context_{exc.reason}") from exc


def _intel_digest_from_payload(
    payload: Mapping[str, object] | AgentIntelDigest,
) -> AgentIntelDigest:
    if isinstance(payload, AgentIntelDigest):
        return payload
    if not isinstance(payload, Mapping):
        raise _SchemaError("intel_digest_not_object")
    try:
        return AgentIntelDigest.from_mapping(payload)
    except _SchemaError as exc:
        raise _SchemaError(f"intel_digest_{exc.reason}") from exc


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    if field_name not in payload:
        raise _SchemaError(f"missing_field:{field_name}")
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise _SchemaError(f"invalid_field:{field_name}")
    return value.strip()


def _required_float(payload: Mapping[str, object], field_name: str) -> float:
    if field_name not in payload:
        raise _SchemaError(f"missing_field:{field_name}")
    value = payload[field_name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _SchemaError(f"invalid_field:{field_name}")
    return float(value)


def _required_int(payload: Mapping[str, object], field_name: str) -> int:
    if field_name not in payload:
        raise _SchemaError(f"missing_field:{field_name}")
    value = payload[field_name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _SchemaError(f"invalid_field:{field_name}")
    return int(value)


def _optional_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _SchemaError("invalid_field:source")
    return value.strip()


def _optional_str_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _SchemaError(f"invalid_field:{field_name}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _SchemaError(f"invalid_field:{field_name}")
        result.append(item.strip())
    return result


def _optional_object_list(value: object, *, field_name: str) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _SchemaError(f"invalid_field:{field_name}")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise _SchemaError(f"invalid_field:{field_name}")
        result.append(dict(item))
    return result


def _optional_feature_mapping(value: object) -> dict[str, FeatureValue]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _SchemaError("invalid_field:features")
    result: dict[str, FeatureValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise _SchemaError("invalid_field:features")
        result[key.strip()] = _feature_json_value(item)
    return result


def _feature_json_value(value: object) -> FeatureValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise _SchemaError("invalid_field:features")
            result[key.strip()] = _feature_json_value(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_feature_json_value(item) for item in value]
    raise _SchemaError("invalid_field:features")


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _parse_timestamp(value: str, *, field_name: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
