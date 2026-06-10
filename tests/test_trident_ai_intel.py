from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    INTEL_DIGEST_EVENT,
    TridentAIConfig,
    TridentAIIntelConfig,
    digest_stats,
    intel_veto_reasons_for_symbol,
    load_fixture_intel_digest,
    run_trident_ai_intel_digest,
    xai_responses_payload,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "intel_digest.json"


class TridentAIIntelTests(unittest.TestCase):
    def test_loads_fixture_digest_and_extracts_veto_symbols(self) -> None:
        digest = load_fixture_intel_digest(FIXTURE_PATH, symbols=("BTC", "HYPE"))

        stats = digest_stats(digest, symbols=("BTC", "HYPE"))

        self.assertEqual(digest.source, "fixture")
        self.assertEqual(stats["items_seen"], 2)
        self.assertEqual(stats["veto_symbols"], ["HYPE"])
        self.assertEqual(stats["impact_counts"], {"neutral": 1, "negative": 1})
        self.assertEqual(intel_veto_reasons_for_symbol(digest, "HYPE"), ["fixture_hype_001"])
        self.assertEqual(intel_veto_reasons_for_symbol(digest, "BTC"), [])

    def test_run_intel_digest_uses_neutral_digest_when_live_calls_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            result = run_trident_ai_intel_digest(
                config=TridentAIConfig(),
                symbols=("BTC", "ETH"),
                as_of="2026-06-07T12:00:00Z",
                journal_path=directory / "intel.jsonl",
                report_json_path=directory / "intel.json",
                report_md_path=directory / "intel.md",
            )

            self.assertEqual(result.global_market_impact, "neutral")
            self.assertEqual(result.items_seen, 0)
            self.assertEqual(result.live_intel_calls, 0)
            self.assertEqual(result.estimated_incremental_cost_usd, 0.0)
            self.assertEqual(result.skip_reasons, {"live_intel_calls_disabled": 1})

            journal_records = (directory / "intel.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(journal_records), 1)
            self.assertEqual(json.loads(journal_records[0])["event_type"], INTEL_DIGEST_EVENT)
            report = json.loads((directory / "intel.json").read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_intel_digest")
            self.assertTrue((directory / "intel.md").exists())

    def test_run_intel_digest_with_fixture_has_no_live_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            result = run_trident_ai_intel_digest(
                config=TridentAIConfig(),
                symbols=("BTC", "HYPE"),
                fixture_input_path=FIXTURE_PATH,
                journal_path=directory / "intel.jsonl",
                report_json_path=directory / "intel.json",
                report_md_path=directory / "intel.md",
            )

            self.assertEqual(result.provider, "fixture")
            self.assertEqual(result.veto_symbols, ("HYPE",))
            self.assertEqual(result.live_intel_calls, 0)
            self.assertEqual(result.estimated_incremental_cost_usd, 0.0)

    def test_xai_payload_uses_x_and_web_search_tools_with_allowlist(self) -> None:
        config = TridentAIIntelConfig(
            x_search_enabled=True,
            web_search_enabled=True,
            allowed_x_handles=("HyperliquidX",),
            allowed_web_domains=("hyperliquid.xyz",),
        )
        request_payload = xai_responses_payload(
            request=_fake_request(),
            config=config,
        )

        self.assertEqual(request_payload["model"], "grok-4.3")
        tools = request_payload["tools"]
        self.assertEqual(tools[0]["type"], "x_search")
        self.assertEqual(tools[0]["allowed_x_handles"], ["HyperliquidX"])
        self.assertEqual(tools[1]["type"], "web_search")


def _fake_request():
    from app.trident_ai.intel import TridentAIIntelRequest

    return TridentAIIntelRequest(
        request_id="request_fixture",
        as_of="2026-06-07T12:00:00Z",
        symbols=("BTC", "ETH"),
        from_date="2026-06-06",
        to_date="2026-06-07",
    )


if __name__ == "__main__":
    unittest.main()
