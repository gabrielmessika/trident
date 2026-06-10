from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.trident_ai.calibration import run_trident_ai_calibration_report
from app.trident_ai.candidate_scan import (
    DEFAULT_MICROPRICE_CONFLICT_BPS,
    DEFAULT_MIN_EDGE_TO_COST_RATIO,
    DEFAULT_MIN_NET_EDGE_BPS,
    DEFAULT_PATTERN_PROFILE,
    RESEARCH_PATTERN_PROFILE,
    STABLE_PATTERN_PROFILE,
    run_trident_ai_candidate_scan,
)
from app.trident_ai.candidate_paper import (
    DEFAULT_CANDIDATE_PAPER_CONFIDENCE,
    DEFAULT_CANDIDATE_PAPER_STOP_BPS,
    DEFAULT_CANDIDATE_PAPER_TAKE_PROFIT_BPS,
    DEFAULT_CANDIDATE_PAPER_TIME_STOP_MINUTES,
    run_trident_ai_candidate_paper_replay,
)
from app.trident_ai.candidate_gate_sweep import (
    DEFAULT_GATE_SWEEP_CATASTROPHIC_FOLD_PENALTY_BPS,
    DEFAULT_GATE_SWEEP_MAX_CATASTROPHIC_NET_BPS,
    DEFAULT_GATE_SWEEP_MAX_NEGATIVE_FOLDS,
    DEFAULT_GATE_SWEEP_MAX_ROUND_TRIP_COST_BPS_VALUES,
    DEFAULT_GATE_SWEEP_MIN_EDGE_TO_COST_VALUES,
    DEFAULT_GATE_SWEEP_MIN_LIQUIDITY_SCORE_VALUES,
    DEFAULT_GATE_SWEEP_MIN_NET_EDGE_BPS_VALUES,
    DEFAULT_GATE_SWEEP_MIN_SYMBOLS,
    DEFAULT_GATE_SWEEP_MIN_TOTAL_CLOSED_TRADES,
    DEFAULT_GATE_SWEEP_NEGATIVE_FOLD_PENALTY_BPS,
    DEFAULT_GATE_SWEEP_OOS_NO_TRADE_PENALTY_BPS,
    run_trident_ai_candidate_gate_sweep,
)
from app.trident_ai.config import load_trident_ai_config
from app.trident_ai.decision_audit import run_trident_ai_llm_decision_audit
from app.trident_ai.edge_calibration import run_trident_ai_edge_calibration_report
from app.trident_ai.entry_veto import (
    DEFAULT_ENTRY_VETO_MIN_DELTA_BPS,
    run_trident_ai_entry_veto_replay,
    run_trident_ai_entry_veto_sweep,
)
from app.trident_ai.exit_audit import (
    DEFAULT_EARLY_ADVERSE_BPS,
    DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES,
    DEFAULT_GIVEBACK_BPS,
    DEFAULT_MIN_FOLLOW_THROUGH_BPS,
    run_trident_ai_exit_follow_through_audit,
)
from app.trident_ai.exit_overlay import (
    DEFAULT_OVERLAY_EARLY_ADVERSE_BPS_VALUES,
    DEFAULT_OVERLAY_EARLY_WINDOW_MINUTES_VALUES,
    DEFAULT_OVERLAY_MFE_ACTIVATION_BPS_VALUES,
    DEFAULT_OVERLAY_MFE_GIVEBACK_BPS_VALUES,
    run_trident_ai_exit_overlay_sweep,
)
from app.trident_ai.failure_pattern import (
    DEFAULT_FAILURE_PATTERN_MAX_DOMINANT_LOSS_SYMBOL_RATIO,
    DEFAULT_FAILURE_PATTERN_MAX_WIN_RATE,
    DEFAULT_FAILURE_PATTERN_MIN_LOSS_FOLDS,
    DEFAULT_FAILURE_PATTERN_MIN_LOSS_SYMBOLS,
    DEFAULT_FAILURE_PATTERN_MIN_LOSS_TRADES,
    DEFAULT_FAILURE_PATTERN_MIN_TRADES,
    run_trident_ai_failure_pattern_audit,
)
from app.trident_ai.intel import run_trident_ai_intel_digest
from app.trident_ai.outcome_audit import (
    DEFAULT_OUTCOME_HORIZONS_MINUTES,
    run_trident_ai_candidate_outcome_audit,
)
from app.trident_ai.pattern_calibration import (
    run_trident_ai_pattern_calibration_report,
    run_trident_ai_pattern_fold_validation_report,
)
from app.trident_ai.pattern_support import (
    DEFAULT_PATTERN_SUPPORT_MAX_CATASTROPHIC_NET_BPS,
    DEFAULT_PATTERN_SUPPORT_MAX_DOMINANT_SYMBOL_RATIO,
    DEFAULT_PATTERN_SUPPORT_MAX_NEGATIVE_FOLDS,
    DEFAULT_PATTERN_SUPPORT_MIN_CLOSED_TRADES,
    DEFAULT_PATTERN_SUPPORT_MIN_FOLDS,
    DEFAULT_PATTERN_SUPPORT_MIN_POSITIVE_FOLDS,
    DEFAULT_PATTERN_SUPPORT_MIN_SYMBOLS,
    run_trident_ai_pattern_support_audit,
)
from app.trident_ai.paper import run_trident_ai_paper_replay
from app.trident_ai.replay import run_trident_ai_llm_replay
from app.trident_ai.shadow_runner import run_trident_ai_shadow


