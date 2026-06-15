import gzip
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.research.hyperliquid_top30_research import CandleRecord
from scripts.run_p110_hyperliquid_history_replay import (
    CalendarRule,
    HistoryTrade,
    RuleSummary,
    attempt_s3_archive_probe,
    classify_summary,
    default_rules,
    replay_calendar_rules,
    rule_to_dict,
    summarize_rules,
)


def _write_candles(path: Path, candles: list[CandleRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump([item.to_dict() for item in candles], handle)


def _candles(symbol: str, start: datetime, closes: list[float], interval_minutes: int = 60) -> list[CandleRecord]:
    rows: list[CandleRecord] = []
    for index, close in enumerate(closes):
        ts = start + timedelta(minutes=index * interval_minutes)
        start_ms = int(ts.timestamp() * 1000)
        rows.append(
            CandleRecord(
                start_time=start_ms,
                end_time=start_ms + interval_minutes * 60_000 - 1,
                interval="1h",
                symbol=symbol,
                open=close,
                high=close * 1.001,
                low=close * 0.999,
                close=close,
                volume=1000.0,
                trade_count=10,
            )
        )
    return rows


class P110HyperliquidHistoryReplayTests(unittest.TestCase):
    def test_daily_open_symbol_dow_rule_does_not_fire_every_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            monday = datetime(2026, 3, 30, tzinfo=UTC)
            _write_candles(
                output_dir / "raw" / "api_candles" / "1h" / "NEAR.json.gz",
                _candles("NEAR", monday, [100.0 + index for index in range(12)]),
            )
            rule = CalendarRule(
                name="near_monday_daily_open",
                description="test",
                interval="1h",
                hold_bars=8,
                mode="daily_open",
                symbol_side_by_dow={("NEAR", 0): "long"},
            )

            trades = replay_calendar_rules(
                output_dir=output_dir,
                symbols=["NEAR"],
                rules=[rule],
                notional_usd=200.0,
                cost_bps=16.0,
            )

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].hour_utc, 0)
        self.assertGreater(trades[0].net_pnl_usd, 0)

    def test_rule_to_dict_serializes_tuple_keys(self) -> None:
        rule = default_rules(["1h"])[0]

        payload = rule_to_dict(rule)

        json.dumps(payload)
        self.assertIsInstance(payload["symbol_side_by_dow"], list)
        self.assertIn("symbol", payload["symbol_side_by_dow"][0])

    def test_classify_requires_pre_local_confirmation_for_candidate(self) -> None:
        summary = RuleSummary(
            rule="test",
            description="",
            status="",
            reason="",
            interval="1h",
            hold_bars=8,
            trade_count=40,
            net_pnl_usd=10.0,
            avg_net_bps=5.0,
            hit_rate=0.55,
            profit_factor=1.3,
            max_drawdown_usd=3.0,
            first_timestamp="2026-04-06T00:00:00Z",
            last_timestamp="2026-06-01T00:00:00Z",
            pre_local_pnl_usd=0.0,
            local_window_pnl_usd=10.0,
            recent_half_pnl_usd=5.0,
            top_symbol="NEAR",
            top_symbol_pnl_usd=10.0,
            worst_symbol="NEAR",
            worst_symbol_pnl_usd=10.0,
        )

        status, reason = classify_summary(summary)

        self.assertEqual(status, "research_only")
        self.assertIn("fenetre locale", reason)

    def test_summarize_rule_computes_symbol_concentration(self) -> None:
        trades = [
            HistoryTrade(
                rule="r",
                timestamp=f"2026-03-{day:02d}T00:00:00Z",
                exit_timestamp=f"2026-03-{day:02d}T08:00:00Z",
                symbol="NEAR" if day < 20 else "TON",
                interval="1h",
                side="long",
                dow_utc=0,
                hour_utc=0,
                entry_price=100.0,
                exit_price=101.0,
                gross_bps=100.0,
                net_bps=84.0,
                notional_usd=200.0,
                net_pnl_usd=1.68 if day < 20 else -0.5,
                period="pre_local_window",
            )
            for day in range(1, 31)
        ]
        rule = CalendarRule("r", "", "1h", 8, "daily_open", symbol_side_by_dow={("NEAR", 0): "long"})

        summary = summarize_rules([rule], trades)[0]

        self.assertEqual(summary.top_symbol, "NEAR")
        self.assertEqual(summary.worst_symbol, "TON")

    def test_s3_probe_reports_missing_aws_without_throwing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch("scripts.run_p110_hyperliquid_history_replay.shutil.which", return_value=None):
            manifest = attempt_s3_archive_probe(
                output_dir=Path(tmpdir),
                dates=["20230916"],
                hours=["9"],
                symbols=["SOL"],
                timeout_seconds=1,
            )

        self.assertEqual(manifest["status"], "unavailable")
        self.assertIn("aws CLI not installed", manifest["reason"])


if __name__ == "__main__":
    unittest.main()
