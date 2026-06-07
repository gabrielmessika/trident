from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from app.backtest.snapshot_loader import SnapshotLoader, SnapshotRecord
from app.persistence.journal import JsonlJournal
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.features import AgentMarketContextBuildConfig, TridentAIFeatureBuilder
from app.trident_ai.llm import (
    JSONFileLLMCache,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    OpenAIResponsesClient,
    estimate_token_cost_usd,
    llm_request_cache_key,
)
from app.trident_ai.types import (
    AgentMarketContext,
    AgentTradeProposal,
    agent_trade_proposal_json_schema,
    validate_agent_proposal,
)


LLM_REPLAY_DECISION_EVENT = "trident_ai_llm_replay_decision"
LLM_REPLAY_CONTEXT_REJECTED_EVENT = "trident_ai_llm_replay_context_rejected"
TRIDENT_AI_REPLAY_PROMPT_VERSION = "trident_ai_replay_v2"
LIVE_CALL_COST_RESERVE_OUTPUT_TOKENS = 800

FULL_BOT_BASELINE_REFERENCES = (
    "server-data/replay_reports/official_baseline_current_cli_20260513.md",
    "server-data/replay_reports/official_baseline_current_cli_20260513.json",
    "server-data/replay_reports/BACKTEST_REFERENCE_STATUS_20260513.md",
)


class LLMJSONClient(Protocol):
    def generate_json(self, request: LLMRequest) -> LLMResponse:
        ...


class TridentAILLMReplayError(ValueError):
    """Raised when a LLM replay would not be reproducible."""


@dataclass(frozen=True, slots=True)
class TridentAILLMReplayResult:
    input_path: str
    journal_path: str
    report_json_path: str
    report_md_path: str
    cache_dir: str
    prompt_version: str
    provider: str
    model: str
    allow_live_llm_calls: bool
    records_processed: int = 0
    contexts_built: int = 0
    context_rejections: int = 0
    llm_requests: int = 0
    cache_hits: int = 0
    live_llm_calls: int = 0
    llm_failures: int = 0
    proposals_generated: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_original_cost_usd: float = 0.0
    incremental_cost_usd: float = 0.0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    action_counts: dict[str, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    max_records: int | None = None
    max_contexts: int | None = None
    symbols_filter: tuple[str, ...] = ()
    limit_reached: bool = False
    max_live_calls: int | None = None
    max_incremental_cost_usd: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "journal_path": self.journal_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "cache_dir": self.cache_dir,
            "prompt_version": self.prompt_version,
            "provider": self.provider,
            "model": self.model,
            "cache_required": True,
            "allow_live_llm_calls": self.allow_live_llm_calls,
            "records_processed": self.records_processed,
            "contexts_built": self.contexts_built,
            "context_rejections": self.context_rejections,
            "llm_requests": self.llm_requests,
            "cache_hits": self.cache_hits,
            "live_llm_calls": self.live_llm_calls,
            "llm_failures": self.llm_failures,
            "proposals_generated": self.proposals_generated,
            "proposals_accepted": self.proposals_accepted,
            "proposals_rejected": self.proposals_rejected,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_original_cost_usd": round(self.estimated_original_cost_usd, 8),
            "incremental_cost_usd": round(self.incremental_cost_usd, 8),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "action_counts": dict(sorted(self.action_counts.items())),
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "max_records": self.max_records,
            "max_contexts": self.max_contexts,
            "symbols_filter": list(self.symbols_filter),
            "limit_reached": self.limit_reached,
            "max_live_calls": self.max_live_calls,
            "max_incremental_cost_usd": self.max_incremental_cost_usd,
        }


@dataclass(slots=True)
class _ReplayCounters:
    records_processed: int = 0
    contexts_built: int = 0
    context_rejections: int = 0
    llm_requests: int = 0
    cache_hits: int = 0
    live_llm_calls: int = 0
    llm_failures: int = 0
    proposals_generated: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_original_cost_usd: float = 0.0
    incremental_cost_usd: float = 0.0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    action_counts: Counter[str] = field(default_factory=Counter)
    rejection_reasons: Counter[str] = field(default_factory=Counter)
    limit_reached: bool = False


