from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.live.runtime_status import load_runtime_status, runtime_status_is_fresh
from app.observability.runtime_merge import merge_runtime_supervisor_snapshot
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName

if TYPE_CHECKING:
    from app.observability.metrics import MetricsRegistry


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
class RuntimeServiceReport:
    service: str
    label: str
    enabled: bool
    healthy: bool
    process_state: str | None = None
    symbol_count: int = 0
    polls_completed: int = 0
    records_written: int = 0
    last_collected_at: str | None = None
    output_path: str | None = None
    comment: str | None = None


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
    enabled_service_count: int = 0
    healthy_service_count: int = 0
    pods: list[PodRuntimeReport] = field(default_factory=list)
    services: list[RuntimeServiceReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["pods"] = [asdict(pod) for pod in self.pods]
        payload["services"] = [asdict(service) for service in self.services]
        return payload


def _is_supervisor_fallback_runtime(payload: dict[str, object] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("last_sync_reason", "")) == "supervisor_planned_state"


def _display_process_state(payload: dict[str, object] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    if _is_supervisor_fallback_runtime(payload):
        return "supervisor_fallback"
    value = payload.get("process_state")
    return str(value) if value is not None else None


def build_runtime_report(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry | None = None,
    runtime_snapshot: dict[str, object] | None = None,
) -> MultiPodRuntimeReport:
    if metrics is not None:
        metrics.refresh_from_supervisor(supervisor)
    pod_a_runtime = load_runtime_status("logs/pod_a_live_status.json")
    pod_b_runtime = load_runtime_status("logs/pod_b_live_status.json")
    pod_c_runtime = load_runtime_status("logs/pod_c_live_status.json")
    runtime_supervisor = merge_runtime_supervisor_snapshot(
        pod_a_runtime,
        pod_b_runtime,
        pod_c_runtime,
        base_snapshot=runtime_snapshot if isinstance(runtime_snapshot, dict) else None,
    )
    pod_health_by_name = {
        health.pod.value: health for health in supervisor.pod_health()
    }
    pod_b_status = _pod_b_runtime_status(supervisor)
    if isinstance(runtime_snapshot, dict) and isinstance(runtime_snapshot.get("pod_b_status"), dict):
        pod_b_status = runtime_snapshot["pod_b_status"]
    runtime_pods = runtime_supervisor.get("pods", {}) if isinstance(runtime_supervisor, dict) else {}
    runtime_capital_plan = (
        runtime_supervisor.get("capital_plan", {}) if isinstance(runtime_supervisor, dict) else {}
    )
    pod_reports: list[PodRuntimeReport] = []
    service_reports = _runtime_service_reports(supervisor)
    active_position_count = 0
    active_open_order_count = 0
    total_fill_count = 0
    realized_pnl_usd = 0.0
    total_unrealized_pnl_usd = 0.0

    def _directional_open_position_metrics(
        runtime_payload: dict[str, object] | None,
    ) -> tuple[int, float]:
        if not isinstance(runtime_payload, dict):
            return 0, 0.0
        positions = runtime_payload.get("open_positions", [])
        if not isinstance(positions, list):
            return 0, 0.0
        position_count = 0
        unrealized_pnl_usd = 0.0
        for position in positions:
            if not isinstance(position, dict):
                continue
            position_count += 1
            unrealized_pnl_usd += float(position.get("unrealized_pnl_usd", 0.0))
        return position_count, round(unrealized_pnl_usd, 4)

    for pod_name, pod in supervisor.pods.items():
        runtime_pod_state = (
            runtime_pods.get(pod_name.value, {}) if isinstance(runtime_pods, dict) else {}
        )
        runtime_capital_pod = (
            runtime_capital_plan.get("pods", {}).get(pod_name.value, {})
            if isinstance(runtime_capital_plan.get("pods", {}), dict)
            else {}
        )
        allocation = supervisor.capital_plan.pod_allocations[pod_name]
        target_pct = float(runtime_pod_state.get("target_pct", allocation.target_pct))
        target_usd = float(runtime_pod_state.get("target_usd", allocation.target_usd))
        if isinstance(runtime_capital_pod, dict):
            target_pct = float(runtime_capital_pod.get("target_pct", target_pct))
            target_usd = float(runtime_capital_pod.get("target_usd", target_usd))
        owned_symbols = supervisor.registry.symbols_for(pod_name)
        runtime_owned_symbols = runtime_pod_state.get("owned_symbols")
        if isinstance(runtime_owned_symbols, list):
            owned_symbols = [str(symbol) for symbol in runtime_owned_symbols]
        health = pod_health_by_name.get(pod_name.value)
        report = PodRuntimeReport(
            pod=pod_name.value,
            enabled=pod.enabled,
            healthy=health.healthy if health is not None else False,
            owned_symbols=owned_symbols,
            target_pct=target_pct,
            target_usd=target_usd,
        )
        if pod_name.value == "pod_a":
            runtime_payload = pod_a_runtime if isinstance(pod_a_runtime, dict) else None
            runtime_report = runtime_payload.get("report", {}) if runtime_payload else {}
            if isinstance(runtime_supervisor, dict):
                report.preview_count = len(runtime_supervisor.get("pod_a_signal_preview", []))
            else:
                report.preview_count = len(supervisor.state.pod_a_signal_preview)
            if pod.enabled:
                report.healthy = runtime_status_is_fresh(runtime_payload)
            report.process_state = (
                str(runtime_payload.get("process_state", "running"))
                if runtime_payload is not None
                else None
            )
            report.position_count, report.total_unrealized_pnl_usd = _directional_open_position_metrics(
                runtime_payload
            )
            report.total_fill_count = int(runtime_report.get("closed_trade_count", 0))
            report.realized_pnl_usd = float(runtime_report.get("realized_pnl_usd", 0.0))
        if pod_name.value == "pod_c":
            runtime_payload = pod_c_runtime if isinstance(pod_c_runtime, dict) else None
            runtime_report = runtime_payload.get("report", {}) if runtime_payload else {}
            if isinstance(runtime_supervisor, dict):
                report.preview_count = len(runtime_supervisor.get("pod_c_signal_preview", []))
            else:
                report.preview_count = len(supervisor.state.pod_c_signal_preview)
            if pod.enabled:
                report.healthy = runtime_status_is_fresh(runtime_payload)
            report.process_state = (
                str(runtime_payload.get("process_state", "running"))
                if runtime_payload is not None
                else None
            )
            report.position_count, report.total_unrealized_pnl_usd = _directional_open_position_metrics(
                runtime_payload
            )
            report.total_fill_count = int(runtime_report.get("closed_trade_count", 0))
            report.realized_pnl_usd = float(runtime_report.get("realized_pnl_usd", 0.0))
        if pod_name.value == "pod_b":
            runtime_payload = pod_b_status if isinstance(pod_b_status, dict) else None
            runtime_report = runtime_payload.get("report", {}) if runtime_payload else {}
            if isinstance(runtime_supervisor, dict):
                report.preview_count = len(runtime_supervisor.get("pod_b_signal_preview", []))
            else:
                report.preview_count = len(supervisor.state.pod_b_signal_preview)
            if pod.enabled:
                report.healthy = (
                    runtime_status_is_fresh(runtime_payload)
                    and not _is_supervisor_fallback_runtime(runtime_payload)
                )
            report.process_state = _display_process_state(runtime_payload)
            report.position_count, report.total_unrealized_pnl_usd = _directional_open_position_metrics(
                runtime_payload
            )
            report.open_order_count = 0
            report.total_fill_count = int(runtime_report.get("closed_trade_count", 0))
            report.realized_pnl_usd = float(runtime_report.get("realized_pnl_usd", 0.0))
        pod_reports.append(report)
        active_position_count += report.position_count
        active_open_order_count += report.open_order_count
        total_fill_count += report.total_fill_count
        realized_pnl_usd += report.realized_pnl_usd
        total_unrealized_pnl_usd += report.total_unrealized_pnl_usd

    return MultiPodRuntimeReport(
        profile=supervisor.profile,
        mode=supervisor.mode,
        regime=(
            str(runtime_supervisor.get("regime"))
            if isinstance(runtime_supervisor, dict) and runtime_supervisor.get("regime") is not None
            else supervisor.state.regime.value
        ),
        cash_usd=float(runtime_capital_plan.get("cash_usd", supervisor.capital_plan.cash_usd)),
        total_target_usd=round(sum(report.target_usd for report in pod_reports), 4),
        enabled_pod_count=(
            len(runtime_supervisor.get("enabled_pods", []))
            if isinstance(runtime_supervisor, dict)
            and isinstance(runtime_supervisor.get("enabled_pods"), list)
            else len(supervisor.state.enabled_pods)
        ),
        healthy_pod_count=sum(1 for pod in pod_reports if pod.healthy),
        ownership_conflict_count=(
            len(runtime_supervisor.get("ownership_conflicts", []))
            if isinstance(runtime_supervisor, dict)
            and isinstance(runtime_supervisor.get("ownership_conflicts"), list)
            else len(supervisor.state.ownership_conflicts)
        ),
        active_position_count=active_position_count,
        active_open_order_count=active_open_order_count,
        total_fill_count=total_fill_count,
        realized_pnl_usd=round(realized_pnl_usd, 4),
        total_unrealized_pnl_usd=round(total_unrealized_pnl_usd, 4),
        enabled_service_count=sum(1 for service in service_reports if service.enabled),
        healthy_service_count=sum(
            1 for service in service_reports if service.enabled and service.healthy
        ),
        pods=pod_reports,
        services=service_reports,
    )


def _pod_b_runtime_status(supervisor: TridentSupervisor) -> dict[str, object]:
    status_path = Path("logs/pod_b_live_status.json")
    if not status_path.exists():
        return supervisor.state.pod_b_status
    payload = load_runtime_status(status_path)
    if runtime_status_is_fresh(payload):
        return payload
    return supervisor.state.pod_b_status


def _runtime_service_reports(supervisor: TridentSupervisor) -> list[RuntimeServiceReport]:
    return [
        _runtime_service_report(
            "logs/funding_collector_status.json",
            service="funding_collector",
            label="Funding Collector",
            enabled=True,
        ),
        _runtime_service_report(
            "logs/tradfi_funding_collector_status.json",
            service="tradfi_funding_collector",
            label="Tradfi Funding Collector",
            enabled=supervisor.pods[PodName.POD_C].enabled,
        ),
    ]


def _runtime_service_report(
    status_path: str | Path,
    *,
    service: str,
    label: str,
    enabled: bool,
) -> RuntimeServiceReport:
    if not enabled:
        return RuntimeServiceReport(
            service=service,
            label=label,
            enabled=False,
            healthy=False,
            process_state="disabled",
            output_path=None,
            comment="Collector désactivé.",
        )
    payload = load_runtime_status(status_path)
    if not isinstance(payload, dict):
        return RuntimeServiceReport(
            service=service,
            label=label,
            enabled=True,
            healthy=False,
            process_state="missing",
            output_path=None,
            comment="Runtime status absent.",
        )
    healthy = runtime_status_is_fresh(payload)
    polls_completed = int(payload.get("polls_completed", 0))
    records_written = int(payload.get("records_written", 0))
    process_state = str(payload.get("process_state") or "unknown")
    return RuntimeServiceReport(
        service=str(payload.get("service") or service),
        label=str(payload.get("label") or label),
        enabled=True,
        healthy=healthy,
        process_state=process_state,
        symbol_count=int(payload.get("symbol_count", 0)),
        polls_completed=polls_completed,
        records_written=records_written,
        last_collected_at=(
            str(payload.get("last_collected_at")) if payload.get("last_collected_at") else None
        ),
        output_path=str(payload.get("output_path")) if payload.get("output_path") else None,
        comment=(
            "Collector healthy."
            if healthy
            else "Runtime status stale."
            if payload.get("updated_at")
            else "Runtime status absent."
        ),
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
