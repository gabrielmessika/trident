import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_p108_expected_live_action_change_passes_when_live_sizing_enabled(tmp_path: Path) -> None:
    payload = run_review_with_p108_log(tmp_path, live_sizing_enabled=True)

    guard = payload["dynamic_symbol_guard"]
    assert payload["status"] == "PASS"
    assert guard["live_action_unchanged_false"] == 1
    assert guard["expected_live_action_changed"] == 1
    assert guard["unexpected_live_action_changed"] == 0
    assert guard["live_sizing_active_records"] == 1


def test_p108_live_action_change_fails_when_policy_disabled(tmp_path: Path) -> None:
    payload = run_review_with_p108_log(tmp_path, live_sizing_enabled=False)

    guard = payload["dynamic_symbol_guard"]
    assert payload["status"] == "FAIL"
    assert guard["live_action_unchanged_false"] == 1
    assert guard["expected_live_action_changed"] == 0
    assert guard["unexpected_live_action_changed"] == 1


def run_review_with_p108_log(tmp_path: Path, *, live_sizing_enabled: bool) -> dict:
    data_dir = tmp_path / "server-data"
    write_minimal_review_inputs(data_dir, live_sizing_enabled=live_sizing_enabled)

    subprocess.run(
        [
            "bash",
            "scripts/fetch_trident_data.sh",
            "--review-only",
            "--local-dir",
            str(data_dir),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    review_dirs = sorted((data_dir / "reviews").glob("*"))
    assert review_dirs
    return json.loads((review_dirs[-1] / "p108_dynamic_symbol_guard_audit.json").read_text())


def write_minimal_review_inputs(data_dir: Path, *, live_sizing_enabled: bool) -> None:
    for name in ("api", "logs", "runtime", "config"):
        (data_dir / name).mkdir(parents=True, exist_ok=True)
    (data_dir / "api" / "health-20260101_000000.json").write_text('{"status":"ok"}\n')
    (data_dir / "api" / "report-20260101_000000.json").write_text(
        json.dumps(
            {
                "pods": [
                    {"pod": "pod_a", "healthy": True},
                    {"pod": "pod_c", "healthy": True},
                ]
            }
        )
        + "\n"
    )
    status = {
        "live_trading_paused": False,
        "live_reconciliation": {
            "ready": True,
            "reasons": [],
            "unknown_exchange_positions": [],
            "missing_exchange_positions": [],
            "side_mismatches": [],
            "open_orders": [],
            "trigger_orders": [],
        },
    }
    (data_dir / "runtime" / "pod_a_live_status.json").write_text(json.dumps(status) + "\n")
    (data_dir / "runtime" / "pod_c_live_status.json").write_text(json.dumps(status) + "\n")
    (data_dir / "config" / "trident.toml").write_text(
        "\n".join(
            [
                "[trident.execution]",
                "live_max_order_notional_usd = 200",
                "live_block_stop_grace_setups = false",
                "live_stop_grace_catastrophic_sl_bps = 120",
                "",
                "[pod_a]",
                "stop_grace_minutes = 60",
                f"dynamic_symbol_guard_live_sizing_enabled = {str(live_sizing_enabled).lower()}",
                "dynamic_symbol_guard_recovery_sizing_enabled = false",
                "dynamic_symbol_guard_throttle_multiplier = 0.50",
                "dynamic_symbol_guard_quarantine_multiplier = 0.50",
                "dynamic_symbol_guard_min_multiplier = 0.10",
                "",
                "[pod_c]",
                'blocked_symbols = ["XYZ:SILVER"]',
                "",
                "[pod_c.cluster_modes.silver]",
                "enabled = false",
            ]
        )
        + "\n"
    )
    record = {
        "event_type": "signal",
        "signal": {
            "symbol": "ENA",
            "setup_details": {
                "symbol_guard_shadow_mode": "observation_only",
                "symbol_guard_state": "throttle",
                "falling_knife_score": 62.5,
                "would_throttle_dynamic_symbol_guard": True,
                "would_block_dynamic_symbol_guard": False,
                "would_reduce_cap_dynamic_symbol_guard": True,
                "dynamic_symbol_guard_live_policy_enabled": True,
                "dynamic_symbol_guard_live_sizing_active": True,
                "dynamic_symbol_guard_live_sizing_multiplier": 0.5,
                "dynamic_symbol_guard_live_sizing_reason": "throttle",
                "symbol_guard_live_action_unchanged": False,
            },
        },
    }
    (data_dir / "logs" / "pod_a_live.jsonl").write_text(json.dumps(record) + "\n")
    (data_dir / "logs" / "pod_c_live.jsonl").write_text("")
