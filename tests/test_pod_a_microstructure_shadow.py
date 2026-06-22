from app.trident.pod_a.microstructure_shadow import microstructure_shadow_setup_details
from app.trident.pod_a.signals import AnchorTrendContext


def test_microstructure_shadow_scores_supportive_context_above_adverse_context() -> None:
    supportive = microstructure_shadow_setup_details(
        _context(
            spread_bps=1.0,
            book_imbalance=0.24,
            trade_flow_bias=0.28,
            bucket_trade_count=48,
            bucket_notional_usd=18_000.0,
            bucket_range_bps=14.0,
            bid_depth_10bps=220.0,
            ask_depth_10bps=90.0,
            microprice_dislocation_bps=1.1,
            volume_ratio=2.0,
            trade_count_ratio=2.2,
        ),
        side="long",
    )
    adverse = microstructure_shadow_setup_details(
        _context(
            spread_bps=7.5,
            book_imbalance=-0.22,
            trade_flow_bias=-0.26,
            bucket_trade_count=2,
            bucket_notional_usd=40.0,
            bucket_range_bps=130.0,
            bid_depth_10bps=35.0,
            ask_depth_10bps=220.0,
            microprice_dislocation_bps=-1.1,
            delta_spread_bps=1.8,
            delta_book_imbalance=0.42,
            delta_trade_flow_bias=0.38,
        ),
        side="long",
    )

    assert supportive["microstructure_shadow_score"] > adverse["microstructure_shadow_score"]
    assert supportive["microstructure_shadow_bucket"] in {"ok", "strong"}
    assert adverse["microstructure_shadow_bucket"] == "poor"


def test_microstructure_shadow_interprets_flow_and_depth_by_side() -> None:
    context = _context(
        spread_bps=1.0,
        book_imbalance=-0.24,
        trade_flow_bias=-0.28,
        bucket_trade_count=30,
        bucket_notional_usd=12_000.0,
        bucket_range_bps=16.0,
        bid_depth_10bps=80.0,
        ask_depth_10bps=240.0,
        microprice_dislocation_bps=-1.0,
        volume_ratio=1.8,
        trade_count_ratio=1.7,
    )

    long_details = microstructure_shadow_setup_details(context, side="long")
    short_details = microstructure_shadow_setup_details(context, side="short")

    assert short_details["microstructure_shadow_score"] > long_details["microstructure_shadow_score"]
    assert short_details["microstructure_shadow_flow"] > 0.0
    assert long_details["microstructure_shadow_flow"] < 0.0


def test_microstructure_shadow_marks_missing_optional_book_features() -> None:
    details = microstructure_shadow_setup_details(_context(), side="long")

    assert details["microstructure_shadow_active"] is True
    assert "microprice" in str(details["microstructure_shadow_missing_flags"])
    assert "depth" in str(details["microstructure_shadow_missing_flags"])


def _context(**overrides: object) -> AnchorTrendContext:
    base = {
        "symbol": "ETH",
        "regime": "TrendExpansion",
        "price": 3000.0,
        "ema_fast": 3010.0,
        "ema_slow": 2980.0,
        "vwap_distance_bps": -4.0,
        "structure_score": 0.65,
        "funding_rate": 0.0,
        "spread_bps": 1.2,
        "btc_aligned": True,
    }
    base.update(overrides)
    return AnchorTrendContext(**base)
