from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.trident_ai.cli import load_trident_ai_env_file, main


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

    def _restore_env(self, key: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
