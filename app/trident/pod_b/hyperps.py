from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.settings import AppConfig
from app.trident.types import PodAllocation, SignalPreview, SymbolMarketSnapshot, TradePlan


ACTIVE_HYPERP_PHASE = "active_hyperp"
COOLING_OFF_PHASE = "cooling_off"
ALUMNI_PHASE = "alumni"
RETIRED_PHASE = "retired"
UNKNOWN_PHASE = "unknown"


@dataclass(frozen=True, slots=True)
class HyperpLifecyclePolicy:
    """Controls how a former Hyperp fades out of the Pod B sleeve."""

    half_life_days: float = 30.0
    cooling_off_days: int = 30
    retired_after_days: int = 120
    min_trade_weight: float = 0.15
    cooling_strictness: float = 1.10
    alumni_strictness: float = 1.25

    def state_from_dates(
        self,
        *,
        symbol: str,
        as_of: datetime,
        first_seen: datetime | None,
        last_seen: datetime | None,
        active_now: bool,
    ) -> "HyperpLifecycleState":
        if active_now:
            return HyperpLifecycleState(
                symbol=symbol.upper(),
                phase=ACTIVE_HYPERP_PHASE,
                weight=1.0,
                strictness_multiplier=1.0,
                first_seen=first_seen,
                last_seen=as_of,
                days_since_active=0.0,
            )
        if last_seen is None:
            return HyperpLifecycleState(
                symbol=symbol.upper(),
                phase=UNKNOWN_PHASE,
                weight=0.0,
                strictness_multiplier=self.alumni_strictness,
                first_seen=first_seen,
                last_seen=None,
                days_since_active=None,
            )
        days_since = max((as_of - last_seen).total_seconds() / 86_400.0, 0.0)
        if days_since > self.retired_after_days:
            phase = RETIRED_PHASE
            weight = 0.0
            strictness = self.alumni_strictness
        else:
            half_life = max(float(self.half_life_days), 1e-9)
            weight = math.exp(-days_since / half_life)
            if weight < self.min_trade_weight:
                phase = RETIRED_PHASE
                weight = 0.0
                strictness = self.alumni_strictness
            elif days_since <= self.cooling_off_days:
                phase = COOLING_OFF_PHASE
                strictness = self.cooling_strictness
            else:
                phase = ALUMNI_PHASE
                strictness = self.alumni_strictness
        return HyperpLifecycleState(
            symbol=symbol.upper(),
            phase=phase,
            weight=round(weight, 6),
            strictness_multiplier=strictness,
            first_seen=first_seen,
            last_seen=last_seen,
            days_since_active=round(days_since, 4),
        )


@dataclass(frozen=True, slots=True)
class HyperpLifecycleState:
    symbol: str
    phase: str
    weight: float
    strictness_multiplier: float
    first_seen: datetime | None
    last_seen: datetime | None
    days_since_active: float | None

    @property
    def tradable(self) -> bool:
        return self.phase not in {UNKNOWN_PHASE, RETIRED_PHASE} and self.weight > 0.0

    def to_details(self) -> dict[str, float | str | bool]:
        return {
            "lifecycle_phase": self.phase,
            "lifecycle_weight": float(self.weight),
            "lifecycle_strictness": float(self.strictness_multiplier),
            "lifecycle_days_since_active": (
                float(self.days_since_active)
                if self.days_since_active is not None
                else -1.0
            ),
            "lifecycle_tradable": self.tradable,
        }


@dataclass(frozen=True, slots=True)
class HyperpUniverseSnapshot:
    timestamp: str
    symbols: tuple[str, ...]
    source: str = "hyperliquid_metaAndAssetCtxs"

    @property
    def as_datetime(self) -> datetime:
        return _parse_datetime(self.timestamp)

    def to_json(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "symbols": list(self.symbols),
            "source": self.source,
        }

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> "HyperpUniverseSnapshot":
        return cls(
            timestamp=str(payload.get("timestamp", "")),
            symbols=tuple(
                sorted(
                    {
                        str(symbol).strip().upper()
                        for symbol in payload.get("symbols", [])
                        if str(symbol).strip()
                    }
                )
            ),
            source=str(payload.get("source", "hyperliquid_metaAndAssetCtxs")),
        )