class TridentAILLMReplayRunner:
    def __init__(
        self,
        *,
        config: TridentAIConfig | None = None,
        client: LLMJSONClient | None = None,
        cache: JSONFileLLMCache | None = None,
        cache_dir: str | Path | None = None,
        allow_live_llm_calls: bool = False,
        prompt_version: str = TRIDENT_AI_REPLAY_PROMPT_VERSION,
        loader: SnapshotLoader | None = None,
        feature_builder: TridentAIFeatureBuilder | None = None,
    ) -> None:
        self.config = config or load_trident_ai_config()
        if not self.config.llm.cache_enabled:
            raise TridentAILLMReplayError("llm_cache_disabled")
        self.cache = cache or JSONFileLLMCache(cache_dir or self.config.paths.llm_cache_dir)
        self.client = client or OpenAIResponsesClient(self.config.llm, cache=self.cache)
        self.allow_live_llm_calls = allow_live_llm_calls
        self.prompt_version = prompt_version
        self.loader = loader or SnapshotLoader()
        self.feature_builder = feature_builder or TridentAIFeatureBuilder(
            AgentMarketContextBuildConfig.from_trident_ai_config(self.config)
        )

    def run(
        self,
        input_path: str | Path,
        *,
        journal_path: str | Path | None = None,
        report_json_path: str | Path | None = None,
        report_md_path: str | Path | None = None,
        truncate_journal: bool = True,
        max_records: int | None = None,
        max_contexts: int | None = None,
        symbols: Sequence[str] | None = None,
        max_live_calls: int | None = None,
        max_incremental_cost_usd: float | None = None,
    ) -> TridentAILLMReplayResult:
        max_records = _positive_optional_int(max_records, field_name="max_records")
        max_contexts = _positive_optional_int(max_contexts, field_name="max_contexts")
        max_live_calls = _positive_optional_int(max_live_calls, field_name="max_live_calls")
        max_incremental_cost_usd = _positive_optional_float(
            max_incremental_cost_usd,
            field_name="max_incremental_cost_usd",
        )
        _validate_live_call_caps(
            allow_live_llm_calls=self.allow_live_llm_calls,
            max_live_calls=max_live_calls,
            max_incremental_cost_usd=max_incremental_cost_usd,
        )
        symbols_filter = _symbols_filter(symbols)
        run_id = _timestamp_id(datetime.now(timezone.utc))
        output_dir = Path(self.config.paths.replay_output_dir)
        journal_output = Path(journal_path or output_dir / f"trident_ai_llm_replay_{run_id}.jsonl")
        report_json_output = Path(
            report_json_path or output_dir / f"trident_ai_llm_replay_{run_id}.json"
        )
        report_md_output = Path(
            report_md_path or output_dir / f"trident_ai_llm_replay_{run_id}.md"
        )
        journal = JsonlJournal(journal_output, truncate=truncate_journal)
        counters = _ReplayCounters()

        for record in self.loader.iter_merged_jsonl(input_path):
            if max_records is not None and counters.records_processed >= max_records:
                counters.limit_reached = True
                break
            counters.records_processed += 1
            record_now = _record_datetime(record)
            timestamp = _format_timestamp(record_now)
            counters.first_timestamp = counters.first_timestamp or timestamp
            counters.last_timestamp = timestamp
            regime = _record_regime(record)

            for build_result in self.feature_builder.build_contexts_from_mappings(
                _filter_symbol_payloads(record.symbols, symbols_filter),
                as_of=timestamp,
                regime=regime,
                now=record_now,
            ):
                if build_result.context is None:
                    counters.context_rejections += 1
                    counters.rejection_reasons[build_result.reason] += 1
                    journal.append(
                        _context_rejection_record(
                            record=record,
                            timestamp=timestamp,
                            symbol=build_result.symbol,
                            reason=build_result.reason,
                        )
                    )
                    continue
                if max_contexts is not None and counters.contexts_built >= max_contexts:
                    counters.limit_reached = True
                    break
                counters.contexts_built += 1
                self._process_context(
                    record=record,
                    timestamp=timestamp,
                    context=build_result.context,
                    now=record_now,
                    journal=journal,
                    counters=counters,
                    max_live_calls=max_live_calls,
                    max_incremental_cost_usd=max_incremental_cost_usd,
                )
            if counters.limit_reached:
                break

        result = TridentAILLMReplayResult(
            input_path=str(input_path),
            journal_path=str(journal_output),
            report_json_path=str(report_json_output),
            report_md_path=str(report_md_output),
            cache_dir=str(self.cache.cache_dir),
            prompt_version=self.prompt_version,
            provider=self.config.llm.provider,
            model=self.config.llm.model,
            allow_live_llm_calls=self.allow_live_llm_calls,
            records_processed=counters.records_processed,
            contexts_built=counters.contexts_built,
            context_rejections=counters.context_rejections,
            llm_requests=counters.llm_requests,
            cache_hits=counters.cache_hits,
            live_llm_calls=counters.live_llm_calls,
            llm_failures=counters.llm_failures,
            proposals_generated=counters.proposals_generated,
            proposals_accepted=counters.proposals_accepted,
            proposals_rejected=counters.proposals_rejected,
            input_tokens=counters.input_tokens,
            output_tokens=counters.output_tokens,
            estimated_original_cost_usd=counters.estimated_original_cost_usd,
            incremental_cost_usd=counters.incremental_cost_usd,
            first_timestamp=counters.first_timestamp,
            last_timestamp=counters.last_timestamp,
            action_counts=dict(counters.action_counts),
            rejection_reasons=dict(counters.rejection_reasons),
            max_records=max_records,
            max_contexts=max_contexts,
            symbols_filter=symbols_filter,
            limit_reached=counters.limit_reached,
            max_live_calls=max_live_calls,
            max_incremental_cost_usd=max_incremental_cost_usd,
        )
        payload = build_llm_replay_report_payload(
            result=result,
            config=self.config,
            generated_at=_format_timestamp(datetime.now(timezone.utc)),
        )
        _write_report_outputs(payload, json_path=report_json_output, md_path=report_md_output)
        return result

    def _process_context(
        self,
        *,
        record: SnapshotRecord,
        timestamp: str,
        context: AgentMarketContext,
        now: datetime,
        journal: JsonlJournal,
        counters: _ReplayCounters,
        max_live_calls: int | None,
        max_incremental_cost_usd: float | None,
    ) -> None:
        request = build_trade_proposal_request(
            context=context,
            config=self.config,
            now=now,
            prompt_version=self.prompt_version,
        )
        cache_key = llm_request_cache_key(provider=self.config.llm.provider, request=request)
        response, live_call_performed = self._cached_or_live_response(
            request,
            cache_key,
            counters=counters,
            max_live_calls=max_live_calls,
            max_incremental_cost_usd=max_incremental_cost_usd,
        )
        counters.llm_requests += 1
        if response.cached:
            counters.cache_hits += 1
        if live_call_performed:
            counters.live_llm_calls += 1
            if response.ok:
                self.cache.put(cache_key, response)
        _accumulate_usage(counters, response.usage, incremental=live_call_performed)

        proposal_payload: dict[str, object] | None = None
        accepted = False
        reason = response.error or "llm_response_failed"
        if response.ok and response.parsed_json is not None:
            proposal_payload = response.parsed_json
            counters.proposals_generated += 1
            validation = validate_agent_proposal(
                proposal_payload,
                market_context=context,
                config=self.config.proposal_validation_config(),
                now=now,
            )
            accepted = validation.accepted
            reason = validation.reason
            if validation.proposal is not None:
                counters.action_counts[validation.proposal.action] += 1
            if accepted:
                counters.proposals_accepted += 1
            else:
                counters.proposals_rejected += 1
                counters.rejection_reasons[reason] += 1
        else:
            counters.llm_failures += 1
            counters.rejection_reasons[reason] += 1

        journal.append(
            _decision_record(
                record=record,
                timestamp=timestamp,
                context=context,
                request=request,
                cache_key=cache_key,
                response=response,
                proposal_payload=proposal_payload,
                accepted=accepted,
                reason=reason,
                mode=self.config.mode,
            )
        )

    def _cached_or_live_response(
        self,
        request: LLMRequest,
        cache_key: str,
        *,
        counters: _ReplayCounters,
        max_live_calls: int | None,
        max_incremental_cost_usd: float | None,
    ) -> tuple[LLMResponse, bool]:
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached, False
        if not self.allow_live_llm_calls:
            return LLMResponse(
                ok=False,
                provider=self.config.llm.provider,
                model=request.model,
                request_id=request.request_id,
                error="cache_miss_live_calls_disabled",
            ), False
        if max_live_calls is not None and counters.live_llm_calls >= max_live_calls:
            return LLMResponse(
                ok=False,
                provider=self.config.llm.provider,
                model=request.model,
                request_id=request.request_id,
                error="live_call_limit_reached",
            ), False
        if max_incremental_cost_usd is not None:
            reserve_cost = _request_cost_reserve_usd(request)
            if counters.incremental_cost_usd + reserve_cost > max_incremental_cost_usd:
                return LLMResponse(
                    ok=False,
                    provider=self.config.llm.provider,
                    model=request.model,
                    request_id=request.request_id,
                    error="incremental_cost_budget_exhausted",
                ), False
        response = self.client.generate_json(request)
        return response, _response_counts_as_live_provider_call(response)


