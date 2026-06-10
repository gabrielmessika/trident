from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai import (
    AgentMarketContextBuildConfig,
    JSONFileLLMCache,
    LLMResponse,
    LLMUsage,
    LLM_REPLAY_CONTEXT_REJECTED_EVENT,
    LLM_REPLAY_DECISION_EVENT,
    TridentAIFeatureBuilder,
    TridentAILLMReplayError,
    TridentAILLMReplayRunner,
    build_trade_proposal_request,
    llm_request_cache_key,
    load_fixture_intel_digest,
    load_trident_ai_config,
)
from app.trident_ai.replay import (
    COMPACT_MARKET_CONTEXT_FEATURES,
    TRIDENT_AI_REPLAY_PROMPT_VERSION,
)


MARKET_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "market_snapshots.json"
PROPOSAL_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "initial_proposals.json"
INTEL_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "intel_digest.json"


class NoLiveLLMClient:
    def generate_json(self, request):
        raise AssertionError("LLM client should not be called in cache-only replay")


class FakeLiveLLMClient:
    def __init__(self, *, estimated_cost_usd: float = 0.004) -> None:
        self.calls: list[str] = []
        self.estimated_cost_usd = estimated_cost_usd

    def generate_json(self, request):
        symbol = request.metadata["symbol"]
        self.calls.append(symbol)
        proposal = _proposal_by_symbol(symbol)
        return LLMResponse(
            ok=True,
            provider="openai",
            model=request.model,
            request_id=request.request_id,
            parsed_json=proposal,
            raw_text=json.dumps(proposal, sort_keys=True),
            response_id=f"fake_live_{symbol}",
            usage=LLMUsage(
                input_tokens=1000,
                output_tokens=200,
                total_tokens=1200,
                estimated_cost_usd=self.estimated_cost_usd,
            ),
        )


class MissingAPIKeyLLMClient:
    def generate_json(self, request):
        return LLMResponse(
            ok=False,
            provider="openai",
            model=request.model,
            request_id=request.request_id,
            error="missing_api_key",
        )


def _market_fixture() -> dict[str, object]:
    return json.loads(MARKET_FIXTURE_PATH.read_text(encoding="utf-8"))


def _proposal_fixture() -> dict[str, object]:
    return json.loads(PROPOSAL_FIXTURE_PATH.read_text(encoding="utf-8"))


def _snapshot_record() -> dict[str, object]:
    fixture = _market_fixture()
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


def _proposal_by_symbol(symbol: str) -> dict[str, object]:
    proposals = _proposal_fixture()["proposals"]
    assert isinstance(proposals, list)
    for proposal in proposals:
        assert isinstance(proposal, dict)
        if proposal.get("symbol") == symbol:
            return deepcopy(proposal)
    raise AssertionError(f"missing proposal for {symbol}")


def _contexts(config) -> list:
    snapshot = _snapshot_record()
    now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
    builder = TridentAIFeatureBuilder(
        AgentMarketContextBuildConfig.from_trident_ai_config(config)
    )
    results = builder.build_contexts_from_mappings(
        snapshot["symbols"],
        as_of="2026-06-07T12:00:00Z",
        regime="TrendExpansion",
        now=now,
    )
    return [result.context for result in results if result.context is not None]


def _prime_cache(cache: JSONFileLLMCache, config) -> None:
    now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
    for context in _contexts(config):
        request = build_trade_proposal_request(context=context, config=config, now=now)
        proposal = _proposal_by_symbol(context.symbol)
        cache.put(
            llm_request_cache_key(provider=config.llm.provider, request=request),
            LLMResponse(
                ok=True,
                provider=config.llm.provider,
                model=config.llm.model,
                request_id=request.request_id,
                parsed_json=proposal,
                raw_text=json.dumps(proposal, sort_keys=True),
                response_id=f"cached_{context.symbol}",
                usage=LLMUsage(
                    input_tokens=1000,
                    output_tokens=200,
                    total_tokens=1200,
                    estimated_cost_usd=0.00165,
                ),
            ),
        )


