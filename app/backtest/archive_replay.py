from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from app.backtest.gbot_converter import GbotL2ToTridentConverter
from app.backtest.pod_a_runner import PodABacktestResult, PodABacktestRunner
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class ArchiveReplayResult:
    data_dir: str
    dates: list[str]
    coins: list[str]
    snapshot_dir: str
    snapshot_records_written: int
    snapshot_files_written: int
    backtest: PodABacktestResult
    report_path: str | None = None
    journal_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["backtest"] = asdict(self.backtest)
        return payload


class ArchiveReplayRunner:
    """Converts archived gbot datasets into snapshots and replays Pod A on them."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run(
        self,
        *,
        data_dir: str | Path,
        dates: list[str],
        coins: list[str],
        snapshot_dir: str | Path | None = None,
        journal_output: str | Path | None = None,
        report_output: str | Path | None = None,
        bucket_ms: int = 60_000,
    ) -> ArchiveReplayResult:
        converter = GbotL2ToTridentConverter(bucket_ms=bucket_ms)
        backtest_runner = PodABacktestRunner(self.config)

        snapshot_dir_path: Path
        if snapshot_dir is None:
            snapshot_dir_path = Path(tempfile.mkdtemp(prefix="trident_snapshots_"))
        else:
            snapshot_dir_path = Path(snapshot_dir)
            snapshot_dir_path.mkdir(parents=True, exist_ok=True)

        snapshot_records_written = 0
        snapshot_files_written = 0
        for replay_date in dates:
            output_path = snapshot_dir_path / f"{replay_date}.jsonl"
            written = converter.convert(
                data_dir=data_dir,
                date=replay_date,
                coins=coins,
                output_path=output_path,
            )
            if written <= 0:
                if output_path.exists():
                    output_path.unlink()
                continue
            snapshot_records_written += written
            snapshot_files_written += 1

        backtest = backtest_runner.run_jsonl(snapshot_dir_path, journal_output)
        result = ArchiveReplayResult(
            data_dir=str(data_dir),
            dates=dates,
            coins=coins,
            snapshot_dir=str(snapshot_dir_path),
            snapshot_records_written=snapshot_records_written,
            snapshot_files_written=snapshot_files_written,
            backtest=backtest,
            report_path=str(report_output) if report_output is not None else None,
            journal_path=str(journal_output) if journal_output is not None else None,
        )

        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")

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
    parser = argparse.ArgumentParser(description="Replay archived gbot data through TRIDENT Pod A")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument(
        "--data-dir",
        default="data/server_archive",
        help="Archive root containing l2/ and trades/",
    )
    parser.add_argument("--date-from", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--date-to", help="Optional end date YYYY-MM-DD")
    parser.add_argument("--coins", required=True, help="Comma-separated list, e.g. BTC,ETH,SOL")
    parser.add_argument("--snapshot-dir", help="Optional directory for generated snapshots")
    parser.add_argument("--journal-output", help="Optional JSONL journal path")
    parser.add_argument("--report-output", help="Optional JSON report path")
    parser.add_argument("--bucket-ms", type=int, default=60_000)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    result = ArchiveReplayRunner(config).run(
        data_dir=args.data_dir,
        dates=parse_dates(date_from=args.date_from, date_to=args.date_to),
        coins=[coin.strip().upper() for coin in args.coins.split(",") if coin.strip()],
        snapshot_dir=args.snapshot_dir,
        journal_output=args.journal_output,
        report_output=args.report_output,
        bucket_ms=args.bucket_ms,
    )
    print(f"dates={result.dates}")
    print(f"coins={result.coins}")
    print(f"snapshot_dir={result.snapshot_dir}")
    print(f"snapshot_files_written={result.snapshot_files_written}")
    print(f"snapshot_records_written={result.snapshot_records_written}")
    print(f"records_processed={result.backtest.records_processed}")
    print(f"signal_count={result.backtest.signal_count}")
    print(f"accepted_count={result.backtest.accepted_count}")
    print(f"rejected_count={result.backtest.rejected_count}")
    print(f"opened_count={result.backtest.opened_count}")
    print(f"skipped_open_count={result.backtest.skipped_open_count}")
    print(f"closed_trade_count={result.backtest.closed_trade_count}")
    print(f"realized_pnl_usd={result.backtest.realized_pnl_usd}")
    print(f"gross_pnl_usd={result.backtest.gross_pnl_usd}")
    print(f"fees_usd={result.backtest.fees_usd}")
    print(f"average_hold_hours={result.backtest.average_hold_hours}")
    print(f"records_by_regime={result.backtest.records_by_regime}")
    print(f"records_by_date={result.backtest.records_by_date}")
    print(f"signals_by_date={result.backtest.signals_by_date}")
    print(f"accepted_by_date={result.backtest.accepted_by_date}")
    print(f"rejected_by_date={result.backtest.rejected_by_date}")
    print(f"regime_transition_count={result.backtest.regime_transition_count}")
    print(f"regime_transitions={result.backtest.regime_transitions}")
    print(f"regime_transitions_by_date={result.backtest.regime_transitions_by_date}")
    print(f"trades_by_symbol={result.backtest.trades_by_symbol}")
    print(f"pnl_by_symbol={result.backtest.pnl_by_symbol}")
    print(f"pnl_by_date={result.backtest.pnl_by_date}")
    if result.journal_path:
        print(f"journal_path={result.journal_path}")
    if result.report_path:
        print(f"report_path={result.report_path}")


if __name__ == "__main__":
    main()
