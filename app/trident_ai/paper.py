from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader
from app.persistence.journal import JsonlJournal
from app.trident_ai.config import (
    TridentAIConfig,
    TridentAIPaperConfig,
    load_trident_ai_config,
)
from app.trident_ai.features import AgentMarketContextBuildConfig, TridentAIFeatureBuilder
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT
from app.trident_ai.types import AgentMarketContext, AgentTradeProposal


PAPER_REPLAY_DECISION_EVENT = "trident_ai_paper_replay_decision"
PAPER_REPLAY_FILL_EVENT = "trident_ai_paper_replay_fill"
PAPER_REPLAY_TRADE_CLOSED_EVENT = "trident_ai_paper_replay_trade_closed"


@dataclass(frozen=True, slots=True)
class TridentAIPaperFill:
    symbol: str
    side: str
    action: str
    price: float
    notional_usd: float
    fee_usd: float
    slippage_bps: float
    timestamp: str
    decision_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "action": self.action,
            "price": self.price,
            "notional_usd": self.notional_usd,
            "fee_usd": self.fee_usd,
            "slippage_bps": self.slippage_bps,
            "timestamp": self.timestamp,
            "decision_id": self.decision_id,
        }


@dataclass(slots=True)
class TridentAIPaperPosition:
    symbol: str
    side: str
    decision_id: str
    opened_at: str
    entry_price: float
    current_notional_usd: float
    entry_fee_usd: float
    confidence: float
    max_leverage: float
    invalidation_price: float
    stop_bps: float
    take_profit_bps: float
    time_stop_minutes: int
    best_price_seen: float

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "decision_id": self.decision_id,
            "opened_at": self.opened_at,
            "entry_price": self.entry_price,
            "current_notional_usd": self.current_notional_usd,
            "entry_fee_usd": self.entry_fee_usd,
            "confidence": self.confidence,
            "max_leverage": self.max_leverage,
            "invalidation_price": self.invalidation_price,
            "stop_bps": self.stop_bps,
            "take_profit_bps": self.take_profit_bps,
            "time_stop_minutes": self.time_stop_minutes,
            "best_price_seen": self.best_price_seen,
        }


@dataclass(frozen=True, slots=True)
class TridentAIPaperClosedTrade:
    symbol: str
    side: str
    decision_id: str
    opened_at: str
    closed_at: str
    entry_price: float
    exit_price: float
    notional_usd: float
    gross_pnl_usd: float
    fees_usd: float
    pnl_usd: float
    close_reason: str
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "decision_id": self.decision_id,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "notional_usd": self.notional_usd,
            "gross_pnl_usd": self.gross_pnl_usd,
            "fees_usd": self.fees_usd,
            "pnl_usd": self.pnl_usd,
            "close_reason": self.close_reason,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class TridentAIPaperReplayResult:
    input_path: str
    journal_path: str
    report_json_path: str
    report_md_path: str
    market_input_path: str = ""
    symbols_filter: tuple[str, ...] = ()
    decisions_seen: int = 0
    market_contexts_seen: int = 0
    market_exit_checks: int = 0
    proposals_seen: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    fills: int = 0
    positions_opened: int = 0
    positions_reduced: int = 0
    positions_closed: int = 0
    open_positions: int = 0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    ai_cost_usd: float = 0.0
    net_after_ai_cost_usd: float = 0.0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    action_counts: dict[str, int] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    close_reasons: dict[str, int] = field(default_factory=dict)
    confidence_buckets: dict[str, dict[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "journal_path": self.journal_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "market_input_path": self.market_input_path,
            "symbols_filter": list(self.symbols_filter),
            "decisions_seen": self.decisions_seen,
            "market_contexts_seen": self.market_contexts_seen,
            "market_exit_checks": self.market_exit_checks,
            "proposals_seen": self.proposals_seen,
            "proposals_accepted": self.proposals_accepted,
            "proposals_rejected": self.proposals_rejected,
            "fills": self.fills,
            "positions_opened": self.positions_opened,
            "positions_reduced": self.positions_reduced,
            "positions_closed": self.positions_closed,
            "open_positions": self.open_positions,
            "realized_pnl_usd": round(self.realized_pnl_usd, 6),
            "unrealized_pnl_usd": round(self.unrealized_pnl_usd, 6),
            "gross_pnl_usd": round(self.gross_pnl_usd, 6),
            "fees_usd": round(self.fees_usd, 6),
            "ai_cost_usd": round(self.ai_cost_usd, 8),
            "net_after_ai_cost_usd": round(self.net_after_ai_cost_usd, 8),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "action_counts": dict(sorted(self.action_counts.items())),
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
            "close_reasons": dict(sorted(self.close_reasons.items())),
            "confidence_buckets": self.confidence_buckets,
        }


@dataclass(slots=True)
class _PaperCounters:
    decisions_seen: int = 0
    market_contexts_seen: int = 0
    market_exit_checks: int = 0
    proposals_seen: int = 0
    proposals_accepted: int = 0
    proposals_rejected: int = 0
    fills: int = 0
    positions_opened: int = 0
    positions_reduced: int = 0
    positions_closed: int = 0
    ai_cost_usd: float = 0.0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    action_counts: Counter[str] = field(default_factory=Counter)
    skip_reasons: Counter[str] = field(default_factory=Counter)
    close_reasons: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True, slots=True)
