import json
import unittest

from app.observability.api import (
    dashboard_html,
    health_payload,
    metrics_payload,
    report_payload,
    state_payload,
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

    def test_metrics_payload_refreshes_registry(self) -> None:
        payload = metrics_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["trident_bootstrap_ready"], 1)
        self.assertEqual(payload["enabled_pod_count"], 1)
        self.assertEqual(payload["owned_symbol_count"], 4)

    def test_report_payload_exposes_multi_pod_runtime_report(self) -> None:
        payload = report_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["profile"], "trident")
        self.assertEqual(payload["enabled_pod_count"], 1)
        self.assertIn("pods", payload)
        self.assertEqual(len(payload["pods"]), 3)
        self.assertEqual(payload["active_position_count"], 0)

    def test_dashboard_html_contains_supervision_sections(self) -> None:
        html = dashboard_html(self.supervisor, self.metrics)
        self.assertIn("TRIDENT Supervisor Dashboard", html)
        self.assertIn("Symbol ownership", html)
        self.assertIn("Ownership conflicts", html)
        self.assertIn("Runtime pod report", html)
        self.assertIn("/api/state", html)
        self.assertIn("/api/report", html)


if __name__ == "__main__":
    unittest.main()
