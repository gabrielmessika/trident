from argparse import Namespace

from scripts.run_p109b_exhaustive_factor_screen import (
    TechSnapshot,
    classify,
    donchian_bucket,
    indicator_keys,
    parse_horizons,
    tradingview_top50_coverage,
)
from tests.test_p109_factor_research_replay import _obs


def test_parse_horizons_accepts_sorted_5m_multiples() -> None:
    assert parse_horizons("60,5,15,60") == [5, 15, 60]


def test_donchian_bucket_detects_edges() -> None:
    assert donchian_bucket(105.0, 105.0, 95.0) == "breakout_up"
    assert donchian_bucket(95.0, 105.0, 95.0) == "breakdown_down"
    assert donchian_bucket(104.0, 105.0, 95.0) == "range_top"


def test_indicator_keys_skip_neutral_buckets_by_default() -> None:
    args = Namespace(include_neutral_indicator_buckets=False, include_symbol_indicators=False)
    keys = indicator_keys(
        _obs(),
        TechSnapshot(
            rsi14=50.0,
            bollinger_z20=0.0,
            donchian20="range_mid",
            ret60_bps=200.0,
        ),
        args,
    )

    assert ("indicator_cluster", "oil", "momentum60", "deep_positive") in keys
    assert ("indicator_cluster", "oil", "rsi14", "neutral") not in keys


def test_indicator_keys_include_all_50_proxy_families() -> None:
    args = Namespace(include_neutral_indicator_buckets=False, include_symbol_indicators=False)
    keys = indicator_keys(
        _obs(),
        TechSnapshot(
            atr14_bps=80.0,
            supertrend="bull_above_band",
            volume_profile20="at_high_volume_node",
            mfi14=82.0,
            anchored_vwap_distance_bps=-20.0,
            technical_rating="sell",
        ),
        args,
    )

    assert ("indicator_cluster", "oil", "atr14", "high") in keys
    assert ("indicator_cluster", "oil", "supertrend", "bull_above_band") in keys
    assert ("indicator_cluster", "oil", "volume_profile20", "at_high_volume_node") in keys
    assert ("indicator_cluster", "oil", "mfi14", "overbought") in keys
    assert ("indicator_cluster", "oil", "anchored_vwap", "deep_below_vwap") in keys
    assert ("indicator_cluster", "oil", "technical_rating", "sell") in keys


def test_tradingview_top50_coverage_is_explicit() -> None:
    coverage = tradingview_top50_coverage()

    assert len(coverage) == 50
    assert all(row["used"] for row in coverage)
    assert coverage[0]["indicator"] == "Moving Average / SMA"
    assert coverage[-1]["indicator"] == "Technical Ratings"


def test_classify_candidate_screen_hit_requires_positive_months() -> None:
    assert classify(
        {
            "n": 300,
            "mean_net_bps": 12.0,
            "profit_factor": 1.3,
            "positive_months": 2,
        }
    ) == "candidate_next_replay"
    assert classify(
        {
            "n": 300,
            "mean_net_bps": 12.0,
            "profit_factor": 1.3,
            "positive_months": 1,
        }
    ) == "one_period_only"
