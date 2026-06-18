from app.trident.pod_c.oil_shadow import (
    build_p109_oil_shadow_features,
    p109_oil_research_regime,
    p109_oil_shadow_details,
)
from app.trident.types import RegimeSnapshot, SymbolMarketSnapshot
from scripts.export_trident_audit_pack import compact_setup_details


def _oil_snapshot(symbol: str = "XYZ:CL") -> SymbolMarketSnapshot:
    return SymbolMarketSnapshot(
        symbol=symbol,
        price=80.0,
        ema_fast=80.1,
        ema_slow=80.0,
        vwap_distance_bps=8.0,
        structure_score=0.0,
        funding_rate=0.0,
        spread_bps=2.0,
        btc_aligned=True,
        market_cluster="oil",
        realized_vol_short_bps=10.0,
    )


def test_oil_shadow_matches_p109_time_gate() -> None:
    features = build_p109_oil_shadow_features(
        snapshot=_oil_snapshot(),
        timestamp="2026-06-15T08:00:00Z",
        cluster_regime_snapshot=RegimeSnapshot(
            ready=True,
            adx=10.0,
            atr_ratio=0.2,
            range_width_bps=10.0,
            coherence_score=0.1,
        ),
    )

    details = p109_oil_shadow_details(features)

    assert details["p109_oil_shadow_mode"] == "observation_only"
    assert details["would_open_p109_oil_short_shadow"] is True
    assert details["p109_oil_shadow_live_action_unchanged"] is True
    assert details["p109_oil_shadow_horizon_min"] == 240.0


def test_oil_shadow_blocks_outside_time_gate() -> None:
    features = build_p109_oil_shadow_features(
        snapshot=_oil_snapshot(),
        timestamp="2026-06-15T11:00:00Z",
        cluster_regime_snapshot=RegimeSnapshot(ready=True, adx=10.0, coherence_score=0.1),
    )

    assert features is not None
    assert features.would_open is False
    assert "hour_outside_07_10" in features.reason


def test_oil_research_regime_maps_high_vol_before_trend() -> None:
    regime = p109_oil_research_regime(
        RegimeSnapshot(
            ready=True,
            adx=30.0,
            atr_ratio=1.0,
            range_width_bps=5.0,
            structure_score=0.4,
            coherence_score=0.9,
        )
    )

    assert regime == "high_vol"


def test_non_oil_symbol_returns_no_shadow() -> None:
    features = build_p109_oil_shadow_features(
        snapshot=_oil_snapshot(symbol="XYZ:GOLD"),
        timestamp="2026-06-15T08:00:00Z",
        cluster_regime_snapshot=RegimeSnapshot(ready=True),
    )

    assert features is None


def test_oil_promoted_fields_are_kept_in_compact_export() -> None:
    compacted = compact_setup_details(
        {
            "p109_oil_shadow_mode": "observation_only",
            "p109_oil_promoted": True,
            "p109_oil_promoted_mode": "active",
            "p109_oil_promoted_setup": "p109_oil_short_4h_time_gate",
            "p109_oil_promoted_live_action": "short_entry_candidate",
            "p109_oil_promoted_confidence": 0.684,
        }
    )

    assert compacted["p109_oil_promoted"] is True
    assert compacted["p109_oil_promoted_setup"] == "p109_oil_short_4h_time_gate"
