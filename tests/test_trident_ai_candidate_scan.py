from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.trident_ai import (
    AgentMarketContext,
    CANDIDATE_SCAN_EVENT,
    TridentAICandidateScanner,
    TridentAIFeatureBuilder,
    load_trident_ai_config,
    score_market_context,
)
from app.trident_ai.cli import main
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD, CANDIDATE_HINT_SCHEMA_VERSION


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "market_snapshots.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _snapshot_record() -> dict[str, object]:
    fixture = _fixture()
    return {
        "timestamp": fixture["as_of"],
        "regime_snapshot": {
            "ready": True,
            "adx": 32.0,
            "atr_ratio": 1.1,
            "range_width_bps": 180.0,
            "structure_score": 0.62,
            "btc_impulse": True,
            "regime": fixture["regime"],
        },
        "symbols": deepcopy(fixture["symbols"]),
    }


def _write_snapshot(path: Path) -> None:
    path.write_text(json.dumps(_snapshot_record()) + "\n", encoding="utf-8")


class TridentAICandidateScanTests(unittest.TestCase):
    def test_scores_market_context_with_direction_and_reasons(self) -> None:
        fixture = _fixture()
        builder = TridentAIFeatureBuilder()
        context = builder.accepted_contexts_from_mappings(
            fixture["symbols"],
            as_of=fixture["as_of"],
            regime=fixture["regime"],
        )[0]

        candidate = score_market_context(context)

        self.assertEqual(candidate.symbol, "BTC")
        self.assertIn(candidate.side, {"long", "short"})
        self.assertGreater(candidate.score, 0.0)
        self.assertGreater(candidate.raw_score, 0.0)
        self.assertGreater(candidate.round_trip_cost_bps, 0.0)
        self.assertGreaterEqual(candidate.cost_score, 0.25)
        self.assertGreaterEqual(candidate.edge_quality_score, 0.85)
        self.assertEqual(candidate.pattern_profile, "none")
        self.assertEqual(candidate.pattern_quality_score, 1.0)
        self.assertTrue(candidate.reasons)

    def test_score_penalizes_candidates_with_expensive_round_trip_costs(self) -> None:
        fixture = _fixture()
        builder = TridentAIFeatureBuilder()
        context = builder.accepted_contexts_from_mappings(
            fixture["symbols"],
            as_of=fixture["as_of"],
            regime=fixture["regime"],
        )[0]
        expensive_features = dict(context.features)
        expensive_features["spread_bps"] = 25.0
        expensive = AgentMarketContext(
            schema_version=context.schema_version,
            context_id=context.context_id,
            as_of=context.as_of,
            symbol=context.symbol,
            price=context.price,
            regime=context.regime,
            features=expensive_features,
            source=context.source,
        )

        baseline = score_market_context(context)
        expensive_candidate = score_market_context(expensive)

        self.assertLess(expensive_candidate.score, baseline.score)
        self.assertLessEqual(expensive_candidate.cost_score, baseline.cost_score)
        self.assertIn("round_trip_cost_high", expensive_candidate.reasons)

    def test_score_penalizes_adverse_microprice_direction(self) -> None:
        fixture = _fixture()
        builder = TridentAIFeatureBuilder()
        context = builder.accepted_contexts_from_mappings(
            fixture["symbols"],
            as_of=fixture["as_of"],
            regime=fixture["regime"],
        )[0]
        aligned_features = dict(context.features)
        aligned_features["microprice_dislocation_bps"] = 2.0
        adverse_features = dict(context.features)
        adverse_features["microprice_dislocation_bps"] = -2.0
        aligned = AgentMarketContext(
            schema_version=context.schema_version,
            context_id=context.context_id,
            as_of=context.as_of,
            symbol=context.symbol,
            price=context.price,
            regime=context.regime,
            features=aligned_features,
            source=context.source,
        )
        adverse = AgentMarketContext(
            schema_version=context.schema_version,
            context_id=context.context_id,
            as_of=context.as_of,
            symbol=context.symbol,
            price=context.price,
            regime=context.regime,
            features=adverse_features,
            source=context.source,
        )

        aligned_candidate = score_market_context(aligned)
        adverse_candidate = score_market_context(adverse)

        self.assertEqual(aligned_candidate.side, "long")
        self.assertEqual(adverse_candidate.side, "long")
        self.assertLess(adverse_candidate.score, aligned_candidate.score)
        self.assertIn("microprice_conflict", adverse_candidate.reasons)

    def test_research_pattern_profile_penalizes_weak_patterns(self) -> None:
        fixture = _fixture()
        builder = TridentAIFeatureBuilder()
        context = builder.accepted_contexts_from_mappings(
            fixture["symbols"],
            as_of=fixture["as_of"],
            regime=fixture["regime"],
        )[0]
        weak_features = dict(context.features)
        weak_features["ema_fast"] = context.price + 1.0
        weak_features["ema_slow"] = context.price
        weak_features["ema_alignment"] = "bullish"
        weak_features["structure_score"] = 0.8
        weak_features["trade_flow_bias"] = 0.5
        weak_features["book_imbalance"] = -0.5
        weak_features["microprice_dislocation_bps"] = 2.0
        weak_features["vwap_distance_bps"] = 0.0
        weak_features["realized_vol_short_bps"] = 30.0
        weak_context = AgentMarketContext(
            schema_version=context.schema_version,
            context_id=context.context_id,
            as_of=context.as_of,
            symbol=context.symbol,
            price=context.price,
            regime=context.regime,
            features=weak_features,
            source=context.source,
        )

        baseline = score_market_context(weak_context)
        profiled = score_market_context(weak_context, pattern_profile="research_v1")

        self.assertEqual(baseline.side, "long")
        self.assertEqual(profiled.side, "long")
        self.assertLess(profiled.score, baseline.score)
        self.assertLess(profiled.pattern_quality_score, 1.0)
        self.assertIn("penalty_flow_book_mixed_conflict", profiled.pattern_reasons)
        self.assertIn("penalty_vwap_neutral", profiled.pattern_reasons)

    def test_stable_pattern_profile_blocks_unvalidated_bonus(self) -> None:
        fixture = _fixture()
        builder = TridentAIFeatureBuilder()
        context = builder.accepted_contexts_from_mappings(
            fixture["symbols"],
            as_of=fixture["as_of"],
            regime=fixture["regime"],
        )[0]
        unstable_features = dict(context.features)
        unstable_features["ema_alignment"] = "bullish"
        unstable_features["structure_score"] = 0.8
        unstable_features["trade_flow_bias"] = 0.8
        unstable_features["book_imbalance"] = 0.0
        unstable_features["microprice_dislocation_bps"] = 2.0
        unstable_features["vwap_distance_bps"] = 10.0
        unstable_features["realized_vol_short_bps"] = 12.0
        unstable_context = AgentMarketContext(
            schema_version=context.schema_version,
            context_id=context.context_id,
            as_of=context.as_of,
            symbol=context.symbol,
            price=context.price,
            regime=context.regime,
            features=unstable_features,
            source=context.source,
        )

        baseline = score_market_context(unstable_context)
        exploratory = score_market_context(unstable_context, pattern_profile="research_v1")
        stable = score_market_context(unstable_context, pattern_profile="research_v2_stable")

        self.assertEqual(stable.side, "long")
        self.assertGreater(exploratory.pattern_quality_score, 1.0)
        self.assertLess(stable.pattern_quality_score, 1.0)
        self.assertLess(stable.score, baseline.score)
        self.assertIn("no_bonus_without_fold_stability", stable.pattern_reasons)
        self.assertIn("penalty_fold_unstable_pattern", stable.pattern_reasons)

    def test_candidate_scan_writes_selected_input_and_reports(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "candidates.jsonl"
            selected_input_path = directory / "selected.jsonl"
            report_json_path = directory / "candidates.json"
            report_md_path = directory / "candidates.md"
            _write_snapshot(input_path)

            result = TridentAICandidateScanner(config=config).run(
                input_path,
                journal_path=journal_path,
                selected_input_path=selected_input_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                top_n=2,
                min_score=0.0,
                min_edge_to_cost=0.0,
                min_net_edge_bps=0.0,
                symbols=["BTC", "ETH", "SOL", "HYPE"],
            )

            self.assertEqual(result.records_processed, 1)
            self.assertGreaterEqual(result.contexts_scored, 4)
            self.assertEqual(result.candidates_selected, 2)
            self.assertTrue(selected_input_path.exists())
            selected = [
                json.loads(line)
                for line in selected_input_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(selected), 2)
            self.assertTrue(all(len(record["symbols"]) == 1 for record in selected))
            candidate_hint = selected[0]["symbols"][0][CANDIDATE_HINT_FIELD]
            self.assertEqual(candidate_hint["schema_version"], CANDIDATE_HINT_SCHEMA_VERSION)
            self.assertEqual(candidate_hint["symbol"], selected[0]["symbols"][0]["symbol"])
            self.assertIn(candidate_hint["side"], {"long", "short"})
            self.assertGreater(candidate_hint["score"], 0.0)
            self.assertGreater(candidate_hint["round_trip_cost_bps"], 0.0)
            self.assertIn("estimated_net_edge_bps", candidate_hint)
            self.assertIn("edge_to_cost_ratio", candidate_hint)
            self.assertIn("edge_quality_score", candidate_hint)
            self.assertIn("pattern_quality_score", candidate_hint)
            self.assertEqual(candidate_hint["pattern_profile"], "none")
            self.assertIn("market_micro_regime", candidate_hint)
            self.assertIn("range_vol_regime", candidate_hint)
            self.assertIn("symbol_range_vol", candidate_hint)
            micro_regime = candidate_hint["market_micro_regime"]
            self.assertEqual(
                candidate_hint["range_vol_regime"],
                micro_regime["range_vol_regime"],
            )
            self.assertEqual(
                candidate_hint["symbol_range_vol"],
                f"{candidate_hint['symbol']}|{candidate_hint['range_vol_regime']}",
            )
            self.assertTrue(candidate_hint["reasons"])
            journal_rows = [
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(all(row["event_type"] == CANDIDATE_SCAN_EVENT for row in journal_rows))
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_candidate_scan")
            self.assertIn("TRIDENT-AI Candidate Scan", report_md_path.read_text(encoding="utf-8"))

    def test_candidate_scan_default_gate_requires_edge_to_cost(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            _write_snapshot(input_path)

            result = TridentAICandidateScanner(config=config).run(
                input_path,
                journal_path=directory / "candidates.jsonl",
                selected_input_path=directory / "selected.jsonl",
                report_json_path=directory / "candidates.json",
                report_md_path=directory / "candidates.md",
                top_n=4,
                min_score=0.0,
                symbols=["BTC", "ETH", "SOL", "HYPE"],
            )

            self.assertEqual(result.candidates_selected, 1)
            self.assertEqual(result.min_edge_to_cost, 1.5)
            self.assertEqual(result.min_net_edge_bps, 5.0)
            self.assertGreaterEqual(result.candidate_rejections, 3)
            self.assertTrue(
                {
                    "edge_to_cost_below_min",
                    "net_edge_below_min",
                }.intersection(result.rejection_reasons)
            )

    def test_candidate_scan_can_require_microprice_alignment(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            snapshot = _snapshot_record()
            for symbol_payload in snapshot["symbols"]:
                symbol_payload["microprice_dislocation_bps"] = 0.0
            input_path.write_text(json.dumps(snapshot) + "\n", encoding="utf-8")

            result = TridentAICandidateScanner(config=config).run(
                input_path,
                journal_path=directory / "candidates.jsonl",
                selected_input_path=directory / "selected.jsonl",
                report_json_path=directory / "candidates.json",
                report_md_path=directory / "candidates.md",
                top_n=4,
                min_score=0.0,
                min_edge_to_cost=0.0,
                min_net_edge_bps=0.0,
                require_microprice_alignment=True,
                symbols=["BTC", "ETH", "SOL", "HYPE"],
            )

            self.assertEqual(result.candidates_selected, 0)
            self.assertTrue(result.require_microprice_alignment)
            self.assertGreaterEqual(result.rejection_reasons["microprice_not_aligned"], 1)

    def test_candidate_scan_filters_by_timestamp_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            early = _snapshot_record()
            early["timestamp"] = "2026-06-07T12:00:00Z"
            late = deepcopy(early)
            late["timestamp"] = "2026-06-08T12:00:00Z"
            input_path.write_text(
                "\n".join(json.dumps(record, sort_keys=True) for record in [early, late]) + "\n",
                encoding="utf-8",
            )

            scanner = TridentAICandidateScanner(
                config=load_trident_ai_config("config/trident_ai.toml")
            )
            result = scanner.run(
                input_path,
                journal_path=directory / "candidates.jsonl",
                selected_input_path=directory / "selected.jsonl",
                report_json_path=directory / "candidates.json",
                report_md_path=directory / "candidates.md",
                start_timestamp="2026-06-08T00:00:00Z",
                end_timestamp="2026-06-09T00:00:00Z",
                top_n=4,
                min_score=0.0,
                min_edge_to_cost=0.0,
                min_net_edge_bps=0.0,
                symbols=["BTC", "ETH", "SOL", "HYPE"],
            )

            self.assertEqual(result.records_processed, 1)
            self.assertEqual(result.start_timestamp, "2026-06-08T00:00:00Z")
            self.assertEqual(result.end_timestamp, "2026-06-09T00:00:00Z")
            self.assertEqual(result.first_timestamp, "2026-06-08T12:00:00Z")
            self.assertEqual(result.last_timestamp, "2026-06-08T12:00:00Z")

    def test_candidate_scan_cli_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            selected_input_path = directory / "selected.jsonl"
            report_json_path = directory / "candidates.json"
            _write_snapshot(input_path)

            exit_code = main(
                [
                    "candidate-scan",
                    "--input",
                    str(input_path),
                    "--journal-path",
                    str(directory / "candidates.jsonl"),
                    "--selected-input-path",
                    str(selected_input_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(directory / "candidates.md"),
                    "--start-timestamp",
                    "2026-06-07T00:00:00Z",
                    "--end-timestamp",
                    "2026-06-08T00:00:00Z",
                    "--top-n",
                    "2",
                    "--min-score",
                    "0",
                    "--min-edge-to-cost",
                    "0",
                    "--min-net-edge-bps",
                    "0",
                    "--pattern-profile",
                    "research_v2_stable",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(selected_input_path.exists())
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["result"]["candidates_selected"], 2)
            self.assertEqual(report["result"]["pattern_profile"], "research_v2_stable")
            self.assertEqual(report["result"]["start_timestamp"], "2026-06-07T00:00:00Z")
            self.assertEqual(report["result"]["end_timestamp"], "2026-06-08T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
