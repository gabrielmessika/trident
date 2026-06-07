from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from app.backtest.snapshot_loader import SnapshotLoader, SnapshotRecord
from app.live.runtime_status import write_runtime_status
from app.persistence.journal import JsonlJournal
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.features import AgentMarketContextBuildConfig, TridentAIFeatureBuilder
from app.trident_ai.types import (
    AgentMarketContext,
    AgentTradeProposal,
    TRIDENT_AI_PROPOSAL_SCHEMA_VERSION,
    validate_agent_proposal,
)


SHADOW_DECISION_EVENT = "trident_ai_shadow_decision"
SHADOW_CONTEXT_REJECTED_EVENT = "trident_ai_shadow_context_rejected"


class ShadowAgent(Protocol):
    name: str

    def decide(
        self,
        context: AgentMarketContext,
        *,
        config: TridentAIConfig,
        now: datetime,
    ) -> AgentTradeProposal:
        ...


@dataclass(frozen=True, slots=True)
class TridentAIShadowRunResult:
    input_path: str
    journal_path: str
    status_path: str
    records_processed: int = 0
    contexts_built: int = 0
    context_rejections: int = 0
    proposals_generated: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    last_timestamp: str | None = None
    max_records: int | None = None
    max_contexts: int | None = None
    symbols_filter: tuple[str, ...] = ()
    limit_reached: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "journal_path": self.journal_path,
            "status_path": self.status_path,
            "records_processed": self.records_processed,
            "contexts_built": self.contexts_built,
            "context_rejections": self.context_rejections,
            "proposals_generated": self.proposals_generated,
            "proposals_accepted": self.proposals_accepted,
            "proposals_rejected": self.proposals_rejected,
            "last_timestamp": self.last_timestamp,
            "max_records": self.max_records,
            "max_contexts": self.max_contexts,
            "symbols_filter": list(self.symbols_filter),
            "limit_reached": self.limit_reached,
        }


class DeterministicShadowAgent:
    """Offline agent used to exercise the pipeline before any LLM call."""

    name = "deterministic_shadow_v1"

    def decide(
        self,
        context: AgentMarketContext,
        *,
        config: TridentAIConfig,
        now: datetime,
    ) -> AgentTradeProposal:
        should_open = _context_supports_long_shadow_open(context)
        action = "open" if should_open else "hold"
        stop_bps = 85.0 if should_open else 0.0
        take_profit_bps = 160.0 if should_open else 0.0
        invalidation_price = (
            context.price * (1.0 - stop_bps / 10_000.0)
            if should_open
            else context.price
        )
        as_of = _format_timestamp(now)
        valid_until = _format_timestamp(
            now + timedelta(seconds=config.risk.max_proposal_age_seconds)
        )
        return AgentTradeProposal(
            schema_version=TRIDENT_AI_PROPOSAL_SCHEMA_VERSION,
            decision_id=f"trident_ai_shadow_{_timestamp_id(now)}_{context.symbol}",
            as_of=as_of,
            valid_until=valid_until,
            action=action,
            symbol=context.symbol,
            side="long",
            confidence=max(config.risk.min_confidence, 0.58 if should_open else 0.55),
            time_horizon_minutes=120 if should_open else 1,
            max_notional_usd=min(config.risk.live_max_order_notional_usd, 25.0),
            max_leverage=min(config.risk.max_leverage, 1.0),
            entry_style="ioc" if should_open else "none",
            invalidation_price=round(invalidation_price, 8),
            stop_bps=stop_bps,
            take_profit_bps=take_profit_bps,
            time_stop_minutes=180 if should_open else 1,
            rationale_tags=_rationale_tags(context, should_open=should_open),
            evidence_ids=[context.context_id],
            risk_notes=["shadow_only", "no_execution"],
        )


