from __future__ import annotations

from collections.abc import Mapping


MARKET_MICRO_REGIME_SCHEMA_VERSION = "trident_ai_market_micro_regime_v1"


def build_market_micro_regime(
    features: Mapping[str, object],
    *,
    symbol: str,
    side: str,
) -> dict[str, object]:
    range_bucket = range_bucket_for_value(_number(features.get("bucket_range_bps")))
    vol_bucket = short_vol_bucket_for_value(_number(features.get("realized_vol_short_bps")))
    volume_bucket = volume_ratio_bucket_for_value(_number(features.get("volume_ratio")))
    vwap_bucket = vwap_bucket_for_value(_number(features.get("vwap_distance_bps")))
    microprice_bucket = microprice_bucket_for_value(
        _number(features.get("microprice_dislocation_bps")),
        side=side,
    )
    normalized_symbol = str(symbol or "unknown").strip().upper() or "unknown"
    range_vol_regime = f"{range_bucket}|{vol_bucket}"
    flow_regime = f"{volume_bucket}|{vwap_bucket}"
    micro_regime = (
        f"{range_bucket}|{vol_bucket}|{volume_bucket}|{vwap_bucket}|"
        f"{microprice_bucket}"
    )
    return {
        "schema_version": MARKET_MICRO_REGIME_SCHEMA_VERSION,
        "symbol": normalized_symbol,
        "side": str(side or "").strip().lower(),
        "range_bucket": range_bucket,
        "short_vol_bucket": vol_bucket,
        "volume_ratio_bucket": volume_bucket,
        "vwap_bucket": vwap_bucket,
        "microprice_bucket": microprice_bucket,
        "range_vol_regime": range_vol_regime,
        "flow_regime": flow_regime,
        "micro_regime": micro_regime,
        "symbol_range_vol": f"{normalized_symbol}|{range_vol_regime}",
        "symbol_micro_regime": f"{normalized_symbol}|{micro_regime}",
    }


def market_micro_regime_labels(regime: Mapping[str, object]) -> tuple[str, ...]:
    symbol = str(regime.get("symbol", "") or "unknown").upper()
    labels = [
        ("range_bucket", regime.get("range_bucket")),
        ("short_vol_bucket", regime.get("short_vol_bucket")),
        ("volume_ratio_bucket", regime.get("volume_ratio_bucket")),
        ("vwap_bucket", regime.get("vwap_bucket")),
        ("microprice_bucket", regime.get("microprice_bucket")),
        ("range_vol_regime", regime.get("range_vol_regime")),
        ("flow_regime", regime.get("flow_regime")),
        ("micro_regime", regime.get("micro_regime")),
        ("symbol_range_vol", regime.get("symbol_range_vol")),
        ("symbol_micro_regime", regime.get("symbol_micro_regime")),
    ]
    if symbol and symbol != "UNKNOWN":
        labels.insert(0, ("symbol", symbol))
    return tuple(
        f"{family}::{bucket}"
        for family, bucket in labels
        if str(bucket or "").strip()
    )


def range_bucket_for_value(value: float) -> str:
    if value <= 45.0:
        return "range_low"
    if value <= 65.0:
        return "range_mid"
    if value <= 90.0:
        return "range_high"
    return "range_extreme"


def short_vol_bucket_for_value(value: float) -> str:
    if value <= 15.0:
        return "vol_low"
    if value <= 20.0:
        return "vol_controlled"
    if value <= 25.0:
        return "vol_high"
    return "vol_extreme"


def volume_ratio_bucket_for_value(value: float) -> str:
    if value <= 3.0:
        return "flow_normal"
    if value <= 8.0:
        return "flow_elevated"
    if value <= 20.0:
        return "flow_crowded"
    return "flow_blowoff"


def vwap_bucket_for_value(value: float) -> str:
    distance = abs(value)
    if distance <= 8.0:
        return "vwap_near"
    if distance <= 16.0:
        return "vwap_extended"
    if distance <= 30.0:
        return "vwap_far"
    return "vwap_extreme"


def microprice_bucket_for_value(value: float, *, side: str) -> str:
    if abs(value) < 0.05:
        return "micro_neutral"
    normalized_side = str(side or "").strip().lower()
    aligned = (normalized_side == "long" and value > 0.0) or (
        normalized_side == "short" and value < 0.0
    )
    if aligned:
        return "micro_aligned"
    return "micro_adverse"


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)
