from __future__ import annotations

from pathlib import Path

from app.live.runtime_status import load_runtime_status, runtime_status_is_fresh
from app.observability.runtime_merge import merge_runtime_supervisor_snapshot
from app.trident.supervisor import TridentSupervisor


class MetricsRegistry:
    """Lightweight in-memory metrics derived from the supervisor snapshot."""

    def __init__(self) -> None:
        self._metrics: dict[str, int | float] = {"trident_bootstrap_ready": 1}

    def refresh_from_supervisor(self, supervisor: TridentSupervisor) -> None:
        pod_health = supervisor.pod_health()
        pod_b_status = self._pod_b_runtime_status(supervisor)
        symbol_ownership = supervisor.registry.snapshot()
        pod_a_runtime = load_runtime_status("logs/pod_a_live_status.json")
        pod_b_runtime = load_runtime_status("logs/pod_b_live_status.json")
        pod_c_runtime = load_runtime_status("logs/pod_c_live_status.json")
        runtime_supervisor = merge_runtime_supervisor_snapshot(
            pod_a_runtime,
            pod_b_runtime,
            pod_c_runtime,
        )

        runtime_pod_a_healthy = runtime_status_is_fresh(pod_a_runtime)
        runtime_pod_c_healthy = runtime_status_is_fresh(pod_c_runtime)
        runtime_pod_b_healthy = runtime_status_is_fresh(pod_b_status)
        healthy_pod_count = 0
        for health in pod_health:
            if health.pod.value == "pod_a" and supervisor.pods[health.pod].enabled:
                healthy_pod_count += 1 if runtime_pod_a_healthy else 0
                continue
            if health.pod.value == "pod_b" and supervisor.pods[health.pod].enabled:
                healthy_pod_count += 1 if runtime_pod_b_healthy else 0
                continue
            if health.pod.value == "pod_c" and supervisor.pods[health.pod].enabled:
                healthy_pod_count += 1 if runtime_pod_c_healthy else 0
                continue
            healthy_pod_count += 1 if health.healthy else 0

        pod_a_report = pod_a_runtime.get("report", {}) if isinstance(pod_a_runtime, dict) else {}
        pod_c_report = pod_c_runtime.get("report", {}) if isinstance(pod_c_runtime, dict) else {}
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
        self._metrics = {
            "trident_bootstrap_ready": 1,
            "enabled_pod_count": len(supervisor.state.enabled_pods),
            "healthy_pod_count": healthy_pod_count,
            "ownership_conflict_count": len(supervisor.state.ownership_conflicts),
            "owned_symbol_count": sum(1 for item in symbol_ownership if item.owner is not None),
            "pod_a_preview_count": pod_a_preview_count,
            "pod_a_process_running": 1 if runtime_pod_a_healthy else 0,
            "pod_a_closed_trade_count": int(pod_a_report.get("closed_trade_count", 0)),
            "pod_a_realized_pnl_usd": float(pod_a_report.get("realized_pnl_usd", 0.0)),
            "pod_b_managed_symbol_count": len(pod_b_status.get("managed_symbols", [])),
            "pod_b_preview_count": len(
                runtime_supervisor.get("pod_b_signal_preview", [])
                if isinstance(runtime_supervisor, dict)
                else supervisor.state.pod_b_signal_preview
            ),
            "pod_b_process_running": 1 if runtime_pod_b_healthy else 0,
            "pod_b_total_position_count": int(pod_b_status.get("total_position_count", 0)),
            "pod_b_total_open_order_count": 0,
            "pod_b_total_fill_count": int(pod_b_status.get("total_fill_count", 0)),
            "pod_b_realized_pnl_usd": float(pod_b_status.get("realized_pnl_usd", 0.0)),
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
        status_path = Path("logs/pod_b_live_status.json")
        if not status_path.exists():
            return supervisor.state.pod_b_status
        payload = load_runtime_status(status_path)
        if runtime_status_is_fresh(payload):
            return payload
        return supervisor.state.pod_b_status
