import unittest
from unittest.mock import patch

from app.reporting.export_daily import build_daily_summary, render_daily_markdown
from app.reporting.multi_pod import build_cohabitation_summary, build_runtime_report
from app.settings import load_config
from app.trident.supervisor import TridentSupervisor


class ReportingTests(unittest.TestCase):
    def test_build_runtime_report_includes_pod_sections(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        config.pod_b.symbols = ["DOGE", "XRP"]
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting",
            mode="observation",
        )

        with patch("app.reporting.multi_pod.load_runtime_status", side_effect=[None, None]):
            report = build_runtime_report(supervisor).to_dict()

        self.assertEqual(report["profile"], "trident-reporting")
        self.assertEqual(report["enabled_pod_count"], 2)
        self.assertEqual(len(report["pods"]), 3)
        pod_b = next(item for item in report["pods"] if item["pod"] == "pod_b")
        self.assertEqual(pod_b["owned_symbols"], ["DOGE", "XRP"])
        self.assertEqual(pod_b["position_count"], 0)
        self.assertEqual(report["active_open_order_count"], 0)

    def test_build_cohabitation_summary_aggregates_pnl(self) -> None:
        class DummyResult:
            records_processed = 42
            ownership_conflict_count = 1
            no_symbol_overlap = True
            pod_a_owned_symbols = ["BTC", "ETH"]
            pod_a_signal_count = 3
            pod_a_accepted_count = 2
            pod_a_opened_count = 1
            pod_a_closed_trade_count = 1
            pod_a_realized_pnl_usd = 1.25
            pod_b_owned_symbols = ["XRP"]
            pod_b_total_fill_count = 5
            pod_b_recent_fill_count = 3
            pod_b_total_open_order_count = 2
            pod_b_total_position_count = 1
            pod_b_realized_pnl_usd = -0.25
            pod_b_total_unrealized_pnl_usd = 0.5

        summary = build_cohabitation_summary(DummyResult())

        self.assertEqual(summary["records_processed"], 42)
        self.assertEqual(summary["total_realized_pnl_usd"], 1.0)
        self.assertEqual(summary["pods"]["pod_b"]["total_fill_count"], 5)

    def test_build_daily_summary_reconciles_reports(self) -> None:
        summary = build_daily_summary(
            pod_a_report={
                "realized_pnl_usd": 12.5,
                "max_drawdown_usd": 3.0,
                "closed_trade_count": 4,
            },
            pod_b_report={
                "realized_pnl_usd": -0.5,
                "total_unrealized_pnl_usd": 1.25,
                "max_drawdown_usd": 1.5,
                "total_fill_count": 10,
            },
            reference_equity_usd=1000.0,
        )

        self.assertEqual(summary["total_realized_pnl_usd"], 12.0)
        self.assertEqual(summary["total_unrealized_pnl_usd"], 1.25)
        self.assertEqual(summary["max_drawdown_usd"], 3.0)
        self.assertEqual(summary["reconciliation_gap_usd"], 0.0)
        markdown = render_daily_markdown(summary)
        self.assertIn("TRIDENT Daily Summary", markdown)
        self.assertIn("Pod B total fill count: 10", markdown)


if __name__ == "__main__":
    unittest.main()
