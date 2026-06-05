from __future__ import annotations

import os
from pathlib import Path

from app.live.runtime_status import load_runtime_status, runtime_status_is_fresh
from app.observability.runtime_merge import merge_runtime_supervisor_snapshot
from app.reporting.live_journal import attach_live_journal_report
from app.reporting.multi_pod import (
    _is_supervisor_fallback_runtime,
    is_hip4_pod_b_replacement_runtime,
)
from app.trident.market_clusters import cluster_for_symbol
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName


HIP4_APP_KINDS = {"trident-hip4", "hip4", "hip4-outcome"}


def _hip4_metrics_enabled() -> bool:
    app_kind = os.getenv("TRIDENT_APP_KIND", "trident").strip().lower()
    if app_kind in HIP4_APP_KINDS:
        return True
    raw_value = os.getenv("TRIDENT_ENABLE_HIP4_OUTCOME")
    if raw_value is None or raw_value == "":
        return False
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


class MetricsRegistry:
    """Lightweight in-memory metrics derived from the supervisor snapshot."""

    def __init__(self) -> None:
        self._metrics: dict[str, int | float] = {"trident_bootstrap_ready": 1}

    def refresh_from_supervisor(self, supervisor: TridentSupervisor) -> None:
        hip4_enabled = _hip4_metrics_enabled()
        pod_health = supervisor.pod_health()
        pod_b_status = self._pod_b_runtime_status(supervisor) if hip4_enabled else {}
        symbol_ownership = supervisor.registry.snapshot()
        live_journals_enabled = supervisor.mode == "live"
        pod_a_runtime = attach_live_journal_report(
            load_runtime_status("logs/pod_a_live_status.json"),
            "logs/pod_a_live.jsonl",
            enabled=live_journals_enabled,
            market_cluster_for_symbol=lambda symbol: cluster_for_symbol(
                supervisor.config,
                symbol,
            ),
        )
        pod_b_runtime = (
            load_runtime_status("logs/pod_b_live_status.json")
            if hip4_enabled
            else None
        )
        pod_c_runtime = attach_live_journal_report(
            load_runtime_status("logs/pod_c_live_status.json"),
            "logs/pod_c_live.jsonl",
            enabled=live_journals_enabled,
            market_cluster_for_symbol=lambda symbol: cluster_for_symbol(
                supervisor.config,
                symbol,
            ),
        )
        runtime_supervisor = merge_runtime_supervisor_snapshot(
            pod_a_runtime,
            pod_b_runtime,
            pod_c_runtime,
        )

        runtime_pod_a_healthy = runtime_status_is_fresh(pod_a_runtime)
        runtime_pod_c_healthy = runtime_status_is_fresh(pod_c_runtime)
        runtime_pod_b_healthy = (
            hip4_enabled
            and isinstance(pod_b_status, dict)
            and runtime_status_is_fresh(pod_b_status)
            and not _is_supervisor_fallback_runtime(pod_b_status)
        )
        pod_b_replacement_enabled = (
            hip4_enabled
            and isinstance(pod_b_status, dict)
            and is_hip4_pod_b_replacement_runtime(pod_b_status)
            and runtime_status_is_fresh(pod_b_status)
        )
        healthy_pod_count = 0
        seen_health_pods: set[str] = set()
        for health in pod_health:
            seen_health_pods.add(health.pod.value)
            if health.pod.value == "pod_a" and supervisor.pods[health.pod].enabled:
                healthy_pod_count += 1 if runtime_pod_a_healthy else 0
                continue
            if health.pod.value == "pod_b" and (
                supervisor.pods[health.pod].enabled or pod_b_replacement_enabled
            ):
                healthy_pod_count += 1 if runtime_pod_b_healthy else 0
                continue
            if health.pod.value == "pod_c" and supervisor.pods[health.pod].enabled:
                healthy_pod_count += 1 if runtime_pod_c_healthy else 0
                continue
            healthy_pod_count += 1 if health.healthy else 0

        if pod_b_replacement_enabled and "pod_b" not in seen_health_pods:
            healthy_pod_count += 1 if runtime_pod_b_healthy else 0

        pod_a_report = pod_a_runtime.get("report", {}) if isinstance(pod_a_runtime, dict) else {}
        pod_b_report = pod_b_status.get("report", {}) if isinstance(pod_b_status, dict) else {}
        pod_c_report = pod_c_runtime.get("report", {}) if isinstance(pod_c_runtime, dict) else {}
        pod_b_runtime_pod = {}
        regime_transition_count = supervisor.state.regime_transition_count
        regime_evaluation_count = supervisor.state.regime_evaluation_count
        pod_a_preview_count = len(supervisor.state.pod_a_signal_preview)
        if isinstance(runtime_supervisor, dict):
            regime_transition_count = int(
                runtime_supervisor.get("regime_transition_count", regime_transition_count)
            )
            regime_evaluation_count = int(
                runtime_supervisor.get("regime_evaluation_count", regime_evaluation_count)
            )
            pod_a_preview_count = len(runtime_supervisor.get("pod_a_signal_preview", []))
            runtime_pods = runtime_supervisor.get("pods", {})
            if isinstance(runtime_pods, dict):
                pod_b_runtime_pod = (
                    runtime_pods.get("pod_b", {})
                    if isinstance(runtime_pods.get("pod_b", {}), dict)
                    else {}
                )
        if not hip4_enabled:
            pod_b_owned_symbols = []
        elif pod_b_replacement_enabled:
            pod_b_owned_symbols = _hip4_managed_symbols(pod_b_status)
        else:
            pod_b_owned_symbols = pod_b_runtime_pod.get("owned_symbols")
        if not isinstance(pod_b_owned_symbols, list):
            pod_b_owned_symbols = pod_b_status.get("managed_symbols")
        if not isinstance(pod_b_owned_symbols, list):
            pod_b_owned_symbols = [
                symbol for symbol in supervisor.registry.symbols_for(PodName.POD_B)
            ]
        pod_b_open_positions = pod_b_status.get("open_positions", []) if isinstance(pod_b_status, dict) else []
        if not isinstance(pod_b_open_positions, list):
            pod_b_open_positions = []
        self._metrics = {
            "trident_bootstrap_ready": 1,
            "enabled_pod_count": len(supervisor.state.enabled_pods)
            + (
                1
                if pod_b_replacement_enabled and PodName.POD_B not in supervisor.state.enabled_pods
                else 0
            ),
            "healthy_pod_count": healthy_pod_count,
            "ownership_conflict_count": len(supervisor.state.ownership_conflicts),
            "owned_symbol_count": sum(1 for item in symbol_ownership if item.owner is not None),
            "pod_a_preview_count": pod_a_preview_count,
            "pod_a_process_running": 1 if runtime_pod_a_healthy else 0,
            "pod_a_closed_trade_count": int(pod_a_report.get("closed_trade_count", 0)),
            "pod_a_realized_pnl_usd": float(pod_a_report.get("realized_pnl_usd", 0.0)),
            "pod_b_managed_symbol_count": len(pod_b_owned_symbols),
            "pod_b_preview_count": (
                len(
                    runtime_supervisor.get("pod_b_signal_preview", [])
                    if isinstance(runtime_supervisor, dict)
                    else supervisor.state.pod_b_signal_preview
                )
                if hip4_enabled
                else 0
            ),
            "pod_b_process_running": 1 if runtime_pod_b_healthy else 0,
            "pod_b_total_position_count": int(
                pod_b_status.get("total_position_count", len(pod_b_open_positions))
            ),
            "pod_b_total_open_order_count": int(pod_b_status.get("total_open_order_count", 0)),
            "pod_b_total_fill_count": int(
                pod_b_status.get("total_fill_count", pod_b_report.get("closed_trade_count", 0))
                if is_hip4_pod_b_replacement_runtime(pod_b_status)
                else pod_b_report.get("closed_trade_count", pod_b_status.get("total_fill_count", 0))
            ),
            "pod_b_realized_pnl_usd": float(
                pod_b_report.get("realized_pnl_usd", pod_b_status.get("realized_pnl_usd", 0.0))
            ),
            "pod_b_total_unrealized_pnl_usd": float(
                pod_b_status.get("total_unrealized_pnl_usd", 0.0)
            ),
            "pod_c_process_running": 1 if runtime_pod_c_healthy else 0,
            "pod_c_closed_trade_count": int(pod_c_report.get("closed_trade_count", 0)),
            "pod_c_realized_pnl_usd": float(pod_c_report.get("realized_pnl_usd", 0.0)),
            "regime_transition_count": regime_transition_count,
            "regime_evaluation_count": regime_evaluation_count,
        }

    def snapshot(self) -> dict[str, int | float]:
        return dict(self._metrics)

    def _pod_b_runtime_status(
        self,
        supervisor: TridentSupervisor,
    ) -> dict[str, object]:
        payload = load_runtime_status(Path("logs/pod_b_live_status.json"))
        if runtime_status_is_fresh(payload):
            return payload
        return supervisor.state.pod_b_status


def _hip4_managed_symbols(payload: dict[str, object]) -> list[str]:
    symbols = payload.get("managed_symbols")
    if isinstance(symbols, list) and symbols:
        return [str(symbol) for symbol in symbols]

    positions = payload.get("open_positions", [])
    if not isinstance(positions, list):
        return []
    underlyings = {
        str(position.get("underlying"))
        for position in positions
        if isinstance(position, dict) and position.get("underlying")
    }
    return sorted(underlyings)
