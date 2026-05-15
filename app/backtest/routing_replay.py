from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader
from app.settings import AppConfig, load_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, SymbolMarketSnapshot, symbol_market_snapshot_from_mapping


@dataclass(slots=True)
class RoutingReplayResult:
    input_path: str
    dedupe_by_timestamp: bool
    records_processed: int
    unique_timestamps_processed: int
    duplicate_timestamps_skipped: int
    first_timestamp: str | None
    last_timestamp: str | None
    max_ownership_conflict_count: int
    final_ownership_conflict_count: int
    initial_assignment_count: int
    reassignment_event_count: int
    deassignment_event_count: int
    unassigned_decision_count: int
    mode_counts: dict[str, int]
    owner_assignment_counts: dict[str, int]
    average_owned_symbols_by_pod: dict[str, float]
    final_owned_symbols_by_pod: dict[str, list[str]]
    average_tradable_pool_size: float
    peak_tradable_pool_size: int
    local_regime_transition_count: int
    divergent_state_count: int
    divergent_symbols: list[str]
    symbol_reassignment_count_by_symbol: dict[str, int]
    max_symbol_reassignment_count: int
    routing_overrides: dict[str, str]
    report_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        total_assignments = sum(self.owner_assignment_counts.values())
        payload["owner_assignment_share_by_pod"] = {
            pod: round(count / total_assignments, 4) if total_assignments > 0 else 0.0
            for pod, count in self.owner_assignment_counts.items()
        }
        payload["divergent_symbol_count"] = len(self.divergent_symbols)
        return payload


