import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.hip4_outcome_policy_market_audit import analyze_logs, render_markdown


class HIP4OutcomePolicyMarketAuditTests(unittest.TestCase):
    def test_analyze_logs_compares_policies_and_audits_non_btc_price_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paper = root / "paper"
            observer = root / "observer"
            self._write_paper_logs(paper)
            self._write_observer_logs(observer)

            payload = analyze_logs(paper_logs_dir=paper, observer_logs_dir=observer)
            markdown = render_markdown(payload)

            replay = payload["exit_policy_replay"]
            cutoffs = [row["entry_cutoff"] for row in replay["entry_cutoff_summaries"]]
            self.assertIn("2026-06-10T00:00:00Z", cutoffs)
            self.assertIn("2026-06-10T00:00:00Z", markdown)
            policies = {row["policy"]: row for row in replay["policy_summaries"]}
            self.assertAlmostEqual(policies["active_paper"]["net_pnl_usdc"], -1.0)
            self.assertAlmostEqual(policies["prob_stop_full"]["net_pnl_usdc"], 4.0)
            self.assertAlmostEqual(policies["hold_to_settlement"]["net_pnl_usdc"], -2.0)
            self.assertEqual(policies["prob_stop_full"]["exit_event_count"], 1)

            profiles = {
                item["profile"]: item
                for item in payload["market_universe_audit"]["profiles"]
            }
            paper_audit = profiles["mainnet_paper"]
            observer_audit = profiles["mainnet_observer"]
            self.assertEqual(paper_audit["opportunities"]["non_btc_underlyings"], ["HYPE"])
            self.assertEqual(
                paper_audit["price_binary_observations"]["non_btc_underlyings"],
                ["HYPE"],
            )
            self.assertEqual(
                observer_audit["price_binary_observations"]["non_btc_underlyings"],
                ["SOL"],
            )
            self.assertIn("Non-BTC priceBinary Audit", markdown)
            self.assertIn("HYPE", markdown)
            self.assertIn("SOL", markdown)

    def _write_paper_logs(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._write_csv(
            root / "trades.csv",
            [
                {
                    "ts": "2026-06-01T01:00:00Z",
                    "market_id": "BTC_GT_100_20260602_0000",
                    "outcome": "1",
                    "underlying": "BTC",
                    "edge_type": "MODEL",
                    "side": "BUY_YES",
                    "coin": "#1",
                    "price": "0.5",
                    "size_usdc": "10",
                    "token_qty": "20",
                    "status": "paper_filled",
                    "oid": "",
                    "cloid": "",
                },
                {
                    "ts": "2026-06-03T01:00:00Z",
                    "market_id": "HYPE_GT_50_20260604_0000",
                    "outcome": "2",
                    "underlying": "HYPE",
                    "edge_type": "MODEL",
                    "side": "BUY_NO",
                    "coin": "#2",
                    "price": "0.4",
                    "size_usdc": "8",
                    "token_qty": "20",
                    "status": "paper_filled",
                    "oid": "",
                    "cloid": "",
                },
            ],
        )
        self._write_csv(
            root / "settlements.csv",
            [
                {
                    "ts": "2026-06-02T00:05:00Z",
                    "market_id": "BTC_GT_100_20260602_0000",
                    "outcome": "1",
                    "underlying": "BTC",
                    "side": "BUY_YES",
                    "result": "NO",
                    "payout_usdc": "0",
                    "fee_usdc": "0",
                    "gross_pnl_usdc": "-10",
                    "net_pnl_usdc": "-10",
                    "pnl_usdc": "-10",
                    "is_win": "false",
                    "notes": "exchange_settlement",
                },
                {
                    "ts": "2026-06-04T00:05:00Z",
                    "market_id": "HYPE_GT_50_20260604_0000",
                    "outcome": "2",
                    "underlying": "HYPE",
                    "side": "BUY_NO",
                    "result": "NO",
                    "payout_usdc": "17",
                    "fee_usdc": "0",
                    "gross_pnl_usdc": "9",
                    "net_pnl_usdc": "9",
                    "pnl_usdc": "9",
                    "is_win": "true",
                    "notes": "exchange_settlement",
                },
            ],
        )
        self._write_csv(
            root / "early_exits.csv",
            [
                {
                    "ts": "2026-06-01T02:00:00Z",
                    "market_id": "BTC_GT_100_20260602_0000",
                    "outcome": "1",
                    "underlying": "BTC",
                    "side": "BUY_YES",
                    "action": "full_exit",
                    "reason": "probability_stop",
                    "position_status_before": "open",
                    "exit_fraction": "1",
                    "token_qty": "20",
                    "exit_price": "0.45",
                    "gross_exit_usdc": "9",
                    "fee_usdc": "0",
                    "net_exit_usdc": "9",
                    "cost_basis_usdc": "10",
                    "realized_pnl_usdc": "-1",
                    "exit_roi": "-0.1",
                    "hold_ev_usdc": "0",
                    "win_probability": "0.2",
                    "conservative_win_probability": "0.17",
                    "bid": "0.45",
                    "ask": "0.46",
                    "reference_price": "99",
                    "strike": "100",
                    "seconds_left": "3600",
                }
            ],
        )
        self._write_csv(
            root / "shadow_exit_policies.csv",
            [
                self._shadow_exit(
                    "2026-06-01T02:00:00Z",
                    "prob_stop_full",
                    "BTC_GT_100_20260602_0000",
                    "BTC",
                    "BUY_YES",
                    "-2",
                ),
                self._shadow_settlement(
                    "2026-06-02T00:05:00Z",
                    "prob_stop_full",
                    "BTC_GT_100_20260602_0000",
                    "BTC",
                    "BUY_YES",
                    "-2",
                ),
                self._shadow_settlement(
                    "2026-06-04T00:05:00Z",
                    "prob_stop_full",
                    "HYPE_GT_50_20260604_0000",
                    "HYPE",
                    "BUY_NO",
                    "6",
                ),
                self._shadow_settlement(
                    "2026-06-02T00:05:00Z",
                    "hold_to_settlement",
                    "BTC_GT_100_20260602_0000",
                    "BTC",
                    "BUY_YES",
                    "-10",
                ),
                self._shadow_settlement(
                    "2026-06-04T00:05:00Z",
                    "hold_to_settlement",
                    "HYPE_GT_50_20260604_0000",
                    "HYPE",
                    "BUY_NO",
                    "8",
                ),
            ],
        )
        self._write_csv(
            root / "opportunities.csv",
            [
                {
                    "ts": "2026-06-03T00:55:00Z",
                    "market_id": "HYPE_GT_50_20260604_0000",
                    "outcome": "2",
                    "underlying": "HYPE",
                    "edge_type": "MODEL",
                    "side": "BUY_NO",
                    "gross_edge": "0.04",
                    "net_edge": "0.02",
                    "confidence": "0.6",
                    "requested_size_usdc": "8",
                    "yes_ask": "",
                    "no_ask": "0.4",
                    "ref_price": "49",
                    "strike": "50",
                    "time_to_expiry": "3600",
                    "reason": "model",
                }
            ],
        )
        self._write_jsonl(
            root / "decisions.jsonl",
            [
                {
                    "ts": "2026-06-03T00:55:00Z",
                    "signal": {
                        "market_id": "HYPE_GT_50_20260604_0000",
                        "underlying": "HYPE",
                        "side": "BUY_NO",
                    },
                    "supervisor_decision": {
                        "approved": True,
                        "reason": "local_outcome_risk_ok",
                    },
                }
            ],
        )
        self._write_jsonl(
            root / "market_observations.jsonl",
            [
                {
                    "ts": "2026-06-03T00:50:00Z",
                    "class_name": "priceBinary",
                    "market_id": "HYPE_GT_50_20260604_0000",
                    "underlying": "HYPE",
                    "period": "1d",
                    "expiry_iso": "2026-06-04T00:00:00Z",
                    "support_status": "trading_supported",
                    "support_reason": "price_binary_supported",
                    "description": (
                        "class:priceBinary|underlying:HYPE|expiry:20260604-0000|"
                        "targetPrice:50|period:1d"
                    ),
                }
            ],
        )

    def _write_observer_logs(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._write_csv(root / "opportunities.csv", [])
        self._write_jsonl(
            root / "decisions.jsonl",
            [
                {
                    "ts": "2026-06-03T00:55:00Z",
                    "signal": {"market_id": "BTC_GT_100", "underlying": "BTC"},
                    "supervisor_decision": {"approved": False, "reason": "observer_mode"},
                }
            ],
        )
        self._write_jsonl(
            root / "market_observations.jsonl",
            [
                {
                    "ts": "2026-06-03T00:50:00Z",
                    "class_name": "priceBinary",
                    "market_id": "SOL_GT_150_20260604_0000",
                    "underlying": "SOL",
                    "period": "1d",
                    "expiry_iso": "2026-06-04T00:00:00Z",
                    "support_status": "observe_only",
                    "support_reason": "price_binary_observe_only",
                    "description": (
                        "class:priceBinary|underlying:SOL|expiry:20260604-0000|"
                        "targetPrice:150|period:1d"
                    ),
                }
            ],
        )

    def _shadow_exit(
        self,
        ts: str,
        policy: str,
        market_id: str,
        underlying: str,
        side: str,
        pnl: str,
    ) -> dict[str, str]:
        row = self._shadow_settlement(ts, policy, market_id, underlying, side, pnl)
        row["event_type"] = "exit"
        row["action"] = "full_exit"
        return row

    def _shadow_settlement(
        self,
        ts: str,
        policy: str,
        market_id: str,
        underlying: str,
        side: str,
        pnl: str,
    ) -> dict[str, str]:
        return {
            "ts": ts,
            "event_type": "settlement",
            "policy": policy,
            "market_id": market_id,
            "outcome": "1",
            "underlying": underlying,
            "side": side,
            "action": "",
            "reason": "",
            "position_status": "settled",
            "result": "YES",
            "remaining_qty_before": "0",
            "exit_fraction": "",
            "token_qty": "",
            "exit_price": "",
            "gross_exit_usdc": "",
            "fee_usdc": "0",
            "net_exit_usdc": "",
            "settlement_payout_usdc": "",
            "total_payout_usdc": "",
            "cost_basis_usdc": "",
            "gross_pnl_usdc": pnl,
            "realized_pnl_usdc": pnl,
            "net_pnl_usdc": pnl,
            "exit_roi": "",
            "hold_ev_usdc": "",
            "win_probability": "",
            "conservative_win_probability": "",
            "bid": "",
            "ask": "",
            "reference_price": "",
            "strike": "",
            "seconds_left": "",
        }

    def _write_csv(self, path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def _write_jsonl(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")


if __name__ == "__main__":
    unittest.main()
