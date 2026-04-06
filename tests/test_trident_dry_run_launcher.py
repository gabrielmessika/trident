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
    def __init__(self, config_path) -> None:
        self.config_path = Path(config_path)

    def run_live(self, **kwargs):
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        return {
            "records_processed": 5,
            "fills_emitted": 2,
            "report_path": str(kwargs.get("report_output")),
            "managed_symbols": payload["trident"]["managed_symbols"],
            "target_usd": payload["trident"]["target_usd"],
        }


class TridentDryRunLauncherTests(unittest.TestCase):
    def test_launcher_runs_three_pods_and_writes_status(self) -> None:
        config = load_config("config/trident.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_dir = Path(tmpdir) / "snapshots"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            config.hyperliquid.snapshot_output_dir = str(snapshot_dir)
            config.pod_b.passivbot_config_path = str(Path(tmpdir) / "runtime" / "pod_b.json")
            launcher = TridentDryRunLauncher(
                config,
                pod_a_runner_factory=_FakePodARunner,
                pod_b_runner_factory=_FakePodBRunner,
                pod_c_runner_factory=_FakePodCRunner,
            )

            result = asyncio.run(
                launcher.run(
                    max_runtime_seconds=0.1,
                    journal_dir=Path(tmpdir) / "journals",
                    report_dir=Path(tmpdir) / "reports",
                    pod_b_max_idle_loops=1,
                )
            )

            self.assertEqual(result.pod_a["records_processed"], 11)
            self.assertEqual(result.pod_b["records_processed"], 5)
            self.assertEqual(result.pod_c["records_processed"], 7)
            self.assertTrue(result.pod_a["pod_b_enabled"])
            self.assertTrue(result.pod_c["pod_c_enabled"])
            self.assertTrue(Path(result.pod_b_config_path).exists())
            self.assertTrue(Path(result.status_path).exists())
            status_payload = json.loads(Path(result.status_path).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["process_state"], "completed")
            self.assertEqual(status_payload["result"]["pod_b"]["fills_emitted"], 2)


if __name__ == "__main__":
    unittest.main()
