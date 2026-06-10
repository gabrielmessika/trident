from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.trident_ai import LLM_REPLAY_DECISION_EVENT
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.paper import PAPER_REPLAY_DECISION_EVENT, PAPER_REPLAY_TRADE_CLOSED_EVENT
from app.trident_ai.cli import load_trident_ai_env_file, main


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "market_snapshots.json"
INTEL_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "intel_digest.json"


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


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


class TridentAICLITests(unittest.TestCase):
    def test_loads_trident_ai_env_file_without_overriding_existing_env(self) -> None:
        old_openai_key = os.environ.get("OPENAI_API_KEY")
        old_xai_key = os.environ.get("XAI_API_KEY")
        old_forbidden = os.environ.get("HYPERLIQUID_SECRET_KEY")
        self.addCleanup(self._restore_env, "OPENAI_API_KEY", old_openai_key)
        self.addCleanup(self._restore_env, "XAI_API_KEY", old_xai_key)
        self.addCleanup(self._restore_env, "HYPERLIQUID_SECRET_KEY", old_forbidden)

        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["XAI_API_KEY"] = "already-set"
        os.environ.pop("HYPERLIQUID_SECRET_KEY", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env.tridentai"
            env_path.write_text(
                "\n".join(
                    [
                        "# local test env",
                        "OPENAI_API_KEY='sk-test-local'",
                        "XAI_API_KEY=xai-from-file",
                        "HYPERLIQUID_SECRET_KEY=must-not-load",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            loaded = load_trident_ai_env_file(env_path)

        self.assertEqual(loaded, {"OPENAI_API_KEY": "sk-test-local"})
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-test-local")
        self.assertEqual(os.environ["XAI_API_KEY"], "already-set")
        self.assertNotIn("HYPERLIQUID_SECRET_KEY", os.environ)

    def test_shadow_cli_runs_bounded_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "shadow.jsonl"
            status_path = directory / "status.json"
            _write_snapshot(input_path)

            exit_code = main(
                [
                    "shadow",
                    "--input",
                    str(input_path),
                    "--journal-path",
                    str(journal_path),
                    "--status-path",
                    str(status_path),
                    "--max-contexts",
                    "2",
                    "--symbols",
                    "BTC,ETH",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(journal_path.exists())
            self.assertTrue(status_path.exists())
            records = journal_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(records), 2)

    def test_llm_replay_cli_runs_cache_only_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "llm.jsonl"
            report_json_path = directory / "llm.json"
            report_md_path = directory / "llm.md"
            _write_snapshot(input_path)

            exit_code = main(
                [
                    "llm-replay",
                    "--input",
                    str(input_path),
                    "--journal-path",
                    str(journal_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--max-contexts",
                    "2",
                    "--symbols",
                    "BTC,ETH",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["result"]["llm_requests"], 2)
            self.assertEqual(report["result"]["live_llm_calls"], 0)
            self.assertEqual(
                report["result"]["rejection_reasons"]["cache_miss_live_calls_disabled"],
                2,
            )

    def test_paper_replay_cli_runs_on_llm_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "llm.jsonl"
            journal_path = directory / "paper.jsonl"
            report_json_path = directory / "paper.json"
            report_md_path = directory / "paper.md"
            input_path.write_text(
                json.dumps(
                    {
                        "event_type": LLM_REPLAY_DECISION_EVENT,
                        "source": "trident_ai_llm_replay",
                        "record_index": 0,
                        "timestamp": "2026-06-07T12:00:00Z",
                        "symbol": "BTC",
                        "request": {"request_id": "request_btc_hold"},
                        "context": {
                            "schema_version": "trident_ai_market_context_v1",
                            "context_id": "market_BTC_20260607T120000Z",
                            "as_of": "2026-06-07T12:00:00Z",
                            "symbol": "BTC",
                            "price": 100.0,
                            "regime": "fixture",
                            "features": {"spread_bps": 1.0},
                            "source": "fixture",
                        },
                        "llm_response": {
                            "ok": True,
                            "usage": {"estimated_cost_usd": 0.001},
                        },
                        "proposal": {
                            "schema_version": "trident_ai_proposal_v1",
                            "decision_id": "btc_hold",
                            "as_of": "2026-06-07T12:00:00Z",
                            "valid_until": "2026-06-07T12:05:00Z",
                            "action": "hold",
                            "symbol": "BTC",
                            "side": "long",
                            "confidence": 0.6,
                            "time_horizon_minutes": 15,
                            "max_notional_usd": 0.0,
                            "max_leverage": 0.0,
                            "entry_style": "none",
                            "invalidation_price": 0.0,
                            "stop_bps": 0.0,
                            "take_profit_bps": 0.0,
                            "time_stop_minutes": 0,
                            "rationale_tags": ["fixture"],
                            "evidence_ids": ["market_BTC_20260607T120000Z"],
                            "risk_notes": ["fixture"],
                        },
                        "validation": {"accepted": True, "reason": "accepted"},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "paper-replay",
                    "--input",
                    str(input_path),
                    "--journal-path",
                    str(journal_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_paper_replay")
            self.assertEqual(report["result"]["decisions_seen"], 1)
            self.assertEqual(report["result"]["action_counts"]["hold"], 1)
            self.assertTrue(journal_path.exists())
            self.assertTrue(report_md_path.exists())

    def test_intel_digest_cli_runs_on_fixture_without_live_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            journal_path = directory / "intel.jsonl"
            report_json_path = directory / "intel.json"
            report_md_path = directory / "intel.md"

            exit_code = main(
                [
                    "intel-digest",
                    "--fixture-input",
                    str(INTEL_FIXTURE_PATH),
                    "--journal-path",
                    str(journal_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--symbols",
                    "BTC,HYPE",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_intel_digest")
            self.assertEqual(report["result"]["provider"], "fixture")
            self.assertEqual(report["result"]["veto_symbols"], ["HYPE"])
            self.assertTrue(journal_path.exists())
            self.assertTrue(report_md_path.exists())

    def test_calibration_report_cli_runs_on_local_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            llm_journal_path = directory / "llm.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "calibration.json"
            report_md_path = directory / "calibration.md"
            context_id = "market_BTC_20260607T120000Z"
            request_id = "request_btc"
            decision_id = "decision_btc"

            _write_jsonl(
                candidate_input_path,
                [_calibration_candidate_record(context_id=context_id)],
            )
            _write_jsonl(
                llm_journal_path,
                [
                    _calibration_llm_record(
                        context_id=context_id,
                        request_id=request_id,
                        decision_id=decision_id,
                    )
                ],
            )
            _write_jsonl(
                paper_journal_path,
                [
                    {
                        "event_type": PAPER_REPLAY_DECISION_EVENT,
                        "timestamp": "2026-06-07T12:00:00Z",
                        "symbol": "BTC",
                        "request_id": request_id,
                        "decision_id": decision_id,
                        "proposal_action": "open",
                        "paper_action": "open",
                        "reason": "agent_open",
                        "price": 100.0,
                    }
                ],
            )

            exit_code = main(
                [
                    "calibration-report",
                    "--candidate-input",
                    str(candidate_input_path),
                    "--llm-journal",
                    str(llm_journal_path),
                    "--paper-journal",
                    str(paper_journal_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_calibration_report")
            self.assertEqual(report["result"]["matched_candidates"], 1)
            self.assertTrue(report_md_path.exists())

    def test_edge_calibration_cli_runs_on_local_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            llm_journal_path = directory / "llm.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "edge_calibration.json"
            report_md_path = directory / "edge_calibration.md"
            context_id = "market_BTC_20260607T120000Z"
            request_id = "request_btc"
            decision_id = "decision_btc"

            _write_jsonl(candidate_input_path, [_calibration_candidate_record(context_id=context_id)])
            _write_jsonl(
                llm_journal_path,
                [
                    _calibration_llm_record(
                        context_id=context_id,
                        request_id=request_id,
                        decision_id=decision_id,
                    )
                ],
            )
            _write_jsonl(
                paper_journal_path,
                [
                    {
                        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
                        "timestamp": "2026-06-07T12:10:00Z",
                        "symbol": "BTC",
                        "close_reason": "time_stop",
                        "trade": {
                            "symbol": "BTC",
                            "side": "long",
                            "decision_id": decision_id,
                            "opened_at": "2026-06-07T12:00:00Z",
                            "closed_at": "2026-06-07T12:10:00Z",
                            "entry_price": 100.0,
                            "exit_price": 99.9,
                            "notional_usd": 25.0,
                            "gross_pnl_usd": -0.025,
                            "fees_usd": 0.0175,
                            "pnl_usd": -0.0425,
                            "close_reason": "time_stop",
                            "confidence": 0.67,
                        },
                    }
                ],
            )

            exit_code = main(
                [
                    "edge-calibration",
                    "--candidate-input",
                    str(candidate_input_path),
                    "--llm-journal",
                    str(llm_journal_path),
                    "--paper-journal",
                    str(paper_journal_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_edge_calibration_report")
            self.assertEqual(report["result"]["closed_trades"], 1)
            self.assertTrue(report_md_path.exists())

    def test_llm_decision_audit_cli_runs_on_local_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            llm_journal_path = directory / "llm.jsonl"
            report_json_path = directory / "audit.json"
            report_md_path = directory / "audit.md"
            context_id = "market_BTC_20260607T120000Z"
            candidate = _calibration_candidate_record(context_id=context_id)
            hint = candidate["symbols"][0][CANDIDATE_HINT_FIELD]
            hint["estimated_edge_bps"] = 24.0
            hint["round_trip_cost_bps"] = 8.0
            hint["estimated_net_edge_bps"] = 16.0
            hint["edge_to_cost_ratio"] = 3.0
            llm_record = _calibration_llm_record(
                context_id=context_id,
                request_id="request_btc",
                decision_id="decision_btc",
            )
            proposal = llm_record["proposal"]
            proposal["action"] = "hold"
            proposal["max_notional_usd"] = 0.0
            proposal["max_leverage"] = 0.0
            proposal["entry_style"] = "none"
            proposal["invalidation_price"] = 0.0
            proposal["stop_bps"] = 0.0
            proposal["take_profit_bps"] = 0.0
            proposal["time_stop_minutes"] = 0
            proposal["evidence_ids"] = [context_id, "edge_to_cost_below_threshold"]

            _write_jsonl(candidate_input_path, [candidate])
            _write_jsonl(llm_journal_path, [llm_record])

            exit_code = main(
                [
                    "llm-decision-audit",
                    "--candidate-input",
                    str(candidate_input_path),
                    "--llm-journal",
                    str(llm_journal_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_llm_decision_audit")
            self.assertEqual(report["result"]["contradictory_decisions"], 1)
            self.assertTrue(report_md_path.exists())

    def test_candidate_outcome_audit_cli_runs_on_local_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "outcome.json"
            report_md_path = directory / "outcome.md"
            candidate = _calibration_candidate_record(context_id="market_BTC_20260607T120000Z")
            candidate["symbols"][0]["price"] = 100.0
            _write_jsonl(candidate_input_path, [candidate])
            _write_jsonl(
                market_input_path,
                [
                    {
                        "timestamp": "2026-06-07T12:15:00Z",
                        "symbols": [{"symbol": "BTC", "price": 101.0}],
                    }
                ],
            )

            exit_code = main(
                [
                    "candidate-outcome-audit",
                    "--candidate-input",
                    str(candidate_input_path),
                    "--market-input",
                    str(market_input_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--horizons-minutes",
                    "15",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_candidate_outcome_audit")
            self.assertEqual(report["result"]["candidates_seen"], 1)
            self.assertTrue(report_md_path.exists())

    def test_exit_follow_through_audit_cli_runs_on_local_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            paper_journal_path = directory / "paper.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "exit_audit.json"
            report_md_path = directory / "exit_audit.md"
            _write_jsonl(
                paper_journal_path,
                [
                    {
                        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
                        "timestamp": "2026-06-07T13:00:00Z",
                        "symbol": "BTC",
                        "close_reason": "time_stop",
                        "trade": {
                            "symbol": "BTC",
                            "side": "long",
                            "decision_id": "decision_btc",
                            "opened_at": "2026-06-07T12:00:00Z",
                            "closed_at": "2026-06-07T13:00:00Z",
                            "entry_price": 100.0,
                            "exit_price": 99.0,
                            "notional_usd": 25.0,
                            "gross_pnl_usd": -0.25,
                            "fees_usd": 0.0175,
                            "pnl_usd": -0.2675,
                            "close_reason": "time_stop",
                            "confidence": 0.62,
                        },
                    }
                ],
            )
            _write_jsonl(
                market_input_path,
                [
                    {
                        "timestamp": "2026-06-07T12:15:00Z",
                        "symbols": [{"symbol": "BTC", "price": 99.5}],
                    },
                    {
                        "timestamp": "2026-06-07T12:30:00Z",
                        "symbols": [{"symbol": "BTC", "price": 99.0}],
                    },
                ],
            )

            exit_code = main(
                [
                    "exit-follow-through-audit",
                    "--paper-journal",
                    str(paper_journal_path),
                    "--market-input",
                    str(market_input_path),
                    "--fold-label",
                    "fixture",
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--early-windows-minutes",
                    "15,30",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_exit_follow_through_audit")
            self.assertEqual(report["result"]["trades_seen"], 1)
            self.assertEqual(report["result"]["classification_counts"]["early_adverse_loss"], 1)
            self.assertTrue(report_md_path.exists())

    def test_exit_overlay_sweep_cli_runs_on_local_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            paper_journal_path = directory / "paper.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "overlay.json"
            report_md_path = directory / "overlay.md"
            _write_jsonl(
                paper_journal_path,
                [
                    {
                        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
                        "timestamp": "2026-06-07T13:00:00Z",
                        "symbol": "BTC",
                        "close_reason": "time_stop",
                        "trade": {
                            "symbol": "BTC",
                            "side": "long",
                            "decision_id": "decision_btc",
                            "opened_at": "2026-06-07T12:00:00Z",
                            "closed_at": "2026-06-07T13:00:00Z",
                            "entry_price": 100.0,
                            "exit_price": 99.0,
                            "notional_usd": 25.0,
                            "gross_pnl_usd": -0.25,
                            "fees_usd": 0.0175,
                            "pnl_usd": -0.2675,
                            "close_reason": "time_stop",
                            "confidence": 0.62,
                        },
                    }
                ],
            )
            _write_jsonl(
                market_input_path,
                [
                    {
                        "timestamp": "2026-06-07T12:15:00Z",
                        "symbols": [{"symbol": "BTC", "price": 99.5}],
                    },
                    {
                        "timestamp": "2026-06-07T12:30:00Z",
                        "symbols": [{"symbol": "BTC", "price": 99.0}],
                    },
                ],
            )

            exit_code = main(
                [
                    "exit-overlay-sweep",
                    "--paper-journal",
                    str(paper_journal_path),
                    "--market-input",
                    str(market_input_path),
                    "--fold-label",
                    "fixture",
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--early-adverse-bps-values",
                    "0,25",
                    "--early-window-minutes-values",
                    "15",
                    "--mfe-activation-bps-values",
                    "0",
                    "--mfe-giveback-bps-values",
                    "0",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_exit_overlay_sweep")
            self.assertGreater(report["result"]["best_profile"]["delta_pnl_usd"], 0.0)
            self.assertTrue(report_md_path.exists())

    def test_failure_pattern_audit_cli_runs_on_trade_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal_path = directory / "decisions.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "failure.json"
            report_md_path = directory / "failure.md"
            decision_id = "decision_btc"

            _write_jsonl(
                decision_journal_path,
                [
                    _calibration_llm_record(
                        context_id="market_BTC_20260607T120000Z",
                        request_id="request_btc",
                        decision_id=decision_id,
                    )
                ],
            )
            _write_jsonl(
                paper_journal_path,
                [
                    {
                        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
                        "timestamp": "2026-06-07T13:00:00Z",
                        "symbol": "BTC",
                        "close_reason": "time_stop",
                        "trade": {
                            "symbol": "BTC",
                            "side": "long",
                            "decision_id": decision_id,
                            "opened_at": "2026-06-07T12:00:00Z",
                            "closed_at": "2026-06-07T13:00:00Z",
                            "entry_price": 100.0,
                            "exit_price": 99.0,
                            "notional_usd": 25.0,
                            "gross_pnl_usd": -0.25,
                            "fees_usd": 0.0175,
                            "pnl_usd": -0.2675,
                            "close_reason": "time_stop",
                            "confidence": 0.62,
                        },
                    }
                ],
            )
            _write_jsonl(
                market_input_path,
                [
                    {"timestamp": "2026-06-07T12:15:00Z", "symbols": [{"symbol": "BTC", "price": 99.0}]},
                    {"timestamp": "2026-06-07T13:00:00Z", "symbols": [{"symbol": "BTC", "price": 99.0}]},
                ],
            )

            exit_code = main(
                [
                    "failure-pattern-audit",
                    "--decision-journal",
                    str(decision_journal_path),
                    "--paper-journal",
                    str(paper_journal_path),
                    "--market-input",
                    str(market_input_path),
                    "--fold-label",
                    "fixture",
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--windows-minutes",
                    "15,60",
                    "--min-trades",
                    "1",
                    "--min-loss-trades",
                    "1",
                    "--min-loss-folds",
                    "1",
                    "--min-loss-symbols",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_failure_pattern_audit")
            self.assertEqual(report["result"]["summary"]["trades"], 1)
            self.assertTrue(report_md_path.exists())

    def test_entry_veto_replay_cli_runs_on_local_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal_path = directory / "decisions.jsonl"
            baseline_paper_path = directory / "baseline_paper.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "entry_veto.json"
            report_md_path = directory / "entry_veto.md"
            artifact_dir = directory / "artifacts"
            decision = _calibration_llm_record(
                context_id="market_BTC_20260607T120000Z",
                request_id="request_btc",
                decision_id="decision_btc_short",
            )
            decision["proposal"]["side"] = "short"
            decision["proposal"]["invalidation_price"] = 102.0
            decision["proposal"]["stop_bps"] = 120.0
            decision["context"]["features"] = {
                "spread_bps": 1.0,
                "microprice_dislocation_bps": -1.0,
                "trade_flow_bias": -0.5,
                "book_imbalance": -0.5,
                "vwap_distance_bps": -5.0,
                "realized_vol_short_bps": 14.0,
            }
            decision["context"][CANDIDATE_HINT_FIELD] = {
                "schema_version": "trident_ai_candidate_hint_v6",
                "context_id": "market_BTC_20260607T120000Z",
                "timestamp": "2026-06-07T12:00:00Z",
                "symbol": "BTC",
                "side": "short",
                "score": 2.0,
                "liquidity_score": 1.2,
                "estimated_edge_bps": 52.0,
                "round_trip_cost_bps": 10.0,
                "estimated_net_edge_bps": 42.0,
                "edge_to_cost_ratio": 4.2,
                "reasons": ["microprice_aligned", "flow_book_aligned"],
            }

            _write_jsonl(decision_journal_path, [decision])
            _write_jsonl(
                baseline_paper_path,
                [
                    {
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
                ],
            )
            _write_jsonl(
                market_input_path,
                [{"timestamp": "2026-06-07T12:30:00Z", "symbols": [{"symbol": "BTC", "price": 101.0}]}],
            )

            exit_code = main(
                [
                    "entry-veto-replay",
                    "--decision-journal",
                    str(decision_journal_path),
                    "--market-input",
                    str(market_input_path),
                    "--baseline-paper-journal",
                    str(baseline_paper_path),
                    "--fold-label",
                    "fixture",
                    "--veto-bucket",
                    "side_pattern::side=short|microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0",
                    "--artifact-dir",
                    str(artifact_dir),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--symbols",
                    "BTC",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_entry_veto_replay")
            self.assertEqual(report["result"]["fold_rows"][0]["decisions_vetoed"], 1)
            self.assertGreater(report["result"]["delta_summary"]["pnl_usd"], 0.0)
            self.assertTrue(report_md_path.exists())

    def test_candidate_paper_replay_cli_runs_on_local_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            decision_journal_path = directory / "candidate_decisions.jsonl"
            journal_path = directory / "paper.jsonl"
            report_json_path = directory / "candidate_paper.json"
            report_md_path = directory / "candidate_paper.md"
            candidate = _calibration_candidate_record(context_id="market_BTC_20260607T120000Z")
            candidate["symbols"][0]["price"] = 100.0
            candidate["symbols"][0][CANDIDATE_HINT_FIELD].update(
                {
                    "edge_to_cost_ratio": 4.5,
                    "estimated_net_edge_bps": 42.0,
                    "liquidity_score": 1.4,
                    "round_trip_cost_bps": 10.0,
                }
            )
            market = deepcopy(candidate)
            market["timestamp"] = "2026-06-07T15:00:00Z"
            market["symbols"][0]["price"] = 101.0
            market["symbols"][0].pop(CANDIDATE_HINT_FIELD, None)

            _write_jsonl(candidate_input_path, [candidate])
            _write_jsonl(market_input_path, [market])

            exit_code = main(
                [
                    "candidate-paper-replay",
                    "--candidate-input",
                    str(candidate_input_path),
                    "--market-input",
                    str(market_input_path),
                    "--decision-journal-path",
                    str(decision_journal_path),
                    "--journal-path",
                    str(journal_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--take-profit-bps",
                    "500",
                    "--time-stop-minutes",
                    "180",
                    "--min-edge-to-cost",
                    "4",
                    "--min-net-edge-bps",
                    "35",
                    "--min-liquidity-score",
                    "1.2",
                    "--max-round-trip-cost-bps",
                    "12",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_candidate_paper_replay")
            self.assertEqual(report["result"]["decisions_written"], 1)
            self.assertEqual(report["result"]["min_edge_to_cost"], 4.0)
            self.assertEqual(report["result"]["paper_result"]["positions_opened"], 1)
            self.assertTrue(decision_journal_path.exists())
            self.assertTrue(journal_path.exists())
            self.assertTrue(report_md_path.exists())

    def test_candidate_gate_sweep_cli_runs_on_local_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            candidate_input_path = directory / "candidates.jsonl"
            market_input_path = directory / "market.jsonl"
            report_json_path = directory / "gate_sweep.json"
            report_md_path = directory / "gate_sweep.md"
            artifact_dir = directory / "artifacts"
            candidate = _calibration_candidate_record(context_id="market_BTC_20260607T120000Z")
            candidate["symbols"][0]["price"] = 100.0
            candidate["symbols"][0][CANDIDATE_HINT_FIELD].update(
                {
                    "edge_to_cost_ratio": 4.5,
                    "estimated_net_edge_bps": 42.0,
                    "liquidity_score": 1.4,
                    "round_trip_cost_bps": 10.0,
                }
            )
            market = deepcopy(candidate)
            market["timestamp"] = "2026-06-07T15:00:00Z"
            market["symbols"][0]["price"] = 101.0
            market["symbols"][0].pop(CANDIDATE_HINT_FIELD, None)

            _write_jsonl(candidate_input_path, [candidate])
            _write_jsonl(market_input_path, [market])

            exit_code = main(
                [
                    "candidate-gate-sweep",
                    "--candidate-input",
                    str(candidate_input_path),
                    "--market-input",
                    str(market_input_path),
                    "--fold-label",
                    "fixture",
                    "--artifact-dir",
                    str(artifact_dir),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--take-profit-bps",
                    "500",
                    "--time-stop-minutes",
                    "180",
                    "--min-edge-to-cost-values",
                    "4",
                    "--min-net-edge-bps-values",
                    "35",
                    "--min-liquidity-score-values",
                    "1.2",
                    "--max-round-trip-cost-bps-values",
                    "12",
                    "--min-total-closed-trades",
                    "1",
                    "--min-symbols",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_candidate_gate_sweep")
            self.assertEqual(report["result"]["profiles_evaluated"], 1)
            self.assertEqual(report["result"]["best_profile"]["closed_trades"], 1)
            self.assertTrue(report_md_path.exists())
            self.assertTrue(artifact_dir.exists())

    def test_pattern_calibration_cli_runs_on_decision_and_paper_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal_path = directory / "decisions.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "pattern.json"
            report_md_path = directory / "pattern.md"
            decision_id = "decision_btc"

            _write_jsonl(
                decision_journal_path,
                [
                    _calibration_llm_record(
                        context_id="market_BTC_20260607T120000Z",
                        request_id="request_btc",
                        decision_id=decision_id,
                    )
                ],
            )
            _write_jsonl(
                paper_journal_path,
                [
                    {
                        "event_type": PAPER_REPLAY_DECISION_EVENT,
                        "timestamp": "2026-06-07T12:00:00Z",
                        "symbol": "BTC",
                        "decision_id": decision_id,
                        "proposal_action": "open",
                        "paper_action": "open",
                        "reason": "agent_open",
                        "price": 100.0,
                    },
                    {
                        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
                        "timestamp": "2026-06-07T15:00:00Z",
                        "symbol": "BTC",
                        "close_reason": "time_stop",
                        "trade": {
                            "symbol": "BTC",
                            "side": "long",
                            "decision_id": decision_id,
                            "opened_at": "2026-06-07T12:00:00Z",
                            "closed_at": "2026-06-07T15:00:00Z",
                            "entry_price": 100.0,
                            "exit_price": 101.0,
                            "notional_usd": 25.0,
                            "gross_pnl_usd": 0.25,
                            "fees_usd": 0.0175,
                            "pnl_usd": 0.2325,
                            "close_reason": "time_stop",
                            "confidence": 0.67,
                        },
                    },
                ],
            )

            exit_code = main(
                [
                    "pattern-calibration",
                    "--decision-journal",
                    str(decision_journal_path),
                    "--paper-journal",
                    str(paper_journal_path),
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--min-trades-per-pattern",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_pattern_calibration_report")
            self.assertEqual(report["result"]["open_decisions"], 1)
            self.assertEqual(report["result"]["closed_trades"], 1)
            self.assertTrue(report_md_path.exists())

    def test_pattern_fold_validation_cli_runs_on_decision_and_paper_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal_path = directory / "decisions.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "folds.json"
            report_md_path = directory / "folds.md"
            decision_id = "decision_btc"

            _write_jsonl(
                decision_journal_path,
                [
                    _calibration_llm_record(
                        context_id="market_BTC_20260607T120000Z",
                        request_id="request_btc",
                        decision_id=decision_id,
                    )
                ],
            )
            _write_jsonl(
                paper_journal_path,
                [
                    {
                        "event_type": PAPER_REPLAY_DECISION_EVENT,
                        "timestamp": "2026-06-07T12:00:00Z",
                        "symbol": "BTC",
                        "decision_id": decision_id,
                        "proposal_action": "open",
                        "paper_action": "open",
                        "reason": "agent_open",
                        "price": 100.0,
                    },
                    {
                        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
                        "timestamp": "2026-06-07T15:00:00Z",
                        "symbol": "BTC",
                        "close_reason": "time_stop",
                        "trade": {
                            "symbol": "BTC",
                            "side": "long",
                            "decision_id": decision_id,
                            "opened_at": "2026-06-07T12:00:00Z",
                            "closed_at": "2026-06-07T15:00:00Z",
                            "entry_price": 100.0,
                            "exit_price": 101.0,
                            "notional_usd": 25.0,
                            "gross_pnl_usd": 0.25,
                            "fees_usd": 0.0175,
                            "pnl_usd": 0.2325,
                            "close_reason": "time_stop",
                            "confidence": 0.67,
                        },
                    },
                ],
            )

            exit_code = main(
                [
                    "pattern-fold-validation",
                    "--decision-journal",
                    str(decision_journal_path),
                    "--paper-journal",
                    str(paper_journal_path),
                    "--fold-label",
                    "fixture",
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--min-trades-per-fold",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_pattern_fold_validation_report")
            self.assertEqual(report["result"]["fold_labels"], ["fixture"])
            self.assertTrue(report_md_path.exists())

    def test_pattern_support_audit_cli_runs_on_decision_and_paper_journals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal_path = directory / "decisions.jsonl"
            paper_journal_path = directory / "paper.jsonl"
            report_json_path = directory / "support.json"
            report_md_path = directory / "support.md"
            decision_id = "decision_btc"

            _write_jsonl(
                decision_journal_path,
                [
                    _calibration_llm_record(
                        context_id="market_BTC_20260607T120000Z",
                        request_id="request_btc",
                        decision_id=decision_id,
                    )
                ],
            )
            _write_jsonl(
                paper_journal_path,
                [
                    {
                        "event_type": PAPER_REPLAY_DECISION_EVENT,
                        "timestamp": "2026-06-07T12:00:00Z",
                        "symbol": "BTC",
                        "decision_id": decision_id,
                        "proposal_action": "open",
                        "paper_action": "open",
                        "reason": "agent_open",
                        "price": 100.0,
                    },
                    {
                        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
                        "timestamp": "2026-06-07T15:00:00Z",
                        "symbol": "BTC",
                        "close_reason": "time_stop",
                        "trade": {
                            "symbol": "BTC",
                            "side": "long",
                            "decision_id": decision_id,
                            "opened_at": "2026-06-07T12:00:00Z",
                            "closed_at": "2026-06-07T15:00:00Z",
                            "entry_price": 100.0,
                            "exit_price": 101.0,
                            "notional_usd": 25.0,
                            "gross_pnl_usd": 0.25,
                            "fees_usd": 0.0175,
                            "pnl_usd": 0.2325,
                            "close_reason": "time_stop",
                            "confidence": 0.67,
                        },
                    },
                ],
            )

            exit_code = main(
                [
                    "pattern-support-audit",
                    "--decision-journal",
                    str(decision_journal_path),
                    "--paper-journal",
                    str(paper_journal_path),
                    "--fold-label",
                    "fixture",
                    "--report-json-path",
                    str(report_json_path),
                    "--report-md-path",
                    str(report_md_path),
                    "--min-closed-trades",
                    "1",
                    "--min-folds",
                    "1",
                    "--min-positive-folds",
                    "1",
                    "--min-symbols",
                    "1",
                ]
            )

            self.assertEqual(exit_code, 0)
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_pattern_support_audit")
            self.assertEqual(report["result"]["fold_labels"], ["fixture"])
            self.assertTrue(report_md_path.exists())

    def _restore_env(self, key: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _calibration_candidate_record(*, context_id: str) -> dict[str, object]:
    return {
        "timestamp": "2026-06-07T12:00:00Z",
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
                "symbol": "BTC",
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
                    "timestamp": "2026-06-07T12:00:00Z",
                    "symbol": "BTC",
                    "side": "long",
                    "score": 2.0,
                    "raw_score": 2.2,
                    "directional_score": 1.8,
                    "liquidity_score": 1.0,
                    "activity_score": 1.0,
                    "cost_score": 0.9,
                    "estimated_edge_bps": 11.0,
                    "round_trip_cost_bps": 12.0,
                    "estimated_net_edge_bps": -1.0,
                    "edge_to_cost_ratio": 0.916667,
                    "reasons": ["ema_bullish"],
                },
            }
        ],
    }


def _calibration_llm_record(
    *,
    context_id: str,
    request_id: str,
    decision_id: str,
) -> dict[str, object]:
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "timestamp": "2026-06-07T12:00:00Z",
        "symbol": "BTC",
        "request": {"request_id": request_id},
        "context": {
            "schema_version": "trident_ai_market_context_v1",
            "context_id": context_id,
            "as_of": "2026-06-07T12:00:00Z",
            "symbol": "BTC",
            "price": 100.0,
            "regime": "fixture",
            "features": {"spread_bps": 1.0},
            "source": "fixture",
        },
        "llm_response": {"usage": {"estimated_cost_usd": 0.001}},
        "proposal": {
            "schema_version": "trident_ai_proposal_v1",
            "decision_id": decision_id,
            "as_of": "2026-06-07T12:00:00Z",
            "valid_until": "2026-06-07T12:05:00Z",
            "action": "open",
            "symbol": "BTC",
            "side": "long",
            "confidence": 0.67,
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


if __name__ == "__main__":
    unittest.main()
