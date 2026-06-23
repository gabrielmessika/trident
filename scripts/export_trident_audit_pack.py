#!/usr/bin/env python3
"""Export a compact, source-free audit pack from fetched TRIDENT server data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

P103_EXTERNAL_REFERENCE_SHADOW_FIELDS = [
    "external_reference_shadow_mode",
    "external_reference_shadow_available",
    "would_block_external_reference_abs_premium_gt_50",
    "would_block_external_reference_abs_premium_gt_100",
    "would_block_external_reference_counter_momentum_5m_6bps",
    "would_block_external_reference_candidate_loose_5m",
    "would_block_external_reference_candidate_default_5m",
    "external_reference_shadow_reason",
    "external_reference_fresh_shadow_available",
    "would_block_external_reference_fresh_abs_premium_gt_50",
    "would_block_external_reference_fresh_counter_momentum_5m_6bps",
    "would_block_external_reference_fresh_candidate_loose_5m",
    "would_block_external_reference_fresh_candidate_default_5m",
    "external_reference_fresh_shadow_reason",
    "external_reference_live_policy_enabled",
    "external_reference_fresh_cap_sizing_active",
    "external_reference_fresh_cap_gate",
    "external_reference_fresh_cap_multiplier",
    "external_reference_fresh_cap_reason",
    "external_reference_fresh_cap_original_target_notional_usd",
    "external_reference_fresh_cap_original_margin_usd",
    "external_reference_fresh_cap_original_risk_budget_usd",
    "external_reference_fresh_cap_original_expected_loss_usd",
    "external_reference_shadow_live_action_unchanged",
]

P106_REGIME_SHADOW_FIELDS = [
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
]

P107_ORDER_BLOCK_SHADOW_FIELDS = [
    "order_block_shadow_mode",
    "bullish_order_blocks_1h4h",
    "bearish_order_blocks_1h4h",
    "has_bullish_order_block_1h4h",
    "has_bearish_order_block_1h4h",
    "would_block_long_order_block_shadow",
    "would_open_defensive_short_order_block_shadow",
]

P108_DYNAMIC_SYMBOL_GUARD_FIELDS = [
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
    "symbol_setup_rolling_trades",
    "symbol_setup_rolling_pnl_usd",
    "symbol_setup_rolling_expectancy_usd",
    "symbol_setup_rolling_profit_factor",
    "dynamic_symbol_guard_live_policy_enabled",
    "dynamic_symbol_guard_live_sizing_active",
    "dynamic_symbol_guard_live_sizing_multiplier",
    "dynamic_symbol_guard_live_sizing_reason",
    "dynamic_symbol_guard_original_target_notional_usd",
    "dynamic_symbol_guard_original_margin_usd",
    "dynamic_symbol_guard_original_risk_budget_usd",
    "dynamic_symbol_guard_original_expected_loss_usd",
    "dynamic_symbol_guard_recovery_sizing_active",
    "dynamic_symbol_guard_recovery_multiplier",
    "dynamic_symbol_guard_recovery_reason",
    "dynamic_symbol_guard_recovery_original_target_notional_usd",
    "dynamic_symbol_guard_recovery_original_margin_usd",
    "dynamic_symbol_guard_recovery_original_risk_budget_usd",
    "dynamic_symbol_guard_recovery_original_expected_loss_usd",
    "dynamic_symbol_guard_loss_probation_sizing_active",
    "dynamic_symbol_guard_loss_probation_multiplier",
    "dynamic_symbol_guard_loss_probation_reason",
    "dynamic_symbol_guard_loss_probation_original_target_notional_usd",
    "dynamic_symbol_guard_loss_probation_original_margin_usd",
    "dynamic_symbol_guard_loss_probation_original_risk_budget_usd",
    "dynamic_symbol_guard_loss_probation_original_expected_loss_usd",
]

P109_OIL_SHADOW_FIELDS = [
    "p109_oil_shadow_mode",
    "p109_oil_pattern",
    "p109_oil_symbol",
    "p109_oil_shadow_side",
    "p109_oil_shadow_horizon_min",
    "p109_oil_shadow_research_regime",
    "p109_oil_shadow_hour_utc",
    "p109_oil_shadow_score",
    "p109_oil_shadow_reason",
    "would_open_p109_oil_short_shadow",
    "p109_oil_shadow_live_action_unchanged",
    "p109_oil_promoted",
    "p109_oil_promoted_mode",
    "p109_oil_promoted_decision_date",
    "p109_oil_promoted_source",
    "p109_oil_promoted_setup",
    "p109_oil_promoted_live_action",
    "p109_oil_promoted_confidence",
]

P115_MICROSTRUCTURE_SHADOW_FIELDS = [
    "microstructure_shadow_active",
    "microstructure_shadow_version",
    "microstructure_shadow_side",
    "microstructure_shadow_score",
    "microstructure_shadow_bucket",
    "microstructure_shadow_spread_score",
    "microstructure_shadow_flow_score",
    "microstructure_shadow_microprice_score",
    "microstructure_shadow_depth_score",
    "microstructure_shadow_activity_score",
    "microstructure_shadow_range_score",
    "microstructure_shadow_churn_score",
    "microstructure_shadow_flow",
    "microstructure_shadow_microprice_bps",
    "microstructure_shadow_depth_ratio",
    "microstructure_shadow_bucket_notional_usd",
    "microstructure_shadow_churn",
    "microstructure_shadow_missing_flags",
    "p115_microstructure_cap_counterfactual",
    "p115_microstructure_cap_multiplier",
    "p115_microstructure_score_threshold",
    "p115_microstructure_cap_reason",
    "p115_microstructure_original_target_notional_usd",
    "p115_microstructure_original_margin_usd",
    "p115_microstructure_original_risk_budget_usd",
    "p115_microstructure_original_expected_loss_usd",
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_file_manifest(output_dir: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        rel_path = str(path.relative_to(output_dir))
        files[rel_path] = {
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return files


def jsonl_records(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError as exc:
                yield line_number, {"_json_error": str(exc)}


def source_meta(path: Path, source_root: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path.relative_to(ROOT) if path.is_absolute() and path.is_relative_to(ROOT) else path),
            "exists": False,
        }
    stat = path.stat()
    return {
        "path": str(path.relative_to(source_root) if path.is_relative_to(source_root) else path),
        "exists": True,
        "size_bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def latest_file(base: Path, pattern: str) -> Path | None:
    paths = [path for path in base.glob(pattern) if path.is_file()]
    if not paths:
        return None
    return max(paths, key=lambda path: path.stat().st_mtime)


def copy_if_exists(src: Path | None, output_dir: Path, output_name: str, source_root: Path) -> str | None:
    if src is None or not src.exists():
        return None
    shutil.copyfile(src, output_dir / output_name)
    return str(src.relative_to(source_root))


def compact_setup_details(details: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "family",
        "global_regime",
        "cluster_regime",
        "market_cluster",
        "cluster_leader",
        "cluster_aligned",
        "cluster_strategy",
        "structure_score",
        "trend_bps",
        "trend_1h_bps",
        "trend_4h_bps",
        "vwap_distance_bps",
        "vwap_reclaim_score",
        "bucket_range_bps",
        "bucket_trade_count",
        "bucket_notional_usd",
        "activity_ratio",
        "trade_count_ratio",
        "flow_support_score",
        "book_imbalance",
        "trade_flow_bias",
        "btc_overextension_score",
        "external_reference_available",
        "external_reference_price",
        "external_reference_source_count",
        "external_reference_sources",
        "external_reference_symbol",
        "external_reference_time",
        "external_reference_age_seconds",
        "external_reference_max_deviation_bps",
        "external_premium_bps",
        "external_momentum_60s_bps",
        "external_momentum_300s_bps",
        "external_alignment_score",
        *P103_EXTERNAL_REFERENCE_SHADOW_FIELDS,
        *P106_REGIME_SHADOW_FIELDS,
        *P107_ORDER_BLOCK_SHADOW_FIELDS,
        *P108_DYNAMIC_SYMBOL_GUARD_FIELDS,
        *P109_OIL_SHADOW_FIELDS,
        *P115_MICROSTRUCTURE_SHADOW_FIELDS,
        "a_grade_active",
        "a_grade_level",
        "a_grade_score",
        "a_grade_size_scale",
        "a_grade_requested_size_scale",
        "a_grade_size_headroom_cap_active",
        "a_grade_size_headroom_cap_reasons",
        "a_grade_size_headroom_cap_margin_usd",
        "a_grade_size_headroom_cap_risk_budget_usd",
        "a_grade_reason",
        "a_grade_strong",
        "live_cap_active",
        "live_quality_sizing_active",
        "live_quality_sizing_multiplier",
        "live_quality_sizing_reasons",
        "live_quality_original_target_notional_usd",
        "live_quality_original_margin_usd",
        "live_quality_original_risk_budget_usd",
        "live_quality_original_expected_loss_usd",
    ]
    return {key: details.get(key) for key in keys if key in details}


def combine_setup_details(*sources: Any) -> dict[str, Any]:
    combined: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key not in combined or combined[key] in (None, ""):
                combined[key] = value
    return combined


def compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "price",
        "spread_bps",
        "structure_score",
        "vwap_distance_bps",
        "funding_rate",
        "bucket_notional_usd",
        "bucket_trade_count",
        "bucket_range_bps",
        "trade_flow_bias",
        "book_imbalance",
        "volume_ratio",
        "trade_count_ratio",
        "realized_vol_short_bps",
        "realized_vol_long_bps",
        "compression_score",
        "external_reference_price",
        "external_reference_source_count",
        "external_reference_sources",
        "external_reference_symbol",
        "external_reference_time",
        "external_reference_age_seconds",
        "external_reference_max_deviation_bps",
        "external_premium_bps",
        "external_momentum_60s_bps",
        "external_momentum_300s_bps",
        "external_alignment_score",
        *P103_EXTERNAL_REFERENCE_SHADOW_FIELDS,
        *P108_DYNAMIC_SYMBOL_GUARD_FIELDS,
        "source",
    ]
    return {key: snapshot.get(key) for key in keys if key in snapshot}


def compact_regime(snapshot: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "ready",
        "adx",
        "atr_ratio",
        "range_width_bps",
        "structure_score",
        "btc_impulse",
        "leader_symbol",
        "symbol_count",
        "active_symbol_count",
        "aligned_symbol_count",
        "breadth_pct",
        "dispersion_pct",
        "leader_trend_score",
        "coherence_score",
    ]
    return {key: snapshot.get(key) for key in keys if key in snapshot}


def export_directional_logs(source_root: Path, output_dir: Path) -> dict[str, Any]:
    decisions_path = output_dir / "trident_ac_signal_decisions.jsonl"
    fills_path = output_dir / "trident_ac_fill_events.csv"
    closed_trades_path = output_dir / "trident_ac_closed_trades.csv"
    sources = {
        "pod_a": source_root / "logs" / "pod_a_live.jsonl",
        "pod_c": source_root / "logs" / "pod_c_live.jsonl",
    }
    decision_count = 0
    fill_count = 0
    opened_count = Counter()
    close_fill_count = Counter()
    risk_reasons = Counter()
    review_reasons = Counter()

    fill_fields = [
        "event_ts",
        "pod",
        "symbol",
        "side",
        "setup",
        "action",
        "fill_ts",
        "price",
        "notional_usd",
        "fee_usd",
        "slippage_bps",
        "filled_size",
        "oid",
        "cloid",
        "complete",
        "exchange_fill_available",
        "exchange_fee_usd",
        "exchange_closed_pnl_usd",
        "exchange_direction",
        "exchange_timestamp_ms",
        "fee_source",
        "close_reason",
        "funding_usd",
        "funding_source",
        "funding_payment_count",
        "risk_accepted",
        "risk_reason",
        "confidence",
        "regime",
        "market_cluster",
        "cluster_regime",
        "target_notional_usd",
        "margin_usd",
        "effective_leverage",
        "stop_bps",
        "expected_loss_usd",
        "risk_budget_usd",
    ]

    with decisions_path.open("w", encoding="utf-8") as decisions, fills_path.open(
        "w", encoding="utf-8", newline=""
    ) as fills_file:
        fills = csv.DictWriter(fills_file, fieldnames=fill_fields)
        fills.writeheader()

        for pod, path in sources.items():
            if not path.exists():
                continue
            for line_number, record in jsonl_records(path):
                if "_json_error" in record:
                    continue
                event_type = record.get("event_type")
                base = {
                    "event_ts": record.get("timestamp"),
                    "pod": pod,
                    "source": record.get("source"),
                    "source_file": str(path.relative_to(source_root)),
                    "source_line": line_number,
                    "record_index": record.get("record_index"),
                    "event_type": event_type,
                    "regime": record.get("regime"),
                    "symbol_snapshot": compact_snapshot(record.get("symbol_snapshot") or {}),
                    "regime_snapshot": compact_regime(record.get("regime_snapshot") or {}),
                }
                if event_type == "signal":
                    signal = record.get("signal") or {}
                    risk = signal.get("risk") or {}
                    execution = signal.get("execution") or {}
                    allocation = signal.get("allocation") or {}
                    setup_details = combine_setup_details(
                        signal.get("setup_details"),
                        signal.get("regime_shadow"),
                        signal.get("order_block_shadow"),
                        signal.get("dynamic_symbol_guard"),
                        signal.get("symbol_guard_shadow"),
                        signal.get("p109_oil_shadow"),
                    )
                    compact = {
                        **base,
                        "symbol": signal.get("symbol"),
                        "side": signal.get("side"),
                        "setup": signal.get("setup"),
                        "confidence": signal.get("confidence"),
                        "reason_summary": signal.get("reason_summary"),
                        "setup_details": compact_setup_details(setup_details),
                        "confidence_components": signal.get("confidence_components") or {},
                        "allocation": {
                            "pod_target_usd": allocation.get("pod_target_usd"),
                            "symbol_target_usd": allocation.get("symbol_target_usd"),
                            "reason_summary": allocation.get("reason_summary"),
                            "correlation_group": allocation.get("correlation_group"),
                            "correlation_density_factor": allocation.get("correlation_density_factor"),
                            "capped_by_correlation": allocation.get("capped_by_correlation"),
                        },
                        "risk": {
                            "accepted": risk.get("accepted"),
                            "reason": risk.get("reason"),
                            "target_notional_usd": risk.get("target_notional_usd"),
                            "margin_usd": risk.get("margin_usd"),
                            "effective_leverage": risk.get("effective_leverage"),
                            "risk_budget_usd": risk.get("risk_budget_usd"),
                            "expected_loss_usd": risk.get("expected_loss_usd"),
                            "stop_bps": risk.get("stop_bps"),
                            "invalidation_price": risk.get("invalidation_price"),
                        },
                        "execution": {
                            "had_open_position_before": execution.get("had_open_position_before"),
                            "has_open_position_after": execution.get("has_open_position_after"),
                            "opened": execution.get("opened"),
                            "skipped_open": execution.get("skipped_open"),
                            "skip_reason": execution.get("skip_reason"),
                            "close_reason": execution.get("close_reason"),
                            "open_fill_count": len(execution.get("open_fills") or []),
                            "close_fill_count": len(execution.get("close_fills") or []),
                        },
                    }
                    decisions.write(json.dumps(compact, sort_keys=True) + "\n")
                    decision_count += 1
                    if risk.get("accepted") is False:
                        risk_reasons[str(risk.get("reason"))] += 1
                    if execution.get("opened"):
                        opened_count[pod] += 1

                    for fill in (execution.get("open_fills") or []) + (execution.get("close_fills") or []):
                        row = {
                            "event_ts": record.get("timestamp"),
                            "pod": pod,
                            "symbol": signal.get("symbol"),
                            "side": signal.get("side"),
                            "setup": signal.get("setup"),
                            "action": fill.get("action"),
                            "fill_ts": fill.get("timestamp"),
                            "price": fill.get("price"),
                            "notional_usd": fill.get("notional_usd"),
                            "fee_usd": fill.get("fee_usd"),
                            "slippage_bps": fill.get("slippage_bps"),
                            "filled_size": fill.get("filled_size"),
                            "oid": fill.get("oid"),
                            "cloid": fill.get("cloid"),
                            "complete": fill.get("complete"),
                            "exchange_fill_available": fill.get("exchange_fill_available"),
                            "exchange_fee_usd": fill.get("exchange_fee_usd"),
                            "exchange_closed_pnl_usd": fill.get("exchange_closed_pnl_usd"),
                            "exchange_direction": fill.get("exchange_direction"),
                            "exchange_timestamp_ms": fill.get("exchange_timestamp_ms"),
                            "fee_source": fill.get("fee_source"),
                            "close_reason": fill.get("close_reason"),
                            "funding_usd": fill.get("funding_usd"),
                            "funding_source": fill.get("funding_source"),
                            "funding_payment_count": fill.get("funding_payment_count"),
                            "risk_accepted": risk.get("accepted"),
                            "risk_reason": risk.get("reason"),
                            "confidence": signal.get("confidence"),
                            "regime": record.get("regime"),
                            "market_cluster": setup_details.get("market_cluster"),
                            "cluster_regime": setup_details.get("cluster_regime"),
                            "target_notional_usd": risk.get("target_notional_usd"),
                            "margin_usd": risk.get("margin_usd"),
                            "effective_leverage": risk.get("effective_leverage"),
                            "stop_bps": risk.get("stop_bps"),
                            "expected_loss_usd": risk.get("expected_loss_usd"),
                            "risk_budget_usd": risk.get("risk_budget_usd"),
                        }
                        fills.writerow(row)
                        fill_count += 1
                        if fill.get("action") == "close":
                            close_fill_count[pod] += 1
                elif event_type == "trade_close":
                    trade = record.get("trade") or {}
                    if not isinstance(trade, dict):
                        continue
                    close_fills = trade.get("close_fills") or record.get("close_fills") or []
                    if not isinstance(close_fills, list):
                        close_fills = []
                    for fill in close_fills:
                        if not isinstance(fill, dict):
                            continue
                        row = {
                            "event_ts": record.get("timestamp"),
                            "pod": pod,
                            "symbol": trade.get("symbol") or fill.get("symbol"),
                            "side": trade.get("side") or fill.get("side"),
                            "setup": trade.get("setup") or trade.get("open_reason"),
                            "action": fill.get("action") or "close",
                            "fill_ts": fill.get("timestamp") or record.get("timestamp"),
                            "price": fill.get("price"),
                            "notional_usd": fill.get("notional_usd"),
                            "fee_usd": fill.get("fee_usd"),
                            "slippage_bps": fill.get("slippage_bps"),
                            "filled_size": fill.get("filled_size"),
                            "oid": fill.get("oid"),
                            "cloid": fill.get("cloid"),
                            "complete": fill.get("complete"),
                            "exchange_fill_available": fill.get("exchange_fill_available"),
                            "exchange_fee_usd": fill.get("exchange_fee_usd"),
                            "exchange_closed_pnl_usd": fill.get("exchange_closed_pnl_usd"),
                            "exchange_direction": fill.get("exchange_direction"),
                            "exchange_timestamp_ms": fill.get("exchange_timestamp_ms"),
                            "fee_source": fill.get("fee_source") or trade.get("fee_source"),
                            "close_reason": fill.get("close_reason") or trade.get("close_reason"),
                            "funding_usd": fill.get("funding_usd") or trade.get("funding_usd"),
                            "funding_source": fill.get("funding_source") or trade.get("funding_source"),
                            "funding_payment_count": (
                                fill.get("funding_payment_count")
                                or trade.get("funding_payment_count")
                            ),
                            "risk_accepted": None,
                            "risk_reason": None,
                            "confidence": trade.get("confidence"),
                            "regime": record.get("regime"),
                            "market_cluster": (trade.get("setup_details") or {}).get("market_cluster")
                            if isinstance(trade.get("setup_details"), dict)
                            else trade.get("market_cluster"),
                            "cluster_regime": (trade.get("setup_details") or {}).get("cluster_regime")
                            if isinstance(trade.get("setup_details"), dict)
                            else None,
                            "target_notional_usd": trade.get("target_notional_usd"),
                            "margin_usd": trade.get("margin_usd"),
                            "effective_leverage": trade.get("effective_leverage") or trade.get("leverage"),
                            "stop_bps": trade.get("stop_bps"),
                            "expected_loss_usd": trade.get("expected_loss_usd"),
                            "risk_budget_usd": trade.get("risk_budget_usd"),
                        }
                        fills.writerow(row)
                        fill_count += 1
                        close_fill_count[pod] += 1
                elif event_type == "signal_review":
                    review = record.get("review") or {}
                    setup_details = combine_setup_details(
                        review.get("setup_details"),
                        review.get("regime_shadow"),
                        review.get("order_block_shadow"),
                        review.get("dynamic_symbol_guard"),
                        review.get("symbol_guard_shadow"),
                        review.get("p109_oil_shadow"),
                    )
                    compact = {
                        **base,
                        "symbol": review.get("symbol"),
                        "status": review.get("status"),
                        "preferred_side": review.get("preferred_side"),
                        "reason_summary": review.get("reason_summary"),
                        "failure_reasons": review.get("failure_reasons"),
                        "setup_details": compact_setup_details(setup_details),
                    }
                    decisions.write(json.dumps(compact, sort_keys=True) + "\n")
                    decision_count += 1
                    if review.get("reason_summary"):
                        review_reasons[str(review.get("reason_summary"))] += 1

    statuses = {}
    open_positions = {}
    closed_counts = Counter()
    closed_pnl = Counter()
    close_reasons = Counter()
    closed_fields = [
        "pod",
        "date",
        "symbol",
        "side",
        "setup",
        "open_reason",
        "confidence",
        "market_cluster",
        "close_regime",
        "entry_price",
        "exit_price",
        "target_notional_usd",
        "margin_usd",
        "leverage",
        "effective_leverage",
        "risk_budget_usd",
        "expected_loss_usd",
        "invalidation_price",
        "stop_bps",
        "time_stop_hours",
        "take_profit_bps",
        "break_even_trigger_bps",
        "trailing_activation_bps",
        "trailing_distance_bps",
        "best_price_seen",
        "worst_price_seen",
        "mfe_bps",
        "mae_bps",
        "pnl_usd",
        "is_win",
        "gross_pnl_usd",
        "fees_usd",
        "exchange_fee_usd",
        "exchange_closed_pnl_usd",
        "fee_source",
        "funding_usd",
        "funding_source",
        "funding_payment_count",
        "close_reason",
        "close_fill_count",
        "exchange_close_fill_count",
        "close_fill_oids",
        "hold_hours",
        "opened_at",
        "closed_at",
        *P106_REGIME_SHADOW_FIELDS,
        *P107_ORDER_BLOCK_SHADOW_FIELDS,
        *P108_DYNAMIC_SYMBOL_GUARD_FIELDS,
        *P109_OIL_SHADOW_FIELDS,
        *P115_MICROSTRUCTURE_SHADOW_FIELDS,
        "a_grade_active",
        "a_grade_score",
        "a_grade_level",
        "a_grade_size_scale",
        "a_grade_requested_size_scale",
        "a_grade_size_headroom_cap_active",
        "a_grade_size_headroom_cap_reasons",
        "a_grade_size_headroom_cap_margin_usd",
        "a_grade_size_headroom_cap_risk_budget_usd",
        "a_grade_reason",
        "live_cap_active",
        "live_cap_effective_target_notional_usd",
        "live_quality_sizing_active",
        "live_quality_sizing_multiplier",
        "live_quality_sizing_reasons",
        "live_quality_original_target_notional_usd",
        "live_quality_original_margin_usd",
        "live_quality_original_risk_budget_usd",
        "live_quality_original_expected_loss_usd",
        "pattern_watch_hits",
        "pattern_watch_count",
        "cluster_strategy",
        "cluster_regime",
        "trend_bps",
        "trend_1h_bps",
        "trend_4h_bps",
        "structure_score",
        "vwap_distance_bps",
        "vwap_reclaim_score",
        "external_reference_available",
        "external_reference_price",
        "external_reference_source_count",
        "external_reference_sources",
        "external_reference_symbol",
        "external_reference_time",
        "external_reference_age_seconds",
        "external_reference_max_deviation_bps",
        "external_premium_bps",
        "external_momentum_60s_bps",
        "external_momentum_300s_bps",
        "external_alignment_score",
        *P103_EXTERNAL_REFERENCE_SHADOW_FIELDS,
    ]

    def closed_trade_identity(pod: str, trade: dict[str, Any], record: dict[str, Any] | None = None) -> tuple[str, ...]:
        return (
            pod,
            str(trade.get("symbol") or ""),
            str(trade.get("side") or ""),
            str(trade.get("opened_at") or ""),
            str(trade.get("closed_at") or ""),
            str(trade.get("close_reason") or ""),
            str(trade.get("pnl_usd") or ""),
        )

    def close_fill_oids(trade: dict[str, Any]) -> str:
        fills = trade.get("close_fills") or []
        if not isinstance(fills, list):
            return ""
        oids: list[str] = []
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            oid = fill.get("oid")
            if oid not in (None, ""):
                oids.append(str(oid))
        return ",".join(oids)

    def write_closed_trade_row(
        writer: csv.DictWriter,
        *,
        pod: str,
        trade: dict[str, Any],
        record: dict[str, Any] | None = None,
    ) -> None:
        details = trade.get("setup_details") or {}
        if not isinstance(details, dict):
            details = {}
        row = {key: trade.get(key) for key in closed_fields}
        row["pod"] = pod
        row["date"] = (
            trade.get("date")
            or str(trade.get("closed_at") or (record or {}).get("timestamp") or "")[:10]
        )
        row["leverage"] = trade.get("leverage") or trade.get("effective_leverage")
        row["is_win"] = trade.get("is_win")
        if row["is_win"] is None:
            try:
                row["is_win"] = float(trade.get("pnl_usd") or 0.0) >= 0
            except (TypeError, ValueError):
                row["is_win"] = None
        row["close_fill_oids"] = close_fill_oids(trade)
        for key in [
            *P106_REGIME_SHADOW_FIELDS,
            *P107_ORDER_BLOCK_SHADOW_FIELDS,
            *P108_DYNAMIC_SYMBOL_GUARD_FIELDS,
            *P109_OIL_SHADOW_FIELDS,
            *P115_MICROSTRUCTURE_SHADOW_FIELDS,
            "a_grade_active",
            "a_grade_score",
            "a_grade_level",
            "a_grade_size_scale",
            "a_grade_requested_size_scale",
            "a_grade_size_headroom_cap_active",
            "a_grade_size_headroom_cap_reasons",
            "a_grade_size_headroom_cap_margin_usd",
            "a_grade_size_headroom_cap_risk_budget_usd",
            "a_grade_reason",
            "live_cap_active",
            "live_cap_effective_target_notional_usd",
            "live_quality_sizing_active",
            "live_quality_sizing_multiplier",
            "live_quality_sizing_reasons",
            "live_quality_original_target_notional_usd",
            "live_quality_original_margin_usd",
            "live_quality_original_risk_budget_usd",
            "live_quality_original_expected_loss_usd",
            "pattern_watch_hits",
            "pattern_watch_count",
            "cluster_strategy",
            "cluster_regime",
            "trend_bps",
            "trend_1h_bps",
            "trend_4h_bps",
            "structure_score",
            "vwap_distance_bps",
            "vwap_reclaim_score",
            "external_reference_available",
            "external_reference_price",
            "external_reference_source_count",
            "external_reference_sources",
            "external_reference_symbol",
            "external_reference_time",
            "external_reference_age_seconds",
            "external_reference_max_deviation_bps",
            "external_premium_bps",
            "external_momentum_60s_bps",
            "external_momentum_300s_bps",
            "external_alignment_score",
            *P103_EXTERNAL_REFERENCE_SHADOW_FIELDS,
        ]:
            row[key] = details.get(key)
        writer.writerow(row)

    with closed_trades_path.open("w", encoding="utf-8", newline="") as closed_file:
        closed_writer = csv.DictWriter(closed_file, fieldnames=closed_fields)
        closed_writer.writeheader()
        seen_closed: set[tuple[str, ...]] = set()
        for pod in ["pod_a", "pod_c"]:
            log_path = sources[pod]
            if log_path.exists():
                for _, record in jsonl_records(log_path):
                    if record.get("event_type") != "trade_close":
                        continue
                    trade = record.get("trade") or {}
                    if not isinstance(trade, dict):
                        continue
                    identity = closed_trade_identity(pod, trade, record)
                    if identity in seen_closed:
                        continue
                    seen_closed.add(identity)
                    write_closed_trade_row(
                        closed_writer,
                        pod=pod,
                        trade=trade,
                        record=record,
                    )
                    closed_counts[pod] += 1
                    try:
                        closed_pnl[pod] += float(trade.get("pnl_usd") or 0.0)
                    except (TypeError, ValueError):
                        pass
                    if trade.get("close_reason"):
                        close_reasons[f"{pod}:{trade.get('close_reason')}"] += 1
            status_path = source_root / "runtime" / f"{pod}_live_status.json"
            if not status_path.exists():
                continue
            status = read_json(status_path)
            report = status.get("report") or {}
            closed_log = report.get("closed_trade_log") or status.get("closed_trade_log") or []
            for trade in closed_log:
                if not isinstance(trade, dict):
                    continue
                identity = closed_trade_identity(pod, trade)
                if identity in seen_closed:
                    continue
                seen_closed.add(identity)
                write_closed_trade_row(
                    closed_writer,
                    pod=pod,
                    trade=trade,
                    record=None,
                )
                closed_counts[pod] += 1
                try:
                    closed_pnl[pod] += float(trade.get("pnl_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
                if trade.get("close_reason"):
                    close_reasons[f"{pod}:{trade.get('close_reason')}"] += 1

    for pod in ["pod_a", "pod_c"]:
        status_path = source_root / "runtime" / f"{pod}_live_status.json"
        if not status_path.exists():
            continue
        status = read_json(status_path)
        statuses[pod] = {
            "mode": status.get("mode"),
            "process_state": status.get("process_state"),
            "updated_at": status.get("updated_at"),
            "live_trading_paused": status.get("live_trading_paused"),
            "live_reconciliation": status.get("live_reconciliation"),
            "report": status.get("report"),
            "open_position_count": len(status.get("open_positions") or []),
        }
        open_positions[pod] = status.get("open_positions") or []
    write_json(output_dir / "trident_ac_runtime_summary.json", statuses)
    write_json(output_dir / "trident_ac_open_positions.json", open_positions)
    live_state_copies = {}
    for pod in ["pod_a", "pod_c"]:
        copied = copy_if_exists(
            source_root / "runtime" / f"live_state_{pod}.json",
            output_dir,
            f"trident_ac_live_state_{pod}.json",
            source_root,
        )
        if copied:
            live_state_copies[f"trident_ac_live_state_{pod}.json"] = copied

    closed_total = sum(closed_counts.values())
    if closed_total:
        limitation = (
            "Closed-trade PnL is exported from append-only live trade_close "
            "journal records when present, with runtime closed_trade_log used "
            "only as fallback. Close fills are exported from enriched trade_close "
            "records and signal execution records; exchange fee/funding fields "
            "remain null when the live runner could not match a userFills record "
            "or no funding-payment stream was available."
        )
    else:
        limitation = (
            "Directional logs contain open fills and signal decisions, but the "
            "current fetched runtime status has empty closed_trade_log and no "
            "append-only trade_close records were found. Closed-trade PnL "
            "attribution still requires historical closed trade logs, exchange "
            "fills, or a full API report containing closed trade details."
        )

    return {
        "decisions_jsonl": str(decisions_path.name),
        "fill_events_csv": str(fills_path.name),
        "closed_trades_csv": str(closed_trades_path.name),
        "decision_rows": decision_count,
        "fill_rows": fill_count,
        "closed_trade_rows": closed_total,
        "closed_trade_count_by_pod": dict(closed_counts),
        "closed_trade_pnl_by_pod": {pod: round(value, 8) for pod, value in closed_pnl.items()},
        "top_close_reasons": close_reasons.most_common(20),
        "copied_live_state_files": live_state_copies,
        "opened_count_by_pod": dict(opened_count),
        "close_fill_count_by_pod": dict(close_fill_count),
        "top_risk_reject_reasons": risk_reasons.most_common(20),
        "top_signal_review_reasons": review_reasons.most_common(20),
        "limitation": limitation,
    }


def compact_hip4_decision(record: dict[str, Any], profile: str, source_file: str, line_number: int) -> dict[str, Any]:
    signal = record.get("signal") or {}
    decision = record.get("supervisor_decision") or {}
    metadata = signal.get("metadata") or {}
    return {
        "profile": profile,
        "event_ts": record.get("ts"),
        "source_file": source_file,
        "source_line": line_number,
        "market_id": signal.get("market_id"),
        "underlying": signal.get("underlying"),
        "edge_type": signal.get("edge_type"),
        "side": signal.get("side"),
        "expiry_ts": signal.get("expiry_ts"),
        "confidence": signal.get("confidence"),
        "gross_edge": signal.get("gross_edge"),
        "net_edge": signal.get("net_edge"),
        "requested_size_usdc": signal.get("requested_size_usdc"),
        "max_loss_usdc": signal.get("max_loss_usdc"),
        "signal_reason": signal.get("reason"),
        "approved": decision.get("approved"),
        "approved_size_usdc": decision.get("approved_size_usdc"),
        "decision_reason": decision.get("reason"),
        "execution_mode": decision.get("execution_mode"),
        "probability_model": metadata.get("probability_model"),
        "probability_yes": metadata.get("probability_yes"),
        "probability_confidence": metadata.get("probability_confidence"),
        "reference_price": metadata.get("reference_price"),
        "reference_source_count": metadata.get("reference_source_count"),
        "reference_max_deviation_bps": metadata.get("reference_max_deviation_bps"),
        "seconds_left": metadata.get("seconds_left") or metadata.get("time_to_expiry_seconds"),
        "strike": metadata.get("strike"),
        "yes_ask": metadata.get("yes_ask"),
        "no_ask": metadata.get("no_ask"),
    }


def export_hip4(source_root: Path, output_dir: Path) -> dict[str, Any]:
    hip4_root = source_root / "hip4"
    logs = {
        "mainnet_paper": hip4_root / "logs" / "hip4_outcome_mainnet_paper" / "decisions.jsonl",
        "mainnet_observer": hip4_root / "logs" / "hip4_outcome_mainnet" / "decisions.jsonl",
    }
    decisions_path = output_dir / "hip4_decisions.jsonl"
    decision_count = 0
    approved_count = Counter()
    reason_count = Counter()
    with decisions_path.open("w", encoding="utf-8") as out:
        for profile, path in logs.items():
            if not path.exists():
                continue
            source_file = str(path.relative_to(source_root))
            for line_number, record in jsonl_records(path):
                if "_json_error" in record:
                    continue
                compact = compact_hip4_decision(record, profile, source_file, line_number)
                out.write(json.dumps(compact, sort_keys=True) + "\n")
                decision_count += 1
                if compact.get("approved"):
                    approved_count[profile] += 1
                if compact.get("decision_reason"):
                    reason_count[f"{profile}:{compact['decision_reason']}"] += 1

    copies: dict[str, str] = {}
    latest_run_review_md = latest_file(hip4_root, "reviews/*/hip4_outcome_run_review.md")
    latest_run_review_json = latest_file(hip4_root, "reviews/*/hip4_outcome_run_review.json")
    copy_sources = {
        "hip4_trades.csv": hip4_root / "logs" / "hip4_outcome_mainnet_paper" / "trades.csv",
        "hip4_settlements.csv": hip4_root / "logs" / "hip4_outcome_mainnet_paper" / "settlements.csv",
        "hip4_shadow_exit_policies.csv": hip4_root / "logs" / "hip4_outcome_mainnet_paper" / "shadow_exit_policies.csv",
        "hip4_policy_market_audit_latest.json": hip4_root / "replay_reports" / "hip4_policy_market_audit_latest.json",
        "hip4_policy_market_audit_latest.md": hip4_root / "replay_reports" / "hip4_policy_market_audit_latest.md",
    }
    for output_name, src in copy_sources.items():
        if src.exists():
            shutil.copyfile(src, output_dir / output_name)
            copies[output_name] = str(src.relative_to(source_root))
    copied = copy_if_exists(latest_run_review_json, output_dir, "hip4_outcome_run_review_latest.json", source_root)
    if copied:
        copies["hip4_outcome_run_review_latest.json"] = copied
    copied = copy_if_exists(latest_run_review_md, output_dir, "hip4_outcome_run_review_latest.md", source_root)
    if copied:
        copies["hip4_outcome_run_review_latest.md"] = copied

    policy_json = copy_sources["hip4_policy_market_audit_latest.json"]
    if policy_json.exists():
        policy = read_json(policy_json).get("exit_policy_replay") or {}
        replay_path = output_dir / "hip4_policy_replay.csv"
        fields = [
            "policy",
            "source",
            "settlement_count",
            "exit_event_count",
            "net_pnl_usdc",
            "delta_vs_active_pnl_usdc",
            "profit_factor",
            "win_rate",
            "worst_pnl_usdc",
            "best_pnl_usdc",
            "unique_markets",
            "gross_profit_usdc",
            "gross_loss_usdc",
        ]
        with replay_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in policy.get("policy_summaries") or []:
                writer.writerow({key: row.get(key) for key in fields})
        copies[replay_path.name] = str(policy_json.relative_to(source_root))

        cutoff_path = output_dir / "hip4_policy_cutoff_replay.csv"
        cutoff_fields = ["entry_cutoff", *fields]
        with cutoff_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=cutoff_fields)
            writer.writeheader()
            for block in policy.get("entry_cutoff_summaries") or []:
                cutoff = block.get("entry_cutoff")
                for row in block.get("policies") or []:
                    writer.writerow({"entry_cutoff": cutoff, **{key: row.get(key) for key in fields}})
        copies[cutoff_path.name] = str(policy_json.relative_to(source_root))

    statuses = {}
    for name, path in {
        "hip4_outcome_status": hip4_root / "runtime" / "hip4_outcome_status.json",
        "hip4_outcome_mainnet_status": hip4_root / "runtime" / "hip4_outcome_mainnet_status.json",
        "hip4_nautilus_shadow_status": hip4_root / "runtime" / "hip4_nautilus_shadow_status.json",
    }.items():
        if path.exists():
            try:
                statuses[name] = read_json(path)
            except json.JSONDecodeError as exc:
                statuses[name] = {
                    "status": "invalid_json",
                    "path": str(path.relative_to(source_root)),
                    "error": str(exc),
                    "size_bytes": path.stat().st_size,
                }
    write_json(output_dir / "hip4_runtime_statuses.json", statuses)

    return {
        "decisions_jsonl": decisions_path.name,
        "decision_rows": decision_count,
        "approved_count_by_profile": dict(approved_count),
        "top_decision_reasons": reason_count.most_common(20),
        "copied_or_derived_files": copies,
    }


def compact_pod_backtest_summary(pod_report: dict[str, Any] | None) -> dict[str, Any]:
    if not pod_report:
        return {}
    keys = [
        "realized_pnl_usd",
        "gross_pnl_usd",
        "fees_usd",
        "closed_trade_count",
        "win_count",
        "loss_count",
        "win_rate",
        "max_drawdown_usd",
        "max_open_positions",
        "max_open_margin_usd",
        "max_open_notional_usd",
        "max_open_expected_loss_usd",
        "average_hold_hours",
    ]
    return {key: pod_report.get(key) for key in keys if key in pod_report}


def export_baseline_replays(source_root: Path, output_dir: Path) -> dict[str, Any]:
    replay_root = source_root / "replay_reports"
    copy_sources = {
        "baseline_reference_status_20260513.md": replay_root / "BACKTEST_REFERENCE_STATUS_20260513.md",
        "baseline_official_current_cli_20260513.md": replay_root / "official_baseline_current_cli_20260513.md",
        "baseline_official_current_cli_20260513.json": replay_root / "official_baseline_current_cli_20260513.json",
        "baseline_pod_a_evo11_promoted_20260513.md": replay_root / "pod_a_evo11_promoted_20260513.md",
        "baseline_pod_a_evo11_comparison_20260513.md": replay_root
        / "pod_a_improvement_levers_20260513"
        / "comparison.md",
        "baseline_no_pod_c_20260513.md": replay_root / "no_pod_c_20260513.md",
        "baseline_pod_c_cluster_multiplier_global_20260526.md": replay_root
        / "pod_c_cluster_multiplier_global_20260526"
        / "pod_c_cluster_multiplier_compare.md",
        "baseline_pod_c_cluster_multiplier_global_20260526.json": replay_root
        / "pod_c_cluster_multiplier_global_20260526"
        / "pod_c_cluster_multiplier_compare.json",
        "baseline_pod_c_cluster_multiplier_recent_20260526.md": replay_root
        / "pod_c_cluster_multiplier_recent_20260526"
        / "pod_c_cluster_multiplier_compare.md",
        "baseline_pod_c_cluster_multiplier_recent_20260526.json": replay_root
        / "pod_c_cluster_multiplier_recent_20260526"
        / "pod_c_cluster_multiplier_compare.json",
    }
    copies: dict[str, str] = {}
    for output_name, src in copy_sources.items():
        copied = copy_if_exists(src, output_dir, output_name, source_root)
        if copied:
            copies[output_name] = copied
    latest_p202_md = latest_file(
        replay_root,
        "p202_pod_c_cluster_multiplier_*/pod_c_cluster_multiplier_compare.md",
    )
    latest_p202_json = latest_file(
        replay_root,
        "p202_pod_c_cluster_multiplier_*/pod_c_cluster_multiplier_compare.json",
    )
    copied = copy_if_exists(
        latest_p202_md,
        output_dir,
        "p202_pod_c_cluster_multiplier_latest.md",
        source_root,
    )
    if copied:
        copies["p202_pod_c_cluster_multiplier_latest.md"] = copied
    copied = copy_if_exists(
        latest_p202_json,
        output_dir,
        "p202_pod_c_cluster_multiplier_latest.json",
        source_root,
    )
    if copied:
        copies["p202_pod_c_cluster_multiplier_latest.json"] = copied

    official_json = replay_root / "official_baseline_current_cli_20260513.json"
    official_summary = {}
    if official_json.exists():
        report = read_json(official_json)
        official_summary = {
            "status": "CURRENT_PROD_REFERENCE",
            "config": "config/trident.toml",
            "input_path": report.get("input_path"),
            "first_timestamp": report.get("first_timestamp"),
            "last_timestamp": report.get("last_timestamp"),
            "records_processed": report.get("records_processed"),
            "duplicate_timestamps_skipped": report.get("duplicate_timestamps_skipped"),
            "total_realized_pnl_usd": report.get("total_realized_pnl_usd"),
            "directional_fees_usd": report.get("directional_fees_usd"),
            "total_activity_count": report.get("total_activity_count"),
            "pod_a": compact_pod_backtest_summary(report.get("pod_a")),
            "pod_b": compact_pod_backtest_summary(report.get("pod_b")),
            "pod_c": compact_pod_backtest_summary(report.get("pod_c")),
            "routing": {
                "max_ownership_conflict_count": (report.get("routing") or {}).get(
                    "max_ownership_conflict_count"
                ),
                "reassignment_event_count": (report.get("routing") or {}).get("reassignment_event_count"),
            },
        }

    return {
        "official_baseline": official_summary,
        "copied_files": copies,
        "limitation": (
            "Baseline replay reports are copied for audit context. The replay input JSONL "
            "is not copied by this compact export; provide it separately if the external "
            "auditor must rerun the baseline."
        ),
    }


def latest_review_paths(source_root: Path) -> dict[str, Any]:
    candidates = {
        "trident_ac_review": latest_file(source_root, "reviews/*/review_summary.md"),
        "trident_ac_review_json": latest_file(source_root, "reviews/*/review_summary.json"),
        "hip4_run_review": latest_file(source_root / "hip4", "reviews/*/hip4_outcome_run_review.md"),
        "hip4_run_review_json": latest_file(source_root / "hip4", "reviews/*/hip4_outcome_run_review.json"),
        "hip4_policy_market_audit": source_root / "hip4" / "replay_reports" / "hip4_policy_market_audit_latest.md",
        "hip4_policy_market_audit_json": source_root / "hip4" / "replay_reports" / "hip4_policy_market_audit_latest.json",
    }
    return {
        name: source_meta(path, source_root) if path is not None else {"exists": False}
        for name, path in candidates.items()
    }


def copy_latest_common_reviews(source_root: Path, output_dir: Path) -> dict[str, str]:
    copies: dict[str, str] = {}
    review_map = {
        "trident_ac_review_summary_latest.md": latest_file(source_root, "reviews/*/review_summary.md"),
        "trident_ac_review_summary_latest.json": latest_file(source_root, "reviews/*/review_summary.json"),
    }
    for output_name, src in review_map.items():
        copied = copy_if_exists(src, output_dir, output_name, source_root)
        if copied:
            copies[output_name] = copied
    return copies




def write_readme(output_dir: Path, manifest: dict[str, Any]) -> None:
    lines = [
        "# TRIDENT external audit export",
        "",
        f"- generated_at: `{manifest['generated_at']}`",
        f"- source_root: `{manifest['source_root']}`",
        f"- fresh_fetch_run: `{manifest['fresh_fetch_run']}`",
        "",
        "This folder is designed to be sent with `docs/trident_project_audit_map.md`",
        "and `docs/trident_audit_annexes/` to an auditor that does not have repo",
        "access.",
        "",
        "Important limitations:",
        "",
        "- A/C closed-trade PnL attribution is available when",
        "  `trident_ac_closed_trades.csv` has rows.",
        "- A/C close-fill reconciliation uses enriched live `trade_close`",
        "  journal records when present; exchange fee/funding fields stay empty",
        "  when the live runner could not match userFills or no realized funding",
        "  stream was available.",
        "- HIP4 decisions are compacted and do not include raw reference-source",
        "  payloads, to avoid unnecessary data bloat.",
        "- No secrets are intentionally exported.",
        "",
        "Key files:",
        "",
        "- `trident_ac_signal_decisions.jsonl`",
        "- `trident_ac_fill_events.csv`",
        "- `trident_ac_closed_trades.csv`",
        "- `trident_ac_runtime_summary.json`",
        "- `trident_ac_open_positions.json`",
        "- `trident_ac_live_state_pod_a.json`",
        "- `trident_ac_live_state_pod_c.json`",
        "- `baseline_reference_status_20260513.md`",
        "- `baseline_official_current_cli_20260513.md`",
        "- `baseline_official_current_cli_20260513.json`",
        "- `trident_active_plan.md`",
        "- `hip4_decisions.jsonl`",
        "- `hip4_trades.csv`",
        "- `hip4_settlements.csv`",
        "- `hip4_policy_replay.csv`",
        "- `hip4_policy_cutoff_replay.csv`",
        "- `manifest.json` with per-file SHA-256 checksums",
        "",
    ]
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(ROOT / "server-data"), help="Fetched server-data directory")
    parser.add_argument("--output", default=None, help="Output directory")
    parser.add_argument(
        "--fresh-fetch-run",
        action="store_true",
        help="Mark manifest as based on a fresh fetch run. This script does not fetch by itself.",
    )
    args = parser.parse_args()

    source_root = Path(args.source).resolve()
    output_dir = Path(args.output).resolve() if args.output else source_root / "audit_exports" / utc_stamp()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root),
        "output_dir": str(output_dir),
        "fresh_fetch_run": bool(args.fresh_fetch_run),
        "contains_secrets": False,
        "source_reviews": latest_review_paths(source_root),
        "exports": {},
        "warnings": [],
    }
    manifest["exports"]["trident_ac"] = export_directional_logs(source_root, output_dir)
    manifest["exports"]["hip4"] = export_hip4(source_root, output_dir)
    manifest["exports"]["baseline_replays"] = export_baseline_replays(source_root, output_dir)
    manifest["exports"]["copied_reviews"] = copy_latest_common_reviews(source_root, output_dir)
    active_plan = ROOT / "docs" / "trident_active_plan.md"
    copied_plan = copy_if_exists(active_plan, output_dir, "trident_active_plan.md", ROOT)
    manifest["exports"]["active_plan"] = {
        "output": "trident_active_plan.md" if copied_plan else None,
        "source": copied_plan,
    }
    manifest["warnings"].append(
        "If the external audit needs exact close-fill reconciliation for TRIDENT A/C, "
        "provide exchange fills in addition to this export."
    )
    manifest["warnings"].append(
        "This script does not run fetch_all_data.sh; use --fresh-fetch-run only after a fresh fetch."
    )

    write_readme(output_dir, manifest)
    manifest["file_manifest"] = build_file_manifest(output_dir)
    write_json(output_dir / "manifest.json", manifest)
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
