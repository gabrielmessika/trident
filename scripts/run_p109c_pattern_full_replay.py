#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_report import PodABacktestReport
from app.execution.directional_executor import DirectionalExecutor
from app.execution.live_cap import apply_live_notional_cap
from app.settings import AppConfig, load_config
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SignalPreview,
    SymbolMarketSnapshot,
    TradePlan,
    symbol_market_snapshot_from_mapping,
)
from scripts.run_p109_factor_research_replay import infer_cluster, regime_mode
from scripts.run_p109b_exhaustive_factor_screen import (
    TechSnapshot,
    alma,
    donchian_bucket,
    linear_regression_bucket,
    pivot_bucket,
    volume_weighted_mean,
    weighted_mean,
)


DEFAULT_BASELINE_INPUT = "server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl"
DEFAULT_LIVE_INPUT = "server-data/live_snapshots"
P109_SETUP_PREFIX = "p109c_"


@dataclass(frozen=True, slots=True)
class WindowSpec:
    name: str
    input_path: Path
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True, slots=True)
class PatternContext:
    timestamp: datetime
    timestamp_text: str
    snapshot: SymbolMarketSnapshot
    cluster: str
    regime: str
    tech: TechSnapshot


@dataclass(frozen=True, slots=True)
class PatternMatch:
    pattern: str
    side: str
    horizon_min: int
    score: float
    reason: str
    stop_bps: float
    confidence: float = 0.74


@dataclass(frozen=True, slots=True)
class PatternSpec:
    name: str
    family: str
    description: str
    matcher: Callable[[PatternContext], PatternMatch | None]


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    description: str
    pattern_names: tuple[str, ...]
    max_new_positions_per_bar: int = 2
    max_open_positions: int = 3
    max_open_per_cluster: int = 3


@dataclass(slots=True)
class OverlayState:
    spec: ScenarioSpec
    executor: DirectionalExecutor
    closed_trades: list[dict[str, Any]]
    signal_count: int = 0
    accepted_signal_count: int = 0
    skipped_overlap_count: int = 0
    skipped_capacity_count: int = 0
    skipped_duplicate_count: int = 0
    skipped_cost_count: int = 0
    opened_count: int = 0
    skipped_open_count: int = 0
    pattern_signal_counts: Counter[str] | None = None
    pattern_open_counts: Counter[str] | None = None
    pattern_pnl: Counter[str] | None = None

    def __post_init__(self) -> None:
        self.pattern_signal_counts = Counter()
        self.pattern_open_counts = Counter()
        self.pattern_pnl = Counter()


@dataclass(slots=True)
class WindowResult:
    window: str
    scenario: str
    description: str
    records_processed: int
    first_timestamp: str | None
    last_timestamp: str | None
    runtime_seconds: float
    baseline_pod_a_pnl_usd: float
    baseline_pod_a_trades: int
    baseline_pod_c_pnl_usd: float
    baseline_pod_c_trades: int
    baseline_ac_pnl_usd: float
    baseline_ac_trades: int
    overlay_pnl_usd: float
    overlay_trades: int
    overlay_win_rate: float | None
    overlay_profit_factor: float | None
    overlay_max_drawdown_usd: float
    total_ac_plus_overlay_pnl_usd: float
    delta_vs_current_ac_usd: float
    signal_count: int
    accepted_signal_count: int
    opened_count: int
    skipped_open_count: int
    skipped_overlap_count: int
    skipped_capacity_count: int
    skipped_duplicate_count: int
    skipped_cost_count: int
    pattern_signal_counts: dict[str, int]
    pattern_open_counts: dict[str, int]
    pattern_pnl_usd: dict[str, float]
    pnl_by_symbol: dict[str, float]
    pnl_by_side: dict[str, float]
    close_reasons: dict[str, int]


