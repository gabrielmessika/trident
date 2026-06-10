from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    PAPER_REPLAY_DECISION_EVENT,
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
    run_trident_ai_pattern_calibration_report,
    run_trident_ai_pattern_fold_validation_report,
)
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


class TridentAIPatternCalibrationTests(unittest.TestCase):
    def test_pattern_calibration_groups_by_signal_pattern_not_symbol_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal = directory / "decisions.jsonl"
            paper_journal = directory / "paper.jsonl"
            report_json_path = directory / "pattern.json"
            report_md_path = directory / "pattern.md"

            _write_jsonl(
                decision_journal,
                [
                    _decision_record(
                        symbol="BTC",
                        decision_id="btc_long",
                        side="long",
                        timestamp="2026-06-07T12:00:00Z",
                    ),
                    _decision_record(
                        symbol="ETH",
                        decision_id="eth_long",
                        side="long",
                        timestamp="2026-06-07T12:01:00Z",
                    ),
                ],
            )
            _write_jsonl(
                paper_journal,
                [
                    _paper_decision("btc_long", "BTC", "open"),
                    _closed_trade("btc_long", "BTC", pnl=0.20),
                    _paper_decision("eth_long", "ETH", "open"),
                    _closed_trade("eth_long", "ETH", pnl=-0.10),
                ],
            )

            result = run_trident_ai_pattern_calibration_report(
                decision_journal_paths=(decision_journal,),
                paper_journal_paths=(paper_journal,),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                min_trades_per_pattern=1,
            )

            self.assertEqual(result.open_decisions, 2)
            self.assertEqual(result.paper_opens, 2)
            self.assertEqual(result.closed_trades, 2)
            self.assertEqual(result.winning_trades, 1)
            self.assertEqual(result.losing_trades, 1)
            self.assertAlmostEqual(result.realized_pnl_usd, 0.10)
            self.assertEqual(len(result.pattern_buckets), 1)
            bucket = result.pattern_buckets[0]
            self.assertEqual(bucket["closed_trades"], 2)
            self.assertEqual(bucket["symbols"], {"BTC": 1, "ETH": 1})
            self.assertEqual(bucket["reliability"], "positive_pattern_watchlist")
            self.assertIn("microprice=aligned", bucket["pattern"])

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_pattern_calibration_report")
            self.assertIn(
                "TRIDENT-AI Pattern Calibration Report",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_pattern_fold_validation_requires_multi_fold_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            fold_a_decisions = directory / "fold_a_decisions.jsonl"
            fold_a_paper = directory / "fold_a_paper.jsonl"
            fold_b_decisions = directory / "fold_b_decisions.jsonl"
            fold_b_paper = directory / "fold_b_paper.jsonl"
            report_json_path = directory / "folds.json"
            report_md_path = directory / "folds.md"

            _write_jsonl(
                fold_a_decisions,
                [
                    _decision_record(
                        symbol="BTC",
                        decision_id="btc_long_a",
                        side="long",
                        timestamp="2026-06-07T12:00:00Z",
                    )
                ],
            )
            _write_jsonl(
                fold_a_paper,
                [
                    _paper_decision("btc_long_a", "BTC", "open"),
                    _closed_trade("btc_long_a", "BTC", pnl=0.20),
                ],
            )
            _write_jsonl(
                fold_b_decisions,
                [
                    _decision_record(
                        symbol="ETH",
                        decision_id="eth_long_b",
                        side="long",
                        timestamp="2026-06-08T12:00:00Z",
                    )
                ],
            )
            _write_jsonl(
                fold_b_paper,
                [
                    _paper_decision("eth_long_b", "ETH", "open"),
                    _closed_trade("eth_long_b", "ETH", pnl=0.30),
                ],
            )

            result = run_trident_ai_pattern_fold_validation_report(
                decision_journal_paths=(fold_a_decisions, fold_b_decisions),
                paper_journal_paths=(fold_a_paper, fold_b_paper),
                fold_labels=("fold_a", "fold_b"),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                min_trades_per_fold=1,
                min_positive_folds=2,
            )

            self.assertEqual(len(result.folds), 2)
            self.assertEqual(len(result.stable_positive_patterns), 1)
            stable = result.stable_positive_patterns[0]
            self.assertEqual(stable["positive_folds"], 2)
            self.assertEqual(stable["classification"], "stable_positive")
            self.assertIn("microprice=aligned", stable["pattern"])
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_pattern_fold_validation_report")
            self.assertIn(
                "TRIDENT-AI Pattern Fold Validation Report",
                report_md_path.read_text(encoding="utf-8"),
            )


def _decision_record(
    *,
    symbol: str,
    decision_id: str,
    side: str,
    timestamp: str,
) -> dict[str, object]:
    features = {
        "spread_bps": 1.0,
        "ema_alignment": "bullish",
        "microprice_dislocation_bps": 1.0,
        "trade_flow_bias": 0.5,
        "book_imbalance": 0.5,
        "vwap_distance_bps": 5.0,
        "realized_vol_short_bps": 10.0,
    }
    hint = {
        "schema_version": "trident_ai_candidate_hint_v5",
        "context_id": f"market_{symbol}_20260607T120000Z",
        "timestamp": timestamp,
        "symbol": symbol,
        "side": side,
        "score": 2.0,
        "raw_score": 2.2,
        "directional_score": 1.8,
        "liquidity_score": 1.0,
        "activity_score": 1.0,
        "cost_score": 1.0,
        "edge_quality_score": 1.0,
        "estimated_edge_bps": 24.0,
        "round_trip_cost_bps": 8.0,
        "estimated_net_edge_bps": 16.0,
        "edge_to_cost_ratio": 3.0,
        "reasons": ["ema_bullish", "microprice_aligned", "trade_flow_bias"],
    }
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "timestamp": timestamp,
        "symbol": symbol,
        "context": {
            "schema_version": "trident_ai_market_context_v1",
            "context_id": hint["context_id"],
            "as_of": timestamp,
            "symbol": symbol,
            "price": 100.0,
            "regime": "TrendExpansion",
            "features": features,
            CANDIDATE_HINT_FIELD: hint,
            "source": "fixture",
        },
        "proposal": {
            "schema_version": "trident_ai_proposal_v1",
            "decision_id": decision_id,
            "as_of": timestamp,
            "valid_until": "2026-06-07T12:05:00Z",
            "action": "open",
            "symbol": symbol,
            "side": side,
            "confidence": 0.62,
            "time_horizon_minutes": 180,
            "max_notional_usd": 25.0,
            "max_leverage": 1.0,
            "entry_style": "ioc",
            "invalidation_price": 95.0,
            "stop_bps": 240.0,
            "take_profit_bps": 480.0,
            "time_stop_minutes": 180,
            "rationale_tags": ["fixture"],
            "evidence_ids": [str(hint["context_id"])],
            "risk_notes": ["fixture"],
        },
        "validation": {"accepted": True, "reason": "accepted"},
    }


def _paper_decision(decision_id: str, symbol: str, action: str) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_DECISION_EVENT,
        "timestamp": "2026-06-07T12:00:00Z",
        "symbol": symbol,
        "decision_id": decision_id,
        "proposal_action": "open",
        "paper_action": action,
        "reason": "agent_open" if action == "open" else "max_open_positions_reached",
        "price": 100.0,
    }


def _closed_trade(decision_id: str, symbol: str, *, pnl: float) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
        "timestamp": "2026-06-07T15:00:00Z",
        "symbol": symbol,
        "close_reason": "time_stop",
        "trade": {
            "symbol": symbol,
            "side": "long",
            "decision_id": decision_id,
            "opened_at": "2026-06-07T12:00:00Z",
            "closed_at": "2026-06-07T15:00:00Z",
            "entry_price": 100.0,
            "exit_price": 101.0 if pnl > 0 else 99.0,
            "notional_usd": 25.0,
            "gross_pnl_usd": pnl + 0.0175,
            "fees_usd": 0.0175,
            "pnl_usd": pnl,
            "close_reason": "time_stop",
            "confidence": 0.62,
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
