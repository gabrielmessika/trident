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
from app.reporting.multi_pod import build_runtime_report
from app.live.runtime_status import load_runtime_status, runtime_status_is_fresh
from app.trident.supervisor import TridentSupervisor


def _latest_snapshot_status(snapshot_dir: Path = Path("data/live_snapshots")) -> dict[str, object]:
    files = sorted(snapshot_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return {
            "status": "bad",
            "label": "No snapshots",
            "comment": "Aucun snapshot live trouve.",
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
                "label": "Pods healthy",
                "comment": f"{healthy_count}/{enabled_count} pods healthy.",
            }
        )
    elif healthy_count > 0:
        items.append(
            {
                "status": "warn",
                "label": "Pods degraded",
                "comment": f"{healthy_count}/{enabled_count} pods healthy.",
            }
        )
    else:
        items.append(
            {
                "status": "bad",
                "label": "Pods unhealthy",
                "comment": f"Aucun pod healthy sur {enabled_count}.",
            }
        )

    conflicts = int(snapshot["metrics"]["ownership_conflict_count"])
    items.append(
        {
            "status": "good" if conflicts == 0 else "bad",
            "label": "Ownership clean" if conflicts == 0 else "Ownership conflict",
            "comment": "Aucun conflit d'ownership." if conflicts == 0 else f"{conflicts} conflit(s) detecte(s).",
        }
    )

    fill_count = int(runtime_report.get("total_fill_count", 0))
    realized_pnl = float(runtime_report.get("realized_pnl_usd", 0.0))
    if fill_count > 0:
        items.append(
            {
                "status": "good",
                "label": "Trade activity",
                "comment": f"{fill_count} fills/trades observes, realized PnL {realized_pnl:.4f} USD.",
            }
        )
    else:
        items.append(
            {
                "status": "warn",
                "label": "No trade yet",
                "comment": "Le systeme tourne, mais aucune execution n'a encore ete observee.",
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
    fill_count = int(runtime_report.get("total_fill_count", 0))
    latest_snapshot = _latest_snapshot_status()
    if latest_snapshot["status"] == "bad":
        return "La collecte live semble interrompue ou trop ancienne. Verifie le collector et les logs API."
    if healthy < enabled:
        return "Le runtime est partiellement degrade: au moins un pod actif n'est pas healthy."
    if conflicts > 0:
        return "Le superviseur detecte des conflits d'ownership. Il faut les corriger avant d'augmenter le risque."
    if fill_count == 0:
        return "Le systeme parait sain et collecte bien les donnees, mais il n'a pas encore execute de trade."
    return "Le runtime parait sain, les donnees live arrivent, et une activite de trading recente est visible."


def health_payload(supervisor: TridentSupervisor) -> dict[str, object]:
    return {
        "status": "ok",
        "profile": supervisor.profile,
        "mode": supervisor.mode,
        "regime": supervisor.state.regime.value,
        "kill_switch_active": supervisor.kill_switch.is_active,
    }


def state_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, object]:
    supervisor.sync_pod_b()
    metrics.refresh_from_supervisor(supervisor)
    snapshot = supervisor.snapshot()
    snapshot = _merge_runtime_snapshot(snapshot)
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
    supervisor.sync_pod_b()
    metrics.refresh_from_supervisor(supervisor)
    return metrics.snapshot()


def report_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, object]:
    supervisor.sync_pod_b()
    snapshot = _merge_runtime_snapshot(supervisor.snapshot())
    return build_runtime_report(supervisor, metrics, runtime_snapshot=snapshot).to_dict()


