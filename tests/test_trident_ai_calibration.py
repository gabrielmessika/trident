from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    run_trident_ai_calibration_report,
    run_trident_ai_edge_calibration_report,
)
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.paper import (
    PAPER_REPLAY_DECISION_EVENT,
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
)
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


class TridentAICalibrationReportTests(unittest.TestCase):
    def test_calibration_report_joins_candidate_llm_and_paper_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            llm_journal_path = directory / "llm.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "calibration.json"
            report_md_path = directory / "calibration.md"

            _write_jsonl(
                candidate_input_path,
                [
                    _candidate_record("BTC", "market_BTC_20260607T120000Z", 2.1),
                    _candidate_record("ETH", "market_ETH_20260607T120000Z", 1.4),
                ],
            )
            _write_jsonl(
                llm_journal_path,
                [
                    _llm_decision(
                        symbol="BTC",
                        context_id="market_BTC_20260607T120000Z",
                        request_id="request_btc",
                        decision_id="decision_btc",
                        action="open",
                        confidence=0.67,
                        cost=0.002,
                    ),
                ],
            )
            _write_jsonl(
                paper_journal_path,
                [
                    _paper_decision(
                        symbol="BTC",
                        request_id="request_btc",
                        decision_id="decision_btc",
                        paper_action="open",
                    ),
                    _closed_trade(decision_id="decision_btc", pnl=0.25),
                ],
            )

            result = run_trident_ai_calibration_report(
                candidate_input_path=candidate_input_path,
                llm_journal_path=llm_journal_path,
                paper_journal_path=paper_journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
            )

            self.assertEqual(result.candidates_seen, 2)
            self.assertEqual(result.llm_decisions_seen, 1)
            self.assertEqual(result.matched_candidates, 1)
            self.assertEqual(result.missing_llm_decisions, 1)
            self.assertEqual(result.matched_paper_decisions, 1)
            self.assertEqual(result.closed_trades, 1)
            self.assertEqual(result.winning_trades, 1)
            self.assertEqual(result.llm_action_counts["open"], 1)
            self.assertEqual(result.paper_action_counts["open"], 1)
            self.assertAlmostEqual(result.ai_cost_usd, 0.002)
            self.assertAlmostEqual(result.realized_pnl_usd, 0.25)
            self.assertAlmostEqual(result.net_after_ai_cost_usd, 0.248)
            self.assertEqual(result.items[1]["status"], "missing_llm_decision")

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_calibration_report")
            self.assertIn(
                "TRIDENT-AI Calibration Report",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_edge_calibration_report_compares_estimated_edge_to_realized_pnl_bps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            llm_journal_path = directory / "llm.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "edge_calibration.json"
            report_md_path = directory / "edge_calibration.md"

            _write_jsonl(
                candidate_input_path,
                [
                    _candidate_record(
                        "BTC",
                        "market_BTC_20260607T120000Z",
                        1.6,
                        estimated_edge_bps=12.0,
                        round_trip_cost_bps=9.0,
                        edge_to_cost_ratio=1.333333,
                    ),
                ],
            )
            _write_jsonl(
                llm_journal_path,
                [
                    _llm_decision(
                        symbol="BTC",
                        context_id="market_BTC_20260607T120000Z",
                        request_id="request_btc",
                        decision_id="decision_btc",
                        action="open",
                        confidence=0.67,
                        cost=0.002,
                    ),
                ],
            )
            _write_jsonl(
                paper_journal_path,
                [
                    _paper_decision(
                        symbol="BTC",
                        request_id="request_btc",
                        decision_id="decision_btc",
                        paper_action="open",
                    ),
                    _closed_trade(decision_id="decision_btc", pnl=-0.055),
                ],
            )

            result = run_trident_ai_edge_calibration_report(
                candidate_input_path=candidate_input_path,
                llm_journal_path=llm_journal_path,
                paper_journal_path=paper_journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
            )

            self.assertEqual(result.candidates_seen, 1)
            self.assertEqual(result.open_decisions, 1)
            self.assertEqual(result.closed_trades, 1)
            self.assertEqual(result.false_positive_trades, 1)
            self.assertEqual(result.overestimated_trades, 1)
            self.assertAlmostEqual(result.avg_estimated_edge_bps, 12.0)
            self.assertAlmostEqual(result.avg_estimated_net_edge_bps, 3.0)
            self.assertAlmostEqual(result.avg_realized_net_bps, -22.0)
            self.assertGreaterEqual(result.suggested_min_edge_to_cost, 1.5)
            self.assertGreaterEqual(result.suggested_min_net_edge_bps, 5.0)
            self.assertEqual(result.sample_warning, "sample_too_small_keep_conservative_gates")

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_edge_calibration_report")
            self.assertIn(
                "TRIDENT-AI Edge Calibration Report",
                report_md_path.read_text(encoding="utf-8"),
            )


def _candidate_record(
    symbol: str,
    context_id: str,
    score: float,
    *,
    estimated_edge_bps: float = 11.0,
    round_trip_cost_bps: float = 12.0,
    edge_to_cost_ratio: float = 0.916667,
) -> dict[str, object]:
    timestamp = "2026-06-07T12:00:00Z"
    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 20.0,
            "atr_ratio": 1.0,
            "range_width_bps": 100.0,
            "structure_score": 0.2,
            "btc_impulse": False,
        },
        "symbols": [
            {
                "symbol": symbol,
                "price": 100.0,
                "ema_fast": 101.0,
                "ema_slow": 100.0,
                "vwap_distance_bps": 1.0,
                "structure_score": 0.2,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
                CANDIDATE_HINT_FIELD: {
                    "schema_version": "trident_ai_candidate_hint_v1",
                    "context_id": context_id,
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "side": "long",
                    "score": score,
                    "raw_score": score + 0.25,
                    "directional_score": 1.8,
                    "liquidity_score": 1.0,
                    "activity_score": 1.0,
                    "cost_score": 0.9,
                    "estimated_edge_bps": estimated_edge_bps,
                    "round_trip_cost_bps": round_trip_cost_bps,
                    "estimated_net_edge_bps": estimated_edge_bps - round_trip_cost_bps,
                    "edge_to_cost_ratio": edge_to_cost_ratio,
                    "reasons": ["ema_bullish", "spread_ok"],
                },
            }
        ],
    }