class RollingIndicatorEngine:
    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}
        self._last_leader_returns: dict[tuple[str, int], float] = {}

    def update_many(self, snapshots: list[SymbolMarketSnapshot]) -> dict[str, TechSnapshot]:
        tech_by_symbol: dict[str, TechSnapshot] = {}
        raw_by_symbol: dict[str, TechSnapshot] = {}
        for snapshot in snapshots:
            raw = self._update_one(snapshot)
            raw_by_symbol[snapshot.symbol] = raw
            if snapshot.symbol in {"BTC", "ETH"}:
                if raw.ret60_bps is not None:
                    self._last_leader_returns[(snapshot.symbol, 60)] = raw.ret60_bps
                if raw.ret240_bps is not None:
                    self._last_leader_returns[(snapshot.symbol, 240)] = raw.ret240_bps
        for symbol, raw in raw_by_symbol.items():
            if symbol in {"BTC", "ETH"}:
                tech_by_symbol[symbol] = raw
                continue
            rel60 = _relative(raw.ret60_bps, self._last_leader_returns, 60)
            rel240 = _relative(raw.ret240_bps, self._last_leader_returns, 240)
            tech_by_symbol[symbol] = TechSnapshot(
                rsi14=raw.rsi14,
                sma20_distance_bps=raw.sma20_distance_bps,
                sma50_distance_bps=raw.sma50_distance_bps,
                ema20_distance_bps=raw.ema20_distance_bps,
                macd_hist_bps=raw.macd_hist_bps,
                macd_cross=raw.macd_cross,
                bollinger_z20=raw.bollinger_z20,
                bollinger_width_bps=raw.bollinger_width_bps,
                stoch14=raw.stoch14,
                donchian20=raw.donchian20,
                ret15_bps=raw.ret15_bps,
                ret60_bps=raw.ret60_bps,
                ret240_bps=raw.ret240_bps,
                rel60_bps=rel60,
                rel240_bps=rel240,
            )
        return tech_by_symbol

    def _update_one(self, snapshot: SymbolMarketSnapshot) -> TechSnapshot:
        state = self._states.setdefault(
            snapshot.symbol,
            {
                "close20": deque(maxlen=20),
                "close50": deque(maxlen=50),
                "close14": deque(maxlen=14),
                "closes": deque(maxlen=60),
                "volumes": deque(maxlen=60),
                "gains14": deque(maxlen=14),
                "losses14": deque(maxlen=14),
                "prices": deque(maxlen=49),
                "true_range14": deque(maxlen=14),
                "ema12": None,
                "ema26": None,
                "ema20": None,
                "ema20_1": None,
                "ema20_2": None,
                "ema20_3": None,
                "prev_ema20_3": None,
                "signal9": None,
                "prev_macd": None,
                "prev_signal": None,
                "prev_price": None,
                "prev_high": None,
                "prev_low": None,
                "prev_day_key": None,
                "current_day_high": None,
                "current_day_low": None,
                "current_day_close": None,
                "previous_day_hlc": None,
                "kama10": None,
                "pvt": 0.0,
                "pvt_history": deque(maxlen=21),
                "sample_count": 0,
            },
        )
        price = float(snapshot.price)
        volume = float(snapshot.bucket_notional_usd or 0.0)
        if volume <= 0.0:
            volume = max(float(snapshot.bucket_volume or 0.0) * price, float(snapshot.volume_ratio or 0.0))
        range_bps = max(float(snapshot.bucket_range_bps or 0.0), 0.0)
        prev_price = state["prev_price"]
        if prev_price is not None and float(prev_price) > 0:
            range_bps = max(range_bps, abs(price / float(prev_price) - 1.0) * 10000.0)
        half_range = price * range_bps / 20000.0
        high = max(price, price + half_range)
        low = max(1e-12, min(price, price - half_range))
        true_range = high - low
        if prev_price is not None:
            delta = price - float(prev_price)
            state["gains14"].append(max(0.0, delta))
            state["losses14"].append(max(0.0, -delta))
            true_range = max(high - low, abs(high - float(prev_price)), abs(low - float(prev_price)))
            state["pvt"] = float(state["pvt"]) + (delta / float(prev_price)) * volume if float(prev_price) > 0 else float(state["pvt"])
        state["true_range14"].append(true_range)
        state["pvt_history"].append(float(state["pvt"]))
        # Replay timestamps are carried outside this method, so use every 288
        # samples as a session proxy for previous-session pivots.
        day_key = int(state["sample_count"]) // 288
        if state["prev_day_key"] is None:
            state["prev_day_key"] = day_key
            state["current_day_high"] = high
            state["current_day_low"] = low
            state["current_day_close"] = price
        elif day_key != state["prev_day_key"]:
            if state["current_day_high"] is not None and state["current_day_low"] is not None and state["current_day_close"] is not None:
                state["previous_day_hlc"] = (
                    float(state["current_day_high"]),
                    float(state["current_day_low"]),
                    float(state["current_day_close"]),
                )
            state["prev_day_key"] = day_key
            state["current_day_high"] = high
            state["current_day_low"] = low
            state["current_day_close"] = price
        else:
            state["current_day_high"] = high if state["current_day_high"] is None else max(float(state["current_day_high"]), high)
            state["current_day_low"] = low if state["current_day_low"] is None else min(float(state["current_day_low"]), low)
            state["current_day_close"] = price
        state["close20"].append(price)
        state["close50"].append(price)
        state["close14"].append(price)
        state["closes"].append(price)
        state["volumes"].append(volume)
        state["prices"].append(price)
        state["ema12"] = _ema(state["ema12"], price, 12)
        state["ema26"] = _ema(state["ema26"], price, 26)
        state["ema20"] = _ema(state["ema20"], price, 20)
        state["ema20_1"] = _ema(state["ema20_1"], price, 20)
        state["ema20_2"] = _ema(state["ema20_2"], float(state["ema20_1"]), 20)
        state["ema20_3"] = _ema(state["ema20_3"], float(state["ema20_2"]), 20)
        macd = float(state["ema12"]) - float(state["ema26"])
        state["signal9"] = _ema(state["signal9"], macd, 9)
        signal = float(state["signal9"])
        macd_cross = _macd_cross(macd, signal, state["prev_macd"], state["prev_signal"])
        close20 = state["close20"]
        close50 = state["close50"]
        close14 = state["close14"]
        sma20 = _mean(close20) if len(close20) >= 20 else None
        sma50 = _mean(close50) if len(close50) >= 50 else None
        sd20 = _sd(close20) if len(close20) >= 20 else None
        rsi = _rsi(state["gains14"], state["losses14"])
        high14 = max(close14) if len(close14) >= 14 else None
        low14 = min(close14) if len(close14) >= 14 else None
        high20 = max(close20) if len(close20) >= 20 else None
        low20 = min(close20) if len(close20) >= 20 else None
        closes = list(state["closes"])
        volumes = list(state["volumes"])
        prices = list(state["prices"])
        wma20 = weighted_mean(closes[-20:]) if len(closes) >= 20 else None
        vwma20 = volume_weighted_mean(closes[-20:], volumes[-20:]) if len(closes) >= 20 else None
        alma20 = alma(closes[-20:]) if len(closes) >= 20 else None
        if len(closes) >= 10:
            recent = closes[-10:]
            change = abs(recent[-1] - recent[0])
            volatility = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent)))
            er = change / volatility if volatility > 0 else 0.0
            fast = 2.0 / 3.0
            slow = 2.0 / 31.0
            sc = (er * (fast - slow) + slow) ** 2
            state["kama10"] = price if state["kama10"] is None else float(state["kama10"]) + sc * (price - float(state["kama10"]))
        tema20 = 3.0 * float(state["ema20_1"]) - 3.0 * float(state["ema20_2"]) + float(state["ema20_3"])
        trix = (
            (float(state["ema20_3"]) / float(state["prev_ema20_3"]) - 1.0) * 10000.0
            if state["prev_ema20_3"] and float(state["prev_ema20_3"]) > 0
            else None
        )
        atr14_bps = (
            (_mean(state["true_range14"]) / price * 10000.0)
            if len(state["true_range14"]) >= 14 and price > 0
            else None
        )
        pvt20 = (
            (float(state["pvt"]) - float(state["pvt_history"][0])) / sum(volumes[-20:]) * 10000.0
            if len(state["pvt_history"]) >= 21 and len(volumes) >= 20 and sum(volumes[-20:]) > 0
            else None
        )
        tech = TechSnapshot(
            rsi14=rsi,
            sma20_distance_bps=((price / sma20 - 1.0) * 10000.0 if sma20 and sma20 > 0 else None),
            sma50_distance_bps=((price / sma50 - 1.0) * 10000.0 if sma50 and sma50 > 0 else None),
            ema20_distance_bps=((price / float(state["ema20"]) - 1.0) * 10000.0 if float(state["ema20"]) > 0 else None),
            wma20_distance_bps=((price / wma20 - 1.0) * 10000.0 if wma20 and wma20 > 0 else None),
            vwma20_distance_bps=((price / vwma20 - 1.0) * 10000.0 if vwma20 and vwma20 > 0 else None),
            kama10_distance_bps=(
                (price / float(state["kama10"]) - 1.0) * 10000.0
                if state["kama10"] and float(state["kama10"]) > 0
                else None
            ),
            alma20_distance_bps=((price / alma20 - 1.0) * 10000.0 if alma20 and alma20 > 0 else None),
            tema20_distance_bps=((price / tema20 - 1.0) * 10000.0 if tema20 > 0 else None),
            macd_hist_bps=((macd - signal) / price * 10000.0 if price > 0 else None),
            macd_cross=macd_cross,
            bollinger_z20=((price - sma20) / (2.0 * sd20) if sma20 and sd20 and sd20 > 0 else None),
            bollinger_width_bps=((4.0 * sd20 / sma20 * 10000.0) if sma20 and sd20 and sma20 > 0 else None),
            stoch14=((price - low14) / (high14 - low14) if high14 and low14 is not None and high14 > low14 else None),
            donchian20=donchian_bucket(price, high20, low20),
            ret15_bps=_ret_from_prices(prices, 3),
            ret60_bps=_ret_from_prices(prices, 12),
            ret240_bps=_ret_from_prices(prices, 48),
            atr14_bps=atr14_bps,
            pivot_standard=pivot_bucket(price, state["previous_day_hlc"]),
            trix_bps=trix,
            pvt20=pvt20,
            linear_regression20=linear_regression_bucket(closes[-20:]) if len(closes) >= 20 else None,
        )
        state["prev_price"] = price
        state["prev_high"] = high
        state["prev_low"] = low
        state["prev_macd"] = macd
        state["prev_signal"] = signal
        state["prev_ema20_3"] = state["ema20_3"]
        state["sample_count"] = int(state["sample_count"]) + 1
        return tech


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-09c complete A/C replay for screened pattern families.")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--live-input", default=DEFAULT_LIVE_INPUT)
    parser.add_argument("--baseline-start", default="2026-04-05T00:00:00Z")
    parser.add_argument("--baseline-end", default="2026-05-13T23:59:59Z")
    parser.add_argument("--live-start", default="2026-05-14T00:00:00Z")
    parser.add_argument("--live-end", default="2026-06-15T23:59:59Z")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--max-spread-crypto-bps", type=float, default=12.0)
    parser.add_argument("--max-spread-tradfi-bps", type=float, default=8.0)
    parser.add_argument("--no-live-caps", action="store_true")
    return parser.parse_args()


