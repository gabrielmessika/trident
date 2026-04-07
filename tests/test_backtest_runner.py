import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.pod_a_runner import PodABacktestRunner
from app.settings import load_config, override_app_config


class PodABacktestRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")
        self.runner = PodABacktestRunner(self.config)

    def test_runner_replays_jsonl_and_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            output_path = Path(tmpdir) / "signals.jsonl"
            records = [
                {
                    "timestamp": "2026-04-04T00:00:00Z",
                    "regime_snapshot": {
                        "ready": True,
                        "adx": 32.0,
                        "atr_ratio": 1.2,
                        "range_width_bps": 180.0,
                        "structure_score": 0.55,
                        "btc_impulse": False,
                    },
                    "symbols": [
                        {
                            "symbol": "ETH",
                            "price": 3100.0,
                            "ema_fast": 3090.0,
                            "ema_slow": 3050.0,
                            "vwap_distance_bps": -8.0,
                            "structure_score": 0.62,
                            "funding_rate": 0.0001,
                            "spread_bps": 1.2,
                            "btc_aligned": True,
                        },
                        {
                            "symbol": "BTC",
                            "price": 68000.0,
                            "ema_fast": 67950.0,
                            "ema_slow": 67800.0,
                            "vwap_distance_bps": -5.0,
                            "structure_score": 0.30,
                            "funding_rate": 0.0,
                            "spread_bps": 0.8,
                            "btc_aligned": True,
                        },
                    ],
                },
                {
                    "timestamp": "2026-04-04T00:15:00Z",
                    "regime_snapshot": {
                        "ready": True,
                        "adx": 32.0,
                        "atr_ratio": 1.2,
                        "range_width_bps": 180.0,
                        "structure_score": -0.55,
                        "btc_impulse": False,
                    },
                    "symbols": [
                        {
                            "symbol": "SOL",
                            "price": 140.0,
                            "ema_fast": 141.0,
                            "ema_slow": 145.0,
                            "vwap_distance_bps": 9.0,
                            "structure_score": -0.58,
                            "funding_rate": -0.0001,
                            "spread_bps": 1.8,
                            "btc_aligned": True,
                        }
                    ],
                },
            ]
            with input_path.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")

            result = self.runner.run_jsonl(input_path, output_path)

            self.assertEqual(result.records_processed, 2)
            self.assertEqual(result.reference_equity_usd, 1000.0)
            self.assertEqual(result.signal_count, 2)
            self.assertEqual(result.accepted_count, 2)
            self.assertEqual(result.rejected_count, 0)
            self.assertEqual(result.opened_count, 2)
            self.assertEqual(result.skipped_open_count, 0)
            self.assertEqual(result.closed_trade_count, 2)
            self.assertEqual(result.win_count, 0)
            self.assertEqual(result.loss_count, 2)
            self.assertLess(result.realized_pnl_usd, 0.0)
            self.assertGreater(result.gross_pnl_usd, result.realized_pnl_usd)
            self.assertGreater(result.fees_usd, 0.0)
            self.assertGreater(result.average_hold_hours, 0.0)
            self.assertEqual(result.signals_by_symbol, {"ETH": 1, "SOL": 1})
            self.assertEqual(result.signals_by_side, {"long": 1, "short": 1})
            self.assertEqual(
                result.signals_by_setup,
                {"trend_pullback_long": 1, "trend_pullback_short": 1},
            )
            self.assertEqual(result.signals_by_regime, {"TrendExpansion": 2})
            self.assertEqual(result.records_by_date, {"2026-04-04": 2})
            self.assertEqual(result.signals_by_date, {"2026-04-04": 2})
            self.assertEqual(result.accepted_by_date, {"2026-04-04": 2})
            self.assertEqual(result.rejected_by_date, {})
            self.assertEqual(result.regime_transition_count, 1)
            self.assertEqual(result.regime_transitions, {"Cash->TrendExpansion": 1})
            self.assertEqual(
                result.regime_transitions_by_date,
                {"2026-04-04": {"Cash->TrendExpansion": 1}},
            )
            self.assertEqual(result.rejections_by_reason, {})
            self.assertGreater(result.average_confidence, 0.8)
            self.assertLess(result.average_confidence, 0.95)
            self.assertEqual(result.close_reasons, {"end_of_backtest": 2})
            self.assertEqual(result.trades_by_symbol, {"ETH": 1, "SOL": 1})
            self.assertEqual(
                result.trades_by_setup,
                {"trend_pullback_long": 1, "trend_pullback_short": 1},
            )
            self.assertEqual(
                result.accepted_by_setup,
                {"trend_pullback_long": 1, "trend_pullback_short": 1},
            )
            self.assertEqual(
                result.opened_by_setup,
                {"trend_pullback_long": 1, "trend_pullback_short": 1},
            )
            self.assertIn("2026-04-04", result.pnl_by_date)
            self.assertIn("trend_pullback_long", result.pnl_by_setup)
            self.assertGreater(result.max_open_positions, 0)
            self.assertGreater(result.max_open_margin_usd, 0.0)
            self.assertGreater(result.max_open_notional_usd, 0.0)
            self.assertGreater(result.max_open_expected_loss_usd, 0.0)
            self.assertEqual(len(result.closed_trade_log), 2)
            self.assertEqual(result.closed_trade_log[0]["date"], "2026-04-04")
            self.assertTrue(output_path.exists())
            lines = output_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 4)
            first = json.loads(lines[0])
            self.assertEqual(first["timestamp"], "2026-04-04T00:00:00Z")
            self.assertIn("symbol_snapshot", first)
            self.assertIn("regime_snapshot", first)
            self.assertEqual(first["signal"]["risk"]["accepted"], True)
            self.assertIn("confidence_components", first["signal"])
            self.assertEqual(first["signal"]["execution"]["had_open_position_before"], False)
            self.assertEqual(first["signal"]["execution"]["has_open_position_after"], True)
            self.assertEqual(first["signal"]["execution"]["skipped_open"], False)
            self.assertEqual(len(first["signal"]["execution"]["open_fills"]), 1)
            self.assertEqual(len(first["signal"]["execution"]["close_fills"]), 0)
            trade_record = json.loads(lines[-1])
            self.assertEqual(trade_record["event_type"], "trade_close")
            self.assertIn("trade", trade_record)
            self.assertIn("hold_hours", trade_record["trade"])

    def test_runner_tracks_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            records = [
                {
                    "timestamp": "2026-04-04T00:00:00Z",
                    "regime_snapshot": {
                        "ready": True,
                        "adx": 32.0,
                        "atr_ratio": 1.2,
                        "range_width_bps": 180.0,
                        "structure_score": 0.55,
                        "btc_impulse": False,
                    },
                    "symbols": [
                        {
                            "symbol": "ETH",
                            "price": 3100.0,
                            "ema_fast": 3099.0,
                            "ema_slow": 3098.0,
                            "vwap_distance_bps": -25.0,
                            "structure_score": 0.41,
                            "funding_rate": 0.0001,
                            "spread_bps": 1.2,
                            "btc_aligned": True,
                        }
                    ],
                }
            ]
            with input_path.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")

            result = self.runner.run_jsonl(input_path)

            self.assertEqual(result.signal_count, 1)
            self.assertEqual(result.accepted_count, 0)
            self.assertEqual(result.rejected_count, 1)
            self.assertEqual(result.opened_count, 0)
            self.assertEqual(result.skipped_open_count, 0)
            self.assertEqual(result.closed_trade_count, 0)
            self.assertEqual(result.trades_by_symbol, {})
            self.assertEqual(result.trades_by_setup, {})
            self.assertEqual(result.pnl_by_symbol, {})
            self.assertEqual(result.pnl_by_setup, {})
            self.assertEqual(result.records_by_date, {"2026-04-04": 1})
            self.assertEqual(result.signals_by_date, {"2026-04-04": 1})
            self.assertEqual(result.accepted_by_date, {})
            self.assertEqual(result.accepted_by_setup, {})
            self.assertEqual(result.rejected_by_date, {"2026-04-04": 1})
            self.assertEqual(result.rejections_by_reason, {"confidence_below_min": 1})
            self.assertEqual(result.rejected_by_setup, {"trend_pullback_long": 1})

    def test_runner_respects_small_wallet_override(self) -> None:
        config = override_app_config(
            self.config,
            reference_equity_usd=500.0,
            pod_a_default_leverage=2.0,
            pod_a_max_leverage=2.0,
        )
        runner = PodABacktestRunner(config)

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            records = [
                {
                    "timestamp": "2026-04-04T00:00:00Z",
                    "regime_snapshot": {
                        "ready": True,
                        "adx": 32.0,
                        "atr_ratio": 1.2,
                        "range_width_bps": 180.0,
                        "structure_score": 0.55,
                        "btc_impulse": False,
                    },
                    "symbols": [
                        {
                            "symbol": "ETH",
                            "price": 3100.0,
                            "ema_fast": 3090.0,
                            "ema_slow": 3050.0,
                            "vwap_distance_bps": -8.0,
                            "structure_score": 0.62,
                            "funding_rate": 0.0001,
                            "spread_bps": 1.2,
                            "btc_aligned": True,
                        }
                    ],
                },
                {
                    "timestamp": "2026-04-05T00:30:00Z",
                    "regime_snapshot": {
                        "ready": True,
                        "adx": 32.0,
                        "atr_ratio": 1.2,
                        "range_width_bps": 180.0,
                        "structure_score": 0.55,
                        "btc_impulse": False,
                    },
                    "symbols": [
                        {
                            "symbol": "ETH",
                            "price": 3075.0,
                            "ema_fast": 3080.0,
                            "ema_slow": 3045.0,
                            "vwap_distance_bps": -6.0,
                            "structure_score": 0.58,
                            "funding_rate": 0.0001,
                            "spread_bps": 1.3,
                            "btc_aligned": True,
                        }
                    ],
                },
            ]
            with input_path.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")

            result = runner.run_jsonl(input_path)

            self.assertEqual(result.reference_equity_usd, 500.0)
            self.assertEqual(result.accepted_count, 1)
            max_margin_cap = (
                config.trident.capital.reference_equity_usd
                * config.trident.capital.max_allocation_per_symbol_pct
            )
            max_risk_budget = (
                config.trident.capital.reference_equity_usd
                * config.pod_a.risk_per_trade_pct
            )
            self.assertLessEqual(result.max_open_margin_usd, max_margin_cap)
            self.assertLessEqual(
                result.max_open_notional_usd,
                max_margin_cap * config.pod_a.max_leverage,
            )
            self.assertLessEqual(result.max_open_expected_loss_usd, max_risk_budget)


if __name__ == "__main__":
    unittest.main()