def run_trident_ai_llm_replay(
    input_path: str | Path,
    *,
    config: TridentAIConfig | None = None,
    cache_dir: str | Path | None = None,
    allow_live_llm_calls: bool = False,
    journal_path: str | Path | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    max_records: int | None = None,
    max_contexts: int | None = None,
    symbols: Sequence[str] | None = None,
    max_live_calls: int | None = None,
    max_incremental_cost_usd: float | None = None,
) -> TridentAILLMReplayResult:
    return TridentAILLMReplayRunner(
        config=config,
        cache_dir=cache_dir,
        allow_live_llm_calls=allow_live_llm_calls,
    ).run(
        input_path,
        journal_path=journal_path,
        report_json_path=report_json_path,
        report_md_path=report_md_path,
        max_records=max_records,
        max_contexts=max_contexts,
        symbols=symbols,
        max_live_calls=max_live_calls,
        max_incremental_cost_usd=max_incremental_cost_usd,
    )


def build_trade_proposal_request(
    *,
    context: AgentMarketContext,
    config: TridentAIConfig,
    now: datetime,
    prompt_version: str = TRIDENT_AI_REPLAY_PROMPT_VERSION,
) -> LLMRequest:
    user_payload = {
        "task": "Return one TRIDENT-AI trade proposal for this market context.",
        "rules": {
            "json_only": True,
            "allowed_symbols": list(config.tradable_symbols),
            "allowed_actions": ["hold", "open", "close", "reduce", "close_only_mode"],
            "max_notional_usd": config.risk.live_max_order_notional_usd,
            "max_leverage": config.risk.max_leverage,
            "min_confidence": config.risk.min_confidence,
            "require_stop_for_open": config.risk.require_stop,
            "require_evidence": config.risk.require_evidence,
            "valid_for_seconds": config.risk.max_proposal_age_seconds,
        },
        "output_contract": {
            "hold": {
                "entry_style": "none",
                "max_notional_usd": 0.0,
                "max_leverage": 0.0,
                "invalidation_price": 0.0,
                "stop_bps": 0.0,
                "take_profit_bps": 0.0,
                "time_stop_minutes": 0,
                "confidence": "Use at least min_confidence unless the JSON schema forces otherwise.",
                "evidence_ids": "Include the market context_id and any decisive feature ids.",
            },
            "open": {
                "confidence": "Must be >= min_confidence.",
                "max_notional_usd": "Must be > 0 and <= max_notional_usd.",
                "max_leverage": "Must be > 0 and <= max_leverage.",
                "stop_bps": "Must be > 0.",
                "take_profit_bps": "Must be > stop_bps.",
                "time_stop_minutes": "Must be > 0.",
            },
            "numeric_format": "Use ordinary decimals. Do not use scientific notation or subnormal floats.",
        },
        "now": _format_timestamp(now),
        "market_context": context.to_dict(),
    }
    return LLMRequest(
        request_id=f"trident_ai_llm_replay_{_timestamp_id(now)}_{context.symbol}",
        model=config.llm.model,
        system_prompt=_system_prompt(prompt_version),
        user_prompt=json.dumps(user_payload, sort_keys=True, separators=(",", ":")),
        schema_name="trident_ai_trade_proposal",
        schema=agent_trade_proposal_json_schema(allowed_symbols=config.tradable_symbols),
        temperature=config.llm.temperature,
        metadata={
            "component": "trident_ai_llm_replay",
            "prompt_version": prompt_version,
            "symbol": context.symbol,
            "context_id": context.context_id,
        },
    )