def default_patterns() -> list[PatternSpec]:
    return [
        PatternSpec("initial_oil_short_4h_time_gate", "initial_oil_short_4h_time_gate", "Initial P1-09 oil short 240m, 07-10 UTC.", _initial_oil_short_time_gate),
        PatternSpec("initial_crypto_alt_short_4h_weak_basket", "initial_crypto_alt_short_4h_weak_basket", "Initial P1-09 weak alt basket short 240m.", _initial_crypto_alt_short_weak_basket),
        PatternSpec("initial_crypto_high_vol_rebound_60m", "initial_crypto_high_vol_rebound_60m", "Initial P1-09 crypto high-vol rebound long 60m.", _initial_crypto_high_vol_rebound),
        PatternSpec("initial_gold_short_filter_4h", "initial_gold_short_filter_4h", "Initial P1-09 gold short/filter 240m.", _initial_gold_short_filter),
        PatternSpec("crypto_high_vol_short_480_h00", "crypto_high_vol_short_480", "Crypto high-vol short 480m at 00 UTC.", _crypto_high_vol_short_hour(0, 480)),
        PatternSpec("crypto_high_vol_short_480_h01", "crypto_high_vol_short_480", "Crypto high-vol short 480m at 01 UTC.", _crypto_high_vol_short_hour(1, 480)),
        PatternSpec("crypto_high_vol_short_480_h02", "crypto_high_vol_short_480", "Crypto high-vol short 480m at 02 UTC.", _crypto_high_vol_short_hour(2, 480)),
        PatternSpec("crypto_high_vol_short_480_h03", "crypto_high_vol_short_480", "Crypto high-vol short 480m at 03 UTC.", _crypto_high_vol_short_hour(3, 480)),
        PatternSpec("crypto_bollinger_very_wide_short_480", "crypto_expansion_fade_short_480", "Crypto short 480m when Bollinger width is very wide.", _crypto_expansion_short("bollinger_very_wide", 480)),
        PatternSpec("crypto_momentum60_deep_positive_short_480", "crypto_expansion_fade_short_480", "Crypto short 480m after deep-positive 60m momentum.", _crypto_expansion_short("momentum60_deep_positive", 480)),
        PatternSpec("crypto_ma20_deep_positive_short_480", "crypto_expansion_fade_short_480", "Crypto short 480m when SMA/EMA20 distance is deeply positive.", _crypto_expansion_short("ma20_deep_positive", 480)),
        PatternSpec("all50_crypto_atr_high_short_480", "all50_crypto_vol_trend_short_480", "All-50 rerun: crypto short 480m when ATR14 proxy is high.", _all50_crypto_atr_high_short),
        PatternSpec("all50_crypto_trix_deep_negative_short_480", "all50_crypto_vol_trend_short_480", "All-50 rerun: crypto short 480m when TRIX proxy is deeply negative.", _all50_crypto_trix_short(480)),
        PatternSpec("all50_crypto_trix_deep_negative_short_240", "all50_crypto_vol_trend_short_480", "All-50 rerun: crypto short 240m when TRIX proxy is deeply negative.", _all50_crypto_trix_short(240)),
        PatternSpec("all50_crypto_adaptive_ma_deep_positive_short_480", "all50_crypto_ma_pvt_exhaustion_short_480", "All-50 rerun: crypto short 480m when adaptive MA distance is deeply positive.", _all50_crypto_adaptive_ma_short),
        PatternSpec("all50_crypto_pvt_deep_positive_short_480", "all50_crypto_ma_pvt_exhaustion_short_480", "All-50 rerun: crypto short 480m when PVT proxy is deeply positive.", _all50_crypto_pvt_short),
        PatternSpec("all50_equity_pivot_above_r2_long_480", "all50_equity_pivot_480", "All-50 rerun: equity long 480m above previous-session R2 pivot.", _all50_equity_pivot("above_r2", "long")),
        PatternSpec("all50_equity_pivot_below_s2_short_480", "all50_equity_pivot_480", "All-50 rerun: equity short 480m below previous-session S2 pivot.", _all50_equity_pivot("below_s2", "short")),
        PatternSpec("all50_oil_trix_deep_negative_long_120", "all50_oil_trix_reversal", "All-50 rerun: oil long 120m when TRIX proxy is deeply negative.", _all50_oil_trix_long(120)),
        PatternSpec("all50_oil_trix_deep_negative_long_240", "all50_oil_trix_reversal", "All-50 rerun: oil long 240m when TRIX proxy is deeply negative.", _all50_oil_trix_long(240)),
        PatternSpec("oil_chop_h04_short_480", "oil_short_4h8h", "Oil chop short 480m at 04 UTC.", _oil_chop_hour_short(4, 480)),
        PatternSpec("oil_chop_h05_short_480", "oil_short_4h8h", "Oil chop short 480m at 05 UTC.", _oil_chop_hour_short(5, 480)),
        PatternSpec("oil_momentum240_deep_positive_short_480", "oil_short_4h8h", "Oil short 480m after deep-positive 240m momentum.", _oil_momentum_short(480)),
        PatternSpec("oil_momentum240_deep_positive_short_240", "oil_short_4h8h", "Oil short 240m after deep-positive 240m momentum.", _oil_momentum_short(240)),
        PatternSpec("crypto_high_vol_long_h19_120", "crypto_high_vol_long_intraday", "Crypto high-vol long 120m at 19 UTC.", _crypto_high_vol_long_hour(19, 120)),
        PatternSpec("crypto_high_vol_long_h10_240", "crypto_high_vol_long_intraday", "Crypto high-vol long 240m at 10 UTC.", _crypto_high_vol_long_hour(10, 240)),
        PatternSpec("crypto_high_vol_long_h20_240", "crypto_high_vol_long_intraday", "Crypto high-vol long 240m at 20 UTC.", _crypto_high_vol_long_hour(20, 240)),
        PatternSpec("chart_tia_absorption_short_480", "chart_symbol_specific", "TIA absorption-against-flow short 480m.", _chart_tia_absorption_short),
        PatternSpec("chart_dym_saga_relative_weakness_short_480", "chart_symbol_specific", "DYM/SAGA relative-weakness short 480m.", _chart_relative_weakness_short),
        PatternSpec("chart_vvv_strk_rsi_overbought_short_480", "chart_symbol_specific", "VVV/STRK RSI overbought fade short 480m.", _chart_rsi_overbought_short),
        PatternSpec("chart_zec_trend_pullback_long_480", "chart_symbol_specific", "ZEC trend-pullback long 480m.", _chart_zec_trend_pullback_long),
        PatternSpec("silver_bollinger_very_wide_short_480", "silver_observation_only", "Silver very-wide Bollinger short 480m, replay only.", _silver_bollinger_short),
        PatternSpec("calendar_crypto_d2_short_480", "calendar_cluster", "Crypto Wednesday short 480m.", _calendar_cluster("crypto", 2, "short", 480)),
        PatternSpec("calendar_crypto_d4_short_480", "calendar_cluster", "Crypto Friday short 480m.", _calendar_cluster("crypto", 4, "short", 480)),
        PatternSpec("calendar_crypto_d6_long_480", "calendar_cluster", "Crypto Sunday long 480m.", _calendar_cluster("crypto", 6, "long", 480)),
        PatternSpec("calendar_symbol_top_hits_480", "calendar_symbol_top_hits", "Top symbol/day hits from P1-09b as one overfit-control basket.", _calendar_symbol_top_hits),
    ]


