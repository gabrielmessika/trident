from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from app.trident.pod_a.order_block_shadow import OrderBlockShadowFeatures
from app.trident.pod_a.regime_shadow import RegimeShadowFeatures, parse_timestamp
from app.trident.types import SymbolMarketSnapshot


@dataclass(slots=True)
class SymbolGuardState:
    state: str = "normal"
    entered_at: str = ""
    last_score: float = 0.0
    reason: str = ""
    ttl_until: str = ""
    last_exit_check: str = ""
    transition_count: int = 0
    subscores: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class SymbolGuardFeatures:
    timestamp: str
    symbol: str
    state: str
    previous_state: str
    falling_knife_score: float
    reason: str
    would_throttle: bool
    would_block: bool
    would_reduce_cap: bool
    shadow_cap_multiplier: float
    quarantine_until: str
    quarantine_exit_reason: str
    structural_block_candidate: bool
    subscores: dict[str, float]


class PodADynamicSymbolGuard:
    """Observation-only falling-knife guard for P1-08."""

    THROTTLE_SCORE = 55.0
    QUARANTINE_SCORE = 75.0
    EXIT_SCORE = 45.0
    THROTTLE_TTL = timedelta(hours=3)
    QUARANTINE_TTL = timedelta(hours=6)
    EXIT_CONFIRMATION = timedelta(minutes=60)

    def __init__(self, state_path: str | Path | None = None) -> None:
        self.state_path = Path(state_path) if state_path is not None else None
        self.states: dict[str, SymbolGuardState] = {}
        if self.state_path is not None:
            self.load()

    def load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        raw_states = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(raw_states, dict):
            return
        loaded: dict[str, SymbolGuardState] = {}
        for symbol, raw in raw_states.items():
            if not isinstance(raw, dict):
                continue
            loaded[str(symbol).upper()] = SymbolGuardState(
                state=str(raw.get("state") or "normal"),
                entered_at=str(raw.get("entered_at") or ""),
                last_score=_float(raw.get("last_score")),
                reason=str(raw.get("reason") or ""),
                ttl_until=str(raw.get("ttl_until") or ""),
                last_exit_check=str(raw.get("last_exit_check") or ""),
                transition_count=int(_float(raw.get("transition_count"))),
                subscores={
                    str(key): _float(value)
                    for key, value in (raw.get("subscores") or {}).items()
                }
                if isinstance(raw.get("subscores"), dict)
                else {},
            )
        self.states = loaded

    def save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mode": "observation_only",
            "symbols": {
                symbol: {
                    "state": state.state,
                    "entered_at": state.entered_at,
                    "last_score": round(state.last_score, 4),
                    "reason": state.reason,
                    "ttl_until": state.ttl_until,
                    "last_exit_check": state.last_exit_check,
                    "transition_count": state.transition_count,
                    "subscores": {key: round(value, 4) for key, value in state.subscores.items()},
                }
                for symbol, state in sorted(self.states.items())
            },
        }
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def evaluate(
        self,
        *,
        timestamp: str | datetime,
        snapshots: list[SymbolMarketSnapshot],
        regime_features: Mapping[str, RegimeShadowFeatures],
        order_block_features: Mapping[str, OrderBlockShadowFeatures],
    ) -> dict[str, SymbolGuardFeatures]:
        parsed = parse_timestamp(timestamp)
        if parsed is None:
            return {}
        features: dict[str, SymbolGuardFeatures] = {}
        for snapshot in snapshots:
            symbol = snapshot.symbol.upper()
            if symbol.startswith("XYZ:"):
                continue
            score, reason, subscores = falling_knife_score(
                snapshot=snapshot,
                regime_features=regime_features.get(symbol),
                order_block_features=order_block_features.get(symbol),
            )
            features[symbol] = self._transition(
                timestamp=parsed,
                symbol=symbol,
                score=score,
                reason=reason,
                subscores=subscores,
            )
        return features

    def _transition(
        self,
        *,
        timestamp: datetime,
        symbol: str,
        score: float,
        reason: str,
        subscores: dict[str, float],
    ) -> SymbolGuardFeatures:
        current = self.states.setdefault(symbol, SymbolGuardState(entered_at=isoformat(timestamp)))
        previous_state = current.state
        next_state = current.state
        exit_reason = ""
        ttl = parse_timestamp(current.ttl_until) if current.ttl_until else None
        if score >= self.QUARANTINE_SCORE:
            next_state = "quarantine"
            ttl = timestamp + self.QUARANTINE_TTL
        elif score >= self.THROTTLE_SCORE:
            if current.state != "quarantine" or (ttl is not None and timestamp >= ttl):
                next_state = "throttle"
                ttl = timestamp + self.THROTTLE_TTL
        elif current.state in {"throttle", "quarantine"}:
            if score <= self.EXIT_SCORE:
                last_exit = parse_timestamp(current.last_exit_check) if current.last_exit_check else None
                if last_exit is None:
                    current.last_exit_check = isoformat(timestamp)
                elif timestamp - last_exit >= self.EXIT_CONFIRMATION:
                    next_state = "normal"
                    ttl = None
                    exit_reason = "score_recovered_for_60m"
            elif ttl is not None and timestamp >= ttl and score <= self.THROTTLE_SCORE:
                next_state = "normal"
                ttl = None
                exit_reason = "ttl_expired_score_below_throttle"

        if next_state != current.state:
            current.transition_count += 1
            current.entered_at = isoformat(timestamp)
            current.last_exit_check = ""
        if score > self.EXIT_SCORE:
            current.last_exit_check = ""
        current.state = next_state
        current.last_score = score
        current.reason = reason
        current.ttl_until = isoformat(ttl) if ttl is not None and next_state != "normal" else ""
        current.subscores = dict(subscores)

        would_block = current.state == "quarantine"
        would_throttle = current.state in {"throttle", "quarantine"}
        return SymbolGuardFeatures(
            timestamp=isoformat(timestamp),
            symbol=symbol,
            state=current.state,
            previous_state=previous_state,
            falling_knife_score=round(score, 4),
            reason=reason,
            would_throttle=would_throttle,
            would_block=would_block,
            would_reduce_cap=would_throttle,
            shadow_cap_multiplier=0.5 if current.state == "throttle" else (0.0 if would_block else 1.0),
            quarantine_until=current.ttl_until if current.state == "quarantine" else "",
            quarantine_exit_reason=exit_reason,
            structural_block_candidate=(
                current.transition_count >= 3 and current.state == "quarantine"
            ),
            subscores=dict(subscores),
        )


