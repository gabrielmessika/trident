import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_report import PodABacktestReport
from app.settings import load_config
from app.trident.types import PodName, Regime, SymbolMarketSnapshot, TradePlan


def _full_bot_record(timestamp: str, *, btc_price: float, eth_price: float, sol_price: float) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 30.0,
            "atr_ratio": 1.15,
            "range_width_bps": 150.0,
            "structure_score": 0.55,
            "btc_impulse": False,
        },
        "symbols": [
            {
                "symbol": "BTC",
                "price": btc_price,
                "ema_fast": btc_price * 0.999,
                "ema_slow": btc_price * 0.996,
                "vwap_distance_bps": -8.0,
                "structure_score": 0.62,
                "funding_rate": 0.0,
                "spread_bps": 1.1,
                "btc_aligned": True,
                "book_imbalance": 0.18,
                "trade_flow_bias": 0.15,
                "bucket_volume": 150.0,
                "bucket_trade_count": 50,
                "bucket_range_bps": 24.0,
                "source": "test",
            },
            {
                "symbol": "ETH",
                "price": eth_price,
                "ema_fast": eth_price * 0.999,
                "ema_slow": eth_price * 0.994,
                "vwap_distance_bps": -6.0,
                "structure_score": 0.58,
                "funding_rate": 0.0001,
                "spread_bps": 1.2,
                "btc_aligned": True,
                "book_imbalance": 0.14,
                "trade_flow_bias": 0.12,
                "bucket_volume": 120.0,
                "bucket_trade_count": 40,
                "bucket_range_bps": 28.0,
                "source": "test",
            },
            {
                "symbol": "SOL",
                "price": sol_price,
                "ema_fast": sol_price * 0.998,
                "ema_slow": sol_price * 0.997,
                "vwap_distance_bps": -10.0,
                "structure_score": 0.71,
                "funding_rate": 0.0002,
                "spread_bps": 1.4,
                "btc_aligned": True,
                "book_imbalance": 0.2,
                "trade_flow_bias": 0.18,
                "bucket_volume": 180.0,
                "bucket_trade_count": 70,
                "bucket_range_bps": 34.0,
                "source": "test",
            },
        ],
    }


