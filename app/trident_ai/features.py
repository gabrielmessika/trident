from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from app.trident.types import SymbolMarketSnapshot, symbol_market_snapshot_from_mapping
from app.trident_ai.config import TridentAIConfig
from app.trident_ai.types import (
    AgentMarketContext,
    TRIDENT_AI_INITIAL_SYMBOLS,
    TRIDENT_AI_MARKET_CONTEXT_SCHEMA_VERSION,
)


REQUIRED_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "symbol",
    "price",
    "ema_fast",
    "ema_slow",
    "vwap_distance_bps",
    "structure_score",
    "funding_rate",
    "spread_bps",
    "btc_aligned",
)


@dataclass(frozen=True, slots=True)
class AgentMarketContextBuildConfig:
    allowed_symbols: tuple[str, ...] = TRIDENT_AI_INITIAL_SYMBOLS
    max_snapshot_age_seconds: float = 300.0
    source: str = "trident_snapshot"

    @classmethod
    def from_trident_ai_config(
        cls,
        config: TridentAIConfig,
    ) -> AgentMarketContextBuildConfig:
        return cls(
            allowed_symbols=config.tradable_symbols,
            max_snapshot_age_seconds=config.risk.max_market_context_age_seconds,
        )


@dataclass(frozen=True, slots=True)
class AgentMarketContextBuildResult:
    accepted: bool
    reason: str
    symbol: str = ""
    context: AgentMarketContext | None = None


class TridentAIFeatureBuilder:
    def __init__(
        self,
        config: AgentMarketContextBuildConfig | None = None,
    ) -> None:
        self.config = config or AgentMarketContextBuildConfig()
        self.allowed_symbols = {symbol.upper() for symbol in self.config.allowed_symbols}

    def build_context_from_mapping(
        self,
        payload: Mapping[str, object],
        *,
        as_of: str | datetime,
        regime: str = "unknown",
        now: datetime | None = None,
    ) -> AgentMarketContextBuildResult:
        reason = _snapshot_mapping_rejection_reason(payload)
        if reason != "accepted":
            return AgentMarketContextBuildResult(
                accepted=False,
                reason=reason,
                symbol=_mapping_symbol(payload),
            )
        snapshot = symbol_market_snapshot_from_mapping(dict(payload))
        return self.build_context(snapshot, as_of=as_of, regime=regime, now=now)

    def build_context(
        self,
        snapshot: SymbolMarketSnapshot,
        *,
        as_of: str | datetime,
        regime: str = "unknown",
        now: datetime | None = None,
    ) -> AgentMarketContextBuildResult:
        symbol = str(snapshot.symbol).strip().upper()
        if not symbol:
            return AgentMarketContextBuildResult(False, "missing_symbol")
        if symbol not in self.allowed_symbols:
            return AgentMarketContextBuildResult(False, "symbol_not_allowed", symbol=symbol)
        if snapshot.price <= 0:
            return AgentMarketContextBuildResult(False, "invalid_price", symbol=symbol)
        if snapshot.spread_bps < 0:
            return AgentMarketContextBuildResult(False, "invalid_spread", symbol=symbol)

        as_of_dt = _parse_datetime(as_of)
        if as_of_dt is None:
            return AgentMarketContextBuildResult(False, "invalid_timestamp", symbol=symbol)
        now_dt = _normalize_datetime(now) if now is not None else None
        if now_dt is not None:
            age_seconds = (now_dt - as_of_dt).total_seconds()
            if age_seconds < -60.0:
                return AgentMarketContextBuildResult(False, "snapshot_from_future", symbol=symbol)
            if age_seconds > self.config.max_snapshot_age_seconds:
                return AgentMarketContextBuildResult(False, "snapshot_stale", symbol=symbol)

        context = AgentMarketContext(
            schema_version=TRIDENT_AI_MARKET_CONTEXT_SCHEMA_VERSION,
            context_id=f"market_{symbol}_{_timestamp_id(as_of_dt)}",
            as_of=_format_timestamp(as_of_dt),
            symbol=symbol,
            price=float(snapshot.price),
            regime=str(regime or "unknown"),
            features=_snapshot_features(snapshot),
            source=self.config.source,
        )
        return AgentMarketContextBuildResult(
            accepted=True,
            reason="accepted",
            symbol=symbol,
            context=context,
        )

    def build_contexts_from_mappings(
        self,
        payloads: Sequence[Mapping[str, object]],
        *,
        as_of: str | datetime,
        regime: str = "unknown",
        now: datetime | None = None,
    ) -> list[AgentMarketContextBuildResult]:
        return [
            self.build_context_from_mapping(payload, as_of=as_of, regime=regime, now=now)
            for payload in payloads
        ]

    def accepted_contexts_from_mappings(
        self,
        payloads: Sequence[Mapping[str, object]],
        *,
        as_of: str | datetime,
        regime: str = "unknown",
        now: datetime | None = None,
    ) -> list[AgentMarketContext]:
        return [
            result.context
            for result in self.build_contexts_from_mappings(
                payloads,
                as_of=as_of,
                regime=regime,
                now=now,
            )
            if result.context is not None
        ]