def build_llm_replay_report_payload(
    *,
    result: TridentAILLMReplayResult,
    config: TridentAIConfig,
    generated_at: str,
) -> dict[str, object]:
    accepted_open_count = int(result.action_counts.get("open", 0))
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_llm_replay",
        "result": result.to_dict(),
        "config": {
            "mode": config.mode,
            "tradable_symbols": list(config.tradable_symbols),
            "risk": {
                "max_notional_usd": config.risk.live_max_order_notional_usd,
                "max_leverage": config.risk.max_leverage,
                "max_daily_loss_usd": config.risk.max_daily_loss_usd,
                "max_open_positions": config.risk.max_open_positions,
                "max_trades_per_day": config.risk.max_trades_per_day,
            },
        },
        "comparison": {
            "hold_baseline": {
                "accepted_open_proposals": 0,
                "model_cost_usd": 0.0,
            },
            "trident_ai_llm": {
                "accepted_open_proposals": accepted_open_count,
                "accepted_total": result.proposals_accepted,
                "rejected_total": result.proposals_rejected,
                "llm_failures": result.llm_failures,
                "incremental_cost_usd": round(result.incremental_cost_usd, 8),
                "estimated_original_cost_usd": round(
                    result.estimated_original_cost_usd,
                    8,
                ),
            },
            "full_bot_baseline": {
                "status": "reference_only_not_run_in_step_6",
                "references": list(FULL_BOT_BASELINE_REFERENCES),
                "note": (
                    "Step 6 validates replay reproducibility and LLM proposal quality. "
                    "PnL comparison is added once dry-run/paper execution exists."
                ),
            },
        },
    }