class HyperpUniverseRegistry:
    """Snapshot-backed Hyperp universe with no-lookahead lifecycle states."""

    def __init__(
        self,
        snapshots: Iterable[HyperpUniverseSnapshot] = (),
        *,
        policy: HyperpLifecyclePolicy | None = None,
    ) -> None:
        self.policy = policy or HyperpLifecyclePolicy()
        self.snapshots = sorted(snapshots, key=lambda snapshot: snapshot.as_datetime)

    @classmethod
    def from_jsonl(
        cls,
        path: str | Path,
        *,
        policy: HyperpLifecyclePolicy | None = None,
    ) -> "HyperpUniverseRegistry":
        snapshot_path = Path(path)
        snapshots: list[HyperpUniverseSnapshot] = []
        if snapshot_path.exists():
            for line in snapshot_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    snapshots.append(HyperpUniverseSnapshot.from_json(payload))
        return cls(snapshots, policy=policy)

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        timestamp: str | None = None,
        policy: HyperpLifecyclePolicy | None = None,
    ) -> "HyperpUniverseRegistry":
        return cls(
            [
                HyperpUniverseSnapshot(
                    timestamp=timestamp or _utc_now_iso(),
                    symbols=tuple(extract_active_hyperp_symbols(payload)),
                )
            ],
            policy=policy,
        )

    def append_jsonl(self, path: str | Path, snapshot: HyperpUniverseSnapshot) -> None:
        snapshot_path = Path(path)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with snapshot_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot.to_json(), sort_keys=True) + "\n")
        self.snapshots.append(snapshot)
        self.snapshots.sort(key=lambda item: item.as_datetime)

    def fetch_snapshot(self, info_client: object, *, timestamp: str | None = None) -> HyperpUniverseSnapshot:
        payload = info_client.post_info({"type": "metaAndAssetCtxs"})
        return HyperpUniverseSnapshot(
            timestamp=timestamp or _utc_now_iso(),
            symbols=tuple(extract_active_hyperp_symbols(payload)),
        )

    def known_symbols(self, *, as_of: str | datetime | None = None) -> list[str]:
        cutoff = _parse_datetime(as_of) if as_of is not None else None
        symbols: set[str] = set()
        for snapshot in self.snapshots:
            if cutoff is not None and snapshot.as_datetime > cutoff:
                break
            symbols.update(snapshot.symbols)
        return sorted(symbols)

    def state_for(self, symbol: str, as_of: str | datetime | None) -> HyperpLifecycleState:
        normalized = symbol.upper()
        as_of_dt = _parse_datetime(as_of)
        applicable = [
            snapshot
            for snapshot in self.snapshots
            if snapshot.as_datetime <= as_of_dt
        ]
        first_seen: datetime | None = None
        last_seen: datetime | None = None
        active_now = False
        for snapshot in applicable:
            if normalized not in snapshot.symbols:
                continue
            first_seen = first_seen or snapshot.as_datetime
            last_seen = snapshot.as_datetime
        if applicable and normalized in applicable[-1].symbols:
            active_now = True
        return self.policy.state_from_dates(
            symbol=normalized,
            as_of=as_of_dt,
            first_seen=first_seen,
            last_seen=last_seen,
            active_now=active_now,
        )

    def states_for(
        self,
        symbols: Iterable[str],
        as_of: str | datetime | None,
    ) -> dict[str, HyperpLifecycleState]:
        return {
            symbol.upper(): self.state_for(symbol.upper(), as_of)
            for symbol in symbols
        }


@dataclass(slots=True)
class HyperpThresholds:
    symbol: str
    positive_funding_extreme: float
    negative_funding_extreme: float
    abs_deviation_extreme_bps: float
    event_range_bps: float
    event_volume_ratio: float


@dataclass(slots=True)
class HyperpReversionProfile:
    name: str = "draft_strict"
    trigger_mode: str = "funding_reversion"
    funding_percentile: float = 0.90
    deviation_percentile: float = 0.80
    min_abs_funding_rate: float = 0.00002
    min_deviation_bps: float = 45.0
    long_rsi_max: float = 42.0
    short_rsi_min: float = 58.0
    require_rsi: bool = True
    require_rejection: bool = True
    require_flow_confirmation: bool = False
    allow_longs: bool = True
    allow_shorts: bool = True
    allowed_regimes: tuple[str, ...] = ()
    max_spread_bps: float = 8.0
    min_bucket_notional_usd: float = 100.0
    min_bucket_trade_count: int = 3
    max_event_range_multiplier: float = 1.40
    max_event_volume_ratio: float = 8.0
    block_event_spikes: bool = True
    min_score: int = 4
    top_n: int = 5
    min_interest_score: float = 0.55
    min_volume_ratio: float = 1.20
    min_trade_count_ratio: float = 1.05
    risk_per_trade_pct: float = 0.0035
    default_leverage: float = 1.5
    max_leverage: float = 3.0
    size_multiplier: float = 0.60
    min_margin_usd: float = 10.0
    min_notional_usd: float = 10.0
    max_concurrent_positions: int = 2
    max_total_open_risk_pct: float = 0.008
    stop_vol_multiplier: float = 1.30
    stop_floor_bps: float = 35.0
    stop_ceiling_bps: float = 180.0
    take_profit_extension_fraction: float = 0.55
    take_profit_stop_ratio: float = 0.85
    take_profit_ceiling_bps: float = 140.0
    break_even_stop_ratio: float = 0.55
    trailing_activation_stop_ratio: float = 0.95
    trailing_distance_stop_ratio: float = 0.60
    time_stop_hours: int = 4
    reentry_cooldown_minutes: int = 180


