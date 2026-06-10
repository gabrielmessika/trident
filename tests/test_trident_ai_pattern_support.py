from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    PAPER_REPLAY_DECISION_EVENT,
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
    run_trident_ai_pattern_support_audit,
)
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


class TridentAIPatternSupportAuditTests(unittest.TestCase):
    def test_pattern_support_requires_fold_and_symbol_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            fold_a_decisions = directory / "fold_a_decisions.jsonl"
            fold_a_paper = directory / "fold_a_paper.jsonl"
            fold_b_decisions = directory / "fold_b_decisions.jsonl"
            fold_b_paper = directory / "fold_b_paper.jsonl"
            report_json_path = directory / "support.json"
            report_md_path = directory / "support.md"

            _write_jsonl(
                fold_a_decisions,
                [
                    _decision_record(
                        symbol="BTC",
                        decision_id="btc_long_a",
                        timestamp="2026-06-07T12:00:00Z",
                    )
                ],
            )
            _write_jsonl(
                fold_a_paper,
                [
                    _paper_decision("btc_long_a", "BTC"),
                    _closed_trade("btc_long_a", "BTC", pnl=0.20),
                ],
            )
            _write_jsonl(
                fold_b_decisions,
                [
                    _decision_record(
                        symbol="ETH",
                        decision_id="eth_long_b",
                        timestamp="2026-06-08T12:00:00Z",
                    )
                ],
            )
            _write_jsonl(
                fold_b_paper,
                [
                    _paper_decision("eth_long_b", "ETH"),
                    _closed_trade("eth_long_b", "ETH", pnl=0.30),
                ],
            )

            result = run_trident_ai_pattern_support_audit(
                decision_journal_paths=(fold_a_decisions, fold_b_decisions),
                paper_journal_paths=(fold_a_paper, fold_b_paper),
                fold_labels=("fold_a", "fold_b"),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                symbols=("BTC", "ETH", "SOL", "HYPE"),
                min_closed_trades=2,
                min_folds=2,
                min_positive_folds=2,
                min_symbols=2,
            )

            self.assertEqual(result.summary["closed_trades"], 2)
            pattern_rows = [
                row
                for row in result.symbol_agnostic_positive_buckets
                if row["bucket_family"] == "pattern"
            ]
            self.assertEqual(len(pattern_rows), 1)
            self.assertEqual(pattern_rows[0]["classification"], "symbol_agnostic_positive")
            self.assertEqual(pattern_rows[0]["positive_folds"], 2)
            self.assertEqual(pattern_rows[0]["symbols_with_closed"], 2)
            self.assertEqual(pattern_rows[0]["dominant_symbol_trade_ratio"], 0.5)

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_pattern_support_audit")
            self.assertIn(
                "TRIDENT-AI Pattern Support Audit",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_pattern_support_allows_missing_paper_journal_when_decisions_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decisions = directory / "empty_decisions.jsonl"
            missing_paper = directory / "missing_paper.jsonl"
            report_json_path = directory / "support.json"
            report_md_path = directory / "support.md"
            decisions.write_text("", encoding="utf-8")

            result = run_trident_ai_pattern_support_audit(
                decision_journal_paths=(decisions,),
                paper_journal_paths=(missing_paper,),
                fold_labels=("empty_fold",),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                symbols=("BTC", "ETH", "SOL", "HYPE"),
                min_closed_trades=1,
                min_folds=1,
                min_positive_folds=1,
                min_symbols=1,
            )

            self.assertEqual(result.summary["decisions_seen"], 0)
            self.assertEqual(result.summary["closed_trades"], 0)
            self.assertEqual(result.folds[0]["fold"], "empty_fold")
            self.assertTrue(report_json_path.exists())


def _decision_record(
    *,
    symbol: str,
    decision_id: str,
    timestamp: str,
) -> dict[str, object]:
    features = {
        "market_cluster": "crypto",
        "spread_bps": 1.0,
        "microprice_dislocation_bps": 1.0,
        "trade_flow_bias": 0.5,
        "book_imbalance": 0.5,
        "vwap_distance_bps": 5.0,
        "realized_vol_short_bps": 10.0,
    }
    hint = {
        "schema_version": "trident_ai_candidate_hint_v6",
        "context_id": f"market_{symbol}_20260607T120000Z",
        "timestamp": timestamp,
        "symbol": symbol,
        "side": "long",
        "score": 2.0,
        "raw_score": 2.2,
        "directional_score": 1.8,
        "liquidity_score": 1.1,
        "activity_score": 1.0,
        "cost_score": 1.0,
        "edge_quality_score": 1.0,
        "estimated_edge_bps": 24.0,
        "round_trip_cost_bps": 8.0,
        "estimated_net_edge_bps": 16.0,
        "edge_to_cost_ratio": 3.0,
        "reasons": ["ema_bullish", "microprice_aligned", "trade_flow_bias"],
        "pattern_reasons": ["fixture_pattern"],
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
            "side": "long",
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


def _paper_decision(decision_id: str, symbol: str) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_DECISION_EVENT,
        "timestamp": "2026-06-07T12:00:00Z",
        "symbol": symbol,
        "decision_id": decision_id,
        "proposal_action": "open",
        "paper_action": "open",
        "reason": "agent_open",
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
            "exit_price": 101.0,
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