DEFAULT_REPLAY_INPUT = "server-data/replay_inputs/full_bot_latest_fetch.jsonl"
DEFAULT_SMOKE_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
DEFAULT_ENV_FILE = ".env.tridentai"
ALLOWED_ENV_KEYS = {"OPENAI_API_KEY", "XAI_API_KEY"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run TRIDENT-AI local replay tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    shadow = subparsers.add_parser("shadow", help="Run deterministic shadow replay")
    _add_common_replay_args(shadow)
    shadow.add_argument("--status-path", default=None)

    llm = subparsers.add_parser("llm-replay", help="Run LLM replay/cache-fill")
    _add_common_replay_args(llm)
    llm.add_argument("--cache-dir", default=None)
    llm.add_argument("--report-json-path", default=None)
    llm.add_argument("--report-md-path", default=None)
    llm.add_argument("--allow-live-llm-calls", action="store_true")
    llm.add_argument("--max-live-calls", type=int, default=None)
    llm.add_argument("--max-incremental-cost-usd", type=float, default=None)
    llm.add_argument(
        "--intel-digest-input",
        default=None,
        help="Optional intel digest JSON/JSONL used as a local veto and prompt context.",
    )

    paper = subparsers.add_parser("paper-replay", help="Run paper execution over a LLM replay journal")
    _add_config_env_args(paper)
    paper.add_argument("--input", required=True)
    paper.add_argument("--journal-path", default=None)
    paper.add_argument("--report-json-path", default=None)
    paper.add_argument("--report-md-path", default=None)
    paper.add_argument("--max-decisions", type=int, default=None)
    paper.add_argument(
        "--symbols",
        default=None,
        help="Optional comma-separated symbol filter for LLM decisions.",
    )
    paper.add_argument(
        "--market-input",
        default=None,
        help="Optional snapshot JSONL used to advance open paper positions between LLM decisions.",
    )

    candidates = subparsers.add_parser(
        "candidate-scan",
        help="Score market contexts locally and write a selected replay input",
    )
    _add_common_replay_args(candidates)
    candidates.add_argument("--selected-input-path", default=None)
    candidates.add_argument("--report-json-path", default=None)
    candidates.add_argument("--report-md-path", default=None)
    candidates.add_argument("--top-n", type=int, default=40)
    candidates.add_argument("--min-score", type=float, default=1.25)
    candidates.add_argument(
        "--min-edge-to-cost",
        type=float,
        default=DEFAULT_MIN_EDGE_TO_COST_RATIO,
        help=(
            "Minimum estimated edge / round-trip cost ratio before a candidate can be selected. "
            f"Default: {DEFAULT_MIN_EDGE_TO_COST_RATIO}"
        ),
    )
    candidates.add_argument(
        "--min-net-edge-bps",
        type=float,
        default=DEFAULT_MIN_NET_EDGE_BPS,
        help=(
            "Minimum estimated net edge after round-trip cost, in bps, before selection. "
            f"Default: {DEFAULT_MIN_NET_EDGE_BPS}"
        ),
    )
    candidates.add_argument(
        "--allow-microprice-conflict",
        action="store_true",
        help="Allow candidates whose microprice dislocation is adverse to the proposed side.",
    )
    candidates.add_argument(
        "--require-microprice-alignment",
        action="store_true",
        help="Require a non-neutral microprice dislocation aligned with the proposed side.",
    )
    candidates.add_argument(
        "--microprice-conflict-bps",
        type=float,
        default=DEFAULT_MICROPRICE_CONFLICT_BPS,
        help=(
            "Minimum adverse microprice dislocation, in bps, treated as a side conflict. "
            f"Default: {DEFAULT_MICROPRICE_CONFLICT_BPS}"
        ),
    )
    candidates.add_argument(
        "--pattern-profile",
        choices=(DEFAULT_PATTERN_PROFILE, RESEARCH_PATTERN_PROFILE, STABLE_PATTERN_PROFILE),
        default=DEFAULT_PATTERN_PROFILE,
        help="Optional pattern-quality score profile. Default: none.",
    )

    calibration = subparsers.add_parser(
        "calibration-report",
        help="Join candidate, LLM and paper journals into a local calibration report",
    )
    _add_config_env_args(calibration)
    calibration.add_argument("--candidate-input", required=True)
    calibration.add_argument("--llm-journal", required=True)
    calibration.add_argument("--paper-journal", required=True)
    calibration.add_argument("--report-json-path", default=None)
    calibration.add_argument("--report-md-path", default=None)

    edge_calibration = subparsers.add_parser(
        "edge-calibration",
        help="Compare candidate edge estimates with realized market-follow paper PnL",
    )
    _add_config_env_args(edge_calibration)
    edge_calibration.add_argument("--candidate-input", required=True)
    edge_calibration.add_argument("--llm-journal", required=True)
    edge_calibration.add_argument("--paper-journal", required=True)
    edge_calibration.add_argument("--report-json-path", default=None)
    edge_calibration.add_argument("--report-md-path", default=None)

    pattern_calibration = subparsers.add_parser(
        "pattern-calibration",
        help="Audit candidate paper results by signal pattern instead of coin-specific rules",
    )
    _add_config_env_args(pattern_calibration)
    pattern_calibration.add_argument(
        "--decision-journal",
        action="append",
        required=True,
        help="Synthetic or LLM decision journal. Repeat once per paper journal.",
    )
    pattern_calibration.add_argument(
        "--paper-journal",
        action="append",
        required=True,
        help="Paper replay journal matching the decision journal. Repeat in the same order.",
    )
    pattern_calibration.add_argument("--report-json-path", default=None)
    pattern_calibration.add_argument("--report-md-path", default=None)
    pattern_calibration.add_argument("--min-trades-per-pattern", type=int, default=3)

    pattern_folds = subparsers.add_parser(
        "pattern-fold-validation",
        help="Validate signal patterns across independent paper replay folds",
    )
    _add_config_env_args(pattern_folds)
    pattern_folds.add_argument(
        "--decision-journal",
        action="append",
        required=True,
        help="Synthetic or LLM decision journal. Repeat once per paper journal.",
    )
    pattern_folds.add_argument(
        "--paper-journal",
        action="append",
        required=True,
        help="Paper replay journal matching the decision journal. Repeat in the same order.",
    )
    pattern_folds.add_argument(
        "--fold-label",
        action="append",
        default=None,
        help="Optional fold label. Repeat once per journal pair.",
    )
    pattern_folds.add_argument("--report-json-path", default=None)
    pattern_folds.add_argument("--report-md-path", default=None)
    pattern_folds.add_argument("--min-trades-per-fold", type=int, default=1)
    pattern_folds.add_argument("--min-positive-folds", type=int, default=2)
    pattern_folds.add_argument("--max-catastrophic-net-bps", type=float, default=50.0)

    pattern_support = subparsers.add_parser(
        "pattern-support-audit",
        help="Audit pattern buckets for multi-fold and multi-symbol support",
    )
    _add_config_env_args(pattern_support)
    pattern_support.add_argument(
        "--decision-journal",
        action="append",
        required=True,
        help="Synthetic or LLM decision journal. Repeat once per paper journal.",
    )
    pattern_support.add_argument(
        "--paper-journal",
        action="append",
        required=True,
        help="Paper replay journal matching the decision journal. Repeat in the same order.",
    )
    pattern_support.add_argument(
        "--fold-label",
        action="append",
        default=None,
        help="Optional fold label. Repeat once per journal pair.",
    )
    pattern_support.add_argument("--report-json-path", default=None)
    pattern_support.add_argument("--report-md-path", default=None)
    pattern_support.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )
    pattern_support.add_argument(
        "--min-closed-trades",
        type=int,
        default=DEFAULT_PATTERN_SUPPORT_MIN_CLOSED_TRADES,
    )
    pattern_support.add_argument(
        "--min-folds",
        type=int,
        default=DEFAULT_PATTERN_SUPPORT_MIN_FOLDS,
    )
    pattern_support.add_argument(
        "--min-positive-folds",
        type=int,
        default=DEFAULT_PATTERN_SUPPORT_MIN_POSITIVE_FOLDS,
    )
    pattern_support.add_argument(
        "--min-symbols",
        type=int,
        default=DEFAULT_PATTERN_SUPPORT_MIN_SYMBOLS,
    )
    pattern_support.add_argument(
        "--max-negative-folds",
        type=int,
        default=DEFAULT_PATTERN_SUPPORT_MAX_NEGATIVE_FOLDS,
    )
    pattern_support.add_argument(
        "--max-dominant-symbol-ratio",
        type=float,
        default=DEFAULT_PATTERN_SUPPORT_MAX_DOMINANT_SYMBOL_RATIO,
    )
    pattern_support.add_argument(
        "--max-catastrophic-net-bps",
        type=float,
        default=DEFAULT_PATTERN_SUPPORT_MAX_CATASTROPHIC_NET_BPS,
    )

    decision_audit = subparsers.add_parser(
        "llm-decision-audit",
        help="Audit LLM decisions against local candidate thresholds",
    )
    _add_config_env_args(decision_audit)
    decision_audit.add_argument("--candidate-input", required=True)
    decision_audit.add_argument("--llm-journal", required=True)
    decision_audit.add_argument("--report-json-path", default=None)
    decision_audit.add_argument("--report-md-path", default=None)
    decision_audit.add_argument(
        "--min-edge-to-cost",
        type=float,
        default=DEFAULT_MIN_EDGE_TO_COST_RATIO,
    )
    decision_audit.add_argument(
        "--min-net-edge-bps",
        type=float,
        default=DEFAULT_MIN_NET_EDGE_BPS,
    )

    outcome_audit = subparsers.add_parser(
        "candidate-outcome-audit",
        help="Measure realized candidate moves at fixed horizons without LLM calls",
    )
    _add_config_env_args(outcome_audit)
    outcome_audit.add_argument("--candidate-input", required=True)
    outcome_audit.add_argument("--market-input", required=True)
    outcome_audit.add_argument("--report-json-path", default=None)
    outcome_audit.add_argument("--report-md-path", default=None)
    outcome_audit.add_argument(
        "--horizons-minutes",
        default=",".join(str(value) for value in DEFAULT_OUTCOME_HORIZONS_MINUTES),
        help="Comma-separated positive integer horizons. Default: 15,30,60,180",
    )

    exit_audit = subparsers.add_parser(
        "exit-follow-through-audit",
        help="Audit paper trade path quality: MFE/MAE, early adverse moves and follow-through",
    )
    _add_config_env_args(exit_audit)
    exit_audit.add_argument(
        "--paper-journal",
        action="append",
        required=True,
        help="Paper replay journal. Repeat once per market input.",
    )
    exit_audit.add_argument(
        "--market-input",
        action="append",
        required=True,
        help="Snapshot JSONL matching the paper journal. Repeat in the same order.",
    )
    exit_audit.add_argument(
        "--fold-label",
        action="append",
        default=None,
        help="Optional fold label. Repeat once per journal/input pair.",
    )
    exit_audit.add_argument("--report-json-path", default=None)
    exit_audit.add_argument("--report-md-path", default=None)
    exit_audit.add_argument(
        "--early-windows-minutes",
        default=",".join(str(value) for value in DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES),
        help="Comma-separated positive integer windows. Default: 15,30,60",
    )
    exit_audit.add_argument("--early-adverse-bps", type=float, default=DEFAULT_EARLY_ADVERSE_BPS)
    exit_audit.add_argument(
        "--min-follow-through-bps",
        type=float,
        default=DEFAULT_MIN_FOLLOW_THROUGH_BPS,
    )
    exit_audit.add_argument("--giveback-bps", type=float, default=DEFAULT_GIVEBACK_BPS)

    exit_overlay = subparsers.add_parser(
        "exit-overlay-sweep",
        help="Sweep local exit overlays over paper trades without LLM calls",
    )
    _add_config_env_args(exit_overlay)
    exit_overlay.add_argument(
        "--paper-journal",
        action="append",
        required=True,
        help="Paper replay journal. Repeat once per market input.",
    )
    exit_overlay.add_argument(
        "--market-input",
        action="append",
        required=True,
        help="Snapshot JSONL matching the paper journal. Repeat in the same order.",
    )
    exit_overlay.add_argument(
        "--fold-label",
        action="append",
        default=None,
        help="Optional fold label. Repeat once per journal/input pair.",
    )
    exit_overlay.add_argument("--report-json-path", default=None)
    exit_overlay.add_argument("--report-md-path", default=None)
    exit_overlay.add_argument(
        "--early-adverse-bps-values",
        default=",".join(str(value) for value in DEFAULT_OVERLAY_EARLY_ADVERSE_BPS_VALUES),
        help="Comma-separated early adverse bps thresholds; include 0 to disable. Default: 0,25,35,50",
    )
    exit_overlay.add_argument(
        "--early-window-minutes-values",
        default=",".join(str(value) for value in DEFAULT_OVERLAY_EARLY_WINDOW_MINUTES_VALUES),
        help="Comma-separated early adverse windows. Default: 15,30,60",
    )
    exit_overlay.add_argument(
        "--mfe-activation-bps-values",
        default=",".join(str(value) for value in DEFAULT_OVERLAY_MFE_ACTIVATION_BPS_VALUES),
        help="Comma-separated MFE activation thresholds; include 0 to disable. Default: 0,25,40,60",
    )
    exit_overlay.add_argument(
        "--mfe-giveback-bps-values",
        default=",".join(str(value) for value in DEFAULT_OVERLAY_MFE_GIVEBACK_BPS_VALUES),
        help="Comma-separated MFE giveback thresholds; include 0 to disable. Default: 0,20,30,45",
    )

    failure_pattern = subparsers.add_parser(
        "failure-pattern-audit",
        help="Audit losing candidate-paper trades by context and path features",
    )
    _add_config_env_args(failure_pattern)
    failure_pattern.add_argument(
        "--decision-journal",
        action="append",
        required=True,
        help="Synthetic or LLM decision journal. Repeat once per paper journal.",
    )
    failure_pattern.add_argument(
        "--paper-journal",
        action="append",
        required=True,
        help="Paper replay journal matching the decision journal. Repeat in the same order.",
    )
    failure_pattern.add_argument(
        "--market-input",
        action="append",
        required=True,
        help="Snapshot JSONL matching the paper journal. Repeat in the same order.",
    )
    failure_pattern.add_argument(
        "--fold-label",
        action="append",
        default=None,
        help="Optional fold label. Repeat once per journal/input triple.",
    )
    failure_pattern.add_argument("--report-json-path", default=None)
    failure_pattern.add_argument("--report-md-path", default=None)
    failure_pattern.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )
    failure_pattern.add_argument(
        "--windows-minutes",
        default=",".join(str(value) for value in DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES),
        help="Comma-separated positive integer path windows. Default: 15,30,60",
    )
    failure_pattern.add_argument("--early-adverse-bps", type=float, default=DEFAULT_EARLY_ADVERSE_BPS)
    failure_pattern.add_argument(
        "--min-follow-through-bps",
        type=float,
        default=DEFAULT_MIN_FOLLOW_THROUGH_BPS,
    )
    failure_pattern.add_argument("--giveback-bps", type=float, default=DEFAULT_GIVEBACK_BPS)
    failure_pattern.add_argument(
        "--min-trades",
        type=int,
        default=DEFAULT_FAILURE_PATTERN_MIN_TRADES,
    )
    failure_pattern.add_argument(
        "--min-loss-trades",
        type=int,
        default=DEFAULT_FAILURE_PATTERN_MIN_LOSS_TRADES,
    )
    failure_pattern.add_argument(
        "--min-loss-folds",
        type=int,
        default=DEFAULT_FAILURE_PATTERN_MIN_LOSS_FOLDS,
    )
    failure_pattern.add_argument(
        "--min-loss-symbols",
        type=int,
        default=DEFAULT_FAILURE_PATTERN_MIN_LOSS_SYMBOLS,
    )
    failure_pattern.add_argument("--max-win-rate", type=float, default=DEFAULT_FAILURE_PATTERN_MAX_WIN_RATE)
    failure_pattern.add_argument(
        "--max-dominant-loss-symbol-ratio",
        type=float,
        default=DEFAULT_FAILURE_PATTERN_MAX_DOMINANT_LOSS_SYMBOL_RATIO,
    )

    entry_veto = subparsers.add_parser(
        "entry-veto-replay",
        help="Replay paper execution after vetoing entry-time decision buckets",
    )
    _add_config_env_args(entry_veto)
    entry_veto.add_argument(
        "--decision-journal",
        action="append",
        required=True,
        help="Synthetic or LLM decision journal. Repeat once per market input.",
    )
    entry_veto.add_argument(
        "--market-input",
        action="append",
        required=True,
        help="Snapshot JSONL matching the decision journal. Repeat in the same order.",
    )
    entry_veto.add_argument(
        "--baseline-paper-journal",
        action="append",
        default=None,
        help="Optional baseline paper journal matching the decision journal. Repeat in the same order.",
    )
    entry_veto.add_argument(
        "--fold-label",
        action="append",
        default=None,
        help="Optional fold label. Repeat once per journal/input pair.",
    )
    entry_veto.add_argument(
        "--veto-bucket",
        action="append",
        required=True,
        help="Entry-time bucket to veto, formatted as family::bucket. Repeat to combine vetoes.",
    )
    entry_veto.add_argument("--report-json-path", default=None)
    entry_veto.add_argument("--report-md-path", default=None)
    entry_veto.add_argument("--artifact-dir", default=None)
    entry_veto.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )
    entry_veto.add_argument(
        "--min-delta-bps",
        type=float,
        default=DEFAULT_ENTRY_VETO_MIN_DELTA_BPS,
    )

    entry_veto_sweep = subparsers.add_parser(
        "entry-veto-sweep",
        help="Compare multiple entry-time veto buckets with local paper replays",
    )
    _add_config_env_args(entry_veto_sweep)
    entry_veto_sweep.add_argument(
        "--decision-journal",
        action="append",
        required=True,
        help="Synthetic or LLM decision journal. Repeat once per market input.",
    )
    entry_veto_sweep.add_argument(
        "--market-input",
        action="append",
        required=True,
        help="Snapshot JSONL matching the decision journal. Repeat in the same order.",
    )
    entry_veto_sweep.add_argument(
        "--baseline-paper-journal",
        action="append",
        default=None,
        help="Optional baseline paper journal matching the decision journal. Repeat in the same order.",
    )
    entry_veto_sweep.add_argument(
        "--fold-label",
        action="append",
        default=None,
        help="Optional fold label. Repeat once per journal/input pair.",
    )
    entry_veto_sweep.add_argument(
        "--veto-bucket",
        action="append",
        required=True,
        help="Entry-time bucket to test, formatted as family::bucket. Repeat for each candidate.",
    )
    entry_veto_sweep.add_argument("--report-json-path", default=None)
    entry_veto_sweep.add_argument("--report-md-path", default=None)
    entry_veto_sweep.add_argument("--artifact-dir", default=None)
    entry_veto_sweep.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )
    entry_veto_sweep.add_argument(
        "--min-delta-bps",
        type=float,
        default=DEFAULT_ENTRY_VETO_MIN_DELTA_BPS,
    )

    candidate_paper = subparsers.add_parser(
        "candidate-paper-replay",
        help="Convert local candidates into synthetic opens and run paper execution",
    )
    _add_config_env_args(candidate_paper)
    candidate_paper.add_argument("--candidate-input", required=True)
    candidate_paper.add_argument("--market-input", required=True)
    candidate_paper.add_argument("--decision-journal-path", default=None)
    candidate_paper.add_argument("--journal-path", default=None)
    candidate_paper.add_argument("--report-json-path", default=None)
    candidate_paper.add_argument("--report-md-path", default=None)
    candidate_paper.add_argument("--max-candidates", type=int, default=None)
    candidate_paper.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )
    candidate_paper.add_argument("--notional-usd", type=float, default=None)
    candidate_paper.add_argument(
        "--confidence",
        type=float,
        default=DEFAULT_CANDIDATE_PAPER_CONFIDENCE,
    )
    candidate_paper.add_argument(
        "--stop-bps",
        type=float,
        default=DEFAULT_CANDIDATE_PAPER_STOP_BPS,
    )
    candidate_paper.add_argument(
        "--take-profit-bps",
        type=float,
        default=DEFAULT_CANDIDATE_PAPER_TAKE_PROFIT_BPS,
    )
    candidate_paper.add_argument(
        "--time-stop-minutes",
        type=int,
        default=DEFAULT_CANDIDATE_PAPER_TIME_STOP_MINUTES,
    )
    candidate_paper.add_argument("--min-edge-to-cost", type=float, default=None)
    candidate_paper.add_argument("--min-net-edge-bps", type=float, default=None)
    candidate_paper.add_argument("--min-liquidity-score", type=float, default=None)
    candidate_paper.add_argument("--max-round-trip-cost-bps", type=float, default=None)

    gate_sweep = subparsers.add_parser(
        "candidate-gate-sweep",
        help="Sweep candidate edge/liquidity/cost gates over paper folds",
    )
    _add_config_env_args(gate_sweep)
    gate_sweep.add_argument(
        "--candidate-input",
        action="append",
        required=True,
        help="Candidate-selected snapshot JSONL. Repeat once per market input.",
    )
    gate_sweep.add_argument(
        "--market-input",
        action="append",
        required=True,
        help="Market snapshot JSONL matching candidate input. Repeat in the same order.",
    )
    gate_sweep.add_argument(
        "--fold-label",
        action="append",
        default=None,
        help="Optional fold label. Repeat once per input pair.",
    )
    gate_sweep.add_argument(
        "--oos-fold-label",
        action="append",
        default=None,
        help="Optional OOS fold label. Defaults to labels containing 'oos'.",
    )
    gate_sweep.add_argument("--artifact-dir", default=None)
    gate_sweep.add_argument("--report-json-path", default=None)
    gate_sweep.add_argument("--report-md-path", default=None)
    gate_sweep.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )
    gate_sweep.add_argument("--notional-usd", type=float, default=None)
    gate_sweep.add_argument(
        "--confidence",
        type=float,
        default=DEFAULT_CANDIDATE_PAPER_CONFIDENCE,
    )
    gate_sweep.add_argument(
        "--stop-bps",
        type=float,
        default=DEFAULT_CANDIDATE_PAPER_STOP_BPS,
    )
    gate_sweep.add_argument(
        "--take-profit-bps",
        type=float,
        default=DEFAULT_CANDIDATE_PAPER_TAKE_PROFIT_BPS,
    )
    gate_sweep.add_argument(
        "--time-stop-minutes",
        type=int,
        default=DEFAULT_CANDIDATE_PAPER_TIME_STOP_MINUTES,
    )
    gate_sweep.add_argument(
        "--min-edge-to-cost-values",
        default=",".join(str(value) for value in DEFAULT_GATE_SWEEP_MIN_EDGE_TO_COST_VALUES),
    )
    gate_sweep.add_argument(
        "--min-net-edge-bps-values",
        default=",".join(str(value) for value in DEFAULT_GATE_SWEEP_MIN_NET_EDGE_BPS_VALUES),
    )
    gate_sweep.add_argument(
        "--min-liquidity-score-values",
        default=",".join(str(value) for value in DEFAULT_GATE_SWEEP_MIN_LIQUIDITY_SCORE_VALUES),
    )
    gate_sweep.add_argument(
        "--max-round-trip-cost-bps-values",
        default=",".join(str(value) for value in DEFAULT_GATE_SWEEP_MAX_ROUND_TRIP_COST_BPS_VALUES),
    )
    gate_sweep.add_argument("--max-profiles", type=int, default=None)
    gate_sweep.add_argument(
        "--min-total-closed-trades",
        type=int,
        default=DEFAULT_GATE_SWEEP_MIN_TOTAL_CLOSED_TRADES,
    )
    gate_sweep.add_argument("--min-symbols", type=int, default=DEFAULT_GATE_SWEEP_MIN_SYMBOLS)
    gate_sweep.add_argument(
        "--max-negative-folds",
        type=int,
        default=DEFAULT_GATE_SWEEP_MAX_NEGATIVE_FOLDS,
    )
    gate_sweep.add_argument(
        "--max-catastrophic-net-bps",
        type=float,
        default=DEFAULT_GATE_SWEEP_MAX_CATASTROPHIC_NET_BPS,
    )
    gate_sweep.add_argument(
        "--oos-no-trade-penalty-bps",
        type=float,
        default=DEFAULT_GATE_SWEEP_OOS_NO_TRADE_PENALTY_BPS,
    )
    gate_sweep.add_argument(
        "--negative-fold-penalty-bps",
        type=float,
        default=DEFAULT_GATE_SWEEP_NEGATIVE_FOLD_PENALTY_BPS,
    )
    gate_sweep.add_argument(
        "--catastrophic-fold-penalty-bps",
        type=float,
        default=DEFAULT_GATE_SWEEP_CATASTROPHIC_FOLD_PENALTY_BPS,
    )

    intel = subparsers.add_parser(
        "intel-digest",
        help="Build a TRIDENT-AI news/social intel digest in shadow mode",
    )
    _add_config_env_args(intel)
    intel.add_argument("--journal-path", default=None)
    intel.add_argument("--report-json-path", default=None)
    intel.add_argument("--report-md-path", default=None)
    intel.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )
    intel.add_argument("--as-of", default=None)
    intel.add_argument(
        "--fixture-input",
        default=None,
        help="Optional local JSON fixture containing an intel_digest object.",
    )
    intel.add_argument("--allow-live-intel-calls", action="store_true")
    intel.add_argument("--max-live-calls", type=int, default=None)
    intel.add_argument("--max-incremental-cost-usd", type=float, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_trident_ai_env_file(args.env_file)
    config = load_trident_ai_config(args.config)

    if args.command == "shadow":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_shadow(
            args.input,
            config=config,
            journal_path=args.journal_path,
            status_path=args.status_path,
            max_records=args.max_records,
            max_contexts=args.max_contexts,
            symbols=symbols,
        )
    elif args.command == "llm-replay":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_llm_replay(
            args.input,
            config=config,
            cache_dir=args.cache_dir,
            allow_live_llm_calls=args.allow_live_llm_calls,
            journal_path=args.journal_path,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            max_records=args.max_records,
            max_contexts=args.max_contexts,
            symbols=symbols,
            max_live_calls=args.max_live_calls,
            max_incremental_cost_usd=args.max_incremental_cost_usd,
            intel_digest_path=args.intel_digest_input,
        )
    elif args.command == "paper-replay":
        symbols = tuple(args.symbols.split(",")) if args.symbols else None
        result = run_trident_ai_paper_replay(
            args.input,
            config=config,
            journal_path=args.journal_path,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            max_decisions=args.max_decisions,
            market_input_path=args.market_input,
            symbols=symbols,
        )
    elif args.command == "candidate-scan":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_candidate_scan(
            args.input,
            config=config,
            journal_path=args.journal_path,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            selected_input_path=args.selected_input_path,
            max_records=args.max_records,
            max_contexts=args.max_contexts,
            top_n=args.top_n,
            min_score=args.min_score,
            min_edge_to_cost=args.min_edge_to_cost,
            min_net_edge_bps=args.min_net_edge_bps,
            allow_microprice_conflict=args.allow_microprice_conflict,
            require_microprice_alignment=args.require_microprice_alignment,
            microprice_conflict_bps=args.microprice_conflict_bps,
            pattern_profile=args.pattern_profile,
            symbols=symbols,
        )
    elif args.command == "calibration-report":
        result = run_trident_ai_calibration_report(
            candidate_input_path=args.candidate_input,
            llm_journal_path=args.llm_journal,
            paper_journal_path=args.paper_journal,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
        )
    elif args.command == "edge-calibration":
        result = run_trident_ai_edge_calibration_report(
            candidate_input_path=args.candidate_input,
            llm_journal_path=args.llm_journal,
            paper_journal_path=args.paper_journal,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
        )
    elif args.command == "pattern-calibration":
        result = run_trident_ai_pattern_calibration_report(
            decision_journal_paths=tuple(args.decision_journal),
            paper_journal_paths=tuple(args.paper_journal),
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            min_trades_per_pattern=args.min_trades_per_pattern,
        )
    elif args.command == "pattern-fold-validation":
        result = run_trident_ai_pattern_fold_validation_report(
            decision_journal_paths=tuple(args.decision_journal),
            paper_journal_paths=tuple(args.paper_journal),
            fold_labels=tuple(args.fold_label) if args.fold_label else None,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            min_trades_per_fold=args.min_trades_per_fold,
            min_positive_folds=args.min_positive_folds,
            max_catastrophic_net_bps=args.max_catastrophic_net_bps,
        )
    elif args.command == "pattern-support-audit":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_pattern_support_audit(
            decision_journal_paths=tuple(args.decision_journal),
            paper_journal_paths=tuple(args.paper_journal),
            fold_labels=tuple(args.fold_label) if args.fold_label else None,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            symbols=symbols,
            min_closed_trades=args.min_closed_trades,
            min_folds=args.min_folds,
            min_positive_folds=args.min_positive_folds,
            min_symbols=args.min_symbols,
            max_negative_folds=args.max_negative_folds,
            max_dominant_symbol_ratio=args.max_dominant_symbol_ratio,
            max_catastrophic_net_bps=args.max_catastrophic_net_bps,
        )
    elif args.command == "llm-decision-audit":
        result = run_trident_ai_llm_decision_audit(
            candidate_input_path=args.candidate_input,
            llm_journal_path=args.llm_journal,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            min_edge_to_cost=args.min_edge_to_cost,
            min_net_edge_bps=args.min_net_edge_bps,
        )
    elif args.command == "candidate-outcome-audit":
        result = run_trident_ai_candidate_outcome_audit(
            candidate_input_path=args.candidate_input,
            market_input_path=args.market_input,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            horizons_minutes=_parse_int_tuple(args.horizons_minutes),
        )
    elif args.command == "exit-follow-through-audit":
        result = run_trident_ai_exit_follow_through_audit(
            paper_journal_paths=tuple(args.paper_journal),
            market_input_paths=tuple(args.market_input),
            fold_labels=tuple(args.fold_label) if args.fold_label else None,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            early_windows_minutes=_parse_int_tuple(args.early_windows_minutes),
            early_adverse_bps=args.early_adverse_bps,
            min_follow_through_bps=args.min_follow_through_bps,
            giveback_bps=args.giveback_bps,
        )
    elif args.command == "exit-overlay-sweep":
        result = run_trident_ai_exit_overlay_sweep(
            paper_journal_paths=tuple(args.paper_journal),
            market_input_paths=tuple(args.market_input),
            fold_labels=tuple(args.fold_label) if args.fold_label else None,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            early_adverse_bps_values=_parse_float_tuple(args.early_adverse_bps_values),
            early_window_minutes_values=_parse_int_tuple(args.early_window_minutes_values),
            mfe_activation_bps_values=_parse_float_tuple(args.mfe_activation_bps_values),
            mfe_giveback_bps_values=_parse_float_tuple(args.mfe_giveback_bps_values),
        )
    elif args.command == "failure-pattern-audit":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_failure_pattern_audit(
            decision_journal_paths=tuple(args.decision_journal),
            paper_journal_paths=tuple(args.paper_journal),
            market_input_paths=tuple(args.market_input),
            fold_labels=tuple(args.fold_label) if args.fold_label else None,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            symbols=symbols,
            windows_minutes=_parse_int_tuple(args.windows_minutes),
            early_adverse_bps=args.early_adverse_bps,
            min_follow_through_bps=args.min_follow_through_bps,
            giveback_bps=args.giveback_bps,
            min_trades=args.min_trades,
            min_loss_trades=args.min_loss_trades,
            min_loss_folds=args.min_loss_folds,
            min_loss_symbols=args.min_loss_symbols,
            max_win_rate=args.max_win_rate,
            max_dominant_loss_symbol_ratio=args.max_dominant_loss_symbol_ratio,
        )
    elif args.command == "entry-veto-replay":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_entry_veto_replay(
            decision_journal_paths=tuple(args.decision_journal),
            market_input_paths=tuple(args.market_input),
            baseline_paper_journal_paths=(
                tuple(args.baseline_paper_journal)
                if args.baseline_paper_journal
                else None
            ),
            fold_labels=tuple(args.fold_label) if args.fold_label else None,
            veto_buckets=tuple(args.veto_bucket),
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            artifact_dir=args.artifact_dir,
            symbols=symbols,
            min_delta_bps=args.min_delta_bps,
        )
    elif args.command == "entry-veto-sweep":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_entry_veto_sweep(
            decision_journal_paths=tuple(args.decision_journal),
            market_input_paths=tuple(args.market_input),
            baseline_paper_journal_paths=(
                tuple(args.baseline_paper_journal)
                if args.baseline_paper_journal
                else None
            ),
            fold_labels=tuple(args.fold_label) if args.fold_label else None,
            veto_buckets=tuple(args.veto_bucket),
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            artifact_dir=args.artifact_dir,
            symbols=symbols,
            min_delta_bps=args.min_delta_bps,
        )
    elif args.command == "candidate-gate-sweep":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_candidate_gate_sweep(
            candidate_input_paths=tuple(args.candidate_input),
            market_input_paths=tuple(args.market_input),
            fold_labels=tuple(args.fold_label) if args.fold_label else None,
            oos_fold_labels=tuple(args.oos_fold_label) if args.oos_fold_label else None,
            config=config,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            artifact_dir=args.artifact_dir,
            symbols=symbols,
            notional_usd=args.notional_usd,
            confidence=args.confidence,
            stop_bps=args.stop_bps,
            take_profit_bps=args.take_profit_bps,
            time_stop_minutes=args.time_stop_minutes,
            min_edge_to_cost_values=_parse_float_tuple(args.min_edge_to_cost_values),
            min_net_edge_bps_values=_parse_float_tuple(args.min_net_edge_bps_values),
            min_liquidity_score_values=_parse_float_tuple(args.min_liquidity_score_values),
            max_round_trip_cost_bps_values=_parse_float_tuple(args.max_round_trip_cost_bps_values),
            max_profiles=args.max_profiles,
            min_total_closed_trades=args.min_total_closed_trades,
            min_symbols=args.min_symbols,
            max_negative_folds=args.max_negative_folds,
            max_catastrophic_net_bps=args.max_catastrophic_net_bps,
            oos_no_trade_penalty_bps=args.oos_no_trade_penalty_bps,
            negative_fold_penalty_bps=args.negative_fold_penalty_bps,
            catastrophic_fold_penalty_bps=args.catastrophic_fold_penalty_bps,
        )
    elif args.command == "candidate-paper-replay":
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_candidate_paper_replay(
            candidate_input_path=args.candidate_input,
            market_input_path=args.market_input,
            config=config,
            decision_journal_path=args.decision_journal_path,
            journal_path=args.journal_path,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            max_candidates=args.max_candidates,
            symbols=symbols,
            notional_usd=args.notional_usd,
            confidence=args.confidence,
            stop_bps=args.stop_bps,
            take_profit_bps=args.take_profit_bps,
            time_stop_minutes=args.time_stop_minutes,
            min_edge_to_cost=args.min_edge_to_cost,
            min_net_edge_bps=args.min_net_edge_bps,
            min_liquidity_score=args.min_liquidity_score,
            max_round_trip_cost_bps=args.max_round_trip_cost_bps,
        )
    else:
        symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS
        result = run_trident_ai_intel_digest(
            config=config,
            symbols=symbols,
            as_of=args.as_of,
            fixture_input_path=args.fixture_input,
            allow_live_intel_calls=args.allow_live_intel_calls,
            max_live_calls=args.max_live_calls,
            max_incremental_cost_usd=args.max_incremental_cost_usd,
            journal_path=args.journal_path,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
        )

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def _add_common_replay_args(parser: argparse.ArgumentParser) -> None:
    _add_config_env_args(parser)
    parser.add_argument("--input", default=DEFAULT_REPLAY_INPUT)
    parser.add_argument("--journal-path", default=None)
    parser.add_argument("--max-records", type=int, default=20)
    parser.add_argument("--max-contexts", type=int, default=50)
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )


def _add_config_env_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config/trident_ai.toml")
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help="Optional local env file for API keys. Default: .env.tridentai",
    )


def load_trident_ai_env_file(path: str | Path | None) -> dict[str, str]:
    if path is None or str(path).strip().lower() in {"", "none", "false"}:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in ALLOWED_ENV_KEYS:
            continue
        if key in os.environ:
            continue
        value = _unquote_env_value(raw_value.strip())
        if not value:
            continue
        os.environ[key] = value
        loaded[key] = value
    return loaded


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_int_tuple(value: str) -> tuple[int, ...]:
    parsed: list[int] = []
    for item in value.split(","):
        text = item.strip()
        if not text:
            continue
        parsed.append(int(text))
    return tuple(parsed)


def _parse_float_tuple(value: str) -> tuple[float, ...]:
    parsed: list[float] = []
    for item in value.split(","):
        text = item.strip()
        if not text:
            continue
        parsed.append(float(text))
    return tuple(parsed)


if __name__ == "__main__":
    raise SystemExit(main())
