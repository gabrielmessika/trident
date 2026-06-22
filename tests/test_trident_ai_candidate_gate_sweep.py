from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import load_trident_ai_config, run_trident_ai_candidate_gate_sweep
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD


class TridentAICandidateGateSweepTests(unittest.TestCase):
    def test_candidate_gate_sweep_penalizes_oos_no_trade_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            is_candidate = directory / "is_candidates.jsonl"
            is_market = directory / "is_market.jsonl"
            oos_candidate = directory / "oos_candidates.jsonl"
            oos_market = directory / "oos_market.jsonl"
            report_json_path = directory / "gate_sweep.json"
            report_md_path = directory / "gate_sweep.md"
            artifact_dir = directory / "artifacts"

            _write_jsonl(
                is_candidate,
                [
                    _candidate_record(
                        symbol="BTC",
                        timestamp="2026-06-07T12:00:00Z",
                        price=100.0,
                        edge_to_cost=4.5,
                        net_edge_bps=42.0,
                    )
                ],
            )
            _write_jsonl(is_market, [_market_record("2026-06-07T15:00:00Z", "BTC", 101.0)])
            _write_jsonl(
                oos_candidate,
                [
                    _candidate_record(
                        symbol="ETH",
                        timestamp="2026-06-08T12:00:00Z",
                        price=100.0,
                        edge_to_cost=3.0,
                        net_edge_bps=20.0,
                    )
                ],
            )
            _write_jsonl(oos_market, [_market_record("2026-06-08T15:00:00Z", "ETH", 101.0)])

            result = run_trident_ai_candidate_gate_sweep(
                candidate_input_paths=(is_candidate, oos_candidate),
                market_input_paths=(is_market, oos_market),
                fold_labels=("is_fixture", "oos_fixture"),
                config=load_trident_ai_config("config/trident_ai.toml"),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                artifact_dir=artifact_dir,
                symbols=("BTC", "ETH"),
                stop_bps=120.0,
                take_profit_bps=500.0,
                time_stop_minutes=180,
                min_edge_to_cost_values=(2.5, 4.0),
                min_net_edge_bps_values=(15.0,),
                min_liquidity_score_values=(1.0,),
                max_round_trip_cost_bps_values=(12.0,),
                min_total_closed_trades=1,
                min_symbols=1,
                max_negative_folds=0,
                oos_no_trade_penalty_bps=25.0,
            )

            self.assertEqual(result.profile_count, 2)
            self.assertEqual(result.profiles_evaluated, 2)
            self.assertEqual(result.classification_counts["robust_candidate"], 1)
            self.assertEqual(result.classification_counts["oos_no_trade"], 1)
            self.assertEqual(result.best_profile["classification"], "robust_candidate")
            self.assertEqual(result.best_robust_profile["profile_id"], result.best_profile["profile_id"])
            strict = next(row for row in result.profile_rows if row["min_edge_to_cost"] == 4.0)
            self.assertEqual(strict["oos_no_trade_folds"], 1)
            self.assertLess(strict["penalized_avg_net_bps"], strict["avg_net_bps"])

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_candidate_gate_sweep")
            self.assertIn(
                "TRIDENT-AI Candidate Gate Sweep",
                report_md_path.read_text(encoding="utf-8"),
            )

    def test_candidate_gate_sweep_applies_technical_veto_bucket_to_each_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_path = directory / "candidates.jsonl"
            market_path = directory / "market.jsonl"
            report_json_path = directory / "gate_sweep.json"
            report_md_path = directory / "gate_sweep.md"
            artifact_dir = directory / "artifacts"

            _write_jsonl(
                candidate_path,
                [
                    _candidate_record(
                        symbol="BTC",
                        timestamp="2026-06-07T12:00:00Z",
                        price=100.0,
                        edge_to_cost=4.5,
                        net_edge_bps=42.0,
                    )
                ],
            )
            _write_jsonl(market_path, [_market_record("2026-06-07T15:00:00Z", "BTC", 101.0)])

            result = run_trident_ai_candidate_gate_sweep(
                candidate_input_paths=(candidate_path,),
                market_input_paths=(market_path,),
                fold_labels=("fixture",),
                config=load_trident_ai_config("config/trident_ai.toml"),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                artifact_dir=artifact_dir,
                symbols=("BTC",),
                stop_bps=120.0,
                take_profit_bps=500.0,
                time_stop_minutes=180,
                min_edge_to_cost_values=(4.0,),
                min_net_edge_bps_values=(35.0,),
                min_liquidity_score_values=(1.0,),
                max_round_trip_cost_bps_values=(12.0,),
                min_pattern_quality_score_values=(0.85,),
                min_total_closed_trades=1,
                min_symbols=1,
                technical_veto_buckets=("family::volume_flow=long",),
            )

            self.assertEqual(result.technical_veto_buckets, ("family::volume_flow=long",))
            self.assertEqual(result.profile_count, 1)
            self.assertEqual(result.profile_rows[0]["min_pattern_quality_score"], 0.85)
            self.assertEqual(result.profile_rows[0]["decisions_written"], 0)
            self.assertEqual(result.profile_rows[0]["closed_trades"], 0)
            self.assertEqual(
                result.profile_rows[0]["candidate_skip_reasons"][
                    "technical_digest_veto_family_volume_flow_long"
                ],
                1,
            )

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["result"]["technical_veto_buckets"], ["family::volume_flow=long"])

    def test_candidate_gate_sweep_evaluates_micro_regime_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_path = directory / "candidates.jsonl"
            market_path = directory / "market.jsonl"
            report_json_path = directory / "gate_sweep.json"
            report_md_path = directory / "gate_sweep.md"
            artifact_dir = directory / "artifacts"

            _write_jsonl(
                candidate_path,
                [
                    _candidate_record(
                        symbol="BTC",
                        timestamp="2026-06-07T12:00:00Z",
                        price=100.0,
                        edge_to_cost=4.5,
                        net_edge_bps=42.0,
                        bucket_range_bps=58.0,
                        realized_vol_short_bps=24.0,
                    )
                ],
            )
            _write_jsonl(market_path, [_market_record("2026-06-07T15:00:00Z", "BTC", 101.0)])

            result = run_trident_ai_candidate_gate_sweep(
                candidate_input_paths=(candidate_path,),
                market_input_paths=(market_path,),
                fold_labels=("fixture",),
                config=load_trident_ai_config("config/trident_ai.toml"),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                artifact_dir=artifact_dir,
                symbols=("BTC",),
                stop_bps=120.0,
                take_profit_bps=500.0,
                time_stop_minutes=180,
                min_edge_to_cost_values=(4.0,),
                min_net_edge_bps_values=(35.0,),
                min_liquidity_score_values=(1.0,),
                max_round_trip_cost_bps_values=(12.0,),
                min_total_closed_trades=1,
                min_symbols=1,
                micro_regime_profile_values=("none", "veto_range_mid_vol_high"),
                cache_market_events=True,
            )

            self.assertEqual(result.profile_count, 2)
            self.assertEqual(result.micro_regime_profile_values, ("none", "veto_range_mid_vol_high"))
            self.assertTrue(result.cache_market_events)
            baseline = next(row for row in result.profile_rows if row["micro_regime_profile"] == "none")
            veto = next(
                row
                for row in result.profile_rows
                if row["micro_regime_profile"] == "veto_range_mid_vol_high"
            )
            self.assertEqual(baseline["closed_trades"], 1)
            self.assertEqual(veto["closed_trades"], 0)
            self.assertEqual(
                veto["candidate_skip_reasons"][
                    "micro_regime_veto_range_vol_regime_range_mid_vol_high"
                ],
                1,
            )

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                report["result"]["threshold_values"]["micro_regime_profile"],
                ["none", "veto_range_mid_vol_high"],
            )
            self.assertTrue(report["result"]["cache_market_events"])

    def test_candidate_gate_sweep_rejects_symbol_concentrated_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_path = directory / "candidates.jsonl"
            market_path = directory / "market.jsonl"
            report_json_path = directory / "gate_sweep.json"
            report_md_path = directory / "gate_sweep.md"
            artifact_dir = directory / "artifacts"

            _write_jsonl(
                candidate_path,
                [
                    _candidate_record(
                        symbol="BTC",
                        timestamp="2026-06-07T12:00:00Z",
                        price=100.0,
                        edge_to_cost=4.5,
                        net_edge_bps=42.0,
                    ),
                    _candidate_record(
                        symbol="BTC",
                        timestamp="2026-06-07T16:00:00Z",
                        price=101.0,
                        edge_to_cost=4.5,
                        net_edge_bps=42.0,
                    ),
                ],
            )
            _write_jsonl(
                market_path,
                [
                    _market_record("2026-06-07T15:00:00Z", "BTC", 101.0),
                    _market_record("2026-06-07T19:00:00Z", "BTC", 102.0),
                ],
            )

            result = run_trident_ai_candidate_gate_sweep(
                candidate_input_paths=(candidate_path,),
                market_input_paths=(market_path,),
                fold_labels=("fixture",),
                config=load_trident_ai_config("config/trident_ai.toml"),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                artifact_dir=artifact_dir,
                symbols=("BTC",),
                stop_bps=120.0,
                take_profit_bps=500.0,
                time_stop_minutes=180,
                min_edge_to_cost_values=(4.0,),
                min_net_edge_bps_values=(35.0,),
                min_liquidity_score_values=(1.0,),
                max_round_trip_cost_bps_values=(12.0,),
                min_total_closed_trades=1,
                min_symbols=1,
                max_dominant_symbol_ratio=0.75,
            )

            row = result.profile_rows[0]
            self.assertEqual(row["closed_trades"], 2)
            self.assertEqual(row["classification"], "symbol_concentrated")
            self.assertEqual(row["dominant_symbol_ratio"], 1.0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["result"]["max_dominant_symbol_ratio"], 0.75)

    def test_candidate_gate_sweep_can_classify_guardrail_loss_avoidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            is_candidate = directory / "is_candidates.jsonl"
            is_market = directory / "is_market.jsonl"
            guard_candidate = directory / "guard_candidates.jsonl"
            guard_market = directory / "guard_market.jsonl"
            report_json_path = directory / "gate_sweep.json"
            report_md_path = directory / "gate_sweep.md"
            artifact_dir = directory / "artifacts"

            _write_jsonl(
                is_candidate,
                [
                    _candidate_record(
                        symbol="BTC",
                        timestamp="2026-06-07T12:00:00Z",
                        price=100.0,
                        edge_to_cost=4.5,
                        net_edge_bps=42.0,
                        flow_bias=0.5,
                    )
                ],
            )
            _write_jsonl(is_market, [_market_record("2026-06-07T15:00:00Z", "BTC", 101.0)])
            _write_jsonl(
                guard_candidate,
                [
                    _candidate_record(
                        symbol="ETH",
                        timestamp="2026-06-08T12:00:00Z",
                        price=100.0,
                        edge_to_cost=4.5,
                        net_edge_bps=42.0,
                        flow_bias=-0.5,
                    )
                ],
            )
            _write_jsonl(guard_market, [_market_record("2026-06-08T15:00:00Z", "ETH", 99.0)])

            result = run_trident_ai_candidate_gate_sweep(
                candidate_input_paths=(is_candidate, guard_candidate),
                market_input_paths=(is_market, guard_market),
                fold_labels=("is_fixture", "oos_guardrail"),
                oos_fold_labels=("oos_guardrail",),
                config=load_trident_ai_config("config/trident_ai.toml"),
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                artifact_dir=artifact_dir,
                symbols=("BTC", "ETH"),
                stop_bps=120.0,
                take_profit_bps=500.0,
                time_stop_minutes=180,
                min_edge_to_cost_values=(4.0,),
                min_net_edge_bps_values=(35.0,),
                min_liquidity_score_values=(1.0,),
                max_round_trip_cost_bps_values=(12.0,),
                min_total_closed_trades=1,
                min_symbols=1,
                max_negative_folds=0,
                technical_veto_buckets=("family::volume_flow=short",),
                allow_guardrail_loss_avoidance=True,
                guardrail_fold_labels=("oos_guardrail",),
            )

            self.assertEqual(result.profile_rows[0]["classification"], "guardrail_loss_avoidance_candidate")
            self.assertEqual(result.profile_rows[0]["oos_no_trade_folds"], 1)
            self.assertEqual(result.profile_rows[0]["guardrail_loss_avoidance_folds"], 1)
            self.assertEqual(result.profile_rows[0]["effective_oos_no_trade_folds"], 0)
            self.assertEqual(
                result.best_guardrail_aware_profile["profile_id"],
                result.profile_rows[0]["profile_id"],
            )
            guardrail_fold = result.profile_rows[0]["folds"][1]
            self.assertTrue(guardrail_fold["guardrail_loss_avoidance"])
            self.assertLess(guardrail_fold["guardrail_baseline"]["realized_pnl_usd"], 0.0)

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertTrue(report["result"]["allow_guardrail_loss_avoidance"])
            self.assertEqual(report["result"]["guardrail_fold_labels"], ["oos_guardrail"])