class TridentAILLMReplayRunnerTests(unittest.TestCase):
    def test_trade_proposal_request_uses_compact_prompt_v8_with_intel_and_pass_flags(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        context = _contexts(config)[0]
        intel_digest = load_fixture_intel_digest(INTEL_FIXTURE_PATH, symbols=("BTC", "HYPE"))
        candidate_hint = {
            "schema_version": "trident_ai_candidate_hint_v1",
            "context_id": context.context_id,
            "timestamp": context.as_of,
            "symbol": context.symbol,
            "side": "long",
            "score": 1.654321,
            "raw_score": 2.123456,
            "directional_score": 1.91,
            "liquidity_score": 0.88,
            "activity_score": 0.99,
            "cost_score": 0.78,
            "estimated_edge_bps": 10.5,
            "round_trip_cost_bps": 13.25,
            "estimated_net_edge_bps": -2.75,
            "edge_to_cost_ratio": 0.79245,
            "reasons": ["long_directional_score", "ema_bullish", "spread_ok"],
        }
        request = build_trade_proposal_request(
            context=context,
            config=config,
            now=datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc),
            candidate_hint=candidate_hint,
            intel_digest=intel_digest,
        )

        payload = json.loads(request.user_prompt)
        compact_context = payload["ctx"]
        compact_features = compact_context["f"]
        compact_candidate = compact_context["candidate"]
        compact_intel = compact_context["intel"]
        full_context_payload = json.dumps(
            {
                "market_context": context.to_dict(),
                "candidate_hint": candidate_hint,
                "intel_digest": intel_digest.to_dict(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        compact_context_payload = json.dumps(
            {"ctx": compact_context},
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(request.metadata["prompt_version"], TRIDENT_AI_REPLAY_PROMPT_VERSION)
        self.assertEqual(TRIDENT_AI_REPLAY_PROMPT_VERSION, "trident_ai_replay_v9")
        self.assertNotIn("market_context", payload)
        self.assertEqual(payload["rules"]["actions"], ["hold", "open"])
        self.assertIn("intel_digest", payload["rules"])
        self.assertEqual(compact_context["id"], context.context_id)
        self.assertEqual(compact_context["s"], context.symbol)
        self.assertEqual(compact_intel["digest_id"], "intel_digest_20260607T120000Z")
        self.assertEqual(compact_intel["global_market_impact"], "neutral")
        self.assertEqual(len(compact_intel["items"]), 1)
        self.assertEqual(compact_intel["items"][0]["symbol"], "BTC")
        self.assertFalse(compact_intel["items"][0]["veto_entry"])
        self.assertEqual(compact_candidate["side"], "long")
        self.assertEqual(compact_candidate["score"], 1.6543)
        self.assertEqual(compact_candidate["raw_score"], 2.1235)
        self.assertEqual(compact_candidate["directional"], 1.91)
        self.assertEqual(compact_candidate["cost_score"], 0.78)
        self.assertEqual(compact_candidate["edge_bps"], 10.5)
        self.assertEqual(compact_candidate["round_trip_cost_bps"], 13.25)
        self.assertEqual(compact_candidate["net_edge_bps"], -2.75)
        self.assertEqual(compact_candidate["edge_to_cost"], 0.79245)
        self.assertEqual(
            compact_candidate["passes"],
            {
                "edge_to_cost": False,
                "net_edge": False,
                "microprice": True,
                "local_gate": False,
                "research_edge_to_cost": False,
                "research_net_edge": False,
                "research_cost": False,
                "research_gate": False,
            },
        )
        self.assertEqual(compact_candidate["reasons"], candidate_hint["reasons"])
        self.assertEqual(set(compact_features), set(COMPACT_MARKET_CONTEXT_FEATURES))
        self.assertNotIn("best_bid", compact_features)
        self.assertNotIn("bid_depth_10bps", compact_features)
        self.assertLess(len(compact_context_payload), len(full_context_payload) * 0.65)
        self.assertIn("scientific notation", request.system_prompt)

    def test_llm_replay_uses_cache_only_and_writes_reports(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "trident_ai_llm_replay.jsonl"
            report_json_path = directory / "trident_ai_llm_replay.json"
            report_md_path = directory / "trident_ai_llm_replay.md"
            cache = JSONFileLLMCache(directory / "cache")
            _write_snapshot(input_path)
            _prime_cache(cache, config)

            runner = TridentAILLMReplayRunner(
                config=config,
                client=NoLiveLLMClient(),
                cache=cache,
                allow_live_llm_calls=False,
            )
            result = runner.run(
                input_path,
                journal_path=journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
            )

            self.assertEqual(result.records_processed, 1)
            self.assertEqual(result.contexts_built, 4)
            self.assertEqual(result.context_rejections, 1)
            self.assertEqual(result.llm_requests, 4)
            self.assertEqual(result.cache_hits, 4)
            self.assertEqual(result.live_llm_calls, 0)
            self.assertEqual(result.llm_failures, 0)
            self.assertEqual(result.proposals_generated, 4)
            self.assertEqual(result.proposals_accepted, 4)
            self.assertEqual(result.proposals_rejected, 0)
            self.assertEqual(result.action_counts["open"], 4)
            self.assertEqual(result.input_tokens, 4000)
            self.assertEqual(result.output_tokens, 800)
            self.assertEqual(result.incremental_cost_usd, 0.0)
            self.assertEqual(result.estimated_original_cost_usd, 0.0066)

            records = _read_jsonl(journal_path)
            self.assertEqual(len(records), 5)
            decisions = [
                record
                for record in records
                if record["event_type"] == LLM_REPLAY_DECISION_EVENT
            ]
            rejected_contexts = [
                record
                for record in records
                if record["event_type"] == LLM_REPLAY_CONTEXT_REJECTED_EVENT
            ]
            self.assertEqual(len(decisions), 4)
            self.assertEqual(len(rejected_contexts), 1)
            self.assertEqual(rejected_contexts[0]["symbol"], "XRP")
            self.assertTrue(all(record["llm_response"]["cached"] for record in decisions))
            self.assertTrue(all(record["validation"]["accepted"] for record in decisions))

            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_llm_replay")
            self.assertEqual(report["result"]["cache_hits"], 4)
            self.assertEqual(
                report["comparison"]["full_bot_baseline"]["status"],
                "reference_only_not_run_in_step_6",
            )
            self.assertIn("TRIDENT-AI LLM Replay", report_md_path.read_text(encoding="utf-8"))

    def test_llm_replay_fails_closed_on_cache_miss(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "journal.jsonl"
            report_json_path = directory / "report.json"
            report_md_path = directory / "report.md"
            _write_snapshot(input_path)

            runner = TridentAILLMReplayRunner(
                config=config,
                client=NoLiveLLMClient(),
                cache=JSONFileLLMCache(directory / "empty_cache"),
                allow_live_llm_calls=False,
            )
            result = runner.run(
                input_path,
                journal_path=journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
            )

            self.assertEqual(result.llm_requests, 4)
            self.assertEqual(result.cache_hits, 0)
            self.assertEqual(result.live_llm_calls, 0)
            self.assertEqual(result.llm_failures, 4)
            self.assertEqual(result.proposals_generated, 0)
            self.assertEqual(result.proposals_accepted, 0)
            self.assertEqual(
                result.rejection_reasons["cache_miss_live_calls_disabled"],
                4,
            )

            records = _read_jsonl(journal_path)
            decisions = [
                record
                for record in records
                if record["event_type"] == LLM_REPLAY_DECISION_EVENT
            ]
            self.assertEqual(len(decisions), 4)
            self.assertTrue(
                all(
                    record["validation"]["reason"] == "cache_miss_live_calls_disabled"
                    for record in decisions
                )
            )
            self.assertTrue(all(record["proposal"] is None for record in decisions))

    def test_llm_replay_applies_intel_veto_before_cache_or_live_call(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            journal_path = directory / "llm.jsonl"
            report_json_path = directory / "llm.json"
            report_md_path = directory / "llm.md"
            input_path = directory / "snapshots.jsonl"
            _write_snapshot(input_path)
            runner = TridentAILLMReplayRunner(
                config=config,
                client=NoLiveLLMClient(),
                cache=JSONFileLLMCache(directory / "empty_cache"),
            )

            result = runner.run(
                input_path,
                journal_path=journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                max_contexts=1,
                symbols=("HYPE",),
                intel_digest_path=INTEL_FIXTURE_PATH,
            )

            self.assertEqual(result.contexts_built, 1)
            self.assertEqual(result.context_rejections, 1)
            self.assertEqual(result.llm_requests, 0)
            self.assertEqual(result.llm_failures, 0)
            self.assertEqual(result.intel_digest_id, "intel_digest_20260607T120000Z")
            self.assertEqual(result.rejection_reasons["intel_veto"], 1)

            records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["event_type"], LLM_REPLAY_CONTEXT_REJECTED_EVENT)
            self.assertEqual(records[0]["reason"], "intel_veto")
            self.assertEqual(records[0]["details"]["intel_veto_reasons"], ["fixture_hype_001"])

    def test_llm_replay_respects_smoke_limits(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "journal.jsonl"
            report_json_path = directory / "report.json"
            report_md_path = directory / "report.md"
            cache = JSONFileLLMCache(directory / "cache")
            _write_snapshot(input_path)
            _prime_cache(cache, config)

            runner = TridentAILLMReplayRunner(
                config=config,
                client=NoLiveLLMClient(),
                cache=cache,
                allow_live_llm_calls=False,
            )
            result = runner.run(
                input_path,
                journal_path=journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                max_records=1,
                max_contexts=2,
            )

            self.assertEqual(result.records_processed, 1)
            self.assertEqual(result.contexts_built, 2)
            self.assertEqual(result.llm_requests, 2)
            self.assertEqual(result.cache_hits, 2)
            self.assertTrue(result.limit_reached)
            self.assertEqual(result.max_records, 1)
            self.assertEqual(result.max_contexts, 2)

            records = _read_jsonl(journal_path)
            decisions = [
                record
                for record in records
                if record["event_type"] == LLM_REPLAY_DECISION_EVENT
            ]
            self.assertEqual(len(decisions), 2)
            self.assertEqual([record["symbol"] for record in decisions], ["BTC", "ETH"])

    def test_llm_replay_filters_symbols_for_smoke_replay(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "journal.jsonl"
            report_json_path = directory / "report.json"
            report_md_path = directory / "report.md"
            cache = JSONFileLLMCache(directory / "cache")
            _write_snapshot(input_path)
            _prime_cache(cache, config)

            runner = TridentAILLMReplayRunner(
                config=config,
                client=NoLiveLLMClient(),
                cache=cache,
                allow_live_llm_calls=False,
            )
            result = runner.run(
                input_path,
                journal_path=journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                symbols=["BTC"],
            )

            self.assertEqual(result.contexts_built, 1)
            self.assertEqual(result.context_rejections, 0)
            self.assertEqual(result.llm_requests, 1)
            self.assertEqual(result.symbols_filter, ("BTC",))
            records = _read_jsonl(journal_path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["symbol"], "BTC")

    def test_live_replay_requires_explicit_call_and_cost_caps(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            _write_snapshot(input_path)
            runner = TridentAILLMReplayRunner(
                config=config,
                client=FakeLiveLLMClient(),
                cache=JSONFileLLMCache(directory / "cache"),
                allow_live_llm_calls=True,
            )

            with self.assertRaisesRegex(TridentAILLMReplayError, "max_live_calls_required"):
                runner.run(input_path, max_incremental_cost_usd=0.05)
            with self.assertRaisesRegex(
                TridentAILLMReplayError,
                "max_incremental_cost_usd_required",
            ):
                runner.run(input_path, max_live_calls=2)

    def test_live_replay_fills_cache_until_live_call_limit(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            live_journal_path = directory / "live_journal.jsonl"
            live_report_json_path = directory / "live_report.json"
            live_report_md_path = directory / "live_report.md"
            cache_only_journal_path = directory / "cache_only_journal.jsonl"
            cache_only_report_json_path = directory / "cache_only_report.json"
            cache_only_report_md_path = directory / "cache_only_report.md"
            cache = JSONFileLLMCache(directory / "cache")
            client = FakeLiveLLMClient(estimated_cost_usd=0.004)
            _write_snapshot(input_path)

            live_runner = TridentAILLMReplayRunner(
                config=config,
                client=client,
                cache=cache,
                allow_live_llm_calls=True,
            )
            live_result = live_runner.run(
                input_path,
                journal_path=live_journal_path,
                report_json_path=live_report_json_path,
                report_md_path=live_report_md_path,
                max_live_calls=2,
                max_incremental_cost_usd=0.05,
            )

            self.assertEqual(client.calls, ["BTC", "ETH"])
            self.assertEqual(live_result.llm_requests, 4)
            self.assertEqual(live_result.live_llm_calls, 2)
            self.assertEqual(live_result.proposals_generated, 2)
            self.assertEqual(live_result.proposals_accepted, 2)
            self.assertEqual(live_result.llm_failures, 2)
            self.assertEqual(live_result.incremental_cost_usd, 0.008)
            self.assertEqual(live_result.rejection_reasons["live_call_limit_reached"], 2)
            self.assertEqual(len(list((directory / "cache").glob("*.json"))), 2)

            cache_only_runner = TridentAILLMReplayRunner(
                config=config,
                client=NoLiveLLMClient(),
                cache=cache,
                allow_live_llm_calls=False,
            )
            cache_only_result = cache_only_runner.run(
                input_path,
                journal_path=cache_only_journal_path,
                report_json_path=cache_only_report_json_path,
                report_md_path=cache_only_report_md_path,
                max_contexts=2,
            )

            self.assertEqual(cache_only_result.llm_requests, 2)
            self.assertEqual(cache_only_result.cache_hits, 2)
            self.assertEqual(cache_only_result.live_llm_calls, 0)
            self.assertEqual(cache_only_result.llm_failures, 0)
            self.assertEqual(cache_only_result.proposals_accepted, 2)

    def test_live_replay_blocks_calls_when_cost_budget_is_too_low(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "journal.jsonl"
            report_json_path = directory / "report.json"
            report_md_path = directory / "report.md"
            client = FakeLiveLLMClient()
            _write_snapshot(input_path)

            runner = TridentAILLMReplayRunner(
                config=config,
                client=client,
                cache=JSONFileLLMCache(directory / "cache"),
                allow_live_llm_calls=True,
            )
            result = runner.run(
                input_path,
                journal_path=journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                max_live_calls=10,
                max_incremental_cost_usd=0.001,
            )

            self.assertEqual(client.calls, [])
            self.assertEqual(result.live_llm_calls, 0)
            self.assertEqual(result.llm_failures, 4)
            self.assertEqual(result.rejection_reasons["incremental_cost_budget_exhausted"], 4)

    def test_missing_api_key_does_not_count_as_provider_live_call(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "snapshots.jsonl"
            journal_path = directory / "journal.jsonl"
            report_json_path = directory / "report.json"
            report_md_path = directory / "report.md"
            _write_snapshot(input_path)

            runner = TridentAILLMReplayRunner(
                config=config,
                client=MissingAPIKeyLLMClient(),
                cache=JSONFileLLMCache(directory / "cache"),
                allow_live_llm_calls=True,
            )
            result = runner.run(
                input_path,
                journal_path=journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
                max_live_calls=10,
                max_incremental_cost_usd=0.05,
            )

            self.assertEqual(result.llm_requests, 4)
            self.assertEqual(result.live_llm_calls, 0)
            self.assertEqual(result.llm_failures, 4)
            self.assertEqual(result.incremental_cost_usd, 0.0)
            self.assertEqual(result.rejection_reasons["missing_api_key"], 4)


if __name__ == "__main__":
    unittest.main()
