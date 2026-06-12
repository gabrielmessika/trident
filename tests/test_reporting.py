import csv
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.live.runtime_status import runtime_status_is_fresh
from app.reporting.export_daily import build_daily_summary, render_daily_markdown
from app.reporting.live_journal import _LIVE_JOURNAL_REPORT_CACHE, load_live_journal_report
from app.reporting.multi_pod import build_cohabitation_summary, build_runtime_report
from app.settings import load_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import RegimeSnapshot
from app.trident.types import SymbolMarketSnapshot
from scripts.export_trident_audit_pack import export_directional_logs


class ReportingTests(unittest.TestCase):
    def test_live_journal_report_cache_tracks_appended_records(self) -> None:
        _LIVE_JOURNAL_REPORT_CACHE.clear()

        def trade_record(index: int, symbol: str, pnl: float) -> dict[str, object]:
            return {
                "event_type": "trade_close",
                "source": "pod_a_live_trade",
                "record_index": index,
                "timestamp": f"2026-06-05T00:0{index}:00Z",
                "trade": {
                    "symbol": symbol,
                    "side": "long",
                    "setup": "trend_pullback_long",
                    "entry_price": 100.0,
                    "exit_price": 101.0,
                    "target_notional_usd": 100.0,
                    "gross_pnl_usd": pnl,
                    "fees_usd": 0.01,
                    "pnl_usd": pnl,
                    "close_reason": "exchange_closed",
                    "opened_at": f"2026-06-05T00:0{index}:00+00:00",
                    "closed_at": f"2026-06-05T00:0{index}:30+00:00",
                },
            }

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pod_a_live.jsonl"
            path.write_text(
                json.dumps(trade_record(1, "ETH", 1.5)) + "\n",
                encoding="utf-8",
            )
            first = load_live_journal_report(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(trade_record(2, "SOL", -0.5)) + "\n")
            second = load_live_journal_report(path)

        _LIVE_JOURNAL_REPORT_CACHE.clear()
        self.assertEqual(first["closed_trade_count"], 1)
        self.assertEqual(second["closed_trade_count"], 2)
        self.assertAlmostEqual(second["realized_pnl_usd"], 1.0)

    def test_audit_export_uses_append_only_trade_close_with_close_fills(self) -> None:
        trade_close = {
            "event_type": "trade_close",
            "source": "pod_a_live_trade",
            "record_index": 7,
            "timestamp": "2026-06-05T00:30:00Z",
            "trade": {
                "symbol": "ETH",
                "side": "long",
                "setup": "trend_pullback_long",
                "entry_price": 2132.51,
                "exit_price": 2120.0,
                "target_notional_usd": 100.0,
                "pnl_usd": -0.59,
                "gross_pnl_usd": -0.58,
                "fees_usd": 0.01,
                "exchange_fee_usd": 0.01,
                "exchange_closed_pnl_usd": -0.58,
                "fee_source": "exchange_user_fills",
                "funding_usd": -0.03,
                "funding_source": "exchange_user_funding_history",
                "funding_payment_count": 1,
                "close_reason": "exchange_closed",
                "close_fill_count": 1,
                "exchange_close_fill_count": 1,
                "opened_at": "2026-06-05T00:00:00+00:00",
                "closed_at": "2026-06-05T00:30:00+00:00",
                "close_fills": [
                    {
                        "symbol": "ETH",
                        "side": "A",
                        "action": "close",
                        "price": 2120.0,
                        "notional_usd": 86.708,
                        "fee_usd": 0.01,
                        "filled_size": "0.0409",
                        "oid": 42,
                        "complete": True,
                        "exchange_fill_available": True,
                        "exchange_fee_usd": 0.01,
                        "exchange_closed_pnl_usd": -0.58,
                        "exchange_direction": "Close Long",
                        "exchange_timestamp_ms": 1780619400000,
                        "fee_source": "exchange_user_fills",
                        "close_reason": "exchange_closed",
                        "funding_usd": -0.03,
                        "funding_source": "exchange_user_funding_history",
                        "funding_payment_count": 1,
                    }
                ],
            },
        }

        with TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir) / "server-data"
            output_dir = Path(tmpdir) / "audit"
            (source_root / "logs").mkdir(parents=True)
            output_dir.mkdir()
            (source_root / "logs" / "pod_a_live.jsonl").write_text(
                json.dumps(trade_close) + "\n",
                encoding="utf-8",
            )

            summary = export_directional_logs(source_root, output_dir)

            self.assertEqual(summary["closed_trade_rows"], 1)
            self.assertEqual(summary["close_fill_count_by_pod"], {"pod_a": 1})
            closed_rows = list(
                csv.DictReader(
                    (output_dir / "trident_ac_closed_trades.csv").open(
                        encoding="utf-8",
                        newline="",
                    )
                )
            )
            fill_rows = list(
                csv.DictReader(
                    (output_dir / "trident_ac_fill_events.csv").open(
                        encoding="utf-8",
                        newline="",
                    )
                )
            )

        self.assertEqual(len(closed_rows), 1)
        self.assertEqual(closed_rows[0]["symbol"], "ETH")
        self.assertEqual(closed_rows[0]["exchange_fee_usd"], "0.01")
        self.assertEqual(closed_rows[0]["exchange_closed_pnl_usd"], "-0.58")
        self.assertEqual(closed_rows[0]["funding_usd"], "-0.03")
        self.assertEqual(closed_rows[0]["funding_source"], "exchange_user_funding_history")
        self.assertEqual(closed_rows[0]["funding_payment_count"], "1")
        self.assertEqual(closed_rows[0]["close_fill_oids"], "42")
        self.assertEqual(len(fill_rows), 1)
        self.assertEqual(fill_rows[0]["action"], "close")
        self.assertEqual(fill_rows[0]["exchange_fee_usd"], "0.01")
        self.assertEqual(fill_rows[0]["funding_usd"], "-0.03")
        self.assertEqual(fill_rows[0]["exchange_direction"], "Close Long")

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

        with patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=[None, None, None, None, None, None],
        ):
            report = build_runtime_report(supervisor).to_dict()

        self.assertEqual(report["profile"], "trident-reporting")
        self.assertEqual(report["enabled_pod_count"], 3)
        self.assertEqual(len(report["pods"]), 3)
        pod_a = next(item for item in report["pods"] if item["pod"] == "pod_a")
        pod_b = next(item for item in report["pods"] if item["pod"] == "pod_b")
        self.assertEqual(pod_a["owned_symbols"], ["DOGE", "XRP"])
        self.assertEqual(pod_b["owned_symbols"], [])
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
            "updated_at": "2999-01-01T00:00:00Z",
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
            side_effect=[pod_a_runtime, None, None, None, None, None],
        ):
            report = build_runtime_report(supervisor).to_dict()

        pod_a = next(item for item in report["pods"] if item["pod"] == "pod_a")
        self.assertEqual(pod_a["position_count"], 2)
        self.assertEqual(pod_a["total_fill_count"], 3)
        self.assertAlmostEqual(pod_a["total_unrealized_pnl_usd"], 0.75)
        self.assertEqual(report["active_position_count"], 2)
        self.assertAlmostEqual(report["total_unrealized_pnl_usd"], 0.75)

    def test_build_runtime_report_counts_live_journal_closed_trades_after_restart(self) -> None:
        config = load_config("config/trident.toml")
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting-live-journal",
            mode="live",
        )
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

        def runtime_status_for(path: object) -> dict[str, object] | None:
            if str(path).endswith("pod_a_live_status.json"):
                return pod_a_runtime
            return None

        records = [
            {
                "event_type": "trade_close",
                "source": "pod_a_live_trade",
                "record_index": 1,
                "timestamp": "2026-05-27T16:30:00Z",
                "trade": {
                    "symbol": "ETH",
                    "side": "long",
                    "setup": "trend_pullback_long",
                    "entry_price": 3900.0,
                    "exit_price": 3880.0,
                    "target_notional_usd": 125.0,
                    "gross_pnl_usd": -1.74,
                    "fees_usd": 0.08,
                    "pnl_usd": -1.82,
                    "close_reason": "exchange_closed",
                    "opened_at": "2026-05-27T16:21:00+00:00",
                    "closed_at": "2026-05-27T16:30:00+00:00",
                },
            },
            {
                "event_type": "trade_close",
                "source": "pod_a_live_trade",
                "record_index": 2,
                "timestamp": "2026-05-27T16:35:00Z",
                "trade": {
                    "symbol": "SOL",
                    "side": "long",
                    "setup": "trend_pullback_long",
                    "entry_price": 178.0,
                    "exit_price": 176.2,
                    "target_notional_usd": 125.0,
                    "gross_pnl_usd": -2.1,
                    "fees_usd": 0.08,
                    "pnl_usd": -2.18,
                    "close_reason": "exchange_closed",
                    "opened_at": "2026-05-27T16:26:00+00:00",
                    "closed_at": "2026-05-27T16:35:00+00:00",
                },
            },
        ]

        previous_cwd = os.getcwd()
        with TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            (logs_dir / "pod_a_live.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            os.chdir(tmpdir)
            try:
                with patch(
                    "app.reporting.multi_pod.load_runtime_status",
                    side_effect=runtime_status_for,
                ):
                    report = build_runtime_report(supervisor).to_dict()
            finally:
                os.chdir(previous_cwd)

        pod_a = next(item for item in report["pods"] if item["pod"] == "pod_a")
        self.assertEqual(pod_a["total_fill_count"], 2)
        self.assertEqual(pod_a["loss_count"], 2)
        self.assertAlmostEqual(pod_a["realized_pnl_usd"], -4.0)
        self.assertAlmostEqual(report["realized_pnl_usd"], -4.0)

    def test_build_runtime_report_ignores_disabled_pod_runtime_artifacts(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = False
        config.pod_c.enabled = False
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting-disabled-runtime",
            mode="observation",
        )
        stale_pod_b_runtime = {
            "updated_at": "2999-01-01T00:00:00Z",
            "process_state": "running",
            "report": {
                "closed_trade_count": 7,
                "realized_pnl_usd": -13.61,
            },
        }
        stale_pod_c_runtime = {
            "updated_at": "2999-01-01T00:00:01Z",
            "process_state": "running",
            "report": {
                "closed_trade_count": 8,
                "realized_pnl_usd": 9.32,
            },
        }

        with patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=[
                None,
                stale_pod_b_runtime,
                stale_pod_c_runtime,
                stale_pod_b_runtime,
                None,
                None,
            ],
        ):
            report = build_runtime_report(supervisor).to_dict()

        pod_b = next(item for item in report["pods"] if item["pod"] == "pod_b")
        pod_c = next(item for item in report["pods"] if item["pod"] == "pod_c")
        self.assertEqual(pod_b["process_state"], "disabled")
        self.assertEqual(pod_c["process_state"], "disabled")
        self.assertEqual(pod_b["total_fill_count"], 0)
        self.assertEqual(pod_c["total_fill_count"], 0)
        self.assertEqual(pod_b["realized_pnl_usd"], 0.0)
        self.assertEqual(pod_c["realized_pnl_usd"], 0.0)
        self.assertEqual(report["total_fill_count"], 0)
        self.assertEqual(report["realized_pnl_usd"], 0.0)

    def test_build_runtime_report_counts_hip4_replacement_as_pod_b(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = False
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting-hip4-pod-b",
            mode="observation",
        )
        pod_b_runtime = {
            "pod": "pod_b",
            "pod_kind": "hip4_outcome_edge_pod",
            "updated_at": "2999-01-01T00:00:00Z",
            "process_state": "running",
            "poll_seconds": 4,
            "managed_symbols": ["BTC", "HYPE"],
            "open_positions": [{"market_id": "BTC_GT_1", "underlying": "BTC"}],
            "total_fill_count": 3,
            "report": {
                "closed_trade_count": 1,
                "realized_pnl_usd": 2.5,
            },
        }

        def runtime_status_for(path: object) -> dict[str, object] | None:
            if str(path).endswith("pod_b_live_status.json"):
                return pod_b_runtime
            return None

        with patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=runtime_status_for,
        ):
            report = build_runtime_report(supervisor).to_dict()

        pod_b = next(item for item in report["pods"] if item["pod"] == "pod_b")
        self.assertTrue(pod_b["enabled"])
        self.assertTrue(pod_b["healthy"])
        self.assertEqual(pod_b["owned_symbols"], ["BTC", "HYPE"])
        self.assertEqual(pod_b["process_state"], "running")
        self.assertEqual(pod_b["position_count"], 1)
        self.assertEqual(pod_b["total_fill_count"], 3)
        self.assertEqual(pod_b["realized_pnl_usd"], 2.5)

    def test_build_runtime_report_merges_pod_a_and_pod_c_runtime_supervisors(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting-merged-runtime",
            mode="observation",
        )
        pod_a_runtime = {
            "pod": "pod_a",
            "process_state": "running",
            "updated_at": "2999-01-01T00:00:00Z",
            "report": {
                "closed_trade_count": 2,
                "realized_pnl_usd": 3.5,
            },
            "supervisor": {
                "regime": "TrendExpansion",
                "enabled_pods": ["pod_a", "pod_c"],
                "ownership_conflicts": [],
                "capital_plan": {
                    "regime": "TrendExpansion",
                    "cash_usd": 100.0,
                    "pods": {
                        "pod_a": {
                            "target_pct": 0.7,
                            "target_usd": 700.0,
                        }
                    },
                },
                "pods": {
                    "pod_a": {
                        "owned_symbols": ["BTC", "ETH"],
                        "target_pct": 0.7,
                        "target_usd": 700.0,
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
                "realized_pnl_usd": 1.2,
            },
            "supervisor": {
                "regime": "TrendExpansion",
                "enabled_pods": ["pod_a", "pod_c"],
                "ownership_conflicts": [],
                "capital_plan": {
                    "regime": "TrendExpansion",
                    "cash_usd": 100.0,
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
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=[pod_a_runtime, pod_c_runtime, None, None, None, None],
        ):
            report = build_runtime_report(supervisor).to_dict()

        pod_a = next(item for item in report["pods"] if item["pod"] == "pod_a")
        pod_c = next(item for item in report["pods"] if item["pod"] == "pod_c")
        self.assertEqual(pod_a["owned_symbols"], ["BTC", "ETH"])
        self.assertEqual(pod_a["preview_count"], 1)
        self.assertEqual(pod_c["owned_symbols"], ["SPX"])
        self.assertEqual(pod_c["preview_count"], 1)
        self.assertEqual(report["regime"], "TrendExpansion")

    def test_build_runtime_report_keeps_provided_snapshot_authoritative(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting-authoritative-snapshot",
            mode="observation",
        )
        runtime_snapshot = {
            "regime": "RangeAuction",
            "enabled_pods": ["pod_a", "pod_c"],
            "ownership_conflicts": [],
            "capital_plan": {
                "regime": "RangeAuction",
                "cash_usd": 940.0,
                "pods": {
                    "pod_a": {"target_pct": 0.0, "target_usd": 0.0},
                    "pod_b": {"target_pct": 0.0, "target_usd": 0.0},
                    "pod_c": {"target_pct": 0.06, "target_usd": 60.0},
                },
            },
            "pods": {
                "pod_a": {"owned_symbols": [], "target_pct": 0.0, "target_usd": 0.0},
                "pod_b": {"owned_symbols": [], "target_pct": 0.0, "target_usd": 0.0},
                "pod_c": {
                    "owned_symbols": ["XYZ:BRENTOIL"],
                    "target_pct": 0.06,
                    "target_usd": 60.0,
                },
            },
            "pod_c_signal_preview": [],
        }
        conflicting_pod_c_runtime = {
            "pod": "pod_c",
            "process_state": "running",
            "updated_at": "2999-01-01T00:00:00Z",
            "open_positions": [
                {"symbol": "XYZ:BRENTOIL", "unrealized_pnl_usd": 9.25}
            ],
            "report": {
                "closed_trade_count": 1,
                "realized_pnl_usd": -0.68,
            },
            "supervisor": {
                "regime": "RangeAuction",
                "capital_plan": {
                    "regime": "RangeAuction",
                    "cash_usd": 890.0,
                    "pods": {
                        "pod_c": {"target_pct": 0.11, "target_usd": 110.0},
                    },
                },
                "pods": {
                    "pod_c": {
                        "owned_symbols": ["XYZ:BRENTOIL", "XYZ:CL", "XYZ:GOLD"],
                        "target_pct": 0.11,
                        "target_usd": 110.0,
                    }
                },
            },
        }

        def _runtime_loader(path):
            if "pod_c_live_status.json" in str(path):
                return conflicting_pod_c_runtime
            return None

        with patch("app.reporting.multi_pod.load_runtime_status", side_effect=_runtime_loader):
            report = build_runtime_report(
                supervisor,
                runtime_snapshot=runtime_snapshot,
            ).to_dict()

        pod_c = next(item for item in report["pods"] if item["pod"] == "pod_c")
        self.assertEqual(pod_c["owned_symbols"], ["XYZ:BRENTOIL"])
        self.assertEqual(pod_c["target_pct"], 0.06)
        self.assertEqual(pod_c["target_usd"], 60.0)
        self.assertEqual(pod_c["position_count"], 1)
        self.assertEqual(pod_c["total_unrealized_pnl_usd"], 9.25)

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

    def test_runtime_status_is_fresh_uses_poll_seconds_for_slow_collectors(self) -> None:
        payload = {
            "updated_at": "2026-04-11T14:43:30Z",
            "poll_seconds": 300,
        }

        with patch("app.live.runtime_status.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 4, 11, 14, 48, 0, tzinfo=timezone.utc
            )
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            self.assertTrue(runtime_status_is_fresh(payload))

    def test_build_runtime_report_keeps_slow_funding_collector_healthy(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting-funding-service",
            mode="observation",
        )
        pod_a_runtime = {"updated_at": "2999-01-01T00:00:00Z", "process_state": "running"}
        pod_c_runtime = {"updated_at": "2999-01-01T00:00:01Z", "process_state": "running"}
        funding_runtime = {
            "service": "funding_collector",
            "label": "Funding Collector",
            "process_state": "running",
            "updated_at": "2026-04-11T14:43:30Z",
            "poll_seconds": 300,
            "symbol_count": 22,
            "polls_completed": 245,
            "records_written": 4410,
            "last_collected_at": "2026-04-11T14:43:30Z",
        }
        tradfi_runtime = {
            "service": "tradfi_funding_collector",
            "label": "Tradfi Funding Collector",
            "process_state": "running",
            "updated_at": "2999-01-01T00:00:02Z",
            "poll_seconds": 60,
            "symbol_count": 5,
        }

        def runtime_status_for(path: object) -> dict[str, object] | None:
            path_value = str(path)
            if path_value.endswith("pod_a_live_status.json"):
                return pod_a_runtime
            if path_value.endswith("pod_b_live_status.json"):
                return None
            if path_value.endswith("pod_c_live_status.json"):
                return pod_c_runtime
            if path_value.endswith("funding_collector_status.json"):
                return funding_runtime
            if path_value.endswith("tradfi_funding_collector_status.json"):
                return tradfi_runtime
            return None

        with patch.dict(
            os.environ,
            {"TRIDENT_ENABLE_FUNDING": "true"},
            clear=False,
        ), patch(
            "app.reporting.multi_pod._deployment_profile",
            return_value={},
        ), patch(
            "app.reporting.multi_pod.load_runtime_status",
            side_effect=runtime_status_for,
        ), patch("app.live.runtime_status.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2026, 4, 11, 14, 48, 0, tzinfo=timezone.utc
            )
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            report = build_runtime_report(supervisor).to_dict()

        funding_service = next(
            item for item in report["services"] if item["service"] == "funding_collector"
        )
        self.assertTrue(funding_service["healthy"])
        self.assertEqual(funding_service["comment"], "Collector healthy.")

    def test_build_runtime_report_marks_global_funding_disabled_from_deploy_flag(self) -> None:
        config = load_config("config/trident.toml")
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting-without-funding",
            mode="observation",
        )

        with patch.dict(
            os.environ,
            {"TRIDENT_ENABLE_FUNDING": "false"},
            clear=False,
        ), patch(
            "app.reporting.multi_pod._deployment_profile",
            return_value={},
        ), patch("app.reporting.multi_pod.load_runtime_status", return_value=None):
            report = build_runtime_report(supervisor).to_dict()

        funding_service = next(
            item for item in report["services"] if item["service"] == "funding_collector"
        )
        self.assertFalse(funding_service["enabled"])
        self.assertFalse(funding_service["healthy"])
        self.assertEqual(funding_service["process_state"], "disabled")
        self.assertIn("--without-funding", funding_service["comment"])

    def test_build_runtime_report_marks_degraded_service_unhealthy(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = False
        supervisor = TridentSupervisor(
            config=config,
            profile="trident-reporting",
            mode="observation",
        )
        funding_runtime = {
            "service": "funding_collector",
            "label": "Funding Collector",
            "process_state": "degraded",
            "updated_at": "2999-01-01T00:00:00Z",
            "poll_seconds": 300,
            "symbol_count": 22,
            "polls_completed": 245,
            "records_written": 4410,
            "last_collected_at": "2999-01-01T00:00:00Z",
            "error_count": 2,
            "last_error": "TimeoutError: temporary read timeout",
        }

        def runtime_status_for(path: object) -> dict[str, object] | None:
            if str(path).endswith("funding_collector_status.json"):
                return funding_runtime
            return None

        with patch.dict(
            os.environ,
            {"TRIDENT_ENABLE_FUNDING": "true"},
            clear=False,
        ), patch(
            "app.reporting.multi_pod._deployment_profile",
            return_value={},
        ), patch("app.reporting.multi_pod.load_runtime_status", side_effect=runtime_status_for):
            report = build_runtime_report(supervisor).to_dict()

        funding_service = next(
            item for item in report["services"] if item["service"] == "funding_collector"
        )
        self.assertFalse(funding_service["healthy"])
        self.assertEqual(funding_service["error_count"], 2)
        self.assertIn("TimeoutError", funding_service["comment"])


if __name__ == "__main__":
    unittest.main()