def _candidate_record(
    *,
    symbol: str,
    timestamp: str,
    price: float,
    edge_to_cost: float,
    net_edge_bps: float,
    flow_bias: float = 0.5,
    pattern_quality_score: float = 1.0,
    bucket_range_bps: float = 58.0,
    realized_vol_short_bps: float = 8.0,
    microprice_dislocation_bps: float = 1.0,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
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
                "symbol": symbol,
                "price": price,
                "ema_fast": price + 1.0,
                "ema_slow": price,
                "vwap_distance_bps": 10.0,
                "structure_score": 0.5,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
                "microprice_dislocation_bps": microprice_dislocation_bps,
                "book_imbalance": flow_bias,
                "trade_flow_bias": flow_bias,
                "bucket_notional_usd": 10_000.0,
                "bucket_trade_count": 20,
                "bucket_range_bps": bucket_range_bps,
                "volume_ratio": 4.0,
                "trade_count_ratio": 2.0,
                "realized_vol_short_bps": realized_vol_short_bps,
                CANDIDATE_HINT_FIELD: {
                    "schema_version": "trident_ai_candidate_hint_v6",
                    "context_id": f"market_{symbol}_{timestamp.replace(':', '').replace('-', '')}",
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
                    "estimated_edge_bps": net_edge_bps + 8.0,
                    "round_trip_cost_bps": 8.0,
                    "estimated_net_edge_bps": net_edge_bps,
                    "edge_to_cost_ratio": edge_to_cost,
                    "pattern_quality_score": pattern_quality_score,
                    "reasons": ["ema_bullish", "microprice_aligned"],
                },
            }
        ],
    }


def _market_record(timestamp: str, symbol: str, price: float) -> dict[str, object]:
    payload = _candidate_record(
        symbol=symbol,
        timestamp=timestamp,
        price=price,
        edge_to_cost=3.0,
        net_edge_bps=20.0,
    )
    payload["symbols"][0].pop(CANDIDATE_HINT_FIELD, None)
    return payload


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
