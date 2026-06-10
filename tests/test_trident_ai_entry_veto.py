from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    LLM_REPLAY_DECISION_EVENT,
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
    run_trident_ai_entry_veto_sweep,
    run_trident_ai_entry_veto_replay,
)
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD


VETO_BUCKET = "side_pattern::side=short|microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0"


class TridentAIEntryVetoReplayTests(unittest.TestCase):
    def test_entry_veto_replay_removes_matching_open_and_improves_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal = directory / "decisions.jsonl"
            baseline_paper = directory / "baseline_paper.jsonl"
            market_input = directory / "market.jsonl"
            report_json = directory / "entry_veto.json"
            report_md = directory / "entry_veto.md"
            artifact_dir = directory / "artifacts"

            _write_jsonl(decision_journal, [_decision_record()])
            _write_jsonl(baseline_paper, [_closed_trade_record()])
            _write_jsonl(market_input, [_market_record()])

            result = run_trident_ai_entry_veto_replay(
                decision_journal_paths=(decision_journal,),
                market_input_paths=(market_input,),
                baseline_paper_journal_paths=(baseline_paper,),
                fold_labels=("fixture",),
                veto_buckets=(VETO_BUCKET,),
                report_json_path=report_json,
                report_md_path=report_md,
                artifact_dir=artifact_dir,
                symbols=("BTC",),
            )

            self.assertEqual(result.veto_summary["closed_trades"], 0)
            self.assertEqual(result.delta_summary["closed_trades"], -1)
            self.assertGreater(result.delta_summary["pnl_usd"], 0.0)
            self.assertEqual(result.fold_rows[0]["decisions_vetoed"], 1)
            self.assertEqual(result.verdict, "promising_no_worse_folds")
            report = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_entry_veto_replay")
            self.assertIn("TRIDENT-AI Entry Veto Replay", report_md.read_text(encoding="utf-8"))

    def test_entry_veto_sweep_ranks_matching_bucket_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal = directory / "decisions.jsonl"
            baseline_paper = directory / "baseline_paper.jsonl"
            market_input = directory / "market.jsonl"
            report_json = directory / "entry_veto_sweep.json"
            report_md = directory / "entry_veto_sweep.md"
            artifact_dir = directory / "sweep_artifacts"

            _write_jsonl(decision_journal, [_decision_record()])
            _write_jsonl(baseline_paper, [_closed_trade_record()])
            _write_jsonl(market_input, [_market_record()])

            result = run_trident_ai_entry_veto_sweep(
                decision_journal_paths=(decision_journal,),
                market_input_paths=(market_input,),
                baseline_paper_journal_paths=(baseline_paper,),
                fold_labels=("fixture",),
                veto_buckets=(
                    "side::side=long",
                    VETO_BUCKET,
                ),
                report_json_path=report_json,
                report_md_path=report_md,
                artifact_dir=artifact_dir,
                symbols=("BTC",),
            )

            self.assertEqual(len(result.rows), 2)
            self.assertEqual(result.best_row["veto_bucket"], VETO_BUCKET)
            self.assertGreater(result.best_row["delta_pnl_usd"], 0.0)
            report = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_entry_veto_sweep")
            self.assertIn("TRIDENT-AI Entry Veto Sweep", report_md.read_text(encoding="utf-8"))


def _decision_record() -> dict[str, object]:
    timestamp = "2026-06-07T12:00:00Z"
    context_id = "market_BTC_20260607T120000Z"
    features = {
        "spread_bps": 1.0,
        "microprice_dislocation_bps": -1.0,
        "trade_flow_bias": -0.5,
        "book_imbalance": -0.5,
        "vwap_distance_bps": -5.0,
        "realized_vol_short_bps": 14.0,
    }
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "timestamp": timestamp,
        "symbol": "BTC",
        "request": {"request_id": "request_btc"},
        "context": {
            "schema_version": "trident_ai_market_context_v1",
            "context_id": context_id,
            "as_of": timestamp,
            "symbol": "BTC",
            "price": 100.0,
            "regime": "TrendExpansion",
            "features": features,
            CANDIDATE_HINT_FIELD: {
                "schema_version": "trident_ai_candidate_hint_v6",
                "context_id": context_id,
                "timestamp": timestamp,
                "symbol": "BTC",
                "side": "short",
                "score": 2.0,
                "raw_score": 2.4,
                "directional_score": 2.0,
                "liquidity_score": 1.2,
                "activity_score": 1.0,
                "cost_score": 1.0,
                "edge_quality_score": 1.0,
                "estimated_edge_bps": 52.0,
                "round_trip_cost_bps": 10.0,
                "estimated_net_edge_bps": 42.0,
                "edge_to_cost_ratio": 4.2,
                "reasons": ["microprice_aligned", "flow_book_aligned"],
            },
            "source": "fixture",
        },
        "llm_response": {"usage": {"estimated_cost_usd": 0.0}},
        "proposal": {
            "schema_version": "trident_ai_proposal_v1",
            "decision_id": "decision_btc_short",
            "as_of": timestamp,
            "valid_until": "2026-06-07T12:05:00Z",
            "action": "open",
            "symbol": "BTC",
            "side": "short",
            "confidence": 0.67,
            "time_horizon_minutes": 30,
            "max_notional_usd": 25.0,
            "max_leverage": 1.0,
            "entry_style": "ioc",
            "invalidation_price": 102.0,
            "stop_bps": 120.0,
            "take_profit_bps": 240.0,
            "time_stop_minutes": 30,
            "rationale_tags": ["fixture"],
            "evidence_ids": [context_id],
            "risk_notes": ["fixture"],
        },
        "validation": {"accepted": True, "reason": "accepted"},
    }


def _closed_trade_record() -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
        "timestamp": "2026-06-07T12:30:00Z",
        "symbol": "BTC",
        "close_reason": "time_stop",
        "trade": {
            "symbol": "BTC",
            "side": "short",
            "decision_id": "decision_btc_short",
            "opened_at": "2026-06-07T12:00:00Z",
            "closed_at": "2026-06-07T12:30:00Z",
            "entry_price": 100.0,
            "exit_price": 101.0,
            "notional_usd": 25.0,
            "gross_pnl_usd": -0.25,
            "fees_usd": 0.0175,
            "pnl_usd": -0.2675,
            "close_reason": "time_stop",
            "confidence": 0.67,
        },
    }


def _market_record() -> dict[str, object]:
    return {
        "timestamp": "2026-06-07T12:30:00Z",
        "regime_snapshot": {
            "ready": True,
            "adx": 20.0,
            "atr_ratio": 1.0,
            "range_width_bps": 100.0,
            "structure_score": 0.2,
            "btc_impulse": False,
            "regime": "fixture",
        },
        "symbols": [
            {
                "symbol": "BTC",
                "price": 101.0,
                "ema_fast": 100.0,
                "ema_slow": 101.0,
                "vwap_distance_bps": -5.0,
                "structure_score": 0.2,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
            }
        ],
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
