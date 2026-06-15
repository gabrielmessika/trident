from datetime import datetime, timezone

from app.trident.types import SymbolMarketSnapshot
from scripts.run_p109b_exhaustive_factor_screen import TechSnapshot
from scripts.run_p109c_pattern_full_replay import (
    PatternContext,
    _calendar_symbol_top_hits,
    _crypto_expansion_short,
    _crypto_high_vol_short_hour,
    _initial_crypto_alt_short_weak_basket,
    _initial_crypto_high_vol_rebound,
    _initial_gold_short_filter,
    _initial_oil_short_time_gate,
    _oil_momentum_short,
    classify_results,
)


def _ctx(
    *,
    symbol: str = "BTC",
    cluster: str = "crypto",
    regime: str = "high_vol",
    hour: int = 0,
    dow: int = 2,
    tech: TechSnapshot | None = None,
) -> PatternContext:
    dt = datetime(2026, 6, 3, hour, tzinfo=timezone.utc)
    while dt.weekday() != dow:
        dt = dt.replace(day=dt.day + 1)
    return PatternContext(
        timestamp=dt,
        timestamp_text=dt.isoformat().replace("+00:00", "Z"),
        snapshot=SymbolMarketSnapshot(
            symbol=symbol,
            price=100.0,
            ema_fast=100.0,
            ema_slow=100.0,
            vwap_distance_bps=20.0,
            structure_score=0.0,
            funding_rate=0.0,
            spread_bps=2.0,
            btc_aligned=True,
            market_cluster=cluster,
            trade_flow_bias=0.0,
        ),
        cluster=cluster,
        regime=regime,
        tech=tech or TechSnapshot(),
    )


def test_initial_oil_short_time_gate_matches_p109_window() -> None:
    match = _initial_oil_short_time_gate(_ctx(symbol="XYZ:CL", cluster="oil", regime="mixed", hour=8))

    assert match is not None
    assert match.side == "short"
    assert match.horizon_min == 240


def test_initial_crypto_weak_basket_requires_weakness_reason() -> None:
    matcher_ctx = _ctx(
        symbol="TIA",
        cluster="crypto",
        regime="mixed",
        tech=TechSnapshot(rel60_bps=-30.0),
    )
    neutral_ctx = _ctx(symbol="TIA", cluster="crypto", regime="mixed")
    neutral_ctx.snapshot.vwap_distance_bps = 0.0

    assert _initial_crypto_alt_short_weak_basket(matcher_ctx) is not None
    assert _initial_crypto_alt_short_weak_basket(neutral_ctx) is None


def test_initial_crypto_high_vol_rebound_requires_depressed_vwap() -> None:
    depressed = _ctx(regime="high_vol")
    depressed.snapshot.vwap_distance_bps = -20.0
    depressed.snapshot.trade_flow_bias = 0.10
    neutral = _ctx(regime="high_vol")
    neutral.snapshot.vwap_distance_bps = -5.0

    assert _initial_crypto_high_vol_rebound(depressed) is not None
    assert _initial_crypto_high_vol_rebound(neutral) is None


def test_initial_gold_short_filter_matches_negative_structure() -> None:
    ctx = _ctx(symbol="XYZ:GOLD", cluster="gold", regime="downtrend")
    ctx.snapshot.structure_score = -0.20

    match = _initial_gold_short_filter(ctx)

    assert match is not None
    assert match.side == "short"


def test_crypto_high_vol_short_hour_matches_only_requested_hour() -> None:
    matcher = _crypto_high_vol_short_hour(1, 480)

    assert matcher(_ctx(hour=1)) is not None
    assert matcher(_ctx(hour=2)) is None


def test_crypto_expansion_short_matches_bollinger_width() -> None:
    matcher = _crypto_expansion_short("bollinger_very_wide", 480)

    assert matcher(_ctx(tech=TechSnapshot(bollinger_width_bps=400.0))) is not None
    assert matcher(_ctx(tech=TechSnapshot(bollinger_width_bps=100.0))) is None


def test_oil_momentum_short_requires_oil_and_positive_240m_momentum() -> None:
    matcher = _oil_momentum_short(480)

    assert matcher(_ctx(symbol="XYZ:CL", cluster="oil", regime="chop", tech=TechSnapshot(ret240_bps=220.0))) is not None
    assert matcher(_ctx(symbol="BTC", cluster="crypto", regime="high_vol", tech=TechSnapshot(ret240_bps=220.0))) is None


def test_calendar_symbol_top_hits_maps_selected_symbol_day() -> None:
    match = _calendar_symbol_top_hits(_ctx(symbol="ONDO", cluster="crypto", dow=4))

    assert match is not None
    assert match.side == "short"


def test_classify_rejects_negative_live_window() -> None:
    class Row:
        def __init__(self, scenario: str, window: str, pnl: float, trades: int = 30) -> None:
            self.scenario = scenario
            self.window = window
            self.overlay_pnl_usd = pnl
            self.overlay_trades = trades
            self.overlay_profit_factor = 1.2
            self.overlay_max_drawdown_usd = 10.0

    decisions = classify_results(
        [
            Row("p109c_test", "baseline_apr_may", 5.0),
            Row("p109c_test", "live_post_baseline", -1.0),
        ]
    )

    assert decisions[0]["status"] == "rejetee"


def test_classify_keeps_calendar_positive_result_research_only() -> None:
    class Row:
        def __init__(self, scenario: str, window: str, pnl: float, trades: int = 30) -> None:
            self.scenario = scenario
            self.window = window
            self.overlay_pnl_usd = pnl
            self.overlay_trades = trades
            self.overlay_profit_factor = 1.4
            self.overlay_max_drawdown_usd = 10.0

    decisions = classify_results(
        [
            Row("p109c_calendar_cluster", "baseline_apr_may", 5.0),
            Row("p109c_calendar_cluster", "live_post_baseline", 20.0),
        ]
    )

    assert decisions[0]["status"] == "research_only"