def _llm_decision(
    *,
    symbol: str,
    context_id: str,
    request_id: str,
    decision_id: str,
    action: str,
    confidence: float,
    cost: float,
) -> dict[str, object]:
    timestamp = "2026-06-07T12:00:00Z"
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "timestamp": timestamp,
        "symbol": symbol,
        "request": {"request_id": request_id},
        "context": {
            "schema_version": "trident_ai_market_context_v1",
            "context_id": context_id,
            "as_of": timestamp,
            "symbol": symbol,
            "price": 100.0,
            "regime": "fixture",
            "features": {"spread_bps": 1.0},
            "source": "fixture",
        },
        "llm_response": {"usage": {"estimated_cost_usd": cost}},
        "proposal": {
            "schema_version": "trident_ai_proposal_v1",
            "decision_id": decision_id,
            "as_of": timestamp,
            "valid_until": "2026-06-07T12:05:00Z",
            "action": action,
            "symbol": symbol,
            "side": "long",
            "confidence": confidence,
            "time_horizon_minutes": 15,
            "max_notional_usd": 25.0,
            "max_leverage": 1.0,
            "entry_style": "ioc",
            "invalidation_price": 99.0,
            "stop_bps": 20.0,
            "take_profit_bps": 40.0,
            "time_stop_minutes": 30,
            "rationale_tags": ["fixture"],
            "evidence_ids": [context_id],
            "risk_notes": ["fixture"],
        },
        "validation": {"accepted": True, "reason": "accepted"},
    }


def _paper_decision(
    *,
    symbol: str,
    request_id: str,
    decision_id: str,
    paper_action: str,
) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_DECISION_EVENT,
        "timestamp": "2026-06-07T12:00:00Z",
        "symbol": symbol,
        "request_id": request_id,
        "decision_id": decision_id,
        "proposal_action": "open",
        "paper_action": paper_action,
        "reason": "agent_open",
        "price": 100.0,
    }


def _closed_trade(*, decision_id: str, pnl: float) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
        "timestamp": "2026-06-07T12:10:00Z",
        "symbol": "BTC",
        "close_reason": "take_profit_hit",
        "trade": {
            "symbol": "BTC",
            "side": "long",
            "decision_id": decision_id,
            "opened_at": "2026-06-07T12:00:00Z",
            "closed_at": "2026-06-07T12:10:00Z",
            "entry_price": 100.0,
            "exit_price": 101.0,
            "notional_usd": 25.0,
            "gross_pnl_usd": 0.2675,
            "fees_usd": 0.0175,
            "pnl_usd": pnl,
            "close_reason": "take_profit_hit",
            "confidence": 0.67,
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
