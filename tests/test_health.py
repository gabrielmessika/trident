import base64
import http.client
import json
import os
import threading
import unittest
from dataclasses import replace
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.observability.api import (
    _global_trade_summary,
    _hip4_observation_health,
    _hip4_routes_enabled,
    _humanize_close_reason,
    _humanize_opportunity_reason,
    _open_position_rows,
    _pod_trade_summary,
    _recent_directional_opportunity_rows,
    _latest_snapshot_record,
    _humanize_setup_reason,
    _opportunity_reason_tooltip,
    _tail_csv_records,
    _tail_jsonl_records,
    build_handler,
    dashboard_html,
    health_payload,
    hip4_outcome_html,
    hip4_outcome_mainnet_payload,
    hip4_outcome_payload,
    metrics_payload,
    report_payload,
    state_payload,
    stats_html,
    system_html,
    trades_html,
)
from app.observability.metrics import MetricsRegistry
from app.settings import load_config
from app.trident.supervisor import TridentSupervisor


class HealthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config("config/trident.toml")
        self.supervisor = TridentSupervisor(
            config=config,
            profile="trident",
            mode="observation",
        )
        self.metrics = MetricsRegistry()

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        with patch.dict("os.environ", env or {}, clear=False):
            server = HTTPServer(
                ("127.0.0.1", 0),
                build_handler(self.supervisor, self.metrics),
            )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            server.server_address[1],
            timeout=5,
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response_body = response.read()
            return response.status, dict(response.getheaders()), response_body
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    @staticmethod
    def _basic_auth_header(username: str, password: str) -> dict[str, str]:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def test_health_payload(self) -> None:
        payload = health_payload(self.supervisor)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["profile"], "trident")
        self.assertEqual(payload["exchange_network"], "mainnet")

    def test_http_basic_auth_protects_ui_and_keeps_health_public(self) -> None:
        env = {
            "TRIDENT_UI_AUTH_USERNAME": "viewer",
            "TRIDENT_UI_AUTH_PASSWORD": "secret",
        }

        status, headers, body = self._api_request("GET", "/api/state", env=env)

        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(json.loads(body), {"error": "authentication_required"})

        health_status, _health_headers, health_body = self._api_request(
            "GET",
            "/health",
            env=env,
        )
        self.assertEqual(health_status, 200)
        self.assertEqual(json.loads(health_body)["status"], "ok")

        authed_status, _authed_headers, authed_body = self._api_request(
            "GET",
            "/api/state",
            env=env,
            headers=self._basic_auth_header("viewer", "secret"),
        )
        self.assertEqual(authed_status, 200)
        self.assertEqual(json.loads(authed_body)["profile"], "trident")

    def test_routing_override_post_is_disabled_by_default(self) -> None:
        body = json.dumps({"symbol": "SOL", "owner": "pod_a"}).encode("utf-8")

        status, _headers, response_body = self._api_request(
            "POST",
            "/api/routing/override",
            env={"TRIDENT_ROUTING_OVERRIDE_ENABLED": ""},
            headers={"Content-Type": "application/json"},
            body=body,
        )

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(response_body), {"error": "routing_override_disabled"})

    def test_routing_override_post_requires_configured_auth_when_enabled(self) -> None:
        body = json.dumps({"symbol": "SOL", "owner": "pod_a"}).encode("utf-8")

        status, _headers, response_body = self._api_request(
            "POST",
            "/api/routing/override",
            env={"TRIDENT_ROUTING_OVERRIDE_ENABLED": "true"},
            headers={"Content-Type": "application/json"},
            body=body,
        )

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(response_body), {"error": "authentication_not_configured"})

    def test_routing_override_post_requires_auth_when_enabled_and_credentials_exist(self) -> None:
        env = {
            "TRIDENT_ROUTING_OVERRIDE_ENABLED": "true",
            "TRIDENT_UI_AUTH_USERNAME": "operator",
            "TRIDENT_UI_AUTH_PASSWORD": "secret",
        }
        body = json.dumps({"symbol": "SOL", "owner": "pod_a"}).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        status, _headers, response_body = self._api_request(
            "POST",
            "/api/routing/override",
            env=env,
            headers=headers,
            body=body,
        )

        self.assertEqual(status, 401)
        self.assertEqual(json.loads(response_body), {"error": "authentication_required"})

        authed_status, _authed_headers, authed_body = self._api_request(
            "POST",
            "/api/routing/override",
            env=env,
            headers={**headers, **self._basic_auth_header("operator", "secret")},
            body=body,
        )
        payload = json.loads(authed_body)
        self.assertEqual(authed_status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["owner"], "pod_a")

    def test_tail_jsonl_records_reads_recent_window(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "journal.jsonl"
            records = [
                {"event_type": "signal", "record_index": index}
                for index in range(20)
            ]
            records.extend(
                {"event_type": "trade_close", "record_index": index}
                for index in range(20, 30)
            )
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            rows = _tail_jsonl_records(
                path,
                event_type="trade_close",
                limit=3,
                scan_lines=5,
            )

        self.assertEqual([row["record_index"] for row in rows], [29, 28, 27])

    def test_tail_csv_records_reads_recent_rows_with_header(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rows.csv"
            path.write_text(
                "ts,value\n" + "".join(f"2026-06-05T00:{index:02d}:00Z,{index}\n" for index in range(30)),
                encoding="utf-8",
            )

            rows = _tail_csv_records(path, limit=3)

        self.assertEqual([row["value"] for row in rows], ["27", "28", "29"])

    def test_latest_snapshot_record_uses_latest_merged_tail_group(self) -> None:
        def symbol_payload(symbol: str) -> dict[str, object]:
            return {
                "symbol": symbol,
                "price": 100.0,
                "ema_fast": 101.0,
                "ema_slow": 99.0,
                "vwap_distance_bps": 1.0,
                "structure_score": 0.2,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
            }

        def snapshot_payload(timestamp: str, symbols: list[str]) -> dict[str, object]:
            return {
                "timestamp": timestamp,
                "regime_snapshot": {
                    "ready": True,
                    "adx": 20.0,
                    "atr_ratio": 1.0,
                    "range_width_bps": 120.0,
                    "structure_score": 0.4,
                    "btc_impulse": True,
                },
                "symbols": [symbol_payload(symbol) for symbol in symbols],
                "cluster_regime_snapshots": {
                    "crypto": {
                        "ready": True,
                        "adx": 20.0,
                        "atr_ratio": 1.0,
                        "range_width_bps": 120.0,
                        "structure_score": 0.4,
                        "btc_impulse": True,
                    }
                },
            }

        with TemporaryDirectory() as tmpdir:
            snapshot_dir = Path(tmpdir)
            path = snapshot_dir / "2026-06-05.jsonl"
            payloads = [
                snapshot_payload("2026-06-05T00:00:00Z", ["OLD"]),
                snapshot_payload("2026-06-05T00:01:00Z", ["BTC"]),
                snapshot_payload("2026-06-05T00:01:00Z", ["XYZ:GOLD"]),
            ]
            path.write_text(
                "".join(json.dumps(payload) + "\n" for payload in payloads),
                encoding="utf-8",
            )

            record = _latest_snapshot_record(
                snapshot_dir=snapshot_dir,
                max_snapshot_age_seconds=10**9,
            )

        self.assertIsNotNone(record)
        self.assertEqual(record.timestamp, "2026-06-05T00:01:00Z")
        self.assertEqual(
            {item["symbol"] for item in record.symbols},
            {"BTC", "XYZ:GOLD"},
        )

    def test_hip4_routes_are_disabled_by_default_for_trident_app(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TRIDENT_APP_KIND": "trident",
                "TRIDENT_ENABLE_HIP4_OUTCOME": "",
            },
            clear=False,
        ):
            self.assertFalse(_hip4_routes_enabled())
        with patch.dict(
            "os.environ",
            {
                "TRIDENT_APP_KIND": "trident",
                "TRIDENT_ENABLE_HIP4_OUTCOME": "true",
            },
            clear=False,
        ):
            self.assertTrue(_hip4_routes_enabled())
        with patch.dict(
            "os.environ",
            {
                "TRIDENT_APP_KIND": "trident-hip4",
                "TRIDENT_ENABLE_HIP4_OUTCOME": "false",
            },
            clear=False,
        ):
            self.assertTrue(_hip4_routes_enabled())

    def test_state_payload_is_json_serializable(self) -> None:
        payload = state_payload(self.supervisor, self.metrics)
        self.assertIn("enabled_pods", payload)
        self.assertIn("pods", payload)
        self.assertIn("runtime_report", payload)
        self.assertIn("routing_overrides", payload)
        self.assertEqual(payload["exchange"]["network"], "mainnet")
        self.assertEqual(payload["metrics"]["trident_bootstrap_ready"], 1)
        self.assertGreaterEqual(payload["metrics"]["enabled_pod_count"], 1)
        self.assertEqual(payload["metrics"]["ownership_conflict_count"], 0)
        json.dumps(payload)

    def test_state_payload_merges_runtime_status_when_present(self) -> None:
        pod_a_runtime = {
            "pod": "pod_a",
            "updated_at": "2999-01-01T00:00:00Z",
            "supervisor": {
                "regime": "TrendExpansion",
                "raw_regime": "TrendExpansion",
                "allocations": {
                    "pod_a": 0.6,
                    "pod_b": 0.2,
                    "pod_c": 0.0,
                    "cash": 0.2,
                },
                "capital_plan": {
                    "regime": "TrendExpansion",
                    "total_equity_usd": 1000.0,
                    "cash_pct": 0.2,
                    "cash_usd": 200.0,
                    "pods": {
                        "pod_a": {
                            "target_pct": 0.6,
                            "target_usd": 600.0,
                        }
                    },
                },
                "symbol_ownership": [
                    {
                        "symbol": "ETH",
                        "owner": "pod_a",
                        "routing_mode": "dynamic_affinity",
                        "routing_reason": "best_affinity:pod_a (0.82)",
                    }
                ],
                "ownership_conflicts": [],
                "symbol_routing": [
                    {
                        "symbol": "ETH",
                        "owner": "pod_a",
                        "previous_owner": None,
                        "mode": "dynamic_affinity",
                        "reason": "best_affinity:pod_a (0.82)",
                        "candidate_pods": ["pod_a"],
                        "pod_scores": {"pod_a": 0.82},
                    }
                ],
                "pods": {
                    "pod_a": {
                        "enabled": True,
                        "candidate_symbols": ["BTC", "ETH"],
                        "desired_symbols": ["BTC", "ETH"],
                        "owned_symbols": ["ETH"],
                        "target_pct": 0.6,
                        "target_usd": 600.0,
                        "capped_by_pod_limit": False,
                    }
                },
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.2,
                    "range_width_bps": 150.0,
                    "structure_score": 0.5,
                    "btc_impulse": True,
                },
                "pending_regime": None,
                "pending_regime_count": 0,
                "regime_transition_count": 2,
                "regime_evaluation_count": 5,
                "regime_history": [],
                "pod_a_signal_preview": [{"symbol": "ETH"}],
                "pod_a_signal_review": [{"symbol": "ETH", "status": "filtered"}],
                "pod_c_signal_preview": [],
            },
        }
        pod_c_runtime = {
            "pod": "pod_c",
            "updated_at": "2999-01-01T00:00:01Z",
            "supervisor": {
                "regime": "TrendExpansion",
                "raw_regime": "TrendExpansion",
                "capital_plan": {
                    "regime": "TrendExpansion",
                    "total_equity_usd": 1000.0,
                    "cash_pct": 0.2,
                    "cash_usd": 200.0,
                    "pods": {
                        "pod_c": {
                            "target_pct": 0.15,
                            "target_usd": 150.0,
                        }
                    },
                },
                "symbol_ownership": [
                    {
                        "symbol": "SPX",
                        "owner": "pod_c",
                        "routing_mode": "dynamic_affinity",
                        "routing_reason": "best_affinity:pod_c (0.79)",
                    }
                ],
                "ownership_conflicts": [],
                "symbol_routing": [
                    {
                        "symbol": "SPX",
                        "owner": "pod_c",
                        "previous_owner": None,
                        "mode": "dynamic_affinity",
                        "reason": "best_affinity:pod_c (0.79)",
                        "candidate_pods": ["pod_c"],
                        "pod_scores": {"pod_c": 0.79},
                    }
                ],
                "pods": {
                    "pod_c": {
                        "enabled": True,
                        "candidate_symbols": ["SPX", "PAXG"],
                        "desired_symbols": ["SPX", "PAXG"],
                        "owned_symbols": ["SPX"],
                        "target_pct": 0.15,
                        "target_usd": 150.0,
                        "capped_by_pod_limit": False,
                    }
                },
                "regime_snapshot": {
                    "ready": True,
                    "adx": 31.0,
                    "atr_ratio": 1.1,
                    "range_width_bps": 145.0,
                    "structure_score": 0.52,
                    "btc_impulse": False,
                },
                "pending_regime": None,
                "pending_regime_count": 0,
                "regime_transition_count": 3,
                "regime_evaluation_count": 6,
                "regime_history": [],
                "pod_a_signal_preview": [],
                "pod_c_signal_preview": [{"symbol": "SPX"}],
                "pod_b_signal_review": [],
            },
        }
        def _api_runtime_loader(path):
            path_str = str(path)
            if "pod_a_live_status.json" in path_str:
                return pod_a_runtime
            if "pod_c_live_status.json" in path_str:
                return pod_c_runtime
            return None

        with patch(
            "app.observability.api.load_runtime_status",
            side_effect=_api_runtime_loader,
        ), patch(
            "app.observability.api._refresh_supervisor_from_latest_snapshot",
            return_value=False,
        ), patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=_api_runtime_loader,
        ):
            payload = state_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["regime"], "TrendExpansion")
        self.assertEqual(payload["capital_plan"]["regime"], "TrendExpansion")
        self.assertEqual(payload["regime_evaluation_count"], 6)
        self.assertEqual(payload["pod_a_signal_preview"][0]["symbol"], "ETH")
        self.assertEqual(payload["pod_a_signal_review"][0]["symbol"], "ETH")
        self.assertEqual(payload["pod_c_signal_preview"][0]["symbol"], "SPX")
        self.assertEqual(payload["runtime_report"]["regime"], "TrendExpansion")
        self.assertEqual(payload["runtime_report"]["cash_usd"], 200.0)
        ownership_by_symbol = {item["symbol"]: item for item in payload["symbol_ownership"]}
        routing_by_symbol = {item["symbol"]: item for item in payload["symbol_routing"]}
        self.assertEqual(ownership_by_symbol["ETH"]["routing_mode"], "dynamic_affinity")
        self.assertEqual(ownership_by_symbol["SPX"]["owner"], "pod_c")
        self.assertEqual(routing_by_symbol["ETH"]["mode"], "dynamic_affinity")
        self.assertEqual(routing_by_symbol["SPX"]["mode"], "dynamic_affinity")
        self.assertEqual(payload["pods"]["pod_a"]["owned_symbols"], ["ETH"])
        self.assertEqual(payload["pods"]["pod_c"]["owned_symbols"], ["SPX"])
        self.assertNotIn("pod_b_status", payload["pod_a_runtime"]["supervisor"])
        self.assertEqual(
            payload["pod_a_runtime"]["supervisor"]["source"],
            "api_merged_runtime_view",
        )
        self.assertEqual(
            payload["pod_c_runtime"]["supervisor"]["source"],
            "api_merged_runtime_view",
        )

    def test_state_payload_uses_live_journal_for_closed_trade_history_after_restart(self) -> None:
        supervisor = TridentSupervisor(
            config=load_config("config/trident.toml"),
            profile="trident",
            mode="live",
        )
        metrics = MetricsRegistry()
        pod_a_runtime = {
            "pod": "pod_a",
            "mode": "live",
            "updated_at": "2999-01-01T00:00:00Z",
            "process_state": "running",
            "open_positions": [],
            "report": {
                "closed_trade_count": 0,
                "realized_pnl_usd": 0.0,
                "closed_trade_log": [],
            },
        }

        def _runtime_loader(path):
            if str(path).endswith("pod_a_live_status.json"):
                return pod_a_runtime
            return None

        trade_close = {
            "event_type": "trade_close",
            "source": "pod_a_live_trade",
            "record_index": 9,
            "timestamp": "2026-05-27T16:40:00Z",
            "trade": {
                "symbol": "ETH",
                "side": "long",
                "setup": "trend_pullback_long",
                "entry_price": 3900.0,
                "exit_price": 3875.0,
                "target_notional_usd": 125.0,
                "gross_pnl_usd": -2.05,
                "fees_usd": 0.08,
                "pnl_usd": -2.13,
                "close_reason": "exchange_closed",
                "opened_at": "2026-05-27T16:29:00+00:00",
                "closed_at": "2026-05-27T16:40:00+00:00",
            },
        }

        previous_cwd = os.getcwd()
        with TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            (logs_dir / "pod_a_live.jsonl").write_text(
                json.dumps(trade_close) + "\n",
                encoding="utf-8",
            )
            os.chdir(tmpdir)
            try:
                with patch(
                    "app.observability.api.load_runtime_status",
                    side_effect=_runtime_loader,
                ), patch(
                    "app.observability.metrics.load_runtime_status",
                    side_effect=_runtime_loader,
                ), patch(
                    "app.reporting.multi_pod.load_runtime_status",
                    side_effect=_runtime_loader,
                ), patch(
                    "app.observability.api._refresh_supervisor_from_latest_snapshot",
                    return_value=False,
                ):
                    payload = state_payload(supervisor, metrics)
            finally:
                os.chdir(previous_cwd)

        pod_a_report = payload["pod_a_runtime"]["report"]
        self.assertEqual(pod_a_report["closed_trade_count"], 1)
        self.assertEqual(pod_a_report["closed_trade_log"][0]["close_reason"], "exchange_closed")
        self.assertEqual(payload["metrics"]["pod_a_closed_trade_count"], 1)
        pod_a_runtime_report = next(
            item for item in payload["runtime_report"]["pods"] if item["pod"] == "pod_a"
        )
        self.assertEqual(pod_a_runtime_report["total_fill_count"], 1)
        self.assertAlmostEqual(pod_a_runtime_report["realized_pnl_usd"], -2.13)

    def test_metrics_payload_refreshes_registry(self) -> None:
        payload = metrics_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["trident_bootstrap_ready"], 1)
        self.assertGreaterEqual(payload["enabled_pod_count"], 1)
        self.assertEqual(payload["owned_symbol_count"], 0)

    def test_metrics_payload_uses_nested_pod_b_runtime_report(self) -> None:
        self.supervisor.config.pod_b.enabled = True
        pod_b_runtime = {
            "pod": "pod_b",
            "updated_at": "2999-01-01T00:00:00Z",
            "process_state": "running",
            "open_positions": [{"symbol": "BTC", "unrealized_pnl_usd": 1.25}],
            "report": {
                "closed_trade_count": 14,
                "realized_pnl_usd": 12.12,
            },
            "supervisor": {
                "pods": {
                    "pod_b": {
                        "owned_symbols": ["AAVE", "BTC", "XRP"],
                    }
                },
                "pod_b_signal_preview": [],
            },
        }

        def _metrics_runtime_loader(path):
            path_str = str(path)
            if "pod_b_live_status.json" in path_str:
                return pod_b_runtime
            return None

        with patch(
            "app.observability.metrics.load_runtime_status",
            side_effect=_metrics_runtime_loader,
        ), patch.dict(
            "os.environ",
            {"TRIDENT_ENABLE_HIP4_OUTCOME": "true"},
            clear=False,
        ):
            payload = metrics_payload(self.supervisor, self.metrics)

        self.assertEqual(payload["pod_b_managed_symbol_count"], 3)
        self.assertEqual(payload["pod_b_total_position_count"], 1)
        self.assertEqual(payload["pod_b_total_fill_count"], 14)
        self.assertEqual(payload["pod_b_realized_pnl_usd"], 12.12)

    def test_metrics_payload_ignores_legacy_pod_b_runtime_for_trident_app(self) -> None:
        pod_b_runtime = {
            "pod": "pod_b",
            "pod_kind": "hip4_outcome_edge_pod",
            "updated_at": "2999-01-01T00:00:00Z",
            "process_state": "running",
            "managed_symbols": ["BTC", "HYPE"],
            "open_positions": [{"underlying": "BTC"}],
            "total_fill_count": 57,
            "total_position_count": 1,
            "total_open_order_count": 2,
            "total_unrealized_pnl_usd": -1.25,
            "report": {"realized_pnl_usd": -72.7395633},
        }

        def _metrics_runtime_loader(path):
            path_str = str(path)
            if "pod_b_live_status.json" in path_str:
                return pod_b_runtime
            return None

        with patch(
            "app.observability.metrics.load_runtime_status",
            side_effect=_metrics_runtime_loader,
        ), patch.dict(
            "os.environ",
            {
                "TRIDENT_APP_KIND": "trident",
                "TRIDENT_ENABLE_HIP4_OUTCOME": "",
            },
            clear=False,
        ):
            payload = metrics_payload(self.supervisor, self.metrics)

        self.assertEqual(payload["pod_b_managed_symbol_count"], 0)
        self.assertEqual(payload["pod_b_preview_count"], 0)
        self.assertEqual(payload["pod_b_process_running"], 0)
        self.assertEqual(payload["pod_b_total_position_count"], 0)
        self.assertEqual(payload["pod_b_total_open_order_count"], 0)
        self.assertEqual(payload["pod_b_total_fill_count"], 0)
        self.assertEqual(payload["pod_b_realized_pnl_usd"], 0.0)
        self.assertEqual(payload["pod_b_total_unrealized_pnl_usd"], 0.0)

    def test_metrics_payload_counts_hip4_replacement_as_pod_b(self) -> None:
        pod_a_runtime = {
            "pod": "pod_a",
            "updated_at": "2999-01-01T00:00:00Z",
            "process_state": "running",
            "report": {},
        }
        pod_b_runtime = {
            "pod": "pod_b",
            "pod_kind": "hip4_outcome_edge_pod",
            "strategy": "HIP4OutcomeEdgePod",
            "updated_at": "2999-01-01T00:00:01Z",
            "process_state": "running",
            "managed_symbols": ["BTC", "HYPE"],
            "open_positions": [
                {"underlying": "BTC", "cost_usdc": 4.75},
                {"underlying": "HYPE", "cost_usdc": 4.8},
            ],
            "total_fill_count": 2,
            "total_position_count": 2,
            "report": {},
        }
        pod_c_runtime = {
            "pod": "pod_c",
            "updated_at": "2999-01-01T00:00:02Z",
            "process_state": "running",
            "report": {},
        }

        def _metrics_runtime_loader(path):
            path_str = str(path)
            if "pod_a_live_status.json" in path_str:
                return pod_a_runtime
            if "pod_b_live_status.json" in path_str:
                return pod_b_runtime
            if "pod_c_live_status.json" in path_str:
                return pod_c_runtime
            return None

        with patch(
            "app.observability.metrics.load_runtime_status",
            side_effect=_metrics_runtime_loader,
        ), patch.dict(
            "os.environ",
            {"TRIDENT_ENABLE_HIP4_OUTCOME": "true"},
            clear=False,
        ):
            payload = metrics_payload(self.supervisor, self.metrics)

        self.assertEqual(payload["enabled_pod_count"], 3)
        self.assertEqual(payload["healthy_pod_count"], 3)
        self.assertEqual(payload["pod_b_process_running"], 1)
        self.assertEqual(payload["pod_b_managed_symbol_count"], 2)
        self.assertEqual(payload["pod_b_total_position_count"], 2)
        self.assertEqual(payload["pod_b_total_fill_count"], 2)

    def test_state_payload_sanitizes_nested_pod_b_supervisor(self) -> None:
        pod_b_runtime = {
            "pod": "pod_b",
            "updated_at": "2999-01-01T00:00:00Z",
            "process_state": "running",
            "managed_symbols": ["BTC"],
            "opening_symbols": ["BTC"],
            "open_positions": [],
            "report": {"closed_trade_count": 1, "realized_pnl_usd": 12.5},
            "supervisor": {
                "regime": "TrendExpansion",
                "raw_regime": "TrendExpansion",
                "enabled_pods": ["pod_a", "pod_b"],
                "ownership_conflicts": [],
                "symbol_ownership": [],
                "symbol_routing": [],
                "pods": {
                    "pod_b": {
                        "enabled": True,
                        "candidate_symbols": ["BTC"],
                        "desired_symbols": ["BTC"],
                        "owned_symbols": ["BTC"],
                        "target_pct": 0.2,
                        "target_usd": 200.0,
                        "capped_by_pod_limit": False,
                    }
                },
                "allocations": {"pod_a": 0.6, "pod_b": 0.2, "pod_c": 0.0, "cash": 0.2},
                "capital_plan": {
                    "regime": "TrendExpansion",
                    "total_equity_usd": 1000.0,
                    "cash_pct": 0.2,
                    "cash_usd": 200.0,
                    "pods": {
                        "pod_b": {
                            "target_pct": 0.2,
                            "target_usd": 200.0,
                        }
                    },
                },
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.2,
                    "range_width_bps": 150.0,
                    "structure_score": 0.5,
                    "btc_impulse": True,
                },
                "pending_regime": None,
                "pending_regime_count": 0,
                "regime_transition_count": 2,
                "regime_evaluation_count": 5,
                "regime_history": [],
                "pod_a_signal_preview": [],
                "pod_b_signal_preview": [{"symbol": "BTC"}],
                "pod_c_signal_preview": [],
                "pod_b_status": {
                    "pod": "pod_b",
                    "managed_symbols": ["BTC"],
                    "supervisor": {
                        "regime": "Range",
                    },
                },
            },
        }

        def _runtime_loader(path):
            path_str = str(path)
            if "pod_b_live_status.json" in path_str:
                return pod_b_runtime
            return None

        with patch(
            "app.observability.api.load_runtime_status",
            side_effect=_runtime_loader,
        ), patch(
            "app.observability.api._refresh_supervisor_from_latest_snapshot",
            return_value=False,
        ), patch(
            "app.observability.metrics.load_runtime_status",
            side_effect=_runtime_loader,
        ), patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=_runtime_loader,
        ), patch.dict(
            "os.environ",
            {"TRIDENT_ENABLE_HIP4_OUTCOME": "true"},
            clear=False,
        ):
            payload = state_payload(self.supervisor, self.metrics)

        self.assertNotIn("supervisor", payload["pod_b_status"])
        self.assertNotIn("pod_b_status", payload["pod_b_runtime"]["supervisor"])
        json.dumps(payload)

    def test_open_position_rows_preserve_runtime_market_data_and_trailing_fields(self) -> None:
        rows = _open_position_rows(
            {
                "pod_a_runtime": {
                    "open_positions": [
                        {
                            "symbol": "ETH",
                            "side": "long",
                            "setup": "trend_pullback_long",
                            "entry_price": 3100.0,
                            "current_price": 3150.0,
                            "target_notional_usd": 120.0,
                            "current_notional_usd": 121.94,
                            "unrealized_pnl_usd": 1.94,
                            "take_profit_bps": 120.0,
                            "break_even_trigger_bps": 45.0,
                            "trailing_activation_bps": 80.0,
                            "trailing_distance_bps": 30.0,
                            "best_price_seen": 3162.0,
                        }
                    ]
                }
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_price"], 3150.0)
        self.assertEqual(rows[0]["unrealized_pnl_usd"], 1.94)
        self.assertEqual(rows[0]["break_even_trigger_bps"], 45.0)
        self.assertEqual(rows[0]["trailing_activation_bps"], 80.0)
        self.assertEqual(rows[0]["trailing_distance_bps"], 30.0)
        self.assertEqual(rows[0]["best_price_seen"], 3162.0)

    def test_trade_summary_aggregates_by_coin_side_and_pnl(self) -> None:
        pod_a_rows = _pod_trade_summary(
            {
                "pod": "pod_a",
                "report": {
                    "closed_trade_log": [
                        {"symbol": "ETH", "side": "long", "pnl_usd": 1.5},
                        {"symbol": "ETH", "side": "short", "pnl_usd": -0.4},
                    ],
                },
                "open_positions": [
                    {"symbol": "ETH", "side": "long", "unrealized_pnl_usd": 0.25},
                ],
            },
            pod="pod_a",
        )
        pod_b_rows = _pod_trade_summary(
            {
                "pod": "pod_b",
                "pod_kind": "hip4_outcome_edge_pod",
                "settled_positions": [
                    {"underlying": "HYPE", "side": "BUY_NO", "estimated_pnl_usdc": 2.156},
                    {"underlying": "BTC", "side": "BUY_YES", "estimated_pnl_usdc": 5.23},
                ],
                "open_positions": [
                    {"underlying": "HYPE", "side": "BUY_NO", "estimated_pnl_usdc": 0.0},
                ],
            },
            pod="pod_b",
        )

        eth_long = next(row for row in pod_a_rows if row["symbol"] == "ETH" and row["side"] == "long")
        self.assertEqual(eth_long["closed_trade_count"], 1)
        self.assertEqual(eth_long["open_position_count"], 1)
        self.assertEqual(eth_long["realized_pnl_usd"], 1.5)
        self.assertEqual(eth_long["unrealized_pnl_usd"], 0.25)

        hype_short = next(row for row in pod_b_rows if row["symbol"] == "HYPE" and row["side"] == "short")
        self.assertEqual(hype_short["closed_trade_count"], 1)
        self.assertEqual(hype_short["open_position_count"], 1)
        self.assertAlmostEqual(float(hype_short["total_pnl_usd"]), 2.156)

        global_rows = _global_trade_summary({"pod_a": pod_a_rows, "pod_b": pod_b_rows})
        global_eth = next(row for row in global_rows if row["symbol"] == "ETH" and row["side"] == "long")
        self.assertEqual(global_eth["pods"], ["pod_a"])
        self.assertEqual(global_eth["closed_trade_count"], 1)

    def test_metrics_payload_uses_runtime_status_when_present(self) -> None:
        pod_a_runtime = {
            "pod": "pod_a",
            "process_state": "running",
            "report": {
                "closed_trade_count": 2,
                "realized_pnl_usd": 4.5,
            },
            "supervisor": {
                "regime_transition_count": 3,
                "regime_evaluation_count": 7,
                "pod_a_signal_preview": [{"symbol": "BTC"}, {"symbol": "ETH"}],
            },
            "updated_at": "2999-01-01T00:00:00Z",
        }
        pod_c_runtime = {
            "pod": "pod_c",
            "process_state": "running",
            "report": {
                "closed_trade_count": 1,
                "realized_pnl_usd": 1.25,
            },
            "supervisor": {
                "regime_transition_count": 4,
                "regime_evaluation_count": 9,
                "pod_a_signal_preview": [],
                "pod_c_signal_preview": [{"symbol": "SPX"}],
            },
            "updated_at": "2999-01-01T00:00:01Z",
        }
        def _metrics_runtime_loader(path):
            path_str = str(path)
            if "pod_a_live_status.json" in path_str:
                return pod_a_runtime
            if "pod_c_live_status.json" in path_str:
                return pod_c_runtime
            return None

        with patch(
            "app.observability.metrics.load_runtime_status",
            side_effect=_metrics_runtime_loader,
        ):
            payload = metrics_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["pod_a_process_running"], 1)
        self.assertEqual(payload["pod_a_closed_trade_count"], 2)
        self.assertEqual(payload["pod_a_realized_pnl_usd"], 4.5)
        self.assertEqual(payload["pod_a_preview_count"], 2)
        self.assertEqual(payload["regime_evaluation_count"], 9)

    def test_report_payload_exposes_multi_pod_runtime_report(self) -> None:
        with patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=[None, None, None, None, None, None],
        ):
            payload = report_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["profile"], "trident")
        self.assertGreaterEqual(payload["enabled_pod_count"], 1)
        self.assertIn("pods", payload)
        self.assertIn("services", payload)
        self.assertEqual(len(payload["pods"]), 3)
        self.assertEqual(len(payload["services"]), 2)
        self.assertEqual(payload["active_position_count"], 0)

    def test_report_payload_uses_runtime_status_when_present(self) -> None:
        pod_a_runtime = {
            "pod": "pod_a",
            "process_state": "running",
            "updated_at": "2999-01-01T00:00:00Z",
            "report": {
                "closed_trade_count": 3,
                "realized_pnl_usd": 6.25,
            },
            "supervisor": {
                "regime": "TrendExpansion",
                "enabled_pods": ["pod_a"],
                "ownership_conflicts": [],
                "capital_plan": {
                    "regime": "TrendExpansion",
                    "cash_usd": 250.0,
                    "pods": {
                        "pod_a": {
                            "target_pct": 0.75,
                            "target_usd": 750.0,
                        }
                    },
                },
                "pods": {
                    "pod_a": {
                        "owned_symbols": ["BTC", "ETH"],
                        "target_pct": 0.75,
                        "target_usd": 750.0,
                    }
                },
                "pod_a_signal_preview": [{"symbol": "ETH"}],
                "pod_c_signal_preview": [],
            },
        }
        pod_c_runtime = {
            "pod": "pod_c",
            "process_state": "running",
            "updated_at": "2999-01-01T00:00:01Z",
            "report": {
                "closed_trade_count": 1,
                "realized_pnl_usd": 1.0,
            },
            "supervisor": {
                "regime": "TrendExpansion",
                "enabled_pods": ["pod_a", "pod_c"],
                "ownership_conflicts": [],
                "capital_plan": {
                    "regime": "TrendExpansion",
                    "cash_usd": 250.0,
                    "pods": {
                        "pod_c": {
                            "target_pct": 0.15,
                            "target_usd": 150.0,
                        }
                    },
                },
                "pods": {
                    "pod_c": {
                        "owned_symbols": ["SPX"],
                        "target_pct": 0.15,
                        "target_usd": 150.0,
                    }
                },
                "pod_a_signal_preview": [],
                "pod_c_signal_preview": [{"symbol": "SPX"}],
            },
        }
        with patch(
            "app.observability.api.load_runtime_status",
            side_effect=[pod_a_runtime, pod_c_runtime, None],
        ), patch(
            "app.observability.api._refresh_supervisor_from_latest_snapshot",
            return_value=False,
        ), patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=[pod_a_runtime, pod_c_runtime, None, None, None, None],
        ):
            payload = report_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["regime"], "TrendExpansion")
        pod_a = next(item for item in payload["pods"] if item["pod"] == "pod_a")
        pod_c = next(item for item in payload["pods"] if item["pod"] == "pod_c")
        self.assertTrue(pod_a["healthy"])
        self.assertEqual(pod_a["process_state"], "running")
        self.assertEqual(pod_a["preview_count"], 1)
        self.assertEqual(pod_a["total_fill_count"], 3)
        self.assertEqual(pod_a["realized_pnl_usd"], 6.25)
        self.assertEqual(pod_a["target_pct"], 0.75)
        self.assertEqual(pod_a["target_usd"], 750.0)
        self.assertEqual(pod_c["owned_symbols"], ["SPX"])
        self.assertEqual(pod_c["preview_count"], 1)
        self.assertEqual(pod_c["target_pct"], 0.15)
        self.assertEqual(pod_c["target_usd"], 150.0)
        self.assertEqual(payload["cash_usd"], 250.0)
        self.assertEqual(payload["enabled_service_count"], 2)

    def test_dashboard_html_contains_supervision_sections(self) -> None:
        html = dashboard_html(self.supervisor, self.metrics)
        self.assertIn("TRIDENT Control Center", html)
        self.assertIn(">Status</button>", html)
        self.assertIn(">Pod A</button>", html)
        self.assertIn(">Pod C</button>", html)
        self.assertIn(">Activity</button>", html)
        self.assertIn("À faire maintenant", html)
        self.assertIn("Performance par coin", html)
        self.assertIn("Positions ouvertes", html)
        self.assertIn("Signaux et filtres", html)
        self.assertIn("Opportunités récentes", html)
        self.assertIn("Cause", html)
        self.assertIn("Prix ref", html)
        self.assertIn("Leverage", html)
        self.assertIn("Prix courant", html)
        self.assertIn("Marge", html)
        self.assertIn("Prix TP", html)
        self.assertIn("Prix SL", html)
        self.assertIn("Trailing TP", html)
        self.assertIn("tooltip-bubble", html)
        self.assertIn("band-", html)
        self.assertIn("status-card-", html)
        self.assertIn("pod-card-", html)
        self.assertIn("<dt>PnL réalisé</dt>", html)
        self.assertIn("<dt>PnL latent</dt>", html)
        self.assertIn('data-refresh-seconds="10"', html)
        self.assertIn("network-chip", html)
        self.assertIn(">Mainnet</span>", html)
        self.assertIn("sessionStorage", html)
        self.assertIn("restoreScrollPosition(initialTab)", html)
        self.assertIn("saveScrollPosition();", html)
        self.assertIn("window.location.replace(target)", html)
        self.assertIn("buttons.map((button) => button.dataset.tabButton)", html)
        self.assertIn("Auto-refresh 10s", html)
        self.assertIn("Réseau A/C mainnet", html)
        self.assertIn("Last updated:", html)
        self.assertIn("/api/state", html)
        self.assertIn("/api/report", html)
        self.assertIn("/stats", html)
        self.assertIn("/system", html)
        self.assertIn("/trades", html)
        self.assertIn('data-tab-panel="status"', html)
        self.assertIn('data-tab-panel="pod_a"', html)
        self.assertIn('data-tab-panel="pod_c"', html)
        self.assertIn('data-tab-panel="activity"', html)
        self.assertNotIn('data-tab-panel="stats"', html)
        self.assertNotIn('data-tab-panel="system"', html)
        self.assertNotIn('data-tab-panel="observation"', html)
        self.assertNotIn(">Observation</button>", html)
        self.assertNotIn(">Pod B</button>", html)
        self.assertNotIn("Pod B", html)
        self.assertNotIn("HIP-4", html)
        self.assertNotIn("/hip4-outcome", html)
        self.assertNotIn('data-tab-panel="pod_b"', html)
        self.assertNotIn("Performance globale par coin", html)
        self.assertNotIn("pod breakout directionnel", html)

    def test_dedicated_stats_and_system_pages_are_renderable(self) -> None:
        stats = stats_html(self.supervisor, self.metrics)
        system = system_html(self.supervisor, self.metrics)
        self.assertIn("TRIDENT Stats", stats)
        self.assertIn('data-tab-panel="status"', stats)
        self.assertNotIn("Pod B", stats)
        self.assertNotIn("HIP-4", stats)
        self.assertIn("TRIDENT System", system)
        self.assertIn('data-tab-panel="status"', system)
        self.assertNotIn("Pod B", system)
        self.assertNotIn("HIP-4", system)

    def test_hip4_outcome_page_and_payload_are_renderable(self) -> None:
        payload = hip4_outcome_payload()
        self.assertEqual(payload["pod"], "hip4_outcome_edge_pod")
        self.assertIn("opportunities", payload)
        self.assertIn("settlements", payload)
        self.assertIn("realized_pnl_usd", payload)
        self.assertIn("fees_usd", payload)
        self.assertIn("gross_pnl_usd", payload)
        self.assertIn("blocked_opportunity_slices", payload)
        self.assertIn("reference_divergence_guard", payload)
        self.assertIn("market_observations", payload)
        self.assertIn("market_observation_health", payload)

        html = hip4_outcome_html(self.supervisor, self.metrics)
        self.assertIn("HIP-4 Outcome Experimental", html)
        self.assertIn("/api/hip4-outcome", html)
        self.assertIn("HIP4 UI", html)
        self.assertNotIn("/dashboard", html)
        self.assertNotIn("/trades", html)
        self.assertIn('data-hip4-tab="dashboard"', html)
        self.assertIn('data-hip4-panel="dashboard"', html)
        self.assertIn('data-hip4-tab="details"', html)
        self.assertIn('data-hip4-panel="details"', html)
        self.assertIn("Signal court terme", html)
        self.assertNotIn('data-hip4-tab="overview"', html)
        self.assertNotIn("Vue générale", html)
        self.assertIn('data-hip4-tab="observation"', html)
        self.assertIn('data-hip4-panel="observation"', html)
        self.assertIn("Observation HIP-4", html)
        self.assertIn("Marchés observés", html)
        self.assertIn("health-pill", html)
        self.assertIn("status-dot", html)
        self.assertIn("Opportunités récentes", html)
        self.assertIn("Realized PnL", html)
        self.assertIn("Gross/Fees", html)
        self.assertIn("Net PnL", html)
        self.assertIn("Performance par coin", html)
        self.assertIn("PnL visible", html)
        self.assertIn("Settlements paper", html)

    def test_hip4_mainnet_payload_does_not_reuse_paper_logs_when_observer_is_off(self) -> None:
        with TemporaryDirectory() as tmpdir:
            previous_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                paper_logs = Path("logs/hip4_outcome_mainnet_paper")
                paper_logs.mkdir(parents=True)
                (paper_logs / "market_observations.jsonl").write_text(
                    json.dumps(
                        {
                            "ts": "2999-01-01T00:00:00Z",
                            "class_name": "priceBinary",
                            "support_status": "trading_supported",
                            "support_reason": "price_binary_supported",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

                payload = hip4_outcome_mainnet_payload()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(payload["logs_dir"], "logs/hip4_outcome_mainnet")
        self.assertEqual(payload["process_state"], "off")
        health = payload["market_observation_health"]
        self.assertEqual(health["label"], "off")
        self.assertEqual(health["count"], 0)

    def test_hip4_observation_unknown_without_book_error_is_watch_only(self) -> None:
        health = _hip4_observation_health(
            [
                {
                    "ts": "2026-05-26T15:00:00Z",
                    "class_name": "unknown",
                    "support_status": "observe_only",
                    "support_reason": "unsupported_outcome_class",
                    "books": {
                        "yes": {"coin": "#1040", "bid": 0.1, "ask": 0.2},
                        "no": {"coin": "#1041", "bid": 0.8, "ask": 0.9},
                    },
                },
                {
                    "ts": "2026-05-26T15:00:00Z",
                    "class_name": "priceBinary",
                    "support_status": "trading_supported",
                    "support_reason": "price_binary_supported",
                    "underlying": "BTC",
                }
            ]
        )

        self.assertEqual(health["tone"], "warn")
        self.assertEqual(health["label"], "watch-only")
        self.assertEqual(health["reason"], "marchés non supportés observés")
        self.assertEqual(health["book_error_count"], 0)
        self.assertEqual(health["unknown_count"], 1)
        self.assertEqual(health["price_binary_count"], 1)
        self.assertEqual(health["by_support_status"], {"observe_only": 1, "trading_supported": 1})
        self.assertEqual(health["by_tone"], {"good": 1, "warn": 1})

    def test_hip4_observation_book_error_stays_bad(self) -> None:
        health = _hip4_observation_health(
            [
                {
                    "ts": "2026-05-26T15:00:00Z",
                    "class_name": "unknown",
                    "support_status": "observe_only",
                    "support_reason": "unsupported_outcome_class",
                    "books": {
                        "yes": {"coin": "#1040", "error": "HTTP 500"},
                        "no": {"coin": "#1041", "bid": 0.8, "ask": 0.9},
                    },
                }
            ]
        )

        self.assertEqual(health["tone"], "bad")
        self.assertEqual(health["label"], "à investiguer")
        self.assertEqual(health["reason"], "erreurs book observées")
        self.assertEqual(health["book_error_count"], 1)
        self.assertEqual(health["unknown_count"], 1)

    def test_hip4_observation_stale_or_empty_stays_bad(self) -> None:
        stale = _hip4_observation_health(
            [
                {
                    "ts": "2026-05-26T15:00:00Z",
                    "class_name": "priceBinary",
                    "support_status": "trading_supported",
                    "support_reason": "price_binary_supported",
                }
            ],
            fresh=False,
        )
        empty = _hip4_observation_health([])

        self.assertEqual(stale["tone"], "bad")
        self.assertEqual(stale["label"], "stale")
        self.assertEqual(empty["tone"], "bad")
        self.assertEqual(empty["label"], "aucune observation")

    def test_trades_html_contains_trade_sections(self) -> None:
        html = trades_html(self.supervisor, self.metrics)
        self.assertIn("TRIDENT Trades", html)
        self.assertIn('data-default-tab="activity"', html)
        self.assertIn(">Activity</button>", html)
        self.assertIn("Open positions", html)
        self.assertIn("Recent trade events", html)
        self.assertIn("Open reason", html)
        self.assertIn("Close reason", html)
        self.assertIn("Current/Exit", html)
        self.assertIn("Prix SL", html)
        self.assertIn("Prix TP", html)
        self.assertIn("data-filter-group=\"status\"", html)
        self.assertIn("Open</button>", html)
        self.assertIn("Closed</button>", html)
        self.assertIn("Pod A</button>", html)
        self.assertIn("Pod C</button>", html)
        self.assertNotIn("Pod B", html)
        self.assertNotIn("HIP-4", html)
        self.assertIn("tooltip-bubble", html)

    def test_reason_labels_are_humanized(self) -> None:
        self.assertIn("BOS retest long", _humanize_setup_reason("bos_retest_long"))
        self.assertIn("take profit", _humanize_close_reason("take_profit_hit").lower())
        self.assertIn("signal oppose", _humanize_close_reason("opposite_signal").lower())
        self.assertIn(
            "Blocage SL grace live",
            _humanize_opportunity_reason("stop_grace_exchange_sl_mismatch:setup=x"),
        )
        self.assertIn(
            "SL catastrophe",
            _opportunity_reason_tooltip("stop_grace_exchange_sl_mismatch:setup=x"),
        )

    def test_directional_opportunity_rows_explain_live_skip_reason(self) -> None:
        record = {
            "event_type": "signal",
            "timestamp": "2026-05-29T10:00:00Z",
            "signal": {
                "symbol": "ETH",
                "side": "long",
                "setup": "trend_pullback_long",
                "confidence": 0.72,
                "reason_summary": "pullback validé",
                "risk": {
                    "accepted": True,
                    "reason": "accepted",
                    "target_notional_usd": 125.0,
                    "margin_usd": 25.0,
                    "effective_leverage": 5.0,
                    "expected_loss_usd": 1.25,
                    "invalidation_price": 3800.0,
                    "stop_bps": 40.0,
                    "take_profit_bps": 80.0,
                },
                "execution": {
                    "opened": False,
                    "skipped_open": True,
                    "skip_reason": "stop_grace_exchange_sl_mismatch:setup=trend_pullback_long,grace_minutes=45",
                    "open_fills": [],
                    "close_fills": [],
                },
            },
            "symbol_snapshot": {"symbol": "ETH", "price": 3900.0},
        }
        previous_cwd = os.getcwd()
        with TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            (logs_dir / "pod_a_live.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )
            os.chdir(tmpdir)
            try:
                rows = _recent_directional_opportunity_rows({}, pod="pod_a")
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "skipped")
        self.assertEqual(rows[0]["cause_label"], "Blocage SL grace live")
        self.assertIn("SL catastrophe", rows[0]["cause_tooltip"])
        self.assertEqual(rows[0]["stop_price"], 3800.0)
        self.assertAlmostEqual(rows[0]["take_profit_price"], 3931.2)

    def test_env_override_enables_pod_b_for_supervisor(self) -> None:
        with patch.dict("os.environ", {"TRIDENT_ENABLE_POD_B": "true"}, clear=False):
            config = load_config("config/trident.toml")
        supervisor = TridentSupervisor(
            config=config,
            profile="trident",
            mode="observation",
        )
        with patch(
            "app.observability.api.load_runtime_status",
            side_effect=[None, None, None],
        ):
            payload = state_payload(supervisor, MetricsRegistry())
        self.assertIn("pod_b", payload["enabled_pods"])
        self.assertTrue(payload["pods"]["pod_b"]["enabled"])


if __name__ == "__main__":
    unittest.main()