def _write_report_outputs(
    payload: dict[str, object],
    *,
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_markdown_report(payload), encoding="utf-8")


def _render_markdown_report(payload: dict[str, object]) -> str:
    result = payload["result"]
    comparison = payload["comparison"]
    assert isinstance(result, dict)
    assert isinstance(comparison, dict)
    trident_ai = comparison["trident_ai_llm"]
    full_bot = comparison["full_bot_baseline"]
    assert isinstance(trident_ai, dict)
    assert isinstance(full_bot, dict)
    lines = [
        "# TRIDENT-AI LLM Replay",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Input: `{result['input_path']}`",
        f"- Provider/model: `{result['provider']}` / `{result['model']}`",
        f"- Prompt version: `{result['prompt_version']}`",
        f"- Cache required: `{result['cache_required']}`",
        f"- Live LLM calls allowed: `{result['allow_live_llm_calls']}`",
        f"- Cache hits: `{result['cache_hits']}` / `{result['llm_requests']}`",
        f"- Live calls: `{result['live_llm_calls']}`",
        f"- Max records: `{result['max_records']}`",
        f"- Max contexts: `{result['max_contexts']}`",
        f"- Symbols filter: `{result['symbols_filter']}`",
        f"- Limit reached: `{result['limit_reached']}`",
        f"- Max live calls: `{result['max_live_calls']}`",
        f"- Max incremental cost: `{result['max_incremental_cost_usd']}`",
        f"- Incremental cost: `${result['incremental_cost_usd']:.6f}`",
        "",
        "## Resultats",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Records processed | {result['records_processed']} |",
        f"| Contexts built | {result['contexts_built']} |",
        f"| Context rejections | {result['context_rejections']} |",
        f"| LLM failures | {result['llm_failures']} |",
        f"| Proposals accepted | {result['proposals_accepted']} |",
        f"| Proposals rejected | {result['proposals_rejected']} |",
        f"| Estimated original model cost | ${result['estimated_original_cost_usd']:.6f} |",
        "",
        "## Comparaison",
        "",
        f"- Hold baseline accepted open proposals: `{comparison['hold_baseline']['accepted_open_proposals']}`",
        f"- TRIDENT-AI accepted open proposals: `{trident_ai['accepted_open_proposals']}`",
        f"- Full-bot baseline status: `{full_bot['status']}`",
        "",
        "References baseline full-bot:",
    ]
    for reference in full_bot["references"]:
        lines.append(f"- `{reference}`")
    lines.append("")
    return "\n".join(lines)


