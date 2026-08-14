from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import run_trident_ai_candidate_outcome_audit
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD


class TridentAICandidateOutcomeAuditTests(unittest.TestCase):
    def test_outcome_audit_measures_long_and_short_fixed_horizons(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "outcome.json"
            report_md_path = directory / "outcome.md"

            _write_jsonl(
                candidate_input_path,
                [
                    _candidate_record(
                        symbol="BTC",
                        side="long",
                        timestamp="2026-06-07T12:00:00Z",
                        price=100.0,
                        estimated_edge_bps=20.0,
                        round_trip_cost_bps=8.0,
                    ),
                    _candidate_record(
                        symbol="ETH",
                        side="short",
                        timestamp="2026-06-07T12:00:00Z",
                        price=200.0,
                        estimated_edge_bps=16.0,
                        round_trip_cost_bps=6.0,
                    ),
                ],
            )
            _write_gzip_jsonl(
                Path(f"{market_input_path}.gz"),
                [
                    _market_record("2026-06-07T12:15:00Z", {"BTC": 101.0, "ETH": 198.0}),
                    _market_record("2026-06-07T12:30:00Z", {"BTC": 99.0, "ETH": 202.0}),
                ],
            )

            result = run_trident_ai_candidate_outcome_audit(
                candidate_input_path=candidate_input_path,
                market_input_path=market_input_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                horizons_minutes=(15, 30),
            )

            self.assertEqual(result.candidates_seen, 2)
            self.assertEqual(result.candidates_with_any_outcome, 2)
            self.assertEqual(result.missing_outcomes, 0)
            self.assertEqual(result.horizon_stats["15"]["samples"], 2)
            self.assertEqual(result.horizon_stats["15"]["wins"], 2)
            self.assertAlmostEqual(result.horizon_stats["15"]["avg_net_bps"], 93.0)
            self.assertEqual(result.horizon_stats["30"]["wins"], 0)
            self.assertEqual(result.bucket_stats["15"]["symbol"]["BTC"]["samples"], 1)
            self.assertEqual(result.bucket_stats["15"]["side"]["short"]["wins"], 1)
            self.assertEqual(result.bucket_stats["15"]["microprice"]["neutral"]["samples"], 2)
            self.assertAlmostEqual(result.items[0]["outcomes"][0]["realized_gross_bps"], 100.0)
            self.assertAlmostEqual(result.items[0]["outcomes"][0]["realized_net_bps"], 92.0)
            self.assertAlmostEqual(result.items[1]["outcomes"][0]["realized_gross_bps"], 100.0)
            self.assertAlmostEqual(result.items[1]["outcomes"][0]["realized_net_bps"], 94.0)

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_candidate_outcome_audit")
            self.assertIn("bucket_stats", report["result"])
            self.assertIn(
                "TRIDENT-AI Candidate Outcome Audit",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_outcome_audit_reports_missing_future_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            _write_jsonl(
                candidate_input_path,
                [
                    _candidate_record(
                        symbol="BTC",
                        side="long",
                        timestamp="2026-06-07T12:00:00Z",
                        price=100.0,
                    ),
                ],
            )
            _write_jsonl(market_input_path, [])

            result = run_trident_ai_candidate_outcome_audit(
                candidate_input_path=candidate_input_path,
                market_input_path=market_input_path,
                report_json_path=directory / "outcome.json",
                report_md_path=directory / "outcome.md",
                horizons_minutes=(15,),
            )

            self.assertEqual(result.candidates_with_any_outcome, 0)
            self.assertEqual(result.missing_outcomes, 1)
            self.assertFalse(result.items[0]["outcomes"][0]["available"])


def _candidate_record(
    *,
    symbol: str,
    side: str,
    timestamp: str,
    price: float,
    estimated_edge_bps: float = 20.0,
    round_trip_cost_bps: float = 8.0,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbols": [
            {
                "symbol": symbol,
                "price": price,
                "spread_bps": 1.0,
                "microprice_dislocation_bps": 0.0,
                CANDIDATE_HINT_FIELD: {
                    "schema_version": "trident_ai_candidate_hint_v4",
                    "context_id": f"market_{symbol}_20260607T120000Z",
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "side": side,
                    "score": 2.0,
                    "estimated_edge_bps": estimated_edge_bps,
                    "round_trip_cost_bps": round_trip_cost_bps,
                    "estimated_net_edge_bps": estimated_edge_bps - round_trip_cost_bps,
                    "edge_to_cost_ratio": estimated_edge_bps / round_trip_cost_bps,
                    "reasons": ["fixture"],
                },
            }
        ],
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
    if not records:
        path.write_text("", encoding="utf-8")
        return
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def _write_gzip_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(
            "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n"
        )


if __name__ == "__main__":
    unittest.main()
