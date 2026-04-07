import unittest

from app.backtest.comparative_analysis import build_backtest_comparative_summary


class ComparativeAnalysisTests(unittest.TestCase):
    def test_builds_trade_stats_by_cluster_symbol_and_regime(self) -> None:
        summary = build_backtest_comparative_summary(
            {
                "realized_pnl_usd": 4.0,
                "gross_pnl_usd": 5.0,
                "fees_usd": 1.0,
                "max_drawdown_usd": 2.0,
                "closed_trade_count": 3,
                "signal_count": 6,
                "records_by_date": {"2026-04-04": 2, "2026-04-05": 2},
                "closed_trade_log": [
                    {
                        "symbol": "BTC",
                        "market_cluster": "crypto",
                        "close_regime": "TrendExpansion",
                        "pnl_usd": 5.0,
                        "gross_pnl_usd": 5.5,
                        "fees_usd": 0.5,
                    },
                    {
                        "symbol": "SPX",
                        "market_cluster": "index",
                        "close_regime": "TrendExpansion",
                        "pnl_usd": -1.0,
                        "gross_pnl_usd": -0.7,
                        "fees_usd": 0.3,
                    },
                    {
                        "symbol": "SPX",
                        "market_cluster": "index",
                        "close_regime": "RangeAuction",
                        "pnl_usd": 0.0,
                        "gross_pnl_usd": 0.2,
                        "fees_usd": 0.2,
                    },
                ],
            }
        )

        self.assertEqual(summary["summary"]["closed_trades_per_day"], 1.5)
        self.assertEqual(summary["summary"]["signals_per_day"], 3.0)
        self.assertEqual(summary["summary"]["trade_stats"]["expectancy_usd"], 1.3333)
        self.assertEqual(summary["by_cluster"]["crypto"]["closed_trade_count"], 1)
        self.assertEqual(summary["by_cluster"]["index"]["closed_trade_count"], 2)
        self.assertEqual(summary["by_symbol"]["SPX"]["win_count"], 1)
        self.assertEqual(summary["by_regime"]["TrendExpansion"]["closed_trade_count"], 2)


if __name__ == "__main__":
    unittest.main()
