from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.trident_ai import (
    CANDIDATE_PAPER_DECISION_SOURCE,
    LLM_REPLAY_DECISION_EVENT,
    load_trident_ai_config,
    run_trident_ai_candidate_paper_replay,
)
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD


class TridentAICandidatePaperReplayTests(unittest.TestCase):
    def test_candidate_paper_replay_writes_synthetic_decisions_and_runs_paper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            decision_journal_path = directory / "candidate_decisions.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "candidate_paper.json"
            report_md_path = directory / "candidate_paper.md"

            _write_jsonl(candidate_input_path, [_candidate_record(price=100.0)])
            _write_jsonl(
                market_input_path,
                [
                    _market_record(
                        timestamp="2026-06-07T15:00:00Z",
                        price=101.0,
                    )
                ],
            )

            result = run_trident_ai_candidate_paper_replay(
                candidate_input_path,
                market_input_path=market_input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                decision_journal_path=decision_journal_path,
                journal_path=paper_journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                stop_bps=120.0,
                take_profit_bps=500.0,
                time_stop_minutes=180,
            )

            self.assertEqual(result.candidates_seen, 1)
            self.assertEqual(result.decisions_written, 1)
            self.assertEqual(result.paper_result.positions_opened, 1)
            self.assertEqual(result.paper_result.positions_closed, 1)
            self.assertEqual(result.paper_result.close_reasons["time_stop"], 1)
            self.assertGreater(result.paper_result.realized_pnl_usd, 0.0)
            self.assertEqual(result.paper_result.ai_cost_usd, 0.0)

            decisions = _read_jsonl(decision_journal_path)
            self.assertEqual(decisions[0]["event_type"], LLM_REPLAY_DECISION_EVENT)
            self.assertEqual(decisions[0]["source"], CANDIDATE_PAPER_DECISION_SOURCE)
            self.assertEqual(decisions[0]["proposal"]["action"], "open")
            self.assertEqual(decisions[0]["proposal"]["time_stop_minutes"], 180)

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_candidate_paper_replay")
            self.assertTrue(paper_journal_path.exists())
            self.assertIn("TRIDENT-AI Candidate Paper Replay", report_md_path.read_text())

    def test_candidate_paper_replay_applies_edge_liquidity_cost_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            decision_journal_path = directory / "candidate_decisions.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "candidate_paper.json"
            report_md_path = directory / "candidate_paper.md"

            passing = _candidate_record(price=100.0)
            passing_hint = passing["symbols"][0][CANDIDATE_HINT_FIELD]
            assert isinstance(passing_hint, dict)
            passing_hint.update(
                {
                    "edge_to_cost_ratio": 4.5,
                    "estimated_net_edge_bps": 42.0,
                    "liquidity_score": 1.4,
                    "round_trip_cost_bps": 10.0,
                }
            )
            failing = deepcopy(passing)
            failing["symbols"][0][CANDIDATE_HINT_FIELD]["edge_to_cost_ratio"] = 3.5
            failing["symbols"][0][CANDIDATE_HINT_FIELD]["context_id"] = (
                "market_BTC_20260607T120100Z"
            )
            failing["timestamp"] = "2026-06-07T12:01:00Z"

            _write_jsonl(candidate_input_path, [failing, passing])
            _write_jsonl(
                market_input_path,
                [
                    _market_record(
                        timestamp="2026-06-07T15:00:00Z",
                        price=101.0,
                    )
                ],
            )

            result = run_trident_ai_candidate_paper_replay(
                candidate_input_path,
                market_input_path=market_input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                decision_journal_path=decision_journal_path,
                journal_path=paper_journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                stop_bps=120.0,
                take_profit_bps=500.0,
                time_stop_minutes=180,
                min_edge_to_cost=4.0,
                min_net_edge_bps=35.0,
                min_liquidity_score=1.2,
                max_round_trip_cost_bps=12.0,
            )

            self.assertEqual(result.candidates_seen, 2)
            self.assertEqual(result.decisions_written, 1)
            self.assertEqual(result.skipped_candidates, 1)
            self.assertEqual(result.skip_reasons["edge_to_cost_below_gate"], 1)
            self.assertEqual(result.paper_result.positions_opened, 1)

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["result"]["min_edge_to_cost"], 4.0)
            self.assertEqual(report["result"]["min_net_edge_bps"], 35.0)
            self.assertEqual(report["result"]["min_liquidity_score"], 1.2)
            self.assertEqual(report["result"]["max_round_trip_cost_bps"], 12.0)


def _candidate_record(*, price: float) -> dict[str, object]:
    return {
        "timestamp": "2026-06-07T12:00:00Z",
        "regime_snapshot": {
            "ready": True,
            "adx": 25.0,
            "atr_ratio": 1.0,
            "range_width_bps": 120.0,
            "structure_score": 0.5,
            "btc_impulse": True,
            "regime": "TrendExpansion",
        },
        "symbols": [
            {
                "symbol": "BTC",
                "price": price,
                "ema_fast": price + 1.0,
                "ema_slow": price,
                "vwap_distance_bps": 10.0,
                "structure_score": 0.5,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
                "microprice_dislocation_bps": 1.0,
                "book_imbalance": 0.5,
                "trade_flow_bias": 0.5,
                "bucket_notional_usd": 10_000.0,
                "bucket_trade_count": 20,
                "volume_ratio": 4.0,
                "trade_count_ratio": 2.0,
                "realized_vol_short_bps": 8.0,
                CANDIDATE_HINT_FIELD: {
                    "schema_version": "trident_ai_candidate_hint_v5",
                    "context_id": "market_BTC_20260607T120000Z",
                    "timestamp": "2026-06-07T12:00:00Z",
                    "symbol": "BTC",
                    "side": "long",
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
                    "reasons": ["ema_bullish", "microprice_aligned"],
                },
            }
        ],
    }


def _market_record(*, timestamp: str, price: float) -> dict[str, object]:
    payload = _candidate_record(price=price)
    payload["timestamp"] = timestamp
    symbol = payload["symbols"][0]
    symbol.pop(CANDIDATE_HINT_FIELD, None)
    return payload


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


if __name__ == "__main__":
    unittest.main()
