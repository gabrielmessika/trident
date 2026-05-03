from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PodABacktestReport:
    reference_equity_usd: float = 0.0
    records_processed: int = 0
    signal_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    opened_count: int = 0
    skipped_open_count: int = 0
    closed_trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    realized_pnl_usd: float = 0.0
    records_by_regime: dict[str, int] = field(default_factory=dict)
    records_by_date: dict[str, int] = field(default_factory=dict)
    signals_by_symbol: dict[str, int] = field(default_factory=dict)
    signals_by_cluster: dict[str, int] = field(default_factory=dict)
    signals_by_side: dict[str, int] = field(default_factory=dict)
    signals_by_setup: dict[str, int] = field(default_factory=dict)
    signals_by_regime: dict[str, int] = field(default_factory=dict)
    signals_by_date: dict[str, int] = field(default_factory=dict)
    accepted_by_date: dict[str, int] = field(default_factory=dict)
    rejected_by_date: dict[str, int] = field(default_factory=dict)
    rejections_by_reason: dict[str, int] = field(default_factory=dict)
    accepted_by_setup: dict[str, int] = field(default_factory=dict)
    rejected_by_setup: dict[str, int] = field(default_factory=dict)
    close_reasons: dict[str, int] = field(default_factory=dict)
    trades_by_setup: dict[str, int] = field(default_factory=dict)
    pnl_by_setup: dict[str, float] = field(default_factory=dict)
    regime_transition_count: int = 0
    regime_transitions: dict[str, int] = field(default_factory=dict)
    regime_transitions_by_date: dict[str, dict[str, int]] = field(default_factory=dict)
    trades_by_symbol: dict[str, int] = field(default_factory=dict)
    trades_by_cluster: dict[str, int] = field(default_factory=dict)
    trades_by_regime: dict[str, int] = field(default_factory=dict)
    pnl_by_symbol: dict[str, float] = field(default_factory=dict)
    pnl_by_cluster: dict[str, float] = field(default_factory=dict)
    pnl_by_regime: dict[str, float] = field(default_factory=dict)
    pnl_by_date: dict[str, float] = field(default_factory=dict)
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    max_open_positions: int = 0
    max_open_margin_usd: float = 0.0
    max_open_notional_usd: float = 0.0
    max_open_expected_loss_usd: float = 0.0
    opened_by_setup: dict[str, int] = field(default_factory=dict)
    skipped_open_by_setup: dict[str, int] = field(default_factory=dict)
    hold_hours_total: float = 0.0
    hold_samples: int = 0
    closed_trade_log: list[dict[str, object]] = field(default_factory=list)
    confidence_total: float = 0.0
    _peak_realized_pnl_usd: float = 0.0

    def add_record_regime(self, regime: str) -> None:
        self.records_by_regime[regime] = self.records_by_regime.get(regime, 0) + 1

    def add_record_date(self, date_key: str) -> None:
        self.records_by_date[date_key] = self.records_by_date.get(date_key, 0) + 1

    def add_signal(
        self,
        *,
        date_key: str,
        symbol: str,
        side: str,
        setup: str,
        regime: str,
        confidence: float,
        market_cluster: str | None = None,
    ) -> None:
        self.signal_count += 1
        self.signals_by_symbol[symbol] = self.signals_by_symbol.get(symbol, 0) + 1
        if market_cluster:
            self.signals_by_cluster[market_cluster] = self.signals_by_cluster.get(
                market_cluster,
                0,
            ) + 1
        self.signals_by_side[side] = self.signals_by_side.get(side, 0) + 1
        self.signals_by_setup[setup] = self.signals_by_setup.get(setup, 0) + 1
        self.signals_by_regime[regime] = self.signals_by_regime.get(regime, 0) + 1
        self.signals_by_date[date_key] = self.signals_by_date.get(date_key, 0) + 1
        self.confidence_total += confidence

    def add_decision(
        self,
        *,
        date_key: str,
        setup: str,
        accepted: bool,
        reason: str,
    ) -> None:
        if accepted:
            self.accepted_count += 1
            self.accepted_by_date[date_key] = self.accepted_by_date.get(date_key, 0) + 1
            self.accepted_by_setup[setup] = self.accepted_by_setup.get(setup, 0) + 1
            return
        self.rejected_count += 1
        self.rejected_by_date[date_key] = self.rejected_by_date.get(date_key, 0) + 1
        self.rejections_by_reason[reason] = self.rejections_by_reason.get(reason, 0) + 1
        self.rejected_by_setup[setup] = self.rejected_by_setup.get(setup, 0) + 1

    def add_regime_transition(
        self,
        *,
        date_key: str,
        previous_regime: str,
        new_regime: str,
    ) -> None:
        transition_key = f"{previous_regime}->{new_regime}"
        self.regime_transition_count += 1
        self.regime_transitions[transition_key] = (
            self.regime_transitions.get(transition_key, 0) + 1
        )
        date_bucket = self.regime_transitions_by_date.setdefault(date_key, {})
        date_bucket[transition_key] = date_bucket.get(transition_key, 0) + 1

    def add_execution_batch(
        self,
        *,
        opened_symbols: list[str],
        skipped_open_symbols: list[str],
    ) -> None:
        self.opened_count += len(opened_symbols)
        self.skipped_open_count += len(skipped_open_symbols)

    def observe_open_exposure(self, open_positions: list[object]) -> None:
        open_margin_usd = round(
            sum(max(float(getattr(position, "margin_usd", 0.0)), 0.0) for position in open_positions),
            6,
        )
        open_notional_usd = round(
            sum(
                max(float(getattr(position, "target_notional_usd", 0.0)), 0.0)
                for position in open_positions
            ),
            6,
        )
        open_expected_loss_usd = round(
            sum(
                max(float(getattr(position, "expected_loss_usd", 0.0)), 0.0)
                for position in open_positions
            ),
            6,
        )
        self.max_open_positions = max(self.max_open_positions, len(open_positions))
        self.max_open_margin_usd = max(self.max_open_margin_usd, open_margin_usd)
        self.max_open_notional_usd = max(self.max_open_notional_usd, open_notional_usd)
        self.max_open_expected_loss_usd = max(
            self.max_open_expected_loss_usd,
            open_expected_loss_usd,
        )

    def add_opened_setup(self, setup: str) -> None:
        self.opened_by_setup[setup] = self.opened_by_setup.get(setup, 0) + 1

    def add_skipped_open_setup(self, setup: str) -> None:
        self.skipped_open_by_setup[setup] = self.skipped_open_by_setup.get(setup, 0) + 1

    def add_closed_trade(
        self,
        *,
        date_key: str,
        symbol: str,
        side: str,
        setup: str | None = None,
        confidence: float | None = None,
        market_cluster: str | None = None,
        close_regime: str | None = None,
        entry_price: float | None = None,
        exit_price: float | None = None,
        target_notional_usd: float | None = None,
        margin_usd: float | None = None,
        effective_leverage: float | None = None,
        risk_budget_usd: float | None = None,
        expected_loss_usd: float | None = None,
        invalidation_price: float | None = None,
        stop_bps: float | None = None,
        time_stop_hours: int | None = None,
        take_profit_bps: float | None = None,
        break_even_trigger_bps: float | None = None,
        trailing_activation_bps: float | None = None,
        trailing_distance_bps: float | None = None,
        pnl_usd: float,
        gross_pnl_usd: float,
        fees_usd: float,
        close_reason: str,
        hold_hours: float | None,
        opened_at: str | None,
        closed_at: str | None,
        setup_details: dict[str, float | str | bool] | None = None,
    ) -> None:
        self.closed_trade_count += 1
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl_usd, 2)
        self.gross_pnl_usd = round(self.gross_pnl_usd + gross_pnl_usd, 2)
        self.fees_usd = round(self.fees_usd + fees_usd, 6)
        if pnl_usd >= 0:
            self.win_count += 1
        else:
            self.loss_count += 1
        self.close_reasons[close_reason] = self.close_reasons.get(close_reason, 0) + 1
        self.trades_by_symbol[symbol] = self.trades_by_symbol.get(symbol, 0) + 1
        self.pnl_by_symbol[symbol] = round(self.pnl_by_symbol.get(symbol, 0.0) + pnl_usd, 2)
        if market_cluster:
            self.trades_by_cluster[market_cluster] = self.trades_by_cluster.get(
                market_cluster,
                0,
            ) + 1
            self.pnl_by_cluster[market_cluster] = round(
                self.pnl_by_cluster.get(market_cluster, 0.0) + pnl_usd,
                2,
            )
        if close_regime:
            self.trades_by_regime[close_regime] = self.trades_by_regime.get(close_regime, 0) + 1
            self.pnl_by_regime[close_regime] = round(
                self.pnl_by_regime.get(close_regime, 0.0) + pnl_usd,
                2,
            )
        self.pnl_by_date[date_key] = round(self.pnl_by_date.get(date_key, 0.0) + pnl_usd, 2)
        if setup:
            self.trades_by_setup[setup] = self.trades_by_setup.get(setup, 0) + 1
            self.pnl_by_setup[setup] = round(self.pnl_by_setup.get(setup, 0.0) + pnl_usd, 2)
        self._update_drawdown()
        if hold_hours is not None:
            self.hold_hours_total += hold_hours
            self.hold_samples += 1
        self.closed_trade_log.append(
            {
                "date": date_key,
                "symbol": symbol,
                "side": side,
                "setup": setup,
                "open_reason": setup,
                "confidence": confidence,
                "market_cluster": market_cluster,
                "close_regime": close_regime,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "target_notional_usd": target_notional_usd,
                "margin_usd": margin_usd,
                "leverage": effective_leverage,
                "effective_leverage": effective_leverage,
                "risk_budget_usd": risk_budget_usd,
                "expected_loss_usd": expected_loss_usd,
                "invalidation_price": invalidation_price,
                "stop_bps": stop_bps,
                "time_stop_hours": time_stop_hours,
                "take_profit_bps": take_profit_bps,
                "break_even_trigger_bps": break_even_trigger_bps,
                "trailing_activation_bps": trailing_activation_bps,
                "trailing_distance_bps": trailing_distance_bps,
                "pnl_usd": pnl_usd,
                "is_win": pnl_usd >= 0,
                "gross_pnl_usd": gross_pnl_usd,
                "fees_usd": fees_usd,
                "close_reason": close_reason,
                "hold_hours": hold_hours,
                "opened_at": opened_at,
                "closed_at": closed_at,
                "setup_details": dict(setup_details or {}),
            }
        )

    @property
    def average_confidence(self) -> float:
        if self.signal_count == 0:
            return 0.0
        return round(self.confidence_total / self.signal_count, 4)

    @property
    def average_hold_hours(self) -> float:
        if self.hold_samples == 0:
            return 0.0
        return round(self.hold_hours_total / self.hold_samples, 4)

    @property
    def win_rate(self) -> float | None:
        closed_count = self.win_count + self.loss_count
        if closed_count <= 0:
            return None
        return round(self.win_count / closed_count, 4)

    def to_dict(self) -> dict[str, object]:
        return {
            "records_processed": self.records_processed,
            "signal_count": self.signal_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "opened_count": self.opened_count,
            "skipped_open_count": self.skipped_open_count,
            "closed_trade_count": self.closed_trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": self.win_rate,
            "realized_pnl_usd": self.realized_pnl_usd,
            "gross_pnl_usd": self.gross_pnl_usd,
            "fees_usd": self.fees_usd,
            "max_drawdown_usd": round(self.max_drawdown_usd, 4),
            "reference_equity_usd": self.reference_equity_usd,
            "max_open_positions": self.max_open_positions,
            "max_open_margin_usd": round(self.max_open_margin_usd, 4),
            "max_open_notional_usd": round(self.max_open_notional_usd, 4),
            "max_open_expected_loss_usd": round(self.max_open_expected_loss_usd, 4),
            "average_hold_hours": self.average_hold_hours,
            "records_by_regime": self.records_by_regime,
            "records_by_date": self.records_by_date,
            "signals_by_symbol": self.signals_by_symbol,
            "signals_by_cluster": self.signals_by_cluster,
            "signals_by_side": self.signals_by_side,
            "signals_by_setup": self.signals_by_setup,
            "signals_by_regime": self.signals_by_regime,
            "signals_by_date": self.signals_by_date,
            "accepted_by_date": self.accepted_by_date,
            "rejected_by_date": self.rejected_by_date,
            "rejections_by_reason": self.rejections_by_reason,
            "accepted_by_setup": self.accepted_by_setup,
            "rejected_by_setup": self.rejected_by_setup,
            "regime_transition_count": self.regime_transition_count,
            "regime_transitions": self.regime_transitions,
            "regime_transitions_by_date": self.regime_transitions_by_date,
            "close_reasons": self.close_reasons,
            "opened_by_setup": self.opened_by_setup,
            "skipped_open_by_setup": self.skipped_open_by_setup,
            "trades_by_symbol": self.trades_by_symbol,
            "trades_by_cluster": self.trades_by_cluster,
            "trades_by_regime": self.trades_by_regime,
            "trades_by_setup": self.trades_by_setup,
            "pnl_by_symbol": self.pnl_by_symbol,
            "pnl_by_cluster": self.pnl_by_cluster,
            "pnl_by_regime": self.pnl_by_regime,
            "pnl_by_setup": self.pnl_by_setup,
            "pnl_by_date": self.pnl_by_date,
            "average_confidence": self.average_confidence,
            "closed_trade_log": self.closed_trade_log,
        }

    def _update_drawdown(self) -> None:
        self._peak_realized_pnl_usd = max(self._peak_realized_pnl_usd, self.realized_pnl_usd)
        drawdown = round(self._peak_realized_pnl_usd - self.realized_pnl_usd, 4)
        self.max_drawdown_usd = max(self.max_drawdown_usd, drawdown)
