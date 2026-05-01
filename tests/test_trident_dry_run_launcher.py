import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.live.trident_dry_run_launcher import TridentDryRunLauncher
from app.settings import load_config


class _FakePodARunner:
    def __init__(self, config, coins=None) -> None:
        self.config = config
        self.coins = coins

    async def run(self, **kwargs):
        return {
            "records_processed": 11,
            "journal_path": str(kwargs.get("journal_path")),
            "pod_b_enabled": self.config.pod_b.enabled,
            "pod_c_enabled": self.config.pod_c.enabled,
        }


class _FakePodCRunner:
    def __init__(self, config, coins=None) -> None:
        self.config = config
        self.coins = coins

    async def run(self, **kwargs):
        return {
            "records_processed": 7,
            "journal_path": str(kwargs.get("journal_path")),
            "pod_b_enabled": self.config.pod_b.enabled,
            "pod_c_enabled": self.config.pod_c.enabled,
        }


class _FakePodBRunner:
    def __init__(self, config, coins=None) -> None:
        self.config = config
        self.coins = coins

    async def run(self, **kwargs):
        raise AssertionError("Legacy directional Pod B must not run")


class _FakeHip4OutcomeRunner:
    def __init__(self, config_path) -> None:
        self.config_path = config_path

    def run(self, **kwargs):
        return {
            "loop_count": kwargs.get("max_loops"),
            "mode": "paper",
            "config_path": str(self.config_path),
        }


class TridentDryRunLauncherTests(unittest.TestCase):
    def test_launcher_runs_pod_a_pod_c_and_hip4_as_pod_b_replacement(self) -> None:
        config = load_config("config/trident.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_dir = Path(tmpdir) / "snapshots"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            config.hyperliquid.snapshot_output_dir = str(snapshot_dir)
            launcher = TridentDryRunLauncher(
                config,
                pod_a_runner_factory=_FakePodARunner,
                pod_b_runner_factory=_FakePodBRunner,
                pod_c_runner_factory=_FakePodCRunner,
                hip4_outcome_runner_factory=_FakeHip4OutcomeRunner,
                hip4_outcome_config_path=Path(tmpdir) / "hip4.toml",
            )

            result = asyncio.run(
                launcher.run(
                    max_runtime_seconds=0.1,
                    journal_dir=Path(tmpdir) / "journals",
                    report_dir=Path(tmpdir) / "reports",
                )
            )

            self.assertEqual(result.pod_a["records_processed"], 11)
            self.assertEqual(result.pod_b["mode"], "paper")
            self.assertEqual(result.pod_b["loop_count"], None)
            self.assertEqual(result.pod_c["records_processed"], 7)
            self.assertEqual(result.hip4_outcome["mode"], "paper")
            self.assertEqual(result.hip4_outcome["loop_count"], None)
            self.assertFalse(result.pod_a["pod_b_enabled"])
            self.assertTrue(result.pod_c["pod_c_enabled"])
            self.assertTrue(Path(result.status_path).exists())
            status_payload = json.loads(Path(result.status_path).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["process_state"], "completed")
            self.assertEqual(status_payload["result"]["pod_b"]["mode"], "paper")
            self.assertEqual(status_payload["result"]["hip4_outcome"]["mode"], "paper")
            self.assertEqual(status_payload["hip4_outcome_status_path"], "logs/hip4_outcome_status.json")


if __name__ == "__main__":
    unittest.main()
