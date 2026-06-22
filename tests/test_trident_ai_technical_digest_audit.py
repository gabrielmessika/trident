from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    run_trident_ai_technical_digest_audit,
    run_trident_ai_technical_digest_fold_validation,
    run_trident_ai_technical_digest_veto_audit,
)
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD


class TridentAITechnicalDigestAuditTests(unittest.TestCase):
    def test_audit_separates_digest_vetoes_from_clean_technical_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "technical_digest.json"
            report_md_path = directory / "technical_digest.md"
            _write_jsonl(
                candidate_input_path,
                [
                    _candidate_record(
                        symbol="BTC",
                        side="long",
                        price=100.0,
                        ema_fast=101.0,
                        ema_slow=100.0,
                        vwap_distance_bps=8.0,
                        structure_score=0.62,
                        trade_flow_bias=0.3,
                        book_imbalance=0.2,
                        volume_ratio=1.4,
                        realized_vol_short_bps=12.0,
                    ),
                    _candidate_record(
                        symbol="ETH",
                        side="long",
                        price=200.0,
                        ema_fast=201.0,
                        ema_slow=200.0,
                        vwap_distance_bps=55.0,
                        structure_score=0.6,
                        trade_flow_bias=0.45,
                        book_imbalance=-0.5,
                        volume_ratio=0.45,
                        realized_vol_short_bps=82.0,
                        external_momentum_60s_bps=-6.0,
                        external_momentum_300s_bps=-4.0,
                    ),
                ],
            )
            _write_jsonl(
                market_input_path,
                [
                    {
                        "timestamp": "2026-06-07T12:15:00Z",
                        "symbols": [
                            {"symbol": "BTC", "price": 101.0},
                            {"symbol": "ETH", "price": 198.0},
                        ],
                    }
                ],
            )

            result = run_trident_ai_technical_digest_audit(
                candidate_input_path=candidate_input_path,
                market_input_path=market_input_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                horizons_minutes=(15,),
                min_bucket_samples=1,
                min_delta_bps=5.0,
            )

            self.assertEqual(result.candidates_seen, 2)
            self.assertEqual(result.candidates_with_digest, 2)
            self.assertEqual(result.missing_digest, 0)
            self.assertEqual(result.candidates_with_any_outcome, 2)
            self.assertEqual(result.best_horizon_minutes, 15)
            self.assertEqual(result.recommendation, "v10_candidate_digest_promising_with_vetoes")
            self.assertIn(
                ("has_veto", "false"),
                {(row["family"], row["bucket"]) for row in result.positive_buckets},
            )
            self.assertIn(
                ("has_veto", "true"),
                {(row["family"], row["bucket"]) for row in result.negative_buckets},
            )
            self.assertTrue(
                any(row["family"] == "conflict" for row in result.veto_or_conflict_buckets)
            )

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_technical_digest_audit")
            self.assertEqual(report["result"]["recommendation"], result.recommendation)
            self.assertIn(
                "TRIDENT-AI Technical Digest Audit",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_fold_validation_requires_stable_multifold_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            fold_a_candidates = directory / "fold_a_candidates.jsonl"
            fold_a_market = directory / "fold_a_market.jsonl"
            fold_b_candidates = directory / "fold_b_candidates.jsonl"
            fold_b_market = directory / "fold_b_market.jsonl"
            report_json_path = directory / "technical_digest_folds.json"
            report_md_path = directory / "technical_digest_folds.md"
            candidate_records = [
                _candidate_record(
                    symbol="BTC",
                    side="long",
                    price=100.0,
                    ema_fast=101.0,
                    ema_slow=100.0,
                    vwap_distance_bps=8.0,
                    structure_score=0.62,
                    trade_flow_bias=0.3,
                    book_imbalance=0.2,
                    volume_ratio=1.4,
                    realized_vol_short_bps=12.0,
                ),
                _candidate_record(
                    symbol="ETH",
                    side="long",
                    price=200.0,
                    ema_fast=201.0,
                    ema_slow=200.0,
                    vwap_distance_bps=55.0,
                    structure_score=0.6,
                    trade_flow_bias=0.45,
                    book_imbalance=-0.5,
                    volume_ratio=0.45,
                    realized_vol_short_bps=82.0,
                    external_momentum_60s_bps=-6.0,
                    external_momentum_300s_bps=-4.0,
                ),
            ]
            market_records = [
                {
                    "timestamp": "2026-06-07T12:15:00Z",
                    "symbols": [
                        {"symbol": "BTC", "price": 101.0},
                        {"symbol": "ETH", "price": 198.0},
                    ],
                }
            ]
            _write_jsonl(fold_a_candidates, candidate_records)
            _write_jsonl(fold_a_market, market_records)
            _write_jsonl(fold_b_candidates, candidate_records)
            _write_jsonl(fold_b_market, market_records)

            result = run_trident_ai_technical_digest_fold_validation(
                candidate_input_paths=(fold_a_candidates, fold_b_candidates),
                market_input_paths=(fold_a_market, fold_b_market),
                fold_labels=("fold_a", "fold_b"),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                horizons_minutes=(15,),
                min_bucket_samples=1,
                min_delta_bps=5.0,
                min_positive_folds=2,
                max_negative_folds=0,
            )

            self.assertEqual(result.recommendation, "v10_candidate_multifold_promising_with_vetoes")
            self.assertEqual(result.summary["folds"], 2)
            stable_positive = {
                (row["family"], row["bucket"]) for row in result.stable_positive_buckets
            }
            stable_negative = {
                (row["family"], row["bucket"]) for row in result.stable_negative_buckets
            }
            self.assertIn(("has_veto", "false"), stable_positive)
            self.assertIn(("has_veto", "true"), stable_negative)
            self.assertIn(("has_conflict", "true"), stable_negative)

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_technical_digest_fold_validation")
            self.assertEqual(report["result"]["recommendation"], result.recommendation)
            self.assertIn(
                "TRIDENT-AI Technical Digest Fold Validation",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_veto_audit_compares_candidate_outcomes_after_bucket_veto(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "technical_digest_veto.json"
            report_md_path = directory / "technical_digest_veto.md"
            _write_jsonl(
                candidate_input_path,
                [
                    _candidate_record(
                        symbol="BTC",
                        side="long",
                        price=100.0,
                        ema_fast=101.0,
                        ema_slow=100.0,
                        vwap_distance_bps=8.0,
                        structure_score=0.62,
                        trade_flow_bias=0.3,
                        book_imbalance=0.2,
                        volume_ratio=1.4,
                        realized_vol_short_bps=12.0,
                    ),
                    _candidate_record(
                        symbol="ETH",
                        side="long",
                        price=200.0,
                        ema_fast=201.0,
                        ema_slow=200.0,
                        vwap_distance_bps=55.0,
                        structure_score=0.6,
                        trade_flow_bias=0.45,
                        book_imbalance=-0.5,
                        volume_ratio=0.45,
                        realized_vol_short_bps=82.0,
                        external_momentum_60s_bps=-6.0,
                        external_momentum_300s_bps=-4.0,
                    ),
                ],
            )
            _write_jsonl(
                market_input_path,
                [
                    {
                        "timestamp": "2026-06-07T12:15:00Z",
                        "symbols": [
                            {"symbol": "BTC", "price": 101.0},
                            {"symbol": "ETH", "price": 198.0},
                        ],
                    }
                ],
            )

            result = run_trident_ai_technical_digest_veto_audit(
                candidate_input_paths=(candidate_input_path,),
                market_input_paths=(market_input_path,),
                fold_labels=("fixture",),
                veto_buckets=("has_veto::true",),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                horizons_minutes=(15,),
                min_delta_bps=5.0,
            )

            self.assertEqual(result.recommendation, "promote_candidate_veto_research")
            self.assertEqual(result.fold_rows[0]["candidates_vetoed"], 1)
            self.assertEqual(result.fold_rows[0]["classification"], "improved")
            self.assertGreater(result.delta_summary["total_net_bps"], 0.0)
            self.assertEqual(result.vetoed_summary["samples"], 1)

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_technical_digest_veto_audit")
            self.assertEqual(report["result"]["recommendation"], result.recommendation)
            self.assertIn(
                "TRIDENT-AI Technical Digest Veto Audit",
                report_md_path.read_text(encoding="utf-8"),
            )


def _candidate_record(
    *,
    symbol: str,
    side: str,
    price: float,
    ema_fast: float,
    ema_slow: float,
    vwap_distance_bps: float,
    structure_score: float,
    trade_flow_bias: float,
    book_imbalance: float,
    volume_ratio: float,
    realized_vol_short_bps: float,
    external_momentum_60s_bps: float = 0.0,
    external_momentum_300s_bps: float = 0.0,
) -> dict[str, object]:
    estimated_edge = 24.0
    round_trip_cost = 8.0
    timestamp = "2026-06-07T12:00:00Z"
    return {
        "timestamp": timestamp,
        "regime_snapshot": {"regime": "TrendExpansion"},
        "symbols": [
            {
                "symbol": symbol,
                "price": price,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "vwap_distance_bps": vwap_distance_bps,
                "structure_score": structure_score,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
                "trade_flow_bias": trade_flow_bias,
                "book_imbalance": book_imbalance,
                "volume_ratio": volume_ratio,
                "bucket_volume": 100.0,
                "bucket_range_bps": 20.0,
                "realized_vol_short_bps": realized_vol_short_bps,
                "external_momentum_60s_bps": external_momentum_60s_bps,
                "external_momentum_300s_bps": external_momentum_300s_bps,
                CANDIDATE_HINT_FIELD: {
                    "schema_version": "trident_ai_candidate_hint_v6",
                    "context_id": f"market_{symbol}_20260607T120000Z",
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "side": side,
                    "score": 2.0,
                    "estimated_edge_bps": estimated_edge,
                    "round_trip_cost_bps": round_trip_cost,
                    "estimated_net_edge_bps": estimated_edge - round_trip_cost,
                    "edge_to_cost_ratio": estimated_edge / round_trip_cost,
                    "reasons": ["fixture"],
                },
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
