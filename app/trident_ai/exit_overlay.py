from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.exit_audit import (
    _closed_trade_rows,
    _duration_minutes,
    _fold_labels,
    _format_timestamp,
    _gross_move_bps,
    _market_points_between,
    _market_price_index,
    _normalize_windows,
    _number,
    _parse_timestamp,
    _pnl_bps,
    _timestamp_id,
)


DEFAULT_OVERLAY_EARLY_ADVERSE_BPS_VALUES: tuple[float, ...] = (0.0, 25.0, 35.0, 50.0)
DEFAULT_OVERLAY_EARLY_WINDOW_MINUTES_VALUES: tuple[int, ...] = (15, 30, 60)
DEFAULT_OVERLAY_MFE_ACTIVATION_BPS_VALUES: tuple[float, ...] = (0.0, 25.0, 40.0, 60.0)
DEFAULT_OVERLAY_MFE_GIVEBACK_BPS_VALUES: tuple[float, ...] = (0.0, 20.0, 30.0, 45.0)
DEFAULT_OVERLAY_FOLLOW_THROUGH_WINDOW_MINUTES_VALUES: tuple[int, ...] = (0,)
DEFAULT_OVERLAY_MIN_FOLLOW_THROUGH_BPS_VALUES: tuple[float, ...] = (0.0,)
DEFAULT_OVERLAY_MAX_FOLLOW_THROUGH_GROSS_BPS_VALUES: tuple[float, ...] = (0.0,)


@dataclass(frozen=True, slots=True)
class TridentAIExitOverlaySweepResult:
    paper_journal_paths: tuple[str, ...]
    market_input_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    baseline_profile: dict[str, object] = field(default_factory=dict)
    best_profile: dict[str, object] = field(default_factory=dict)
    best_robust_profile: dict[str, object] = field(default_factory=dict)
    profiles: list[dict[str, object]] = field(default_factory=list)
    robust_profiles: list[dict[str, object]] = field(default_factory=list)
    best_profile_trades: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "paper_journal_paths": list(self.paper_journal_paths),
            "market_input_paths": list(self.market_input_paths),
            "fold_labels": list(self.fold_labels),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "baseline_profile": self.baseline_profile,
            "best_profile": self.best_profile,
            "best_robust_profile": self.best_robust_profile,
            "profiles": self.profiles,
            "robust_profiles": self.robust_profiles,
            "best_profile_trades": self.best_profile_trades,
        }


@dataclass(frozen=True, slots=True)
class _PathPoint:
    timestamp: datetime
    timestamp_text: str
    minutes_from_open: float
    price: float
    gross_bps: float


@dataclass(frozen=True, slots=True)
class _TradePath:
    fold_label: str
    trade: dict[str, object]
    path: tuple[_PathPoint, ...]
    path_available: bool


@dataclass(frozen=True, slots=True)
class _OverlayProfile:
    profile_id: str
    early_adverse_bps: float
    early_window_minutes: int
    mfe_activation_bps: float
    mfe_giveback_bps: float
    follow_through_window_minutes: int
    min_follow_through_bps: float
    max_follow_through_gross_bps: float

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "early_adverse_bps": round(self.early_adverse_bps, 6),
            "early_window_minutes": self.early_window_minutes,
            "mfe_activation_bps": round(self.mfe_activation_bps, 6),
            "mfe_giveback_bps": round(self.mfe_giveback_bps, 6),
            "follow_through_window_minutes": self.follow_through_window_minutes,
            "min_follow_through_bps": round(self.min_follow_through_bps, 6),
            "max_follow_through_gross_bps": round(self.max_follow_through_gross_bps, 6),
        }