class RoutingReplayRunner:
    """Replays supervisor routing decisions on snapshot JSONL input."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.loader = SnapshotLoader()

    def run_jsonl(
        self,
        input_path: str | Path,
        *,
        dedupe_by_timestamp: bool = True,
        report_output: str | Path | None = None,
    ) -> RoutingReplayResult:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-routing-replay",
            mode="observation",
        )
        seen_timestamps: set[str] = set()
        previous_owner_by_symbol: dict[str, str | None] = {}
        previous_local_regime_by_symbol: dict[str, str] = {}
        mode_counts: dict[str, int] = {}
        owner_assignment_counts = {
            PodName.POD_A.value: 0,
            PodName.POD_B.value: 0,
            PodName.POD_C.value: 0,
        }
        owned_symbol_tick_totals = {
            PodName.POD_A.value: 0,
            PodName.POD_B.value: 0,
            PodName.POD_C.value: 0,
        }
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        records_processed = 0
        duplicate_timestamps_skipped = 0
        max_ownership_conflict_count = 0
        initial_assignment_count = 0
        reassignment_event_count = 0
        deassignment_event_count = 0
        unassigned_decision_count = 0
        tradable_pool_size_total = 0
        peak_tradable_pool_size = 0
        local_regime_transition_count = 0
        divergent_state_count = 0
        divergent_symbols: set[str] = set()

        for record in self.loader.iter_merged_jsonl(input_path):
            timestamp = record.timestamp
            if dedupe_by_timestamp and timestamp:
                if timestamp in seen_timestamps:
                    duplicate_timestamps_skipped += 1
                    continue
                seen_timestamps.add(timestamp)
            records_processed += 1
            if first_timestamp is None:
                first_timestamp = timestamp
            last_timestamp = timestamp

            replay_symbols = {
                str(item.get("symbol", "")).strip().upper()
                for item in record.symbols
                if isinstance(item, dict) and str(item.get("symbol", "")).strip()
            }
            if replay_symbols:
                current_universe = {
                    str(symbol).strip().upper()
                    for symbol in (supervisor.config.hyperliquid.observation_universe or [])
                    if str(symbol).strip()
                }
                supervisor.config.hyperliquid.observation_universe = sorted(
                    current_universe | replay_symbols
                )

            supervisor.apply_regime_snapshot(
                RegimeSnapshot(**record.regime_snapshot),
                cluster_regime_snapshots={
                    cluster: RegimeSnapshot(**snap)
                    for cluster, snap in (record.cluster_regime_snapshots or {}).items()
                    if isinstance(snap, dict)
                },
            )
            supervisor.refresh_symbol_routing(
                [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
            )
            snapshot = supervisor.snapshot()

            conflict_count = len(snapshot["ownership_conflicts"])
            if conflict_count > max_ownership_conflict_count:
                max_ownership_conflict_count = conflict_count

            tradable_pool_size = len(snapshot["tradable_pool"])
            tradable_pool_size_total += tradable_pool_size
            peak_tradable_pool_size = max(peak_tradable_pool_size, tradable_pool_size)

            current_owner_by_symbol: dict[str, str | None] = {}
            for item in snapshot["symbol_routing"]:
                if not isinstance(item, dict):
                    continue
                mode = str(item.get("mode") or "unknown")
                mode_counts[mode] = mode_counts.get(mode, 0) + 1
                symbol = str(item.get("symbol") or "")
                owner = item.get("owner")
                normalized_owner = str(owner) if owner is not None else None
                current_owner_by_symbol[symbol] = normalized_owner
                if normalized_owner is None:
                    unassigned_decision_count += 1
                    continue
                owner_assignment_counts[normalized_owner] = (
                    owner_assignment_counts.get(normalized_owner, 0) + 1
                )

            for symbol in sorted(set(previous_owner_by_symbol) | set(current_owner_by_symbol)):
                previous_owner = previous_owner_by_symbol.get(symbol)
                current_owner = current_owner_by_symbol.get(symbol)
                if previous_owner == current_owner:
                    continue
                if previous_owner is None and current_owner is not None:
                    initial_assignment_count += 1
                elif previous_owner is not None and current_owner is None:
                    deassignment_event_count += 1
                elif previous_owner is not None and current_owner is not None:
                    reassignment_event_count += 1
            previous_owner_by_symbol = current_owner_by_symbol

            for item in snapshot.get("local_regime_by_symbol", []):
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "")
                local_regime = str(item.get("local_regime") or "")
                previous_local_regime = previous_local_regime_by_symbol.get(symbol)
                if previous_local_regime != local_regime:
                    local_regime_transition_count += 1
                previous_local_regime_by_symbol[symbol] = local_regime
                if str(item.get("global_alignment") or "") == "divergent":
                    divergent_state_count += 1
                    divergent_symbols.add(symbol)

            pods = snapshot.get("pods", {})
            if isinstance(pods, dict):
                for pod_name in (PodName.POD_A.value, PodName.POD_B.value, PodName.POD_C.value):
                    pod_payload = pods.get(pod_name, {})
                    if not isinstance(pod_payload, dict):
                        continue
                    owned = pod_payload.get("owned_symbols", [])
                    if isinstance(owned, list):
                        owned_symbol_tick_totals[pod_name] += len(owned)

        supervisor.flush_compact_logs()
        final_snapshot = supervisor.snapshot()
        average_owned_symbols_by_pod = {
            pod: round(total / records_processed, 4) if records_processed > 0 else 0.0
            for pod, total in owned_symbol_tick_totals.items()
        }
        final_owned_symbols_by_pod = {
            pod.value: list(final_snapshot["pods"][pod.value]["owned_symbols"])
            for pod in (PodName.POD_A, PodName.POD_B, PodName.POD_C)
            if pod.value in final_snapshot["pods"]
        }
        symbol_reassignment_count_by_symbol = {
            str(symbol): int(count)
            for symbol, count in final_snapshot["symbol_reassignment_count_by_symbol"].items()
        }
        result = RoutingReplayResult(
            input_path=str(input_path),
            dedupe_by_timestamp=dedupe_by_timestamp,
            records_processed=records_processed,
            unique_timestamps_processed=records_processed,
            duplicate_timestamps_skipped=duplicate_timestamps_skipped,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            max_ownership_conflict_count=max_ownership_conflict_count,
            final_ownership_conflict_count=len(final_snapshot["ownership_conflicts"]),
            initial_assignment_count=initial_assignment_count,
            reassignment_event_count=reassignment_event_count,
            deassignment_event_count=deassignment_event_count,
            unassigned_decision_count=unassigned_decision_count,
            mode_counts=dict(sorted(mode_counts.items())),
            owner_assignment_counts=owner_assignment_counts,
            average_owned_symbols_by_pod=average_owned_symbols_by_pod,
            final_owned_symbols_by_pod=final_owned_symbols_by_pod,
            average_tradable_pool_size=(
                round(tradable_pool_size_total / records_processed, 4)
                if records_processed > 0
                else 0.0
            ),
            peak_tradable_pool_size=peak_tradable_pool_size,
            local_regime_transition_count=local_regime_transition_count,
            divergent_state_count=divergent_state_count,
            divergent_symbols=sorted(divergent_symbols),
            symbol_reassignment_count_by_symbol=dict(
                sorted(symbol_reassignment_count_by_symbol.items())
            ),
            max_symbol_reassignment_count=max(
                symbol_reassignment_count_by_symbol.values(),
                default=0,
            ),
            routing_overrides=dict(
                sorted(final_snapshot.get("routing_overrides", {}).get("effective", {}).items())
            )
            if isinstance(final_snapshot.get("routing_overrides"), dict)
            else {},
            report_path=str(report_output) if report_output is not None else None,
        )
        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        return result


def _runtime_config(
    config: AppConfig,
    *,
    force_enable_all_pods: bool,
    min_assign_score: float | None,
    min_hold_score: float | None,
    hysteresis_margin: float | None,
    reassignment_cooldown_seconds: int | None,
) -> AppConfig:
    runtime_config = config
    if force_enable_all_pods:
        runtime_config = replace(
            runtime_config,
            pod_a=replace(runtime_config.pod_a, enabled=True),
            pod_b=replace(runtime_config.pod_b, enabled=True),
            pod_c=replace(runtime_config.pod_c, enabled=True),
        )
    runtime_config = replace(
        runtime_config,
        trident=replace(
            runtime_config.trident,
            routing=replace(
                runtime_config.trident.routing,
                min_assign_score=(
                    runtime_config.trident.routing.min_assign_score
                    if min_assign_score is None
                    else float(min_assign_score)
                ),
                min_hold_score=(
                    runtime_config.trident.routing.min_hold_score
                    if min_hold_score is None
                    else float(min_hold_score)
                ),
                hysteresis_margin=(
                    runtime_config.trident.routing.hysteresis_margin
                    if hysteresis_margin is None
                    else float(hysteresis_margin)
                ),
                reassignment_cooldown_seconds=(
                    runtime_config.trident.routing.reassignment_cooldown_seconds
                    if reassignment_cooldown_seconds is None
                    else int(reassignment_cooldown_seconds)
                ),
            ),
        ),
    )
    return runtime_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay routing decisions on TRIDENT snapshot JSONL input",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--report-output")
    parser.add_argument(
        "--no-dedupe-timestamps",
        action="store_true",
        help="Do not skip duplicate timestamps in the input stream.",
    )
    parser.add_argument(
        "--force-enable-all-pods",
        action="store_true",
        help="Force-enable Pod A / Pod B / Pod C for the routing replay.",
    )
    parser.add_argument("--min-assign-score", type=float)
    parser.add_argument("--min-hold-score", type=float)
    parser.add_argument("--hysteresis-margin", type=float)
    parser.add_argument("--reassignment-cooldown-seconds", type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runtime_config = _runtime_config(
        load_config(args.config),
        force_enable_all_pods=args.force_enable_all_pods,
        min_assign_score=args.min_assign_score,
        min_hold_score=args.min_hold_score,
        hysteresis_margin=args.hysteresis_margin,
        reassignment_cooldown_seconds=args.reassignment_cooldown_seconds,
    )
    result = RoutingReplayRunner(runtime_config).run_jsonl(
        input_path=args.input,
        dedupe_by_timestamp=not args.no_dedupe_timestamps,
        report_output=args.report_output,
    )
    print(f"records_processed={result.records_processed}")
    print(f"duplicate_timestamps_skipped={result.duplicate_timestamps_skipped}")
    print(f"max_ownership_conflict_count={result.max_ownership_conflict_count}")
    print(f"reassignment_event_count={result.reassignment_event_count}")
    print(f"deassignment_event_count={result.deassignment_event_count}")
    print(f"max_symbol_reassignment_count={result.max_symbol_reassignment_count}")
    print(f"average_tradable_pool_size={result.average_tradable_pool_size}")
    print(f"peak_tradable_pool_size={result.peak_tradable_pool_size}")
    print(f"divergent_symbol_count={len(result.divergent_symbols)}")
    print(f"owner_assignment_counts={result.owner_assignment_counts}")
    print(f"mode_counts={result.mode_counts}")


if __name__ == "__main__":
    main()
