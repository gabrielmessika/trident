from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
    run_trident_ai_exit_follow_through_audit,
)


class TridentAIExitFollowThroughAuditTests(unittest.TestCase):
    def test_exit_audit_classifies_early_adverse_and_giveback_losses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            paper_journal_path = directory / "paper.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "exit_audit.json"
            report_md_path = directory / "exit_audit.md"

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
                    _market_record("2026-06-07T12:30:00Z", {"BTC": 99.0, "ETH": 100.5}),
                    _market_record("2026-06-07T13:00:00Z", {"BTC": 99.0, "ETH": 99.9}),
                ],
            )

            result = run_trident_ai_exit_follow_through_audit(
                paper_journal_paths=(paper_journal_path,),
                market_input_paths=(market_input_path,),
                fold_labels=("fixture",),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                early_windows_minutes=(15, 30, 60),
                early_adverse_bps=25.0,
                min_follow_through_bps=15.0,
                giveback_bps=25.0,
            )

            self.assertEqual(result.trades_seen, 2)
            self.assertEqual(result.trades_with_path, 2)
            self.assertEqual(result.time_stop_trades, 2)
            self.assertEqual(result.losing_time_stop_trades, 2)
            self.assertEqual(result.classification_counts["early_adverse_loss"], 1)
            self.assertEqual(result.classification_counts["no_follow_through_loss"], 1)
            self.assertEqual(result.classification_counts["gave_back_to_loss"], 1)
            self.assertEqual(result.window_stats["15"]["samples"], 2)
            self.assertAlmostEqual(result.window_stats["15"]["positive_rate"], 0.5)
            self.assertEqual(result.folds[0]["fold_label"], "fixture")

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_exit_follow_through_audit")
            self.assertIn(
                "TRIDENT-AI Exit Follow-Through Audit",
                report_md_path.read_text(encoding="utf-8"),
            )


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