def _merge_runtime_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    pod_a_runtime = _normalized_runtime_payload(load_runtime_status("logs/pod_a_live_status.json"))
    pod_c_runtime = _normalized_runtime_payload(load_runtime_status("logs/pod_c_live_status.json"))
    snapshot["pod_a_runtime"] = pod_a_runtime
    snapshot["pod_c_runtime"] = pod_c_runtime
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

    runtime_supervisor = None
    if runtime_status_is_fresh(pod_a_runtime):
        runtime_supervisor = pod_a_runtime.get("supervisor")
    if runtime_supervisor is None and runtime_status_is_fresh(pod_c_runtime):
        runtime_supervisor = pod_c_runtime.get("supervisor")
    if not isinstance(runtime_supervisor, dict):
        return snapshot

    for key in (
        "regime",
        "raw_regime",
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


def dashboard_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    snapshot = state_payload(supervisor, metrics)
    runtime_report = report_payload(supervisor, metrics)
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    refresh_seconds = 10
    status_items = _dashboard_status_items(snapshot, runtime_report)
    commentary = _dashboard_commentary(snapshot, runtime_report)
    recent_activity = _recent_activity_rows(snapshot)
    pods = snapshot["pods"]
    symbol_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['symbol']))}</td>"
            f"<td>{escape(str(item['owner'] or 'unassigned'))}</td>"
            "</tr>"
        )
        for item in snapshot["symbol_ownership"]
    )
    if not symbol_rows:
        symbol_rows = '<tr><td colspan="2">No symbol ownership yet</td></tr>'

    pod_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(name)}</td>"
            f"<td>{'yes' if bool(data['enabled']) else 'no'}</td>"
            f"<td>{', '.join(escape(str(symbol)) for symbol in data['owned_symbols']) or '-'}</td>"
            f"<td>{float(data['target_pct']):.2f}</td>"
            f"<td>{float(data['target_usd']):.2f}</td>"
            "</tr>"
        )
        for name, data in pods.items()
    )
    conflict_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(conflict['symbol']))}</td>"
            f"<td>{escape(str(conflict['requested_by']))}</td>"
            f"<td>{escape(str(conflict['owner']))}</td>"
            "</tr>"
        )
        for conflict in snapshot["ownership_conflicts"]
    )
    if not conflict_rows:
        conflict_rows = '<tr><td colspan="3">No ownership conflicts</td></tr>'

    regime_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['recorded_at']))}</td>"
            f"<td>{escape(str(item['previous_regime']))}</td>"
            f"<td>{escape(str(item['new_regime']))}</td>"
            f"<td>{float(item['snapshot']['adx']):.2f}</td>"
            f"<td>{float(item['snapshot']['atr_ratio']):.2f}</td>"
            "</tr>"
        )
        for item in snapshot["regime_history"]
    )
    if not regime_rows:
        regime_rows = '<tr><td colspan="5">No regime transitions recorded yet</td></tr>'

    metric_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(name))}</td>"
            f"<td>{escape(str(value))}</td>"
            "</tr>"
        )
        for name, value in snapshot["metrics"].items()
    )
    report_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(report['pod']))}</td>"
            f"<td>{_status_badge('good' if bool(report['healthy']) else 'bad', 'healthy' if bool(report['healthy']) else 'degraded')}</td>"
            f"<td>{escape(str(report['process_state'] or '-'))}</td>"
            f"<td>{int(report['position_count'])}</td>"
            f"<td>{int(report['open_order_count'])}</td>"
            f"<td>{int(report['total_fill_count'])}</td>"
            f"<td>{float(report['realized_pnl_usd']):.4f}</td>"
            f"<td>{float(report['total_unrealized_pnl_usd']):.4f}</td>"
            "</tr>"
        )
        for report in runtime_report["pods"]
    )
    status_rows = "".join(
        (
            "<div class='status-item'>"
            f"{_status_badge(str(item['status']), str(item['label']))}"
            f"<p>{escape(str(item['comment']))}</p>"
            "</div>"
        )
        for item in status_items
    )
    activity_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['timestamp']))}</td>"
            f"<td>{escape(str(item['pod']))}</td>"
            f"<td>{escape(str(item['symbol']))}</td>"
            f"<td>{escape(str(item['side']))}</td>"
            f"<td>{escape(str(item['event']))}</td>"
            f"<td>{'-' if item['price'] is None else f'{float(item['price']):.4f}'}</td>"
            f"<td>{'-' if item['notional_usd'] is None else f'{float(item['notional_usd']):.2f}'}</td>"
            f"<td>{_format_leverage(item.get('leverage'))}</td>"
            f"<td>{'-' if item['pnl_usd'] is None else f'{float(item['pnl_usd']):.4f}'}</td>"
            f"<td>{escape(str(item['comment']))}</td>"
            "</tr>"
        )
        for item in recent_activity
    )
    if not activity_rows:
        activity_rows = "<tr><td colspan='10'>Aucune execution recente visible. Les trades apparaitront ici des qu'un pod ecrira un trade close ou un fill recent.</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <title>TRIDENT Supervisor</title>
  <style>
    :root {{
      --bg: #f4f1ea;
      --panel: #fffdf8;
      --ink: #1f2a33;
      --muted: #6a7680;
      --line: #d7d0c4;
      --accent: #0f766e;
      --accent-soft: #d8f0ec;
      --good: #176b3a;
      --good-soft: #ddf5e5;
      --warn: #9a6700;
      --warn-soft: #fff0cc;
      --bad: #a12d2f;
      --bad-soft: #ffe1e1;
    }}
    body {{
      margin: 0;
      font-family: Georgia, "Iowan Old Style", serif;
      background: radial-gradient(circle at top, #fff8ea, var(--bg) 45%);
      color: var(--ink);
    }}
    main {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1, h2 {{
      margin: 0 0 12px;
    }}
    p {{
      color: var(--muted);
      margin: 0;
    }}
    .hero {{
      display: grid;
      gap: 12px;
      margin-bottom: 24px;
    }}
    .refresh-note {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      font-size: 0.92rem;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin: 20px 0 28px;
    }}
    .card, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 12px 24px rgba(31, 42, 51, 0.05);
    }}
    .card {{
      padding: 14px 16px;
    }}
    .card strong {{
      display: block;
      font-size: 1.4rem;
      margin-top: 6px;
    }}
    .chip {{
      display: inline-block;
      background: var(--accent-soft);
      color: var(--accent);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 0.9rem;
      margin-right: 8px;
    }}
    .layout {{
      display: grid;
      gap: 18px;
    }}
    .status-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .status-item {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 16px;
      box-shadow: 0 12px 24px rgba(31, 42, 51, 0.05);
    }}
    .status-item p {{
      margin-top: 10px;
      line-height: 1.4;
    }}
    .badge {{
      display: inline-block;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 0.86rem;
      font-weight: 700;
    }}
    .badge-good {{
      background: var(--good-soft);
      color: var(--good);
    }}
    .badge-warn {{
      background: var(--warn-soft);
      color: var(--warn);
    }}
    .badge-bad {{
      background: var(--bad-soft);
      color: var(--bad);
    }}
    .commentary {{
      margin-bottom: 18px;
      padding: 16px 18px;
      background: linear-gradient(135deg, #fff9e9 0%, #fffdf8 100%);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 12px 24px rgba(31, 42, 51, 0.05);
    }}
    section {{
      padding: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.96rem;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      vertical-align: bottom;
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
    .links {{
      margin-top: 16px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .links a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }}
  </style>
</head>
<body>
  <main>
    <div class="hero">
      <div>
        <span class="chip">Profile {escape(str(snapshot['profile']))}</span>
        <span class="chip">Mode {escape(str(snapshot['mode']))}</span>
        <span class="chip">Regime {escape(str(snapshot['regime']))}</span>
        <span class="chip">Auto-refresh {refresh_seconds}s</span>
      </div>
      <h1>TRIDENT Supervisor Dashboard</h1>
      <p>Quick supervision view for ownership, pod allocation, conflicts, and regime transitions.</p>
      <div class="refresh-note">
        <span>Last updated: {escape(refreshed_at)}</span>
        <a href="/dashboard">Refresh now</a>
      </div>
    </div>

    <div class="commentary">
      <h2>Runtime status</h2>
      <p>{escape(commentary)}</p>
    </div>

    <div class="status-grid">
      {status_rows}
    </div>

    <div class="summary">
      <div class="card"><span>Enabled pods</span><strong>{snapshot['metrics']['enabled_pod_count']}</strong></div>
      <div class="card"><span>Owned symbols</span><strong>{snapshot['metrics']['owned_symbol_count']}</strong></div>
      <div class="card"><span>Conflicts</span><strong>{snapshot['metrics']['ownership_conflict_count']}</strong></div>
      <div class="card"><span>Regime transitions</span><strong>{snapshot['regime_transition_count']}</strong></div>
      <div class="card"><span>Cash USD</span><strong>{float(snapshot['capital_plan']['cash_usd']):.2f}</strong></div>
      <div class="card"><span>Open positions</span><strong>{runtime_report['active_position_count']}</strong></div>
      <div class="card"><span>Open orders</span><strong>{runtime_report['active_open_order_count']}</strong></div>
      <div class="card"><span>Total fills</span><strong>{runtime_report['total_fill_count']}</strong></div>
      <div class="card"><span>Realized PnL USD</span><strong>{float(runtime_report['realized_pnl_usd']):.4f}</strong></div>
    </div>

    <div class="layout">
      <section>
        <h2>Recent trading activity</h2>
        <p>Les trades apparaissent ici a partir des journaux live de Pod A / Pod C et des recent fills de Pod B.</p>
        <table>
          <thead>
            <tr>{_table_header("Timestamp", "Heure de l'evenement tel qu'enregistre par le pod.")}{_table_header("Pod", "Pod responsable de l'evenement affiche.")}{_table_header("Symbol", "Marché ou coin concerné.")}{_table_header("Side", "Sens de l'ordre ou de la position: buy/sell ou long/short.")}{_table_header("Event", "Type d'evenement: fill, ouverture implicite ou fermeture de trade.")}{_table_header("Price", "Prix d'execution ou prix de sortie du trade.")}{_table_header("Notional USD", "Valeur notionnelle approximative de l'execution ou du trade.")}{_table_header("Leverage", "Levier configuré quand il est modélisé. Peut rester '-' pour les pods directionnels en dry-run.")}{_table_header("PnL USD", "PnL net si connu. Les fills Pod B n'ont pas de PnL ligne par ligne ici.")}{_table_header("Comment", "Raison de sortie, détail de fee ou autre contexte utile.")}</tr>
          </thead>
          <tbody>{activity_rows}</tbody>
        </table>
      </section>

      <section>
        <h2>Runtime pod report</h2>
        <table>
          <thead>
            <tr>{_table_header("Pod", "Nom logique du pod.")}{_table_header("Healthy", "Etat runtime du pod selon la fraîcheur du status et les checks connus.")}{_table_header("Process", "Etat du process ou runner associé.")}{_table_header("Positions", "Nombre de positions actuellement ouvertes.")}{_table_header("Open orders", "Nombre d'ordres ouverts actuellement suivis.")}{_table_header("Fills", "Nombre cumulé de fills ou trades observés pour ce pod.")}{_table_header("Realized PnL", "PnL réalisé cumulé du pod.")}{_table_header("Unrealized PnL", "PnL latent courant du pod, si applicable.")}</tr>
          </thead>
          <tbody>{report_rows}</tbody>
        </table>
      </section>

      <section>
        <h2>Pods</h2>
        <table>
          <thead>
            <tr><th>Pod</th><th>Enabled</th><th>Owned symbols</th><th>Target %</th><th>Target USD</th></tr>
          </thead>
          <tbody>{pod_rows}</tbody>
        </table>
      </section>

      <section>
        <h2>Symbol ownership</h2>
        <table>
          <thead>
            <tr><th>Symbol</th><th>Owner</th></tr>
          </thead>
          <tbody>{symbol_rows}</tbody>
        </table>
      </section>

      <section>
        <h2>Ownership conflicts</h2>
        <table>
          <thead>
            <tr><th>Symbol</th><th>Requested by</th><th>Owner</th></tr>
          </thead>
          <tbody>{conflict_rows}</tbody>
        </table>
      </section>

      <section>
        <h2>Regime history</h2>
        <table>
          <thead>
            <tr><th>Recorded at</th><th>Previous</th><th>New</th><th>ADX</th><th>ATR ratio</th></tr>
          </thead>
          <tbody>{regime_rows}</tbody>
        </table>
      </section>

      <section>
        <h2>Metrics</h2>
        <table>
          <thead>
            <tr><th>Name</th><th>Value</th></tr>
          </thead>
          <tbody>{metric_rows}</tbody>
        </table>
        <div class="links">
          <a href="/health">/health</a>
          <a href="/api/state">/api/state</a>
          <a href="/api/metrics">/api/metrics</a>
          <a href="/api/report">/api/report</a>
          <a href="/trades">/trades</a>
        </div>
      </section>
    </div>
  </main>
</body>
</html>"""


def trades_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    snapshot = state_payload(supervisor, metrics)
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    refresh_seconds = 10
    open_rows = _open_position_rows(snapshot)
    event_rows = _trade_event_rows(snapshot)

    open_position_rows = "".join(
        (
            "<tr data-filter-status='open' "
            f"data-filter-pod='{escape(str(item['pod']))}'>"
            f"<td>{escape(str(item['pod']))}</td>"
            f"<td>{escape(str(item['symbol']))}</td>"
            f"<td>{escape(str(item['side']))}</td>"
            f"<td>{escape(str(item['open_reason']))}</td>"
            f"<td>{'-' if item.get('entry_price') is None else f'{float(item['entry_price']):.6f}'}</td>"
            f"<td>{'-' if item.get('notional_usd') is None else f'{float(item['notional_usd']):.2f}'}</td>"
            f"<td>{_format_leverage(item.get('leverage'))}</td>"
            f"<td>{'-' if item.get('confidence') is None else f'{float(item['confidence']):.2f}'}</td>"
            f"<td>{'-' if item.get('stop_bps') is None else f'{float(item['stop_bps']):.1f}'}</td>"
            f"<td>{'-' if item.get('time_stop_hours') is None else str(item['time_stop_hours'])}</td>"
            f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
            "</tr>"
        )
        for item in open_rows
    )
    if not open_position_rows:
        open_position_rows = "<tr><td colspan='11'>Aucune position ouverte visible pour le moment.</td></tr>"

    trade_event_rows = "".join(
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
            f"<td>{'-' if item.get('entry_price') is None else f'{float(item['entry_price']):.6f}'}</td>"
            f"<td>{'-' if item.get('exit_price') is None else f'{float(item['exit_price']):.6f}'}</td>"
            f"<td>{'-' if item.get('notional_usd') is None else f'{float(item['notional_usd']):.2f}'}</td>"
            f"<td>{_format_leverage(item.get('leverage'))}</td>"
            f"<td>{'-' if item.get('pnl_usd') is None else f'{float(item['pnl_usd']):.4f}'}</td>"
            "</tr>"
        )
        for item in event_rows
    )
    if not trade_event_rows:
        trade_event_rows = "<tr><td colspan='12'>Aucun événement de trade récent visible pour le moment.</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <title>TRIDENT Trades</title>
  <style>
    :root {{
      --bg: #f7f3ea;
      --panel: rgba(255, 255, 255, 0.92);
      --text: #1f2a33;
      --muted: #5f6b73;
      --line: #d7d0c4;
      --accent: #0f766e;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top left, #f4ece1, #f7f3ea 45%, #efe7da 100%);
      color: var(--text);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
    }}
    main {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 28px 20px 48px;
    }}
    .hero, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 20px;
      box-shadow: 0 12px 24px rgba(31, 42, 51, 0.05);
      margin-bottom: 18px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      padding: 6px 12px;
      margin-right: 8px;
      border-radius: 999px;
      background: #f0e7d7;
      color: var(--muted);
      font-size: 0.86rem;
      font-weight: 600;
    }}
    .links {{
      margin-top: 14px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .links a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }}
    .filters {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    .filter-group {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .filter-label {{
      color: var(--muted);
      font-size: 0.9rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
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
    .filter-note {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      vertical-align: bottom;
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
    .is-hidden {{
      display: none;
    }}
  </style>
</head>
<body>
  <main>
    <div class="hero">
      <div>
        <span class="chip">Profile {escape(str(snapshot['profile']))}</span>
        <span class="chip">Mode {escape(str(snapshot['mode']))}</span>
        <span class="chip">Regime {escape(str(snapshot['regime']))}</span>
        <span class="chip">Auto-refresh {refresh_seconds}s</span>
      </div>
      <h1>TRIDENT Trades</h1>
      <p>Vue dédiée aux positions ouvertes et aux événements de trades récents, avec les raisons d'ouverture et de fermeture quand elles sont connues.</p>
      <p>Last updated: {escape(refreshed_at)}</p>
      <div class="filters" aria-label="Trade filters">
        <div class="filter-group">
          <span class="filter-label">Status</span>
          <button class="filter-chip is-active" type="button" data-filter-group="status" data-filter-value="open">Open</button>
          <button class="filter-chip is-active" type="button" data-filter-group="status" data-filter-value="closed">Closed</button>
        </div>
        <div class="filter-group">
          <span class="filter-label">Pods</span>
          <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_a">Pod A</button>
          <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_b">Pod B</button>
          <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_c">Pod C</button>
        </div>
      </div>
      <p class="filter-note">Les fills maker de Pod B sont classés visuellement dans “Open”, car ils représentent une activité d’inventaire en cours plutôt qu’une fermeture de trade directionnel.</p>
      <div class="links">
        <a href="/dashboard">/dashboard</a>
        <a href="/api/state">/api/state</a>
        <a href="/api/report">/api/report</a>
      </div>
    </div>

    <section>
      <h2>Open positions</h2>
      <table>
        <thead>
          <tr>{_table_header("Pod", "Pod qui porte actuellement la position.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens actuel de la position.")}{_table_header("Open reason", "Pourquoi la position a été ouverte: setup directionnel ou fill maker.")}{_table_header("Entry", "Prix d'entrée connu ou prix moyen de la position.")}{_table_header("Notional USD", "Valeur notionnelle actuelle ou cible en USD.")}{_table_header("Leverage", "Levier configuré quand il est disponible.")}{_table_header("Confidence", "Confiance du signal directionnel quand elle existe.")}{_table_header("Stop bps", "Distance du stop en basis points pour les pods directionnels.")}{_table_header("Time stop h", "Durée maximale de détention prévue pour les pods directionnels.")}{_table_header("Opened at", "Horodatage d'ouverture si connu.")}</tr>
        </thead>
        <tbody>{open_position_rows}</tbody>
      </table>
    </section>

    <section>
      <h2>Recent trade events</h2>
      <table>
        <thead>
          <tr>{_table_header("Timestamp", "Horodatage de l'événement.")}{_table_header("Pod", "Pod responsable.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens buy/sell ou long/short.")}{_table_header("Status", "closed pour un trade fermé, fill pour un fill Pod B.")}{_table_header("Open reason", "Pourquoi la position ou l'exécution a été initiée.")}{_table_header("Close reason", "Pourquoi le trade s'est fermé: stop_hit, time_stop, opposite_signal, etc.")}{_table_header("Entry", "Prix d'entrée si connu.")}{_table_header("Exit", "Prix de sortie si connu.")}{_table_header("Notional USD", "Valeur notionnelle concernée.")}{_table_header("Leverage", "Levier configuré quand il est disponible.")}{_table_header("PnL USD", "PnL net quand disponible.")}</tr>
        </thead>
        <tbody>{trade_event_rows}</tbody>
      </table>
    </section>
  </main>
  <script>
    (() => {{
      const active = {{
        status: new Set(["open", "closed"]),
        pod: new Set(["pod_a", "pod_b", "pod_c"]),
      }};
      const buttons = Array.from(document.querySelectorAll("[data-filter-group]"));
      const rows = Array.from(document.querySelectorAll("tr[data-filter-status][data-filter-pod]"));

      function refreshRows() {{
        rows.forEach((row) => {{
          const status = row.dataset.filterStatus;
          const pod = row.dataset.filterPod;
          const visible = active.status.has(status) && active.pod.has(pod);
          row.classList.toggle("is-hidden", !visible);
        }});
      }}

      buttons.forEach((button) => {{
        button.addEventListener("click", () => {{
          const group = button.dataset.filterGroup;
          const value = button.dataset.filterValue;
          const set = active[group];
          if (!set) return;
          if (set.has(value)) {{
            if (set.size === 1) return;
            set.delete(value);
            button.classList.remove("is-active");
          }} else {{
            set.add(value);
            button.classList.add("is-active");
          }}
          refreshRows();
        }});
      }});

      refreshRows();
    }})();
  </script>
</body>
</html>"""


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

        def log_message(self, format: str, *args: object) -> None:
            return

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
