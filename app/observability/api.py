from __future__ import annotations

import contextlib
import copy
import json
from collections import deque
from datetime import datetime, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from app.observability.metrics import MetricsRegistry
from app.version import VERSION
from app.reporting.multi_pod import build_runtime_report
from app.backtest.snapshot_loader import SnapshotLoader, SnapshotRecord
from app.live.runtime_status import (
    load_runtime_status,
    runtime_status_age_seconds,
    runtime_status_is_fresh,
)
from app.observability.runtime_merge import merge_runtime_supervisor_snapshot
from app.trident.market_clusters import (
    normalize_cluster_names,
    observation_universe_symbols,
    symbols_in_allowed_clusters,
)
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, SymbolMarketSnapshot


def _latest_snapshot_status(snapshot_dir: Path = Path("data/live_snapshots")) -> dict[str, object]:
    files = sorted(snapshot_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return {
            "status": "bad",
            "label": "No snapshots",
            "comment": "Aucun snapshot live trouvé.",
            "age_minutes": None,
            "path": None,
        }
    latest = files[0]
    age_minutes = (datetime.now(timezone.utc).timestamp() - latest.stat().st_mtime) / 60.0
    if age_minutes <= 2.0:
        status = "good"
        label = "Fresh snapshots"
    elif age_minutes <= 10.0:
        status = "warn"
        label = "Snapshots aging"
    else:
        status = "bad"
        label = "Snapshots stale"
    return {
        "status": status,
        "label": label,
        "comment": f"Dernier snapshot il y a {age_minutes:.1f} min dans {latest.name}.",
        "age_minutes": round(age_minutes, 2),
        "path": str(latest),
    }


def _tail_jsonl_records(
    path: Path,
    *,
    event_type: str | None = None,
    limit: int = 5,
    scan_lines: int = 250,
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=scan_lines)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            lines.append(line)
    records: list[dict[str, object]] = []
    for raw in reversed(lines):
        with contextlib.suppress(json.JSONDecodeError):
            record = json.loads(raw)
            if not isinstance(record, dict):
                continue
            if event_type is not None and record.get("event_type") != event_type:
                continue
            records.append(record)
            if len(records) >= limit:
                break
    return records


def _recent_activity_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for pod_name, log_name in (("pod_a", "pod_a_live.jsonl"), ("pod_c", "pod_c_live.jsonl")):
        for record in _tail_jsonl_records(Path("logs") / log_name, event_type="trade_close", limit=4):
            trade = record.get("trade", {})
            if not isinstance(trade, dict):
                continue
            rows.append(
                {
                    "timestamp": str(record.get("timestamp") or trade.get("closed_at") or "-"),
                    "pod": pod_name,
                    "symbol": str(trade.get("symbol", "-")),
                    "side": str(trade.get("side", "-")),
                    "event": "trade_close",
                    "price": trade.get("exit_price"),
                    "notional_usd": trade.get("target_notional_usd"),
                    "leverage": trade.get("leverage"),
                    "pnl_usd": trade.get("pnl_usd"),
                    "comment": str(trade.get("close_reason", "-")),
                }
            )

    pod_b_status = snapshot.get("pod_b_status", {})
    pod_b_leverage = None
    if isinstance(pod_b_status, dict):
        pod_b_leverage = pod_b_status.get("leverage")
        recent_fills = pod_b_status.get("recent_fills", [])
        if isinstance(recent_fills, list):
            for fill in reversed(recent_fills[-6:]):
                if not isinstance(fill, dict):
                    continue
                rows.append(
                    {
                        "timestamp": str(fill.get("timestamp") or "-"),
                        "pod": "pod_b",
                        "symbol": str(fill.get("symbol", "-")),
                        "side": str(fill.get("side", "-")),
                        "event": f"fill_{fill.get('action', 'unknown')}",
                        "price": fill.get("price"),
                        "notional_usd": fill.get("notional_usd"),
                        "leverage": pod_b_leverage,
                        "pnl_usd": None,
                        "comment": f"fee={float(fill.get('fee_usd', 0.0)):.4f}",
                    }
                )

    rows.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
    return rows[:10]


def _status_badge(status: str, label: str) -> str:
    return f'<span class="badge badge-{escape(status)}">{escape(label)}</span>'


def _table_header(label: str, tooltip: str) -> str:
    return (
        "<th>"
        "<span class='th-with-tooltip'>"
        f"<span>{escape(label)}</span>"
        "<button class='tooltip-trigger' type='button' aria-label='Afficher l’aide'>i</button>"
        f"<span class='tooltip-bubble'>{escape(tooltip)}</span>"
        "</span>"
        "</th>"
    )


def _format_leverage(value: object) -> str:
    if value in (None, "", 0, 0.0):
        return "-"
    try:
        return f"{float(value):.1f}x"
    except (TypeError, ValueError):
        return escape(str(value))


def _directional_price_from_bps(entry_price: object, side: object, bps: object) -> float | None:
    try:
        entry = float(entry_price)
        distance_bps = float(bps)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or distance_bps <= 0:
        return None
    ratio = distance_bps / 10_000.0
    if str(side) == "short":
        return round(entry * (1 - ratio), 8)
    return round(entry * (1 + ratio), 8)


def _directional_stop_price(item: dict[str, object]) -> float | None:
    invalidation = item.get("invalidation_price")
    if invalidation not in (None, ""):
        try:
            return round(float(invalidation), 8)
        except (TypeError, ValueError):
            pass
    return _directional_price_from_bps(
        item.get("entry_price"),
        "short" if str(item.get("side")) == "long" else "long",
        item.get("stop_bps"),
    )


def _directional_take_profit_price(item: dict[str, object]) -> float | None:
    return _directional_price_from_bps(
        item.get("entry_price"),
        item.get("side"),
        item.get("take_profit_bps"),
    )


def _directional_favorable_move_bps(entry_price: object, side: object, price: object) -> float | None:
    try:
        entry = float(entry_price)
        current = float(price)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    if str(side) == "short":
        return ((entry - current) / entry) * 10_000.0
    return ((current - entry) / entry) * 10_000.0


def _directional_trailing_stop_price(item: dict[str, object]) -> float | None:
    best_price_seen = item.get("best_price_seen")
    trailing_distance_bps = item.get("trailing_distance_bps")
    try:
        reference = float(best_price_seen)
        distance_bps = float(trailing_distance_bps)
    except (TypeError, ValueError):
        return None
    if reference <= 0 or distance_bps <= 0:
        return None
    ratio = distance_bps / 10_000.0
    if str(item.get("side")) == "short":
        return round(reference * (1 + ratio), 8)
    return round(reference * (1 - ratio), 8)


def _directional_trailing_status(item: dict[str, object]) -> str:
    activation_bps = item.get("trailing_activation_bps")
    trailing_distance_bps = item.get("trailing_distance_bps")
    if activation_bps in (None, "", 0, 0.0) or trailing_distance_bps in (None, "", 0, 0.0):
        return "Non configure"
    best_favorable_bps = _directional_favorable_move_bps(
        item.get("entry_price"),
        item.get("side"),
        item.get("best_price_seen"),
    )
    try:
        activation = float(activation_bps)
        distance = float(trailing_distance_bps)
    except (TypeError, ValueError):
        return "Non configure"
    if best_favorable_bps is None or best_favorable_bps < activation:
        return f"En attente, activation apres +{activation:.1f} bps"
    trailing_price = _directional_trailing_stop_price(item)
    if trailing_price is None:
        return f"Actif, distance {distance:.1f} bps"
    return f"Actif, stop suiveur {trailing_price:.6f}"


def _humanize_setup_reason(value: object) -> str:
    reason = str(value or "").strip()
    mapping = {
        "bos_retest_long": "BOS retest long: reprise haussiere apres retest du breakout",
        "bos_retest_short": "BOS retest short: reprise baissiere apres retest du breakout",
        "liquidity_sweep_reclaim_long": "Liquidity sweep reclaim long: reprise haussiere apres chasse de liquidite",
        "liquidity_sweep_reclaim_short": "Liquidity sweep reclaim short: reprise baissiere apres chasse de liquidite",
        "vwap_reclaim_long": "VWAP reclaim long: reprise haussiere apres recuperation de la VWAP",
        "vwap_reclaim_short": "VWAP reclaim short: reprise baissiere apres rejet sous la VWAP",
        "trend_pullback_long": "Trend pullback long: achat de repli dans une tendance haussiere",
        "trend_pullback_short": "Trend pullback short: vente de rebond dans une tendance baissiere",
        "tradfi_continuation_long": "Tradfi continuation long: continuation haussiere sur instrument Tradfi",
        "tradfi_continuation_short": "Tradfi continuation short: continuation baissiere sur instrument Tradfi",
        "tradfi_reclaim_long": "Tradfi reclaim long: reprise haussiere apres recapture du niveau cle",
        "tradfi_reclaim_short": "Tradfi reclaim short: reprise baissiere apres rejet sous le niveau cle",
    }
    return mapping.get(reason, reason or "-")


def _humanize_close_reason(value: object) -> str:
    reason = str(value or "").strip()
    mapping = {
        "take_profit_hit": "Take profit atteint: la cible de gain fixe a ete touchee",
        "trailing_stop": "Trailing stop declenche: le trade avait avance puis a retrace",
        "break_even_stop": "Sortie break-even: la protection est remontee a zero apres avance favorable",
        "stop_hit": "Stop loss touche: l'invalidation du trade a ete atteinte",
        "time_stop": "Time stop atteint: duree maximale de detention depassee",
        "routing_revoked": "Routing revoke: le symbole n'etait plus autorise pour ce pod",
        "opposite_signal": "Signal oppose: un signal inverse a force la fermeture",
        "upgrade_setup": "Upgrade setup: fermeture pour reouvrir sur un setup juge meilleur",
        "end_of_backtest": "Fin de replay: cloture technique de fin de session",
    }
    return mapping.get(reason, reason or "-")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _format_duration_compact(total_seconds: float) -> str:
    seconds = max(int(total_seconds), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}j {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _panel_tone(tone: object) -> str:
    value = str(tone or "neutral")
    if value in {"good", "warn", "bad", "neutral"}:
        return value
    return "neutral"


def _recent_directional_trade_rows(runtime_payload: dict[str, object] | None, *, pod: str) -> list[dict[str, object]]:
    if not isinstance(runtime_payload, dict):
        return []
    report = runtime_payload.get("report", {})
    if not isinstance(report, dict):
        return []
    closed_trade_log = report.get("closed_trade_log", [])
    if not isinstance(closed_trade_log, list):
        return []
    rows: list[dict[str, object]] = []
    for item in reversed(closed_trade_log[-12:]):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "timestamp": str(item.get("closed_at") or item.get("date") or "-"),
                "pod": pod,
                "symbol": str(item.get("symbol", "-")),
                "side": str(item.get("side", "-")),
                "status": "closed",
                "open_reason": str(item.get("setup") or item.get("open_reason") or "-"),
                "close_reason": str(item.get("close_reason") or "-"),
                "entry_price": item.get("entry_price"),
                "exit_price": item.get("exit_price"),
                "notional_usd": item.get("target_notional_usd"),
                "leverage": item.get("leverage"),
                "pnl_usd": item.get("pnl_usd"),
                "opened_at": item.get("opened_at"),
                "closed_at": item.get("closed_at"),
            }
        )
    return rows


def _open_position_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for pod_name in ("pod_a", "pod_c"):
        runtime_payload = snapshot.get(f"{pod_name}_runtime")
        if not isinstance(runtime_payload, dict):
            continue
        positions = runtime_payload.get("open_positions", [])
        if not isinstance(positions, list):
            continue
        for item in positions:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "pod": pod_name,
                    "symbol": str(item.get("symbol", "-")),
                    "side": str(item.get("side", "-")),
                    "status": "open",
                    "open_reason": str(item.get("open_reason") or item.get("setup") or "-"),
                    "close_reason": "-",
                    "entry_price": item.get("entry_price"),
                    "exit_price": None,
                    "notional_usd": item.get("target_notional_usd"),
                    "leverage": item.get("leverage"),
                    "pnl_usd": None,
                    "opened_at": item.get("opened_at"),
                    "closed_at": None,
                    "stop_bps": item.get("stop_bps"),
                    "time_stop_hours": item.get("time_stop_hours"),
                    "confidence": item.get("confidence"),
                }
            )

    pod_b_status = snapshot.get("pod_b_status", {})
    if isinstance(pod_b_status, dict):
        for item in pod_b_status.get("positions", []) if isinstance(pod_b_status.get("positions", []), list) else []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "pod": "pod_b",
                    "symbol": str(item.get("symbol", "-")),
                    "side": str(item.get("side", "-")),
                    "status": "open",
                    "open_reason": "maker inventory fill",
                    "close_reason": "-",
                    "entry_price": item.get("entry_price"),
                    "exit_price": None,
                    "notional_usd": item.get("notional_usd"),
                    "leverage": pod_b_status.get("leverage"),
                    "pnl_usd": item.get("unrealized_pnl_usd"),
                    "opened_at": pod_b_status.get("started_at"),
                    "closed_at": None,
                    "stop_bps": None,
                    "time_stop_hours": None,
                    "confidence": None,
                }
            )
    rows.sort(key=lambda item: (str(item.get("pod")), str(item.get("symbol"))))
    return rows


