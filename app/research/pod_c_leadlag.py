from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader
from app.trident.types import SymbolMarketSnapshot, symbol_market_snapshot_from_mapping


@dataclass(slots=True)
class LeadLagStudyResult:
    leader_symbol: str
    follower_symbols: list[str]
    impulse_threshold_bps: float
    horizon_bars: int
    sample_count_by_symbol: dict[str, int]
    expectancy_bps_by_symbol: dict[str, float]
    hit_rate_by_symbol: dict[str, float]
    best_symbol: str | None
    best_expectancy_bps: float
    recommendation: str
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class LeadLagResearchRunner:
    """Measures follower expectancy after leader impulse events on TRIDENT snapshots."""

    def __init__(self) -> None:
        self.loader = SnapshotLoader()

    def run(
        self,
        *,
        input_path: str | Path,
        leader_symbol: str,
        follower_symbols: list[str],
        impulse_threshold_bps: float = 8.0,
        horizon_bars: int = 2,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
    ) -> LeadLagStudyResult:
        leader_symbol = leader_symbol.upper()
        follower_symbols = [symbol.upper() for symbol in follower_symbols]
        records = list(self.loader.iter_jsonl(input_path))
        snapshot_matrix: list[dict[str, SymbolMarketSnapshot]] = [
            {
                item["symbol"].upper(): symbol_market_snapshot_from_mapping(item)
                for item in record.symbols
                if isinstance(item, dict)
            }
            for record in records
        ]

        leader_returns: list[float] = []
        for index in range(1, len(snapshot_matrix)):
            previous = snapshot_matrix[index - 1].get(leader_symbol)
            current = snapshot_matrix[index].get(leader_symbol)
            if previous is None or current is None or previous.price <= 0:
                leader_returns.append(0.0)
                continue
            leader_returns.append((current.price - previous.price) / previous.price * 10_000.0)

        samples: dict[str, list[float]] = {symbol: [] for symbol in follower_symbols}
        hits: dict[str, int] = {symbol: 0 for symbol in follower_symbols}

        for index in range(1, len(snapshot_matrix) - horizon_bars):
            leader_move_bps = leader_returns[index - 1]
            if abs(leader_move_bps) < impulse_threshold_bps:
                continue
            for symbol in follower_symbols:
                current = snapshot_matrix[index].get(symbol)
                future = snapshot_matrix[index + horizon_bars].get(symbol)
                if current is None or future is None or current.price <= 0:
                    continue
                follower_return_bps = (future.price - current.price) / current.price * 10_000.0
                aligned_return_bps = follower_return_bps if leader_move_bps > 0 else -follower_return_bps
                samples[symbol].append(aligned_return_bps)
                if aligned_return_bps > 0:
                    hits[symbol] += 1

        sample_count_by_symbol = {symbol: len(values) for symbol, values in samples.items()}
        expectancy_bps_by_symbol = {
            symbol: round(sum(values) / len(values), 4) if values else 0.0
            for symbol, values in samples.items()
        }
        hit_rate_by_symbol = {
            symbol: round(hits[symbol] / len(values), 4) if values else 0.0
            for symbol, values in samples.items()
        }

        best_symbol = None
        best_expectancy_bps = 0.0
        for symbol, expectancy in expectancy_bps_by_symbol.items():
            if sample_count_by_symbol[symbol] < 3:
                continue
            if best_symbol is None or expectancy > best_expectancy_bps:
                best_symbol = symbol
                best_expectancy_bps = expectancy

        recommendation = "no-go"
        rationale = "No follower showed robust positive expectancy."
        if best_symbol is not None and best_expectancy_bps > 2.0 and hit_rate_by_symbol[best_symbol] >= 0.55:
            recommendation = "go"
            rationale = (
                f"{best_symbol} shows positive aligned expectancy after {leader_symbol} impulse "
                f"with expectancy {best_expectancy_bps:.4f} bps and hit rate {hit_rate_by_symbol[best_symbol]:.2%}."
            )

        result = LeadLagStudyResult(
            leader_symbol=leader_symbol,
            follower_symbols=follower_symbols,
            impulse_threshold_bps=impulse_threshold_bps,
            horizon_bars=horizon_bars,
            sample_count_by_symbol=sample_count_by_symbol,
            expectancy_bps_by_symbol=expectancy_bps_by_symbol,
            hit_rate_by_symbol=hit_rate_by_symbol,
            best_symbol=best_symbol,
            best_expectancy_bps=round(best_expectancy_bps, 4),
            recommendation=recommendation,
            rationale=rationale,
        )
        if output_json is not None:
            path = Path(output_json)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        if output_md is not None:
            path = Path(output_md)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._render_markdown(result), encoding="utf-8")
        return result

    def _render_markdown(self, result: LeadLagStudyResult) -> str:
        lines = [
            "# Pod C Research Memo",
            "",
            f"- Leader: {result.leader_symbol}",
            f"- Followers: {', '.join(result.follower_symbols)}",
            f"- Impulse threshold bps: {result.impulse_threshold_bps}",
            f"- Horizon bars: {result.horizon_bars}",
            f"- Recommendation: {result.recommendation}",
            f"- Rationale: {result.rationale}",
            "",
            "| Symbol | Samples | Expectancy (bps) | Hit rate |",
            "|--------|---------|------------------|----------|",
        ]
        for symbol in result.follower_symbols:
            lines.append(
                f"| {symbol} | {result.sample_count_by_symbol.get(symbol, 0)} | "
                f"{result.expectancy_bps_by_symbol.get(symbol, 0.0)} | "
                f"{result.hit_rate_by_symbol.get(symbol, 0.0)} |"
            )
        lines.append("")
        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Study lead-lag expectancy for Pod C")
    parser.add_argument("--input", required=True)
    parser.add_argument("--leader-symbol", required=True)
    parser.add_argument("--follower-symbols", required=True, help="Comma-separated list")
    parser.add_argument("--impulse-threshold-bps", type=float, default=8.0)
    parser.add_argument("--horizon-bars", type=int, default=2)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runner = LeadLagResearchRunner()
    result = runner.run(
        input_path=args.input,
        leader_symbol=args.leader_symbol,
        follower_symbols=[symbol.strip().upper() for symbol in args.follower_symbols.split(",") if symbol.strip()],
        impulse_threshold_bps=args.impulse_threshold_bps,
        horizon_bars=args.horizon_bars,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(f"best_symbol={result.best_symbol}")
    print(f"best_expectancy_bps={result.best_expectancy_bps}")
    print(f"recommendation={result.recommendation}")


if __name__ == "__main__":
    main()
