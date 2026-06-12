from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
    run_trident_ai_exit_overlay_sweep,
)


class TridentAIExitOverlaySweepTests(unittest.TestCase):
    def test_exit_overlay_sweep_improves_early_adverse_and_giveback_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            paper_journal_path = directory / "paper.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "overlay.json"
            report_md_path = directory / "overlay.md"

            _write_jsonl(
                paper_journal_path,
                [
                    _closed_trade(
                        decision_id="btc_early_adverse",
                        symbol="BTC",
                        side="long",
                        entry_price=100.0,
                        exit_price=99.0,
                        gross_pnl=-0.25,
                        pnl=-0.2675,
                    ),
                    _closed_trade(
                        decision_id="eth_giveback",
                        symbol="ETH",
                        side="long",
                        entry_price=100.0,
                        exit_price=99.9,
                        gross_pnl=-0.025,
                        pnl=-0.0425,
                    ),
                ],
            )
            _write_jsonl(
                market_input_path,
                [
                    _market_record("2026-06-07T12:15:00Z", {"BTC": 99.5, "ETH": 101.0}),
                    _market_record("2026-06-07T12:30:00Z", {"BTC": 99.0, "ETH": 100.2}),
                    _market_record("2026-06-07T13:00:00Z", {"BTC": 99.0, "ETH": 99.9}),
                ],
            )

            result = run_trident_ai_exit_overlay_sweep(
                paper_journal_paths=(paper_journal_path,),
                market_input_paths=(market_input_path,),
                fold_labels=("fixture",),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                early_adverse_bps_values=(0.0, 25.0),
                early_window_minutes_values=(15,),
                mfe_activation_bps_values=(0.0, 50.0),
                mfe_giveback_bps_values=(0.0, 50.0),
            )

            self.assertEqual(result.baseline_profile["profile_id"], "baseline_original")
            self.assertLess(
                result.baseline_profile["realized_pnl_usd"],
                result.best_profile["realized_pnl_usd"],
            )
            self.assertEqual(result.best_profile["overlay_exits"], 2)
            self.assertEqual(result.best_robust_profile["profile_id"], result.best_profile["profile_id"])
            self.assertEqual(result.best_robust_profile["improved_fold_count"], 1)
            self.assertEqual(result.best_robust_profile["worse_fold_count"], 0)
            self.assertGreater(result.best_robust_profile["worst_fold_delta_pnl_usd"], 0.0)
            self.assertEqual(result.best_robust_profile["dominant_symbol_trade_ratio"], 0.5)
            self.assertGreater(result.best_profile["delta_pnl_usd"], 0.0)
            self.assertEqual(result.best_profile["exit_reason_counts"]["early_adverse_exit"], 1)
            self.assertEqual(result.best_profile["exit_reason_counts"]["mfe_giveback_exit"], 1)
            self.assertEqual(result.best_profile["folds"][0]["fold_label"], "fixture")

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_exit_overlay_sweep")
            self.assertIn(
                "TRIDENT-AI Exit Overlay Sweep",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_exit_overlay_sweep_can_use_no_follow_through_without_cutting_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            paper_journal_path = directory / "paper.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "overlay.json"
            report_md_path = directory / "overlay.md"

            _write_jsonl(
                paper_journal_path,
                [
                    _closed_trade(
                        decision_id="btc_no_follow",
                        symbol="BTC",
                        side="long",
                        entry_price=100.0,
                        exit_price=99.0,
                        gross_pnl=-0.25,
                        pnl=-0.2675,
                    ),
                    _closed_trade(
                        decision_id="eth_followed",
                        symbol="ETH",
                        side="long",
                        entry_price=100.0,
                        exit_price=101.0,
                        gross_pnl=0.25,
                        pnl=0.2325,
                    ),
                ],
            )
            _write_jsonl(
                market_input_path,
                [
                    _market_record("2026-06-07T12:15:00Z", {"BTC": 99.9, "ETH": 100.2}),
                    _market_record("2026-06-07T12:30:00Z", {"BTC": 99.6, "ETH": 100.6}),
                    _market_record("2026-06-07T13:00:00Z", {"BTC": 99.0, "ETH": 101.0}),
                ],
            )

            result = run_trident_ai_exit_overlay_sweep(
                paper_journal_paths=(paper_journal_path,),
                market_input_paths=(market_input_path,),
                fold_labels=("fixture",),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                early_adverse_bps_values=(0.0,),
                early_window_minutes_values=(15,),
                mfe_activation_bps_values=(0.0,),
                mfe_giveback_bps_values=(0.0,),
                follow_through_window_minutes_values=(15,),
                min_follow_through_bps_values=(15.0,),
                max_follow_through_gross_bps_values=(0.0,),
            )

            self.assertEqual(result.best_profile["profile_id"], "nft15@15m_max0")
            self.assertGreater(result.best_profile["realized_pnl_usd"], 0.0)
            self.assertEqual(result.best_profile["exit_reason_counts"]["no_follow_through_exit"], 1)
            self.assertEqual(result.best_profile["exit_reason_counts"]["original_time_stop"], 1)
            self.assertEqual(result.best_profile["worse_fold_count"], 0)


def _closed_trade(
    *,
    decision_id: str,
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    gross_pnl: float,
    pnl: float,
) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
        "timestamp": "2026-06-07T13:00:00Z",
        "symbol": symbol,
        "close_reason": "time_stop",
        "trade": {
            "symbol": symbol,
            "side": side,
            "decision_id": decision_id,
            "opened_at": "2026-06-07T12:00:00Z",
            "closed_at": "2026-06-07T13:00:00Z",
            "entry_price": entry_price,
            "exit_price": exit_price,
            "notional_usd": 25.0,
            "gross_pnl_usd": gross_pnl,
            "fees_usd": 0.0175,
            "pnl_usd": pnl,
            "close_reason": "time_stop",
            "confidence": 0.62,
        },
    }


def _market_record(timestamp: str, prices: dict[str, float]) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbols": [
            {"symbol": symbol, "price": price}
            for symbol, price in sorted(prices.items())
        ],
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
