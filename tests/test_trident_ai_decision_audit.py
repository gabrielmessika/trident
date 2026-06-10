from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import run_trident_ai_llm_decision_audit
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


class TridentAIDecisionAuditTests(unittest.TestCase):
    def test_audit_flags_false_threshold_claims_on_eligible_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            llm_journal_path = directory / "llm.jsonl"
            report_json_path = directory / "audit.json"
            report_md_path = directory / "audit.md"

            _write_jsonl(
                candidate_input_path,
                [
                    _candidate_record(
                        symbol="BTC",
                        context_id="market_BTC_20260607T120000Z",
                        edge_to_cost_ratio=1.87,
                        estimated_net_edge_bps=11.6,
                    )
                ],
            )
            _write_jsonl(
                llm_journal_path,
                [
                    _llm_hold_decision(
                        symbol="BTC",
                        context_id="market_BTC_20260607T120000Z",
                        evidence_ids=[
                            "market_BTC_20260607T120000Z",
                            "edge_to_cost_below_threshold",
                            "net_edge_bps_below_threshold",
                        ],
                        risk_notes=[
                            "Round-trip cost is high relative to edge.",
                            "Net edge is below hold/open threshold.",
                        ],
                    )
                ],
            )

            result = run_trident_ai_llm_decision_audit(
                candidate_input_path=candidate_input_path,
                llm_journal_path=llm_journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
            )

            self.assertEqual(result.candidates_seen, 1)
            self.assertEqual(result.matched_llm_decisions, 1)
            self.assertEqual(result.hold_decisions, 1)
            self.assertEqual(result.eligible_candidates, 1)
            self.assertEqual(result.eligible_holds, 1)
            self.assertEqual(result.contradictory_decisions, 1)
            self.assertEqual(result.contradiction_counts["false_edge_to_cost_below_threshold"], 1)
            self.assertEqual(result.contradiction_counts["false_net_edge_below_threshold"], 1)
            self.assertEqual(result.items[0]["status"], "contradiction")

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_llm_decision_audit")
            self.assertIn(
                "TRIDENT-AI LLM Decision Audit",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_audit_tracks_eligible_hold_without_false_threshold_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            llm_journal_path = directory / "llm.jsonl"
            report_json_path = directory / "audit.json"
            report_md_path = directory / "audit.md"

            _write_jsonl(
                candidate_input_path,
                [
                    _candidate_record(
                        symbol="ETH",
                        context_id="market_ETH_20260607T120000Z",
                        edge_to_cost_ratio=3.1,
                        estimated_net_edge_bps=18.4,
                    )
                ],
            )
            _write_jsonl(
                llm_journal_path,
                [
                    _llm_hold_decision(
                        symbol="ETH",
                        context_id="market_ETH_20260607T120000Z",
                        evidence_ids=["market_ETH_20260607T120000Z"],
                        risk_notes=["Hold: confidence below minimum threshold despite positive candidate."],
                    )
                ],
            )

            result = run_trident_ai_llm_decision_audit(
                candidate_input_path=candidate_input_path,
                llm_journal_path=llm_journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
            )

            self.assertEqual(result.eligible_holds, 1)
            self.assertEqual(result.contradictory_decisions, 0)
            self.assertEqual(result.items[0]["status"], "eligible_hold")


def _candidate_record(
    *,
    symbol: str,
    context_id: str,
    edge_to_cost_ratio: float,
    estimated_net_edge_bps: float,
) -> dict[str, object]:
    estimated_edge_bps = estimated_net_edge_bps + 8.0
    timestamp = "2026-06-07T12:00:00Z"
    return {
        "timestamp": timestamp,
        "symbols": [
            {
                "symbol": symbol,
                "price": 100.0,
                "spread_bps": 1.0,
                "microprice_dislocation_bps": 0.2,
                CANDIDATE_HINT_FIELD: {
                    "schema_version": "trident_ai_candidate_hint_v4",
                    "context_id": context_id,
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "side": "long",
                    "score": 2.0,
                    "estimated_edge_bps": estimated_edge_bps,
                    "round_trip_cost_bps": 8.0,
                    "estimated_net_edge_bps": estimated_net_edge_bps,
                    "edge_to_cost_ratio": edge_to_cost_ratio,
                    "reasons": ["ema_bullish", "cost_edge_ok"],
                },
            }
        ],
    }


def _llm_hold_decision(
    *,
    symbol: str,
    context_id: str,
    evidence_ids: list[str],
    risk_notes: list[str],
) -> dict[str, object]:
    timestamp = "2026-06-07T12:00:00Z"
    proposal = {
        "schema_version": "trident_ai_proposal_v1",
        "decision_id": f"{symbol.lower()}_hold",
        "as_of": timestamp,
        "valid_until": "2026-06-07T12:05:00Z",
        "action": "hold",
        "symbol": symbol,
        "side": "long",
        "confidence": 0.52,
        "time_horizon_minutes": 15,
        "max_notional_usd": 0.0,
        "max_leverage": 0.0,
        "entry_style": "none",
        "invalidation_price": 0.0,
        "stop_bps": 0.0,
        "take_profit_bps": 0.0,
        "time_stop_minutes": 0,
        "rationale_tags": ["fixture"],
        "evidence_ids": evidence_ids,
        "risk_notes": risk_notes,
    }
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "timestamp": timestamp,
        "symbol": symbol,
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
        "llm_response": {"ok": True, "raw_text": json.dumps(proposal, sort_keys=True)},
        "proposal": proposal,
        "validation": {"accepted": True, "reason": "accepted"},
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