def _trade_event_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    rows.extend(_recent_directional_trade_rows(snapshot.get("pod_a_runtime"), pod="pod_a"))
    rows.extend(_recent_directional_trade_rows(snapshot.get("pod_c_runtime"), pod="pod_c"))

    pod_b_status = snapshot.get("pod_b_status", {})
    if isinstance(pod_b_status, dict):
        leverage = pod_b_status.get("leverage")
        recent_fills = pod_b_status.get("recent_fills", [])
        if isinstance(recent_fills, list):
            for fill in reversed(recent_fills[-20:]):
                if not isinstance(fill, dict):
                    continue
                rows.append(
                    {
                        "timestamp": str(fill.get("timestamp") or "-"),
                        "pod": "pod_b",
                        "symbol": str(fill.get("symbol", "-")),
                        "side": str(fill.get("side", "-")),
                        "status": "fill",
                        "open_reason": "maker quote fill",
                        "close_reason": "-",
                        "entry_price": fill.get("price"),
                        "exit_price": None,
                        "notional_usd": fill.get("notional_usd"),
                        "leverage": leverage,
                        "pnl_usd": None,
                        "opened_at": fill.get("timestamp"),
                        "closed_at": None,
                    }
                )
    rows.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return rows[:30]


def _dashboard_status_items(
    snapshot: dict[str, object],
    runtime_report: dict[str, object],
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    snapshot_status = _latest_snapshot_status()
    items.append(snapshot_status)

    healthy_count = int(runtime_report.get("healthy_pod_count", 0))
    enabled_count = int(runtime_report.get("enabled_pod_count", 0))
    if healthy_count == enabled_count:
        items.append(
            {
                "status": "good",
                "label": "Pods OK",
                "comment": f"{healthy_count}/{enabled_count} pod(s) OK.",
            }
        )
    elif healthy_count > 0:
        items.append(
            {
                "status": "warn",
                "label": "Pods à surveiller",
                "comment": f"{healthy_count}/{enabled_count} pod(s) OK.",
            }
        )
    else:
        items.append(
            {
                "status": "bad",
                "label": "Pods KO",
                "comment": f"0/{enabled_count} pod(s) OK.",
            }
        )

    healthy_collectors = int(runtime_report.get("healthy_service_count", 0))
    enabled_collectors = int(runtime_report.get("enabled_service_count", 0))
    if enabled_collectors > 0:
        if healthy_collectors == enabled_collectors:
            items.append(
                {
                    "status": "good",
                    "label": "Collectors OK",
                    "comment": f"{healthy_collectors}/{enabled_collectors} collector(s) OK.",
                }
            )
        elif healthy_collectors > 0:
            items.append(
                {
                    "status": "warn",
                    "label": "Collectors à surveiller",
                    "comment": f"{healthy_collectors}/{enabled_collectors} collector(s) OK.",
                }
            )
        else:
            items.append(
                {
                    "status": "bad",
                    "label": "Collectors KO",
                    "comment": f"0/{enabled_collectors} collector(s) OK.",
                }
            )

    conflicts = int(snapshot["metrics"]["ownership_conflict_count"])
    items.append(
        {
            "status": "good" if conflicts == 0 else "bad",
            "label": "Aucun conflit" if conflicts == 0 else "Conflit ownership",
            "comment": "Ownership propre." if conflicts == 0 else f"{conflicts} conflit(s) détecté(s).",
        }
    )

    fill_count = int(runtime_report.get("total_fill_count", 0))
    realized_pnl = float(runtime_report.get("realized_pnl_usd", 0.0))
    if fill_count > 0:
        items.append(
            {
                "status": "good",
                "label": "Activité visible",
                "comment": f"{fill_count} exécution(s), PnL réalisé {realized_pnl:.4f} USD.",
            }
        )
    else:
        items.append(
            {
                "status": "warn",
                "label": "Pas d'exécution",
                "comment": "Runtime actif, aucune exécution visible.",
            }
        )

    return items


def _dashboard_commentary(
    snapshot: dict[str, object],
    runtime_report: dict[str, object],
) -> str:
    conflicts = int(snapshot["metrics"]["ownership_conflict_count"])
    enabled = int(snapshot["metrics"]["enabled_pod_count"])
    healthy = int(runtime_report.get("healthy_pod_count", 0))
    collector_enabled = int(runtime_report.get("enabled_service_count", 0))
    collector_healthy = int(runtime_report.get("healthy_service_count", 0))
    fill_count = int(runtime_report.get("total_fill_count", 0))
    latest_snapshot = _latest_snapshot_status()
    if latest_snapshot["status"] == "bad":
        return "Collector en retard. Vérifie la collecte live et les logs API."
    if healthy < enabled:
        return "Un pod actif est à surveiller."
    if collector_enabled > collector_healthy:
        return "Un collector de données est à surveiller."
    if conflicts > 0:
        return "Conflit d'ownership détecté. Corriger avant d'augmenter le risque."
    if fill_count == 0:
        return "Runtime OK. Pas encore d'exécution visible."
    return "Runtime OK. Activité récente visible."


def health_payload(supervisor: TridentSupervisor) -> dict[str, object]:
    refreshed_from_snapshots = _refresh_supervisor_from_latest_snapshot(supervisor)
    regime = supervisor.state.regime.value
    if not refreshed_from_snapshots:
        runtime_supervisor = merge_runtime_supervisor_snapshot(
            _normalized_runtime_payload(load_runtime_status("logs/pod_a_live_status.json")),
            _normalized_runtime_payload(load_runtime_status("logs/pod_c_live_status.json")),
        )
        if isinstance(runtime_supervisor, dict) and runtime_supervisor.get("regime") is not None:
            regime = str(runtime_supervisor["regime"])
    return {
        "status": "ok",
        "version": VERSION,
        "profile": supervisor.profile,
        "mode": supervisor.mode,
        "regime": regime,
        "kill_switch_active": supervisor.kill_switch.is_active,
    }


def state_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, object]:
    refreshed_from_snapshots = _refresh_supervisor_from_latest_snapshot(supervisor)
    supervisor.sync_pod_b()
    metrics.refresh_from_supervisor(supervisor)
    snapshot = supervisor.snapshot()
    snapshot = _merge_runtime_snapshot(
        snapshot,
        allow_runtime_authority_override=not refreshed_from_snapshots,
    )
    snapshot["metrics"] = metrics.snapshot()
    snapshot["runtime_report"] = build_runtime_report(
        supervisor,
        metrics,
        runtime_snapshot=snapshot,
    ).to_dict()
    return snapshot


def metrics_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, int | float]:
    _refresh_supervisor_from_latest_snapshot(supervisor)
    supervisor.sync_pod_b()
    metrics.refresh_from_supervisor(supervisor)
    return metrics.snapshot()


def report_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, object]:
    refreshed_from_snapshots = _refresh_supervisor_from_latest_snapshot(supervisor)
    supervisor.sync_pod_b()
    snapshot = _merge_runtime_snapshot(
        supervisor.snapshot(),
        allow_runtime_authority_override=not refreshed_from_snapshots,
    )
    return build_runtime_report(supervisor, metrics, runtime_snapshot=snapshot).to_dict()


def _merge_runtime_snapshot(
    snapshot: dict[str, object],
    *,
    allow_runtime_authority_override: bool = True,
) -> dict[str, object]:
    pod_a_runtime = _normalized_runtime_payload(load_runtime_status("logs/pod_a_live_status.json"))
    pod_c_runtime = _normalized_runtime_payload(load_runtime_status("logs/pod_c_live_status.json"))
    pod_b_runtime = None
    pod_b_status_path = Path(_pod_b_status_path_from_snapshot(snapshot))
    if pod_b_status_path.exists():
        pod_b_runtime = _normalized_runtime_payload(load_runtime_status(pod_b_status_path))
    snapshot["pod_a_runtime"] = pod_a_runtime
    snapshot["pod_c_runtime"] = pod_c_runtime
    if isinstance(pod_b_runtime, dict):
        snapshot["pod_b_status"] = pod_b_runtime
    if isinstance(snapshot.get("pod_health"), list):
        merged_health: list[dict[str, object]] = []
        for health in snapshot["pod_health"]:
            if not isinstance(health, dict):
                merged_health.append(health)
                continue
            merged = dict(health)
            pod_name = merged.get("pod")
            if pod_name == "pod_a" and snapshot["pods"]["pod_a"]["enabled"]:
                merged["healthy"] = runtime_status_is_fresh(pod_a_runtime)
                merged["message"] = (
                    "runtime status fresh"
                    if merged["healthy"]
                    else "runtime status missing or stale"
                )
            elif pod_name == "pod_b" and snapshot["pods"]["pod_b"]["enabled"]:
                pod_b_status = snapshot.get("pod_b_status", {})
                merged["healthy"] = runtime_status_is_fresh(
                    pod_b_status if isinstance(pod_b_status, dict) else None
                )
                merged["message"] = (
                    "runtime status fresh"
                    if merged["healthy"]
                    else "runtime status missing or stale"
                )
            elif pod_name == "pod_c" and snapshot["pods"]["pod_c"]["enabled"]:
                merged["healthy"] = runtime_status_is_fresh(pod_c_runtime)
                merged["message"] = (
                    "runtime status fresh"
                    if merged["healthy"]
                    else "runtime status missing or stale"
                )
            merged_health.append(merged)
        snapshot["pod_health"] = merged_health

    runtime_supervisor = merge_runtime_supervisor_snapshot(
        pod_a_runtime,
        pod_c_runtime,
        base_snapshot=snapshot,
    )
    if not allow_runtime_authority_override or not isinstance(runtime_supervisor, dict):
        return snapshot

    for key in (
        "enabled_pods",
        "regime",
        "raw_regime",
        "symbol_ownership",
        "ownership_conflicts",
        "symbol_routing",
        "local_regime_by_symbol",
        "local_regime_transitions",
        "symbol_reassignment_count_by_symbol",
        "routing_overrides",
        "pods",
        "allocations",
        "capital_plan",
        "regime_snapshot",
        "pending_regime",
        "pending_regime_count",
        "regime_transition_count",
        "regime_evaluation_count",
        "regime_history",
        "pod_a_signal_preview",
        "pod_c_signal_preview",
    ):
        if key in runtime_supervisor:
            snapshot[key] = runtime_supervisor[key]

    embedded_supervisor = _embedded_supervisor_snapshot(snapshot)
    if isinstance(pod_a_runtime, dict):
        pod_a_runtime["supervisor"] = copy.deepcopy(embedded_supervisor)
    if isinstance(pod_c_runtime, dict):
        pod_c_runtime["supervisor"] = copy.deepcopy(embedded_supervisor)
    return snapshot


def _pod_b_status_path_from_snapshot(snapshot: dict[str, object]) -> str:
    pod_b_status = snapshot.get("pod_b_status", {})
    if isinstance(pod_b_status, dict):
        status_path = pod_b_status.get("status_path")
        if isinstance(status_path, str) and status_path:
            return status_path
    return "runtime/passivbot/live.status.json"


