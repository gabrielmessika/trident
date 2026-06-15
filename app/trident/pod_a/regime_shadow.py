from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping

from app.trident.types import RegimeSnapshot, SymbolMarketSnapshot


@dataclass(slots=True)
class PriceHistory:
    timestamps: list[datetime] = field(default_factory=list)
    prices: list[float] = field(default_factory=list)

    def append(self, timestamp: datetime, price: float) -> None:
        if price <= 0:
            return
        if self.timestamps and timestamp < self.timestamps[-1]:
            return
        self.timestamps.append(timestamp)
        self.prices.append(price)

    def return_bps(self, timestamp: datetime, minutes: int, current_price: float) -> float | None:
        cutoff = timestamp - timedelta(minutes=minutes)
        index = bisect_right(self.timestamps, cutoff) - 1
        if index < 0:
            return None
        previous_price = self.prices[index]
        if previous_price <= 0 or current_price <= 0:
            return None
        return (current_price / previous_price - 1.0) * 10000.0


@dataclass(slots=True)
class RegimeShadowFeatures:
    timestamp: str
    symbol: str
    bull_regime_score: int
    bear_regime_score: int
    regime_gate_decision: str
    btc_ret_60m_bps: float | None
    btc_ret_240m_bps: float | None
    btc_ret_1440m_bps: float | None
    symbol_ret_60m_bps: float | None
    symbol_ret_240m_bps: float | None
    btc_above_ema_slow: bool
    btc_fast_above_slow: bool
    symbol_above_ema_slow: bool
    symbol_fast_above_slow: bool
    structure_score: float
    breadth_pct: float | None
    alt_participation_pct: float | None
    leader_trend_score: float | None
    coherence_score: float | None
    dispersion_pct: float | None


class PodARegimeShadowTracker:
    """Observation-only regime gate used by P1-06 shadow analysis."""

    def __init__(self) -> None:
        self.histories: dict[str, PriceHistory] = {}

    def evaluate(
        self,
        *,
        timestamp: str | datetime,
        snapshots: list[SymbolMarketSnapshot],
        regime_snapshot: RegimeSnapshot | Mapping[str, object],
    ) -> dict[str, RegimeShadowFeatures]:
        parsed_timestamp = parse_timestamp(timestamp)
        if parsed_timestamp is None:
            return {}
        snapshot_by_symbol = {snapshot.symbol.upper(): snapshot for snapshot in snapshots}
        btc_snapshot = snapshot_by_symbol.get("BTC")
        if btc_snapshot is None:
            return {}
        regime = regime_snapshot_mapping(regime_snapshot)
        return {
            symbol: build_regime_shadow_features(
                timestamp=parsed_timestamp,
                symbol=symbol,
                snapshot=snapshot,
                btc_snapshot=btc_snapshot,
                histories=self.histories,
                regime=regime,
            )
            for symbol, snapshot in snapshot_by_symbol.items()
        }

    def observe(
        self,
        *,
        timestamp: str | datetime,
        snapshots: list[SymbolMarketSnapshot],
    ) -> None:
        parsed_timestamp = parse_timestamp(timestamp)
        if parsed_timestamp is None:
            return
        for snapshot in snapshots:
            symbol = snapshot.symbol.upper()
            history = self.histories.setdefault(symbol, PriceHistory())
            history.append(parsed_timestamp, float(snapshot.price or 0.0))