@dataclass(slots=True)
class HyperpReversionContext:
    snapshot: SymbolMarketSnapshot
    regime: str
    thresholds: HyperpThresholds
    rsi14: float | None
    price_move_bps: float
    timestamp: str | None = None
    lifecycle_phase: str = ACTIVE_HYPERP_PHASE
    lifecycle_weight: float = 1.0
    lifecycle_days_since_active: float | None = None
    lifecycle_strictness: float = 1.0


@dataclass(slots=True)
class HyperpRiskGate:
    config: AppConfig
    profile: HyperpReversionProfile
    _recent_pnl_by_symbol: dict[str, list[float]] = field(default_factory=dict)

    def evaluate_many(self, plans: list[TradePlan], open_positions: dict[str, object]) -> list[object]:
        from app.trident.types import RiskDecision

        decisions: list[RiskDecision] = []
        total_equity = max(float(self.config.trident.capital.reference_equity_usd), 1e-9)
        open_risk = sum(
            max(float(getattr(position, "expected_loss_usd", 0.0)), 0.0)
            for position in open_positions.values()
        )
        for plan in plans:
            accepted = True
            reason = "accepted"
            if len(open_positions) >= self.profile.max_concurrent_positions:
                accepted = False
                reason = "max_concurrent_positions"
            elif plan.symbol in open_positions:
                accepted = False
                reason = "already_open"
            elif plan.margin_usd < self.profile.min_margin_usd:
                accepted = False
                reason = "min_margin"
            elif plan.target_notional_usd < self.profile.min_notional_usd:
                accepted = False
                reason = "min_notional"
            elif open_risk + plan.expected_loss_usd > total_equity * self.profile.max_total_open_risk_pct:
                accepted = False
                reason = "max_total_open_risk"
            elif self._rolling_loss_blocked(plan):
                accepted = False
                reason = "rolling_symbol_loss_guardrail"
            if accepted:
                open_risk += max(float(plan.expected_loss_usd), 0.0)
            decisions.append(RiskDecision(accepted=accepted, reason=reason, trade_plan=plan))
        return decisions

    def record_closed_trade(
        self,
        *,
        symbol: str,
        pnl_usd: float | None,
    ) -> None:
        values = self._recent_pnl_by_symbol.setdefault(symbol.upper(), [])
        values.append(float(pnl_usd or 0.0))
        del values[:-4]

    def _rolling_loss_blocked(self, plan: TradePlan) -> bool:
        values = self._recent_pnl_by_symbol.get(plan.symbol.upper(), [])
        if len(values) < 3:
            return False
        return sum(values[-3:]) <= -6.0


