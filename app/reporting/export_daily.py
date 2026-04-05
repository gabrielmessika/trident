from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class DailyExportResult:
    output_json: str | None
    output_md: str | None
    total_realized_pnl_usd: float
    total_unrealized_pnl_usd: float
    max_drawdown_usd: float
    reconciliation_gap_usd: float

    def to_dict(self) -> dict[str, object]:
        return {
            "output_json": self.output_json,
            "output_md": self.output_md,
            "total_realized_pnl_usd": self.total_realized_pnl_usd,
            "total_unrealized_pnl_usd": self.total_unrealized_pnl_usd,
            "max_drawdown_usd": self.max_drawdown_usd,
            "reconciliation_gap_usd": self.reconciliation_gap_usd,
        }


def build_daily_summary(
    *,
    pod_a_report: dict[str, object] | None = None,
    pod_b_report: dict[str, object] | None = None,
    runtime_report: dict[str, object] | None = None,
    reference_equity_usd: float = 1000.0,
    cash_balance_usd: float | None = None,
    actual_total_equity_usd: float | None = None,
) -> dict[str, object]:
    pod_a_report = pod_a_report or {}
    pod_b_report = pod_b_report or {}
    runtime_report = runtime_report or {}

    pod_a_realized = float(pod_a_report.get("realized_pnl_usd", 0.0))
    pod_b_realized = float(pod_b_report.get("realized_pnl_usd", 0.0))
    pod_b_unrealized = float(pod_b_report.get("total_unrealized_pnl_usd", 0.0))
    total_realized = round(pod_a_realized + pod_b_realized, 4)
    total_unrealized = round(pod_b_unrealized, 4)
    estimated_total_equity = round(reference_equity_usd + total_realized + total_unrealized, 4)
    if actual_total_equity_usd is not None:
        actual_total_equity = round(actual_total_equity_usd, 4)
    elif cash_balance_usd is not None:
        actual_total_equity = round(cash_balance_usd + total_unrealized, 4)
    else:
        actual_total_equity = estimated_total_equity
    reconciliation_gap = round(actual_total_equity - estimated_total_equity, 4)
    max_drawdown_usd = round(
        max(
            float(pod_a_report.get("max_drawdown_usd", 0.0)),
            float(pod_b_report.get("max_drawdown_usd", 0.0)),
        ),
        4,
    )
    runtime_cash_usd = float(runtime_report.get("cash_usd", 0.0)) if runtime_report else 0.0
    runtime_total_target_usd = (
        float(runtime_report.get("total_target_usd", 0.0)) if runtime_report else 0.0
    )

    return {
        "reference_equity_usd": reference_equity_usd,
        "actual_total_equity_usd": actual_total_equity,
        "cash_balance_usd": cash_balance_usd,
        "pods": {
            "pod_a": {
                "realized_pnl_usd": pod_a_realized,
                "max_drawdown_usd": float(pod_a_report.get("max_drawdown_usd", 0.0)),
                "closed_trade_count": int(pod_a_report.get("closed_trade_count", 0)),
            },
            "pod_b": {
                "realized_pnl_usd": pod_b_realized,
                "total_unrealized_pnl_usd": pod_b_unrealized,
                "max_drawdown_usd": float(pod_b_report.get("max_drawdown_usd", 0.0)),
                "total_fill_count": int(pod_b_report.get("total_fill_count", 0)),
            },
        },
        "runtime": {
            "cash_usd": round(runtime_cash_usd, 4),
            "total_target_usd": round(runtime_total_target_usd, 4),
        },
        "total_realized_pnl_usd": total_realized,
        "total_unrealized_pnl_usd": total_unrealized,
        "max_drawdown_usd": max_drawdown_usd,
        "estimated_total_equity_usd": estimated_total_equity,
        "reconciliation_gap_usd": reconciliation_gap,
    }


def render_daily_markdown(summary: dict[str, object]) -> str:
    pods = summary["pods"]
    return "\n".join(
        [
            "# TRIDENT Daily Summary",
            "",
            f"- Reference equity USD: {summary['reference_equity_usd']}",
            f"- Total realized PnL USD: {summary['total_realized_pnl_usd']}",
            f"- Total unrealized PnL USD: {summary['total_unrealized_pnl_usd']}",
            f"- Max drawdown USD: {summary['max_drawdown_usd']}",
            f"- Estimated total equity USD: {summary['estimated_total_equity_usd']}",
            f"- Actual total equity USD: {summary['actual_total_equity_usd']}",
            f"- Reconciliation gap USD: {summary['reconciliation_gap_usd']}",
            f"- Runtime cash USD: {summary['runtime']['cash_usd']}",
            f"- Runtime total target USD: {summary['runtime']['total_target_usd']}",
            "",
            "## Pods",
            "",
            f"- Pod A realized PnL USD: {pods['pod_a']['realized_pnl_usd']}",
            f"- Pod A max drawdown USD: {pods['pod_a']['max_drawdown_usd']}",
            f"- Pod A closed trade count: {pods['pod_a']['closed_trade_count']}",
            f"- Pod B realized PnL USD: {pods['pod_b']['realized_pnl_usd']}",
            f"- Pod B unrealized PnL USD: {pods['pod_b']['total_unrealized_pnl_usd']}",
            f"- Pod B max drawdown USD: {pods['pod_b']['max_drawdown_usd']}",
            f"- Pod B total fill count: {pods['pod_b']['total_fill_count']}",
            "",
        ]
    ) + "\n"


def _load_optional_json(path: str | Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload.get("backtest", payload.get("report", payload))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a consolidated TRIDENT daily summary")
    parser.add_argument("--pod-a-report")
    parser.add_argument("--pod-b-report")
    parser.add_argument("--runtime-report")
    parser.add_argument("--reference-equity-usd", type=float, default=1000.0)
    parser.add_argument("--cash-balance-usd", type=float)
    parser.add_argument("--actual-total-equity-usd", type=float)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    summary = build_daily_summary(
        pod_a_report=_load_optional_json(args.pod_a_report),
        pod_b_report=_load_optional_json(args.pod_b_report),
        runtime_report=_load_optional_json(args.runtime_report),
        reference_equity_usd=args.reference_equity_usd,
        cash_balance_usd=args.cash_balance_usd,
        actual_total_equity_usd=args.actual_total_equity_usd,
    )
    result = DailyExportResult(
        output_json=args.output_json,
        output_md=args.output_md,
        total_realized_pnl_usd=float(summary["total_realized_pnl_usd"]),
        total_unrealized_pnl_usd=float(summary["total_unrealized_pnl_usd"]),
        max_drawdown_usd=float(summary["max_drawdown_usd"]),
        reconciliation_gap_usd=float(summary["reconciliation_gap_usd"]),
    )
    if args.output_json:
        output = Path(args.output_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if args.output_md:
        output = Path(args.output_md)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_daily_markdown(summary), encoding="utf-8")
    print(f"total_realized_pnl_usd={result.total_realized_pnl_usd}")
    print(f"total_unrealized_pnl_usd={result.total_unrealized_pnl_usd}")
    print(f"max_drawdown_usd={result.max_drawdown_usd}")
    print(f"reconciliation_gap_usd={result.reconciliation_gap_usd}")


if __name__ == "__main__":
    main()