class FullBotReplayTests(unittest.TestCase):
    def test_full_bot_replay_writes_report_summary_and_history(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_a.enabled = True
        config.pod_b.enabled = True
        config.pod_c.enabled = True
        
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            input_path = temp_dir / "snapshots.jsonl"
            input_path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        _full_bot_record(
                            "2026-04-05T10:00:00Z",
                            btc_price=1000.0,
                            eth_price=100.0,
                            sol_price=200.0,
                        ),
                        _full_bot_record(
                            "2026-04-05T10:01:00Z",
                            btc_price=1005.0,
                            eth_price=100.4,
                            sol_price=200.8,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report_path = temp_dir / "full_bot_report.json"
            summary_path = temp_dir / "full_bot_summary.md"
            comparison_path = temp_dir / "full_bot_history.jsonl"

            result = FullBotBacktestRunner(config).run_jsonl(
                input_path=input_path,
                report_output=report_path,
                summary_output=summary_path,
                comparison_output=comparison_path,
            )

            self.assertEqual(result.records_processed, 2)
            self.assertEqual(result.duplicate_timestamps_skipped, 0)
            self.assertTrue(report_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertTrue(comparison_path.exists())
            self.assertIn("directional_fees_usd", report_path.read_text(encoding="utf-8"))
            self.assertIn("TRIDENT full-bot backtest", summary_path.read_text(encoding="utf-8"))
            history_lines = comparison_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(history_lines), 1)
            history_entry = json.loads(history_lines[0])
            self.assertEqual(history_entry["records_processed"], 2)
            self.assertIn("pod_a_realized_pnl_usd", history_entry)
            self.assertIn("pod_b_closed_trade_count", history_entry)
            self.assertIn("pod_c_realized_pnl_usd", history_entry)

    def test_force_enable_backtest_keeps_pod_b_disabled_like_live_dry_run(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_a.enabled = False
        config.pod_b.enabled = False
        config.pod_c.enabled = False

        runner = FullBotBacktestRunner(config, force_enable_all_pods=True)

        self.assertTrue(runner.config.pod_a.enabled)
        self.assertFalse(runner.config.pod_b.enabled)
        self.assertTrue(runner.config.pod_c.enabled)

    def test_full_bot_replay_can_apply_live_notional_caps(self) -> None:
        config = load_config("config/trident.toml")
        config.trident.execution.live_max_order_notional_usd = 125.0
        runner = FullBotBacktestRunner(config, apply_live_notional_caps=True)

        [capped] = runner._apply_live_notional_caps(
            PodName.POD_A,
            [
                TradePlan(
                    symbol="BTC",
                    side="long",
                    setup="trend_pullback_long",
                    confidence=0.7,
                    target_notional_usd=500.0,
                    stop_bps=100.0,
                    time_stop_hours=4,
                    take_profit_bps=100.0,
                    margin_usd=50.0,
                    effective_leverage=10.0,
                )
            ],
        )

        self.assertEqual(capped.target_notional_usd, 125.0)
        self.assertEqual(capped.expected_loss_usd, 1.25)
        self.assertTrue(capped.setup_details["live_cap_active"])

    def test_full_bot_replay_merges_same_timestamp_snapshot_lines(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_a.enabled = True
        config.pod_b.enabled = True
        config.pod_c.enabled = True
        config.hyperliquid.observation_universe = ["BTC", "PAXG"]

        crypto_record = _full_bot_record(
            "2026-04-05T10:00:00Z",
            btc_price=1000.0,
            eth_price=100.0,
            sol_price=200.0,
        )
        crypto_record["cluster_regime_snapshots"] = {
            "crypto": {
                "ready": True,
                "adx": 30.0,
                "atr_ratio": 1.15,
                "range_width_bps": 150.0,
                "structure_score": 0.55,
                "btc_impulse": False,
            }
        }
        tradfi_record = {
            "timestamp": "2026-04-05T10:00:00Z",
            "regime_snapshot": {
                "ready": True,
                "adx": 12.0,
                "atr_ratio": 0.3,
                "range_width_bps": 20.0,
                "structure_score": 0.1,
                "btc_impulse": False,
            },
            "cluster_regime_snapshots": {
                "gold": {
                    "ready": True,
                    "adx": 12.0,
                    "atr_ratio": 0.3,
                    "range_width_bps": 20.0,
                    "structure_score": 0.1,
                    "btc_impulse": False,
                }
            },
            "symbols": [
                {
                    "symbol": "PAXG",
                    "price": 2400.0,
                    "ema_fast": 2401.0,
                    "ema_slow": 2398.0,
                    "vwap_distance_bps": -1.0,
                    "structure_score": 0.12,
                    "funding_rate": 0.0,
                    "spread_bps": 0.3,
                    "btc_aligned": True,
                    "book_imbalance": 0.04,
                    "trade_flow_bias": 0.03,
                    "bucket_volume": 20.0,
                    "bucket_trade_count": 9,
                    "bucket_range_bps": 6.0,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            input_path.write_text(
                json.dumps(crypto_record) + "\n" + json.dumps(tradfi_record) + "\n",
                encoding="utf-8",
            )

            result = FullBotBacktestRunner(config).run_jsonl(input_path=input_path)

        self.assertEqual(result.records_processed, 1)
        self.assertEqual(result.duplicate_timestamps_skipped, 0)
        self.assertEqual(result.first_timestamp, "2026-04-05T10:00:00Z")
        self.assertEqual(result.last_timestamp, "2026-04-05T10:00:00Z")

    def test_full_bot_replay_skips_maintenance_refresh_as_decision_record(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_a.enabled = True
        config.pod_b.enabled = True
        config.pod_c.enabled = True
        maintenance_record = _full_bot_record(
            "2026-04-05T10:00:15Z",
            btc_price=999.0,
            eth_price=99.0,
            sol_price=199.0,
        )
        maintenance_record["capture_reason"] = "maintenance_refresh"
        maintenance_record["stream_source"] = "pod_a_live"
        collector_record = _full_bot_record(
            "2026-04-05T10:01:00Z",
            btc_price=1005.0,
            eth_price=100.4,
            sol_price=200.8,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            input_path.write_text(
                json.dumps(maintenance_record) + "\n" + json.dumps(collector_record) + "\n",
                encoding="utf-8",
            )

            result = FullBotBacktestRunner(config).run_jsonl(input_path=input_path)

        self.assertEqual(result.records_processed, 1)
        self.assertEqual(result.pod_a.get("records_processed"), 1)

    def test_full_bot_replay_runs_directional_pod_b(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_a.enabled = True
        config.pod_b.enabled = True
        config.pod_c.enabled = True
        config.hyperliquid.observation_universe = ["BTC"]
        config.trident.routing.symbol_pod_overrides["BTC"] = "pod_b"
        config.trident.allocations.trend_expansion.pod_b = 1.0
        config.trident.allocations.trend_expansion.pod_a = 0.0
        config.trident.allocations.trend_expansion.pod_c = 0.0
        config.trident.allocations.trend_expansion.cash = 0.0

        records = [
            {
                "timestamp": "2026-04-05T10:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 28.0,
                    "atr_ratio": 1.0,
                    "range_width_bps": 120.0,
                    "structure_score": 0.50,
                    "btc_impulse": True,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 100.0,
                        "ema_fast": 100.8,
                        "ema_slow": 99.9,
                        "vwap_distance_bps": 9.0,
                        "structure_score": 0.42,
                        "funding_rate": 0.0,
                        "spread_bps": 1.1,
                        "btc_aligned": True,
                        "market_cluster": "crypto",
                        "cluster_aligned": True,
                        "cluster_leader": "BTC",
                        "book_imbalance": 0.32,
                        "trade_flow_bias": 0.28,
                        "bucket_volume": 8.0,
                        "bucket_notional_usd": 800.0,
                        "bucket_trade_count": 24,
                        "bucket_range_bps": 34.0,
                        "delta_book_imbalance": 0.22,
                        "delta_trade_flow_bias": 0.30,
                        "volume_ratio": 2.4,
                        "trade_count_ratio": 1.9,
                        "realized_vol_short_bps": 7.0,
                        "realized_vol_long_bps": 4.0,
                        "compression_score": 0.70,
                        "microprice_dislocation_bps": 1.4,
                        "source": "test",
                    }
                ],
            },
            {
                "timestamp": "2026-04-05T10:01:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.1,
                    "range_width_bps": 140.0,
                    "structure_score": 0.54,
                    "btc_impulse": True,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 101.0,
                        "ema_fast": 101.2,
                        "ema_slow": 100.3,
                        "vwap_distance_bps": 11.0,
                        "structure_score": 0.48,
                        "funding_rate": 0.0,
                        "spread_bps": 1.2,
                        "btc_aligned": True,
                        "market_cluster": "crypto",
                        "cluster_aligned": True,
                        "cluster_leader": "BTC",
                        "book_imbalance": 0.30,
                        "trade_flow_bias": 0.22,
                        "bucket_volume": 9.0,
                        "bucket_notional_usd": 909.0,
                        "bucket_trade_count": 26,
                        "bucket_range_bps": 18.0,
                        "delta_book_imbalance": 0.10,
                        "delta_trade_flow_bias": 0.12,
                        "volume_ratio": 1.8,
                        "trade_count_ratio": 1.4,
                        "realized_vol_short_bps": 8.0,
                        "realized_vol_long_bps": 4.5,
                        "compression_score": 0.62,
                        "microprice_dislocation_bps": 1.0,
                        "source": "test",
                    }
                ],
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            result = FullBotBacktestRunner(config).run_jsonl(
                input_path=input_path,
            )

        self.assertGreaterEqual(result.pod_b.get("signal_count", 0), 1)
        self.assertGreaterEqual(result.pod_b.get("closed_trade_count", 0), 1)
        self.assertGreater(result.pod_b.get("realized_pnl_usd", 0.0), 0.0)
        self.assertEqual(result.pod_b.get("trades_by_cluster", {}).get("crypto", 0), 1)

    def test_pod_c_keeps_active_symbol_when_opening_allocation_drops(self) -> None:
        config = load_config("config/trident.toml")
        runner = FullBotBacktestRunner(config, force_enable_all_pods=False)
        runner.pod_c_risk_gate = SimpleNamespace(evaluate_many=lambda plans: [])
        runner.pod_c_executor.portfolio.open_from_plan(
            TradePlan(
                symbol="XYZ:BRENTOIL",
                side="long",
                setup="tradfi_continuation_long",
                confidence=0.7,
                target_notional_usd=1000.0,
                stop_bps=500.0,
                time_stop_hours=24,
                take_profit_bps=500.0,
                margin_usd=100.0,
                effective_leverage=10.0,
            ),
            price=100.0,
            entry_fee_usd=0.35,
            timestamp="2026-04-27T04:57:00+00:00",
        )
        supervisor = SimpleNamespace(
            state=SimpleNamespace(regime=Regime.RANGE_AUCTION),
            preview_pod_c_signals=lambda snapshots: [],
            build_pod_c_trade_plans=lambda snapshots: [],
            opening_symbols_for=lambda pod_name: set(),
            managed_symbols_for=lambda pod_name, active_symbols=None: (
                {"XYZ:BRENTOIL"} if pod_name == PodName.POD_C and active_symbols else set()
            ),
        )

        runner._process_pod_c(
            supervisor=supervisor,
            report=PodABacktestReport(),
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="XYZ:BRENTOIL",
                    price=100.1,
                    ema_fast=100.0,
                    ema_slow=100.0,
                    vwap_distance_bps=0.0,
                    structure_score=0.0,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    market_cluster="oil",
                )
            ],
            timestamp="2026-04-27T05:57:19+00:00",
            source_file="snapshots.jsonl",
            previous_regime=Regime.RANGE_AUCTION.value,
            current_regime=Regime.RANGE_AUCTION.value,
        )

        self.assertIn("XYZ:BRENTOIL", runner.pod_c_executor.portfolio.open_positions)
        self.assertEqual(runner.pod_c_executor.portfolio.closed_trades, [])

    def test_loss_reaction_flips_losing_trade_once_without_cascade(self) -> None:
        config = load_config("config/trident.toml")
        runner = FullBotBacktestRunner(config, enable_loss_reaction_trades=True)
        losing_trade = SimpleNamespace(
            symbol="BTC",
            side="long",
            setup="trend_pullback_long",
            confidence=0.72,
            target_notional_usd=250.0,
            stop_bps=120.0,
            time_stop_hours=4,
            take_profit_bps=240.0,
            break_even_trigger_bps=80.0,
            trailing_activation_bps=140.0,
            trailing_distance_bps=70.0,
            margin_usd=25.0,
            effective_leverage=10.0,
            risk_budget_usd=3.0,
            expected_loss_usd=3.0,
            isolated=True,
            pnl_usd=-3.25,
            close_reason="stop_hit",
            opened_at=None,
            closed_at=None,
            setup_details={"market_cluster": "crypto", "current_date_key": "2026-04-05"},
        )

        [decision] = runner._loss_reaction_decisions_from_closed_trades(
            pod_name=PodName.POD_A,
            closed_trades=[losing_trade],
            snapshots=[],
        )

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, "loss_reaction")
        plan = decision.trade_plan
        self.assertEqual(plan.symbol, "BTC")
        self.assertEqual(plan.side, "short")
        self.assertEqual(plan.setup, "loss_reaction")
        self.assertEqual(plan.target_notional_usd, 250.0)
        self.assertEqual(plan.stop_bps, 120.0)
        self.assertEqual(plan.reentry_cooldown_minutes, 0)
        self.assertTrue(plan.setup_details["loss_reaction_trade"])
        self.assertEqual(plan.setup_details["loss_reaction_parent_side"], "long")
        self.assertEqual(plan.setup_details["loss_reaction_parent_setup"], "trend_pullback_long")

        reaction_loss = SimpleNamespace(
            **{
                **losing_trade.__dict__,
                "side": "short",
                "setup": "loss_reaction",
                "setup_details": {"loss_reaction_trade": True},
            }
        )
        self.assertEqual(
            runner._loss_reaction_decisions_from_closed_trades(
                pod_name=PodName.POD_A,
                closed_trades=[reaction_loss],
                snapshots=[],
            ),
            [],
        )

    def test_loss_reaction_can_filter_parent_reason_and_pod(self) -> None:
        config = load_config("config/trident.toml")
        runner = FullBotBacktestRunner(
            config,
            enable_loss_reaction_trades=True,
            loss_reaction_parent_close_reasons={"stop_hit"},
            loss_reaction_allowed_pods={"pod_a"},
        )
        base_trade = SimpleNamespace(
            symbol="BTC",
            side="long",
            setup="trend_pullback_long",
            confidence=0.72,
            target_notional_usd=250.0,
            stop_bps=120.0,
            time_stop_hours=4,
            take_profit_bps=240.0,
            break_even_trigger_bps=80.0,
            trailing_activation_bps=140.0,
            trailing_distance_bps=70.0,
            margin_usd=25.0,
            effective_leverage=10.0,
            risk_budget_usd=3.0,
            expected_loss_usd=3.0,
            isolated=True,
            pnl_usd=-3.25,
            close_reason="early_failure_exit",
            opened_at=None,
            closed_at=None,
            setup_details={},
        )

        self.assertEqual(
            runner._loss_reaction_decisions_from_closed_trades(
                pod_name=PodName.POD_A,
                closed_trades=[base_trade],
                snapshots=[],
            ),
            [],
        )
        stop_trade = SimpleNamespace(**{**base_trade.__dict__, "close_reason": "stop_hit"})
        self.assertEqual(
            runner._loss_reaction_decisions_from_closed_trades(
                pod_name=PodName.POD_C,
                closed_trades=[stop_trade],
                snapshots=[],
            ),
            [],
        )
        self.assertEqual(
            len(
                runner._loss_reaction_decisions_from_closed_trades(
                    pod_name=PodName.POD_A,
                    closed_trades=[stop_trade],
                    snapshots=[],
                )
            ),
            1,
        )

    def test_loss_reaction_ignore_entry_guards_bypasses_routing_for_reaction_only(self) -> None:
        config = load_config("config/trident.toml")
        snapshot = SymbolMarketSnapshot(
            symbol="BTC",
            price=94.0,
            ema_fast=96.0,
            ema_slow=97.0,
            vwap_distance_bps=-10.0,
            structure_score=-0.2,
            funding_rate=0.0,
            spread_bps=1.0,
            btc_aligned=True,
        )
        for ignore_guards, expected_open in ((False, False), (True, True)):
            runner = FullBotBacktestRunner(
                config,
                enable_loss_reaction_trades=True,
                loss_reaction_ignore_entry_guards=ignore_guards,
            )
            self.assertTrue(
                runner.pod_a_executor.portfolio.open_from_plan(
                    TradePlan(
                        symbol="BTC",
                        side="long",
                        setup="trend_pullback_long",
                        confidence=0.72,
                        target_notional_usd=250.0,
                        stop_bps=500.0,
                        time_stop_hours=4,
                        take_profit_bps=1000.0,
                        margin_usd=25.0,
                        effective_leverage=10.0,
                        risk_budget_usd=12.5,
                        expected_loss_usd=12.5,
                    ),
                    price=100.0,
                    entry_fee_usd=0.1,
                    timestamp="2026-04-05T10:00:00Z",
                )
            )

            execution, decisions = runner._execute_directional_record(
                pod_name=PodName.POD_A,
                executor=runner.pod_a_executor,
                snapshots=[snapshot],
                risk_decisions=[],
                signal_sides_by_symbol={},
                timestamp="2026-04-05T10:01:00Z",
                entry_allowed_symbols=set(),
                managed_symbols=set(),
            )

            self.assertEqual(execution.opened_symbols == ["BTC"], expected_open)
            self.assertEqual(any(decision.trade_plan.setup == "loss_reaction" for decision in decisions), True)
            if expected_open:
                self.assertEqual(runner.pod_a_executor.portfolio.open_positions["BTC"].side, "short")
            else:
                self.assertFalse(runner.pod_a_executor.portfolio.has_open_position("BTC"))


if __name__ == "__main__":
    unittest.main()
