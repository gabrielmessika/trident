from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence


TRIDENT_AI_TECHNICAL_DIGEST_SCHEMA_VERSION = "trident_ai_technical_digest_v1"
TECHNICAL_DIGEST_FEATURE_NAME = "technical_digest"
MAX_TECHNICAL_DIGEST_CHARS = 1200
MAX_TECHNICAL_TOP_SIGNALS = 8
MAX_TECHNICAL_VETO_SIGNALS = 4
MAX_TECHNICAL_CONFLICTS = 4

IndicatorSpec = tuple[int, str, str, str]

TECHNICAL_INDICATOR_COVERAGE: tuple[IndicatorSpec, ...] = (
    (1, "sma", "Moving Average / SMA", "trend_ma"),
    (2, "ema", "Exponential Moving Average / EMA", "trend_ma"),
    (3, "volume", "Volume", "volume_flow"),
    (4, "rsi", "Relative Strength Index", "momentum"),
    (5, "macd", "MACD", "momentum"),
    (6, "bollinger_bands", "Bollinger Bands", "volatility_bands"),
    (7, "vwap", "VWAP", "levels"),
    (8, "stochastic", "Stochastic Oscillator", "momentum"),
    (9, "atr", "Average True Range", "volatility_bands"),
    (10, "volume_profile", "Volume Profile", "volume_flow"),
    (11, "fibonacci_retracement", "Fibonacci Retracement", "levels"),
    (12, "supertrend", "Supertrend", "trend_ma"),
    (13, "ichimoku", "Ichimoku Cloud", "trend_ma"),
    (14, "pivot_points", "Pivot Points Standard", "levels"),
    (15, "adx_dmi", "ADX / DMI", "composite"),
    (16, "ma_cross", "Moving Average Cross", "trend_ma"),
    (17, "stochastic_rsi", "Stochastic RSI", "momentum"),
    (18, "parabolic_sar", "Parabolic SAR", "trend_ma"),
    (19, "obv", "On Balance Volume", "volume_flow"),
    (20, "cci", "Commodity Channel Index", "momentum"),
    (21, "williams_r", "Williams %R", "momentum"),
    (22, "mfi", "Money Flow Index", "volume_flow"),
    (23, "keltner_channels", "Keltner Channels", "volatility_bands"),
    (24, "donchian_channels", "Donchian Channels", "volatility_bands"),
    (25, "aroon", "Aroon", "momentum"),
    (26, "awesome_oscillator", "Awesome Oscillator", "momentum"),
    (27, "accumulation_distribution", "Accumulation / Distribution", "volume_flow"),
    (28, "chaikin_money_flow", "Chaikin Money Flow", "volume_flow"),
    (29, "roc", "Rate of Change / ROC", "momentum"),
    (30, "momentum", "Momentum", "momentum"),
    (31, "hma", "Hull Moving Average", "trend_ma"),
    (32, "wma", "Weighted Moving Average", "trend_ma"),
    (33, "vwma", "Volume Weighted Moving Average", "trend_ma"),
    (34, "ma_ribbon", "Moving Average Ribbon", "trend_ma"),
    (35, "linear_regression", "Linear Regression", "trend_ma"),
    (36, "kama", "Kaufman Adaptive Moving Average", "trend_ma"),
    (37, "alma", "Arnaud Legoux Moving Average", "trend_ma"),
    (38, "tema", "Triple EMA", "trend_ma"),
    (39, "trix", "TRIX", "momentum"),
    (40, "ultimate_oscillator", "Ultimate Oscillator", "momentum"),
    (41, "tsi", "True Strength Index", "momentum"),
    (42, "rvi", "Relative Vigor Index", "momentum"),
    (43, "vortex", "Vortex Indicator", "momentum"),
    (44, "klinger", "Klinger Oscillator", "volume_flow"),
    (45, "ease_of_movement", "Ease of Movement", "volume_flow"),
    (46, "pvt", "Price Volume Trend", "volume_flow"),
    (47, "net_volume", "Net Volume", "volume_flow"),
    (48, "volume_delta", "Volume Delta", "volume_flow"),
    (49, "anchored_vwap", "Anchored VWAP", "levels"),
    (50, "technical_ratings", "Technical Ratings", "composite"),
)


