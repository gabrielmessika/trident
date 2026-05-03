from __future__ import annotations

import copy

from app.live.runtime_status import runtime_status_age_seconds, runtime_status_is_fresh


def merge_runtime_supervisor_snapshot(
    *payloads: dict[str, object] | None,
    base_snapshot: dict[str, object] | None = None,
) -> dict[str, object] | None:
    sources: list[tuple[float, str, dict[str, object], dict[str, object]]] = []
    for payload in payloads:
        if not runtime_status_is_fresh(payload):
            continue
        if not isinstance(payload, dict):
            continue
        supervisor = payload.get("supervisor")
        if not isinstance(supervisor, dict):
            continue
        age_seconds = runtime_status_age_seconds(payload)
        if age_seconds is None:
            continue
        sources.append((age_seconds, _updated_at_value(payload), payload, supervisor))

    if not sources:
        return copy.deepcopy(base_snapshot) if isinstance(base_snapshot, dict) else None

    ordered = sorted(sources, key=lambda item: (item[1], item[0]))
    freshest_supervisor = max(sources, key=lambda item: (-item[0], item[1]))[3]
    merged = (
        copy.deepcopy(base_snapshot)
        if isinstance(base_snapshot, dict)
        else copy.deepcopy(ordered[0][3])
    )

    for key in (
        "profile",
        "mode",
        "regime",
        "raw_regime",
        "kill_switch",
        "allocations",
        "regime_snapshot",
        "pending_regime",
        "pending_regime_count",
        "regime_transition_count",
        "regime_evaluation_count",
        "regime_history",
        "routing_overrides",
    ):
        if key in freshest_supervisor:
            merged[key] = copy.deepcopy(freshest_supervisor[key])

    merged["enabled_pods"] = _merge_enabled_pods(merged, [item[3] for item in ordered])
    merged["ownership_conflicts"] = _merge_rows_by_identity(
        existing=merged.get("ownership_conflicts"),
        supervisors=[item[3] for item in ordered],
        key="ownership_conflicts",
        identity_keys=("symbol", "requested_by", "owner"),
    )
    merged["symbol_ownership"] = _merge_rows_by_symbol(
        existing=merged.get("symbol_ownership"),
        supervisors=[item[3] for item in ordered],
        key="symbol_ownership",
    )
    merged["symbol_routing"] = _merge_rows_by_symbol(
        existing=merged.get("symbol_routing"),
        supervisors=[item[3] for item in ordered],
        key="symbol_routing",
    )
    merged["local_regime_by_symbol"] = _merge_rows_by_symbol(
        existing=merged.get("local_regime_by_symbol"),
        supervisors=[item[3] for item in ordered],
        key="local_regime_by_symbol",
    )
    merged["local_regime_transitions"] = _merge_rows_by_identity(
        existing=merged.get("local_regime_transitions"),
        supervisors=[item[3] for item in ordered],
        key="local_regime_transitions",
        identity_keys=("recorded_at", "symbol", "new_local_regime"),
    )
    merged["symbol_reassignment_count_by_symbol"] = _merge_symbol_counts(
        merged.get("symbol_reassignment_count_by_symbol"),
        [item[3].get("symbol_reassignment_count_by_symbol") for item in ordered],
    )
    merged["pods"] = _merge_pod_state_maps(
        existing=merged.get("pods"),
        sources=ordered,
        key="pods",
    )
    merged["capital_plan"] = _merge_capital_plan(
        existing=merged.get("capital_plan"),
        freshest_supervisor=freshest_supervisor,
        sources=ordered,
    )
    previews = _merge_directional_previews(
        base_snapshot=merged,
        sources=ordered,
    )
    merged.update(previews)
    merged["source"] = "runtime_merged_view"
    return merged


def _merge_enabled_pods(
    merged: dict[str, object],
    supervisors: list[dict[str, object]],
) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def consume(items: object) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            name = str(item).strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)

    consume(merged.get("enabled_pods"))
    for supervisor in supervisors:
        consume(supervisor.get("enabled_pods"))
    return names


def _merge_rows_by_symbol(
    *,
    existing: object,
    supervisors: list[dict[str, object]],
    key: str,
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict):
                symbol = str(item.get("symbol", "")).upper()
                if symbol:
                    merged[symbol] = copy.deepcopy(item)
    for supervisor in supervisors:
        rows = supervisor.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            merged[symbol] = copy.deepcopy(item)
    return [merged[symbol] for symbol in sorted(merged)]


def _merge_rows_by_identity(
    *,
    existing: object,
    supervisors: list[dict[str, object]],
    key: str,
    identity_keys: tuple[str, ...],
) -> list[dict[str, object]]:
    merged: dict[tuple[str, ...], dict[str, object]] = {}

    def consume(rows: object) -> None:
        if not isinstance(rows, list):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            identity = tuple(str(item.get(field, "")) for field in identity_keys)
            if not any(identity):
                continue
            merged[identity] = copy.deepcopy(item)

    consume(existing)
    for supervisor in supervisors:
        consume(supervisor.get(key))
    return [merged[identity] for identity in sorted(merged)]


def _merge_symbol_counts(existing: object, sources: list[object]) -> dict[str, int]:
    merged: dict[str, int] = {}
    if isinstance(existing, dict):
        for symbol, count in existing.items():
            merged[str(symbol).upper()] = int(count)
    for source in sources:
        if not isinstance(source, dict):
            continue
        for symbol, count in source.items():
            normalized = str(symbol).upper()
            merged[normalized] = max(merged.get(normalized, 0), int(count))
    return dict(sorted(merged.items()))


