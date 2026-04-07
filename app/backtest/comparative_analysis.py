from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any


@dataclass(slots=True)
class TradeStats:
    closed_trade_count: int
    win_count: int
    loss_count: int
    realized_pnl_usd: float
    gross_pnl_usd: float
    fees_usd: float
    win_rate: float
    expectancy_usd: float
    avg_win_usd: float
    avg_loss_usd: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_backtest_comparative_summary(backtest: Any) -> dict[str, object]:
    payload = _as_payload(backtest)
    closed_trade_log = list(payload.get("closed_trade_log", []))
    records_by_date = payload.get("records_by_date", {})
    active_days = max(len(records_by_date) if isinstance(records_by_date, dict) else 0, 1)
    closed_trade_count = int(payload.get("closed_trade_count", 0))
    signal_count = int(payload.get("signal_count", 0))
    summary = {
        "realized_pnl_usd": round(float(payload.get("realized_pnl_usd", 0.0)), 4),
        "gross_pnl_usd": round(float(payload.get("gross_pnl_usd", 0.0)), 4),
        "fees_usd": round(float(payload.get("fees_usd", 0.0)), 6),
        "max_drawdown_usd": round(float(payload.get("max_drawdown_usd", 0.0)), 4),
        "closed_trade_count": closed_trade_count,
        "signal_count": signal_count,
        "closed_trades_per_day": round(closed_trade_count / active_days, 4),
        "signals_per_day": round(signal_count / active_days, 4),
        "signal_to_trade_ratio": round(
            closed_trade_count / max(signal_count, 1),
            4,
        ),
    }
    summary["trade_stats"] = _trade_stats(closed_trade_log).to_dict()
    return {
        "summary": summary,
        "by_cluster": _group_trade_stats(closed_trade_log, "market_cluster"),
        "by_symbol": _group_trade_stats(closed_trade_log, "symbol"),
        "by_regime": _group_trade_stats(closed_trade_log, "close_regime"),
    }


def _as_payload(backtest: Any) -> dict[str, object]:
    if isinstance(backtest, dict):
        return backtest
    if is_dataclass(backtest):
        return asdict(backtest)
    if hasattr(backtest, "__dict__"):
        return dict(backtest.__dict__)
    raise TypeError("backtest must be a dict-like object or dataclass instance")


def _group_trade_stats(
    closed_trade_log: list[dict[str, object]],
    key: str,
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for trade in closed_trade_log:
        name = str(trade.get(key) or "unknown")
        grouped.setdefault(name, []).append(trade)
    return {
        name: _trade_stats(trades).to_dict()
        for name, trades in sorted(grouped.items())
    }


def _trade_stats(trades: list[dict[str, object]]) -> TradeStats:
    closed_trade_count = len(trades)
    pnl_values = [float(trade.get("pnl_usd", 0.0) or 0.0) for trade in trades]
    gross_values = [float(trade.get("gross_pnl_usd", 0.0) or 0.0) for trade in trades]
    fee_values = [float(trade.get("fees_usd", 0.0) or 0.0) for trade in trades]
    wins = [value for value in pnl_values if value >= 0]
    losses = [value for value in pnl_values if value < 0]
    realized_pnl_usd = round(sum(pnl_values), 4)
    gross_pnl_usd = round(sum(gross_values), 4)
    fees_usd = round(sum(fee_values), 6)
    win_count = len(wins)
    loss_count = len(losses)
    return TradeStats(
        closed_trade_count=closed_trade_count,
        win_count=win_count,
        loss_count=loss_count,
        realized_pnl_usd=realized_pnl_usd,
        gross_pnl_usd=gross_pnl_usd,
        fees_usd=fees_usd,
        win_rate=round(win_count / closed_trade_count, 4) if closed_trade_count else 0.0,
        expectancy_usd=round(realized_pnl_usd / closed_trade_count, 4)
        if closed_trade_count
        else 0.0,
        avg_win_usd=round(sum(wins) / win_count, 4) if win_count else 0.0,
        avg_loss_usd=round(sum(losses) / loss_count, 4) if loss_count else 0.0,
    )