def build_technical_digest(
    features: Mapping[str, object],
    *,
    max_chars: int = MAX_TECHNICAL_DIGEST_CHARS,
) -> dict[str, object]:
    signals: list[dict[str, object]] = []
    vetoes: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []

    ema_alignment = str(features.get("ema_alignment", "") or "").lower()
    ema_gap_strength = _ema_gap_strength(features)
    if ema_alignment == "bullish":
        _add_signal(
            signals,
            signal_id="ma_stack_bullish",
            family="trend_ma",
            side="long",
            strength=max(0.35, ema_gap_strength),
            sources=("sma", "ema", "ma_cross", "ma_ribbon"),
        )
    elif ema_alignment == "bearish":
        _add_signal(
            signals,
            signal_id="ma_stack_bearish",
            family="trend_ma",
            side="short",
            strength=max(0.35, ema_gap_strength),
            sources=("sma", "ema", "ma_cross", "ma_ribbon"),
        )

    structure_score = _number(features.get("structure_score"))
    if abs(structure_score) >= 0.2:
        _add_signal(
            signals,
            signal_id="trend_structure",
            family="trend_ma",
            side=_side_from_signed(structure_score),
            strength=_clamp(abs(structure_score), 0.2, 1.0),
            sources=("supertrend", "ichimoku", "parabolic_sar", "linear_regression"),
        )

    vwap_distance = _number(features.get("vwap_distance_bps"))
    if abs(vwap_distance) >= 2.0:
        _add_signal(
            signals,
            signal_id="vwap_distance",
            family="levels",
            side=_side_from_signed(vwap_distance),
            strength=_clamp(abs(vwap_distance) / 35.0, 0.2, 1.0),
            sources=("vwap", "anchored_vwap", "pivot_points"),
        )
    if abs(vwap_distance) >= 35.0:
        _add_signal(
            vetoes,
            signal_id="vwap_overextension",
            family="levels",
            side="risk",
            strength=_clamp(abs(vwap_distance) / 80.0, 0.4, 1.0),
            sources=("vwap", "anchored_vwap", "fibonacci_retracement"),
        )

    momentum_60 = _number(features.get("external_momentum_60s_bps"))
    momentum_300 = _number(features.get("external_momentum_300s_bps"))
    momentum_score = momentum_60 + 0.65 * momentum_300
    if abs(momentum_score) >= 1.5:
        _add_signal(
            signals,
            signal_id="momentum_roc_confirmed",
            family="momentum",
            side=_side_from_signed(momentum_score),
            strength=_clamp(abs(momentum_score) / 45.0, 0.2, 1.0),
            sources=("macd", "roc", "momentum", "trix", "tsi"),
        )

    flow_score = (
        1.0 * _number(features.get("trade_flow_bias"))
        + 0.8 * _number(features.get("book_imbalance"))
        + 0.4 * _signed_delta_score(features)
    )
    if abs(flow_score) >= 0.12:
        _add_signal(
            signals,
            signal_id="volume_flow_bias",
            family="volume_flow",
            side=_side_from_signed(flow_score),
            strength=_clamp(abs(flow_score), 0.2, 1.0),
            sources=("volume", "obv", "chaikin_money_flow", "net_volume", "volume_delta"),
        )

    volume_ratio = _number(features.get("volume_ratio"))
    if volume_ratio >= 1.35:
        _add_signal(
            signals,
            signal_id="volume_expansion",
            family="volume_flow",
            side="confirm",
            strength=_clamp((volume_ratio - 1.0) / 3.0, 0.2, 1.0),
            sources=("volume", "volume_profile", "mfi", "pvt"),
        )
    elif 0.0 < volume_ratio <= 0.55:
        _add_signal(
            vetoes,
            signal_id="volume_contraction",
            family="volume_flow",
            side="risk",
            strength=_clamp(1.0 - volume_ratio, 0.3, 1.0),
            sources=("volume", "volume_profile", "ease_of_movement"),
        )

    volatility = _number(features.get("realized_vol_short_bps"))
    range_bps = _number(features.get("bucket_range_bps"))
    compression = _number(features.get("compression_score"))
    if volatility >= 25.0 or range_bps >= 25.0:
        _add_signal(
            signals,
            signal_id="volatility_band_expansion",
            family="volatility_bands",
            side="risk",
            strength=_clamp(max(volatility, range_bps) / 80.0, 0.25, 1.0),
            sources=("atr", "bollinger_bands", "keltner_channels", "donchian_channels"),
        )
    if volatility >= 70.0:
        _add_signal(
            vetoes,
            signal_id="atr_extreme",
            family="volatility_bands",
            side="risk",
            strength=_clamp(volatility / 120.0, 0.45, 1.0),
            sources=("atr", "bollinger_bands", "keltner_channels"),
        )
    if compression >= 0.6:
        _add_signal(
            signals,
            signal_id="compression_squeeze",
            family="volatility_bands",
            side="watch",
            strength=_clamp(compression, 0.2, 1.0),
            sources=("bollinger_bands", "keltner_channels", "donchian_channels"),
        )

    rating_score = _technical_rating_score(signals, vetoes)
    if abs(rating_score) >= 0.2:
        _add_signal(
            signals,
            signal_id="technical_rating_proxy",
            family="composite",
            side=_side_from_signed(rating_score),
            strength=_clamp(abs(rating_score), 0.2, 1.0),
            sources=("adx_dmi", "technical_ratings"),
        )

    _add_conflicts(features, conflicts)
    bias = _bias_from_signals(signals, vetoes, conflicts)
    digest: dict[str, object] = {
        "schema_version": TRIDENT_AI_TECHNICAL_DIGEST_SCHEMA_VERSION,
        "coverage": _coverage_payload(),
        "bias": bias,
        "families": _family_payload(signals, vetoes, conflicts),
        "top_signals": _trim_signals(signals, MAX_TECHNICAL_TOP_SIGNALS),
        "veto_signals": _trim_signals(vetoes, MAX_TECHNICAL_VETO_SIGNALS),
        "conflicts": _trim_signals(conflicts, MAX_TECHNICAL_CONFLICTS),
        "omitted_neutral_count": _omitted_neutral_count(signals, vetoes, conflicts),
    }
    return _enforce_digest_budget(digest, max_chars=max_chars)


