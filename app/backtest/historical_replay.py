"""End-to-end historical replay: fetch candles/funding from HL API, convert, backtest."""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from app.backtest.candle_converter import CandleToSnapshotConverter
from app.backtest.pod_a_runner import PodABacktestResult, PodABacktestRunner
from app.hyperliquid.historical_fetcher import HyperliquidHistoricalFetcher
from app.hyperliquid.info_client import apply_live_asset_leverage_caps
from app.settings import AppConfig, load_config, override_app_config


@dataclass(slots=True)
class HistoricalReplayResult:
    dates: list[str]
    coins: list[str]
    interval: str
    candle_dir: str
    funding_dir: str | None
    snapshot_dir: str
    snapshot_records_written: int
    snapshot_files_written: int
    reference_equity_usd: float
    pod_a_default_leverage: float
    pod_a_max_leverage: float
    pod_a_risk_per_trade_pct: float
    fetch_candle_requests: int
    fetch_candles_total: int
    fetch_funding_requests: int
    fetch_funding_total: int
    backtest: PodABacktestResult
    report_path: str | None = None
    journal_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["backtest"] = asdict(self.backtest)
        return payload


class HistoricalReplayRunner:
    """Fetches historical data from Hyperliquid, converts to snapshots, runs backtest."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run(
        self,
        *,
        dates: list[str],
        coins: list[str],
        interval: str = "1h",
        candle_dir: str | Path | None = None,
        funding_dir: str | Path | None = None,
        snapshot_dir: str | Path | None = None,
        journal_output: str | Path | None = None,
        report_output: str | Path | None = None,
        skip_fetch: bool = False,
        skip_funding: bool = False,
        reference_equity_usd: float | None = None,
        pod_a_default_leverage: float | None = None,
        pod_a_max_leverage: float | None = None,
        pod_a_risk_per_trade_pct: float | None = None,
        use_live_asset_caps: bool = False,
    ) -> HistoricalReplayResult:
        start_date = date.fromisoformat(min(dates))
        end_date = date.fromisoformat(max(dates))

        candle_path = Path(candle_dir or "data/historical_candles")
        funding_path = Path(funding_dir or "data/historical_funding") if not skip_funding else None

        # --- Step 1: Fetch data from API ---
        fetch_stats_candles = 0
        fetch_stats_candle_req = 0
        fetch_stats_funding = 0
        fetch_stats_funding_req = 0

        if not skip_fetch:
            fetcher = HyperliquidHistoricalFetcher(self.config.hyperliquid)

            fetcher.fetch_candles(
                coins=coins,
                start_date=start_date,
                end_date=end_date,
                interval=interval,
                output_dir=candle_path,
            )
            fetch_stats_candles = fetcher.stats.candles_fetched
            fetch_stats_candle_req = fetcher.stats.candle_requests

            if funding_path is not None:
                fetcher.fetch_funding(
                    coins=coins,
                    start_date=start_date,
                    end_date=end_date,
                    output_dir=funding_path,
                )
                fetch_stats_funding = fetcher.stats.funding_records_fetched
                fetch_stats_funding_req = fetcher.stats.funding_requests

        # --- Step 2: Convert candles to snapshots ---
        converter = CandleToSnapshotConverter()
        snapshot_dir_path: Path
        if snapshot_dir is None:
            snapshot_dir_path = Path(tempfile.mkdtemp(prefix="trident_hist_snapshots_"))
        else:
            snapshot_dir_path = Path(snapshot_dir)
            snapshot_dir_path.mkdir(parents=True, exist_ok=True)

        snapshot_records_written = 0
        snapshot_files_written = 0
        for replay_date in dates:
            output_file = snapshot_dir_path / f"{replay_date}.jsonl"
            written = converter.convert(
                candle_dir=candle_path,
                funding_dir=funding_path,
                date=replay_date,
                coins=coins,
                interval=interval,
                output_path=output_file,
            )
            if written <= 0:
                if output_file.exists():
                    output_file.unlink()
                continue
            snapshot_records_written += written
            snapshot_files_written += 1

        # --- Step 3: Run backtest ---
        runtime_config = override_app_config(
            self.config,
            reference_equity_usd=reference_equity_usd,
            pod_a_default_leverage=pod_a_default_leverage,
            pod_a_max_leverage=pod_a_max_leverage,
            pod_a_risk_per_trade_pct=pod_a_risk_per_trade_pct,
        )
        if use_live_asset_caps:
            runtime_config = apply_live_asset_leverage_caps(
                runtime_config,
                symbols=coins,
                sleep_fn=lambda _: None,
            )

        backtest_runner = PodABacktestRunner(runtime_config)
        backtest = backtest_runner.run_jsonl(snapshot_dir_path, journal_output)

        result = HistoricalReplayResult(
            dates=dates,
            coins=coins,
            interval=interval,
            candle_dir=str(candle_path),
            funding_dir=str(funding_path) if funding_path else None,
            snapshot_dir=str(snapshot_dir_path),
            snapshot_records_written=snapshot_records_written,
            snapshot_files_written=snapshot_files_written,
            reference_equity_usd=runtime_config.trident.capital.reference_equity_usd,
            pod_a_default_leverage=runtime_config.pod_a.default_leverage,
            pod_a_max_leverage=runtime_config.pod_a.max_leverage,
            pod_a_risk_per_trade_pct=runtime_config.pod_a.risk_per_trade_pct,
            fetch_candle_requests=fetch_stats_candle_req,
            fetch_candles_total=fetch_stats_candles,
            fetch_funding_requests=fetch_stats_funding_req,
            fetch_funding_total=fetch_stats_funding,
            backtest=backtest,
            report_path=str(report_output) if report_output else None,
            journal_path=str(journal_output) if journal_output else None,
        )

        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )

        return result


def parse_dates(*, date_from: str, date_to: str | None) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to) if date_to is not None else start
    if end < start:
        raise ValueError("date_to must be >= date_from")
    current = start
    dates: list[str] = []
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Historical replay: fetch HL candles/funding → convert → backtest"
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--date-from", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--date-to", help="End date YYYY-MM-DD (default: same as date-from)")
    parser.add_argument("--coins", required=True, help="Comma-separated, e.g. BTC,ETH,SOL")
    parser.add_argument(
        "--interval", default="1h", help="Candle interval: 1m, 5m, 15m, 1h, 4h, 1d"
    )
    parser.add_argument("--candle-dir", default="data/historical_candles")
    parser.add_argument("--funding-dir", default="data/historical_funding")
    parser.add_argument("--snapshot-dir", help="Optional directory for generated snapshots")
    parser.add_argument("--journal-output", help="Optional JSONL journal path")
    parser.add_argument("--report-output", help="Optional JSON report path")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip API fetch, use existing local data",
    )
    parser.add_argument(
        "--skip-funding",
        action="store_true",
        help="Skip funding rate fetch/integration",
    )
    parser.add_argument("--reference-equity-usd", type=float)
    parser.add_argument("--pod-a-default-leverage", type=float)
    parser.add_argument("--pod-a-max-leverage", type=float)
    parser.add_argument("--pod-a-risk-per-trade-pct", type=float)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    result = HistoricalReplayRunner(config).run(
        dates=parse_dates(date_from=args.date_from, date_to=args.date_to),
        coins=[c.strip().upper() for c in args.coins.split(",") if c.strip()],
        interval=args.interval,
        candle_dir=args.candle_dir,
        funding_dir=args.funding_dir,
        snapshot_dir=args.snapshot_dir,
        journal_output=args.journal_output,
        report_output=args.report_output,
        skip_fetch=args.skip_fetch,
        skip_funding=args.skip_funding,
        reference_equity_usd=args.reference_equity_usd,
        pod_a_default_leverage=args.pod_a_default_leverage,
        pod_a_max_leverage=args.pod_a_max_leverage,
        pod_a_risk_per_trade_pct=args.pod_a_risk_per_trade_pct,
        use_live_asset_caps=True,
    )
    print(f"dates={result.dates}")
    print(f"coins={result.coins}")
    print(f"interval={result.interval}")
    print(f"fetch_candles={result.fetch_candles_total} (requests={result.fetch_candle_requests})")
    print(f"fetch_funding={result.fetch_funding_total} (requests={result.fetch_funding_requests})")
    print(f"snapshot_files_written={result.snapshot_files_written}")
    print(f"snapshot_records_written={result.snapshot_records_written}")
    print(f"records_processed={result.backtest.records_processed}")
    print(f"signal_count={result.backtest.signal_count}")
    print(f"accepted_count={result.backtest.accepted_count}")
    print(f"opened_count={result.backtest.opened_count}")
    print(f"closed_trade_count={result.backtest.closed_trade_count}")
    print(f"realized_pnl_usd={result.backtest.realized_pnl_usd}")
    print(f"gross_pnl_usd={result.backtest.gross_pnl_usd}")
    print(f"fees_usd={result.backtest.fees_usd}")
    print(f"average_hold_hours={result.backtest.average_hold_hours}")
    print(f"pnl_by_date={result.backtest.pnl_by_date}")
    print(f"pnl_by_symbol={result.backtest.pnl_by_symbol}")
    print(f"trades_by_symbol={result.backtest.trades_by_symbol}")
    if result.report_path:
        print(f"report_path={result.report_path}")
    if result.journal_path:
        print(f"journal_path={result.journal_path}")


if __name__ == "__main__":
    main()