def _merge_pod_state_maps(
    *,
    existing: object,
    sources: list[tuple[float, str, dict[str, object], dict[str, object]]],
    key: str,
) -> dict[str, dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    if isinstance(existing, dict):
        for pod_name, payload in existing.items():
            if isinstance(payload, dict):
                merged[str(pod_name)] = copy.deepcopy(payload)
    for _, _, runtime_payload, supervisor in sources:
        pod_states = supervisor.get(key)
        if not isinstance(pod_states, dict):
            continue
        runtime_pod = str(runtime_payload.get("pod", "")).strip().lower()
        if runtime_pod and runtime_pod in pod_states and isinstance(pod_states[runtime_pod], dict):
            merged[runtime_pod] = copy.deepcopy(pod_states[runtime_pod])
            continue
        for pod_name, payload in pod_states.items():
            if isinstance(payload, dict):
                merged[str(pod_name)] = copy.deepcopy(payload)
    return merged


def _merge_capital_plan(
    *,
    existing: object,
    freshest_supervisor: dict[str, object],
    sources: list[tuple[float, str, dict[str, object], dict[str, object]]],
) -> dict[str, object]:
    base = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    freshest_capital = freshest_supervisor.get("capital_plan")
    if isinstance(freshest_capital, dict):
        for key in ("regime", "total_equity_usd", "cash_pct", "cash_usd"):
            if key in freshest_capital:
                base[key] = copy.deepcopy(freshest_capital[key])
    merged_pods = {}
    existing_pods = base.get("pods")
    if isinstance(existing_pods, dict):
        for pod_name, payload in existing_pods.items():
            if isinstance(payload, dict):
                merged_pods[str(pod_name)] = copy.deepcopy(payload)
    for _, _, runtime_payload, supervisor in sources:
        capital_plan = supervisor.get("capital_plan")
        if not isinstance(capital_plan, dict):
            continue
        pods = capital_plan.get("pods")
        if not isinstance(pods, dict):
            continue
        runtime_pod = str(runtime_payload.get("pod", "")).strip().lower()
        if runtime_pod and runtime_pod in pods and isinstance(pods[runtime_pod], dict):
            merged_pods[runtime_pod] = copy.deepcopy(pods[runtime_pod])
            continue
        for pod_name, payload in pods.items():
            if isinstance(payload, dict):
                merged_pods[str(pod_name)] = copy.deepcopy(payload)
    base["pods"] = merged_pods
    return base


def _merge_directional_previews(
    *,
    base_snapshot: dict[str, object],
    sources: list[tuple[float, str, dict[str, object], dict[str, object]]],
) -> dict[str, object]:
    previews = {
        "pod_a_signal_preview": copy.deepcopy(base_snapshot.get("pod_a_signal_preview", [])),
        "pod_b_signal_preview": copy.deepcopy(base_snapshot.get("pod_b_signal_preview", [])),
        "pod_c_signal_preview": copy.deepcopy(base_snapshot.get("pod_c_signal_preview", [])),
        "pod_a_signal_review": copy.deepcopy(base_snapshot.get("pod_a_signal_review", [])),
        "pod_b_signal_review": copy.deepcopy(base_snapshot.get("pod_b_signal_review", [])),
        "pod_c_signal_review": copy.deepcopy(base_snapshot.get("pod_c_signal_review", [])),
    }
    freshest_supervisor = max(sources, key=lambda item: (-item[0], item[1]))[3]
    for key in (
        "pod_a_signal_preview",
        "pod_b_signal_preview",
        "pod_c_signal_preview",
        "pod_a_signal_review",
        "pod_b_signal_review",
        "pod_c_signal_review",
    ):
        if key in freshest_supervisor and not previews.get(key):
            previews[key] = copy.deepcopy(freshest_supervisor[key])
    for _, _, runtime_payload, supervisor in sources:
        runtime_pod = str(runtime_payload.get("pod", "")).strip().lower()
        if runtime_pod == "pod_a" and "pod_a_signal_preview" in supervisor:
            previews["pod_a_signal_preview"] = copy.deepcopy(supervisor["pod_a_signal_preview"])
        if runtime_pod == "pod_a" and "pod_a_signal_review" in supervisor:
            previews["pod_a_signal_review"] = copy.deepcopy(supervisor["pod_a_signal_review"])
        if runtime_pod == "pod_b" and "pod_b_signal_preview" in supervisor:
            previews["pod_b_signal_preview"] = copy.deepcopy(supervisor["pod_b_signal_preview"])
        if runtime_pod == "pod_b" and "pod_b_signal_review" in supervisor:
            previews["pod_b_signal_review"] = copy.deepcopy(supervisor["pod_b_signal_review"])
        if runtime_pod == "pod_c" and "pod_c_signal_preview" in supervisor:
            previews["pod_c_signal_preview"] = copy.deepcopy(supervisor["pod_c_signal_preview"])
        if runtime_pod == "pod_c" and "pod_c_signal_review" in supervisor:
            previews["pod_c_signal_review"] = copy.deepcopy(supervisor["pod_c_signal_review"])
    return previews


def _updated_at_value(payload: dict[str, object]) -> str:
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str):
        return ""
    return updated_at
