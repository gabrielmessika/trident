from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.live.runtime_status import load_runtime_status
from app.trident_ai import (
    SHADOW_CONTEXT_REJECTED_EVENT,
    SHADOW_DECISION_EVENT,
    TridentAIShadowRunner,
    load_trident_ai_config,
    run_trident_ai_shadow,
)


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


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TridentAIShadowRunnerTests(unittest.TestCase):
    def test_shadow_runner_writes_decision_journal_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "trident_ai_shadow.jsonl"
            status_path = directory / "trident_ai_status.json"
            _write_snapshot(input_path)

            result = run_trident_ai_shadow(
                input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                journal_path=journal_path,
                status_path=status_path,
            )

            self.assertEqual(result.records_processed, 1)
            self.assertEqual(result.contexts_built, 4)
            self.assertEqual(result.context_rejections, 1)
            self.assertEqual(result.proposals_generated, 4)
            self.assertEqual(result.proposals_accepted, 4)
            self.assertEqual(result.proposals_rejected, 0)
            self.assertEqual(result.last_timestamp, "2026-06-07T12:00:00Z")

            journal_records = _read_jsonl(journal_path)
            self.assertEqual(len(journal_records), 5)
            decision_records = [
                record
                for record in journal_records
                if record["event_type"] == SHADOW_DECISION_EVENT
            ]
            rejected_records = [
                record
                for record in journal_records
                if record["event_type"] == SHADOW_CONTEXT_REJECTED_EVENT
            ]
            self.assertEqual(len(decision_records), 4)
            self.assertEqual(len(rejected_records), 1)
            self.assertEqual(rejected_records[0]["symbol"], "XRP")
            self.assertEqual(rejected_records[0]["reason"], "symbol_not_allowed")
            self.assertEqual(
                sorted(record["symbol"] for record in decision_records),
                ["BTC", "ETH", "HYPE", "SOL"],
            )
            self.assertTrue(
                all(record["validation"]["accepted"] is True for record in decision_records)
            )
            self.assertEqual(
                {record["proposal"]["action"] for record in decision_records},
                {"hold", "open"},
            )
            self.assertEqual(decision_records[0]["agent"]["name"], "deterministic_shadow_v1")

            status = load_runtime_status(status_path)
            self.assertIsNotNone(status)
            assert status is not None
            self.assertEqual(status["pod"], "trident_ai")
            self.assertEqual(status["mode"], "shadow")
            self.assertEqual(status["agent"]["name"], "deterministic_shadow_v1")
            self.assertEqual(status["tradable_symbols"], ["BTC", "ETH", "SOL", "HYPE"])
            self.assertEqual(status["shadow"]["proposals_accepted"], 4)
            self.assertEqual(status["shadow"]["context_rejections"], 1)

    def test_runner_can_truncate_existing_shadow_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "trident_ai_shadow.jsonl"
            status_path = directory / "trident_ai_status.json"
            _write_snapshot(input_path)
            journal_path.write_text('{"old": true}\n', encoding="utf-8")

            runner = TridentAIShadowRunner(config=load_trident_ai_config("config/trident_ai.toml"))
            runner.run(input_path, journal_path=journal_path, status_path=status_path)

            records = _read_jsonl(journal_path)
            self.assertTrue(records)
            self.assertNotIn("old", records[0])

    def test_runner_respects_smoke_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "trident_ai_shadow.jsonl"
            status_path = directory / "trident_ai_status.json"
            _write_snapshot(input_path)

            result = run_trident_ai_shadow(
                input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                journal_path=journal_path,
                status_path=status_path,
                max_records=1,
                max_contexts=2,
            )

            self.assertEqual(result.records_processed, 1)
            self.assertEqual(result.contexts_built, 2)
            self.assertEqual(result.proposals_generated, 2)
            self.assertTrue(result.limit_reached)
            self.assertEqual(result.max_records, 1)
            self.assertEqual(result.max_contexts, 2)
            records = _read_jsonl(journal_path)
            self.assertEqual(
                len([record for record in records if record["event_type"] == SHADOW_DECISION_EVENT]),
                2,
            )

    def test_runner_filters_symbols_for_smoke_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "trident_ai_shadow.jsonl"
            status_path = directory / "trident_ai_status.json"
            _write_snapshot(input_path)

            result = run_trident_ai_shadow(
                input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                journal_path=journal_path,
                status_path=status_path,
                symbols=["BTC"],
            )

            self.assertEqual(result.contexts_built, 1)
            self.assertEqual(result.context_rejections, 0)
            self.assertEqual(result.symbols_filter, ("BTC",))
            records = _read_jsonl(journal_path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["symbol"], "BTC")


if __name__ == "__main__":
    unittest.main()