def _decision_record(
    *,
    record: SnapshotRecord,
    timestamp: str,
    context: AgentMarketContext,
    request: LLMRequest,
    cache_key: str,
    response: LLMResponse,
    proposal_payload: dict[str, object] | None,
    accepted: bool,
    reason: str,
    mode: str,
) -> dict[str, object]:
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "source": "trident_ai_llm_replay",
        "record_index": record.record_index,
        "source_file": record.source_file,
        "timestamp": timestamp,
        "symbol": context.symbol,
        "mode": mode,
        "request": {
            "request_id": request.request_id,
            "provider": response.provider,
            "model": request.model,
            "prompt_version": request.metadata.get("prompt_version", ""),
            "cache_key": cache_key,
        },
        "context": context.to_dict(),
        "llm_response": response.to_dict(),
        "proposal": proposal_payload,
        "validation": {
            "accepted": accepted,
            "reason": reason,
        },
    }


def _context_rejection_record(
    *,
    record: SnapshotRecord,
    timestamp: str,
    symbol: str,
    reason: str,
) -> dict[str, object]:
    return {
        "event_type": LLM_REPLAY_CONTEXT_REJECTED_EVENT,
        "source": "trident_ai_llm_replay",
        "record_index": record.record_index,
        "source_file": record.source_file,
        "timestamp": timestamp,
        "symbol": symbol,
        "reason": reason,
    }


def _accumulate_usage(
    counters: _ReplayCounters,
    usage: LLMUsage,
    *,
    incremental: bool,
) -> None:
    counters.input_tokens += usage.input_tokens
    counters.output_tokens += usage.output_tokens
    if usage.estimated_cost_usd is not None:
        counters.estimated_original_cost_usd += usage.estimated_cost_usd
        if incremental:
            counters.incremental_cost_usd += usage.estimated_cost_usd


def _system_prompt(prompt_version: str) -> str:
    return (
        "You are TRIDENT-AI in replay mode. "
        f"Prompt version: {prompt_version}. "
        "Return exactly one JSON object matching the provided schema. "
        "Do not include prose, markdown, tool calls, secrets, or exchange actions. "
        "If the setup is not clearly valid, return action=hold with ordinary zero values "
        "for notional, leverage, stop, take-profit, invalidation and time-stop. "
        "Do not use scientific notation or tiny subnormal numbers."
    )


def _record_regime(record: SnapshotRecord) -> str:
    for field_name in ("regime", "effective_regime", "regime_label"):
        value = record.regime_snapshot.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _filter_symbol_payloads(
    payloads: Sequence[dict[str, object]],
    symbols_filter: tuple[str, ...],
) -> list[dict[str, object]]:
    if not symbols_filter:
        return list(payloads)
    allowed = set(symbols_filter)
    return [
        payload
        for payload in payloads
        if str(payload.get("symbol", "")).strip().upper() in allowed
    ]


def _symbols_filter(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if symbols is None:
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        value = str(symbol).strip().upper()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


def _positive_optional_int(value: int | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TridentAILLMReplayError(f"{field_name}_must_be_positive")
    return value


def _response_counts_as_live_provider_call(response: LLMResponse) -> bool:
    return response.error not in {"missing_api_key", "unsupported_provider"}


def _positive_optional_float(value: float | None, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0.0:
        raise TridentAILLMReplayError(f"{field_name}_must_be_positive")
    return float(value)


def _validate_live_call_caps(
    *,
    allow_live_llm_calls: bool,
    max_live_calls: int | None,
    max_incremental_cost_usd: float | None,
) -> None:
    if not allow_live_llm_calls:
        return
    if max_live_calls is None:
        raise TridentAILLMReplayError("max_live_calls_required")
    if max_incremental_cost_usd is None:
        raise TridentAILLMReplayError("max_incremental_cost_usd_required")


def _request_cost_reserve_usd(request: LLMRequest) -> float:
    input_tokens = max(
        int((len(request.system_prompt) + len(request.user_prompt)) / 4),
        1,
    )
    estimated = estimate_token_cost_usd(
        model=request.model,
        input_tokens=input_tokens,
        output_tokens=LIVE_CALL_COST_RESERVE_OUTPUT_TOKENS,
    )
    return estimated if estimated is not None else 0.01


def _record_datetime(record: SnapshotRecord) -> datetime:
    if record.timestamp:
        parsed = _parse_timestamp(record.timestamp)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