class HyperpReversionService:
    """Funding/extension mean-reversion engine for Hyperps."""

    def __init__(self, profile: HyperpReversionProfile) -> None:
        self.profile = profile

    def evaluate(self, context: HyperpReversionContext) -> SignalPreview | None:
        if self.profile.trigger_mode == "funding_veto_flow":
            return self._evaluate_flow(context, rank=1, metrics=self._flow_metrics(context))
        if self.profile.trigger_mode == "flow_exhaustion_fade":
            return self._evaluate_flow_exhaustion(context, rank=1, metrics=self._flow_metrics(context))
        return self._evaluate_funding_reversion(context)

    def evaluate_many(self, contexts: list[HyperpReversionContext]) -> list[SignalPreview]:
        if self.profile.trigger_mode in {"funding_veto_flow", "flow_exhaustion_fade"}:
            ranked = sorted(
                (
                    (context, self._flow_metrics(context))
                    for context in contexts
                    if context.snapshot.price > 0.0
                ),
                key=lambda item: (
                    float(item[1]["interest_score"]),
                    item[0].snapshot.bucket_notional_usd,
                    item[0].snapshot.symbol,
                ),
                reverse=True,
            )
            signals: list[SignalPreview] = []
            for rank, (context, metrics) in enumerate(ranked, start=1):
                if self.profile.trigger_mode == "flow_exhaustion_fade":
                    signal = self._evaluate_flow_exhaustion(context, rank=rank, metrics=metrics)
                else:
                    signal = self._evaluate_flow(context, rank=rank, metrics=metrics)
                if signal is not None:
                    signals.append(signal)
            return signals
        return [signal for context in contexts if (signal := self.evaluate(context)) is not None]

    def _evaluate_funding_reversion(self, context: HyperpReversionContext) -> SignalPreview | None:
        snapshot = context.snapshot
        if not self._lifecycle_ok(context):
            return None
        if self.profile.allowed_regimes and context.regime not in self.profile.allowed_regimes:
            return None
        strictness = max(float(context.lifecycle_strictness), 1.0)
        if not self._liquidity_ok(snapshot, strictness=strictness):
            return None
        if self._event_spike_blocked(snapshot, context.thresholds, strictness=strictness):
            return None

        deviation_bps = self._ema_deviation_bps(snapshot)
        funding = float(snapshot.funding_rate)
        abs_deviation_threshold = max(
            float(context.thresholds.abs_deviation_extreme_bps) * strictness,
            self.profile.min_deviation_bps * strictness,
        )
        long_signal = (
            self.profile.allow_longs
            and funding <= min(
                context.thresholds.negative_funding_extreme * strictness,
                -self.profile.min_abs_funding_rate * strictness,
            )
            and deviation_bps <= -abs_deviation_threshold
        )
        short_signal = (
            self.profile.allow_shorts
            and funding >= max(
                context.thresholds.positive_funding_extreme * strictness,
                self.profile.min_abs_funding_rate * strictness,
            )
            and deviation_bps >= abs_deviation_threshold
        )
        if not long_signal and not short_signal:
            return None

        side = "long" if long_signal else "short"
        rsi_ok = self._rsi_ok(side, context.rsi14)
        rejection_ok = self._rejection_ok(side, snapshot, context.price_move_bps)
        flow_ok = self._flow_ok(side, snapshot)
        if self.profile.require_rsi and not rsi_ok:
            return None
        if self.profile.require_rejection and not rejection_ok:
            return None
        if self.profile.require_flow_confirmation and not flow_ok:
            return None

        score = self._score(
            side=side,
            snapshot=snapshot,
            funding=funding,
            deviation_bps=deviation_bps,
            threshold=context.thresholds,
            rsi_ok=rsi_ok,
            rejection_ok=rejection_ok,
            flow_ok=flow_ok,
        )
        if score < self.profile.min_score:
            return None
        confidence = round(min(0.86, 0.46 + score * 0.065), 4)
        setup = f"hyperp_funding_reversion_{side}"
        details: dict[str, float | str | bool] = {
            "profile": self.profile.name,
            "regime": context.regime,
            "funding_rate": funding,
            "positive_funding_extreme": context.thresholds.positive_funding_extreme,
            "negative_funding_extreme": context.thresholds.negative_funding_extreme,
            "deviation_bps": deviation_bps,
            "abs_deviation_extreme_bps": abs_deviation_threshold,
            "rsi14": float(context.rsi14) if context.rsi14 is not None else 50.0,
            "price_move_bps": context.price_move_bps,
            "rejection_ok": rejection_ok,
            "flow_ok": flow_ok,
            "score": score,
            "spread_bps": float(snapshot.spread_bps),
            "bucket_notional_usd": float(snapshot.bucket_notional_usd),
            "bucket_trade_count": float(snapshot.bucket_trade_count),
            "bucket_range_bps": float(snapshot.bucket_range_bps),
            "realized_vol_short_bps": float(snapshot.realized_vol_short_bps),
            **self._lifecycle_details(context),
        }
        return SignalPreview(
            symbol=snapshot.symbol,
            side=side,
            setup=setup,
            confidence=confidence,
            reason_summary="funding_extreme_plus_extension",
            setup_details=details,
            confidence_components={
                "score": float(score),
                "funding_extreme": min(abs(funding) / max(abs(self.profile.min_abs_funding_rate), 1e-9), 2.0),
                "extension": min(abs(deviation_bps) / max(abs_deviation_threshold, 1e-9), 2.0),
            },
        )

    def _lifecycle_ok(self, context: HyperpReversionContext) -> bool:
        return (
            context.lifecycle_phase not in {UNKNOWN_PHASE, RETIRED_PHASE}
            and float(context.lifecycle_weight) > 0.0
        )

    def _lifecycle_details(self, context: HyperpReversionContext) -> dict[str, float | str | bool]:
        return {
            "lifecycle_phase": context.lifecycle_phase,
            "lifecycle_weight": float(context.lifecycle_weight),
            "lifecycle_strictness": float(context.lifecycle_strictness),
            "lifecycle_days_since_active": (
                float(context.lifecycle_days_since_active)
                if context.lifecycle_days_since_active is not None
                else -1.0
            ),
        }

    def _liquidity_ok(self, snapshot: SymbolMarketSnapshot, *, strictness: float = 1.0) -> bool:
        return (
            float(snapshot.spread_bps) <= self.profile.max_spread_bps / max(strictness, 1e-9)
            and float(snapshot.bucket_notional_usd) >= self.profile.min_bucket_notional_usd * strictness
            and int(snapshot.bucket_trade_count) >= math.ceil(self.profile.min_bucket_trade_count * strictness)
        )

    def _event_spike_blocked(
        self,
        snapshot: SymbolMarketSnapshot,
        thresholds: HyperpThresholds,
        *,
        strictness: float = 1.0,
    ) -> bool:
        if not self.profile.block_event_spikes:
            return False
        range_limit = max(
            thresholds.event_range_bps * self.profile.max_event_range_multiplier / strictness,
            0.0,
        )
        volume_limit = min(
            thresholds.event_volume_ratio,
            self.profile.max_event_volume_ratio,
        ) / strictness
        return (
            range_limit > 0.0
            and float(snapshot.bucket_range_bps) > range_limit
        ) or (
            volume_limit > 0.0
            and float(snapshot.volume_ratio) > volume_limit
        )

    def _rsi_ok(self, side: str, rsi14: float | None) -> bool:
        if rsi14 is None:
            return False
        if side == "long":
            return rsi14 <= self.profile.long_rsi_max
        return rsi14 >= self.profile.short_rsi_min

    def _rejection_ok(
        self,
        side: str,
        snapshot: SymbolMarketSnapshot,
        price_move_bps: float,
    ) -> bool:
        flow = float(snapshot.trade_flow_bias)
        book = float(snapshot.book_imbalance)
        if side == "long":
            return price_move_bps >= 0.0 or flow >= 0.15 or book >= 0.15
        return price_move_bps <= 0.0 or flow <= -0.15 or book <= -0.15

    def _flow_ok(self, side: str, snapshot: SymbolMarketSnapshot) -> bool:
        flow = float(snapshot.trade_flow_bias)
        book = float(snapshot.book_imbalance)
        if side == "long":
            return flow >= 0.0 and book >= -0.20
        return flow <= 0.0 and book <= 0.20

    def _score(
        self,
        *,
        side: str,
        snapshot: SymbolMarketSnapshot,
        funding: float,
        deviation_bps: float,
        threshold: HyperpThresholds,
        rsi_ok: bool,
        rejection_ok: bool,
        flow_ok: bool,
    ) -> int:
        score = 0
        funding_threshold = (
            abs(threshold.negative_funding_extreme)
            if side == "long"
            else abs(threshold.positive_funding_extreme)
        )
        funding_threshold = max(funding_threshold, self.profile.min_abs_funding_rate)
        deviation_threshold = max(threshold.abs_deviation_extreme_bps, self.profile.min_deviation_bps)
        if abs(funding) >= funding_threshold:
            score += 1
        if abs(deviation_bps) >= deviation_threshold:
            score += 1
        if abs(deviation_bps) >= deviation_threshold * 1.5:
            score += 1
        if rsi_ok:
            score += 1
        if rejection_ok:
            score += 1
        if flow_ok:
            score += 1
        if float(snapshot.spread_bps) <= self.profile.max_spread_bps * 0.6:
            score += 1
        return score

    def _ema_deviation_bps(self, snapshot: SymbolMarketSnapshot) -> float:
        ema = float(snapshot.ema_fast or 0.0)
        price = float(snapshot.price or 0.0)
        if ema <= 0.0 or price <= 0.0:
            return 0.0
        return ((price / ema) - 1.0) * 10_000.0

    def _evaluate_flow(
        self,
        context: HyperpReversionContext,
        *,
        rank: int,
        metrics: dict[str, float],
    ) -> SignalPreview | None:
        snapshot = context.snapshot
        if not self._lifecycle_ok(context):
            return None
        strictness = max(float(context.lifecycle_strictness), 1.0)
        if rank > self.profile.top_n:
            return None
        if self.profile.allowed_regimes and context.regime not in self.profile.allowed_regimes:
            return None
        if not self._liquidity_ok(snapshot, strictness=strictness):
            return None
        if self._event_spike_blocked(snapshot, context.thresholds, strictness=strictness):
            return None
        if float(metrics["interest_score"]) < min(self.profile.min_interest_score * strictness, 0.95):
            return None
        if float(snapshot.volume_ratio) < self.profile.min_volume_ratio * strictness:
            return None
        if float(snapshot.trade_count_ratio) < self.profile.min_trade_count_ratio * strictness:
            return None
        if self._momentum_vetoed(context):
            return None
        delta_flow_ok = float(snapshot.delta_trade_flow_bias) >= 0.05 or (
            float(snapshot.delta_trade_flow_bias) == 0.0
            and float(snapshot.trade_flow_bias) >= 0.45
        )
        delta_book_ok = float(snapshot.delta_book_imbalance) >= -0.05 or (
            float(snapshot.delta_book_imbalance) == 0.0
            and float(snapshot.book_imbalance) >= 0.05
        )
        if not (
            float(snapshot.trade_flow_bias) >= 0.12
            and delta_flow_ok
            and float(snapshot.book_imbalance) >= 0.05
            and delta_book_ok
        ):
            return None
        flow_quality = float(metrics["flow_quality"])
        event_score = float(metrics["event_score"])
        interest_score = float(metrics["interest_score"])
        if max(flow_quality, event_score) < 0.50:
            return None
        confidence = round(
            min(
                0.88,
                0.48
                + flow_quality * 0.16
                + event_score * 0.10
                + interest_score * 0.14
                + max(0.0, 1.0 - float(snapshot.spread_bps) / max(self.profile.max_spread_bps, 1.0)) * 0.06,
            ),
            4,
        )
        deviation_bps = self._ema_deviation_bps(snapshot)
        details: dict[str, float | str | bool] = {
            "profile": self.profile.name,
            "trigger_mode": self.profile.trigger_mode,
            "rank": float(rank),
            "regime": context.regime,
            "funding_rate": float(snapshot.funding_rate),
            "positive_funding_extreme": context.thresholds.positive_funding_extreme,
            "negative_funding_extreme": context.thresholds.negative_funding_extreme,
            "deviation_bps": deviation_bps,
            "abs_deviation_extreme_bps": context.thresholds.abs_deviation_extreme_bps,
            "interest_score": interest_score,
            "event_score": event_score,
            "flow_quality": flow_quality,
            "money_flow_quality": float(metrics["money_flow_quality"]),
            "price_move_bps": context.price_move_bps,
            "rsi14": float(context.rsi14) if context.rsi14 is not None else 50.0,
            "spread_bps": float(snapshot.spread_bps),
            "bucket_notional_usd": float(snapshot.bucket_notional_usd),
            "bucket_trade_count": float(snapshot.bucket_trade_count),
            "bucket_range_bps": float(snapshot.bucket_range_bps),
            "realized_vol_short_bps": float(snapshot.realized_vol_short_bps),
            "momentum_vetoed": False,
            **self._lifecycle_details(context),
        }
        return SignalPreview(
            symbol=snapshot.symbol,
            side="long",
            setup="hyperp_flow_following_long",
            confidence=confidence,
            reason_summary="flow_following_with_hyperp_momentum_veto",
            setup_details=details,
            confidence_components={
                "interest_score": interest_score,
                "event_score": event_score,
                "flow_quality": flow_quality,
            },
        )

    def _momentum_vetoed(self, context: HyperpReversionContext) -> bool:
        snapshot = context.snapshot
        deviation_bps = self._ema_deviation_bps(snapshot)
        funding_threshold = max(
            context.thresholds.positive_funding_extreme,
            self.profile.min_abs_funding_rate,
        )
        deviation_threshold = max(
            context.thresholds.abs_deviation_extreme_bps,
            self.profile.min_deviation_bps,
        )
        return (
            float(snapshot.funding_rate) >= funding_threshold
            and deviation_bps >= deviation_threshold
        )

    def _flow_metrics(self, context: HyperpReversionContext) -> dict[str, float]:
        snapshot = context.snapshot
        volume_accel = _clamp((float(snapshot.volume_ratio) - 1.0) / 2.5)
        trade_accel = _clamp((float(snapshot.trade_count_ratio) - 1.0) / 2.5)
        notional_presence = _clamp(_safe_log10(max(float(snapshot.bucket_notional_usd), 1.0)) / 4.0)
        volatility = _clamp(float(snapshot.realized_vol_short_bps) / 80.0)
        spread_quality = 1.0 - _clamp(float(snapshot.spread_bps) / max(self.profile.max_spread_bps, 1.0))
        directional_pressure = _clamp(
            max(float(snapshot.trade_flow_bias), 0.0) * 0.55
            + max(float(snapshot.book_imbalance), 0.0) * 0.35
            + max(float(snapshot.delta_trade_flow_bias), 0.0) * 0.10
        )
        event_score = _clamp(
            abs(float(snapshot.delta_trade_flow_bias)) * 0.30
            + abs(float(snapshot.delta_book_imbalance)) * 0.18
            + volume_accel * 0.22
            + trade_accel * 0.18
            + volatility * 0.12
        )
        money_flow_quality = _clamp(
            max(float(snapshot.trade_flow_bias), 0.0) * 0.45
            + max(float(snapshot.book_imbalance), 0.0) * 0.25
            + volume_accel * 0.18
            + trade_accel * 0.12
        )
        flow_quality = _clamp(
            directional_pressure * 0.45
            + money_flow_quality * 0.35
            + max(context.price_move_bps, 0.0) / 20.0 * 0.20
        )
        interest_score = _clamp(
            event_score * 0.25
            + volume_accel * 0.16
            + trade_accel * 0.12
            + notional_presence * 0.12
            + volatility * 0.10
            + spread_quality * 0.12
            + directional_pressure * 0.13
        )
        return {
            "event_score": round(event_score, 4),
            "flow_quality": round(flow_quality, 4),
            "money_flow_quality": round(money_flow_quality, 4),
            "interest_score": round(interest_score, 4),
        }

    def _evaluate_flow_exhaustion(
        self,
        context: HyperpReversionContext,
        *,
        rank: int,
        metrics: dict[str, float],
    ) -> SignalPreview | None:
        snapshot = context.snapshot
        if not self._lifecycle_ok(context):
            return None
        strictness = max(float(context.lifecycle_strictness), 1.0)
        if rank > self.profile.top_n:
            return None
        if self.profile.allowed_regimes and context.regime not in self.profile.allowed_regimes:
            return None
        if not self._liquidity_ok(snapshot, strictness=strictness):
            return None
        if self._event_spike_blocked(snapshot, context.thresholds, strictness=strictness):
            return None
        if float(metrics["interest_score"]) < min(self.profile.min_interest_score * strictness, 0.95):
            return None
        if float(snapshot.volume_ratio) < self.profile.min_volume_ratio * strictness:
            return None
        if float(snapshot.trade_count_ratio) < self.profile.min_trade_count_ratio * strictness:
            return None
        if self.profile.require_rsi:
            if context.rsi14 is None or context.rsi14 < self.profile.short_rsi_min:
                return None
        deviation_bps = self._ema_deviation_bps(snapshot)
        extension_threshold = max(
            self.profile.min_deviation_bps * strictness,
            context.thresholds.abs_deviation_extreme_bps * 0.35 * strictness,
        )
        if deviation_bps < extension_threshold and context.price_move_bps < 15.0:
            return None
        delta_flow_ok = float(snapshot.delta_trade_flow_bias) >= 0.05 or (
            float(snapshot.delta_trade_flow_bias) == 0.0
            and float(snapshot.trade_flow_bias) >= 0.45
        )
        if not (
            float(snapshot.trade_flow_bias) >= 0.45
            and delta_flow_ok
            and float(snapshot.book_imbalance) >= 0.05
        ):
            return None
        if max(float(metrics["flow_quality"]), float(metrics["event_score"])) < 0.50:
            return None
        funding = float(snapshot.funding_rate)
        funding_bonus = funding >= max(
            context.thresholds.positive_funding_extreme,
            self.profile.min_abs_funding_rate,
        )
        confidence = round(
            min(
                0.88,
                0.50
                + float(metrics["flow_quality"]) * 0.13
                + float(metrics["event_score"]) * 0.09
                + min(max(deviation_bps / max(extension_threshold, 1.0), 0.0), 2.0) * 0.05
                + (0.04 if funding_bonus else 0.0),
            ),
            4,
        )
        details: dict[str, float | str | bool] = {
            "profile": self.profile.name,
            "trigger_mode": self.profile.trigger_mode,
            "rank": float(rank),
            "regime": context.regime,
            "funding_rate": funding,
            "funding_bonus": funding_bonus,
            "positive_funding_extreme": context.thresholds.positive_funding_extreme,
            "negative_funding_extreme": context.thresholds.negative_funding_extreme,
            "deviation_bps": deviation_bps,
            "abs_deviation_extreme_bps": context.thresholds.abs_deviation_extreme_bps,
            "interest_score": float(metrics["interest_score"]),
            "event_score": float(metrics["event_score"]),
            "flow_quality": float(metrics["flow_quality"]),
            "money_flow_quality": float(metrics["money_flow_quality"]),
            "price_move_bps": context.price_move_bps,
            "rsi14": float(context.rsi14) if context.rsi14 is not None else 50.0,
            "spread_bps": float(snapshot.spread_bps),
            "bucket_notional_usd": float(snapshot.bucket_notional_usd),
            "bucket_trade_count": float(snapshot.bucket_trade_count),
            "bucket_range_bps": float(snapshot.bucket_range_bps),
            "realized_vol_short_bps": float(snapshot.realized_vol_short_bps),
            **self._lifecycle_details(context),
        }
        return SignalPreview(
            symbol=snapshot.symbol,
            side="short",
            setup="hyperp_flow_exhaustion_short",
            confidence=confidence,
            reason_summary="fade_positive_flow_extension",
            setup_details=details,
            confidence_components={
                "interest_score": float(metrics["interest_score"]),
                "event_score": float(metrics["event_score"]),
                "flow_quality": float(metrics["flow_quality"]),
                "funding_bonus": 1.0 if funding_bonus else 0.0,
            },
        )


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _safe_log10(value: float) -> float:
    import math

    if value <= 0.0:
        return 0.0
    return math.log10(value)


