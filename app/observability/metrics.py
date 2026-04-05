from __future__ import annotations

from app.trident.supervisor import TridentSupervisor


class MetricsRegistry:
    """Lightweight in-memory metrics derived from the supervisor snapshot."""

    def __init__(self) -> None:
        self._metrics: dict[str, int | float] = {"trident_bootstrap_ready": 1}

    def refresh_from_supervisor(self, supervisor: TridentSupervisor) -> None:
        pod_health = supervisor.pod_health()
        pod_b_status = supervisor.state.pod_b_status
        symbol_ownership = supervisor.registry.snapshot()
        self._metrics = {
            "trident_bootstrap_ready": 1,
            "enabled_pod_count": len(supervisor.state.enabled_pods),
            "healthy_pod_count": sum(1 for health in pod_health if health.healthy),
            "ownership_conflict_count": len(supervisor.state.ownership_conflicts),
            "owned_symbol_count": sum(1 for item in symbol_ownership if item.owner is not None),
            "pod_a_preview_count": len(supervisor.state.pod_a_signal_preview),
            "pod_b_managed_symbol_count": len(pod_b_status.get("managed_symbols", [])),
            "pod_b_process_running": 1
            if pod_b_status.get("process_state") == "running"
            else 0,
            "pod_b_total_position_count": int(pod_b_status.get("total_position_count", 0)),
            "pod_b_total_open_order_count": int(
                pod_b_status.get("total_open_order_count", 0)
            ),
            "pod_b_total_fill_count": int(pod_b_status.get("total_fill_count", 0)),
            "pod_b_realized_pnl_usd": float(pod_b_status.get("realized_pnl_usd", 0.0)),
            "pod_b_total_unrealized_pnl_usd": float(
                pod_b_status.get("total_unrealized_pnl_usd", 0.0)
            ),
            "regime_transition_count": supervisor.state.regime_transition_count,
            "regime_evaluation_count": supervisor.state.regime_evaluation_count,
        }

    def snapshot(self) -> dict[str, int | float]:
        return dict(self._metrics)
