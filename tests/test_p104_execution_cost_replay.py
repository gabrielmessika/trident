from app.settings import load_config
from scripts.run_p104_execution_cost_replay import (
    SpreadAwareDryRunVenue,
    era_for,
    expected_entry_cost_bps,
    parse_timestamp,
)


def test_expected_entry_cost_uses_half_spread_model_from_config() -> None:
    assert expected_entry_cost_bps(
        spread_bps=10.0,
        dry_run_spread_multiplier=0.5,
        dry_run_slippage_bps=0.5,
    ) == 5.5


def test_spread_aware_venue_rejects_expensive_open_without_touching_close() -> None:
    config = load_config("config/trident.toml")
    venue = SpreadAwareDryRunVenue(
        config.trident.execution,
        max_spread_bps=6.0,
        max_expected_entry_cost_bps=None,
    )

    fill = venue.open_fill(
        symbol="ETH",
        side="long",
        mid_price=100.0,
        spread_bps=7.0,
        notional_usd=100.0,
        timestamp="2026-06-01T00:00:00Z",
    )
    close = venue.close_fill(
        symbol="ETH",
        side="long",
        mid_price=100.0,
        spread_bps=7.0,
        notional_usd=100.0,
        timestamp="2026-06-01T00:01:00Z",
    )

    assert fill is None
    assert venue.last_block_reason_by_symbol["ETH"] == "p104_spread_above_6bps"
    assert close is not None


def test_era_for_maps_known_live_periods() -> None:
    assert era_for(parse_timestamp("2026-05-25T00:00:00Z")) == "era_1_stop_immediate_bug"
    assert era_for(parse_timestamp("2026-06-05T00:00:00Z")) == "era_2_stop_grace_165_cat_300"
    assert era_for(parse_timestamp("2026-06-10T00:00:00Z")) == "era_3_quality_sizing_efe"
