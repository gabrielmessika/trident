import json
import unittest
from unittest.mock import patch

from app.observability.api import (
    _humanize_close_reason,
    _open_position_rows,
    _humanize_setup_reason,
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
        self.assertIn("routing_overrides", payload)
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
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=_api_runtime_loader,
        ):
            payload = state_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["regime"], "TrendExpansion")
        self.assertEqual(payload["capital_plan"]["regime"], "TrendExpansion")
        self.assertEqual(payload["regime_evaluation_count"], 6)
        self.assertEqual(payload["pod_a_signal_preview"][0]["symbol"], "ETH")
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

    def test_metrics_payload_refreshes_registry(self) -> None:
        payload = metrics_payload(self.supervisor, self.metrics)
        self.assertEqual(payload["trident_bootstrap_ready"], 1)
        self.assertGreaterEqual(payload["enabled_pod_count"], 1)
        self.assertEqual(payload["owned_symbol_count"], 0)

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
        self.assertIn("TRIDENT Supervisor Dashboard", html)
        self.assertIn("TRIDENT Control Center", html)
        self.assertIn(">Status</button>", html)
        self.assertIn(">Pod A</button>", html)
        self.assertIn(">Pod B</button>", html)
        self.assertIn(">Pod C</button>", html)
        self.assertIn(">Activity</button>", html)
        self.assertIn(">System</button>", html)
        self.assertIn("À faire maintenant", html)
        self.assertIn("En un coup d’œil", html)
        self.assertIn("Régimes par cluster", html)
        self.assertIn("Crypto", html)
        self.assertIn("Index", html)
        self.assertIn("Runtime pod report", html)
        self.assertIn("Data collectors", html)
        self.assertIn("Pod C scope visibility", html)
        self.assertIn("Symbol ownership", html)
        self.assertIn("Routing overrides", html)
        self.assertIn("Routing decisions", html)
        self.assertIn("Set runtime pin", html)
        self.assertIn("Clear pin", html)
        self.assertIn("/api/routing/override", html)
        self.assertIn("Ownership conflicts", html)
        self.assertIn("Recent trading activity", html)
        self.assertIn("Les trades apparaîtront ici", html)
        self.assertIn("Leverage", html)
        self.assertIn("Levier configuré", html)
        self.assertIn("Prix courant", html)
        self.assertIn("Valeur courante USD", html)
        self.assertIn("Marge utilisee", html)
        self.assertIn("Prix TP", html)
        self.assertIn("Prix SL", html)
        self.assertIn("Trailing TP", html)
        self.assertIn("capital immobilise", html.lower())
        self.assertIn("tooltip-bubble", html)
        self.assertIn("panel-good", html)
        self.assertIn("panel-neutral", html)
        self.assertIn("global-banner-good", html)
        self.assertIn("status-card-good", html)
        self.assertIn("pod-card-good", html)
        self.assertIn('data-refresh-seconds="10"', html)
        self.assertIn("window.location.replace(target)", html)
        self.assertIn("Auto-refresh 10s", html)
        self.assertIn("Last updated:", html)
        self.assertIn("/api/state", html)
        self.assertIn("/api/report", html)
        self.assertIn("/trades", html)
        self.assertIn('data-tab-panel="status"', html)

    def test_trades_html_contains_trade_sections(self) -> None:
        html = trades_html(self.supervisor, self.metrics)
        self.assertIn("TRIDENT Trades", html)
        self.assertIn('data-default-tab="activity"', html)
        self.assertIn(">Activity</button>", html)
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

    def test_reason_labels_are_humanized(self) -> None:
        self.assertIn("BOS retest long", _humanize_setup_reason("bos_retest_long"))
        self.assertIn("take profit", _humanize_close_reason("take_profit_hit").lower())
        self.assertIn("signal oppose", _humanize_close_reason("opposite_signal").lower())

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
