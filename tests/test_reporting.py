import unittest
from unittest.mock import patch

from app.reporting.export_daily import build_daily_summary, render_daily_markdown
from app.reporting.multi_pod import build_cohabitation_summary, build_runtime_report
from app.settings import load_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import RegimeSnapshot
from app.trident.types import SymbolMarketSnapshot


class ReportingTests(unittest.TestCase):
    def test_build_runtime_report_includes_pod_sections(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=8.0,
                atr_ratio=0.5,
                range_width_bps=35.0,
                structure_score=0.05,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=0.18,
                    ema_fast=0.1801,
                    ema_slow=0.18,
                    vwap_distance_bps=-1.0,
                    structure_score=0.03,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=5000.0,
                    bucket_trade_count=30,
                    bucket_range_bps=12.0,
                ),
                SymbolMarketSnapshot(
                    symbol="XRP",
                    price=0.64,
                    ema_fast=0.6401,
                    ema_slow=0.64,
                    vwap_distance_bps=-1.0,
                    structure_score=0.02,
                    funding_rate=0.0,
                    spread_bps=1.1,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=4000.0,
                    bucket_trade_count=24,
                    bucket_range_bps=14.0,
                ),
            ]
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

    def test_build_runtime_report_counts_directional_open_positions(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting-open-positions",
            mode="observation",
        )
        pod_a_runtime = {
            "process_state": "running",
            "open_positions": [
                {"symbol": "ETH", "unrealized_pnl_usd": 1.25},
                {"symbol": "SOL", "unrealized_pnl_usd": -0.5},
            ],
            "report": {
                "closed_trade_count": 3,
                "realized_pnl_usd": 2.75,
            },
        }

        with patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=[pod_a_runtime, None],
        ):
            report = build_runtime_report(supervisor).to_dict()

        pod_a = next(item for item in report["pods"] if item["pod"] == "pod_a")
        self.assertEqual(pod_a["position_count"], 2)
        self.assertEqual(pod_a["total_fill_count"], 3)
        self.assertAlmostEqual(pod_a["total_unrealized_pnl_usd"], 0.75)
        self.assertEqual(report["active_position_count"], 2)
        self.assertAlmostEqual(report["total_unrealized_pnl_usd"], 0.75)

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
