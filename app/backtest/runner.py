from __future__ import annotations

import argparse

from app.backtest.pod_a_runner import PodABacktestRunner
from app.settings import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TRIDENT backtest runner")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True, help="Path to JSONL input snapshots")
    parser.add_argument("--output", help="Optional JSONL output journal path")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    result = PodABacktestRunner(config).run_jsonl(args.input, args.output)
    print(f"records_processed={result.records_processed}")
    print(f"signal_count={result.signal_count}")
    print(f"accepted_count={result.accepted_count}")
    print(f"rejected_count={result.rejected_count}")
    print(f"opened_count={result.opened_count}")
    print(f"skipped_open_count={result.skipped_open_count}")
    print(f"closed_trade_count={result.closed_trade_count}")
    print(f"win_count={result.win_count}")
    print(f"loss_count={result.loss_count}")
    print(f"realized_pnl_usd={result.realized_pnl_usd}")
    print(f"gross_pnl_usd={result.gross_pnl_usd}")
    print(f"fees_usd={result.fees_usd}")
    print(f"average_hold_hours={result.average_hold_hours}")
    print(f"records_by_regime={result.records_by_regime}")
    print(f"signals_by_symbol={result.signals_by_symbol}")
    print(f"signals_by_side={result.signals_by_side}")
    print(f"signals_by_setup={result.signals_by_setup}")
    print(f"signals_by_regime={result.signals_by_regime}")
    print(f"rejections_by_reason={result.rejections_by_reason}")
    print(f"close_reasons={result.close_reasons}")
    print(f"trades_by_symbol={result.trades_by_symbol}")
    print(f"pnl_by_symbol={result.pnl_by_symbol}")
    print(f"average_confidence={result.average_confidence}")
    if result.output_path:
        print(f"output_path={result.output_path}")


if __name__ == "__main__":
    main()