def run_trident_ai_exit_overlay_sweep(
    *,
    paper_journal_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    early_adverse_bps_values: tuple[float, ...] = DEFAULT_OVERLAY_EARLY_ADVERSE_BPS_VALUES,
    early_window_minutes_values: tuple[int, ...] = DEFAULT_OVERLAY_EARLY_WINDOW_MINUTES_VALUES,
    mfe_activation_bps_values: tuple[float, ...] = DEFAULT_OVERLAY_MFE_ACTIVATION_BPS_VALUES,
    mfe_giveback_bps_values: tuple[float, ...] = DEFAULT_OVERLAY_MFE_GIVEBACK_BPS_VALUES,
    follow_through_window_minutes_values: tuple[int, ...] = DEFAULT_OVERLAY_FOLLOW_THROUGH_WINDOW_MINUTES_VALUES,
    min_follow_through_bps_values: tuple[float, ...] = DEFAULT_OVERLAY_MIN_FOLLOW_THROUGH_BPS_VALUES,
    max_follow_through_gross_bps_values: tuple[float, ...] = DEFAULT_OVERLAY_MAX_FOLLOW_THROUGH_GROSS_BPS_VALUES,
) -> TridentAIExitOverlaySweepResult:
    if not paper_journal_paths:
        raise ValueError("paper_journal_paths_required")
    if len(paper_journal_paths) != len(market_input_paths):
        raise ValueError("paper_and_market_input_counts_must_match")

    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_exit_overlay_sweep_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_exit_overlay_sweep_{run_id}.md")
    labels = _fold_labels(fold_labels, len(paper_journal_paths))

    trade_paths = _trade_paths(
        paper_journal_paths=paper_journal_paths,
        market_input_paths=market_input_paths,
        fold_labels=labels,
    )
    profiles = _overlay_profiles(
        early_adverse_bps_values=early_adverse_bps_values,
        early_window_minutes_values=early_window_minutes_values,
        mfe_activation_bps_values=mfe_activation_bps_values,
        mfe_giveback_bps_values=mfe_giveback_bps_values,
        follow_through_window_minutes_values=follow_through_window_minutes_values,
        min_follow_through_bps_values=min_follow_through_bps_values,
        max_follow_through_gross_bps_values=max_follow_through_gross_bps_values,
    )
    baseline = next(profile for profile in profiles if profile.profile_id == "baseline_original")
    baseline_summary, _baseline_trades = _simulate_profile(
        baseline,
        trade_paths=trade_paths,
        baseline_summary=None,
    )
    baseline_summary = _annotate_profile_against_baseline(baseline_summary, baseline_summary)
    profile_summaries: list[dict[str, object]] = []
    for profile in profiles:
        summary, _simulated_trades = _simulate_profile(
            profile,
            trade_paths=trade_paths,
            baseline_summary=baseline_summary,
        )
        profile_summaries.append(summary)
    profile_summaries.sort(key=_profile_sort_key)
    best_profile = profile_summaries[0] if profile_summaries else {}
    robust_profiles = sorted(
        (profile for profile in profile_summaries if _is_robust_profile(profile)),
        key=_robust_profile_sort_key,
    )
    best_robust_profile = robust_profiles[0] if robust_profiles else {}
    best_profile_trades: list[dict[str, object]] = []
    if profile_summaries:
        best_profile_id = str(best_profile.get("profile_id", "") or "")
        best_profile_obj = next(profile for profile in profiles if profile.profile_id == best_profile_id)
        _best_summary, best_profile_trades = _simulate_profile(
            best_profile_obj,
            trade_paths=trade_paths,
            baseline_summary=baseline_summary,
        )

    result = TridentAIExitOverlaySweepResult(
        paper_journal_paths=tuple(str(path) for path in paper_journal_paths),
        market_input_paths=tuple(str(path) for path in market_input_paths),
        fold_labels=labels,
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        baseline_profile=baseline_summary,
        best_profile=best_profile,
        best_robust_profile=best_robust_profile,
        profiles=profile_summaries,
        robust_profiles=robust_profiles,
        best_profile_trades=sorted(
            best_profile_trades,
            key=lambda item: (_number(item.get("net_bps")), item.get("opened_at", "")),
        ),
    )
    payload = build_exit_overlay_sweep_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_exit_overlay_sweep_report_payload(
    *,
    result: TridentAIExitOverlaySweepResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_exit_overlay_sweep",
        "result": result.to_dict(),
    }


def _trade_paths(
    *,
    paper_journal_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: tuple[str, ...],
) -> list[_TradePath]:
    paths: list[_TradePath] = []
    for label, paper_path, market_path in zip(fold_labels, paper_journal_paths, market_input_paths, strict=True):
        market_index = _market_price_index(market_path)
        for trade in _closed_trade_rows(paper_path):
            paths.append(_build_trade_path(dict(trade), market_index=market_index, fold_label=label))
    return paths


def _build_trade_path(
    trade: dict[str, object],
    *,
    market_index: dict[str, list[tuple[datetime, str, float]]],
    fold_label: str,
) -> _TradePath:
    symbol = str(trade.get("symbol", "") or "").strip().upper()
    side = str(trade.get("side", "") or "").strip().lower()
    opened_at = _parse_timestamp(str(trade.get("opened_at", "") or ""))
    closed_at = _parse_timestamp(str(trade.get("closed_at", "") or ""))
    entry_price = _number(trade.get("entry_price"))
    exit_price = _number(trade.get("exit_price"))
    if opened_at is None or closed_at is None or entry_price <= 0.0 or exit_price <= 0.0:
        return _TradePath(fold_label=fold_label, trade=trade, path=(), path_available=False)
    market_points = _market_points_between(
        market_index.get(symbol, []),
        opened_at=opened_at,
        closed_at=closed_at,
    )
    raw_path = [
        (opened_at, _format_timestamp(opened_at), entry_price),
        *market_points,
        (closed_at, _format_timestamp(closed_at), exit_price),
    ]
    path = tuple(
        _PathPoint(
            timestamp=timestamp,
            timestamp_text=timestamp_text,
            minutes_from_open=_duration_minutes(opened_at, timestamp),
            price=price,
            gross_bps=_gross_move_bps(side=side, entry_price=entry_price, future_price=price),
        )
        for timestamp, timestamp_text, price in raw_path
    )
    return _TradePath(fold_label=fold_label, trade=trade, path=path, path_available=bool(market_points))