class _PaperReplayEvent:
    timestamp: datetime
    priority: int
    sequence: int
    kind: str
    decision_record: dict[str, object] | None = None
    market_context: AgentMarketContext | None = None


class TridentAIPaperExecutionVenue:
    def __init__(self, config: TridentAIPaperConfig) -> None:
        self.config = config

    def fill(
        self,
        *,
        symbol: str,
        side: str,
        action: str,
        mid_price: float,
        spread_bps: float,
        notional_usd: float,
        timestamp: str,
        decision_id: str,
    ) -> TridentAIPaperFill:
        impact_bps = max(spread_bps, 0.0) * self.config.spread_multiplier
        impact_bps += self.config.slippage_bps
        signed_impact = _signed_impact_bps(action=action, side=side, impact_bps=impact_bps)
        price = round(mid_price * (1 + signed_impact / 10_000.0), 8)
        fee_usd = round(notional_usd * self.config.taker_fee_bps / 10_000.0, 6)
        return TridentAIPaperFill(
            symbol=symbol,
            side=side,
            action=action,
            price=price,
            notional_usd=round(notional_usd, 6),
            fee_usd=fee_usd,
            slippage_bps=round(impact_bps, 4),
            timestamp=timestamp,
            decision_id=decision_id,
        )


class TridentAIPaperPortfolio:
    def __init__(self) -> None:
        self.open_positions: dict[str, TridentAIPaperPosition] = {}
        self.closed_trades: list[TridentAIPaperClosedTrade] = []
        self.realized_pnl_usd = 0.0
        self.gross_pnl_usd = 0.0
        self.fees_usd = 0.0

    def open_position(
        self,
        proposal: AgentTradeProposal,
        fill: TridentAIPaperFill,
    ) -> bool:
        if proposal.symbol in self.open_positions:
            return False
        self.open_positions[proposal.symbol] = TridentAIPaperPosition(
            symbol=proposal.symbol,
            side=proposal.side,
            decision_id=proposal.decision_id,
            opened_at=fill.timestamp,
            entry_price=fill.price,
            current_notional_usd=fill.notional_usd,
            entry_fee_usd=fill.fee_usd,
            confidence=proposal.confidence,
            max_leverage=proposal.max_leverage,
            invalidation_price=proposal.invalidation_price,
            stop_bps=proposal.stop_bps,
            take_profit_bps=proposal.take_profit_bps,
            time_stop_minutes=proposal.time_stop_minutes,
            best_price_seen=fill.price,
        )
        return True

    def reduce_position(
        self,
        symbol: str,
        fill: TridentAIPaperFill,
        *,
        reason: str,
    ) -> TridentAIPaperClosedTrade | None:
        position = self.open_positions.get(symbol)
        if position is None:
            return None
        close_notional = min(fill.notional_usd, position.current_notional_usd)
        if close_notional <= 0:
            return None
        entry_fee_share = round(
            position.entry_fee_usd * close_notional / position.current_notional_usd,
            6,
        )
        trade = self._closed_trade(
            position=position,
            fill=fill,
            close_notional=close_notional,
            entry_fee_usd=entry_fee_share,
            reason=reason,
        )
        remaining = round(position.current_notional_usd - close_notional, 6)
        if remaining <= 0.000001:
            self.open_positions.pop(symbol, None)
        else:
            position.current_notional_usd = remaining
            position.entry_fee_usd = round(position.entry_fee_usd - entry_fee_share, 6)
        self._record_closed_trade(trade)
        return trade

    def close_position(
        self,
        symbol: str,
        fill: TridentAIPaperFill,
        *,
        reason: str,
    ) -> TridentAIPaperClosedTrade | None:
        position = self.open_positions.pop(symbol, None)
        if position is None:
            return None
        trade = self._closed_trade(
            position=position,
            fill=fill,
            close_notional=position.current_notional_usd,
            entry_fee_usd=position.entry_fee_usd,
            reason=reason,
        )
        self._record_closed_trade(trade)
        return trade

    def protective_exit_reason(
        self,
        symbol: str,
        *,
        price: float,
        timestamp: str,
    ) -> str | None:
        position = self.open_positions.get(symbol)
        if position is None:
            return None
        self._update_best_price(position, price)
        if position.invalidation_price > 0:
            if position.side == "long" and price <= position.invalidation_price:
                return "invalidation_price_hit"
            if position.side == "short" and price >= position.invalidation_price:
                return "invalidation_price_hit"
        if position.stop_bps > 0 and self._stop_hit(position, price):
            return "stop_hit"
        if position.take_profit_bps > 0 and self._favorable_move_bps(position, price) >= position.take_profit_bps:
            return "take_profit_hit"
        if _time_stop_hit(position.opened_at, timestamp, position.time_stop_minutes):
            return "time_stop"
        return None

    def unrealized_pnl_usd(self, last_prices: Mapping[str, float]) -> float:
        total = 0.0
        for position in self.open_positions.values():
            price = float(last_prices.get(position.symbol, 0.0) or 0.0)
            if price <= 0:
                continue
            total += _gross_pnl_usd(
                side=position.side,
                entry_price=position.entry_price,
                exit_price=price,
                notional_usd=position.current_notional_usd,
            )
        return round(total, 6)

    def _closed_trade(
        self,
        *,
        position: TridentAIPaperPosition,
        fill: TridentAIPaperFill,
        close_notional: float,
        entry_fee_usd: float,
        reason: str,
    ) -> TridentAIPaperClosedTrade:
        gross_pnl = _gross_pnl_usd(
            side=position.side,
            entry_price=position.entry_price,
            exit_price=fill.price,
            notional_usd=close_notional,
        )
        fees = round(entry_fee_usd + fill.fee_usd, 6)
        return TridentAIPaperClosedTrade(
            symbol=position.symbol,
            side=position.side,
            decision_id=position.decision_id,
            opened_at=position.opened_at,
            closed_at=fill.timestamp,
            entry_price=position.entry_price,
            exit_price=fill.price,
            notional_usd=round(close_notional, 6),
            gross_pnl_usd=gross_pnl,
            fees_usd=fees,
            pnl_usd=round(gross_pnl - fees, 6),
            close_reason=reason,
            confidence=position.confidence,
        )

    def _record_closed_trade(self, trade: TridentAIPaperClosedTrade) -> None:
        self.closed_trades.append(trade)
        self.realized_pnl_usd = round(self.realized_pnl_usd + trade.pnl_usd, 6)
        self.gross_pnl_usd = round(self.gross_pnl_usd + trade.gross_pnl_usd, 6)
        self.fees_usd = round(self.fees_usd + trade.fees_usd, 6)

    def _stop_hit(self, position: TridentAIPaperPosition, price: float) -> bool:
        threshold = position.stop_bps / 10_000.0
        if position.side == "long":
            return price <= position.entry_price * (1 - threshold)
        return price >= position.entry_price * (1 + threshold)

    def _update_best_price(self, position: TridentAIPaperPosition, price: float) -> None:
        if position.side == "long":
            position.best_price_seen = max(position.best_price_seen, price)
        else:
            position.best_price_seen = min(position.best_price_seen, price)

    def _favorable_move_bps(self, position: TridentAIPaperPosition, price: float) -> float:
        if position.entry_price <= 0:
            return 0.0
        if position.side == "long":
            return ((price - position.entry_price) / position.entry_price) * 10_000.0
        return ((position.entry_price - price) / position.entry_price) * 10_000.0