def build_regime_shadow_features(
    *,
    timestamp: datetime,
    symbol: str,
    snapshot: SymbolMarketSnapshot,
    btc_snapshot: SymbolMarketSnapshot,
    histories: dict[str, PriceHistory],
    regime: Mapping[str, object],
) -> RegimeShadowFeatures:
    btc_history = histories.get("BTC", PriceHistory())
    symbol_history = histories.get(symbol.upper(), PriceHistory())
    btc_ret_60 = btc_history.return_bps(timestamp, 60, btc_snapshot.price)
    btc_ret_240 = btc_history.return_bps(timestamp, 240, btc_snapshot.price)
    btc_ret_1440 = btc_history.return_bps(timestamp, 1440, btc_snapshot.price)
    symbol_ret_60 = symbol_history.return_bps(timestamp, 60, snapshot.price)
    symbol_ret_240 = symbol_history.return_bps(timestamp, 240, snapshot.price)
    breadth = _maybe(regime.get("breadth_pct"))
    alt_participation = _maybe(regime.get("alt_participation_pct"))
    leader_trend = _maybe(regime.get("leader_trend_score"))
    coherence = _maybe(regime.get("coherence_score"))
    dispersion = _maybe(regime.get("dispersion_pct"))
    structure = _float(regime.get("structure_score"))
    btc_above_ema = btc_snapshot.ema_slow > 0 and btc_snapshot.price >= btc_snapshot.ema_slow
    btc_fast_above_slow = btc_snapshot.ema_slow > 0 and btc_snapshot.ema_fast >= btc_snapshot.ema_slow
    symbol_above_ema = snapshot.ema_slow > 0 and snapshot.price >= snapshot.ema_slow
    symbol_fast_above_slow = snapshot.ema_slow > 0 and snapshot.ema_fast >= snapshot.ema_slow

    bear = 0
    bull = 0
    if btc_ret_60 is not None and btc_ret_60 <= -35.0:
        bear += 1
    if btc_ret_60 is not None and btc_ret_60 >= 35.0:
        bull += 1
    if btc_ret_240 is not None and btc_ret_240 <= -120.0:
        bear += 1
    if btc_ret_240 is not None and btc_ret_240 >= 120.0:
        bull += 1
    if btc_ret_1440 is not None and btc_ret_1440 <= -250.0:
        bear += 1
    if btc_ret_1440 is not None and btc_ret_1440 >= 250.0:
        bull += 1
    if not btc_above_ema or not btc_fast_above_slow:
        bear += 1
    if btc_above_ema and btc_fast_above_slow:
        bull += 1
    if structure <= 0.20:
        bear += 1
    if structure >= 0.20:
        bull += 1
    if (breadth is not None and breadth <= 0.45) or (
        alt_participation is not None and alt_participation <= 0.45
    ):
        bear += 1
    if (breadth is not None and breadth >= 0.55) or (
        alt_participation is not None and alt_participation >= 0.55
    ):
        bull += 1
    if leader_trend is not None and leader_trend <= -0.05:
        bear += 1
    if leader_trend is not None and leader_trend >= 0.05:
        bull += 1
    if symbol_ret_60 is not None and symbol_ret_240 is not None:
        if symbol_ret_60 <= -20.0 and symbol_ret_240 <= -100.0:
            bear += 1
        if symbol_ret_60 >= 20.0 and symbol_ret_240 >= 100.0:
            bull += 1
    if not symbol_above_ema or not symbol_fast_above_slow:
        bear += 1
    if symbol_above_ema and symbol_fast_above_slow:
        bull += 1

    if bull >= 4 and bear <= 2:
        gate = "bullish"
    elif bear >= 4 and bull <= 2:
        gate = "bearish"
    elif bull >= 3 and bear <= 3:
        gate = "constructive"
    elif bear >= 3 and bull <= 3:
        gate = "defensive"
    else:
        gate = "neutral"

    return RegimeShadowFeatures(
        timestamp=isoformat(timestamp),
        symbol=symbol.upper(),
        bull_regime_score=bull,
        bear_regime_score=bear,
        regime_gate_decision=gate,
        btc_ret_60m_bps=_round_optional(btc_ret_60),
        btc_ret_240m_bps=_round_optional(btc_ret_240),
        btc_ret_1440m_bps=_round_optional(btc_ret_1440),
        symbol_ret_60m_bps=_round_optional(symbol_ret_60),
        symbol_ret_240m_bps=_round_optional(symbol_ret_240),
        btc_above_ema_slow=btc_above_ema,
        btc_fast_above_slow=btc_fast_above_slow,
        symbol_above_ema_slow=symbol_above_ema,
        symbol_fast_above_slow=symbol_fast_above_slow,
        structure_score=round(structure, 6),
        breadth_pct=_round_optional(breadth),
        alt_participation_pct=_round_optional(alt_participation),
        leader_trend_score=_round_optional(leader_trend),
        coherence_score=_round_optional(coherence),
        dispersion_pct=_round_optional(dispersion),
    )