def _overlay_profiles(
    *,
    early_adverse_bps_values: tuple[float, ...],
    early_window_minutes_values: tuple[int, ...],
    mfe_activation_bps_values: tuple[float, ...],
    mfe_giveback_bps_values: tuple[float, ...],
    follow_through_window_minutes_values: tuple[int, ...],
    min_follow_through_bps_values: tuple[float, ...],
    max_follow_through_gross_bps_values: tuple[float, ...],
) -> list[_OverlayProfile]:
    early_thresholds = _normalize_non_negative_floats(early_adverse_bps_values)
    early_windows = _normalize_windows(early_window_minutes_values)
    mfe_activations = _normalize_non_negative_floats(mfe_activation_bps_values)
    mfe_givebacks = _normalize_non_negative_floats(mfe_giveback_bps_values)
    follow_windows = _normalize_non_negative_ints(follow_through_window_minutes_values)
    follow_thresholds = _normalize_non_negative_floats(min_follow_through_bps_values)
    follow_current_max_values = _normalize_float_values(max_follow_through_gross_bps_values)
    profiles: list[_OverlayProfile] = []
    seen: set[tuple[float, int, float, float, int, float, float]] = set()
    for early_threshold in early_thresholds:
        early_window_candidates = (0,) if early_threshold <= 0.0 else early_windows
        for early_window in early_window_candidates:
            for mfe_activation in mfe_activations:
                giveback_candidates = (0.0,) if mfe_activation <= 0.0 else tuple(
                    value for value in mfe_givebacks if value > 0.0
                )
                for mfe_giveback in giveback_candidates:
                    for follow_threshold in follow_thresholds:
                        follow_window_candidates = (
                            (0,)
                            if follow_threshold <= 0.0
                            else tuple(value for value in follow_windows if value > 0)
                        )
                        follow_current_candidates = (
                            (0.0,) if follow_threshold <= 0.0 else follow_current_max_values
                        )
                        for follow_window in follow_window_candidates:
                            for follow_current_max in follow_current_candidates:
                                key = (
                                    round(early_threshold, 6),
                                    int(early_window),
                                    round(mfe_activation, 6),
                                    round(mfe_giveback, 6),
                                    int(follow_window),
                                    round(follow_threshold, 6),
                                    round(follow_current_max, 6),
                                )
                                if key in seen:
                                    continue
                                seen.add(key)
                                profiles.append(
                                    _OverlayProfile(
                                        profile_id=_profile_id(
                                            early_adverse_bps=early_threshold,
                                            early_window_minutes=early_window,
                                            mfe_activation_bps=mfe_activation,
                                            mfe_giveback_bps=mfe_giveback,
                                            follow_through_window_minutes=follow_window,
                                            min_follow_through_bps=follow_threshold,
                                            max_follow_through_gross_bps=follow_current_max,
                                        ),
                                        early_adverse_bps=early_threshold,
                                        early_window_minutes=int(early_window),
                                        mfe_activation_bps=mfe_activation,
                                        mfe_giveback_bps=mfe_giveback,
                                        follow_through_window_minutes=int(follow_window),
                                        min_follow_through_bps=follow_threshold,
                                        max_follow_through_gross_bps=follow_current_max,
                                    )
                                )
    return profiles


def _profile_id(
    *,
    early_adverse_bps: float,
    early_window_minutes: int,
    mfe_activation_bps: float,
    mfe_giveback_bps: float,
    follow_through_window_minutes: int,
    min_follow_through_bps: float,
    max_follow_through_gross_bps: float,
) -> str:
    if early_adverse_bps <= 0.0 and mfe_activation_bps <= 0.0 and min_follow_through_bps <= 0.0:
        return "baseline_original"
    parts: list[str] = []
    if early_adverse_bps > 0.0:
        parts.append(f"ea{early_adverse_bps:g}@{early_window_minutes}m")
    if mfe_activation_bps > 0.0:
        parts.append(f"mfe{mfe_activation_bps:g}_gb{mfe_giveback_bps:g}")
    if min_follow_through_bps > 0.0:
        parts.append(
            f"nft{min_follow_through_bps:g}@{follow_through_window_minutes}m"
            f"_max{max_follow_through_gross_bps:g}"
        )
    return "+".join(parts)