def _snapshot_features(snapshot: SymbolMarketSnapshot) -> dict[str, float | str | bool | None]:
    return {
        "ema_fast": float(snapshot.ema_fast),
        "ema_slow": float(snapshot.ema_slow),
        "ema_alignment": _ema_alignment(snapshot),
        "vwap_distance_bps": float(snapshot.vwap_distance_bps),
        "structure_score": float(snapshot.structure_score),
        "funding_rate": float(snapshot.funding_rate),
        "spread_bps": float(snapshot.spread_bps),
        "btc_aligned": bool(snapshot.btc_aligned),
        "market_cluster": str(snapshot.market_cluster),
        "cluster_aligned": bool(snapshot.cluster_aligned),
        "cluster_leader": str(snapshot.cluster_leader),
        "book_imbalance": float(snapshot.book_imbalance),
        "trade_flow_bias": float(snapshot.trade_flow_bias),
        "bucket_volume": float(snapshot.bucket_volume),
        "bucket_trade_count": int(snapshot.bucket_trade_count),
        "bucket_range_bps": float(snapshot.bucket_range_bps),
        "best_bid": float(snapshot.best_bid),
        "best_ask": float(snapshot.best_ask),
        "best_bid_size": float(snapshot.best_bid_size),
        "best_ask_size": float(snapshot.best_ask_size),
        "bid_depth_10bps": float(snapshot.bid_depth_10bps),
        "ask_depth_10bps": float(snapshot.ask_depth_10bps),
        "bid_depth_velocity": float(snapshot.bid_depth_velocity),
        "ask_depth_velocity": float(snapshot.ask_depth_velocity),
        "best_bid_size_velocity": float(snapshot.best_bid_size_velocity),
        "best_ask_size_velocity": float(snapshot.best_ask_size_velocity),
        "microprice": float(snapshot.microprice),
        "microprice_dislocation_bps": float(snapshot.microprice_dislocation_bps),
        "buy_count": int(snapshot.buy_count),
        "sell_count": int(snapshot.sell_count),
        "buy_volume": float(snapshot.buy_volume),
        "sell_volume": float(snapshot.sell_volume),
        "vwap": _feature_value(snapshot.vwap),
        "bucket_notional_usd": float(snapshot.bucket_notional_usd),
        "signed_trade_delta": float(snapshot.signed_trade_delta),
        "delta_spread_bps": float(snapshot.delta_spread_bps),
        "delta_book_imbalance": float(snapshot.delta_book_imbalance),
        "delta_trade_flow_bias": float(snapshot.delta_trade_flow_bias),
        "volume_ratio": float(snapshot.volume_ratio),
        "trade_count_ratio": float(snapshot.trade_count_ratio),
        "realized_vol_short_bps": float(snapshot.realized_vol_short_bps),
        "realized_vol_long_bps": float(snapshot.realized_vol_long_bps),
        "compression_score": float(snapshot.compression_score),
        "open_interest": _feature_value(snapshot.open_interest),
        "mark_px": _feature_value(snapshot.mark_px),
        "oracle_px": _feature_value(snapshot.oracle_px),
        "premium": _feature_value(snapshot.premium),
        "day_ntl_vlm": _feature_value(snapshot.day_ntl_vlm),
        "day_base_vlm": _feature_value(snapshot.day_base_vlm),
        "asset_ctx_observation_age_seconds": _feature_value(
            snapshot.asset_ctx_observation_age_seconds
        ),
        "external_reference_source_count": int(snapshot.external_reference_source_count),
        "external_reference_symbol": str(snapshot.external_reference_symbol),
        "external_reference_age_seconds": _feature_value(
            snapshot.external_reference_age_seconds
        ),
        "external_reference_max_deviation_bps": float(
            snapshot.external_reference_max_deviation_bps
        ),
        "external_premium_bps": float(snapshot.external_premium_bps),
        "external_momentum_60s_bps": float(snapshot.external_momentum_60s_bps),
        "external_momentum_300s_bps": float(snapshot.external_momentum_300s_bps),
        "external_alignment_score": float(snapshot.external_alignment_score),
    }


def _snapshot_mapping_rejection_reason(payload: Mapping[str, object]) -> str:
    for field_name in REQUIRED_SNAPSHOT_FIELDS:
        if field_name not in payload:
            return f"missing_field:{field_name}"
    if not isinstance(payload.get("symbol"), str) or not str(payload.get("symbol", "")).strip():
        return "invalid_field:symbol"
    for field_name in REQUIRED_SNAPSHOT_FIELDS:
        if field_name in {"symbol", "btc_aligned"}:
            continue
        value = payload.get(field_name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"invalid_field:{field_name}"
    if not isinstance(payload.get("btc_aligned"), bool):
        return "invalid_field:btc_aligned"
    return "accepted"


def _mapping_symbol(payload: Mapping[str, object]) -> str:
    value = payload.get("symbol", "")
    return str(value).strip().upper() if isinstance(value, str) else ""


def _ema_alignment(snapshot: SymbolMarketSnapshot) -> str:
    if snapshot.ema_fast > snapshot.ema_slow:
        return "bullish"
    if snapshot.ema_fast < snapshot.ema_slow:
        return "bearish"
    return "flat"


def _feature_value(value: object) -> float | str | bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return value
    return None


def _parse_datetime(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
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


def _format_timestamp(value: datetime) -> str:
    normalized = _normalize_datetime(value).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return _normalize_datetime(value).strftime("%Y%m%dT%H%M%SZ")
