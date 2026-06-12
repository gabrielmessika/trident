import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_p101_recent_replay import (
    build_report_payload,
    load_live_trades,
    parse_timestamp,
    replay_trades_from_report,
    summarize_by_pod,
    trade_alignment,
)


class P101RecentReplayTests(unittest.TestCase):
    def test_report_compares_live_replay_and_cost_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backfill = root / "closed.csv"
            fill_events = root / "fills.csv"
            _write_csv(
                backfill,
                [
                    "pod",
                    "symbol",
                    "side",
                    "setup",
                    "close_reason",
                    "opened_at",
                    "closed_at",
                    "event_ts",
                    "target_notional_usd",
                    "exchange_fee_usd",
                    "exchange_closed_pnl_usd",
                    "funding_usd",
                    "exchange_net_pnl_usd",
                ],
                [
                    {
                        "pod": "pod_a",
                        "symbol": "BTC",
                        "side": "long",
                        "setup": "trend_pullback_long",
                        "close_reason": "exchange_closed",
                        "opened_at": "2026-06-10T10:00:00+00:00",
                        "closed_at": "2026-06-10T10:30:00+00:00",
                        "event_ts": "2026-06-10T10:30:00Z",
                        "target_notional_usd": "100",
                        "exchange_fee_usd": "0.04",
                        "exchange_closed_pnl_usd": "1.25",
                        "funding_usd": "-0.01",
                        "exchange_net_pnl_usd": "1.20",
                    }
                ],
            )
            _write_csv(
                fill_events,
                [
                    "event_ts",
                    "pod",
                    "symbol",
                    "action",
                    "fill_ts",
                    "slippage_bps",
                ],
                [
                    {
                        "event_ts": "2026-06-10T10:00:00Z",
                        "pod": "pod_a",
                        "symbol": "BTC",
                        "action": "open",
                        "fill_ts": "2026-06-10T10:00:00Z",
                        "slippage_bps": "9",
                    },
                    {
                        "event_ts": "2026-06-10T10:30:00Z",
                        "pod": "pod_a",
                        "symbol": "BTC",
                        "action": "close",
                        "fill_ts": "2026-06-10T10:30:00Z",
                        "slippage_bps": "11",
                    },
                ],
            )
            replay_payload = {
                "records_processed": 2,
                "duplicate_timestamps_skipped": 0,
                "first_timestamp": "2026-06-10T10:00:00Z",
                "last_timestamp": "2026-06-10T10:30:00Z",
                "dates_covered": ["2026-06-10"],
                "total_realized_pnl_usd": 1.0,
                "directional_fees_usd": 0.07,
                "total_activity_count": 1,
                "pod_a": {
                    "closed_trade_log": [
                        {
                            "symbol": "BTC",
                            "side": "long",
                            "setup": "trend_pullback_long",
                            "close_reason": "take_profit",
                            "opened_at": "2026-06-10T10:00:00+00:00",
                            "closed_at": "2026-06-10T10:30:00+00:00",
                            "target_notional_usd": 100,
                            "pnl_usd": 1.0,
                            "gross_pnl_usd": 1.07,
                            "fees_usd": 0.07,
                        }
                    ]
                },
            }

            start = parse_timestamp("2026-06-10T00:00:00Z")
            end = parse_timestamp("2026-06-11T00:00:00Z")
            self.assertIsNotNone(start)
            self.assertIsNotNone(end)
            live_trades = load_live_trades(backfill, start=start, end=end)  # type: ignore[arg-type]
            replay_trades = replay_trades_from_report(replay_payload)
            payload, alignment_rows = build_report_payload(
                start=start,  # type: ignore[arg-type]
                end=end,  # type: ignore[arg-type]
                input_files=[{"name": "2026-06-10.jsonl", "path": "x", "line_count": 2}],
                replay_report_path=root / "replay.json",
                replay_payload=replay_payload,
                live_trades=live_trades,
                replay_trades=replay_trades,
                fill_events_path=fill_events,
                backfill_path=backfill,
                max_open_delta_minutes=5,
                apply_live_notional_caps=True,
            )

        self.assertEqual(payload["live_exchange"]["total"]["closed_trade_count"], 1)
        self.assertEqual(payload["replay_current_config"]["total"]["pnl_usd"], 1.0)
        self.assertEqual(payload["trade_alignment"]["matched_trade_count"], 1)
        self.assertEqual(alignment_rows[0]["status"], "matched")
        observed = payload["cost_sensitivity_overlay"]["observed_by_symbol"]["total"]
        self.assertAlmostEqual(observed["slippage_cost_usd"], 0.2)
        self.assertAlmostEqual(observed["net_after_cost_overlay_usd"], 0.8)

    def test_alignment_reports_unmatched_sides(self) -> None:
        live = replay_trades_from_report(
            {
                "pod_a": {
                    "closed_trade_log": [
                        {
                            "symbol": "BTC",
                            "side": "long",
                            "opened_at": "2026-06-10T10:00:00Z",
                            "closed_at": "2026-06-10T10:30:00Z",
                            "target_notional_usd": 100,
                            "pnl_usd": 1.0,
                            "gross_pnl_usd": 1.1,
                            "fees_usd": 0.1,
                        }
                    ]
                }
            }
        )
        replay = replay_trades_from_report(
            {
                "pod_a": {
                    "closed_trade_log": [
                        {
                            "symbol": "ETH",
                            "side": "long",
                            "opened_at": "2026-06-10T10:00:00Z",
                            "closed_at": "2026-06-10T10:30:00Z",
                            "target_notional_usd": 100,
                            "pnl_usd": -1.0,
                            "gross_pnl_usd": -0.9,
                            "fees_usd": 0.1,
                        }
                    ]
                }
            }
        )

        summary, rows = trade_alignment(
            live_trades=live,
            replay_trades=replay,
            max_open_delta_minutes=5,
        )

        self.assertEqual(summary["matched_trade_count"], 0)
        self.assertEqual(summary["live_unmatched_count"], 1)
        self.assertEqual(summary["replay_unmatched_count"], 1)
        self.assertEqual({row["status"] for row in rows}, {"live_unmatched", "replay_unmatched"})

    def test_summarize_by_pod_uses_report_pod_keys(self) -> None:
        replay = replay_trades_from_report(
            {
                "pod_a": {
                    "closed_trade_log": [
                        {
                            "symbol": "BTC",
                            "side": "long",
                            "target_notional_usd": 100,
                            "pnl_usd": 2.0,
                            "gross_pnl_usd": 2.1,
                            "fees_usd": 0.1,
                        }
                    ]
                },
                "pod_c": {
                    "closed_trade_log": [
                        {
                            "symbol": "XYZ:GOLD",
                            "side": "long",
                            "target_notional_usd": 100,
                            "pnl_usd": -1.0,
                            "gross_pnl_usd": -0.9,
                            "fees_usd": 0.1,
                        }
                    ]
                },
            }
        )

        summary = summarize_by_pod(replay)

        self.assertEqual(summary["pod_a"]["pnl_usd"], 2.0)
        self.assertEqual(summary["pod_c"]["pnl_usd"], -1.0)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