class TridentAIPaperReplayRunner:
    def __init__(
        self,
        *,
        config: TridentAIConfig | None = None,
        loader: SnapshotLoader | None = None,
        feature_builder: TridentAIFeatureBuilder | None = None,
    ) -> None:
        self.config = config or load_trident_ai_config()
        self.venue = TridentAIPaperExecutionVenue(self.config.paper)
        self.portfolio = TridentAIPaperPortfolio()
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
        max_decisions: int | None = None,
        market_input_path: str | Path | None = None,
        symbols: Sequence[str] | None = None,
        market_event_cache: Sequence[_PaperReplayEvent] | None = None,
    ) -> TridentAIPaperReplayResult:
        if max_decisions is not None and max_decisions <= 0:
            raise ValueError("max_decisions_must_be_positive")
        symbols_filter = _symbols_filter(symbols)
        run_id = _timestamp_id(datetime.now(timezone.utc))
        output_dir = Path(self.config.paths.replay_output_dir)
        journal_output = Path(journal_path or output_dir / f"trident_ai_paper_replay_{run_id}.jsonl")
        report_json_output = Path(
            report_json_path or output_dir / f"trident_ai_paper_replay_{run_id}.json"
        )
        report_md_output = Path(
            report_md_path or output_dir / f"trident_ai_paper_replay_{run_id}.md"
        )
        journal = JsonlJournal(journal_output, truncate=truncate_journal)
        counters = _PaperCounters()
        opens_by_day: dict[str, int] = defaultdict(int)
        last_prices: dict[str, float] = {}
        last_spreads: dict[str, float] = {}

        events = self._build_replay_events(
            input_path=input_path,
            max_decisions=max_decisions,
            market_input_path=market_input_path,
            symbols_filter=symbols_filter,
            market_event_cache=market_event_cache,
        )
        for event in events:
            if event.kind == "decision" and event.decision_record is not None:
                self._process_decision_event(
                    event.decision_record,
                    journal=journal,
                    counters=counters,
                    opens_by_day=opens_by_day,
                    last_prices=last_prices,
                    last_spreads=last_spreads,
                )
                continue
            if event.kind == "market" and event.market_context is not None:
                self._process_market_event(
                    event.market_context,
                    journal=journal,
                    counters=counters,
                    last_prices=last_prices,
                    last_spreads=last_spreads,
                )

        if self.config.paper.force_close_at_end:
            self._force_close_open_positions(
                timestamp=counters.last_timestamp or "",
                last_prices=last_prices,
                last_spreads=last_spreads,
                journal=journal,
                counters=counters,
            )

        unrealized = self.portfolio.unrealized_pnl_usd(last_prices)
        open_entry_fees = _open_position_entry_fees(self.portfolio)
        fees_usd = round(self.portfolio.fees_usd + open_entry_fees, 6)
        net_after_ai_cost = round(
            self.portfolio.realized_pnl_usd + unrealized - open_entry_fees - counters.ai_cost_usd,
            8,
        )
        result = TridentAIPaperReplayResult(
            input_path=str(input_path),
            journal_path=str(journal_output),
            report_json_path=str(report_json_output),
            report_md_path=str(report_md_output),
            market_input_path=str(market_input_path or ""),
            symbols_filter=symbols_filter,
            decisions_seen=counters.decisions_seen,
            market_contexts_seen=counters.market_contexts_seen,
            market_exit_checks=counters.market_exit_checks,
            proposals_seen=counters.proposals_seen,
            proposals_accepted=counters.proposals_accepted,
            proposals_rejected=counters.proposals_rejected,
            fills=counters.fills,
            positions_opened=counters.positions_opened,
            positions_reduced=counters.positions_reduced,
            positions_closed=counters.positions_closed,
            open_positions=len(self.portfolio.open_positions),
            realized_pnl_usd=self.portfolio.realized_pnl_usd,
            unrealized_pnl_usd=unrealized,
            gross_pnl_usd=self.portfolio.gross_pnl_usd,
            fees_usd=fees_usd,
            ai_cost_usd=counters.ai_cost_usd,
            net_after_ai_cost_usd=net_after_ai_cost,
            first_timestamp=counters.first_timestamp,
            last_timestamp=counters.last_timestamp,
            action_counts=dict(counters.action_counts),
            skip_reasons=dict(counters.skip_reasons),
            close_reasons=dict(counters.close_reasons),
            confidence_buckets=_confidence_buckets(self.portfolio.closed_trades),
        )
        payload = build_paper_replay_report_payload(
            result=result,
            config=self.config,
            generated_at=_format_timestamp(datetime.now(timezone.utc)),
        )
        _write_report_outputs(payload, json_path=report_json_output, md_path=report_md_output)
        return result

    def build_market_event_cache(
        self,
        market_input_path: str | Path,
        *,
        min_timestamp: datetime,
        symbols: Sequence[str] | None = None,
    ) -> tuple[_PaperReplayEvent, ...]:
        return tuple(
            self._market_events_from_snapshots(
                market_input_path,
                min_timestamp=min_timestamp,
                start_sequence=0,
                symbols_filter=_symbols_filter(symbols),
            )
        )

    def _build_replay_events(
        self,
        *,
        input_path: str | Path,
        max_decisions: int | None,
        market_input_path: str | Path | None,
        symbols_filter: tuple[str, ...],
        market_event_cache: Sequence[_PaperReplayEvent] | None,
    ) -> list[_PaperReplayEvent]:
        decision_events = _decision_events_from_journal(
            input_path,
            max_decisions=max_decisions,
            symbols_filter=symbols_filter,
        )
        if market_input_path is None or not decision_events:
            return sorted(decision_events, key=_paper_event_sort_key)
        first_decision_at = min(event.timestamp for event in decision_events)
        if market_event_cache is not None:
            market_events = [
                event for event in market_event_cache if event.timestamp >= first_decision_at
            ]
        else:
            market_events = self._market_events_from_snapshots(
                market_input_path,
                min_timestamp=first_decision_at,
                start_sequence=len(decision_events),
                symbols_filter=(),
            )
        return sorted([*decision_events, *market_events], key=_paper_event_sort_key)

    def _market_events_from_snapshots(
        self,
        input_path: str | Path,
        *,
        min_timestamp: datetime,
        start_sequence: int,
        symbols_filter: tuple[str, ...],
    ) -> list[_PaperReplayEvent]:
        events: list[_PaperReplayEvent] = []
        sequence = start_sequence
        allowed = set(symbols_filter)
        for record in self.loader.iter_merged_jsonl(input_path):
            timestamp = _parse_timestamp(record.timestamp or "")
            if timestamp is None or timestamp < min_timestamp:
                continue
            regime = _record_regime(record)
            symbols_payload = record.symbols
            if allowed:
                symbols_payload = [
                    payload
                    for payload in record.symbols
                    if str(payload.get("symbol", "") or "").strip().upper() in allowed
                ]
            for build_result in self.feature_builder.build_contexts_from_mappings(
                symbols_payload,
                as_of=_format_timestamp(timestamp),
                regime=regime,
                now=timestamp,
            ):
                if build_result.context is None:
                    continue
                events.append(
                    _PaperReplayEvent(
                        timestamp=timestamp,
                        priority=1,
                        sequence=sequence,
                        kind="market",
                        market_context=build_result.context,
                    )
                )
                sequence += 1
        return events

    def _process_decision_event(
        self,
        record: Mapping[str, object],
        *,
        journal: JsonlJournal,
        counters: _PaperCounters,
        opens_by_day: dict[str, int],
        last_prices: dict[str, float],
        last_spreads: dict[str, float],
    ) -> None:
        counters.decisions_seen += 1
        timestamp = str(record.get("timestamp", "") or "")
        _mark_event_time(counters, timestamp)
        _accumulate_ai_cost(counters, record)

        context = _context_from_record(record)
        if context is None:
            counters.skip_reasons["invalid_context"] += 1
            return
        last_prices[context.symbol] = context.price
        last_spreads[context.symbol] = _spread_bps(context)
        self._process_protective_exit(
            context=context,
            timestamp=timestamp,
            journal=journal,
            counters=counters,
        )

        proposal = _proposal_from_record(record)
        accepted = _record_is_accepted(record)
        if proposal is None:
            counters.skip_reasons["missing_proposal"] += 1
            journal.append(_paper_decision_record(record, context, None, "skip", "missing_proposal"))
            return
        counters.proposals_seen += 1
        if not accepted:
            counters.proposals_rejected += 1
            reason = _validation_reason(record) or "proposal_rejected"
            counters.skip_reasons[reason] += 1
            journal.append(_paper_decision_record(record, context, proposal, "skip", reason))
            return
        counters.proposals_accepted += 1
        counters.action_counts[proposal.action] += 1
        paper_action, reason = self._apply_proposal(
            proposal=proposal,
            context=context,
            timestamp=timestamp,
            opens_by_day=opens_by_day,
            journal=journal,
            counters=counters,
        )
        journal.append(_paper_decision_record(record, context, proposal, paper_action, reason))

    def _process_market_event(
        self,
        context: AgentMarketContext,
        *,
        journal: JsonlJournal,
        counters: _PaperCounters,
        last_prices: dict[str, float],
        last_spreads: dict[str, float],
    ) -> None:
        counters.market_contexts_seen += 1
        _mark_event_time(counters, context.as_of)
        last_prices[context.symbol] = context.price
        last_spreads[context.symbol] = _spread_bps(context)
        if context.symbol not in self.portfolio.open_positions:
            return
        counters.market_exit_checks += 1
        self._process_protective_exit(
            context=context,
            timestamp=context.as_of,
            journal=journal,
            counters=counters,
        )

    def _apply_proposal(
        self,
        *,
        proposal: AgentTradeProposal,
        context: AgentMarketContext,
        timestamp: str,
        opens_by_day: dict[str, int],
        journal: JsonlJournal,
        counters: _PaperCounters,
    ) -> tuple[str, str]:
        if proposal.action in {"hold", "close_only_mode"}:
            return "no_op", proposal.action
        if proposal.action == "open":
            return self._open_position(
                proposal=proposal,
                context=context,
                timestamp=timestamp,
                opens_by_day=opens_by_day,
                journal=journal,
                counters=counters,
            )
        if proposal.action == "close":
            trade = self._close_position(
                symbol=proposal.symbol,
                timestamp=timestamp,
                price=context.price,
                spread_bps=_spread_bps(context),
                decision_id=proposal.decision_id,
                reason="agent_close",
                journal=journal,
                counters=counters,
            )
            return ("close", "agent_close") if trade is not None else ("skip", "no_open_position")
        if proposal.action == "reduce":
            return self._reduce_position(
                proposal=proposal,
                context=context,
                timestamp=timestamp,
                journal=journal,
                counters=counters,
            )
        counters.skip_reasons["unsupported_action"] += 1
        return "skip", "unsupported_action"

    def _open_position(
        self,
        *,
        proposal: AgentTradeProposal,
        context: AgentMarketContext,
        timestamp: str,
        opens_by_day: dict[str, int],
        journal: JsonlJournal,
        counters: _PaperCounters,
    ) -> tuple[str, str]:
        if proposal.symbol in self.portfolio.open_positions:
            counters.skip_reasons["position_already_open"] += 1
            return "skip", "position_already_open"
        if len(self.portfolio.open_positions) >= self.config.risk.max_open_positions:
            counters.skip_reasons["max_open_positions_reached"] += 1
            return "skip", "max_open_positions_reached"
        date_key = timestamp[:10]
        if opens_by_day[date_key] >= self.config.risk.max_trades_per_day:
            counters.skip_reasons["max_trades_per_day_reached"] += 1
            return "skip", "max_trades_per_day_reached"
        fill = self.venue.fill(
            symbol=proposal.symbol,
            side=proposal.side,
            action="open",
            mid_price=context.price,
            spread_bps=_spread_bps(context),
            notional_usd=proposal.max_notional_usd,
            timestamp=timestamp,
            decision_id=proposal.decision_id,
        )
        if not self.portfolio.open_position(proposal, fill):
            counters.skip_reasons["portfolio_open_rejected"] += 1
            return "skip", "portfolio_open_rejected"
        opens_by_day[date_key] += 1
        counters.fills += 1
        counters.positions_opened += 1
        journal.append(_fill_record(fill, reason="agent_open"))
        return "open", "agent_open"

    def _reduce_position(
        self,
        *,
        proposal: AgentTradeProposal,
        context: AgentMarketContext,
        timestamp: str,
        journal: JsonlJournal,
        counters: _PaperCounters,
    ) -> tuple[str, str]:
        position = self.portfolio.open_positions.get(proposal.symbol)
        if position is None:
            counters.skip_reasons["no_open_position"] += 1
            return "skip", "no_open_position"
        reduce_notional = min(proposal.max_notional_usd, position.current_notional_usd)
        fill = self.venue.fill(
            symbol=proposal.symbol,
            side=position.side,
            action="close",
            mid_price=context.price,
            spread_bps=_spread_bps(context),
            notional_usd=reduce_notional,
            timestamp=timestamp,
            decision_id=proposal.decision_id,
        )
        trade = self.portfolio.reduce_position(
            proposal.symbol,
            fill,
            reason="agent_reduce",
        )
        if trade is None:
            counters.skip_reasons["portfolio_reduce_rejected"] += 1
            return "skip", "portfolio_reduce_rejected"
        counters.fills += 1
        counters.positions_reduced += 1
        counters.positions_closed += 1
        counters.close_reasons[trade.close_reason] += 1
        journal.append(_fill_record(fill, reason="agent_reduce"))
        journal.append(_closed_trade_record(trade))
        return "reduce", "agent_reduce"

    def _process_protective_exit(
        self,
        *,
        context: AgentMarketContext,
        timestamp: str,
        journal: JsonlJournal,
        counters: _PaperCounters,
    ) -> None:
        reason = self.portfolio.protective_exit_reason(
            context.symbol,
            price=context.price,
            timestamp=timestamp,
        )
        if reason is None:
            return
        self._close_position(
            symbol=context.symbol,
            timestamp=timestamp,
            price=context.price,
            spread_bps=_spread_bps(context),
            decision_id="",
            reason=reason,
            journal=journal,
            counters=counters,
        )

    def _close_position(
        self,
        *,
        symbol: str,
        timestamp: str,
        price: float,
        spread_bps: float,
        decision_id: str,
        reason: str,
        journal: JsonlJournal,
        counters: _PaperCounters,
    ) -> TridentAIPaperClosedTrade | None:
        position = self.portfolio.open_positions.get(symbol)
        if position is None:
            counters.skip_reasons["no_open_position"] += 1
            return None
        fill = self.venue.fill(
            symbol=symbol,
            side=position.side,
            action="close",
            mid_price=price,
            spread_bps=spread_bps,
            notional_usd=position.current_notional_usd,
            timestamp=timestamp,
            decision_id=decision_id,
        )
        trade = self.portfolio.close_position(symbol, fill, reason=reason)
        if trade is None:
            counters.skip_reasons["portfolio_close_rejected"] += 1
            return None
        counters.fills += 1
        counters.positions_closed += 1
        counters.close_reasons[trade.close_reason] += 1
        journal.append(_fill_record(fill, reason=reason))
        journal.append(_closed_trade_record(trade))
        return trade

    def _force_close_open_positions(
        self,
        *,
        timestamp: str,
        last_prices: Mapping[str, float],
        last_spreads: Mapping[str, float],
        journal: JsonlJournal,
        counters: _PaperCounters,
    ) -> None:
        for symbol in list(self.portfolio.open_positions.keys()):
            price = float(last_prices.get(symbol, 0.0) or 0.0)
            if price <= 0:
                continue
            self._close_position(
                symbol=symbol,
                timestamp=timestamp,
                price=price,
                spread_bps=float(last_spreads.get(symbol, 0.0) or 0.0),
                decision_id="",
                reason="end_of_paper_replay",
                journal=journal,
                counters=counters,
            )