def default_scenarios(patterns: list[PatternSpec]) -> list[ScenarioSpec]:
    all_names = tuple(pattern.name for pattern in patterns)
    family_names: dict[str, list[str]] = defaultdict(list)
    for pattern in patterns:
        family_names[pattern.family].append(pattern.name)
    scenarios = [
        ScenarioSpec("current_ac", "Current A/C baseline, no P1-09c overlay.", ()),
    ]
    for family, names in family_names.items():
        scenarios.append(
            ScenarioSpec(
                name=f"p109c_{family}",
                description=f"P1-09c family replay: {family}.",
                pattern_names=tuple(names),
            )
        )
    scenarios.append(
        ScenarioSpec(
            name="p109c_all_non_calendar",
            description="All non-calendar P1-09c pattern families combined.",
            pattern_names=tuple(pattern.name for pattern in patterns if not pattern.family.startswith("calendar")),
            max_new_positions_per_bar=2,
            max_open_positions=4,
            max_open_per_cluster=3,
        )
    )
    scenarios.append(
        ScenarioSpec(
            name="p109c_all_candidates",
            description="All P1-09c candidates combined, including calendar baskets.",
            pattern_names=all_names,
            max_new_positions_per_bar=2,
            max_open_positions=4,
            max_open_per_cluster=3,
        )
    )
    return scenarios


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p109c_pattern_full_replay_{generated_at}")
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    patterns = default_patterns()
    scenarios = default_scenarios(patterns)
    windows = [
        WindowSpec("baseline_apr_may", Path(args.baseline_input), _parse_ts(args.baseline_start), _parse_ts(args.baseline_end)),
        WindowSpec("live_post_baseline", Path(args.live_input), _parse_ts(args.live_start), _parse_ts(args.live_end)),
    ]
    rows: list[WindowResult] = []
    for window in windows:
        print(f"window={window.name} status=running", flush=True)
        window_rows = run_window(
            config=config,
            window=window,
            patterns=patterns,
            scenarios=scenarios,
            notional_usd=args.notional_usd,
            max_spread_crypto_bps=args.max_spread_crypto_bps,
            max_spread_tradfi_bps=args.max_spread_tradfi_bps,
            apply_live_caps=not args.no_live_caps,
        )
        rows.extend(window_rows)
        for row in window_rows:
            print(
                f"window={row.window} scenario={row.scenario} total={row.total_ac_plus_overlay_pnl_usd:.2f} "
                f"delta={row.delta_vs_current_ac_usd:+.2f} overlay={row.overlay_pnl_usd:.2f} "
                f"trades={row.overlay_trades}",
                flush=True,
            )
    decisions = classify_results(rows)
    write_csv(output_dir / "scenario_summary.csv", rows)
    write_pattern_csv(output_dir / "pattern_decisions.csv", decisions)
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "live_caps": not args.no_live_caps,
        "patterns": [
            {
                "name": pattern.name,
                "family": pattern.family,
                "description": pattern.description,
            }
            for pattern in patterns
        ],
        "scenarios": [asdict(scenario) for scenario in scenarios],
        "windows": [asdict(window) | {"input_path": str(window.input_path)} for window in windows],
        "results": [asdict(row) for row in rows],
        "decisions": decisions,
        "method": {
            "baseline": "current Pod A/C replayed in the same pass; overlay P1-09c trades are added as synthetic research sleeve",
            "overlay_overlap_guard": "overlay entries are skipped when baseline A/C or overlay already owns the symbol",
            "exit_model": "DirectionalExecutor with time-stop matching screened horizon and wide research stop",
        },
    }
    (output_dir / "p109c_pattern_full_replay.json").write_text(json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n", encoding="utf-8")
    write_report(output_dir / "p109c_pattern_full_replay.md", rows=rows, decisions=decisions, generated_at=generated_at)
    print(output_dir)


def run_window(
    *,
    config: AppConfig,
    window: WindowSpec,
    patterns: list[PatternSpec],
    scenarios: list[ScenarioSpec],
    notional_usd: float,
    max_spread_crypto_bps: float,
    max_spread_tradfi_bps: float,
    apply_live_caps: bool,
) -> list[WindowResult]:
    helper = FullBotBacktestRunner(config, force_enable_all_pods=True, apply_live_notional_caps=apply_live_caps)
    supervisor = TridentSupervisor(config=config, profile=f"p109c-{window.name}", mode="dry-run")
    pod_a_report = PodABacktestReport(reference_equity_usd=config.trident.capital.reference_equity_usd)
    pod_c_report = PodABacktestReport(reference_equity_usd=config.trident.capital.reference_equity_usd)
    pattern_by_name = {pattern.name: pattern for pattern in patterns}
    states = [
        OverlayState(
            spec=scenario,
            executor=DirectionalExecutor(config),
            closed_trades=[],
        )
        for scenario in scenarios
        if scenario.name != "current_ac"
    ]
    indicators = RollingIndicatorEngine()
    latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
    records_processed = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    started = time.perf_counter()

    for record in helper.loader.iter_merged_jsonl(window.input_path):
        timestamp = _parse_ts(record.timestamp)
        if timestamp is None:
            continue
        if window.start is not None and timestamp < window.start:
            continue
        if window.end is not None and timestamp > window.end:
            continue
        timestamp_text = _iso(timestamp)
        first_timestamp = first_timestamp or timestamp_text
        last_timestamp = timestamp_text
        snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
        latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})
        tech_by_symbol = indicators.update_many(snapshots)

        if record.capture_reason == "maintenance_refresh":
            helper._process_maintenance_record(
                supervisor=supervisor,
                pod_a_report=pod_a_report,
                pod_b_report=PodABacktestReport(),
                pod_c_report=pod_c_report,
                snapshots=snapshots,
                timestamp=timestamp_text,
                source_file=record.source_file,
                stream_source=record.stream_source,
            )
            process_overlay_maintenance(states, helper, config, snapshots, timestamp_text, record.source_file)
            continue

        previous_regime = supervisor.state.regime.value
        cluster_regime_snapshots = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        }
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(**record.regime_snapshot),
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        current_regime = supervisor.state.regime.value
        helper._process_pod_a(
            supervisor=supervisor,
            report=pod_a_report,
            snapshots=snapshots,
            timestamp=timestamp_text,
            source_file=record.source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        helper._process_pod_c(
            supervisor=supervisor,
            report=pod_c_report,
            snapshots=snapshots,
            timestamp=timestamp_text,
            source_file=record.source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        baseline_open_symbols = {
            *helper.pod_a_executor.portfolio.open_positions.keys(),
            *helper.pod_c_executor.portfolio.open_positions.keys(),
        }
        contexts = build_contexts(
            snapshots=snapshots,
            tech_by_symbol=tech_by_symbol,
            timestamp=timestamp,
            timestamp_text=timestamp_text,
            cluster_regime_snapshots=record.cluster_regime_snapshots or {},
        )
        matches_by_pattern = evaluate_patterns(patterns, contexts)
        for state in states:
            process_overlay_state(
                helper=helper,
                config=config,
                state=state,
                pattern_by_name=pattern_by_name,
                matches_by_pattern=matches_by_pattern,
                snapshots=snapshots,
                timestamp=timestamp_text,
                source_file=record.source_file,
                baseline_open_symbols=baseline_open_symbols,
                notional_usd=notional_usd,
                max_spread_crypto_bps=max_spread_crypto_bps,
                max_spread_tradfi_bps=max_spread_tradfi_bps,
                apply_live_caps=apply_live_caps,
            )
        records_processed += 1

    latest_snapshots = list(latest_snapshots_by_symbol.values())
    helper._finalize_directional_report(
        supervisor=supervisor,
        report=pod_a_report,
        executor=helper.pod_a_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
        closed_trade_recorder=helper._record_pod_a_closed_trade,
    )
    helper._finalize_directional_report(
        supervisor=supervisor,
        report=pod_c_report,
        executor=helper.pod_c_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
    )
    for state in states:
        closed, _fills = state.executor.finalize(snapshots=latest_snapshots, timestamp=last_timestamp)
        for trade in closed:
            record_overlay_trade(state, trade)

    pod_a = pod_a_report.to_dict()
    pod_c = pod_c_report.to_dict()
    baseline_a = float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
    baseline_c = float(pod_c.get("realized_pnl_usd", 0.0) or 0.0)
    baseline_a_trades = int(pod_a.get("closed_trade_count", 0) or 0)
    baseline_c_trades = int(pod_c.get("closed_trade_count", 0) or 0)
    baseline_ac = round(baseline_a + baseline_c, 6)
    baseline_trades = baseline_a_trades + baseline_c_trades
    runtime = round(time.perf_counter() - started, 3)
    baseline_row = WindowResult(
        window=window.name,
        scenario="current_ac",
        description="Current A/C baseline, no P1-09c overlay.",
        records_processed=records_processed,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        runtime_seconds=runtime,
        baseline_pod_a_pnl_usd=round(baseline_a, 6),
        baseline_pod_a_trades=baseline_a_trades,
        baseline_pod_c_pnl_usd=round(baseline_c, 6),
        baseline_pod_c_trades=baseline_c_trades,
        baseline_ac_pnl_usd=baseline_ac,
        baseline_ac_trades=baseline_trades,
        overlay_pnl_usd=0.0,
        overlay_trades=0,
        overlay_win_rate=None,
        overlay_profit_factor=None,
        overlay_max_drawdown_usd=0.0,
        total_ac_plus_overlay_pnl_usd=baseline_ac,
        delta_vs_current_ac_usd=0.0,
        signal_count=0,
        accepted_signal_count=0,
        opened_count=0,
        skipped_open_count=0,
        skipped_overlap_count=0,
        skipped_capacity_count=0,
        skipped_duplicate_count=0,
        skipped_cost_count=0,
        pattern_signal_counts={},
        pattern_open_counts={},
        pattern_pnl_usd={},
        pnl_by_symbol={},
        pnl_by_side={},
        close_reasons={},
    )
    return [baseline_row] + [
        build_window_result(
            state=state,
            window=window,
            description=state.spec.description,
            records_processed=records_processed,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            runtime=runtime,
            baseline_a=baseline_a,
            baseline_a_trades=baseline_a_trades,
            baseline_c=baseline_c,
            baseline_c_trades=baseline_c_trades,
            baseline_ac=baseline_ac,
            baseline_trades=baseline_trades,
        )
        for state in states
    ]


def build_contexts(
    *,
    snapshots: list[SymbolMarketSnapshot],
    tech_by_symbol: dict[str, TechSnapshot],
    timestamp: datetime,
    timestamp_text: str,
    cluster_regime_snapshots: dict[str, Any],
) -> list[PatternContext]:
    contexts = []
    for snapshot in snapshots:
        cluster = infer_cluster(snapshot.symbol, snapshot.market_cluster)
        regime_payload = cluster_regime_snapshots.get(cluster) if isinstance(cluster_regime_snapshots, dict) else None
        regime = regime_mode(regime_payload if isinstance(regime_payload, dict) else None, cluster)
        contexts.append(
            PatternContext(
                timestamp=timestamp,
                timestamp_text=timestamp_text,
                snapshot=snapshot,
                cluster=cluster,
                regime=regime,
                tech=tech_by_symbol.get(snapshot.symbol, TechSnapshot()),
            )
        )
    return contexts


def evaluate_patterns(
    patterns: list[PatternSpec],
    contexts: list[PatternContext],
) -> dict[str, list[tuple[PatternContext, PatternMatch]]]:
    matches: dict[str, list[tuple[PatternContext, PatternMatch]]] = {pattern.name: [] for pattern in patterns}
    for context in contexts:
        for pattern in patterns:
            match = pattern.matcher(context)
            if match is not None:
                matches[pattern.name].append((context, match))
    return matches


def process_overlay_maintenance(
    states: list[OverlayState],
    helper: FullBotBacktestRunner,
    config: AppConfig,
    snapshots: list[SymbolMarketSnapshot],
    timestamp: str,
    source_file: str,
) -> None:
    for state in states:
        execution = state.executor.process_record(
            snapshots=snapshots,
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp=timestamp,
            managed_symbols=set(snapshot.symbol for snapshot in snapshots),
        )
        for trade in execution.closed_trades:
            record_overlay_trade(state, trade)
        state.skipped_open_count += len(execution.skipped_open_symbols)


def process_overlay_state(
    *,
    helper: FullBotBacktestRunner,
    config: AppConfig,
    state: OverlayState,
    pattern_by_name: dict[str, PatternSpec],
    matches_by_pattern: dict[str, list[tuple[PatternContext, PatternMatch]]],
    snapshots: list[SymbolMarketSnapshot],
    timestamp: str,
    source_file: str,
    baseline_open_symbols: set[str],
    notional_usd: float,
    max_spread_crypto_bps: float,
    max_spread_tradfi_bps: float,
    apply_live_caps: bool,
) -> None:
    del source_file
    candidates: list[tuple[PatternContext, PatternMatch]] = []
    for pattern_name in state.spec.pattern_names:
        candidates.extend(matches_by_pattern.get(pattern_name, []))
    candidates.sort(key=lambda item: (item[1].score, item[0].snapshot.symbol), reverse=True)
    plans: list[TradePlan] = []
    seen_symbols: set[str] = set()
    open_by_cluster = Counter(
        infer_cluster(symbol)
        for symbol in state.executor.portfolio.open_positions.keys()
    )
    open_count = len(state.executor.portfolio.open_positions)
    for context, match in candidates:
        state.signal_count += 1
        assert state.pattern_signal_counts is not None
        state.pattern_signal_counts[match.pattern] += 1
        symbol = context.snapshot.symbol
        max_spread = max_spread_crypto_bps if context.cluster == "crypto" else max_spread_tradfi_bps
        if context.snapshot.spread_bps > max_spread:
            state.skipped_cost_count += 1
            continue
        if symbol in seen_symbols:
            state.skipped_duplicate_count += 1
            continue
        if symbol in baseline_open_symbols or state.executor.portfolio.has_open_position(symbol):
            state.skipped_overlap_count += 1
            continue
        if open_count >= state.spec.max_open_positions or open_by_cluster[context.cluster] >= state.spec.max_open_per_cluster:
            state.skipped_capacity_count += 1
            continue
        plan = build_overlay_plan(
            context=context,
            match=match,
            notional_usd=notional_usd,
        )
        plans.append(plan)
        seen_symbols.add(symbol)
        open_count += 1
        open_by_cluster[context.cluster] += 1
        state.accepted_signal_count += 1
        if len(plans) >= state.spec.max_new_positions_per_bar:
            break
    if apply_live_caps:
        leverage = LeveragePolicy(config.pod_c)
        plans = [
            apply_live_notional_cap(
                plan,
                config.trident.execution.live_max_order_notional_usd,
                max_leverage=leverage.max_allowed(plan.symbol),
            )
            for plan in plans
        ]
    decisions = [RiskDecision(accepted=True, reason="accepted", trade_plan=plan) for plan in plans]
    previews = [
        SignalPreview(
            symbol=plan.symbol,
            side=plan.side,
            setup=plan.setup,
            confidence=plan.confidence,
            setup_details=dict(plan.setup_details),
        )
        for plan in plans
    ]
    managed_symbols = set(snapshot.symbol for snapshot in snapshots) - baseline_open_symbols
    execution = state.executor.process_record(
        snapshots=snapshots,
        risk_decisions=decisions,
        signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
        timestamp=timestamp,
        entry_allowed_symbols=managed_symbols,
        managed_symbols=managed_symbols,
    )
    state.opened_count += len(execution.opened_symbols)
    state.skipped_open_count += len(execution.skipped_open_symbols)
    opened = set(execution.opened_symbols)
    for plan in plans:
        if plan.symbol in opened:
            assert state.pattern_open_counts is not None
            state.pattern_open_counts[str(plan.setup_details.get("p109c_pattern", plan.setup))] += 1
    for trade in execution.closed_trades:
        record_overlay_trade(state, trade)


def build_overlay_plan(*, context: PatternContext, match: PatternMatch, notional_usd: float) -> TradePlan:
    horizon_hours = max(1, math.ceil(match.horizon_min / 60.0))
    margin = round(notional_usd / 2.0, 6)
    expected_loss = round(notional_usd * match.stop_bps / 10000.0, 6)
    return TradePlan(
        symbol=context.snapshot.symbol,
        side=match.side,
        setup=f"{P109_SETUP_PREFIX}{match.pattern}",
        confidence=match.confidence,
        target_notional_usd=notional_usd,
        stop_bps=match.stop_bps,
        time_stop_hours=horizon_hours,
        take_profit_bps=0.0,
        break_even_trigger_bps=0.0,
        trailing_activation_bps=0.0,
        trailing_distance_bps=0.0,
        reentry_cooldown_minutes=match.horizon_min,
        margin_usd=margin,
        requested_leverage=2.0,
        effective_leverage=2.0,
        risk_budget_usd=expected_loss,
        expected_loss_usd=expected_loss,
        setup_details={
            "p109c_pattern": match.pattern,
            "p109c_family": match.pattern.rsplit("_", 1)[0],
            "p109c_reason": match.reason,
            "p109c_horizon_min": match.horizon_min,
            "market_cluster": context.cluster,
            "cluster_regime": context.regime,
            "hour_utc": context.timestamp.hour,
            "dow_utc": context.timestamp.weekday(),
            "spread_bps": round(context.snapshot.spread_bps, 4),
            "rsi14": _round_optional(context.tech.rsi14),
            "ret60_bps": _round_optional(context.tech.ret60_bps),
            "ret240_bps": _round_optional(context.tech.ret240_bps),
            "bollinger_width_bps": _round_optional(context.tech.bollinger_width_bps),
        },
    )


def record_overlay_trade(state: OverlayState, trade: Any) -> None:
    row = asdict(trade)
    state.closed_trades.append(row)
    pattern = str((row.get("setup_details") or {}).get("p109c_pattern") or row.get("setup") or "unknown")
    assert state.pattern_pnl is not None
    state.pattern_pnl[pattern] += float(row.get("pnl_usd", 0.0) or 0.0)


def build_window_result(
    *,
    state: OverlayState,
    window: WindowSpec,
    description: str,
    records_processed: int,
    first_timestamp: str | None,
    last_timestamp: str | None,
    runtime: float,
    baseline_a: float,
    baseline_a_trades: int,
    baseline_c: float,
    baseline_c_trades: int,
    baseline_ac: float,
    baseline_trades: int,
) -> WindowResult:
    pnls = [float(row.get("pnl_usd", 0.0) or 0.0) for row in state.closed_trades]
    overlay_pnl = round(sum(pnls), 6)
    by_symbol = defaultdict(float)
    by_side = defaultdict(float)
    close_reasons = Counter()
    for row in state.closed_trades:
        by_symbol[str(row.get("symbol", ""))] += float(row.get("pnl_usd", 0.0) or 0.0)
        by_side[str(row.get("side", ""))] += float(row.get("pnl_usd", 0.0) or 0.0)
        close_reasons[str(row.get("close_reason", ""))] += 1
    return WindowResult(
        window=window.name,
        scenario=state.spec.name,
        description=description,
        records_processed=records_processed,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        runtime_seconds=runtime,
        baseline_pod_a_pnl_usd=round(baseline_a, 6),
        baseline_pod_a_trades=baseline_a_trades,
        baseline_pod_c_pnl_usd=round(baseline_c, 6),
        baseline_pod_c_trades=baseline_c_trades,
        baseline_ac_pnl_usd=baseline_ac,
        baseline_ac_trades=baseline_trades,
        overlay_pnl_usd=overlay_pnl,
        overlay_trades=len(pnls),
        overlay_win_rate=_win_rate(pnls),
        overlay_profit_factor=_profit_factor(pnls),
        overlay_max_drawdown_usd=round(_max_drawdown(pnls), 6),
        total_ac_plus_overlay_pnl_usd=round(baseline_ac + overlay_pnl, 6),
        delta_vs_current_ac_usd=overlay_pnl,
        signal_count=state.signal_count,
        accepted_signal_count=state.accepted_signal_count,
        opened_count=state.opened_count,
        skipped_open_count=state.skipped_open_count,
        skipped_overlap_count=state.skipped_overlap_count,
        skipped_capacity_count=state.skipped_capacity_count,
        skipped_duplicate_count=state.skipped_duplicate_count,
        skipped_cost_count=state.skipped_cost_count,
        pattern_signal_counts=dict(state.pattern_signal_counts or {}),
        pattern_open_counts=dict(state.pattern_open_counts or {}),
        pattern_pnl_usd={key: round(value, 6) for key, value in (state.pattern_pnl or {}).items()},
        pnl_by_symbol={key: round(value, 6) for key, value in by_symbol.items()},
        pnl_by_side={key: round(value, 6) for key, value in by_side.items()},
        close_reasons=dict(close_reasons),
    )


def classify_results(rows: list[WindowResult]) -> list[dict[str, Any]]:
    by_scenario: dict[str, list[WindowResult]] = defaultdict(list)
    for row in rows:
        if row.scenario != "current_ac":
            by_scenario[row.scenario].append(row)
    decisions = []
    for scenario, items in sorted(by_scenario.items()):
        baseline = next((row for row in items if row.window == "baseline_apr_may"), None)
        live = next((row for row in items if row.window == "live_post_baseline"), None)
        total_trades = sum(row.overlay_trades for row in items)
        total_pnl = sum(row.overlay_pnl_usd for row in items)
        status = "research_only"
        reason = "resultat incomplet"
        if baseline is not None and live is not None:
            if total_trades < 20:
                status = "research_only"
                reason = "sample trop faible en replay complet"
            elif baseline.overlay_pnl_usd > 0 and live.overlay_pnl_usd > 0 and (live.overlay_profit_factor or 0.0) >= 1.15:
                if scenario.startswith("p109c_calendar"):
                    status = "research_only"
                    reason = "positif mais pur calendrier/symbole: walk-forward requis avant shadow"
                else:
                    status = "promouvable_shadow"
                    reason = "positif baseline et live; shadow possible apres revue conflits/couts"
            elif live.overlay_pnl_usd <= 0:
                status = "rejetee"
                reason = "fenetre live/post-baseline negative"
            elif baseline.overlay_pnl_usd < 0:
                status = "research_only"
                reason = "positif live mais degrade baseline"
            else:
                status = "research_only"
                reason = "positif partiel mais PF/sample insuffisant"
        decisions.append(
            {
                "scenario": scenario,
                "status": status,
                "reason": reason,
                "total_overlay_pnl_usd": round(total_pnl, 6),
                "total_overlay_trades": total_trades,
                "baseline_delta_usd": baseline.overlay_pnl_usd if baseline else None,
                "live_delta_usd": live.overlay_pnl_usd if live else None,
                "baseline_trades": baseline.overlay_trades if baseline else None,
                "live_trades": live.overlay_trades if live else None,
                "live_profit_factor": live.overlay_profit_factor if live else None,
                "live_max_drawdown_usd": live.overlay_max_drawdown_usd if live else None,
            }
        )
    return decisions


def write_csv(path: Path, rows: list[WindowResult]) -> None:
    fieldnames = list(asdict(rows[0]).keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = asdict(row)
            writer.writerow({key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in payload.items()})


def write_pattern_csv(path: Path, decisions: list[dict[str, Any]]) -> None:
    fieldnames = list(decisions[0].keys()) if decisions else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(decisions)


def write_report(path: Path, *, rows: list[WindowResult], decisions: list[dict[str, Any]], generated_at: str) -> None:
    by_window = {(row.window, row.scenario): row for row in rows}
    lines = [
        "# P1-09c - Replay complet des patterns screenes",
        "",
        f"- Genere le: `{generated_at}`",
        "- Statut: `research_only_no_live_change`",
        "- Methode: baseline Pod A/C courante + overlay P1-09c synthetique, positions overlay skippees en cas d'overlap avec A/C.",
        "",
        "| Scenario | Decision | Baseline delta | Live delta | Live trades | Live PF | Live DD | Raison |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for decision in decisions:
        lines.append(
            f"| `{decision['scenario']}` | `{decision['status']}` | "
            f"{_fmt(decision['baseline_delta_usd'])} | {_fmt(decision['live_delta_usd'])} | "
            f"{decision['live_trades']} | {_fmt(decision['live_profit_factor'])} | "
            f"{_fmt(decision['live_max_drawdown_usd'])} | {decision['reason']} |"
        )
    lines.append("")
    lines.append("## Details par fenetre")
    for row in rows:
        if row.scenario == "current_ac":
            lines.append(
                f"- `{row.window}` baseline A/C: `{row.baseline_ac_pnl_usd:.2f}` USD "
                f"(`Pod A {row.baseline_pod_a_pnl_usd:.2f}`, `Pod C {row.baseline_pod_c_pnl_usd:.2f}`), "
                f"`{row.baseline_ac_trades}` trades."
            )
    for decision in decisions:
        lines.append("")
        lines.append(f"### {decision['scenario']}")
        for window in ("baseline_apr_may", "live_post_baseline"):
            row = by_window.get((window, decision["scenario"]))
            if row is None:
                continue
            lines.append(
                f"- `{window}`: overlay `{row.overlay_pnl_usd:.2f}` USD, trades `{row.overlay_trades}`, "
                f"WR `{_pct(row.overlay_win_rate)}`, PF `{_fmt(row.overlay_profit_factor)}`, DD `{row.overlay_max_drawdown_usd:.2f}`, "
                f"opens `{row.opened_count}`, overlap skips `{row.skipped_overlap_count}`, capacity skips `{row.skipped_capacity_count}`."
            )
            top_patterns = sorted(row.pattern_pnl_usd.items(), key=lambda item: item[1], reverse=True)[:5]
            if top_patterns:
                lines.append(f"  Top pattern PnL: `{top_patterns}`.")
    lines.append("")
    lines.append("## Garde-fous")
    lines.append("- Les patterns restent research: pas de changement live, pas de fetch/export p109_* tant qu'un shadow n'est pas code.")
    lines.append("- Les rules calendrier/symbole restent suspectes de multiple testing meme si le replay complet est positif.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _crypto_high_vol_short_hour(hour: int, horizon: int) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != "crypto" or ctx.regime != "high_vol" or ctx.timestamp.hour != hour:
            return None
        score = 100.0 + abs(ctx.snapshot.vwap_distance_bps) * 0.05 + max(0.0, ctx.tech.ret60_bps or 0.0) * 0.02
        return PatternMatch(f"crypto_high_vol_short_{horizon}_h{hour:02d}", "short", horizon, score, f"crypto high_vol h{hour:02d}", 500.0)
    return matcher


def _crypto_expansion_short(kind: str, horizon: int) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != "crypto":
            return None
        if kind == "bollinger_very_wide" and not ((ctx.tech.bollinger_width_bps or 0.0) >= 350.0):
            return None
        if kind == "momentum60_deep_positive" and not ((ctx.tech.ret60_bps or 0.0) >= 100.0):
            return None
        if kind == "ma20_deep_positive" and not (
            (ctx.tech.sma20_distance_bps or 0.0) >= 60.0 or (ctx.tech.ema20_distance_bps or 0.0) >= 60.0
        ):
            return None
        score = 80.0 + max(0.0, ctx.tech.ret60_bps or 0.0) * 0.04 + max(0.0, ctx.tech.bollinger_width_bps or 0.0) * 0.01
        return PatternMatch(f"crypto_{kind}_short_{horizon}", "short", horizon, score, kind, 500.0)
    return matcher


def _all50_crypto_atr_high_short(ctx: PatternContext) -> PatternMatch | None:
    if ctx.cluster != "crypto" or (ctx.tech.atr14_bps or 0.0) < 60.0:
        return None
    score = 70.0 + min(ctx.tech.atr14_bps or 0.0, 180.0) * 0.20
    return PatternMatch("all50_crypto_atr_high_short_480", "short", 480, score, "crypto ATR14 high all-50 proxy", 550.0)


def _all50_crypto_trix_short(horizon: int) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != "crypto" or (ctx.tech.trix_bps or 0.0) > -16.0:
            return None
        score = 75.0 + min(abs(ctx.tech.trix_bps or 0.0), 120.0) * 0.35
        return PatternMatch(
            f"all50_crypto_trix_deep_negative_short_{horizon}",
            "short",
            horizon,
            score,
            "crypto TRIX deep negative all-50 proxy",
            550.0,
        )

    return matcher


def _all50_crypto_adaptive_ma_short(ctx: PatternContext) -> PatternMatch | None:
    if ctx.cluster != "crypto":
        return None
    distances = [
        ctx.tech.wma20_distance_bps,
        ctx.tech.vwma20_distance_bps,
        ctx.tech.kama10_distance_bps,
        ctx.tech.alma20_distance_bps,
        ctx.tech.tema20_distance_bps,
    ]
    best = max((value for value in distances if value is not None), default=0.0)
    if best < 60.0:
        return None
    score = 70.0 + min(best, 180.0) * 0.15
    return PatternMatch(
        "all50_crypto_adaptive_ma_deep_positive_short_480",
        "short",
        480,
        score,
        "crypto adaptive MA deep-positive all-50 proxy",
        550.0,
    )


def _all50_crypto_pvt_short(ctx: PatternContext) -> PatternMatch | None:
    if ctx.cluster != "crypto" or (ctx.tech.pvt20 or 0.0) < 60.0:
        return None
    score = 65.0 + min(ctx.tech.pvt20 or 0.0, 240.0) * 0.10
    return PatternMatch("all50_crypto_pvt_deep_positive_short_480", "short", 480, score, "crypto PVT deep-positive all-50 proxy", 550.0)


def _all50_equity_pivot(pivot_bucket_name: str, side: str) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != "equity" or ctx.tech.pivot_standard != pivot_bucket_name:
            return None
        score = 70.0 + abs(ctx.snapshot.vwap_distance_bps) * 0.05
        return PatternMatch(
            f"all50_equity_pivot_{pivot_bucket_name}_{side}_480",
            side,
            480,
            score,
            f"equity pivot {pivot_bucket_name} all-50 proxy",
            260.0,
            0.72,
        )

    return matcher


def _all50_oil_trix_long(horizon: int) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != "oil" or (ctx.tech.trix_bps or 0.0) > -16.0:
            return None
        score = 70.0 + min(abs(ctx.tech.trix_bps or 0.0), 120.0) * 0.25
        return PatternMatch(
            f"all50_oil_trix_deep_negative_long_{horizon}",
            "long",
            horizon,
            score,
            "oil TRIX deep-negative long all-50 proxy",
            180.0,
            0.72,
        )

    return matcher


def _oil_chop_hour_short(hour: int, horizon: int) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != "oil" or ctx.regime != "chop" or ctx.timestamp.hour != hour:
            return None
        score = 80.0 + max(0.0, ctx.tech.ret240_bps or 0.0) * 0.05
        return PatternMatch(f"oil_chop_h{hour:02d}_short_{horizon}", "short", horizon, score, f"oil chop h{hour:02d}", 180.0, 0.72)
    return matcher


def _oil_momentum_short(horizon: int) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != "oil" or (ctx.tech.ret240_bps or 0.0) < 180.0:
            return None
        score = 70.0 + (ctx.tech.ret240_bps or 0.0) * 0.08
        return PatternMatch(f"oil_momentum240_deep_positive_short_{horizon}", "short", horizon, score, "oil momentum240 deep positive", 180.0, 0.72)
    return matcher


def _initial_oil_short_time_gate(ctx: PatternContext) -> PatternMatch | None:
    if ctx.snapshot.symbol not in {"XYZ:CL", "XYZ:BRENTOIL"}:
        return None
    if ctx.regime not in {"chop", "mixed", "high_vol"}:
        return None
    if not (7 <= ctx.timestamp.hour < 10):
        return None
    vol_short = float(getattr(ctx.snapshot, "realized_vol_short_bps", 0.0) or 0.0)
    score = (
        max(0.0, 10.0 - ctx.snapshot.spread_bps)
        + max(0.0, ctx.snapshot.vwap_distance_bps) * 0.10
        + max(0.0, vol_short - 6.0) * 0.15
    )
    reason = f"initial oil short 240m; regime={ctx.regime}; hour={ctx.timestamp.hour}"
    return PatternMatch("oil_short_4h_time_gate", "short", 240, score, reason, 180.0, 0.72)


def _initial_crypto_alt_short_weak_basket(ctx: PatternContext) -> PatternMatch | None:
    if ctx.snapshot.symbol not in {"PENGU", "TIA", "VVV", "STRK", "ZRO", "ICP", "SAGA", "DYM"}:
        return None
    if ctx.regime not in {"mixed", "high_vol", "broad_up", "positive_breadth"}:
        return None
    weak_reasons: list[str] = []
    rel60 = ctx.tech.rel60_bps
    rel240 = ctx.tech.rel240_bps
    if ctx.snapshot.structure_score <= -0.10:
        weak_reasons.append("structure_weak")
    if ctx.snapshot.trade_flow_bias <= -0.30:
        weak_reasons.append("flow_sell")
    if rel60 is not None and rel60 <= -15.0:
        weak_reasons.append("rel60_weak")
    if rel240 is not None and rel240 <= -40.0:
        weak_reasons.append("rel240_weak")
    if ctx.snapshot.vwap_distance_bps >= 5.0 and ctx.snapshot.trade_flow_bias <= 0.0:
        weak_reasons.append("stretched_without_buy_flow")
    if not weak_reasons:
        return None
    score = (
        len(weak_reasons) * 10.0
        + max(0.0, -(rel60 or 0.0)) * 0.10
        + max(0.0, ctx.snapshot.vwap_distance_bps) * 0.05
        - ctx.snapshot.spread_bps
    )
    reason = f"initial crypto weak basket short 240m; reasons={'+'.join(weak_reasons)}"
    return PatternMatch("crypto_alt_short_4h_weak_basket", "short", 240, score, reason, 650.0)


def _initial_crypto_high_vol_rebound(ctx: PatternContext) -> PatternMatch | None:
    if ctx.cluster != "crypto" or ctx.regime != "high_vol":
        return None
    if ctx.snapshot.vwap_distance_bps > -15.0:
        return None
    if not (
        ctx.snapshot.trade_flow_bias >= 0.0
        or ctx.snapshot.microprice_dislocation_bps >= 0.0
        or ctx.snapshot.book_imbalance >= 0.15
    ):
        return None
    score = (
        max(0.0, -ctx.snapshot.vwap_distance_bps) * 0.30
        + max(0.0, ctx.snapshot.trade_flow_bias) * 8.0
        + max(0.0, ctx.snapshot.microprice_dislocation_bps) * 0.10
        - ctx.snapshot.spread_bps
    )
    reason = f"initial crypto high-vol rebound long 60m; vwap_bps={ctx.snapshot.vwap_distance_bps:.2f}"
    return PatternMatch("crypto_high_vol_rebound_60m", "long", 60, score, reason, 450.0)


def _initial_gold_short_filter(ctx: PatternContext) -> PatternMatch | None:
    if ctx.snapshot.symbol != "XYZ:GOLD":
        return None
    if ctx.regime not in {"downtrend", "mixed"}:
        return None
    if not (
        ctx.snapshot.structure_score <= -0.10
        or ctx.snapshot.trade_flow_bias <= -0.30
        or ctx.snapshot.vwap_distance_bps >= 5.0
    ):
        return None
    score = (
        max(0.0, -ctx.snapshot.structure_score) * 20.0
        + max(0.0, -ctx.snapshot.trade_flow_bias) * 8.0
        + max(0.0, ctx.snapshot.vwap_distance_bps) * 0.05
        - ctx.snapshot.spread_bps
    )
    reason = f"initial gold short/filter 240m; regime={ctx.regime}; structure={ctx.snapshot.structure_score:.2f}"
    return PatternMatch("gold_short_filter_4h", "short", 240, score, reason, 180.0, 0.72)


def _crypto_high_vol_long_hour(hour: int, horizon: int) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != "crypto" or ctx.regime != "high_vol" or ctx.timestamp.hour != hour:
            return None
        if (ctx.snapshot.trade_flow_bias < 0.0 and ctx.snapshot.microprice_dislocation_bps < 0.0):
            return None
        score = 80.0 + max(0.0, -ctx.snapshot.vwap_distance_bps) * 0.08 + max(0.0, ctx.snapshot.trade_flow_bias) * 10.0
        return PatternMatch(f"crypto_high_vol_long_h{hour:02d}_{horizon}", "long", horizon, score, f"crypto high_vol long h{hour:02d}", 450.0)
    return matcher


def _chart_tia_absorption_short(ctx: PatternContext) -> PatternMatch | None:
    if ctx.snapshot.symbol != "TIA":
        return None
    if ctx.snapshot.book_imbalance <= -0.25 and ctx.snapshot.trade_flow_bias >= 0.30 and ctx.snapshot.microprice_dislocation_bps <= -0.15:
        return PatternMatch("chart_tia_absorption_short_480", "short", 480, 95.0, "TIA absorption against buy flow", 500.0)
    return None


def _chart_relative_weakness_short(ctx: PatternContext) -> PatternMatch | None:
    if ctx.snapshot.symbol not in {"DYM", "SAGA"}:
        return None
    if (ctx.tech.rel60_bps or 0.0) <= -80.0 and ctx.snapshot.trade_flow_bias <= 0.0:
        score = 90.0 + abs(ctx.tech.rel60_bps or 0.0) * 0.03
        return PatternMatch("chart_dym_saga_relative_weakness_short_480", "short", 480, score, "relative weakness short", 650.0)
    return None


def _chart_rsi_overbought_short(ctx: PatternContext) -> PatternMatch | None:
    if ctx.snapshot.symbol not in {"VVV", "STRK"}:
        return None
    if (ctx.tech.rsi14 or 0.0) >= 65.0 and ctx.snapshot.trade_flow_bias <= 0.0:
        score = 80.0 + ((ctx.tech.rsi14 or 65.0) - 65.0)
        return PatternMatch("chart_vvv_strk_rsi_overbought_short_480", "short", 480, score, "RSI overbought fade", 650.0)
    return None


def _chart_zec_trend_pullback_long(ctx: PatternContext) -> PatternMatch | None:
    if ctx.snapshot.symbol != "ZEC":
        return None
    if ctx.snapshot.structure_score >= 0.20 and -12.0 <= ctx.snapshot.vwap_distance_bps <= 3.0 and ctx.snapshot.trade_flow_bias >= 0.25:
        return PatternMatch("chart_zec_trend_pullback_long_480", "long", 480, 80.0, "ZEC trend pullback", 500.0)
    return None


def _silver_bollinger_short(ctx: PatternContext) -> PatternMatch | None:
    if ctx.cluster != "silver" or (ctx.tech.bollinger_width_bps or 0.0) < 350.0:
        return None
    return PatternMatch("silver_bollinger_very_wide_short_480", "short", 480, 60.0, "silver Bollinger very wide", 160.0, 0.72)


def _calendar_cluster(cluster: str, dow: int, side: str, horizon: int) -> Callable[[PatternContext], PatternMatch | None]:
    def matcher(ctx: PatternContext) -> PatternMatch | None:
        if ctx.cluster != cluster or ctx.timestamp.weekday() != dow:
            return None
        return PatternMatch(f"calendar_{cluster}_d{dow}_{side}_{horizon}", side, horizon, 30.0, "calendar cluster hit", 500.0)
    return matcher


def _calendar_symbol_top_hits(ctx: PatternContext) -> PatternMatch | None:
    symbol = ctx.snapshot.symbol
    dow = ctx.timestamp.weekday()
    rules: dict[tuple[str, int], str] = {
        ("TON", 0): "long",
        ("NEAR", 0): "long",
        ("INJ", 0): "long",
        ("ZEC", 6): "long",
        ("HYPE", 6): "long",
        ("ONDO", 4): "short",
        ("VVV", 2): "short",
        ("SAGA", 4): "short",
        ("PENDLE", 2): "short",
        ("TIA", 4): "short",
    }
    side = rules.get((symbol, dow))
    if side is None:
        return None
    return PatternMatch("calendar_symbol_top_hits_480", side, 480, 50.0, f"symbol/dow top hit {symbol} d{dow}", 600.0)


def _ema(prev: float | None, value: float, length: int) -> float:
    alpha = 2.0 / (length + 1.0)
    return value if prev is None else prev + alpha * (value - prev)


def _mean(values: deque[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sd(values: deque[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _rsi(gains: deque[float], losses: deque[float]) -> float | None:
    if len(gains) < 14 or len(losses) < 14:
        return None
    avg_gain = _mean(gains)
    avg_loss = _mean(losses)
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _macd_cross(macd: float, signal: float, prev_macd: float | None, prev_signal: float | None) -> str:
    if prev_macd is not None and prev_signal is not None:
        if prev_macd <= prev_signal and macd > signal:
            return "cross_up"
        if prev_macd >= prev_signal and macd < signal:
            return "cross_down"
    return "bull" if macd > signal else "bear"


def _ret_from_prices(prices: list[float], lookback_steps: int) -> float | None:
    if len(prices) <= lookback_steps:
        return None
    prev = prices[-lookback_steps - 1]
    now = prices[-1]
    if prev <= 0:
        return None
    return (now / prev - 1.0) * 10000.0


def _relative(local: float | None, leaders: dict[tuple[str, int], float], lookback: int) -> float | None:
    if local is None:
        return None
    values = [value for (symbol, horizon), value in leaders.items() if horizon == lookback and symbol in {"BTC", "ETH"}]
    if not values:
        return None
    return local - sum(values) / len(values)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _round_optional(value: float | None) -> float | str:
    return round(value, 4) if value is not None else ""


def _win_rate(pnls: list[float]) -> float | None:
    return (sum(1 for pnl in pnls if pnl > 0) / len(pnls)) if pnls else None


def _profit_factor(pnls: list[float]) -> float | None:
    gains = sum(pnl for pnl in pnls if pnl > 0)
    losses = -sum(pnl for pnl in pnls if pnl < 0)
    return gains / losses if losses > 0 else None


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "na"
    return f"{float(value):.2f}"


def _pct(value: float | None) -> str:
    return "na" if value is None else f"{value:.1%}"


if __name__ == "__main__":
    main()