def _normalized_runtime_payload(payload: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    return copy.deepcopy(payload)

def _embedded_supervisor_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in (
        "profile",
        "mode",
        "regime",
        "raw_regime",
        "kill_switch",
        "enabled_pods",
        "pod_health",
        "symbol_ownership",
        "ownership_conflicts",
        "symbol_routing",
        "local_regime_by_symbol",
        "local_regime_transitions",
        "symbol_reassignment_count_by_symbol",
        "routing_overrides",
        "pods",
        "allocations",
        "capital_plan",
        "regime_snapshot",
        "pending_regime",
        "pending_regime_count",
        "regime_transition_count",
        "regime_evaluation_count",
        "regime_history",
        "pod_a_signal_preview",
        "pod_c_signal_preview",
    ):
        if key in snapshot:
            payload[key] = copy.deepcopy(snapshot[key])
    payload["source"] = "api_merged_runtime_view"
    return payload


def _refresh_supervisor_from_latest_snapshot(
    supervisor: TridentSupervisor,
    *,
    max_snapshot_age_seconds: float = 180.0,
) -> bool:
    record = _latest_snapshot_record(
        snapshot_dir=Path(supervisor.config.hyperliquid.snapshot_output_dir),
        max_snapshot_age_seconds=max_snapshot_age_seconds,
    )
    if record is None:
        return False
    supervisor.apply_regime_snapshot(
        RegimeSnapshot(**record.regime_snapshot),
        cluster_regime_snapshots={
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        },
    )
    supervisor.refresh_symbol_routing(
        [
            SymbolMarketSnapshot(**item)
            for item in record.symbols
            if isinstance(item, dict)
        ]
    )
    return True


def _latest_snapshot_record(
    *,
    snapshot_dir: Path,
    max_snapshot_age_seconds: float,
) -> SnapshotRecord | None:
    files = sorted(snapshot_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return None
    latest_file = files[0]
    age_seconds = max(
        datetime.now(timezone.utc).timestamp() - latest_file.stat().st_mtime,
        0.0,
    )
    if age_seconds > max_snapshot_age_seconds:
        return None
    loader = SnapshotLoader()
    latest_record: SnapshotRecord | None = None
    for record in loader.iter_merged_jsonl(latest_file):
        latest_record = record
    return latest_record


def _pod_label(pod_name: str) -> str:
    return {
        "pod_a": "Pod A",
        "pod_b": "Pod B",
        "pod_c": "Pod C",
    }.get(pod_name, pod_name.replace("_", " ").title())


def _control_center_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
    *,
    active_tab: str,
    title: str,
    subtitle: str,
) -> str:
    snapshot = state_payload(supervisor, metrics)
    runtime_report = report_payload(supervisor, metrics)
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    started_at = _parse_timestamp(snapshot.get("started_at"))
    uptime_label = (
        _format_duration_compact((datetime.now(timezone.utc) - started_at).total_seconds())
        if started_at is not None
        else "-"
    )
    refresh_seconds = 10
    status_items = _dashboard_status_items(snapshot, runtime_report)
    commentary = _dashboard_commentary(snapshot, runtime_report)
    recent_activity = _recent_activity_rows(snapshot)
    open_rows = _open_position_rows(snapshot)
    event_rows = _trade_event_rows(snapshot)
    pod_b_status = snapshot.get("pod_b_status", {})
    health_map = {
        str(item.get("pod")): item
        for item in snapshot.get("pod_health", [])
        if isinstance(item, dict) and item.get("pod") is not None
    }
    runtime_pod_map = {
        str(item.get("pod")): item
        for item in runtime_report.get("pods", [])
        if isinstance(item, dict) and item.get("pod") is not None
    }
    runtime_service_rows = [
        item
        for item in runtime_report.get("services", [])
        if isinstance(item, dict) and item.get("service") is not None
    ]
    pod_c_allowed_clusters = sorted(
        normalize_cluster_names(supervisor.config.pod_c.allowed_market_clusters)
    )
    pod_c_scope_symbols = symbols_in_allowed_clusters(
        supervisor.config,
        observation_universe_symbols(supervisor.config),
        supervisor.config.pod_c.allowed_market_clusters,
    )
    pod_c_scope_symbol_set = set(pod_c_scope_symbols)
    observed_status_rows = [
        item
        for item in snapshot.get("observed_symbol_status", [])
        if isinstance(item, dict) and item.get("symbol") is not None
    ]
    observed_symbols = {
        str(item.get("symbol")).upper()
        for item in observed_status_rows
    }
    tradable_symbols = {
        str(item.get("symbol")).upper()
        for item in observed_status_rows
        if bool(item.get("tradable"))
    }
    routing_rows = [
        item
        for item in snapshot.get("symbol_routing", [])
        if isinstance(item, dict) and item.get("symbol") is not None
    ]
    pod_c_routing_rows = [
        item
        for item in routing_rows
        if str(item.get("symbol")).upper() in pod_c_scope_symbol_set
    ]
    pod_c_routed_symbols = [
        str(item.get("symbol")).upper()
        for item in pod_c_routing_rows
        if item.get("owner") == "pod_c"
    ]
    pod_c_seen_not_observed = [
        symbol for symbol in pod_c_scope_symbols if symbol not in observed_symbols
    ]
    pod_c_observed_not_tradable = [
        symbol for symbol in pod_c_scope_symbols if symbol in observed_symbols and symbol not in tradable_symbols
    ]

    def fmt_number(value: object, digits: int = 2, *, fallback: str = "-") -> str:
        if value in (None, ""):
            return fallback
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return escape(str(value))

    def fmt_signed_usd(value: object, digits: int = 4) -> str:
        if value in (None, ""):
            return "-"
        try:
            return f"{float(value):+.{digits}f}"
        except (TypeError, ValueError):
            return escape(str(value))

    def render_stat_cards(cards: list[dict[str, str]]) -> str:
        return "".join(
            (
                "<article class='metric-card'>"
                f"<span>{escape(str(card['label']))}</span>"
                f"<strong>{escape(str(card['value']))}</strong>"
                f"<small>{escape(str(card['note']))}</small>"
                "</article>"
            )
            for card in cards
        )

    def render_preview_list(items: object) -> str:
        if not isinstance(items, list) or not items:
            return "<p class='soft-note'>Aucun signal en attente pour le moment.</p>"
        rows = []
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            symbol = escape(str(item.get("symbol", "-")))
            side = escape(str(item.get("side", ""))).upper()
            setup = escape(str(item.get("setup", "")))
            confidence = (
                f"{float(item['confidence']):.2f}"
                if item.get("confidence") not in (None, "")
                else "-"
            )
            parts = [symbol]
            if side:
                parts.append(side)
            if setup:
                parts.append(setup)
            if confidence != "-":
                parts.append(f"conf {confidence}")
            rows.append(f"<li>{' · '.join(parts)}</li>")
        if not rows:
            return "<p class='soft-note'>Aucun signal en attente pour le moment.</p>"
        return f"<ul class='simple-list'>{''.join(rows)}</ul>"

    def pod_summary(pod_name: str) -> dict[str, object]:
        pod_cfg = snapshot["pods"].get(pod_name, {})
        pod_report = runtime_pod_map.get(pod_name, {})
        pod_health = health_map.get(pod_name, {})
        enabled = bool(pod_cfg.get("enabled", pod_report.get("enabled", False)))
        healthy = bool(pod_report.get("healthy", pod_health.get("healthy", False)))
        if not enabled:
            tone = "neutral"
            badge = "Off"
            comment = "Pod coupé."
        elif healthy:
            tone = "good"
            badge = "OK"
            comment = "Pod OK."
        elif str(pod_report.get("process_state", "")) in {"running", "completed"}:
            tone = "warn"
            badge = "Check"
            comment = str(
                pod_health.get("message")
                or "Pod à vérifier."
            )
        else:
            tone = "bad"
            badge = "KO"
            comment = str(
                pod_health.get("message")
                or "Statut runtime absent ou obsolète."
            )
        if enabled and pod_name == "pod_b":
            if int(pod_report.get("open_order_count", 0)) > 0:
                comment = (
                    f"{len(pod_report.get('owned_symbols', []))} symbole(s), "
                    f"{int(pod_report.get('open_order_count', 0))} ordre(s) maker."
                )
            elif int(pod_report.get("total_fill_count", 0)) > 0:
                comment = (
                    f"{int(pod_report.get('total_fill_count', 0))} fill(s), "
                    "inventory en nettoyage."
                )
        if enabled and pod_name in {"pod_a", "pod_c"}:
            if int(pod_report.get("position_count", 0)) > 0:
                comment = f"{int(pod_report.get('position_count', 0))} position(s) ouverte(s)."
            elif int(pod_report.get("preview_count", 0)) > 0:
                comment = (
                    f"{int(pod_report.get('preview_count', 0))} signal(aux), "
                    "0 position."
                )
        return {
            "label": _pod_label(pod_name),
            "tone": tone,
            "badge": badge,
            "comment": comment,
            "enabled": enabled,
            "healthy": healthy,
            "owned_symbols": pod_report.get("owned_symbols", pod_cfg.get("owned_symbols", [])),
            "target_pct": pod_report.get("target_pct", pod_cfg.get("target_pct", 0.0)),
            "target_usd": pod_report.get("target_usd", pod_cfg.get("target_usd", 0.0)),
            "preview_count": int(pod_report.get("preview_count", 0)),
            "process_state": str(pod_report.get("process_state") or "-"),
            "position_count": int(pod_report.get("position_count", 0)),
            "open_order_count": int(pod_report.get("open_order_count", 0)),
            "total_fill_count": int(pod_report.get("total_fill_count", 0)),
            "realized_pnl_usd": float(pod_report.get("realized_pnl_usd", 0.0)),
            "total_unrealized_pnl_usd": float(pod_report.get("total_unrealized_pnl_usd", 0.0)),
        }

    pod_a_summary = pod_summary("pod_a")
    pod_b_summary = pod_summary("pod_b")
    pod_c_summary = pod_summary("pod_c")
    pod_summaries = (pod_a_summary, pod_b_summary, pod_c_summary)

    latest_snapshot = _latest_snapshot_status()
    enabled_count = int(runtime_report.get("enabled_pod_count", 0))
    healthy_count = int(runtime_report.get("healthy_pod_count", 0))
    conflict_count = int(snapshot["metrics"]["ownership_conflict_count"])
    active_positions = int(runtime_report.get("active_position_count", 0))
    active_orders = int(runtime_report.get("active_open_order_count", 0))
    total_fills = int(runtime_report.get("total_fill_count", 0))
    enabled_collectors = int(runtime_report.get("enabled_service_count", 0))
    healthy_collectors = int(runtime_report.get("healthy_service_count", 0))
    if latest_snapshot["status"] == "bad" or conflict_count > 0:
        global_tone = "bad"
        global_label = "Agir"
    elif healthy_count < enabled_count:
        global_tone = "warn"
        global_label = "Surveiller"
    else:
        global_tone = "good"
        global_label = "OK"

    focus_items: list[dict[str, str]] = []
    if latest_snapshot["status"] == "bad":
        focus_items.append(
                {
                    "tone": "bad",
                    "label": "Maintenant",
                    "title": "Collector en retard",
                    "comment": str(latest_snapshot["comment"]),
            }
        )
    if conflict_count > 0:
        focus_items.append(
                {
                    "tone": "bad",
                    "label": "Maintenant",
                    "title": "Corriger l'ownership",
                    "comment": f"{conflict_count} conflit(s) détecté(s) entre pods.",
                }
            )
    for service in runtime_service_rows:
        if bool(service.get("enabled", True)) and not bool(service.get("healthy")):
            focus_items.append(
                {
                    "tone": "warn",
                    "label": "Vérifier",
                    "title": f"{service.get('label', service.get('service', 'collector'))} à vérifier",
                    "comment": str(service.get("comment") or "Runtime status collector absent ou obsolète."),
                }
            )
    for pod in pod_summaries:
        if bool(pod["enabled"]) and str(pod["tone"]) in {"warn", "bad"}:
            focus_items.append(
                {
                    "tone": str(pod["tone"]),
                    "label": "Vérifier",
                    "title": f"{pod['label']} à vérifier",
                    "comment": str(pod["comment"]),
                }
            )
    if not focus_items and total_fills == 0:
        focus_items.append(
            {
                "tone": "good",
                "label": "RAS",
                "title": "Aucune urgence",
                "comment": "Pas d'exécution visible, mais aucun incident runtime.",
            }
        )
    if not focus_items:
        focus_items.append(
            {
                "tone": "good",
                "label": "RAS",
                "title": "Runtime stable",
                "comment": "Statuts frais et activité visible.",
            }
        )
    focus_tone = "good"
    if any(str(item["tone"]) == "bad" for item in focus_items):
        focus_tone = "bad"
    elif any(str(item["tone"]) == "warn" for item in focus_items):
        focus_tone = "warn"

    status_rows = "".join(
        (
            f"<article class='status-card status-card-{escape(str(item['status']))}'>"
            f"<div class='status-head'>{_status_badge(str(item['status']), str(item['label']))}</div>"
            f"<p>{escape(str(item['comment']))}</p>"
            "</article>"
        )
        for item in status_items
    )
    focus_rows = "".join(
        (
            "<article class='focus-item'>"
            f"<div class='focus-item-top'><span class='dot dot-{escape(str(item['tone']))}'></span>"
            f"<div><strong>{escape(str(item['title']))}</strong>"
            f"<small>{escape(str(item['comment']))}</small></div></div>"
            f"<span class='focus-tag focus-tag-{escape(str(item['tone']))}'>{escape(str(item['label']))}</span>"
            "</article>"
        )
        for item in focus_items[:4]
    )
    summary_cards = render_stat_cards(
        [
            {
                "label": "Régime",
                "value": str(snapshot["regime"]),
                "note": "État de marché courant",
            },
            {
                "label": "Pods",
                "value": f"{int(runtime_report['healthy_pod_count'])}/{int(runtime_report['enabled_pod_count'])}",
                "note": "Pods sains sur pods actifs",
            },
            {
                "label": "Risque live",
                "value": f"{active_positions} pos · {active_orders} ordres",
                "note": "Exposition visible maintenant",
            },
            {
                "label": "Collectors",
                "value": f"{healthy_collectors}/{enabled_collectors}",
                "note": "Services funding visibles",
            },
            {
                "label": "Exécutions",
                "value": str(total_fills),
                "note": "Fills / trades observés",
            },
            {
                "label": "Realized PnL",
                "value": f"{float(runtime_report['realized_pnl_usd']):.4f} USD",
                "note": "Cumul runtime visible",
            },
            {
                "label": "Cash",
                "value": f"{float(snapshot['capital_plan']['cash_usd']):.2f} USD",
                "note": "Capital non déployé",
            },
        ]
    )

    pod_cards = "".join(
        (
            f"<article class='pod-card pod-card-{escape(str(pod['tone']))}'>"
            f"<div class='pod-card-head'><span class='dot dot-{pod['tone']}'></span>"
            f"<div><h3>{escape(str(pod['label']))}</h3><p>{escape(str(pod['comment']))}</p></div></div>"
            f"<div class='pod-card-meta'>{_status_badge(str(pod['tone']), str(pod['badge']))}"
            f"<span class='meta-chip'>Process {escape(str(pod['process_state']))}</span></div>"
            "<dl class='pod-facts'>"
            f"<div><dt>Symbols</dt><dd>{', '.join(escape(str(symbol)) for symbol in pod['owned_symbols']) or '-'}</dd></div>"
            f"<div><dt>Allocation</dt><dd>{float(pod['target_pct']):.2f} · {float(pod['target_usd']):.2f} USD</dd></div>"
            f"<div><dt>Ouvert</dt><dd>{int(pod['position_count'])} pos · {int(pod['open_order_count'])} ordres</dd></div>"
            f"<div><dt>Exécution</dt><dd>{int(pod['total_fill_count'])} exec · {float(pod['realized_pnl_usd']):.4f} USD</dd></div>"
            "</dl>"
            f"<button class='tab-link' type='button' data-jump-tab='{escape(str(pod['label']).lower().replace(' ', '_'))}'>Voir l'onglet {escape(str(pod['label']))}</button>"
            "</article>"
        )
        for pod in (pod_a_summary, pod_b_summary, pod_c_summary)
    )

    def render_directional_open_rows(pod_name: str) -> str:
        rows = [
            item
            for item in open_rows
            if str(item.get("pod")) == pod_name and str(item.get("status")) == "open"
        ]
        if not rows:
            return "<tr><td colspan='12'>Aucune position ouverte visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('open_reason')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_price'), 6)}</td>"
                f"<td>{'-' if item.get('current_notional_usd') is None else format(float(item.get('current_notional_usd', 0.0)), '.2f')}</td>"
                f"<td>{'-' if item.get('margin_usd') is None else format(float(item.get('margin_usd', 0.0)), '.2f')}</td>"
                f"<td>{fmt_number(_directional_take_profit_price(item), 6)}</td>"
                f"<td>{fmt_number(_directional_stop_price(item), 6)}</td>"
                f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
                f"<td>{escape(_directional_trailing_status(item))}</td>"
                f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def render_directional_closed_rows(pod_name: str) -> str:
        rows = [
            item
            for item in event_rows
            if str(item.get("pod")) == pod_name and str(item.get("status")) == "closed"
        ]
        if not rows:
            return "<tr><td colspan='10'>Aucun trade fermé visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('closed_at') or item.get('timestamp') or '-'))}</td>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('open_reason')))}</td>"
                f"<td>{escape(_humanize_close_reason(item.get('close_reason')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('exit_price'), 6)}</td>"
                f"<td>{'-' if item.get('notional_usd') is None else format(float(item.get('notional_usd', 0.0)), '.2f')}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def render_activity_open_rows() -> str:
        if not open_rows:
            return "<tr><td colspan='11'>Aucune position ouverte visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr data-filter-status='open' "
                f"data-filter-pod='{escape(str(item['pod']))}'>"
                f"<td>{escape(str(item['pod']))}</td>"
                f"<td>{escape(str(item['symbol']))}</td>"
                f"<td>{escape(str(item['side']))}</td>"
                f"<td>{escape(str(item['open_reason']))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{'-' if item.get('notional_usd') is None else format(float(item.get('notional_usd', 0.0)), '.2f')}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_number(item.get('confidence'), 2)}</td>"
                f"<td>{fmt_number(item.get('stop_bps'), 1)}</td>"
                f"<td>{escape(str(item.get('time_stop_hours') or '-'))}</td>"
                f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for item in open_rows
        )

    def render_activity_event_rows() -> str:
        if not event_rows:
            return "<tr><td colspan='12'>Aucun évènement de trade récent visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr "
                f"data-filter-status=\"{'closed' if str(item.get('status')) == 'closed' else 'open'}\" "
                f"data-filter-pod='{escape(str(item['pod']))}'>"
                f"<td>{escape(str(item.get('timestamp') or '-'))}</td>"
                f"<td>{escape(str(item['pod']))}</td>"
                f"<td>{escape(str(item['symbol']))}</td>"
                f"<td>{escape(str(item['side']))}</td>"
                f"<td>{escape(str(item['status']))}</td>"
                f"<td>{escape(str(item.get('open_reason') or '-'))}</td>"
                f"<td>{escape(str(item.get('close_reason') or '-'))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('exit_price'), 6)}</td>"
                f"<td>{'-' if item.get('notional_usd') is None else format(float(item.get('notional_usd', 0.0)), '.2f')}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
                "</tr>"
            )
            for item in event_rows
        )

    recent_activity_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['timestamp']))}</td>"
            f"<td>{escape(str(item['pod']))}</td>"
            f"<td>{escape(str(item['symbol']))}</td>"
            f"<td>{escape(str(item['side']))}</td>"
            f"<td>{escape(str(item['event']))}</td>"
            f"<td>{fmt_number(item.get('price'), 4)}</td>"
            f"<td>{'-' if item['notional_usd'] is None else format(float(item.get('notional_usd', 0.0)), '.2f')}</td>"
            f"<td>{_format_leverage(item.get('leverage'))}</td>"
            f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
            f"<td>{escape(str(item['comment']))}</td>"
            "</tr>"
        )
        for item in recent_activity
    )
    if not recent_activity_rows:
        recent_activity_rows = (
            "<tr><td colspan='10'>Aucune exécution récente visible. "
            "Les trades apparaîtront ici dès qu'un pod écrira un trade close ou un fill récent.</td></tr>"
        )

    pod_b_positions_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('symbol', '-')))}</td>"
            f"<td>{escape(str(item.get('side', '-')))}</td>"
            f"<td>{fmt_number(item.get('size'), 6)}</td>"
            f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
            f"<td>{fmt_number(item.get('mark_price'), 6)}</td>"
            f"<td>{fmt_number(item.get('notional_usd'), 2)}</td>"
            f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
            "</tr>"
        )
        for item in (
            pod_b_status.get("positions", []) if isinstance(pod_b_status, dict) else []
        )
        if isinstance(item, dict)
    )
    if not pod_b_positions_rows:
        pod_b_positions_rows = "<tr><td colspan='7'>Aucune position d'inventory ouverte.</td></tr>"

    pod_b_orders_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('symbol', '-')))}</td>"
            f"<td>{escape(str(item.get('side', '-')))}</td>"
            f"<td>{fmt_number(item.get('price'), 6)}</td>"
            f"<td>{fmt_number(item.get('size'), 6)}</td>"
            f"<td>{escape(str(item.get('order_type', '-')))}</td>"
            f"<td>{escape(str(item.get('status', '-')))}</td>"
            "</tr>"
        )
        for item in (
            pod_b_status.get("open_orders", []) if isinstance(pod_b_status, dict) else []
        )
        if isinstance(item, dict)
    )
    if not pod_b_orders_rows:
        pod_b_orders_rows = "<tr><td colspan='6'>Aucun ordre maker ouvert pour le moment.</td></tr>"

    pod_b_inventory_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('symbol', '-')))}</td>"
            f"<td>{fmt_number(item.get('target_notional_usd'), 2)}</td>"
            f"<td>{fmt_number(item.get('current_notional_usd'), 2)}</td>"
            f"<td>{fmt_number(item.get('inventory_skew_pct'), 2)}</td>"
            f"<td>{'oui' if bool(item.get('has_position')) else 'non'}</td>"
            f"<td>{escape(str(item.get('open_order_count', '-')))}</td>"
            "</tr>"
        )
        for item in (
            pod_b_status.get("inventory", []) if isinstance(pod_b_status, dict) else []
        )
        if isinstance(item, dict)
    )
    if not pod_b_inventory_rows:
        pod_b_inventory_rows = "<tr><td colspan='6'>Aucune ligne d'inventory disponible.</td></tr>"

    pod_b_fill_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('timestamp') or '-'))}</td>"
            f"<td>{escape(str(item.get('symbol', '-')))}</td>"
            f"<td>{escape(str(item.get('side', '-')))}</td>"
            f"<td>{escape(str(item.get('action', '-')))}</td>"
            f"<td>{fmt_number(item.get('price'), 6)}</td>"
            f"<td>{fmt_number(item.get('size'), 6)}</td>"
            f"<td>{fmt_number(item.get('notional_usd'), 2)}</td>"
            f"<td>{fmt_number(item.get('fee_usd'), 4)}</td>"
            "</tr>"
        )
        for item in reversed(
            [
                item
                for item in (
                    pod_b_status.get("recent_fills", []) if isinstance(pod_b_status, dict) else []
                )
                if isinstance(item, dict)
            ][-20:]
        )
    )
    if not pod_b_fill_rows:
        pod_b_fill_rows = "<tr><td colspan='8'>Aucun fill récent visible pour Pod B.</td></tr>"

    runtime_report_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['pod']))}</td>"
            f"<td>{_status_badge('good' if bool(item['healthy']) else 'bad', 'healthy' if bool(item['healthy']) else 'degraded')}</td>"
            f"<td>{escape(str(item['process_state'] or '-'))}</td>"
            f"<td>{int(item['position_count'])}</td>"
            f"<td>{int(item['open_order_count'])}</td>"
            f"<td>{int(item['total_fill_count'])}</td>"
            f"<td>{float(item['realized_pnl_usd']):.4f}</td>"
            f"<td>{float(item['total_unrealized_pnl_usd']):.4f}</td>"
            "</tr>"
        )
        for item in runtime_report["pods"]
    )
    runtime_service_report_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['label']))}</td>"
            f"<td>{_status_badge('good' if bool(item['healthy']) else 'bad', 'healthy' if bool(item['healthy']) else 'degraded')}</td>"
            f"<td>{escape(str(item.get('process_state') or '-'))}</td>"
            f"<td>{int(item.get('symbol_count', 0))}</td>"
            f"<td>{int(item.get('polls_completed', 0))}</td>"
            f"<td>{int(item.get('records_written', 0))}</td>"
            f"<td>{escape(str(item.get('last_collected_at') or '-'))}</td>"
            f"<td>{escape(str(item.get('output_path') or '-'))}</td>"
            "</tr>"
        )
        for item in runtime_service_rows
        if bool(item.get("enabled", True))
    ) or "<tr><td colspan='8'>Aucun collector runtime actif visible.</td></tr>"
    pod_c_scope_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(symbol)}</td>"
            f"<td>{'oui' if symbol in observed_symbols else 'non'}</td>"
            f"<td>{'oui' if symbol in tradable_symbols else 'non'}</td>"
            f"<td>{escape(str(next((item.get('owner') for item in pod_c_routing_rows if str(item.get('symbol')).upper() == symbol), '-')))}</td>"
            f"<td>{escape(str(next((item.get('reason') for item in pod_c_routing_rows if str(item.get('symbol')).upper() == symbol), '-')))}</td>"
            "</tr>"
        )
        for symbol in pod_c_scope_symbols
    ) or "<tr><td colspan='5'>Aucun symbole Pod C derive de l'univers observe.</td></tr>"
    ownership_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['symbol']))}</td>"
            f"<td>{escape(str(item['owner'] or 'unassigned'))}</td>"
            f"<td>{escape(str(item.get('override_owner') or '-'))}</td>"
            f"<td>{escape(str(item.get('routing_mode') or '-'))}</td>"
            f"<td>{escape(str(item.get('routing_reason') or '-'))}</td>"
            "</tr>"
        )
        for item in snapshot["symbol_ownership"]
    ) or "<tr><td colspan='5'>Aucune ownership visible.</td></tr>"
    local_regime_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('symbol') or '-'))}</td>"
            f"<td>{escape(str(item.get('local_regime') or '-'))}</td>"
            f"<td>{escape(str(item.get('global_alignment') or '-'))}</td>"
            f"<td>{escape(str(item.get('owner') or 'unassigned'))}</td>"
            f"<td>{escape(str(item.get('override_owner') or '-'))}</td>"
            f"<td>{escape(str(item.get('reassignment_count') or 0))}</td>"
            f"<td>{escape(str(item.get('reason') or '-'))}</td>"
            "</tr>"
        )
        for item in snapshot.get("local_regime_by_symbol", [])
        if isinstance(item, dict)
    ) or "<tr><td colspan='7'>Aucun état local visible.</td></tr>"
    routing_override_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(symbol))}</td>"
            f"<td>{escape(str(owner))}</td>"
            f"<td>config</td>"
            "</tr>"
        )
        for symbol, owner in sorted(
            (
                snapshot.get("routing_overrides", {}).get("config", {})
                if isinstance(snapshot.get("routing_overrides"), dict)
                else {}
            ).items()
        )
    )
    runtime_override_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(symbol))}</td>"
            f"<td>{escape(str(owner))}</td>"
            f"<td>runtime</td>"
            "</tr>"
        )
        for symbol, owner in sorted(
            (
                snapshot.get("routing_overrides", {}).get("runtime", {})
                if isinstance(snapshot.get("routing_overrides"), dict)
                else {}
            ).items()
        )
    )
    routing_override_rows = (
        routing_override_rows + runtime_override_rows
    ) or "<tr><td colspan='3'>Aucun override de routing actif.</td></tr>"
    routing_override_meta = (
        snapshot.get("routing_overrides", {})
        if isinstance(snapshot.get("routing_overrides"), dict)
        else {}
    )
    routing_override_runtime_updated_at = escape(
        str(routing_override_meta.get("runtime_updated_at") or "-")
    )
    routing_override_runtime_path = escape(
        str(routing_override_meta.get("runtime_path") or "-")
    )
    routing_decision_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('symbol') or '-'))}</td>"
            f"<td>{escape(str(item.get('owner') or 'unassigned'))}</td>"
            f"<td>{escape(str(item.get('previous_owner') or '-'))}</td>"
            f"<td>{escape(str(item.get('mode') or '-'))}</td>"
            f"<td>{escape(', '.join(item.get('candidate_pods', [])) if isinstance(item.get('candidate_pods'), list) else '-')}</td>"
            f"<td>{'<br>'.join(f'{escape(str(pod))}: {float(score):.2f}' for pod, score in sorted((item.get('pod_scores') or {}).items())) if isinstance(item.get('pod_scores'), dict) and item.get('pod_scores') else '-'}</td>"
            f"<td>{escape(str(item.get('local_regime') or '-'))}</td>"
            f"<td>{'on' if bool(item.get('reassignment_cooldown_active')) else 'off'}</td>"
            f"<td>{escape(str(item.get('override_owner') or '-'))}</td>"
            f"<td>{'<br>'.join(f'{escape(str(pod))}: {escape(str(reason))}' for pod, reason in sorted((item.get('pod_reasoning') or {}).items())) if isinstance(item.get('pod_reasoning'), dict) and item.get('pod_reasoning') else '-'}</td>"
            f"<td>{escape(str(item.get('reason') or '-'))}</td>"
            "</tr>"
        )
        for item in snapshot.get("symbol_routing", [])
        if isinstance(item, dict)
    ) or "<tr><td colspan='11'>Aucune décision de routing visible.</td></tr>"
    conflict_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(conflict['symbol']))}</td>"
            f"<td>{escape(str(conflict['requested_by']))}</td>"
            f"<td>{escape(str(conflict['owner']))}</td>"
            "</tr>"
        )
        for conflict in snapshot["ownership_conflicts"]
    ) or "<tr><td colspan='3'>Aucun conflit d'ownership.</td></tr>"
    regime_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['recorded_at']))}</td>"
            f"<td>{escape(str(item['previous_regime']))}</td>"
            f"<td>{escape(str(item['new_regime']))}</td>"
            f"<td>{fmt_number(item['snapshot'].get('adx'), 2)}</td>"
            f"<td>{fmt_number(item['snapshot'].get('atr_ratio'), 2)}</td>"
            "</tr>"
        )
        for item in snapshot["regime_history"]
        if isinstance(item, dict)
    ) or "<tr><td colspan='5'>Aucune transition de régime enregistrée.</td></tr>"
    metric_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(name))}</td>"
            f"<td>{escape(str(value))}</td>"
            "</tr>"
        )
        for name, value in snapshot["metrics"].items()
    )

    tabs = [
        ("status", "Status"),
        ("pod_a", "Pod A"),
        ("pod_b", "Pod B"),
        ("pod_c", "Pod C"),
        ("activity", "Activity"),
        ("system", "System"),
    ]
    tab_nav = "".join(
        (
            f"<button class='tab-button{' is-active' if key == active_tab else ''}' "
            f"type='button' data-tab-button='{key}' aria-selected='{'true' if key == active_tab else 'false'}'>"
            f"{escape(label)}</button>"
        )
        for key, label in tabs
    )

    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #efe6d8;
      --panel: rgba(255, 251, 244, 0.94);
      --panel-strong: #fffdf9;
      --text: #1f2a33;
      --muted: #66727c;
      --line: #d8ccbb;
      --accent: #145b57;
      --accent-soft: #d8eeeb;
      --good: #176b3a;
      --good-soft: #ddf5e5;
      --warn: #9a6700;
      --warn-soft: #fff0cc;
      --bad: #a12d2f;
      --bad-soft: #ffe1e1;
      --neutral: #6a7680;
      --neutral-soft: #edf0f2;
      --shadow: 0 18px 40px rgba(31, 42, 51, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, #fff4dc, transparent 28%),
        radial-gradient(circle at top right, #d8eeeb, transparent 22%),
        linear-gradient(180deg, #f4ecdf 0%, var(--bg) 100%);
    }}
    main {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 28px 18px 48px;
    }}
    h1, h2, h3 {{ margin: 0; }}
    p {{ margin: 0; color: var(--muted); }}
    .hero {{
      background: linear-gradient(135deg, rgba(255,253,249,0.95), rgba(248,241,230,0.92));
      border: 1px solid var(--line);
      border-radius: 28px;
      box-shadow: var(--shadow);
      padding: 24px;
      display: grid;
      gap: 16px;
    }}
    .eyebrow {{
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
      font-size: 0.95rem;
      color: var(--accent);
      letter-spacing: 0.03em;
    }}
    .chip-row, .hero-links, .tab-nav, .filter-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .chip, .meta-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.9rem;
      font-weight: 600;
    }}
    .hero h1 {{
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
      font-size: clamp(2rem, 4vw, 3.4rem);
      line-height: 1.02;
      letter-spacing: -0.03em;
    }}
    .hero-copy {{
      max-width: 760px;
      line-height: 1.55;
    }}
    .hero-links a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 700;
    }}
    .hero-links a:hover {{
      text-decoration: underline;
    }}
    .tab-shell {{
      margin-top: 20px;
      display: grid;
      gap: 16px;
    }}
    .tab-nav {{
      background: rgba(255, 253, 249, 0.84);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px;
      box-shadow: 0 10px 24px rgba(31, 42, 51, 0.05);
      position: sticky;
      top: 12px;
      z-index: 10;
      backdrop-filter: blur(12px);
    }}
    .tab-button {{
      border: 0;
      background: transparent;
      color: var(--muted);
      border-radius: 999px;
      padding: 10px 14px;
      font-weight: 700;
      font-size: 0.95rem;
      cursor: pointer;
      transition: background 120ms ease, color 120ms ease, transform 120ms ease;
    }}
    .tab-button:hover {{
      color: var(--text);
      transform: translateY(-1px);
    }}
    .tab-button.is-active {{
      background: var(--accent);
      color: #fff;
      box-shadow: 0 10px 20px rgba(20, 91, 87, 0.18);
    }}
    .tab-panel {{
      display: none;
      gap: 16px;
    }}
    .tab-panel.is-active {{
      display: grid;
    }}
    .panel, .status-card, .metric-card, .pod-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
    }}
    .panel {{
      padding: 20px;
      overflow: hidden;
    }}
    .panel-header {{
      display: grid;
      gap: 6px;
      margin-bottom: 16px;
    }}
    .panel-header p {{
      max-width: 820px;
      line-height: 1.5;
    }}
    .status-grid, .metric-grid, .pod-grid, .pod-detail-grid {{
      display: grid;
      gap: 14px;
    }}
    .focus-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
    }}
    .status-grid {{
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }}
    .metric-grid {{
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    }}
    .pod-grid {{
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    }}
    .pod-detail-grid {{
      grid-template-columns: 1.1fr 0.9fr;
      align-items: start;
    }}
    .status-card, .metric-card, .pod-card {{
      padding: 18px;
    }}
    .panel-good {{
      background: linear-gradient(180deg, rgba(248, 255, 250, 0.98), rgba(255, 251, 244, 0.97));
      border-color: rgba(23, 107, 58, 0.20);
    }}
    .panel-warn {{
      background: linear-gradient(180deg, rgba(255, 251, 240, 0.98), rgba(255, 251, 244, 0.97));
      border-color: rgba(154, 103, 0, 0.22);
    }}
    .panel-bad {{
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.98), rgba(255, 251, 244, 0.97));
      border-color: rgba(161, 45, 47, 0.22);
    }}
    .panel-neutral {{
      background: linear-gradient(180deg, rgba(255, 253, 249, 0.98), rgba(252, 246, 238, 0.96));
      border-color: rgba(158, 144, 118, 0.16);
    }}
    .global-banner {{
      display: grid;
      gap: 12px;
      padding: 22px;
      background: linear-gradient(135deg, rgba(255,253,249,0.96), rgba(240,247,246,0.92));
    }}
    .global-banner-good {{
      background: linear-gradient(135deg, rgba(248, 255, 250, 0.98), rgba(231, 247, 236, 0.94));
      border-color: rgba(23, 107, 58, 0.22);
    }}
    .global-banner-warn {{
      background: linear-gradient(135deg, rgba(255, 251, 237, 0.98), rgba(255, 243, 209, 0.94));
      border-color: rgba(154, 103, 0, 0.24);
    }}
    .global-banner-bad {{
      background: linear-gradient(135deg, rgba(255, 247, 247, 0.98), rgba(255, 229, 229, 0.94));
      border-color: rgba(161, 45, 47, 0.24);
    }}
    .global-banner h2 {{
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
      font-size: clamp(1.6rem, 3vw, 2.4rem);
      line-height: 1.05;
    }}
    .global-banner p {{
      max-width: 780px;
      line-height: 1.55;
    }}
    .status-head {{
      margin-bottom: 12px;
    }}
    .status-card-good {{
      background: linear-gradient(180deg, rgba(248, 255, 250, 0.96), rgba(255, 251, 244, 0.96));
      border-color: rgba(23, 107, 58, 0.18);
    }}
    .status-card-warn {{
      background: linear-gradient(180deg, rgba(255, 251, 240, 0.96), rgba(255, 251, 244, 0.96));
      border-color: rgba(154, 103, 0, 0.20);
    }}
    .status-card-bad {{
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.96), rgba(255, 251, 244, 0.96));
      border-color: rgba(161, 45, 47, 0.20);
    }}
    .metric-card strong {{
      display: block;
      margin-top: 6px;
      font-size: 1.5rem;
      line-height: 1.1;
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
    }}
    .metric-card small {{
      display: block;
      margin-top: 8px;
      color: var(--muted);
      line-height: 1.4;
    }}
    .pod-card-head {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .pod-card-head p {{
      margin-top: 6px;
      line-height: 1.45;
    }}
    .pod-card-good {{
      background: linear-gradient(180deg, rgba(248, 255, 250, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(23, 107, 58, 0.18);
    }}
    .pod-card-warn {{
      background: linear-gradient(180deg, rgba(255, 251, 240, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(154, 103, 0, 0.20);
    }}
    .pod-card-bad {{
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(161, 45, 47, 0.20);
    }}
    .pod-card-neutral {{
      background: linear-gradient(180deg, rgba(246, 248, 249, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(106, 118, 128, 0.18);
    }}
    .dot {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
      margin-top: 6px;
      flex: none;
    }}
    .dot-good {{ background: var(--good); box-shadow: 0 0 0 6px var(--good-soft); }}
    .dot-warn {{ background: var(--warn); box-shadow: 0 0 0 6px var(--warn-soft); }}
    .dot-bad {{ background: var(--bad); box-shadow: 0 0 0 6px var(--bad-soft); }}
    .dot-neutral {{ background: var(--neutral); box-shadow: 0 0 0 6px var(--neutral-soft); }}
    .pod-card-meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }}
    .pod-facts {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px 14px;
      margin: 0 0 14px;
    }}
    .pod-facts div {{
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }}
    .pod-facts dt {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .pod-facts dd {{
      margin: 0;
      font-weight: 600;
      line-height: 1.45;
    }}
    .tab-link {{
      border: 0;
      background: transparent;
      color: var(--accent);
      padding: 0;
      font-weight: 700;
      cursor: pointer;
    }}
    .tab-link:hover {{
      text-decoration: underline;
    }}
    .badge {{
      display: inline-block;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 0.86rem;
      font-weight: 700;
    }}
    .badge-good {{ background: var(--good-soft); color: var(--good); }}
    .badge-warn {{ background: var(--warn-soft); color: var(--warn); }}
    .badge-bad {{ background: var(--bad-soft); color: var(--bad); }}
    .badge-neutral {{ background: var(--neutral-soft); color: var(--neutral); }}
    .soft-note {{
      color: var(--muted);
      line-height: 1.55;
    }}
    .inline-form {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) auto auto;
      gap: 10px;
      align-items: end;
    }}
    .field-stack {{
      display: grid;
      gap: 6px;
    }}
    .field-stack label {{
      font-size: 0.82rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .field-stack input,
    .field-stack select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      padding: 11px 12px;
      color: var(--text);
      font: inherit;
    }}
    .action-button {{
      border: 0;
      border-radius: 12px;
      padding: 11px 14px;
      font-weight: 700;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
    }}
    .action-button.secondary {{
      background: rgba(143, 103, 71, 0.12);
      color: var(--accent);
      border: 1px solid rgba(143, 103, 71, 0.22);
    }}
    .inline-status {{
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(143, 103, 71, 0.08);
      color: var(--muted);
      min-height: 22px;
      line-height: 1.45;
    }}
    .focus-item {{
      display: grid;
      gap: 12px;
      padding: 18px;
      border-radius: 18px;
      background: var(--panel-strong);
      border: 1px solid rgba(216, 204, 187, 0.75);
    }}
    .focus-item-top {{
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }}
    .focus-item-top strong {{
      display: block;
      line-height: 1.35;
    }}
    .focus-item-top small {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      line-height: 1.45;
    }}
    .focus-tag {{
      justify-self: start;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 700;
    }}
    .focus-tag-good {{
      background: var(--good-soft);
      color: var(--good);
    }}
    .focus-tag-warn {{
      background: var(--warn-soft);
      color: var(--warn);
    }}
    .focus-tag-bad {{
      background: var(--bad-soft);
      color: var(--bad);
    }}
    .simple-list {{
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.55;
    }}
    .table-wrap {{
      overflow-x: auto;
      border-radius: 16px;
      border: 1px solid rgba(216, 204, 187, 0.55);
      background: var(--panel-strong);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
      min-width: 780px;
    }}
    th, td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 700;
      background: rgba(244, 236, 223, 0.45);
    }}
    .th-with-tooltip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      position: relative;
    }}
    .tooltip-trigger {{
      width: 18px;
      height: 18px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--accent);
      font-size: 0.72rem;
      font-weight: 700;
      line-height: 1;
      padding: 0;
      cursor: help;
    }}
    .tooltip-bubble {{
      position: absolute;
      left: 0;
      top: calc(100% + 8px);
      width: min(260px, 42vw);
      padding: 10px 12px;
      border-radius: 12px;
      background: #1f2a33;
      color: #fff;
      font-size: 0.84rem;
      line-height: 1.4;
      box-shadow: 0 12px 24px rgba(31, 42, 51, 0.18);
      opacity: 0;
      pointer-events: none;
      transform: translateY(-4px);
      transition: opacity 120ms ease, transform 120ms ease;
      z-index: 20;
    }}
    .tooltip-bubble::before {{
      content: "";
      position: absolute;
      left: 12px;
      top: -6px;
      width: 12px;
      height: 12px;
      background: #1f2a33;
      transform: rotate(45deg);
    }}
    .th-with-tooltip:hover .tooltip-bubble,
    .th-with-tooltip:focus-within .tooltip-bubble {{
      opacity: 1;
      transform: translateY(0);
    }}
    .filter-row {{
      margin-bottom: 12px;
    }}
    .filter-chip {{
      border: 1px solid var(--line);
      background: #fffaf0;
      color: var(--text);
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 0.92rem;
      font-weight: 600;
      cursor: pointer;
      transition: transform 120ms ease, background 120ms ease, color 120ms ease, border-color 120ms ease;
    }}
    .filter-chip:hover {{
      transform: translateY(-1px);
      border-color: var(--accent);
    }}
    .filter-chip.is-active {{
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }}
    .is-hidden {{
      display: none;
    }}
    @media (max-width: 980px) {{
      .inline-form {{
        grid-template-columns: 1fr;
      }}
      .pod-detail-grid {{
        grid-template-columns: 1fr;
      }}
      .pod-facts {{
        grid-template-columns: 1fr;
      }}
      .tab-nav {{
        position: static;
      }}
    }}
  </style>