def run_trident_ai_paper_replay(
    input_path: str | Path,
    *,
    config: TridentAIConfig | None = None,
    journal_path: str | Path | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    max_decisions: int | None = None,
    market_input_path: str | Path | None = None,
    symbols: Sequence[str] | None = None,
    market_event_cache: Sequence[_PaperReplayEvent] | None = None,
) -> TridentAIPaperReplayResult:
    return TridentAIPaperReplayRunner(config=config).run(
        input_path,
        journal_path=journal_path,
        report_json_path=report_json_path,
        report_md_path=report_md_path,
        max_decisions=max_decisions,
        market_input_path=market_input_path,
        symbols=symbols,
        market_event_cache=market_event_cache,
    )


def build_paper_replay_report_payload(
    *,
    result: TridentAIPaperReplayResult,
    config: TridentAIConfig,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_paper_replay",
        "result": result.to_dict(),
        "config": {
            "mode": config.mode,
            "tradable_symbols": list(config.tradable_symbols),
            "paper": {
                "taker_fee_bps": config.paper.taker_fee_bps,
                "slippage_bps": config.paper.slippage_bps,
                "spread_multiplier": config.paper.spread_multiplier,
                "force_close_at_end": config.paper.force_close_at_end,
            },
            "risk": {
                "max_open_positions": config.risk.max_open_positions,
                "max_trades_per_day": config.risk.max_trades_per_day,
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
    assert isinstance(result, dict)
    lines = [
        "# TRIDENT-AI Paper Replay",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Input: `{result['input_path']}`",
            f"- Market input: `{result['market_input_path']}`",
            f"- Symbols filter: `{result['symbols_filter']}`",
            f"- Journal: `{result['journal_path']}`",
        f"- Decisions seen: `{result['decisions_seen']}`",
        f"- Market contexts seen: `{result['market_contexts_seen']}`",
        f"- Market exit checks: `{result['market_exit_checks']}`",
        f"- Proposals accepted: `{result['proposals_accepted']}` / `{result['proposals_seen']}`",
        f"- Opened/reduced/closed: `{result['positions_opened']}` / `{result['positions_reduced']}` / `{result['positions_closed']}`",
        f"- Open positions after replay: `{result['open_positions']}`",
        f"- Realized PnL: `${result['realized_pnl_usd']:.6f}`",
        f"- Unrealized PnL: `${result['unrealized_pnl_usd']:.6f}`",
        f"- Fees: `${result['fees_usd']:.6f}`",
        f"- AI cost estimate: `${result['ai_cost_usd']:.8f}`",
        f"- Net after AI cost: `${result['net_after_ai_cost_usd']:.8f}`",
        "",
        "## Actions",
        "",
        "| Action | Count |",
        "|---|---:|",
    ]
    for action, count in result["action_counts"].items():
        lines.append(f"| {action} | {count} |")
    if not result["action_counts"]:
        lines.append("| none | 0 |")
    lines.extend(["", "## Close Reasons", "", "| Reason | Count |", "|---|---:|"])
    for reason, count in result["close_reasons"].items():
        lines.append(f"| {reason} | {count} |")
    if not result["close_reasons"]:
        lines.append("| none | 0 |")
    lines.extend(["", "## Confidence Calibration", "", "| Bucket | Trades | Win rate | Avg PnL |", "|---|---:|---:|---:|"])
    buckets = result["confidence_buckets"]
    assert isinstance(buckets, dict)
    for bucket, stats in buckets.items():
        assert isinstance(stats, dict)
        lines.append(
            f"| {bucket} | {stats['trades']} | {stats['win_rate']:.2%} | ${stats['avg_pnl_usd']:.6f} |"
        )
    if not buckets:
        lines.append("| none | 0 | 0.00% | $0.000000 |")
    lines.append("")
    return "\n".join(lines)


def _iter_jsonl(input_path: str | Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in Path(input_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _decision_events_from_journal(
    input_path: str | Path,
    *,
    max_decisions: int | None,
    symbols_filter: tuple[str, ...],
) -> list[_PaperReplayEvent]:
    events: list[_PaperReplayEvent] = []
    allowed = set(symbols_filter)
    for record in _iter_jsonl(input_path):
        if record.get("event_type") != LLM_REPLAY_DECISION_EVENT:
            continue
        if allowed and str(record.get("symbol", "") or "").strip().upper() not in allowed:
            continue
        if max_decisions is not None and len(events) >= max_decisions:
            break
        timestamp = _parse_timestamp(str(record.get("timestamp", "") or ""))
        if timestamp is None:
            continue
        events.append(
            _PaperReplayEvent(
                timestamp=timestamp,
                priority=0,
                sequence=len(events),
                kind="decision",
                decision_record=record,
            )
        )
    return events


def _symbols_filter(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if not symbols:
        return ()
    normalized: list[str] = []
    for symbol in symbols:
        item = str(symbol).strip().upper()
        if item and item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _paper_event_sort_key(event: _PaperReplayEvent) -> tuple[datetime, int, int]:
    return (event.timestamp, event.priority, event.sequence)


def _context_from_record(record: Mapping[str, object]) -> AgentMarketContext | None:
    payload = record.get("context")
    if not isinstance(payload, Mapping):
        return None
    try:
        return AgentMarketContext.from_mapping(payload)
    except Exception:
        return None


def _proposal_from_record(record: Mapping[str, object]) -> AgentTradeProposal | None:
    payload = record.get("proposal")
    if not isinstance(payload, Mapping):
        return None
    try:
        return AgentTradeProposal.from_mapping(payload)
    except Exception:
        return None


def _record_is_accepted(record: Mapping[str, object]) -> bool:
    validation = record.get("validation")
    if not isinstance(validation, Mapping):
        return False
    return bool(validation.get("accepted", False))


def _validation_reason(record: Mapping[str, object]) -> str:
    validation = record.get("validation")
    if not isinstance(validation, Mapping):
        return ""
    reason = validation.get("reason", "")
    return str(reason) if reason is not None else ""


def _mark_event_time(counters: _PaperCounters, timestamp: str) -> None:
    if not timestamp:
        return
    counters.first_timestamp = counters.first_timestamp or timestamp
    counters.last_timestamp = timestamp


def _paper_decision_record(
    source_record: Mapping[str, object],
    context: AgentMarketContext,
    proposal: AgentTradeProposal | None,
    paper_action: str,
    reason: str,
) -> dict[str, object]:
    request = source_record.get("request")
    request_id = ""
    if isinstance(request, Mapping):
        request_id = str(request.get("request_id", "") or "")
    return {
        "event_type": PAPER_REPLAY_DECISION_EVENT,
        "source": "trident_ai_paper_replay",
        "record_index": source_record.get("record_index"),
        "timestamp": source_record.get("timestamp"),
        "symbol": context.symbol,
        "request_id": request_id,
        "decision_id": proposal.decision_id if proposal is not None else "",
        "proposal_action": proposal.action if proposal is not None else "",
        "paper_action": paper_action,
        "reason": reason,
        "price": context.price,
    }


def _fill_record(fill: TridentAIPaperFill, *, reason: str) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_FILL_EVENT,
        "source": "trident_ai_paper_replay",
        "timestamp": fill.timestamp,
        "symbol": fill.symbol,
        "reason": reason,
        "fill": fill.to_dict(),
    }


def _closed_trade_record(trade: TridentAIPaperClosedTrade) -> dict[str, object]:
    return {
        "event_type": PAPER_REPLAY_TRADE_CLOSED_EVENT,
        "source": "trident_ai_paper_replay",
        "timestamp": trade.closed_at,
        "symbol": trade.symbol,
        "close_reason": trade.close_reason,
        "trade": trade.to_dict(),
    }


def _spread_bps(context: AgentMarketContext) -> float:
    value = context.features.get("spread_bps", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _signed_impact_bps(*, action: str, side: str, impact_bps: float) -> float:
    if side == "long":
        return impact_bps if action == "open" else -impact_bps
    return -impact_bps if action == "open" else impact_bps


def _gross_pnl_usd(
    *,
    side: str,
    entry_price: float,
    exit_price: float,
    notional_usd: float,
) -> float:
    if entry_price <= 0:
        return 0.0
    if side == "long":
        ret = (exit_price - entry_price) / entry_price
    else:
        ret = (entry_price - exit_price) / entry_price
    return round(notional_usd * ret, 6)


def _time_stop_hit(opened_at: str, timestamp: str, time_stop_minutes: int) -> bool:
    if time_stop_minutes <= 0:
        return False
    opened = _parse_timestamp(opened_at)
    current = _parse_timestamp(timestamp)
    if opened is None or current is None:
        return False
    return (current - opened).total_seconds() >= time_stop_minutes * 60


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_regime(record: object) -> str:
    regime_snapshot = getattr(record, "regime_snapshot", {})
    if not isinstance(regime_snapshot, Mapping):
        return "unknown"
    for field_name in ("regime", "effective_regime", "regime_label"):
        value = regime_snapshot.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _accumulate_ai_cost(counters: _PaperCounters, record: Mapping[str, object]) -> None:
    response = record.get("llm_response")
    if not isinstance(response, Mapping):
        return
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return
    cost = usage.get("estimated_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return
    counters.ai_cost_usd = round(counters.ai_cost_usd + float(cost), 8)


def _confidence_buckets(
    trades: list[TridentAIPaperClosedTrade],
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[TridentAIPaperClosedTrade]] = defaultdict(list)
    for trade in trades:
        grouped[_confidence_bucket(trade.confidence)].append(trade)
    result: dict[str, dict[str, object]] = {}
    for bucket in sorted(grouped):
        bucket_trades = grouped[bucket]
        wins = [trade for trade in bucket_trades if trade.pnl_usd > 0]
        avg_pnl = sum(trade.pnl_usd for trade in bucket_trades) / len(bucket_trades)
        result[bucket] = {
            "trades": len(bucket_trades),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(bucket_trades), 6),
            "avg_pnl_usd": round(avg_pnl, 6),
            "total_pnl_usd": round(sum(trade.pnl_usd for trade in bucket_trades), 6),
        }
    return result


def _confidence_bucket(confidence: float) -> str:
    lower = int(max(min(confidence, 0.999999), 0.0) * 20) / 20
    upper = lower + 0.05
    return f"{lower:.2f}-{upper:.2f}"


def _open_position_entry_fees(portfolio: TridentAIPaperPortfolio) -> float:
    return round(
        sum(position.entry_fee_usd for position in portfolio.open_positions.values()),
        6,
    )


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
