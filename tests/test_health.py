import json
import unittest
from unittest.mock import patch

from app.observability.api import (
    dashboard_html,
    health_payload,
    metrics_payload,
    report_payload,
    state_payload,
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

    def test_health_payload(self) -> None:
        payload = health_payload(self.supervisor)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["profile"], "trident")

    def test_state_payload_is_json_serializable(self) -> None:
        payload = state_payload(self.supervisor, self.metrics)
        self.assertIn("enabled_pods", payload)
        self.assertIn("pods", payload)
        self.assertIn("runtime_report", payload)
        self.assertEqual(payload["metrics"]["trident_bootstrap_ready"], 1)
        self.assertEqual(payload["metrics"]["enabled_pod_count"], 1)
        self.assertEqual(payload["metrics"]["ownership_conflict_count"], 0)
        json.dumps(payload)

    def test_state_payload_merges_runtime_status_when_present(self) -> None:
        with patch(
            "app.observability.api.load_runtime_status",
            side_effect=[
                {
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
                            "pods": {},
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
                        "pod_c_signal_preview": [],
                    },
                },
                None,
            ],
        ):
            payload = state_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["regime"], "TrendExpansion")
        self.assertEqual(payload["capital_plan"]["regime"], "TrendExpansion")
        self.assertEqual(payload["regime_evaluation_count"], 5)
        self.assertEqual(payload["pod_a_signal_preview"][0]["symbol"], "ETH")
        self.assertEqual(payload["runtime_report"]["regime"], "TrendExpansion")
        self.assertEqual(payload["runtime_report"]["cash_usd"], 200.0)
        self.assertNotIn("pod_b_status", payload["pod_a_runtime"]["supervisor"])
        self.assertEqual(
            payload["pod_a_runtime"]["supervisor"]["source"],
            "api_merged_runtime_view",
        )

    def test_metrics_payload_refreshes_registry(self) -> None:
        payload = metrics_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["trident_bootstrap_ready"], 1)
        self.assertEqual(payload["enabled_pod_count"], 1)
        self.assertEqual(payload["owned_symbol_count"], 4)

    def test_metrics_payload_uses_runtime_status_when_present(self) -> None:
        with patch(
            "app.observability.metrics.load_runtime_status",
            side_effect=[
                {
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
                },
                None,
            ],
        ):
            payload = metrics_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["pod_a_process_running"], 1)
        self.assertEqual(payload["pod_a_closed_trade_count"], 2)
        self.assertEqual(payload["pod_a_realized_pnl_usd"], 4.5)
        self.assertEqual(payload["pod_a_preview_count"], 2)
        self.assertEqual(payload["regime_evaluation_count"], 7)

    def test_report_payload_exposes_multi_pod_runtime_report(self) -> None:
        payload = report_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["profile"], "trident")
        self.assertEqual(payload["enabled_pod_count"], 1)
        self.assertIn("pods", payload)
        self.assertEqual(len(payload["pods"]), 3)
        self.assertEqual(payload["active_position_count"], 0)

    def test_report_payload_uses_runtime_status_when_present(self) -> None:
        runtime_payload = {
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
        with patch(
            "app.observability.api.load_runtime_status",
            side_effect=[runtime_payload, None],
        ), patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=[runtime_payload, None],
        ):
            payload = report_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["regime"], "TrendExpansion")
        pod_a = next(item for item in payload["pods"] if item["pod"] == "pod_a")
        self.assertTrue(pod_a["healthy"])
        self.assertEqual(pod_a["process_state"], "running")
        self.assertEqual(pod_a["preview_count"], 1)
        self.assertEqual(pod_a["total_fill_count"], 3)
        self.assertEqual(pod_a["realized_pnl_usd"], 6.25)
        self.assertEqual(pod_a["target_pct"], 0.75)
        self.assertEqual(pod_a["target_usd"], 750.0)
        self.assertEqual(payload["cash_usd"], 250.0)

    def test_dashboard_html_contains_supervision_sections(self) -> None:
        html = dashboard_html(self.supervisor, self.metrics)
        self.assertIn("TRIDENT Supervisor Dashboard", html)
        self.assertIn("Runtime status", html)
        self.assertIn("Symbol ownership", html)
        self.assertIn("Ownership conflicts", html)
        self.assertIn("Runtime pod report", html)
        self.assertIn("Recent trading activity", html)
        self.assertIn("Les trades apparaitront ici", html)
        self.assertIn("Leverage", html)
        self.assertIn("Levier configuré", html)
        self.assertIn("tooltip-bubble", html)
        self.assertIn('http-equiv="refresh"', html)
        self.assertIn("Auto-refresh 10s", html)
        self.assertIn("Last updated:", html)
        self.assertIn("/api/state", html)
        self.assertIn("/api/report", html)
        self.assertIn("/trades", html)

    def test_trades_html_contains_trade_sections(self) -> None:
        html = trades_html(self.supervisor, self.metrics)
        self.assertIn("TRIDENT Trades", html)
        self.assertIn("Open positions", html)
        self.assertIn("Recent trade events", html)
        self.assertIn("Open reason", html)
        self.assertIn("Close reason", html)
        self.assertIn("data-filter-group=\"status\"", html)
        self.assertIn("Open</button>", html)
        self.assertIn("Closed</button>", html)
        self.assertIn("Pod A</button>", html)
        self.assertIn("Pod B</button>", html)
        self.assertIn("Pod C</button>", html)
        self.assertIn("tooltip-bubble", html)

    def test_env_override_enables_pod_b_for_supervisor(self) -> None:
        with patch.dict("os.environ", {"TRIDENT_ENABLE_POD_B": "true"}, clear=False):
            config = load_config("config/trident.toml")
        supervisor = TridentSupervisor(
            config=config,
            profile="trident",
            mode="observation",
        )
        payload = state_payload(supervisor, MetricsRegistry())
        self.assertIn("pod_b", payload["enabled_pods"])
        self.assertTrue(payload["pods"]["pod_b"]["enabled"])


if __name__ == "__main__":
    unittest.main()