class TridentAIShadowRunner:
    def __init__(
        self,
        *,
        config: TridentAIConfig | None = None,
        agent: ShadowAgent | None = None,
        loader: SnapshotLoader | None = None,
        feature_builder: TridentAIFeatureBuilder | None = None,
    ) -> None:
        self.config = config or load_trident_ai_config()
        self.agent = agent or DeterministicShadowAgent()
        self.loader = loader or SnapshotLoader()
        self.feature_builder = feature_builder or TridentAIFeatureBuilder(
            AgentMarketContextBuildConfig.from_trident_ai_config(self.config)
        )

    def run(
        self,
        input_path: str | Path,
        *,
        journal_path: str | Path | None = None,
        status_path: str | Path | None = None,
        truncate_journal: bool = True,
        max_records: int | None = None,
        max_contexts: int | None = None,
        symbols: Sequence[str] | None = None,
    ) -> TridentAIShadowRunResult:
        max_records = _positive_optional_int(max_records, field_name="max_records")
        max_contexts = _positive_optional_int(max_contexts, field_name="max_contexts")
        symbols_filter = _symbols_filter(symbols)
        journal_output = Path(journal_path or self.config.paths.shadow_journal_path)
        status_output = Path(status_path or self.config.paths.status_path)
        journal = JsonlJournal(journal_output, truncate=truncate_journal)

        records_processed = 0
        contexts_built = 0
        context_rejections = 0
        proposals_generated = 0
        proposals_accepted = 0
        proposals_rejected = 0
        last_timestamp: str | None = None
        limit_reached = False

        for record in self.loader.iter_merged_jsonl(input_path):
            if max_records is not None and records_processed >= max_records:
                limit_reached = True
                break
            records_processed += 1
            record_now = _record_datetime(record)
            timestamp = _format_timestamp(record_now)
            last_timestamp = timestamp
            regime = _record_regime(record)

            for build_result in self.feature_builder.build_contexts_from_mappings(
                _filter_symbol_payloads(record.symbols, symbols_filter),
                as_of=timestamp,
                regime=regime,
                now=record_now,
            ):
                if build_result.context is None:
                    context_rejections += 1
                    journal.append(
                        _context_rejection_record(
                            record=record,
                            timestamp=timestamp,
                            symbol=build_result.symbol,
                            reason=build_result.reason,
                        )
                    )
                    continue

                if max_contexts is not None and contexts_built >= max_contexts:
                    limit_reached = True
                    break
                contexts_built += 1
                proposal = self.agent.decide(
                    build_result.context,
                    config=self.config,
                    now=record_now,
                )
                proposals_generated += 1
                validation = validate_agent_proposal(
                    proposal,
                    market_context=build_result.context,
                    config=self.config.proposal_validation_config(),
                    now=record_now,
                )
                if validation.accepted:
                    proposals_accepted += 1
                else:
                    proposals_rejected += 1
                journal.append(
                    _decision_record(
                        record=record,
                        timestamp=timestamp,
                        context=build_result.context,
                        proposal=proposal,
                        accepted=validation.accepted,
                        reason=validation.reason,
                        agent_name=self.agent.name,
                        mode=self.config.mode,
                    )
                )
            if limit_reached:
                break

        result = TridentAIShadowRunResult(
            input_path=str(input_path),
            journal_path=str(journal_output),
            status_path=str(status_output),
            records_processed=records_processed,
            contexts_built=contexts_built,
            context_rejections=context_rejections,
            proposals_generated=proposals_generated,
            proposals_accepted=proposals_accepted,
            proposals_rejected=proposals_rejected,
            last_timestamp=last_timestamp,
            max_records=max_records,
            max_contexts=max_contexts,
            symbols_filter=symbols_filter,
            limit_reached=limit_reached,
        )
        write_runtime_status(status_output, _status_payload(result, self.config, self.agent.name))
        return result


def run_trident_ai_shadow(
    input_path: str | Path,
    *,
    config: TridentAIConfig | None = None,
    journal_path: str | Path | None = None,
    status_path: str | Path | None = None,
    truncate_journal: bool = True,
    max_records: int | None = None,
    max_contexts: int | None = None,
    symbols: Sequence[str] | None = None,
) -> TridentAIShadowRunResult:
    return TridentAIShadowRunner(config=config).run(
        input_path,
        journal_path=journal_path,
        status_path=status_path,
        truncate_journal=truncate_journal,
        max_records=max_records,
        max_contexts=max_contexts,
        symbols=symbols,
    )


def _context_supports_long_shadow_open(context: AgentMarketContext) -> bool:
    features = context.features
    return (
        features.get("ema_alignment") == "bullish"
        and features.get("btc_aligned") is True
        and _float_feature(features.get("spread_bps")) <= 3.0
        and _float_feature(features.get("structure_score")) >= 0.40
        and _float_feature(features.get("trade_flow_bias")) >= 0.05
    )


def _rationale_tags(context: AgentMarketContext, *, should_open: bool) -> list[str]:
    if not should_open:
        return ["shadow_hold", "criteria_not_met"]
    tags = ["shadow_open_candidate", "ema_bullish", "btc_aligned"]
    if _float_feature(context.features.get("trade_flow_bias")) >= 0.15:
        tags.append("trade_flow_positive")
    return tags


def _decision_record(
    *,
    record: SnapshotRecord,
    timestamp: str,
    context: AgentMarketContext,
    proposal: AgentTradeProposal,
    accepted: bool,
    reason: str,
    agent_name: str,
    mode: str,
) -> dict[str, object]:
    return {
        "event_type": SHADOW_DECISION_EVENT,
        "source": "trident_ai_shadow",
        "record_index": record.record_index,
        "source_file": record.source_file,
        "timestamp": timestamp,
        "symbol": context.symbol,
        "mode": mode,
        "agent": {"name": agent_name},
        "context": context.to_dict(),
        "proposal": proposal.to_dict(),
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
        "event_type": SHADOW_CONTEXT_REJECTED_EVENT,
        "source": "trident_ai_shadow",
        "record_index": record.record_index,
        "source_file": record.source_file,
        "timestamp": timestamp,
        "symbol": symbol,
        "reason": reason,
    }


def _status_payload(
    result: TridentAIShadowRunResult,
    config: TridentAIConfig,
    agent_name: str,
) -> dict[str, object]:
    return {
        "pod": "trident_ai",
        "mode": config.mode,
        "enabled": config.enabled,
        "healthy": True,
        "updated_at": _format_timestamp(datetime.now(timezone.utc)),
        "poll_seconds": config.decision_interval_seconds,
        "agent": {"name": agent_name},
        "tradable_symbols": list(config.tradable_symbols),
        "risk": {
            "max_notional_usd": config.risk.live_max_order_notional_usd,
            "max_leverage": config.risk.max_leverage,
            "max_daily_loss_usd": config.risk.max_daily_loss_usd,
            "max_open_positions": config.risk.max_open_positions,
            "max_trades_per_day": config.risk.max_trades_per_day,
        },
        "shadow": result.to_dict(),
    }


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
        raise ValueError(f"{field_name} must be a positive integer")
    return value


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


def _float_feature(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)
