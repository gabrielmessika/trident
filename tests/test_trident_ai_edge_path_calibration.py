from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    LLM_REPLAY_DECISION_EVENT,
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
    run_trident_ai_edge_path_calibration_report,
)
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.cli import main


class TridentAIEdgePathCalibrationTests(unittest.TestCase):
    def test_edge_path_calibration_detects_non_separable_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_path = directory / "candidates.jsonl"
            llm_path = directory / "llm.jsonl"
            paper_path = directory / "paper.jsonl"
            market_path = directory / "market.jsonl"
            report_json_path = directory / "edge_path.json"
            report_md_path = directory / "edge_path.md"

            _write_fixture_files(candidate_path, llm_path, paper_path, market_path)

            result = run_trident_ai_edge_path_calibration_report(
                candidate_input_paths=(candidate_path,),
                llm_journal_paths=(llm_path,),
                paper_journal_paths=(paper_path,),
                market_input_paths=(market_path,),
                fold_labels=("fixture",),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                symbols=("BTC", "ETH"),
                windows_minutes=(5, 15, 60),
                early_adverse_bps=20.0,
                min_follow_through_bps=15.0,
                giveback_bps=25.0,
                fast_invalidation_minutes=15,
            )

            self.assertEqual(result.summary["closed_trades"], 2)
            self.assertEqual(result.summary["false_positive_trades"], 1)
            self.assertEqual(result.summary["fast_invalidations"], 1)
            self.assertEqual(
                result.threshold_diagnostics["verdict"],
                "edge_thresholds_do_not_separate_winners_from_false_positives",
            )
            self.assertTrue(
                result.threshold_diagnostics["suggested_min_net_edge_would_block_winner"]
            )
            self.assertTrue(report_json_path.exists())
            self.assertIn(
                "TRIDENT-AI Edge Path Calibration",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_edge_path_calibration_cli_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_path = directory / "candidates.jsonl"
            llm_path = directory / "llm.jsonl"
            paper_path = directory / "paper.jsonl"
            market_path = directory / "market.jsonl"
            report_json_path = directory / "edge_path_cli.json"
            report_md_path = directory / "edge_path_cli.md"

            _write_fixture_files(candidate_path, llm_path, paper_path, market_path)

            exit_code = main(
                [
                    "edge-path-calibration",
                    "--candidate-input",
                    str(candidate_path),
                    "--llm-journal",
                    str(llm_path),
                    "--paper-journal",
                    str(paper_path),
                    "--market-input",
                    str(market_path),
                    "--fold-label",
                    "fixture",
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--symbols",
                    "BTC,ETH",
                    "--windows-minutes",
                    "5,15,60",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_edge_path_calibration_report")
            self.assertEqual(report["result"]["summary"]["closed_trades"], 2)
            self.assertTrue(report_md_path.exists())


def _write_fixture_files(
    candidate_path: Path,
    llm_path: Path,
    paper_path: Path,
    market_path: Path,
) -> None:
    _write_jsonl(
        candidate_path,
        [
            _candidate_record(
                timestamp="2026-06-07T12:00:00Z",
                symbol="BTC",
                context_id="ctx_win",
                side="long",
                estimated_edge=28.0,
                cost=8.0,
                edge_to_cost=3.5,
                score=4.2,
            ),
            _candidate_record(
                timestamp="2026-06-07T13:00:00Z",
                symbol="ETH",
                context_id="ctx_loss",
                side="long",
                estimated_edge=40.0,
                cost=10.0,
                edge_to_cost=4.0,
                score=5.1,
            ),
        ],
    )
    _write_jsonl(
        llm_path,
        [
            _llm_record(
                timestamp="2026-06-07T12:00:00Z",
                symbol="BTC",
                context_id="ctx_win",
                decision_id="decision_win",
            ),
            _llm_record(
                timestamp="2026-06-07T13:00:00Z",
                symbol="ETH",
                context_id="ctx_loss",
                decision_id="decision_loss",
            ),
        ],
    )
    _write_jsonl(
        paper_path,
        [
            _closed_trade(
                decision_id="decision_win",
                symbol="BTC",
                opened_at="2026-06-07T12:00:00Z",
                closed_at="2026-06-07T13:00:00Z",
                exit_price=101.0,
                pnl=0.2325,
                gross=0.25,
                close_reason="time_stop",
            ),
            _closed_trade(
                decision_id="decision_loss",
                symbol="ETH",
                opened_at="2026-06-07T13:00:00Z",
                closed_at="2026-06-07T13:12:00Z",
                exit_price=99.0,
                pnl=-0.2675,
                gross=-0.25,
                close_reason="invalidation_price_hit",
            ),
        ],
    )
    _write_jsonl(
        market_path,
        [
            _market_record("2026-06-07T12:05:00Z", {"BTC": 100.2}),
            _market_record("2026-06-07T12:30:00Z", {"BTC": 100.7}),
            _market_record("2026-06-07T13:00:00Z", {"BTC": 101.0, "ETH": 100.0}),
            _market_record("2026-06-07T13:05:00Z", {"ETH": 99.6}),
            _market_record("2026-06-07T13:12:00Z", {"ETH": 99.0}),
        ],
    )


def _candidate_record(
    *,
    timestamp: str,
    symbol: str,
    context_id: str,
    side: str,
    estimated_edge: float,
    cost: float,
    edge_to_cost: float,
    score: float,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbols": [
            {
                "symbol": symbol,
                "price": 100.0,
                "spread_bps": 1.0,
                "microprice_dislocation_bps": 1.0 if side == "long" else -1.0,
                "vwap_distance_bps": 5.0 if side == "long" else -5.0,
                "structure_score": 0.5 if side == "long" else -0.5,
                "trade_flow_bias": 0.5 if side == "long" else -0.5,
                "book_imbalance": 0.4 if side == "long" else -0.4,
                CANDIDATE_HINT_FIELD: {
                    "schema_version": "trident_ai_candidate_hint_v6",
                    "context_id": context_id,
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "side": side,
                    "score": score,
                    "raw_score": score,
                    "estimated_edge_bps": estimated_edge,
                    "round_trip_cost_bps": cost,
                    "estimated_net_edge_bps": estimated_edge - cost,
                    "edge_to_cost_ratio": edge_to_cost,
                    "liquidity_score": 1.2,
                    "activity_score": 1.1,
                    "reasons": ["microprice_aligned", "trade_flow_bias"],
                    "pattern_reasons": ["fixture_pattern"],
                },
            }
        ],
    }


def _llm_record(
    *,
    timestamp: str,
    symbol: str,
    context_id: str,
    decision_id: str,
) -> dict[str, object]:
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "timestamp": timestamp,
        "symbol": symbol,
        "context": {"context_id": context_id, "symbol": symbol},
        "proposal": {
            "schema_version": "trident_ai_proposal_v1",
            "decision_id": decision_id,
            "as_of": timestamp,
            "valid_until": "2026-06-07T13:05:00Z",
            "action": "open",
            "symbol": symbol,
            "side": "long",
            "confidence": 0.7,
            "time_horizon_minutes": 60,
            "max_notional_usd": 25.0,
            "max_leverage": 1.0,
            "entry_style": "market",
            "invalidation_price": 99.0,
            "stop_bps": 80.0,
            "take_profit_bps": 160.0,
            "time_stop_minutes": 60,
            "rationale_tags": ["fixture"],
            "evidence_ids": [context_id],
            "risk_notes": ["fixture"],
        },
        "validation": {"accepted": True, "reason": "accepted"},
    }


def _closed_trade(
    *,
    decision_id: str,
    symbol: str,
    opened_at: str,
    closed_at: str,
    exit_price: float,
    pnl: float,
    gross: float,
    close_reason: str,
) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
        "timestamp": closed_at,
        "symbol": symbol,
        "close_reason": close_reason,
        "trade": {
            "symbol": symbol,
            "side": "long",
            "decision_id": decision_id,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "entry_price": 100.0,
            "exit_price": exit_price,
            "notional_usd": 25.0,
            "gross_pnl_usd": gross,
            "fees_usd": 0.0175,
            "pnl_usd": pnl,
            "close_reason": close_reason,
            "confidence": 0.7,
        },
    }


def _market_record(timestamp: str, prices: dict[str, float]) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbols": [{"symbol": symbol, "price": price} for symbol, price in prices.items()],
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