def compact_technical_digest(
    value: object,
    *,
    max_chars: int = MAX_TECHNICAL_DIGEST_CHARS,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    coverage = value.get("coverage")
    bias = value.get("bias")
    compact: dict[str, object] = {
        "schema_version": str(
            value.get("schema_version", TRIDENT_AI_TECHNICAL_DIGEST_SCHEMA_VERSION)
        )[:64],
        "coverage": _compact_coverage(coverage),
        "bias": _compact_bias(bias),
        "families": _compact_families(value.get("families")),
        "top_signals": _trim_signals(
            _sequence_of_mappings(value.get("top_signals")),
            MAX_TECHNICAL_TOP_SIGNALS,
        ),
        "veto_signals": _trim_signals(
            _sequence_of_mappings(value.get("veto_signals")),
            MAX_TECHNICAL_VETO_SIGNALS,
        ),
        "conflicts": _trim_signals(
            _sequence_of_mappings(value.get("conflicts")),
            MAX_TECHNICAL_CONFLICTS,
        ),
        "omitted_neutral_count": int(max(_number(value.get("omitted_neutral_count")), 0.0)),
    }
    return _enforce_digest_budget(compact, max_chars=max_chars)


def _coverage_payload() -> dict[str, object]:
    family_counts = Counter(spec[3] for spec in TECHNICAL_INDICATOR_COVERAGE)
    return {
        "universe": "tradingview_top50",
        "used_count": len(TECHNICAL_INDICATOR_COVERAGE),
        "missing_count": 0,
        "source": "trident_snapshot_local_proxy",
        "mode": "compact_proxy_v1",
        "families": dict(sorted(family_counts.items())),
    }


def _compact_coverage(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return _coverage_payload()
    families = value.get("families")
    family_payload = (
        {
            str(key)[:32]: int(max(_number(item), 0.0))
            for key, item in families.items()
            if isinstance(key, str)
        }
        if isinstance(families, Mapping)
        else {}
    )
    return {
        "universe": str(value.get("universe", "tradingview_top50"))[:40],
        "used_count": int(
            max(_number(value.get("used_count", len(TECHNICAL_INDICATOR_COVERAGE))), 0.0)
        ),
        "missing_count": int(max(_number(value.get("missing_count")), 0.0)),
        "source": str(value.get("source", "trident_snapshot_local_proxy"))[:48],
        "mode": str(value.get("mode", "compact_proxy_v1"))[:32],
        "families": family_payload or _coverage_payload()["families"],
    }


def _add_signal(
    target: list[dict[str, object]],
    *,
    signal_id: str,
    family: str,
    side: str,
    strength: float,
    sources: Sequence[str],
) -> None:
    target.append(
        {
            "id": signal_id,
            "fam": family,
        "side": side,
        "strength": round(_clamp(strength, 0.0, 1.0), 4),
        "src": [str(item) for item in sources[:3]],
        }
    )


def _add_conflicts(
    features: Mapping[str, object],
    conflicts: list[dict[str, object]],
) -> None:
    flow = _number(features.get("trade_flow_bias"))
    book = _number(features.get("book_imbalance"))
    if abs(flow) >= 0.15 and abs(book) >= 0.15 and flow * book < 0.0:
        _add_signal(
            conflicts,
            signal_id="flow_book_conflict",
            family="volume_flow",
            side="risk",
            strength=_clamp((abs(flow) + abs(book)) / 2.0, 0.25, 1.0),
            sources=("obv", "chaikin_money_flow", "volume_delta"),
        )
    ema_side = str(features.get("ema_alignment", "") or "").lower()
    momentum = _number(features.get("external_momentum_60s_bps")) + 0.65 * _number(
        features.get("external_momentum_300s_bps")
    )
    if ema_side == "bullish" and momentum <= -3.0:
        _add_signal(
            conflicts,
            signal_id="trend_momentum_conflict",
            family="momentum",
            side="risk",
            strength=_clamp(abs(momentum) / 35.0, 0.25, 1.0),
            sources=("ema", "macd", "roc", "momentum"),
        )
    elif ema_side == "bearish" and momentum >= 3.0:
        _add_signal(
            conflicts,
            signal_id="trend_momentum_conflict",
            family="momentum",
            side="risk",
            strength=_clamp(abs(momentum) / 35.0, 0.25, 1.0),
            sources=("ema", "macd", "roc", "momentum"),
        )


def _bias_from_signals(
    signals: Sequence[Mapping[str, object]],
    vetoes: Sequence[Mapping[str, object]],
    conflicts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    score = 0.0
    for signal in signals:
        strength = _number(signal.get("strength"))
        side = str(signal.get("side", "") or "")
        if side == "long":
            score += strength
        elif side == "short":
            score -= strength
    if conflicts:
        score *= 0.75
    if vetoes:
        score *= 0.85
    normalized = _clamp(score / max(len(signals), 1), -1.0, 1.0)
    if abs(normalized) < 0.12:
        side = "mixed"
    else:
        side = "long" if normalized > 0.0 else "short"
    return {
        "side": side,
        "score": round(normalized, 4),
        "quality": _quality_bucket(abs(normalized), bool(vetoes), bool(conflicts)),
    }


def _compact_bias(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"side": "mixed", "score": 0.0, "quality": "low"}
    return {
        "side": str(value.get("side", "mixed"))[:12],
        "score": round(_number(value.get("score")), 4),
        "quality": str(value.get("quality", "low"))[:16],
    }


def _family_payload(
    signals: Sequence[Mapping[str, object]],
    vetoes: Sequence[Mapping[str, object]],
    conflicts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    families = sorted({spec[3] for spec in TECHNICAL_INDICATOR_COVERAGE})
    for family in families:
        family_signals = [item for item in signals if item.get("fam") == family]
        family_vetoes = [item for item in vetoes if item.get("fam") == family]
        family_conflicts = [item for item in conflicts if item.get("fam") == family]
        payload[family] = _family_state(family_signals, family_vetoes, family_conflicts)
    return payload


def _compact_families(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    compact: dict[str, object] = {}
    for family, item in value.items():
        if not isinstance(family, str):
            continue
        if isinstance(item, Mapping):
            compact[family[:32]] = str(item.get("state", "neutral"))[:16]
        else:
            compact[family[:32]] = str(item or "neutral")[:16]
    return compact


def _family_state(
    signals: Sequence[Mapping[str, object]],
    vetoes: Sequence[Mapping[str, object]],
    conflicts: Sequence[Mapping[str, object]],
) -> str:
    if vetoes:
        return "veto"
    if conflicts:
        return "conflict"
    sides = {str(item.get("side", "") or "") for item in signals}
    if "long" in sides and "short" in sides:
        return "mixed"
    if "long" in sides:
        return "long"
    if "short" in sides:
        return "short"
    if "risk" in sides:
        return "risk"
    if signals:
        return "active"
    return "neutral"


def _trim_signals(
    signals: Sequence[Mapping[str, object]],
    limit: int,
) -> list[dict[str, object]]:
    sorted_signals = sorted(
        signals,
        key=lambda item: (_number(item.get("strength")), str(item.get("id", ""))),
        reverse=True,
    )
    return [_compact_signal(item) for item in sorted_signals[:limit]]


def _compact_signal(signal: Mapping[str, object]) -> dict[str, object]:
    sources = signal.get("src", [])
    source_items = (
        sources
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes, bytearray))
        else []
    )
    return {
        "id": str(signal.get("id", ""))[:48],
        "fam": str(signal.get("fam", ""))[:24],
        "side": str(signal.get("side", ""))[:12],
        "strength": round(_number(signal.get("strength")), 4),
        "src": [
            str(item)[:32]
            for item in source_items
            if not isinstance(item, (bytes, bytearray))
        ][:3],
    }


def _sequence_of_mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _omitted_neutral_count(
    signals: Sequence[Mapping[str, object]],
    vetoes: Sequence[Mapping[str, object]],
    conflicts: Sequence[Mapping[str, object]],
) -> int:
    active_sources: set[str] = set()
    for signal in (*signals, *vetoes, *conflicts):
        sources = signal.get("src", [])
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes, bytearray)):
            continue
        active_sources.update(str(item) for item in sources)
    all_sources = {spec[1] for spec in TECHNICAL_INDICATOR_COVERAGE}
    return max(0, len(all_sources - active_sources))


def _enforce_digest_budget(
    digest: dict[str, object],
    *,
    max_chars: int,
) -> dict[str, object]:
    compact = _with_char_count(dict(digest))
    if _encoded_len(compact) <= max_chars:
        return compact

    top_signals = compact.get("top_signals")
    veto_signals = compact.get("veto_signals")
    conflicts = compact.get("conflicts")
    if isinstance(top_signals, list):
        compact["top_signals"] = top_signals[:6]
    if isinstance(veto_signals, list):
        compact["veto_signals"] = veto_signals[:3]
    if isinstance(conflicts, list):
        compact["conflicts"] = conflicts[:3]
    compact = _with_char_count(compact)
    if _encoded_len(compact) <= max_chars:
        return compact

    coverage = compact.get("coverage")
    if isinstance(coverage, Mapping):
        compact["coverage"] = {
            "universe": coverage.get("universe", "tradingview_top50"),
            "used_count": coverage.get("used_count", 50),
            "missing_count": coverage.get("missing_count", 0),
            "source": coverage.get("source", "trident_snapshot_local_proxy"),
            "mode": coverage.get("mode", "compact_proxy_v1"),
        }
    compact = _with_char_count(compact)
    if _encoded_len(compact) <= max_chars:
        return compact

    top_signals = compact.get("top_signals")
    if isinstance(top_signals, list):
        compact["top_signals"] = top_signals[:5]
    compact = _with_char_count(compact)
    if _encoded_len(compact) <= max_chars:
        return compact

    compact = _without_signal_sources(compact)
    compact = _with_char_count(compact)
    if _encoded_len(compact) <= max_chars:
        return compact

    compact["families"] = {}
    compact = _with_char_count(compact)
    if _encoded_len(compact) <= max_chars:
        return compact

    top_signals = compact.get("top_signals")
    veto_signals = compact.get("veto_signals")
    conflicts = compact.get("conflicts")
    if isinstance(top_signals, list):
        compact["top_signals"] = top_signals[:4]
    if isinstance(veto_signals, list):
        compact["veto_signals"] = veto_signals[:2]
    if isinstance(conflicts, list):
        compact["conflicts"] = conflicts[:2]
    return _with_char_count(compact)


def _without_signal_sources(digest: dict[str, object]) -> dict[str, object]:
    compact = dict(digest)
    for key in ("top_signals", "veto_signals", "conflicts"):
        signals = compact.get(key)
        if not isinstance(signals, list):
            continue
        compact[key] = [
            {item_key: item_value for item_key, item_value in item.items() if item_key != "src"}
            if isinstance(item, Mapping)
            else item
            for item in signals
        ]
    return compact


def _with_char_count(digest: dict[str, object]) -> dict[str, object]:
    payload = dict(digest)
    payload.pop("char_count", None)
    for _ in range(3):
        payload["char_count"] = _encoded_len(payload)
    return payload


def _encoded_len(payload: Mapping[str, object]) -> int:
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _ema_gap_strength(features: Mapping[str, object]) -> float:
    fast = _number(features.get("ema_fast"))
    slow = _number(features.get("ema_slow"))
    price = _number(features.get("price"))
    denominator = price if price > 0.0 else max(abs(slow), abs(fast), 1.0)
    gap_bps = abs(fast - slow) / denominator * 10_000.0
    return _clamp(gap_bps / 40.0, 0.0, 1.0)


def _signed_delta_score(features: Mapping[str, object]) -> float:
    signed_delta = _number(features.get("signed_trade_delta"))
    buy_volume = max(_number(features.get("buy_volume")), 0.0)
    sell_volume = max(_number(features.get("sell_volume")), 0.0)
    total_volume = buy_volume + sell_volume
    if total_volume > 0.0:
        return _clamp(signed_delta / total_volume, -1.0, 1.0)
    bucket_volume = max(_number(features.get("bucket_volume")), 1.0)
    return _clamp(signed_delta / bucket_volume, -1.0, 1.0)


def _technical_rating_score(
    signals: Sequence[Mapping[str, object]],
    vetoes: Sequence[Mapping[str, object]],
) -> float:
    score = 0.0
    for signal in signals:
        side = str(signal.get("side", "") or "")
        strength = _number(signal.get("strength"))
        if side == "long":
            score += strength
        elif side == "short":
            score -= strength
    if vetoes:
        score *= 0.8
    return _clamp(score / max(len(signals), 1), -1.0, 1.0)


def _side_from_signed(value: float) -> str:
    if value > 0.0:
        return "long"
    if value < 0.0:
        return "short"
    return "neutral"


def _quality_bucket(strength: float, has_veto: bool, has_conflict: bool) -> str:
    if has_veto or has_conflict:
        return "veto_or_conflict"
    if strength >= 0.55:
        return "high"
    if strength >= 0.25:
        return "medium"
    return "low"


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
