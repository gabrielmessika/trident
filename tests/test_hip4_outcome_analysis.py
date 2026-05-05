import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.trident.hip4_outcome.analysis import (
    ReviewThresholds,
    analyze_profile,
    analyze_profiles,
    render_markdown,
)


class HIP4OutcomeAnalysisTests(unittest.TestCase):
    def test_analyze_profile_builds_economics_calibration_and_loss_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_sample_logs(root)

            payload = analyze_profile(
                "testnet",
                root,
                thresholds=ReviewThresholds(
                    min_testnet_settlements=2,
                    min_testnet_markets=2,
                    min_testnet_days=1,
                    min_calibration_samples=2,
                    min_profit_factor=0.5,
                    max_brier_score=0.3,
                ),
            )

            self.assertEqual(payload["row_counts"]["opportunities"], 2)
            self.assertEqual(payload["decisions"]["approved_count"], 2)
            self.assertEqual(payload["settlements"]["count"], 2)
            self.assertEqual(payload["settlements"]["win_count"], 1)
            self.assertAlmostEqual(payload["settlements"]["net_pnl_usdc"], -1.0)
            self.assertAlmostEqual(payload["settlements"]["profit_factor"], 0.75)
            self.assertEqual(payload["calibration"]["count"], 2)
            self.assertAlmostEqual(payload["calibration"]["brier_score"], 0.34)
            self.assertEqual(
                payload["loss_review"]["categories"][0]["category"],
                "late_expiry_reversal",
            )
            guardrails = payload["guardrail_candidates"]["candidates"]
            self.assertTrue(
                any(item["name"] == "loss_category:late_expiry_reversal" for item in guardrails)
            )
            late_guardrail = next(
                item
                for item in guardrails
                if item["name"] == "loss_category:late_expiry_reversal"
            )
            self.assertEqual(late_guardrail["kind"], "post_trade_loss_category")
            self.assertEqual(late_guardrail["verdict"], "watch")
            self.assertEqual(late_guardrail["excluded_count"], 1)
            self.assertAlmostEqual(late_guardrail["pnl_delta_usdc"], 4.0)
            self.assertEqual(payload["readiness"]["status"], "collect_more_data")
            self.assertTrue(
                any("Brier" in reason for reason in payload["readiness"]["reasons"])
            )

    def test_analyze_profiles_and_render_markdown_are_usable_with_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            testnet = root / "testnet"
            mainnet = root / "mainnet"
            testnet.mkdir()
            mainnet.mkdir()
            self._write_sample_logs(testnet)
            self._write_csv(
                mainnet / "opportunities.csv",
                [
                    {
                        "ts": "2026-05-03T09:01:00Z",
                        "market_id": "BTC_GT_100_20260503_0915",
                        "outcome": "3",
                        "underlying": "BTC",
                        "edge_type": "MODEL",
                        "side": "BUY_YES",
                        "gross_edge": "0.05",
                        "net_edge": "0.03",
                        "confidence": "0.55",
                        "requested_size_usdc": "50",
                        "yes_ask": "0.4",
                        "no_ask": "",
                        "ref_price": "101",
                        "strike": "100",
                        "time_to_expiry": "900",
                        "reason": "mainnet observer signal",
                    }
                ],
            )

            payload = analyze_profiles(
                {"testnet": testnet, "mainnet": mainnet},
                thresholds=ReviewThresholds(
                    min_testnet_settlements=2,
                    min_testnet_markets=2,
                    min_testnet_days=1,
                    min_mainnet_opportunities=1,
                    min_calibration_samples=2,
                    min_profit_factor=0.5,
                    max_brier_score=0.5,
                ),
            )
            markdown = render_markdown(payload)

            self.assertEqual(len(payload["profiles"]), 2)
            self.assertIn("HIP-4 Outcome Run Review", markdown)
            self.assertIn("Guardrail Candidates", markdown)
            self.assertIn("testnet", markdown)
            self.assertIn("mainnet", markdown)

    def _write_sample_logs(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._write_csv(
            root / "opportunities.csv",
            [
                {
                    "ts": "2026-05-03T08:00:00Z",
                    "market_id": "BTC_GT_100_20260503_0815",
                    "outcome": "1",
                    "underlying": "BTC",
                    "edge_type": "MODEL",
                    "side": "BUY_YES",
                    "gross_edge": "0.2",
                    "net_edge": "0.18",
                    "confidence": "0.7",
                    "requested_size_usdc": "10",
                    "yes_ask": "0.6",
                    "no_ask": "",
                    "ref_price": "101",
                    "strike": "100",
                    "time_to_expiry": "600",
                    "reason": "model",
                },
                {
                    "ts": "2026-05-03T08:10:00Z",
                    "market_id": "HYPE_GT_50_20260503_0815",
                    "outcome": "2",
                    "underlying": "HYPE",
                    "edge_type": "SHORT_EXPIRY",
                    "side": "BUY_NO",
                    "gross_edge": "0.2",
                    "net_edge": "0.18",
                    "confidence": "0.8",
                    "requested_size_usdc": "10",
                    "yes_ask": "",
                    "no_ask": "0.55",
                    "ref_price": "49",
                    "strike": "50",
                    "time_to_expiry": "200",
                    "reason": "short",
                },
            ],
        )
        self._write_csv(
            root / "trades.csv",
            [
                {
                    "ts": "2026-05-03T08:00:01Z",
                    "market_id": "BTC_GT_100_20260503_0815",
                    "outcome": "1",
                    "underlying": "BTC",
                    "edge_type": "MODEL",
                    "side": "BUY_YES",
                    "coin": "#10",
                    "price": "0.6",
                    "size_usdc": "6",
                    "token_qty": "10",
                    "status": "testnet_filled",
                    "oid": "1",
                    "cloid": "",
                },
                {
                    "ts": "2026-05-03T08:10:01Z",
                    "market_id": "HYPE_GT_50_20260503_0815",
                    "outcome": "2",
                    "underlying": "HYPE",
                    "edge_type": "SHORT_EXPIRY",
                    "side": "BUY_NO",
                    "coin": "#21",
                    "price": "0.55",
                    "size_usdc": "5.5",
                    "token_qty": "10",
                    "status": "testnet_filled",
                    "oid": "2",
                    "cloid": "",
                },
            ],
        )
        self._write_csv(
            root / "settlements.csv",
            [
                {
                    "ts": "2026-05-03T08:16:00Z",
                    "market_id": "BTC_GT_100_20260503_0815",
                    "outcome": "1",
                    "underlying": "BTC",
                    "side": "BUY_YES",
                    "result": "YES",
                    "payout_usdc": "10",
                    "fee_usdc": "0",
                    "gross_pnl_usdc": "3",
                    "net_pnl_usdc": "3",
                    "pnl_usdc": "3",
                    "is_win": "true",
                    "notes": "exchange_settlement",
                },
                {
                    "ts": "2026-05-03T08:17:00Z",
                    "market_id": "HYPE_GT_50_20260503_0815",
                    "outcome": "2",
                    "underlying": "HYPE",
                    "side": "BUY_NO",
                    "result": "YES",
                    "payout_usdc": "0",
                    "fee_usdc": "0",
                    "gross_pnl_usdc": "-4",
                    "net_pnl_usdc": "-4",
                    "pnl_usdc": "-4",
                    "is_win": "false",
                    "notes": "exchange_settlement",
                },
            ],
        )
        self._write_csv(
            root / "edge_decay.csv",
            [
                {
                    "ts": "2026-05-03T08:00:00Z",
                    "market_id": "BTC_GT_100_20260503_0815",
                    "underlying": "BTC",
                    "edge_type": "MODEL",
                    "side": "BUY_YES",
                    "first_seen_at": "2026-05-03T08:00:00Z",
                    "first_net_edge": "0.18",
                    "current_net_edge": "0.18",
                    "delta_net_edge": "0",
                    "elapsed_seconds": "0",
                    "ref_price": "101",
                    "yes_ask": "0.6",
                    "no_ask": "",
                    "source_count": "2",
                }
            ],
        )
        self._write_jsonl(
            root / "decisions.jsonl",
            [
                self._decision(
                    ts="2026-05-03T08:00:00Z",
                    market_id="BTC_GT_100_20260503_0815",
                    underlying="BTC",
                    edge_type="MODEL",
                    side="BUY_YES",
                    probability_yes=0.8,
                    seconds_left=600,
                ),
                self._decision(
                    ts="2026-05-03T08:10:00Z",
                    market_id="HYPE_GT_50_20260503_0815",
                    underlying="HYPE",
                    edge_type="SHORT_EXPIRY",
                    side="BUY_NO",
                    probability_yes=0.2,
                    seconds_left=200,
                ),
            ],
        )

    def _decision(
        self,
        *,
        ts: str,
        market_id: str,
        underlying: str,
        edge_type: str,
        side: str,
        probability_yes: float,
        seconds_left: int,
    ) -> dict[str, object]:
        return {
            "ts": ts,
            "pod": "HIP4OutcomeEdgePod",
            "signal": {
                "market_id": market_id,
                "underlying": underlying,
                "edge_type": edge_type,
                "side": side,
                "confidence": 0.8,
                "net_edge": 0.18,
                "metadata": {
                    "probability_yes": probability_yes,
                    "short_probability_yes": probability_yes,
                    "time_to_expiry_seconds": seconds_left,
                    "reference_source_count": 2,
                    "reference_max_deviation_bps": 0,
                },
            },
            "supervisor_decision": {
                "approved": True,
                "approved_size_usdc": 10,
                "execution_mode": "TESTNET",
                "reason": "local_outcome_risk_ok",
                "constraints": {},
            },
        }

    def _write_csv(self, path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
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
