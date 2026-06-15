from __future__ import annotations

from app.trident.pod_c.external_reference_shadow import (
    external_reference_shadow_details,
)


def test_shadow_flags_abs_premium_without_changing_live_action() -> None:
    details = external_reference_shadow_details(
        {
            "external_reference_source_count": 1,
            "external_reference_age_seconds": 60.0,
            "external_premium_bps": -75.0,
            "external_momentum_300s_bps": 4.0,
        },
        side="long",
    )

    assert details["external_reference_shadow_mode"] == "observation_only"
    assert details["would_block_external_reference_abs_premium_gt_50"] is True
    assert details["would_block_external_reference_candidate_default_5m"] is True
    assert details["external_reference_shadow_live_action_unchanged"] is True


def test_shadow_flags_missing_reference_for_candidate_gates_only() -> None:
    details = external_reference_shadow_details(
        {"external_reference_source_count": 0},
        side="long",
    )

    assert details["external_reference_shadow_available"] is False
    assert details["would_block_external_reference_abs_premium_gt_50"] is False
    assert details["would_block_external_reference_candidate_loose_5m"] is True
    assert details["would_block_external_reference_candidate_default_5m"] is True
    assert details["external_reference_shadow_reason"] == "missing_reference"


def test_shadow_flags_counter_momentum_against_side() -> None:
    details = external_reference_shadow_details(
        {
            "external_reference_source_count": 1,
            "external_reference_age_seconds": 60.0,
            "external_premium_bps": 5.0,
            "external_momentum_300s_bps": -8.0,
        },
        side="long",
    )

    assert details["would_block_external_reference_counter_momentum_5m_6bps"] is True
    assert "counter_momentum_5m_6bps" in details["external_reference_shadow_reason"]