def falling_knife_score(
    *,
    snapshot: SymbolMarketSnapshot,
    regime_features: RegimeShadowFeatures | None,
    order_block_features: OrderBlockShadowFeatures | None,
) -> tuple[float, str, dict[str, float]]:
    regime = 0.0
    relative = 0.0
    structure = 0.0
    volatility = 0.0
    order_block = 0.0
    stops = 0.0

    if regime_features is not None:
        if regime_features.regime_gate_decision == "bearish":
            regime = 20.0
        elif regime_features.regime_gate_decision == "defensive":
            regime = 14.0
        elif regime_features.bear_regime_score >= 3:
            regime = 10.0
        btc_60 = regime_features.btc_ret_60m_bps
        symbol_60 = regime_features.symbol_ret_60m_bps
        symbol_240 = regime_features.symbol_ret_240m_bps
        if btc_60 is not None and symbol_60 is not None:
            underperformance = btc_60 - symbol_60
            if underperformance >= 80.0:
                relative = 20.0
            elif underperformance >= 40.0:
                relative = 14.0
            elif underperformance >= 20.0:
                relative = 8.0
        if symbol_60 is not None and symbol_240 is not None:
            if symbol_60 <= -80.0 and symbol_240 <= -180.0:
                relative = max(relative, 18.0)
            elif symbol_60 <= -40.0 and symbol_240 <= -120.0:
                relative = max(relative, 12.0)
        if not regime_features.symbol_above_ema_slow and not regime_features.symbol_fast_above_slow:
            structure += 10.0
        elif not regime_features.symbol_above_ema_slow:
            structure += 6.0
    if snapshot.vwap_distance_bps <= -35.0:
        structure += 6.0
    if snapshot.structure_score <= 0.15:
        structure += 4.0
    structure = min(structure, 20.0)

    if snapshot.spread_bps >= 12.0:
        volatility += 7.0
    elif snapshot.spread_bps >= 6.0:
        volatility += 4.0
    if snapshot.bucket_range_bps >= 120.0:
        volatility += 8.0
    elif snapshot.bucket_range_bps >= 70.0:
        volatility += 5.0
    volatility = min(volatility, 15.0)

    if order_block_features is not None and order_block_features.bearish_order_blocks_1h4h:
        order_block = 15.0

    subscores = {
        "regime": regime,
        "relative_weakness": relative,
        "structure": structure,
        "volatility_slippage": volatility,
        "order_block": order_block,
        "recent_stops": stops,
    }
    score = min(sum(subscores.values()), 100.0)
    reasons = [key for key, value in subscores.items() if value > 0]
    return score, ",".join(reasons) if reasons else "normal", subscores