class HyperpReversionPlanner:
    def __init__(self, config: AppConfig, profile: HyperpReversionProfile) -> None:
        self.config = config
        self.profile = profile

    def build_trade_plan(
        self,
        signal: SignalPreview,
        pod_allocation: PodAllocation,
    ) -> TradePlan | None:
        symbol_allocation = next(
            (item for item in pod_allocation.symbols if item.symbol == signal.symbol),
            None,
        )
        if symbol_allocation is None or symbol_allocation.target_usd <= 0:
            return None
        details = dict(signal.setup_details)
        stop_bps = self._stop_bps(details)
        lifecycle_weight = _clamp(float(details.get("lifecycle_weight", 1.0) or 1.0))
        sized = self._size(symbol_allocation.target_usd, stop_bps, size_weight=lifecycle_weight)
        if sized is None:
            return None
        take_profit_bps = self._take_profit_bps(details, stop_bps)
        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=signal.confidence,
            target_notional_usd=sized["target_notional_usd"],
            stop_bps=stop_bps,
            time_stop_hours=self.profile.time_stop_hours,
            take_profit_bps=take_profit_bps,
            break_even_trigger_bps=round(stop_bps * self.profile.break_even_stop_ratio, 4),
            trailing_activation_bps=round(stop_bps * self.profile.trailing_activation_stop_ratio, 4),
            trailing_distance_bps=round(stop_bps * self.profile.trailing_distance_stop_ratio, 4),
            reentry_cooldown_minutes=self.profile.reentry_cooldown_minutes,
            confidence_components=dict(signal.confidence_components),
            margin_usd=sized["margin_usd"],
            requested_leverage=sized["requested_leverage"],
            effective_leverage=sized["effective_leverage"],
            risk_budget_usd=sized["risk_budget_usd"],
            expected_loss_usd=sized["expected_loss_usd"],
            isolated=True,
            setup_details=details,
        )

    def _stop_bps(self, details: dict[str, object]) -> float:
        realized = float(details.get("realized_vol_short_bps", 0.0) or 0.0)
        bucket_range = float(details.get("bucket_range_bps", 0.0) or 0.0)
        raw = max(realized, bucket_range * 0.55) * self.profile.stop_vol_multiplier
        return round(
            min(max(raw, self.profile.stop_floor_bps), self.profile.stop_ceiling_bps),
            4,
        )

    def _take_profit_bps(self, details: dict[str, object], stop_bps: float) -> float:
        extension = abs(float(details.get("deviation_bps", 0.0) or 0.0))
        raw = max(
            extension * self.profile.take_profit_extension_fraction,
            stop_bps * self.profile.take_profit_stop_ratio,
        )
        return round(min(raw, self.profile.take_profit_ceiling_bps), 4)

    def _size(
        self,
        margin_cap_usd: float,
        stop_bps: float,
        *,
        size_weight: float = 1.0,
    ) -> dict[str, float] | None:
        if margin_cap_usd <= 0.0 or stop_bps <= 0.0:
            return None
        size_weight = _clamp(size_weight)
        total_equity = max(float(self.config.trident.capital.reference_equity_usd), 1e-9)
        risk_pct = min(
            max(self.profile.risk_per_trade_pct, 0.0),
            max(float(self.config.trident.risk.max_risk_per_trade_pct), 0.0),
        )
        risk_budget_usd = round(total_equity * risk_pct * self.profile.size_multiplier * size_weight, 6)
        if risk_budget_usd <= 0.0:
            return None
        desired_notional = risk_budget_usd / (stop_bps / 10_000.0)
        requested_leverage = max(1.0, min(self.profile.default_leverage, self.profile.max_leverage))
        effective_leverage = max(
            requested_leverage,
            min(self.profile.max_leverage, desired_notional / max(margin_cap_usd, 1e-9)),
        )
        target_notional_usd = min(desired_notional, margin_cap_usd * effective_leverage)
        if target_notional_usd <= 0.0:
            return None
        margin_usd = target_notional_usd / max(effective_leverage, 1e-9)
        expected_loss_usd = target_notional_usd * (stop_bps / 10_000.0)
        return {
            "margin_usd": round(margin_usd, 6),
            "target_notional_usd": round(target_notional_usd, 6),
            "requested_leverage": round(requested_leverage, 4),
            "effective_leverage": round(effective_leverage, 4),
            "risk_budget_usd": round(risk_budget_usd, 6),
            "expected_loss_usd": round(expected_loss_usd, 6),
        }


def extract_active_hyperp_symbols(payload: object) -> list[str]:
    if not isinstance(payload, list) or not payload:
        return []
    meta = payload[0]
    if not isinstance(meta, dict):
        return []
    universe = meta.get("universe", [])
    if not isinstance(universe, list):
        return []
    symbols: list[str] = []
    for item in universe:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("name", "")).strip().upper()
        if not symbol or bool(item.get("isDelisted", False)):
            continue
        if str(item.get("marginMode", "")) != "strictIsolated":
            continue
        symbols.append(symbol)
    return sorted(dict.fromkeys(symbols))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return datetime.now(timezone.utc)
    if len(text) == 10:
        text = f"{text}T00:00:00Z"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
