import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.backfill_trident_exchange_audit import (
    enrich_trades,
    load_trade_closes,
    parse_exchange_fills,
    parse_funding_payments,
    write_outputs,
)


class ExchangeAuditBackfillTests(unittest.TestCase):
    def test_backfill_matches_close_fill_and_funding_window(self) -> None:
        trade_close = {
            "event_type": "trade_close",
            "timestamp": "2026-06-12T00:30:00Z",
            "trade": {
                "symbol": "ETH",
                "side": "long",
                "setup": "trend_pullback_long",
                "entry_price": 2000.0,
                "exit_price": 1990.0,
                "target_notional_usd": 100.0,
                "pnl_usd": -0.55,
                "gross_pnl_usd": -0.5,
                "fees_usd": 0.05,
                "close_reason": "early_failure_exit",
                "opened_at": "2026-06-12T00:00:00Z",
                "closed_at": "2026-06-12T00:30:00Z",
            },
        }
        raw_fills = [
            {
                "coin": "ETH",
                "oid": 123,
                "side": "A",
                "dir": "Close Long",
                "sz": "0.05",
                "px": "1990",
                "closedPnl": "-0.5",
                "fee": "0.04",
                "time": 1781224200000,
                "hash": "0xfill",
            },
            {
                "coin": "ETH",
                "oid": 122,
                "side": "B",
                "dir": "Open Long",
                "sz": "0.05",
                "px": "2000",
                "closedPnl": "0",
                "fee": "0.04",
                "time": 1781222400000,
                "hash": "0xopen",
            },
        ]
        raw_funding = [
            {
                "time": 1781223300000,
                "hash": "0xfunding",
                "delta": {
                    "coin": "ETH",
                    "usdc": "-0.01",
                    "fundingRate": "0.0001",
                    "szi": "0.05",
                },
            }
        ]

        with TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir) / "server-data"
            output_dir = Path(tmpdir) / "out"
            (source_root / "logs").mkdir(parents=True)
            (source_root / "logs" / "pod_a_live.jsonl").write_text(
                json.dumps(trade_close) + "\n",
                encoding="utf-8",
            )

            trades = load_trade_closes(source_root)
            fills = parse_exchange_fills(raw_fills)
            funding = parse_funding_payments(raw_funding)
            enrichments, owners = enrich_trades(trades, fills, funding)
            summary = write_outputs(
                output_dir=output_dir,
                source_root=source_root,
                trades=trades,
                fills=fills,
                funding=funding,
                enrichments=enrichments,
                matched_fill_owner=owners,
                start_ms=1781222400000,
                end_ms=1781226000000,
                raw_fills_payload=raw_fills,
                raw_funding_payload=raw_funding,
            )

            closed_rows = list(
                csv.DictReader(
                    (output_dir / "trident_ac_closed_trades_full.csv").open(
                        encoding="utf-8",
                        newline="",
                    )
                )
            )
            fill_rows = list(
                csv.DictReader(
                    (output_dir / "trident_ac_exchange_fills.csv").open(
                        encoding="utf-8",
                        newline="",
                    )
                )
            )

        self.assertEqual(summary["matched_trade_rows_by_pod"], {"pod_a": 1})
        self.assertEqual(len(closed_rows), 1)
        self.assertEqual(closed_rows[0]["exchange_close_fill_count"], "1")
        self.assertEqual(closed_rows[0]["exchange_closed_pnl_usd"], "-0.5")
        self.assertEqual(closed_rows[0]["exchange_fee_usd"], "0.04")
        self.assertEqual(closed_rows[0]["funding_usd"], "-0.01")
        self.assertEqual(closed_rows[0]["exchange_net_pnl_usd"], "-0.55")
        matched_close = [row for row in fill_rows if row["matched_action"] == "close"]
        self.assertEqual(len(matched_close), 1)
        self.assertEqual(matched_close[0]["oid"], "123")


if __name__ == "__main__":
    unittest.main()
