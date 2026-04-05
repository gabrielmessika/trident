from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.observability.metrics import MetricsRegistry
from app.trident.supervisor import TridentSupervisor


@dataclass(slots=True)
class PodRuntimeReport:
    pod: str
    enabled: bool
    healthy: bool
    owned_symbols: list[str]
    target_pct: float
    target_usd: float
    preview_count: int = 0
    process_state: str | None = None
    position_count: int = 0
    open_order_count: int = 0
    total_fill_count: int = 0
    realized_pnl_usd: float = 0.0
    total_unrealized_pnl_usd: float = 0.0


@dataclass(slots=True)
class MultiPodRuntimeReport:
    profile: str
    mode: str
    regime: str
    cash_usd: float
    total_target_usd: float
    enabled_pod_count: int
    healthy_pod_count: int
    ownership_conflict_count: int
    active_position_count: int
    active_open_order_count: int
    total_fill_count: int
    realized_pnl_usd: float
    total_unrealized_pnl_usd: float
    pods: list[PodRuntimeReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["pods"] = [asdict(pod) for pod in self.pods]
        return payload


def build_runtime_report(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry | None = None,
) -> MultiPodRuntimeReport:
    if metrics is not None:
        metrics.refresh_from_supervisor(supervisor)
    pod_health_by_name = {
        health.pod.value: health for health in supervisor.pod_health()
    }
    pod_b_status = supervisor.state.pod_b_status
    pod_reports: list[PodRuntimeReport] = []
    active_position_count = 0
    active_open_order_count = 0
    total_fill_count = 0
    realized_pnl_usd = 0.0
    total_unrealized_pnl_usd = 0.0

    for pod_name, pod in supervisor.pods.items():
        allocation = supervisor.capital_plan.pod_allocations[pod_name]
        health = pod_health_by_name.get(pod_name.value)
        report = PodRuntimeReport(
            pod=pod_name.value,
            enabled=pod.enabled,
            healthy=health.healthy if health is not None else False,
            owned_symbols=supervisor.registry.symbols_for(pod_name),
            target_pct=allocation.target_pct,
            target_usd=allocation.target_usd,
        )
        if pod_name.value == "pod_a":
            report.preview_count = len(supervisor.state.pod_a_signal_preview)
        if pod_name.value == "pod_c":
            report.preview_count = len(supervisor.state.pod_c_signal_preview)
        if pod_name.value == "pod_b":
            report.process_state = str(pod_b_status.get("process_state", "unknown"))
            report.position_count = int(pod_b_status.get("total_position_count", 0))
            report.open_order_count = int(pod_b_status.get("total_open_order_count", 0))
            report.total_fill_count = int(pod_b_status.get("total_fill_count", 0))
            report.realized_pnl_usd = float(pod_b_status.get("realized_pnl_usd", 0.0))
            report.total_unrealized_pnl_usd = float(
                pod_b_status.get("total_unrealized_pnl_usd", 0.0)
            )
        pod_reports.append(report)
        active_position_count += report.position_count
        active_open_order_count += report.open_order_count
        total_fill_count += report.total_fill_count
        realized_pnl_usd += report.realized_pnl_usd
        total_unrealized_pnl_usd += report.total_unrealized_pnl_usd

    return MultiPodRuntimeReport(
        profile=supervisor.profile,
        mode=supervisor.mode,
        regime=supervisor.state.regime.value,
        cash_usd=supervisor.capital_plan.cash_usd,
        total_target_usd=round(
            sum(allocation.target_usd for allocation in supervisor.capital_plan.pod_allocations.values()),
            4,
        ),
        enabled_pod_count=len(supervisor.state.enabled_pods),
        healthy_pod_count=sum(1 for pod in pod_reports if pod.healthy),
        ownership_conflict_count=len(supervisor.state.ownership_conflicts),
        active_position_count=active_position_count,
        active_open_order_count=active_open_order_count,
        total_fill_count=total_fill_count,
        realized_pnl_usd=round(realized_pnl_usd, 4),
        total_unrealized_pnl_usd=round(total_unrealized_pnl_usd, 4),
        pods=pod_reports,
    )


def build_cohabitation_summary(result: object) -> dict[str, object]:
    return {
        "records_processed": getattr(result, "records_processed", 0),
        "ownership_conflict_count": getattr(result, "ownership_conflict_count", 0),
        "no_symbol_overlap": getattr(result, "no_symbol_overlap", False),
        "total_realized_pnl_usd": round(
            float(getattr(result, "pod_a_realized_pnl_usd", 0.0))
            + float(getattr(result, "pod_b_realized_pnl_usd", 0.0)),
            4,
        ),
        "pods": {
            "pod_a": {
                "owned_symbols": getattr(result, "pod_a_owned_symbols", []),
                "signal_count": getattr(result, "pod_a_signal_count", 0),
                "accepted_count": getattr(result, "pod_a_accepted_count", 0),
                "opened_count": getattr(result, "pod_a_opened_count", 0),
                "closed_trade_count": getattr(result, "pod_a_closed_trade_count", 0),
                "realized_pnl_usd": getattr(result, "pod_a_realized_pnl_usd", 0.0),
            },
            "pod_b": {
                "owned_symbols": getattr(result, "pod_b_owned_symbols", []),
                "total_fill_count": getattr(result, "pod_b_total_fill_count", 0),
                "recent_fill_count": getattr(result, "pod_b_recent_fill_count", 0),
                "total_open_order_count": getattr(result, "pod_b_total_open_order_count", 0),
                "total_position_count": getattr(result, "pod_b_total_position_count", 0),
                "realized_pnl_usd": getattr(result, "pod_b_realized_pnl_usd", 0.0),
                "total_unrealized_pnl_usd": getattr(
                    result, "pod_b_total_unrealized_pnl_usd", 0.0
                ),
            },
        },
    }