def _simulate_profile(
    profile: _OverlayProfile,
    *,
    trade_paths: list[_TradePath],
    baseline_summary: dict[str, object] | None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    simulated = [_simulate_trade(profile, trade_path) for trade_path in trade_paths]
    summary = _summary(profile, simulated)
    if baseline_summary is not None:
        summary = _annotate_profile_against_baseline(summary, baseline_summary)
    else:
        summary["delta_pnl_usd"] = 0.0
        summary["delta_avg_net_bps"] = 0.0
    return summary, simulated


def _simulate_trade(profile: _OverlayProfile, trade_path: _TradePath) -> dict[str, object]:
    trade = trade_path.trade
    notional = _number(trade.get("notional_usd"))
    fees = _number(trade.get("fees_usd"))
    side = str(trade.get("side", "") or "").strip().lower()
    entry_price = _number(trade.get("entry_price"))
    if not trade_path.path or notional <= 0.0:
        return _original_trade_result(profile, trade_path, reason="missing_path")

    best_gross = 0.0
    for point in trade_path.path[1:]:
        best_gross = max(best_gross, point.gross_bps)
        if _early_adverse_trigger(profile, point):
            return _simulated_trade_result(
                profile,
                trade_path,
                point=point,
                exit_reason="early_adverse_exit",
                notional=notional,
                fees=fees,
                side=side,
                entry_price=entry_price,
                best_gross=best_gross,
            )
        if _mfe_giveback_trigger(profile, point, best_gross=best_gross):
            return _simulated_trade_result(
                profile,
                trade_path,
                point=point,
                exit_reason="mfe_giveback_exit",
                notional=notional,
                fees=fees,
                side=side,
                entry_price=entry_price,
                best_gross=best_gross,
            )
        if _no_follow_through_trigger(profile, point, best_gross=best_gross):
            return _simulated_trade_result(
                profile,
                trade_path,
                point=point,
                exit_reason="no_follow_through_exit",
                notional=notional,
                fees=fees,
                side=side,
                entry_price=entry_price,
                best_gross=best_gross,
            )
    return _original_trade_result(profile, trade_path, reason="original_" + str(trade.get("close_reason", "") or "close"))


def _early_adverse_trigger(profile: _OverlayProfile, point: _PathPoint) -> bool:
    return (
        profile.early_adverse_bps > 0.0
        and profile.early_window_minutes > 0
        and point.minutes_from_open <= profile.early_window_minutes
        and point.gross_bps <= -abs(profile.early_adverse_bps)
    )


def _mfe_giveback_trigger(
    profile: _OverlayProfile,
    point: _PathPoint,
    *,
    best_gross: float,
) -> bool:
    return (
        profile.mfe_activation_bps > 0.0
        and profile.mfe_giveback_bps > 0.0
        and best_gross >= profile.mfe_activation_bps
        and best_gross - point.gross_bps >= profile.mfe_giveback_bps
    )


def _no_follow_through_trigger(
    profile: _OverlayProfile,
    point: _PathPoint,
    *,
    best_gross: float,
) -> bool:
    return (
        profile.follow_through_window_minutes > 0
        and profile.min_follow_through_bps > 0.0
        and point.minutes_from_open >= profile.follow_through_window_minutes
        and best_gross < profile.min_follow_through_bps
        and point.gross_bps <= profile.max_follow_through_gross_bps
    )


def _simulated_trade_result(
    profile: _OverlayProfile,
    trade_path: _TradePath,
    *,
    point: _PathPoint,
    exit_reason: str,
    notional: float,
    fees: float,
    side: str,
    entry_price: float,
    best_gross: float,
) -> dict[str, object]:
    gross_bps = _gross_move_bps(side=side, entry_price=entry_price, future_price=point.price)
    gross_pnl = round(notional * gross_bps / 10_000.0, 6)
    pnl = round(gross_pnl - fees, 6)
    return _trade_result_payload(
        profile,
        trade_path,
        exit_timestamp=point.timestamp_text,
        exit_minutes=point.minutes_from_open,
        exit_price=point.price,
        exit_reason=exit_reason,
        gross_pnl_usd=gross_pnl,
        fees_usd=fees,
        pnl_usd=pnl,
        gross_bps=gross_bps,
        net_bps=_pnl_bps(pnl, notional),
        best_gross_bps=best_gross,
    )


def _original_trade_result(
    profile: _OverlayProfile,
    trade_path: _TradePath,
    *,
    reason: str,
) -> dict[str, object]:
    trade = trade_path.trade
    notional = _number(trade.get("notional_usd"))
    gross_pnl = _number(trade.get("gross_pnl_usd"))
    pnl = _number(trade.get("pnl_usd"))
    exit_price = _number(trade.get("exit_price"))
    exit_timestamp = str(trade.get("closed_at", "") or "")
    exit_minutes = 0.0
    best_gross = 0.0
    if trade_path.path:
        exit_minutes = trade_path.path[-1].minutes_from_open
        best_gross = max(point.gross_bps for point in trade_path.path)
    return _trade_result_payload(
        profile,
        trade_path,
        exit_timestamp=exit_timestamp,
        exit_minutes=exit_minutes,
        exit_price=exit_price,
        exit_reason=reason,
        gross_pnl_usd=gross_pnl,
        fees_usd=_number(trade.get("fees_usd")),
        pnl_usd=pnl,
        gross_bps=_pnl_bps(gross_pnl, notional),
        net_bps=_pnl_bps(pnl, notional),
        best_gross_bps=best_gross,
    )


def _trade_result_payload(
    profile: _OverlayProfile,
    trade_path: _TradePath,
    *,
    exit_timestamp: str,
    exit_minutes: float,
    exit_price: float,
    exit_reason: str,
    gross_pnl_usd: float,
    fees_usd: float,
    pnl_usd: float,
    gross_bps: float,
    net_bps: float,
    best_gross_bps: float,
) -> dict[str, object]:
    trade = trade_path.trade
    original_pnl = _number(trade.get("pnl_usd"))
    original_notional = _number(trade.get("notional_usd"))
    original_net_bps = _pnl_bps(original_pnl, original_notional)
    return {
        **profile.to_dict(),
        "fold_label": trade_path.fold_label,
        "decision_id": str(trade.get("decision_id", "") or ""),
        "symbol": str(trade.get("symbol", "") or ""),
        "side": str(trade.get("side", "") or ""),
        "opened_at": str(trade.get("opened_at", "") or ""),
        "original_closed_at": str(trade.get("closed_at", "") or ""),
        "exit_timestamp": exit_timestamp,
        "exit_minutes": round(exit_minutes, 6),
        "entry_price": round(_number(trade.get("entry_price")), 8),
        "exit_price": round(exit_price, 8),
        "exit_reason": exit_reason,
        "notional_usd": round(original_notional, 6),
        "gross_pnl_usd": round(gross_pnl_usd, 6),
        "fees_usd": round(fees_usd, 6),
        "pnl_usd": round(pnl_usd, 6),
        "gross_bps": round(gross_bps, 6),
        "net_bps": round(net_bps, 6),
        "best_gross_bps": round(best_gross_bps, 6),
        "original_pnl_usd": round(original_pnl, 6),
        "original_net_bps": round(original_net_bps, 6),
        "delta_pnl_usd": round(pnl_usd - original_pnl, 6),
        "delta_net_bps": round(net_bps - original_net_bps, 6),
        "path_available": trade_path.path_available,
    }


def _summary(profile: _OverlayProfile, trades: list[dict[str, object]]) -> dict[str, object]:
    folds: dict[str, list[dict[str, object]]] = defaultdict(list)
    symbol_counts: Counter[str] = Counter()
    symbol_pnl: defaultdict[str, float] = defaultdict(float)
    symbol_abs_pnl: defaultdict[str, float] = defaultdict(float)
    for trade in trades:
        folds[str(trade.get("fold_label", "") or "unknown")].append(trade)
        symbol = str(trade.get("symbol", "") or "unknown").upper()
        pnl = _number(trade.get("pnl_usd"))
        symbol_counts[symbol] += 1
        symbol_pnl[symbol] += pnl
        symbol_abs_pnl[symbol] += abs(pnl)
    dominant_symbol, dominant_count = ("", 0)
    if symbol_counts:
        dominant_symbol, dominant_count = symbol_counts.most_common(1)[0]
    dominant_abs_pnl_symbol, dominant_abs_pnl = ("", 0.0)
    if symbol_abs_pnl:
        dominant_abs_pnl_symbol, dominant_abs_pnl = max(symbol_abs_pnl.items(), key=lambda item: item[1])
    total_abs_pnl = sum(symbol_abs_pnl.values())
    payload = {
        **profile.to_dict(),
        "trades_seen": len(trades),
        "overlay_exits": sum(1 for trade in trades if not str(trade.get("exit_reason", "")).startswith("original_")),
        "original_exits": sum(1 for trade in trades if str(trade.get("exit_reason", "")).startswith("original_")),
        "realized_pnl_usd": round(sum(_number(trade.get("pnl_usd")) for trade in trades), 6),
        "gross_pnl_usd": round(sum(_number(trade.get("gross_pnl_usd")) for trade in trades), 6),
        "fees_usd": round(sum(_number(trade.get("fees_usd")) for trade in trades), 6),
        "avg_net_bps": round(_average([_number(trade.get("net_bps")) for trade in trades]), 6),
        "avg_gross_bps": round(_average([_number(trade.get("gross_bps")) for trade in trades]), 6),
        "avg_duration_minutes": round(_average([_number(trade.get("exit_minutes")) for trade in trades]), 6),
        "exit_reason_counts": dict(Counter(str(trade.get("exit_reason", "") or "unknown") for trade in trades)),
        "positive_trades": sum(1 for trade in trades if _number(trade.get("pnl_usd")) > 0.0),
        "folds": [_fold_summary(label, rows) for label, rows in sorted(folds.items())],
        "symbol_counts": dict(symbol_counts),
        "symbol_pnl_usd": {symbol: round(value, 6) for symbol, value in sorted(symbol_pnl.items())},
        "dominant_symbol": dominant_symbol,
        "dominant_symbol_trade_ratio": round(dominant_count / len(trades), 6) if trades else 0.0,
        "dominant_abs_pnl_symbol": dominant_abs_pnl_symbol,
        "dominant_abs_pnl_ratio": round(dominant_abs_pnl / total_abs_pnl, 6) if total_abs_pnl > 0.0 else 0.0,
    }
    payload["win_rate"] = round(payload["positive_trades"] / len(trades), 6) if trades else 0.0
    return payload


def _fold_summary(label: str, trades: list[dict[str, object]]) -> dict[str, object]:
    return {
        "fold_label": label,
        "trades_seen": len(trades),
        "overlay_exits": sum(1 for trade in trades if not str(trade.get("exit_reason", "")).startswith("original_")),
        "realized_pnl_usd": round(sum(_number(trade.get("pnl_usd")) for trade in trades), 6),
        "avg_net_bps": round(_average([_number(trade.get("net_bps")) for trade in trades]), 6),
        "positive_trades": sum(1 for trade in trades if _number(trade.get("pnl_usd")) > 0.0),
    }


def _annotate_profile_against_baseline(
    summary: dict[str, object],
    baseline_summary: Mapping[str, object],
) -> dict[str, object]:
    summary["delta_pnl_usd"] = round(
        _number(summary.get("realized_pnl_usd")) - _number(baseline_summary.get("realized_pnl_usd")),
        6,
    )
    summary["delta_avg_net_bps"] = round(
        _number(summary.get("avg_net_bps")) - _number(baseline_summary.get("avg_net_bps")),
        6,
    )

    baseline_folds = {
        str(fold.get("fold_label", "") or "unknown"): fold
        for fold in baseline_summary.get("folds", [])
        if isinstance(fold, Mapping)
    }
    folds = summary.get("folds", [])
    if not isinstance(folds, list):
        folds = []

    fold_count = 0
    positive_fold_count = 0
    negative_fold_count = 0
    improved_fold_count = 0
    worse_fold_count = 0
    worst_fold_pnl = 0.0
    worst_fold_avg = 0.0
    worst_fold_delta = 0.0
    single_trade_positive_fold_pnl = 0.0
    single_trade_positive_fold_delta = 0.0
    first_fold = True

    for fold in folds:
        if not isinstance(fold, dict):
            continue
        fold_count += 1
        label = str(fold.get("fold_label", "") or "unknown")
        baseline_fold = baseline_folds.get(label, {})
        baseline_pnl = _number(baseline_fold.get("realized_pnl_usd")) if isinstance(baseline_fold, Mapping) else 0.0
        baseline_avg = _number(baseline_fold.get("avg_net_bps")) if isinstance(baseline_fold, Mapping) else 0.0
        fold_pnl = _number(fold.get("realized_pnl_usd"))
        fold_avg = _number(fold.get("avg_net_bps"))
        delta_pnl = round(fold_pnl - baseline_pnl, 6)
        delta_avg = round(fold_avg - baseline_avg, 6)
        fold["baseline_realized_pnl_usd"] = round(baseline_pnl, 6)
        fold["baseline_avg_net_bps"] = round(baseline_avg, 6)
        fold["delta_pnl_usd"] = delta_pnl
        fold["delta_avg_net_bps"] = delta_avg
        if fold_pnl > 0.0:
            positive_fold_count += 1
        elif fold_pnl < 0.0:
            negative_fold_count += 1
        if delta_pnl > 0.0:
            improved_fold_count += 1
        elif delta_pnl < 0.0:
            worse_fold_count += 1
        if _number(fold.get("trades_seen")) <= 1.0 and fold_pnl > 0.0:
            single_trade_positive_fold_pnl += fold_pnl
            single_trade_positive_fold_delta += delta_pnl
        if first_fold or fold_pnl < worst_fold_pnl:
            worst_fold_pnl = fold_pnl
            worst_fold_avg = fold_avg
        if first_fold or delta_pnl < worst_fold_delta:
            worst_fold_delta = delta_pnl
        first_fold = False

    summary["fold_count"] = fold_count
    summary["positive_fold_count"] = positive_fold_count
    summary["negative_fold_count"] = negative_fold_count
    summary["improved_fold_count"] = improved_fold_count
    summary["worse_fold_count"] = worse_fold_count
    summary["worst_fold_pnl_usd"] = round(worst_fold_pnl, 6)
    summary["worst_fold_avg_net_bps"] = round(worst_fold_avg, 6)
    summary["worst_fold_delta_pnl_usd"] = round(worst_fold_delta, 6)
    summary["single_trade_positive_fold_pnl_usd"] = round(single_trade_positive_fold_pnl, 6)
    summary["single_trade_positive_fold_delta_pnl_usd"] = round(single_trade_positive_fold_delta, 6)
    summary["non_single_trade_pnl_usd"] = round(
        _number(summary.get("realized_pnl_usd")) - single_trade_positive_fold_pnl,
        6,
    )
    summary["non_single_trade_delta_pnl_usd"] = round(
        _number(summary.get("delta_pnl_usd")) - single_trade_positive_fold_delta,
        6,
    )
    return summary


def _profile_sort_key(profile: Mapping[str, object]) -> tuple[float, float, float, str]:
    return (
        -_number(profile.get("realized_pnl_usd")),
        -_number(profile.get("avg_net_bps")),
        _number(profile.get("overlay_exits")),
        str(profile.get("profile_id", "")),
    )


def _is_robust_profile(profile: Mapping[str, object]) -> bool:
    return (
        str(profile.get("profile_id", "") or "") != "baseline_original"
        and _number(profile.get("overlay_exits")) > 0.0
        and _number(profile.get("delta_pnl_usd")) > 0.0
        and _number(profile.get("fold_count")) > 0.0
        and _number(profile.get("improved_fold_count")) > 0.0
        and _number(profile.get("worse_fold_count")) == 0.0
    )


def _robust_profile_sort_key(profile: Mapping[str, object]) -> tuple[float, float, float, float, str]:
    return (
        -_number(profile.get("worst_fold_delta_pnl_usd")),
        -_number(profile.get("delta_pnl_usd")),
        -_number(profile.get("positive_fold_count")),
        _number(profile.get("dominant_symbol_trade_ratio")),
        str(profile.get("profile_id", "")),
    )


def _normalize_non_negative_floats(values: tuple[float, ...]) -> tuple[float, ...]:
    parsed = sorted({round(float(value), 6) for value in values if float(value) >= 0.0})
    if not parsed:
        raise ValueError("sweep_values_must_include_non_negative_value")
    if 0.0 not in parsed:
        parsed.insert(0, 0.0)
    return tuple(parsed)


def _normalize_non_negative_ints(values: tuple[int, ...]) -> tuple[int, ...]:
    parsed = sorted({int(value) for value in values if int(value) >= 0})
    if not parsed:
        raise ValueError("sweep_values_must_include_non_negative_value")
    if 0 not in parsed:
        parsed.insert(0, 0)
    return tuple(parsed)


def _normalize_float_values(values: tuple[float, ...]) -> tuple[float, ...]:
    parsed = sorted({round(float(value), 6) for value in values})
    if not parsed:
        raise ValueError("sweep_values_must_include_value")
    return tuple(parsed)


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _write_report_outputs(
    payload: dict[str, object],
    *,
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_markdown_report(payload), encoding="utf-8")


def _render_markdown_report(payload: dict[str, object]) -> str:
    result = payload["result"]
    assert isinstance(result, Mapping)
    baseline = result.get("baseline_profile", {})
    best = result.get("best_profile", {})
    best_robust = result.get("best_robust_profile", {})
    assert isinstance(baseline, Mapping)
    assert isinstance(best, Mapping)
    assert isinstance(best_robust, Mapping)
    lines = [
        "# TRIDENT-AI Exit Overlay Sweep",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Paper journals: `{result['paper_journal_paths']}`",
        f"- Market inputs: `{result['market_input_paths']}`",
        f"- Fold labels: `{result['fold_labels']}`",
        "",
        "## Baseline vs Best",
        "",
        "| Profile | PnL | Avg Net | Trades | Overlay Exits | Win Rate |",
        "|---|---:|---:|---:|---:|---:|",
        _profile_markdown_row("baseline", baseline),
        _profile_markdown_row("best", best),
    ]
    if best_robust:
        lines.append(_profile_markdown_row("best_robust", best_robust))
    else:
        lines.append("| `best_robust: none` | $0.000000 | 0.00 | 0 | 0 | 0.00% |")
    lines.extend(
        [
            "",
            "## Top Profiles",
            "",
            "| Rank | Profile | PnL | Delta PnL | Avg Net | Delta Avg | Overlay Exits | Exit Reasons |",
            "|---:|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    profiles = result.get("profiles", [])
    assert isinstance(profiles, list)
    for index, profile in enumerate(profiles[:20], start=1):
        assert isinstance(profile, Mapping)
        exit_reasons = profile.get("exit_reason_counts", {})
        reasons_text = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(exit_reasons.items())
        ) if isinstance(exit_reasons, Mapping) else ""
        lines.append(
            f"| {index} | `{profile.get('profile_id', '')}` | "
            f"${_number(profile.get('realized_pnl_usd')):.6f} | "
            f"${_number(profile.get('delta_pnl_usd')):.6f} | "
            f"{_number(profile.get('avg_net_bps')):.2f} | "
            f"{_number(profile.get('delta_avg_net_bps')):.2f} | "
            f"{int(_number(profile.get('overlay_exits')))} | {reasons_text} |"
        )
    if not profiles:
        lines.append("| 0 | none | $0.000000 | $0.000000 | 0.00 | 0.00 | 0 | none |")

    lines.extend(
        [
            "",
            "## Robust Profiles",
            "",
            "| Rank | Profile | Delta PnL | Improved Folds | Positive Folds | Worst Fold Delta | "
            "Worst Fold PnL | Dominant Symbol |",
            "|---:|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    robust_profiles = result.get("robust_profiles", [])
    assert isinstance(robust_profiles, list)
    for index, profile in enumerate(robust_profiles[:20], start=1):
        assert isinstance(profile, Mapping)
        dominant = str(profile.get("dominant_symbol", "") or "n/a")
        dominant_ratio = _number(profile.get("dominant_symbol_trade_ratio"))
        lines.append(
            f"| {index} | `{profile.get('profile_id', '')}` | "
            f"${_number(profile.get('delta_pnl_usd')):.6f} | "
            f"{int(_number(profile.get('improved_fold_count')))}/{int(_number(profile.get('fold_count')))} | "
            f"{int(_number(profile.get('positive_fold_count')))}/{int(_number(profile.get('fold_count')))} | "
            f"${_number(profile.get('worst_fold_delta_pnl_usd')):.6f} | "
            f"${_number(profile.get('worst_fold_pnl_usd')):.6f} | "
            f"{dominant} {dominant_ratio:.2%} |"
        )
    if not robust_profiles:
        lines.append("| 0 | none | $0.000000 | 0/0 | 0/0 | $0.000000 | $0.000000 | n/a |")

    _append_fold_table(lines, title="Best Profile Folds", profile=best)
    if best_robust and best_robust.get("profile_id") != best.get("profile_id"):
        _append_fold_table(lines, title="Best Robust Profile Folds", profile=best_robust)

    lines.extend(
        [
            "",
            "## Best Profile Trades",
            "",
            "| Fold | Symbol | Side | Opened | Exit Reason | Exit Min | Net | Delta Net | PnL | Delta PnL |",
            "|---|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    trades = result.get("best_profile_trades", [])
    assert isinstance(trades, list)
    for trade in trades[:40]:
        assert isinstance(trade, Mapping)
        lines.append(
            f"| {trade.get('fold_label', '')} | {trade.get('symbol', '')} | {trade.get('side', '')} | "
            f"{trade.get('opened_at', '')} | {trade.get('exit_reason', '')} | "
            f"{_number(trade.get('exit_minutes')):.0f} | {_number(trade.get('net_bps')):.2f} | "
            f"{_number(trade.get('delta_net_bps')):.2f} | ${_number(trade.get('pnl_usd')):.6f} | "
            f"${_number(trade.get('delta_pnl_usd')):.6f} |"
        )
    if not trades:
        lines.append("| none | n/a | n/a | n/a | n/a | 0 | 0.00 | 0.00 | $0.000000 | $0.000000 |")
    lines.append("")
    return "\n".join(lines)


def _append_fold_table(lines: list[str], *, title: str, profile: Mapping[str, object]) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Fold | Trades | Overlay Exits | PnL | Delta PnL | Avg Net | Delta Avg | Positive Trades |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    folds = profile.get("folds", []) if isinstance(profile, Mapping) else []
    if isinstance(folds, list) and folds:
        for fold in folds:
            assert isinstance(fold, Mapping)
            lines.append(
                f"| {fold.get('fold_label', '')} | {fold.get('trades_seen', 0)} | "
                f"{fold.get('overlay_exits', 0)} | ${_number(fold.get('realized_pnl_usd')):.6f} | "
                f"${_number(fold.get('delta_pnl_usd')):.6f} | "
                f"{_number(fold.get('avg_net_bps')):.2f} | "
                f"{_number(fold.get('delta_avg_net_bps')):.2f} | {fold.get('positive_trades', 0)} |"
            )
    else:
        lines.append("| none | 0 | 0 | $0.000000 | $0.000000 | 0.00 | 0.00 | 0 |")


def _profile_markdown_row(label: str, profile: Mapping[str, object]) -> str:
    return (
        f"| `{label}: {profile.get('profile_id', '')}` | "
        f"${_number(profile.get('realized_pnl_usd')):.6f} | "
        f"{_number(profile.get('avg_net_bps')):.2f} | "
        f"{int(_number(profile.get('trades_seen')))} | "
        f"{int(_number(profile.get('overlay_exits')))} | "
        f"{_number(profile.get('win_rate')):.2%} |"
    )