</head>
<body data-default-tab="{escape(active_tab)}" data-refresh-seconds="{refresh_seconds}">
  <main>
    <header class="hero">
      <div class="chip-row">
        <span class="chip">Version {escape(VERSION)}</span>
        <span class="chip">Profile {escape(str(snapshot['profile']))}</span>
        <span class="chip">Mode {escape(str(snapshot['mode']))}</span>
        <span class="chip">Régime {escape(str(snapshot['regime']))}</span>
        <span class="chip">Actif depuis {escape(uptime_label)}</span>
        <span class="chip">Auto-refresh {refresh_seconds}s</span>
      </div>
      <div class="eyebrow">TRIDENT Supervisor Dashboard</div>
      <h1>{escape(title)}</h1>
      <p class="hero-copy">{escape(subtitle)}</p>
      <div class="hero-links">
        <span>Last updated: {escape(refreshed_at)}</span>
        <a href="/dashboard">/dashboard</a>
        <a href="/trades">/trades</a>
        <a href="/api/state">/api/state</a>
        <a href="/api/report">/api/report</a>
        <a href="/api/metrics">/api/metrics</a>
      </div>
    </header>

    <div class="tab-shell">
      <nav class="tab-nav" aria-label="Navigation principale">
        {tab_nav}
      </nav>

      <section class="tab-panel{' is-active' if active_tab == 'status' else ''}" data-tab-panel="status">
        <div class="panel global-banner global-banner-{escape(global_tone)}">
          <div class="panel-header">
            <h2>{escape(global_label)}</h2>
            <p>{escape(commentary)}</p>
          </div>
          <div>
            {_status_badge(global_tone, global_label)}
            <span class="meta-chip">Régime {escape(str(snapshot['regime']))}</span>
            <span class="meta-chip">{healthy_count}/{enabled_count} pod(s) sain(s)</span>
            <span class="meta-chip">{active_positions} position(s)</span>
            <span class="meta-chip">{active_orders} ordre(s)</span>
          </div>
        </div>

        <div class="panel panel-{escape(_panel_tone(focus_tone))}">
          <div class="panel-header">
            <h2>À faire maintenant</h2>
            <p>Liste courte et concrète. Si tu ne dois lire qu'un bloc sur cette page, c'est celui-ci.</p>
          </div>
          <div class="focus-grid">
            {focus_rows}
          </div>
        </div>

        <div class="panel panel-{escape(_panel_tone(global_tone))}">
          <div class="panel-header">
            <h2>Status</h2>
            <p>État général très compact : collecte, santé des pods, ownership et activité récente. Le détail est volontairement renvoyé vers les onglets de pod et système.</p>
          </div>
          <div class="status-grid">
            {status_rows}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h2>En un coup d’œil</h2>
            <p>Quelques chiffres seulement : régime, santé, exposition, exécutions et cash disponible.</p>
          </div>
          <div class="metric-grid">
            {summary_cards}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h2>Pods</h2>
            <p>Chaque carte dit simplement si le pod est OK, à surveiller, ou s'il mérite qu'on ouvre son onglet détail.</p>
          </div>
          <div class="pod-grid">
            {pod_cards}
          </div>
        </div>
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'pod_a' else ''}" data-tab-panel="pod_a">
        <div class="panel panel-{escape(_panel_tone(pod_a_summary['tone']))}">
          <div class="panel-header">
            <h2>Pod A</h2>
            <p>Pod directionnel trend / structure. On suit ici les positions ouvertes, les trades fermés, le levier, le stop et la raison exacte d'ouverture / fermeture.</p>
          </div>
          <div class="metric-grid">
            {render_stat_cards([
                {"label": "Status", "value": str(pod_a_summary["badge"]), "note": str(pod_a_summary["comment"])},
                {"label": "Target", "value": f"{float(pod_a_summary['target_usd']):.2f} USD", "note": f"{float(pod_a_summary['target_pct']):.2f} du capital"},
                {"label": "Open positions", "value": str(pod_a_summary['position_count']), "note": "Positions directionnelles ouvertes"},
                {"label": "Signals", "value": str(pod_a_summary['preview_count']), "note": "Previews actuellement visibles"},
                {"label": "Exec", "value": str(pod_a_summary['total_fill_count']), "note": "Trades/fills observés"},
                {"label": "Realized PnL", "value": f"{float(pod_a_summary['realized_pnl_usd']):.4f} USD", "note": "Cumul runtime"},
            ])}
          </div>
        </div>

        <div class="pod-detail-grid">
          <div class="panel panel-{escape(_panel_tone(pod_a_summary['tone']))}">
            <div class="panel-header">
              <h3>Trades ouverts</h3>
              <p>Ce tableau sert a lire la position telle qu'elle vit maintenant: prix courant, valeur actuelle, marge immobilisee, niveaux TP/SL et etat du trailing.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Symbol", "Marché actuellement détenu par le pod.")}{_table_header("Side", "Sens de la position: long si le pod gagne sur une hausse, short s'il gagne sur une baisse.")}{_table_header("Raison ouverture", "Nom lisible du setup qui a ouvert le trade. C'est la logique d'entree initiale, pas l'etat actuel du trade.")}{_table_header("Prix entree", "Prix moyen d'entree retenu par le moteur dry-run au moment de l'ouverture.")}{_table_header("Prix courant", "Dernier prix vu dans le snapshot live pour ce symbole. C'est la reference utilisee pour valoriser le trade maintenant.")}{_table_header("Valeur courante USD", "Valeur notionnelle actuelle de la position au prix courant. Elle peut bouger meme si la taille unitaire ne change pas.")}{_table_header("Marge utilisee", "Capital immobilise pour porter la position. C'est plus utile operatoirement que la notionnelle brute.")}{_table_header("Prix TP", "Prix theorique auquel le take profit fixe sortirait le trade s'il etait touche maintenant.")}{_table_header("Prix SL", "Prix de stop loss actuel. Quand une invalidation structurelle existe, on l'affiche directement; sinon on reconstruit le stop a partir des bps.")}{_table_header("Unrealized PnL", "PnL latent marque au dernier prix courant. Ce n'est pas realise tant que le trade n'est pas ferme.")}{_table_header("Trailing TP", "Etat du trailing: non configure, en attente d'activation, ou actif avec le niveau actuel du stop suiveur.")}{_table_header("Ouvert le", "Horodatage d'ouverture du trade pour juger son age reel.")}</tr>
                </thead>
                <tbody>{render_directional_open_rows("pod_a")}</tbody>
              </table>
            </div>
          </div>
          <div class="panel panel-neutral">
            <div class="panel-header">
              <h3>Signal preview</h3>
              <p>Signaux vus par le superviseur mais pas encore forcément convertis en position.</p>
            </div>
            {render_preview_list(snapshot.get("pod_a_signal_preview"))}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Trades fermés récents</h3>
            <p>Les raisons d'ouverture et de fermeture sont maintenant formulees en langage lisible, pour comprendre rapidement ce qui a declenche l'entree puis la sortie.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{_table_header("Ferme le", "Horodatage reel de la sortie du trade.")}{_table_header("Symbol", "Marche concerne par le trade ferme.")}{_table_header("Side", "Sens du trade qui a ete porte: long ou short.")}{_table_header("Raison ouverture", "Setup lisible qui avait justifie l'entree du trade au depart.")}{_table_header("Raison fermeture", "Explication lisible de la sortie: TP touche, trailing stop, stop loss, time stop, signal oppose, etc.")}{_table_header("Prix entree", "Prix moyen d'entree du trade au moment de l'ouverture.")}{_table_header("Prix sortie", "Prix de sortie effectivement retenu lors de la cloture.")}{_table_header("Notional USD", "Notionnelle cible du trade au moment ou il a ete ouvert.")}{_table_header("Leverage", "Levier effectif configure pour ce trade si l'information est disponible.")}{_table_header("PnL USD", "Resultat net du trade, frais inclus.")}</tr>
              </thead>
              <tbody>{render_directional_closed_rows("pod_a")}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'pod_b' else ''}" data-tab-panel="pod_b">
        <div class="panel panel-{escape(_panel_tone(pod_b_summary['tone']))}">
          <div class="panel-header">
            <h2>Pod B</h2>
            <p>Pod B ne se lit pas comme une liste de trades directionnels. Il faut surtout voir son inventory, ses ordres maker ouverts, ses fills récents et sa capacité à rester propre dans un marché range.</p>
          </div>
          <div class="metric-grid">
            {render_stat_cards([
                {"label": "Status", "value": str(pod_b_summary["badge"]), "note": str(pod_b_summary["comment"])},
                {"label": "Process", "value": str(pod_b_summary["process_state"]), "note": f"Sync reason {escape(str(pod_b_status.get('last_sync_reason', '-')))}"},
                {"label": "Managed symbols", "value": str(len(pod_b_status.get("managed_symbols", []) if isinstance(pod_b_status, dict) else [])), "note": ", ".join(str(x) for x in (pod_b_status.get("managed_symbols", []) if isinstance(pod_b_status, dict) else [])) or "-"},
                {"label": "Open orders", "value": str(pod_b_summary["open_order_count"]), "note": "Quotes maker visibles"},
                {"label": "Fills", "value": str(pod_b_summary["total_fill_count"]), "note": "Exécutions observées"},
                {"label": "Realized PnL", "value": f"{float(pod_b_summary['realized_pnl_usd']):.4f} USD", "note": f"Unrealized {float(pod_b_summary['total_unrealized_pnl_usd']):.4f} USD"},
            ])}
          </div>
        </div>

        <div class="pod-detail-grid">
          <div class="panel panel-{escape(_panel_tone(pod_b_summary['tone']))}">
            <div class="panel-header">
              <h3>Inventory</h3>
              <p>Le tableau clé pour Pod B : on voit si l'inventory reste propre, si le skew devient trop fort et si les ordres ouverts suffisent encore à la rééquilibrer.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Symbol", "Marché suivi.")}{_table_header("Target USD", "Notionnel cible sur ce symbole.")}{_table_header("Current USD", "Notionnel actuellement porté.")}{_table_header("Skew %", "Décalage entre cible et inventory actuelle.")}{_table_header("Position", "Indique si une position est actuellement ouverte.")}{_table_header("Open orders", "Nombre d'ordres maker en attente sur ce symbole.")}</tr>
                </thead>
                <tbody>{pod_b_inventory_rows}</tbody>
              </table>
            </div>
          </div>
          <div class="panel panel-neutral">
            <div class="panel-header">
              <h3>Positions d'inventory</h3>
              <p>Quand Pod B est chargé dans un sens, c'est ici qu'on voit son exposition réelle et son PnL latent.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens de l'inventory.")}{_table_header("Size", "Taille unitaire.")}{_table_header("Entry", "Prix moyen d'entrée.")}{_table_header("Mark", "Prix courant marqué.")}{_table_header("Notional USD", "Valeur notionnelle actuelle.")}{_table_header("Unrealized", "PnL latent actuel.")}</tr>
                </thead>
                <tbody>{pod_b_positions_rows}</tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="pod-detail-grid">
          <div class="panel panel-neutral">
            <div class="panel-header">
              <h3>Ordres maker ouverts</h3>
              <p>La lecture la plus utile pour savoir si Pod B quote bilatéralement ou s'il reste seulement en mode de désencombrement.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Côté du quote.")}{_table_header("Prix", "Prix du quote.")}{_table_header("Size", "Taille de l'ordre.")}{_table_header("Type", "Type d'ordre, ici généralement maker.")}{_table_header("Status", "Statut de l'ordre dans le status runtime.")}</tr>
                </thead>
                <tbody>{pod_b_orders_rows}</tbody>
              </table>
            </div>
          </div>
          <div class="panel panel-neutral">
            <div class="panel-header">
              <h3>Fills récents</h3>
              <p>Vue exécution de Pod B : on suit la cadence, la fee et le sens des fills, pas une logique TP/SL directionnelle.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Timestamp", "Horodatage du fill.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens du fill.")}{_table_header("Action", "Type d'action enregistrée.")}{_table_header("Prix", "Prix d'exécution.")}{_table_header("Size", "Taille exécutée.")}{_table_header("Notional USD", "Valeur notionnelle du fill.")}{_table_header("Fee USD", "Frais du fill.")}</tr>
                </thead>
                <tbody>{pod_b_fill_rows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'pod_c' else ''}" data-tab-panel="pod_c">
        <div class="panel panel-{escape(_panel_tone(pod_c_summary['tone']))}">
          <div class="panel-header">
            <h2>Pod C</h2>
            <p>Pod opportuniste event / lead-lag. On suit les positions, la logique d'ouverture, puis la raison de fermeture comme sur Pod A.</p>
          </div>
          <div class="metric-grid">
            {render_stat_cards([
                {"label": "Status", "value": str(pod_c_summary["badge"]), "note": str(pod_c_summary["comment"])},
                {"label": "Target", "value": f"{float(pod_c_summary['target_usd']):.2f} USD", "note": f"{float(pod_c_summary['target_pct']):.2f} du capital"},
                {"label": "Open positions", "value": str(pod_c_summary['position_count']), "note": "Positions event-driven ouvertes"},
                {"label": "Signals", "value": str(pod_c_summary['preview_count']), "note": "Previews actuellement visibles"},
                {"label": "Exec", "value": str(pod_c_summary['total_fill_count']), "note": "Trades/fills observés"},
                {"label": "Realized PnL", "value": f"{float(pod_c_summary['realized_pnl_usd']):.4f} USD", "note": "Cumul runtime"},
            ])}
          </div>
        </div>

        <div class="pod-detail-grid">
          <div class="panel panel-{escape(_panel_tone(pod_c_summary['tone']))}">
            <div class="panel-header">
              <h3>Trades ouverts</h3>
              <p>On lit ici les positions Tradfi vivantes avec les niveaux operatoires vraiment utiles: prix live, valeur, marge, TP, SL et trailing.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Symbol", "Marche Tradfi actuellement porte par Pod C.")}{_table_header("Side", "Sens de la position: long a la hausse, short a la baisse.")}{_table_header("Raison ouverture", "Setup lisible qui a motive l'ouverture initiale du trade.")}{_table_header("Prix entree", "Prix moyen d'entree retenu au moment de l'ouverture.")}{_table_header("Prix courant", "Dernier prix live vu par le runner pour ce symbole.")}{_table_header("Valeur courante USD", "Valorisation actuelle de la position au dernier prix courant.")}{_table_header("Marge utilisee", "Capital immobilise pour porter ce trade. C'est la mesure pratique de l'exposition engagee.")}{_table_header("Prix TP", "Prix theorique du take profit fixe si la cible est atteinte.")}{_table_header("Prix SL", "Prix du stop de protection actuellement applicable au trade.")}{_table_header("Unrealized PnL", "PnL latent calcule au dernier prix courant. Il deviendra realise seulement a la sortie.")}{_table_header("Trailing TP", "Indique si le trailing est deja arme, et si oui a quel niveau se situe le stop suiveur actuel.")}{_table_header("Ouvert le", "Horodatage d'ouverture pour estimer l'anciennete du trade.")}</tr>
                </thead>
                <tbody>{render_directional_open_rows("pod_c")}</tbody>
              </table>
            </div>
          </div>
          <div class="panel panel-neutral">
            <div class="panel-header">
              <h3>Signal preview</h3>
              <p>Signaux event / lead-lag vus mais pas encore transformés en position.</p>
            </div>
            {render_preview_list(snapshot.get("pod_c_signal_preview"))}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Trades fermés récents</h3>
            <p>Les codes internes de setup et de sortie sont traduits en formulations lisibles pour faciliter la review operatoire.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{_table_header("Ferme le", "Horodatage reel de sortie du trade.")}{_table_header("Symbol", "Marche Tradfi concerne.")}{_table_header("Side", "Sens du trade qui a ete porte.")}{_table_header("Raison ouverture", "Setup lisible qui avait motive l'ouverture du trade.")}{_table_header("Raison fermeture", "Explication lisible de la sortie: TP, trailing stop, stop, time stop, signal oppose, etc.")}{_table_header("Prix entree", "Prix moyen d'entree du trade.")}{_table_header("Prix sortie", "Prix retenu a la fermeture.")}{_table_header("Notional USD", "Notionnelle cible du trade au moment de l'ouverture.")}{_table_header("Leverage", "Levier effectif configure si disponible.")}{_table_header("PnL USD", "Resultat net final du trade, frais inclus.")}</tr>
              </thead>
              <tbody>{render_directional_closed_rows("pod_c")}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'activity' else ''}" data-tab-panel="activity">
        <div class="panel">
          <div class="panel-header">
            <h2>Activity</h2>
            <p>Onglet transversal pour les positions ouvertes et les évènements récents. Il remplace l'ancienne page de trades, mais reste organisé par filtres pour ne pas noyer la vue principale.</p>
          </div>
          <div class="filter-row" aria-label="Filtres activity">
            <button class="filter-chip is-active" type="button" data-filter-group="status" data-filter-value="open">Open</button>
            <button class="filter-chip is-active" type="button" data-filter-group="status" data-filter-value="closed">Closed</button>
            <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_a">Pod A</button>
            <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_b">Pod B</button>
            <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_c">Pod C</button>
          </div>
          <p class="soft-note" style="margin-bottom:16px;">Les fills maker de Pod B restent visibles ici comme de l'activité d'inventory. Pour comprendre vraiment Pod B, son onglet dédié reste la meilleure vue.</p>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Open positions</h3>
              <p>Ce qui est en risque maintenant, tous pods confondus.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Pod", "Pod qui porte actuellement la position.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens actuel de la position.")}{_table_header("Open reason", "Pourquoi la position a été ouverte.")}{_table_header("Entry", "Prix d'entrée connu ou prix moyen.")}{_table_header("Notional USD", "Valeur notionnelle actuelle ou cible.")}{_table_header("Leverage", "Levier configuré quand disponible.")}{_table_header("Confidence", "Confiance du signal directionnel quand elle existe.")}{_table_header("Stop bps", "Distance du stop pour les pods directionnels.")}{_table_header("Time stop h", "Durée maximale de détention prévue.")}{_table_header("Opened at", "Horodatage d'ouverture si connu.")}</tr>
                </thead>
                <tbody>{render_activity_open_rows()}</tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Recent trade events</h3>
              <p>Historique récent des sorties directionnelles et fills Pod B.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Timestamp", "Horodatage de l'évènement.")}{_table_header("Pod", "Pod responsable.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens buy/sell ou long/short.")}{_table_header("Status", "closed pour un trade fermé, fill pour un fill Pod B.")}{_table_header("Open reason", "Pourquoi la position ou l'exécution a été initiée.")}{_table_header("Close reason", "Pourquoi le trade s'est fermé.")}{_table_header("Entry", "Prix d'entrée si connu.")}{_table_header("Exit", "Prix de sortie si connu.")}{_table_header("Notional USD", "Valeur notionnelle concernée.")}{_table_header("Leverage", "Levier configuré quand il est disponible.")}{_table_header("PnL USD", "PnL net quand disponible.")}</tr>
                </thead>
                <tbody>{render_activity_event_rows()}</tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Recent trading activity</h3>
              <p>Résumé mixte des derniers journaux live visibles.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Timestamp", "Heure de l'évènement.")}{_table_header("Pod", "Pod responsable.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens de l'ordre ou de la position.")}{_table_header("Event", "Type d'évènement.")}{_table_header("Price", "Prix d'exécution ou de sortie.")}{_table_header("Notional USD", "Valeur notionnelle approx.")}{_table_header("Leverage", "Levier configuré quand il est modélisé.")}{_table_header("PnL USD", "PnL net si connu.")}{_table_header("Comment", "Contexte utile : fee, raison de sortie, etc.")}</tr>
                </thead>
                <tbody>{recent_activity_rows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'system' else ''}" data-tab-panel="system">
        <div class="panel">
          <div class="panel-header">
            <h2>System</h2>
            <p>Onglet réservé aux détails opératoires : ownership, conflits, transitions de régime et métriques brutes. On le sort de la vue principale pour garder l'écran Status vraiment lisible.</p>
          </div>
          <div class="pod-detail-grid">
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Runtime pod report</h3>
                <p>Résumé structurel par pod.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>{_table_header("Pod", "Nom logique du pod.")}{_table_header("Healthy", "État runtime selon la fraîcheur du status.")}{_table_header("Process", "État du process ou runner associé.")}{_table_header("Positions", "Nombre de positions ouvertes.")}{_table_header("Open orders", "Nombre d'ordres suivis.")}{_table_header("Fills", "Nombre cumulé d'exécutions.")}{_table_header("Realized PnL", "PnL réalisé cumulé.")}{_table_header("Unrealized PnL", "PnL latent courant.")}</tr>
                  </thead>
                  <tbody>{runtime_report_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Ownership conflicts</h3>
                <p>Conflits d'ownership à régler avant d'augmenter le risque.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Symbol</th><th>Requested by</th><th>Owner</th></tr></thead>
                  <tbody>{conflict_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Data collectors</h3>
                <p>État runtime des services de collecte funding et asset context.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>{_table_header("Service", "Nom logique du collector.")}{_table_header("Healthy", "Fraîcheur du runtime status du collector.")}{_table_header("Process", "État du process de collecte.")}{_table_header("Symbols", "Nombre de symbols suivis.")}{_table_header("Polls", "Nombre de polls terminés.")}{_table_header("Records", "Records JSONL écrits.")}{_table_header("Last collected", "Horodatage du dernier lot collecté.")}{_table_header("Output", "Fichier de sortie courant.")}</tr>
                  </thead>
                  <tbody>{runtime_service_report_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Pod C scope visibility</h3>
                <p>Cette vue derive le scope de Pod C depuis `hyperliquid.observation_universe`, puis le filtre avec `pod_c.allowed_market_clusters`. Si un symbol n'apparait pas dans Routing decisions, c'est souvent qu'il n'est pas observe ou pas tradable, pas qu'il manque dans une liste secondaire.</p>
              </div>
              <div class="metric-grid" style="margin-bottom:16px;">
                {render_stat_cards([
                    {"label": "Clusters", "value": str(len(pod_c_allowed_clusters)), "note": ", ".join(pod_c_allowed_clusters) or "-"},
                    {"label": "In Scope", "value": str(len(pod_c_scope_symbols)), "note": ", ".join(pod_c_scope_symbols) or "-"},
                    {"label": "Observed", "value": str(len([symbol for symbol in pod_c_scope_symbols if symbol in observed_symbols])), "note": "Symbols Pod C presents dans les snapshots visibles"},
                    {"label": "Tradable", "value": str(len([symbol for symbol in pod_c_scope_symbols if symbol in tradable_symbols])), "note": "Symbols Pod C qui passent les gates live"},
                    {"label": "Routed To Pod C", "value": str(len(pod_c_routed_symbols)), "note": ", ".join(pod_c_routed_symbols) or "-"},
                ])}
              </div>
              <p class="soft-note" style="margin-bottom:12px;">Non observes: {escape(", ".join(pod_c_seen_not_observed) or "-")} | Observes mais non tradables: {escape(", ".join(pod_c_observed_not_tradable) or "-")}</p>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>{_table_header("Symbol", "Symbole configure dans le scope Pod C.")}{_table_header("Observed", "Oui si le symbole apparait dans la vue snapshot/superviseur utilisee par l'UI au moment du rendu.")}{_table_header("Tradable", "Oui si le symbole est vu et passe les gates de tradabilite live: spread, activite, funding, etc.")}{_table_header("Current owner", "Pod actuellement assigne par le routeur, ou '-' si aucune decision visible.")}{_table_header("Routing note", "Raison de routing actuellement visible pour ce symbole. Si la ligne est vide, l'UI n'a pas de decision runtime pour ce symbol.")}</tr>
                  </thead>
                  <tbody>{pod_c_scope_rows}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="pod-detail-grid">
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Symbol ownership</h3>
                <p>Qui possède quoi en ce moment.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Symbol</th><th>Owner</th><th>Override</th><th>Routing</th><th>Reason</th></tr></thead>
                  <tbody>{ownership_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Local symbol states</h3>
                <p>Lecture locale par coin, alignement global/local et compteur de réattributions.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Symbol</th><th>Local regime</th><th>Alignment</th><th>Owner</th><th>Override</th><th>Reassignments</th><th>Reason</th></tr></thead>
                  <tbody>{local_regime_rows}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="pod-detail-grid">
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Regime history</h3>
                <p>Transitions récentes du régime de marché.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Recorded at</th><th>Previous</th><th>New</th><th>ADX</th><th>ATR ratio</th></tr></thead>
                  <tbody>{regime_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Local transitions</h3>
                <p>Transitions locales récentes détectées coin par coin.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Recorded at</th><th>Symbol</th><th>Previous</th><th>New</th><th>Reason</th></tr></thead>
                  <tbody>{
                    "".join(
                        (
                            "<tr>"
                            f"<td>{escape(str(item.get('recorded_at') or '-'))}</td>"
                            f"<td>{escape(str(item.get('symbol') or '-'))}</td>"
                            f"<td>{escape(str(item.get('previous_local_regime') or '-'))}</td>"
                            f"<td>{escape(str(item.get('new_local_regime') or '-'))}</td>"
                            f"<td>{escape(str(item.get('reason') or '-'))}</td>"
                            "</tr>"
                        )
                        for item in snapshot.get("local_regime_transitions", [])[-20:]
                        if isinstance(item, dict)
                    ) or "<tr><td colspan='5'>Aucune transition locale enregistrée.</td></tr>"
                  }</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Routing overrides</h3>
              <p>Overrides statiques et runtime actuellement pris en compte par le routeur. Runtime file: <code>{routing_override_runtime_path}</code>. Last runtime update: <code>{routing_override_runtime_updated_at}</code>.</p>
            </div>
            <form class="inline-form" data-routing-override-form>
              <div class="field-stack">
                <label for="routing-override-symbol">Symbol</label>
                <input id="routing-override-symbol" name="symbol" type="text" placeholder="SOL" autocomplete="off">
              </div>
              <div class="field-stack">
                <label for="routing-override-owner">Owner</label>
                <select id="routing-override-owner" name="owner">
                  <option value="pod_a">pod_a</option>
                  <option value="pod_b">pod_b</option>
                  <option value="pod_c">pod_c</option>
                </select>
              </div>
              <button class="action-button" type="submit">Set runtime pin</button>
              <button class="action-button secondary" type="button" data-routing-override-clear>Clear pin</button>
            </form>
            <div class="inline-status" data-routing-override-status>
              Utilisez ce panneau pour forcer un symbole en live sans redémarrer le supervisor.
            </div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Symbol</th><th>Owner</th><th>Source</th></tr></thead>
                <tbody>{routing_override_rows}</tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Routing decisions</h3>
              <p>Vue détaillée des candidats, scores et raisons par symbole pour analyser le choix effectif.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Symbol</th><th>Owner</th><th>Previous</th><th>Mode</th><th>Candidates</th><th>Scores</th><th>Local regime</th><th>Cooldown</th><th>Override</th><th>Reasoning</th><th>Decision</th></tr></thead>
                <tbody>{routing_decision_rows}</tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Metrics</h3>
              <p>Métriques brutes exposées par l'API d'observabilité.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Name</th><th>Value</th></tr></thead>
                <tbody>{metric_rows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </section>
    </div>
  </main>
  <script>
    (() => {{
      const validTabs = new Set(["status", "pod_a", "pod_b", "pod_c", "activity", "system"]);
      const body = document.body;
      const refreshSeconds = Number(body.dataset.refreshSeconds || "0");
      const buttons = Array.from(document.querySelectorAll("[data-tab-button]"));
      const panels = Array.from(document.querySelectorAll("[data-tab-panel]"));
      const jumpButtons = Array.from(document.querySelectorAll("[data-jump-tab]"));
      const activeFilters = {{
        status: new Set(["open", "closed"]),
        pod: new Set(["pod_a", "pod_b", "pod_c"]),
      }};
      const filterButtons = Array.from(document.querySelectorAll("[data-filter-group]"));
      const filterRows = Array.from(document.querySelectorAll("tr[data-filter-status][data-filter-pod]"));
      const routingOverrideForm = document.querySelector("[data-routing-override-form]");
      const routingOverrideStatus = document.querySelector("[data-routing-override-status]");
      const routingOverrideClearButton = document.querySelector("[data-routing-override-clear]");

      function normalizedTab(tabName) {{
        return validTabs.has(tabName) ? tabName : body.dataset.defaultTab || "status";
      }}

      function activeTabName() {{
        return normalizedTab((window.location.hash || "").replace("#", ""));
      }}

      function scrollStorageKey(tabName) {{
        return `trident:scroll:${{window.location.pathname}}:${{normalizedTab(tabName)}}`;
      }}

      function saveScrollPosition(tabName = activeTabName()) {{
        try {{
          window.sessionStorage.setItem(scrollStorageKey(tabName), String(window.scrollY || 0));
        }} catch (_error) {{
          // Ignore storage failures and keep refresh behavior intact.
        }}
      }}

      function restoreScrollPosition(tabName = activeTabName()) {{
        try {{
          const raw = window.sessionStorage.getItem(scrollStorageKey(tabName));
          if (raw === null) return;
          const next = Number(raw);
          if (!Number.isFinite(next) || next < 0) return;
          window.requestAnimationFrame(() => {{
            window.scrollTo(0, next);
          }});
        }} catch (_error) {{
          // Ignore storage failures and keep refresh behavior intact.
        }}
      }}

      function setTab(tabName, updateHash = true) {{
        const next = normalizedTab(tabName);
        buttons.forEach((button) => {{
          const active = button.dataset.tabButton === next;
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-selected", active ? "true" : "false");
        }});
        panels.forEach((panel) => {{
          panel.classList.toggle("is-active", panel.dataset.tabPanel === next);
        }});
        if (updateHash) {{
          history.replaceState(null, "", `#${{next}}`);
        }}
      }}

      function refreshFilterRows() {{
        filterRows.forEach((row) => {{
          const status = row.dataset.filterStatus;
          const pod = row.dataset.filterPod;
          const visible = activeFilters.status.has(status) && activeFilters.pod.has(pod);
          row.classList.toggle("is-hidden", !visible);
        }});
      }}

      buttons.forEach((button) => {{
        button.addEventListener("click", () => {{
          setTab(button.dataset.tabButton);
        }});
      }});

      jumpButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          const key = button.dataset.jumpTab || "";
          const mapping = {{
            "pod_a": "pod_a",
            "pod_b": "pod_b",
            "pod_c": "pod_c",
          }};
          setTab(mapping[key] || key);
        }});
      }});

      filterButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          const group = button.dataset.filterGroup;
          const value = button.dataset.filterValue;
          const set = activeFilters[group];
          if (!set) return;
          if (set.has(value)) {{
            if (set.size === 1) return;
            set.delete(value);
            button.classList.remove("is-active");
          }} else {{
            set.add(value);
            button.classList.add("is-active");
          }}
          refreshFilterRows();
        }});
      }});

      async function submitRoutingOverride(ownerValue) {{
        if (!routingOverrideForm || !routingOverrideStatus) return;
        const formData = new window.FormData(routingOverrideForm);
        const symbol = String(formData.get("symbol") || "").trim().toUpperCase();
        if (!symbol) {{
          routingOverrideStatus.textContent = "Symbol requis pour modifier un pin runtime.";
          return;
        }}
        const owner =
          ownerValue === null
            ? null
            : String(ownerValue || formData.get("owner") || "").trim().toLowerCase();
        routingOverrideStatus.textContent = owner === null
          ? `Suppression du pin runtime pour ${{symbol}}...`
          : `Application du pin runtime ${{symbol}} -> ${{owner}}...`;
        try {{
          const response = await window.fetch("/api/routing/override", {{
            method: "POST",
            headers: {{
              "Content-Type": "application/json",
            }},
            body: JSON.stringify({{ symbol, owner }}),
          }});
          const payload = await response.json();
          if (!response.ok || !payload.ok) {{
            routingOverrideStatus.textContent = `Échec update runtime pin: ${{payload.error || response.status}}`;
            return;
          }}
          routingOverrideStatus.textContent = owner === null
            ? `Pin runtime supprimé pour ${{symbol}}. Rafraîchissement en cours...`
            : `Pin runtime appliqué: ${{symbol}} -> ${{payload.owner || owner}}. Rafraîchissement en cours...`;
          saveScrollPosition("system");
          window.setTimeout(() => {{
            window.location.hash = "#system";
            window.location.reload();
          }}, 450);
        }} catch (_error) {{
          routingOverrideStatus.textContent = "Erreur réseau pendant la mise à jour du pin runtime.";
        }}
      }}

      if (routingOverrideForm) {{
        routingOverrideForm.addEventListener("submit", (event) => {{
          event.preventDefault();
          void submitRoutingOverride(undefined);
        }});
      }}

      if (routingOverrideClearButton) {{
        routingOverrideClearButton.addEventListener("click", () => {{
          void submitRoutingOverride(null);
        }});
      }}

      const hashTab = (window.location.hash || "").replace("#", "");
      setTab(hashTab || body.dataset.defaultTab || "status", false);
      refreshFilterRows();
      restoreScrollPosition(hashTab || body.dataset.defaultTab || "status");
      window.addEventListener("beforeunload", () => {{
        saveScrollPosition();
      }});
      window.addEventListener("hashchange", () => {{
        const next = (window.location.hash || "").replace("#", "");
        if (validTabs.has(next)) {{
          setTab(next, false);
        }}
      }});

      if (Number.isFinite(refreshSeconds) && refreshSeconds > 0) {{
        window.setTimeout(() => {{
          saveScrollPosition();
          const target = `${{window.location.pathname}}${{window.location.search}}${{window.location.hash}}`;
          window.location.replace(target);
        }}, refreshSeconds * 1000);
      }}
    }})();
  </script>