def symbol_guard_details(features: SymbolGuardFeatures | None) -> dict[str, object]:
    if features is None:
        return {
            "symbol_guard_shadow_mode": "observation_only",
            "symbol_guard_state": "missing_features",
            "previous_symbol_guard_state": "missing_features",
            "falling_knife_score": None,
            "falling_knife_reason": "missing_features",
            "would_throttle_dynamic_symbol_guard": False,
            "would_block_dynamic_symbol_guard": False,
            "would_reduce_cap_dynamic_symbol_guard": False,
            "shadow_cap_multiplier": 1.0,
            "quarantine_until": "",
            "quarantine_exit_reason": "",
            "structural_block_candidate": False,
            "symbol_guard_live_action_unchanged": True,
        }
    details: dict[str, object] = {
        "symbol_guard_shadow_mode": "observation_only",
        "symbol_guard_state": features.state,
        "previous_symbol_guard_state": features.previous_state,
        "falling_knife_score": features.falling_knife_score,
        "falling_knife_reason": features.reason,
        "would_throttle_dynamic_symbol_guard": features.would_throttle,
        "would_block_dynamic_symbol_guard": features.would_block,
        "would_reduce_cap_dynamic_symbol_guard": features.would_reduce_cap,
        "shadow_cap_multiplier": features.shadow_cap_multiplier,
        "quarantine_until": features.quarantine_until,
        "quarantine_exit_reason": features.quarantine_exit_reason,
        "structural_block_candidate": features.structural_block_candidate,
        "symbol_guard_live_action_unchanged": True,
    }
    for key, value in features.subscores.items():
        details[f"falling_knife_{key}_score"] = round(value, 4)
    return details


def symbol_guard_setup_details(details: Mapping[str, object]) -> dict[str, float | str | bool]:
    allowed = {
        "symbol_guard_shadow_mode",
        "symbol_guard_state",
        "previous_symbol_guard_state",
        "falling_knife_score",
        "falling_knife_reason",
        "would_throttle_dynamic_symbol_guard",
        "would_block_dynamic_symbol_guard",
        "would_reduce_cap_dynamic_symbol_guard",
        "shadow_cap_multiplier",
        "quarantine_until",
        "quarantine_exit_reason",
        "structural_block_candidate",
        "symbol_guard_live_action_unchanged",
        "falling_knife_regime_score",
        "falling_knife_relative_weakness_score",
        "falling_knife_structure_score",
        "falling_knife_volatility_slippage_score",
        "falling_knife_order_block_score",
        "falling_knife_recent_stops_score",
    }
    return {
        key: _setup_detail_value(value)
        for key, value in details.items()
        if key in allowed
    }


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _setup_detail_value(value: object) -> float | str | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return ""
    return str(value)
