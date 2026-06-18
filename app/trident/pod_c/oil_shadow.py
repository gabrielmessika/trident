from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from app.trident.types import RegimeSnapshot, SymbolMarketSnapshot


P109_OIL_SYMBOLS = {"XYZ:CL", "XYZ:BRENTOIL"}
P109_OIL_ALLOWED_RESEARCH_REGIMES = {"chop", "mixed", "high_vol"}
P109_OIL_HORIZON_MIN = 240


@dataclass(frozen=True, slots=True)
class P109OilShadowFeatures:
    symbol: str
    mode: str
    research_regime: str
    would_open: bool
    reason: str
    score: float
    hour_utc: int | None
    horizon_min: int = P109_OIL_HORIZON_MIN
    side: str = "short"
    live_action_unchanged: bool = True


def build_p109_oil_shadow_features(
    *,
    snapshot: SymbolMarketSnapshot,
    timestamp: str | datetime,
    cluster_regime_snapshot: RegimeSnapshot | Mapping[str, object] | None,
) -> P109OilShadowFeatures | None:
    symbol = str(snapshot.symbol).strip().upper()
    if symbol not in P109_OIL_SYMBOLS:
        return None

    parsed = _parse_timestamp(timestamp)
    hour = parsed.hour if parsed is not None else None
    research_regime = p109_oil_research_regime(cluster_regime_snapshot)
    reasons: list[str] = []
    if research_regime not in P109_OIL_ALLOWED_RESEARCH_REGIMES:
        reasons.append(f"regime_not_allowed:{research_regime}")
    if hour is None:
        reasons.append("timestamp_missing")
    elif not (7 <= hour < 10):
        reasons.append(f"hour_outside_07_10:{hour:02d}")

    score = (
        max(0.0, 10.0 - float(snapshot.spread_bps or 0.0))
        + max(0.0, float(snapshot.vwap_distance_bps or 0.0)) * 0.10
        + max(0.0, float(snapshot.realized_vol_short_bps or 0.0) - 6.0) * 0.15
    )
    would_open = not reasons
    return P109OilShadowFeatures(
        symbol=symbol,
        mode="observation_only",
        research_regime=research_regime,
        would_open=would_open,
        reason="matched_oil_short_4h_time_gate" if would_open else ";".join(reasons),
        score=round(score, 6),
        hour_utc=hour,
    )


def p109_oil_shadow_details(features: P109OilShadowFeatures | None) -> dict[str, float | str | bool]:
    if features is None:
        return {}
    return {
        "p109_oil_shadow_mode": features.mode,
        "p109_oil_pattern": "oil_short_4h_time_gate",
        "p109_oil_symbol": features.symbol,
        "p109_oil_shadow_side": features.side,
        "p109_oil_shadow_horizon_min": float(features.horizon_min),
        "p109_oil_shadow_research_regime": features.research_regime,
        "p109_oil_shadow_hour_utc": float(features.hour_utc) if features.hour_utc is not None else "",
        "p109_oil_shadow_score": features.score,
        "p109_oil_shadow_reason": features.reason,
        "would_open_p109_oil_short_shadow": features.would_open,
        "p109_oil_shadow_live_action_unchanged": features.live_action_unchanged,
    }


def p109_oil_research_regime(snapshot: RegimeSnapshot | Mapping[str, object] | None) -> str:
    if snapshot is None:
        return "not_ready"
    ready = _bool_value(_get(snapshot, "ready"))
    if not ready:
        return "not_ready"
    adx = _float_value(_get(snapshot, "adx"))
    atr = _float_value(_get(snapshot, "atr_ratio"))
    width = _float_value(_get(snapshot, "range_width_bps"))
    structure = _float_value(_get(snapshot, "structure_score"))
    coherence = _float_value(_get(snapshot, "coherence_score"))
    if atr >= 0.85 or width >= 30.0:
        return "high_vol"
    if structure >= 0.20 and adx >= 14.0:
        return "uptrend"
    if structure <= -0.20 and adx >= 14.0:
        return "downtrend"
    if adx < 12.0 or coherence < 0.25:
        return "chop"
    return "mixed"


def _get(snapshot: RegimeSnapshot | Mapping[str, object], key: str) -> object:
    if isinstance(snapshot, RegimeSnapshot):
        return getattr(snapshot, key)
    return snapshot.get(key)


def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _parse_timestamp(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