def signal_regime_shadow_details(
    features: RegimeShadowFeatures | None,
    *,
    side: str,
    setup: str,
) -> dict[str, object]:
    short_candidate = str(setup) == "trend_pullback_short" or str(side) == "short"
    return regime_shadow_details(features, side=side, short_candidate=short_candidate)


def review_regime_shadow_details(
    features: RegimeShadowFeatures | None,
    review: Mapping[str, object],
) -> dict[str, object]:
    candidates = review.get("candidate_setups", [])
    if not isinstance(candidates, list):
        candidates = []
    preferred_side = str(review.get("preferred_side", ""))
    return regime_shadow_details(
        features,
        side=preferred_side,
        short_candidate="trend_pullback_short" in {str(item) for item in candidates},
    )


def regime_shadow_details(
    features: RegimeShadowFeatures | None,
    *,
    side: str,
    short_candidate: bool = False,
) -> dict[str, object]:
    if features is None:
        return {
            "regime_shadow_mode": "observation_only",
            "bull_regime_score": None,
            "bear_regime_score": None,
            "regime_gate_decision": "missing_features",
            "would_block_long": False,
            "would_open_defensive_short_shadow": False,
            "live_action_unchanged": True,
        }
    side_normalized = str(side).strip().lower()
    defensive_short = (
        bool(short_candidate)
        and side_normalized == "short"
        and features.regime_gate_decision == "defensive"
    )
    return {
        "regime_shadow_mode": "observation_only",
        "bull_regime_score": features.bull_regime_score,
        "bear_regime_score": features.bear_regime_score,
        "regime_gate_decision": features.regime_gate_decision,
        "would_block_long": side_normalized == "long" and features.bear_regime_score >= 4,
        "would_open_defensive_short_shadow": defensive_short,
        "live_action_unchanged": True,
        "btc_ret_60m_bps": features.btc_ret_60m_bps,
        "btc_ret_240m_bps": features.btc_ret_240m_bps,
        "btc_ret_1440m_bps": features.btc_ret_1440m_bps,
        "symbol_ret_60m_bps": features.symbol_ret_60m_bps,
        "symbol_ret_240m_bps": features.symbol_ret_240m_bps,
        "btc_above_ema_slow": features.btc_above_ema_slow,
        "btc_fast_above_slow": features.btc_fast_above_slow,
        "symbol_above_ema_slow": features.symbol_above_ema_slow,
        "symbol_fast_above_slow": features.symbol_fast_above_slow,
        "breadth_pct": features.breadth_pct,
        "leader_trend_score": features.leader_trend_score,
    }


def regime_shadow_setup_details(details: Mapping[str, object]) -> dict[str, float | str | bool]:
    return {
        key: _setup_detail_value(value)
        for key, value in details.items()
        if key
        in {
            "regime_shadow_mode",
            "bull_regime_score",
            "bear_regime_score",
            "regime_gate_decision",
            "would_block_long",
            "would_open_defensive_short_shadow",
            "live_action_unchanged",
            "btc_ret_60m_bps",
            "btc_ret_240m_bps",
            "btc_ret_1440m_bps",
            "symbol_ret_60m_bps",
            "symbol_ret_240m_bps",
            "btc_above_ema_slow",
            "btc_fast_above_slow",
            "symbol_above_ema_slow",
            "symbol_fast_above_slow",
            "breadth_pct",
            "leader_trend_score",
        }
    }


def parse_timestamp(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def regime_snapshot_mapping(
    regime_snapshot: RegimeSnapshot | Mapping[str, object],
) -> Mapping[str, object]:
    if isinstance(regime_snapshot, RegimeSnapshot):
        return {
            "structure_score": regime_snapshot.structure_score,
            "breadth_pct": regime_snapshot.breadth_pct,
            "alt_participation_pct": regime_snapshot.alt_participation_pct,
            "leader_trend_score": regime_snapshot.leader_trend_score,
            "coherence_score": regime_snapshot.coherence_score,
            "dispersion_pct": regime_snapshot.dispersion_pct,
        }
    return regime_snapshot


def _maybe(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _float(value: object) -> float:
    number = _maybe(value)
    return number if number is not None else 0.0


def _round_optional(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _setup_detail_value(value: object) -> float | str | bool:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return str(value)