</body>
</html>"""


def dashboard_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    return _control_center_html(
        supervisor,
        metrics,
        active_tab="status",
        title="TRIDENT Control Center",
        subtitle=(
            "Une seule interface pour piloter les trois pods : un onglet Status très lisible pour savoir "
            "si tout tourne bien, puis un onglet détail par pod et un onglet système pour les informations opératoires."
        ),
    )


def trades_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    return _control_center_html(
        supervisor,
        metrics,
        active_tab="activity",
        title="TRIDENT Trades",
        subtitle=(
            "Vue activity ouverte directement sur l'onglet exécution. Les détails par pod restent accessibles "
            "dans les autres onglets sans changer d'interface."
        ),
    )


def build_handler(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> type[BaseHTTPRequestHandler]:
    class TridentHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            routes: dict[str, Callable[[], dict[str, object]]] = {
                "/health": lambda: health_payload(supervisor),
                "/api/state": lambda: state_payload(supervisor, metrics),
                "/api/metrics": lambda: metrics_payload(supervisor, metrics),
                "/api/report": lambda: report_payload(supervisor, metrics),
            }
            html_routes: dict[str, Callable[[], str]] = {
                "/": lambda: dashboard_html(supervisor, metrics),
                "/dashboard": lambda: dashboard_html(supervisor, metrics),
                "/trades": lambda: trades_html(supervisor, metrics),
            }
            if self.path in html_routes:
                self._send_html(HTTPStatus.OK, html_routes[self.path]())
                return
            if self.path not in routes:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, routes[self.path]())

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/routing/override":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            symbol = str(payload.get("symbol", "")).strip().upper()
            if not symbol:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_symbol"})
                return
            owner_raw = payload.get("owner")
            if owner_raw in (None, ""):
                supervisor.clear_runtime_symbol_override(symbol)
                response_owner = None
                action = "cleared"
            else:
                try:
                    owner = PodName(str(owner_raw).strip().lower())
                except ValueError:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_owner", "valid_owners": [pod.value for pod in PodName]},
                    )
                    return
                supervisor.set_runtime_symbol_override(symbol, owner)
                response_owner = owner.value
                action = "set"
            metrics.refresh_from_supervisor(supervisor)
            snapshot = supervisor.snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "action": action,
                    "symbol": symbol,
                    "owner": response_owner,
                    "routing_overrides": snapshot.get("routing_overrides", {}),
                    "symbol_routing": next(
                        (
                            item
                            for item in snapshot.get("symbol_routing", [])
                            if isinstance(item, dict) and item.get("symbol") == symbol
                        ),
                        None,
                    ),
                },
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json_body(self) -> dict[str, object] | None:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                content_length = int(raw_length)
            except ValueError:
                return None
            if content_length <= 0:
                return None
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status: HTTPStatus, payload: str) -> None:
            body = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return TridentHandler


def run_http_server(
    host: str,
    port: int,
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> None:
    server = ThreadingHTTPServer((host, port), build_handler(supervisor, metrics))
    try:
        server.serve_forever()
    finally:
        server.server_close()
