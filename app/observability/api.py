from __future__ import annotations

import json
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from app.observability.metrics import MetricsRegistry
from app.reporting.multi_pod import build_runtime_report
from app.trident.supervisor import TridentSupervisor


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
    metrics.refresh_from_supervisor(supervisor)
    snapshot = supervisor.snapshot()
    snapshot["metrics"] = metrics.snapshot()
    snapshot["runtime_report"] = build_runtime_report(supervisor, metrics).to_dict()
    return snapshot


def metrics_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, int | float]:
    metrics.refresh_from_supervisor(supervisor)
    return metrics.snapshot()


def report_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, object]:
    return build_runtime_report(supervisor, metrics).to_dict()


def dashboard_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    snapshot = state_payload(supervisor, metrics)
    runtime_report = report_payload(supervisor, metrics)
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
            f"<td>{'yes' if bool(report['healthy']) else 'no'}</td>"
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

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
      </div>
      <h1>TRIDENT Supervisor Dashboard</h1>
      <p>Quick supervision view for ownership, pod allocation, conflicts, and regime transitions.</p>
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
        <h2>Runtime pod report</h2>
        <table>
          <thead>
            <tr><th>Pod</th><th>Healthy</th><th>Process</th><th>Positions</th><th>Open orders</th><th>Fills</th><th>Realized PnL</th><th>Unrealized PnL</th></tr>
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
        </div>
      </section>
    </div>
  </main>
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
