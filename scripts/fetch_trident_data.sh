#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

usage() {
    cat <<'EOF'
Usage: ./scripts/fetch_trident_data.sh [--days N] [--date YYYY-MM-DD] [--all] [--logs-only] [--snapshots-only] [--review-only] [--dry-run] [--remote-dir /opt/trident] [--local-dir server-data]

Rapatrie les données TRIDENT A/C depuis le serveur, sans Pod B/HIP-4:
- /health, /api/state, /api/report, /api/metrics
- snapshots live A/C
- logs et runtime Pod A / Pod C
- diagnostic P1-03 `external_reference_*` Pod C dans la review générée
- configs TRIDENT
- logs Docker TRIDENT A/C

HIP-4 est rapatrié séparément par ./trident-hip4/fetch_data.sh.
EOF
}

HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
SSH_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
REMOTE_DIR="${TRIDENT_DEPLOY_DIR:-/opt/trident}"
LOCAL_DIR="server-data"
DAYS=3
DATE_FILTER=""
FETCH_ALL=""
LOGS_ONLY=""
SNAPSHOTS_ONLY=""
REVIEW_ONLY=""
DRY_RUN=""
LOG_LINES="${TRIDENT_FETCH_LOG_LINES:-300}"

while [ $# -gt 0 ]; do
    case "$1" in
        --host)
            HOST="$2"
            shift 2
            ;;
        --user)
            SSH_USER="$2"
            shift 2
            ;;
        --identity)
            IDENTITY_FILE="$2"
            shift 2
            ;;
        --remote-dir)
            REMOTE_DIR="$2"
            shift 2
            ;;
        --local-dir)
            LOCAL_DIR="$2"
            shift 2
            ;;
        --days)
            DAYS="$2"
            shift 2
            ;;
        --date)
            DATE_FILTER="$2"
            shift 2
            ;;
        --all)
            FETCH_ALL="true"
            shift
            ;;
        --logs-only)
            LOGS_ONLY="true"
            shift
            ;;
        --snapshots-only)
            SNAPSHOTS_ONLY="true"
            shift
            ;;
        --review-only)
            REVIEW_ONLY="true"
            shift
            ;;
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            error "Option inconnue: $1"
            usage
            exit 1
            ;;
    esac
done

if [ -n "$LOGS_ONLY" ] && [ -n "$SNAPSHOTS_ONLY" ]; then
    error "--logs-only et --snapshots-only sont incompatibles"
    exit 1
fi

SSH_TARGET="${SSH_USER}@${HOST}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
if [ -f "$IDENTITY_FILE" ]; then
    SSH_OPTS=(-i "$IDENTITY_FILE" "${SSH_OPTS[@]}")
fi

RAW_DIR="${LOCAL_DIR}/raw"
API_DIR="${LOCAL_DIR}/api"
SNAPSHOT_DIR="${LOCAL_DIR}/live_snapshots"
LOG_DIR="${LOCAL_DIR}/logs"
RUNTIME_DIR="${LOCAL_DIR}/runtime"
DOCKER_DIR="${LOCAL_DIR}/docker"
CONFIG_DIR="${LOCAL_DIR}/config"
REVIEW_DIR="${LOCAL_DIR}/reviews"

mkdir -p "$RAW_DIR" "$API_DIR" "$SNAPSHOT_DIR" "$LOG_DIR" "$RUNTIME_DIR" "$DOCKER_DIR" "$CONFIG_DIR" "$REVIEW_DIR"

timestamp() {
    date -u +"%Y%m%dT%H%M%SZ"
}

ssh_remote() {
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"
}

rsync_remote() {
    rsync -azP -e "ssh ${SSH_OPTS[*]}" "$@"
}

remote_quote() {
    printf "%q" "$1"
}

remote_api_get_command() {
    local url="$1"
    printf '{ set -a; [ -f .env.trident ] && . ./.env.trident; set +a; auth_args=(); if [ -n "${TRIDENT_UI_AUTH_USERNAME:-}" ] && [ -n "${TRIDENT_UI_AUTH_PASSWORD:-}" ]; then auth_args=(-u "${TRIDENT_UI_AUTH_USERNAME}:${TRIDENT_UI_AUTH_PASSWORD}"); fi; curl -fsS "${auth_args[@]}" %q; }' "$url"
}

capture_remote() {
    local local_path="$1"
    local command="$2"
    if [ -n "$DRY_RUN" ]; then
        printf '  [dry-run] %s <- %s\n' "$local_path" "$command"
        return 0
    fi
    if ssh_remote "bash -lc $(remote_quote "cd '${REMOTE_DIR}' && ${command}")" > "$local_path" 2>/dev/null; then
        return 0
    fi
    warn "Capture distante échouée: ${local_path}"
    : > "$local_path"
}

fetch_remote_file() {
    local remote_path="$1"
    local local_path="$2"
    local label="$3"
    if [ -n "$DRY_RUN" ]; then
        printf '  [dry-run] %s:%s -> %s\n' "$SSH_TARGET" "$remote_path" "$local_path"
        return 0
    fi
    if rsync_remote "${SSH_TARGET}:${REMOTE_DIR}/${remote_path}" "$local_path" >/dev/null 2>&1; then
        return 0
    fi
    warn "Impossible de récupérer ${label} (${remote_path})"
    : > "$local_path"
}

fetch_optional_remote_file() {
    local remote_path="$1"
    local local_path="$2"
    local label="$3"
    if [ -n "$DRY_RUN" ]; then
        printf '  [dry-run] optional %s:%s -> %s\n' "$SSH_TARGET" "$remote_path" "$local_path"
        return 0
    fi
    rsync_remote "${SSH_TARGET}:${REMOTE_DIR}/${remote_path}" "$local_path" >/dev/null 2>&1 || {
        warn "Fichier optionnel absent: ${label} (${remote_path})"
        : > "$local_path"
    }
}

active_snapshot_dir() {
    local fallback="data/live_snapshots"
    if [ -n "$DRY_RUN" ]; then
        printf '%s\n' "$fallback"
        return
    fi
    ssh_remote "bash -lc $(remote_quote "cd '${REMOTE_DIR}' && $(remote_api_get_command "http://127.0.0.1:3000/api/state") 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); raw=str((d.get(\"exchange\",{}) or {}).get(\"snapshot_output_dir\", \"data/live_snapshots\")).strip(); raw=raw[2:] if raw.startswith(\"./\") else raw; print(raw if raw.startswith(\"data/\") and \"..\" not in raw.split(\"/\") else \"data/live_snapshots\")' 2>/dev/null || printf '%s\n' data/live_snapshots")"
}

fetch_api() {
    local ts
    ts="$(date -u +"%Y-%m-%d_%H%M%S")"
    info "Rapatriement API TRIDENT A/C..."
    capture_remote "${API_DIR}/health-${ts}.json" "$(remote_api_get_command "http://127.0.0.1:3000/health")"
    capture_remote "${API_DIR}/state-${ts}.json" "$(remote_api_get_command "http://127.0.0.1:3000/api/state")"
    capture_remote "${API_DIR}/metrics-${ts}.json" "$(remote_api_get_command "http://127.0.0.1:3000/api/metrics")"
    capture_remote "${API_DIR}/report-${ts}.json" "$(remote_api_get_command "http://127.0.0.1:3000/api/report")"
    ok "API TRIDENT sauvegardée dans ${API_DIR}/"
}

fetch_snapshots() {
    local remote_snapshot_dir
    remote_snapshot_dir="$(active_snapshot_dir | tail -n 1)"
    info "Rapatriement snapshots TRIDENT depuis ${remote_snapshot_dir}..."
    if [ -n "$DRY_RUN" ]; then
        printf '  [dry-run] snapshots %s:%s -> %s/\n' "$SSH_TARGET" "$remote_snapshot_dir" "$SNAPSHOT_DIR"
        return
    fi

    local find_expr
    if [ -n "$DATE_FILTER" ]; then
        find_expr="-name '${DATE_FILTER}.jsonl'"
    elif [ -n "$FETCH_ALL" ]; then
        find_expr="-name '*.jsonl'"
    else
        find_expr="-name '*.jsonl' -mtime -${DAYS}"
    fi

    local remote_list
    remote_list="$(ssh_remote "bash -lc $(remote_quote "cd '${REMOTE_DIR}' && find '${remote_snapshot_dir}' -maxdepth 1 -type f ${find_expr} -printf '%p\n' 2>/dev/null")")" || remote_list=""
    if [ -z "$remote_list" ]; then
        warn "Aucun snapshot trouvé"
        return
    fi
    while IFS= read -r remote_file; do
        [ -z "$remote_file" ] && continue
        rsync_remote "${SSH_TARGET}:${REMOTE_DIR}/${remote_file}" "${SNAPSHOT_DIR}/" >/dev/null 2>&1 || warn "Snapshot non récupéré: ${remote_file}"
    done <<< "$remote_list"
    ok "Snapshots TRIDENT rapatriés"
}

fetch_logs_runtime() {
    info "Rapatriement logs/runtime TRIDENT A/C..."
    fetch_remote_file "logs/pod_a_live.jsonl" "${LOG_DIR}/pod_a_live.jsonl" "Journal Pod A"
    fetch_remote_file "logs/pod_c_live.jsonl" "${LOG_DIR}/pod_c_live.jsonl" "Journal Pod C"
    fetch_optional_remote_file "logs/pod_a_live_report.json" "${LOG_DIR}/pod_a_live_report.json" "Rapport Pod A"
    fetch_optional_remote_file "logs/pod_c_live_report.json" "${LOG_DIR}/pod_c_live_report.json" "Rapport Pod C"
    fetch_remote_file "logs/pod_a_live_status.json" "${RUNTIME_DIR}/pod_a_live_status.json" "Runtime status Pod A"
    fetch_remote_file "logs/pod_c_live_status.json" "${RUNTIME_DIR}/pod_c_live_status.json" "Runtime status Pod C"
    fetch_optional_remote_file "runtime/trident/live_state_pod_a.json" "${RUNTIME_DIR}/live_state_pod_a.json" "Live state Pod A"
    fetch_optional_remote_file "runtime/trident/live_state_pod_c.json" "${RUNTIME_DIR}/live_state_pod_c.json" "Live state Pod C"
    fetch_optional_remote_file "logs/trident_deployment_profile.json" "${RUNTIME_DIR}/trident_deployment_profile.json" "Profil de déploiement TRIDENT"
    fetch_optional_remote_file "logs/funding_collector_status.json" "${RUNTIME_DIR}/funding_collector_status.json" "Runtime status Funding Collector"
    fetch_optional_remote_file "logs/tradfi_funding_collector_status.json" "${RUNTIME_DIR}/tradfi_funding_collector_status.json" "Runtime status Tradfi Funding Collector"
    fetch_optional_remote_file "config/trident.toml" "${CONFIG_DIR}/trident.toml" "Config TRIDENT"
    fetch_optional_remote_file "config/trident_testnet.toml" "${CONFIG_DIR}/trident_testnet.toml" "Config TRIDENT testnet"

    capture_remote "${DOCKER_DIR}/trident-api.log" "docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} trident-api 2>&1"
    capture_remote "${DOCKER_DIR}/pod-a-live.log" "docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} pod-a-live 2>&1"
    capture_remote "${DOCKER_DIR}/pod-c-live.log" "docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} pod-c-live 2>&1"
    capture_remote "${DOCKER_DIR}/tradfi-funding-collector.log" "docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} tradfi-funding-collector 2>&1"
    capture_remote "${DOCKER_DIR}/funding-collector.log" "docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} funding-collector 2>&1"
    ok "Logs/runtime TRIDENT A/C rapatriés"
}

latest_file() {
    find "$1" -maxdepth 1 -type f -name "$2" 2>/dev/null | sort | tail -n 1
}

write_review() {
    local ts output raw_dir
    ts="$(timestamp)"
    output="${REVIEW_DIR}/${ts}"
    raw_dir="${output}/raw"
    mkdir -p "$raw_dir"

    local latest_health latest_state latest_report latest_metrics
    latest_health="$(latest_file "$API_DIR" 'health-*.json')"
    latest_state="$(latest_file "$API_DIR" 'state-*.json')"
    latest_report="$(latest_file "$API_DIR" 'report-*.json')"
    latest_metrics="$(latest_file "$API_DIR" 'metrics-*.json')"
    [ -n "$latest_health" ] && cp "$latest_health" "${raw_dir}/health.json" || : > "${raw_dir}/health.json"
    [ -n "$latest_state" ] && cp "$latest_state" "${raw_dir}/state.json" || : > "${raw_dir}/state.json"
    [ -n "$latest_report" ] && cp "$latest_report" "${raw_dir}/report.json" || : > "${raw_dir}/report.json"
    [ -n "$latest_metrics" ] && cp "$latest_metrics" "${raw_dir}/metrics.json" || : > "${raw_dir}/metrics.json"

    python3 - "$output" "$LOG_DIR" "$RUNTIME_DIR" "$DOCKER_DIR" "$CONFIG_DIR" "$SNAPSHOT_DIR" <<'PY'
from __future__ import annotations

import json
import sys
import ast
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

output = Path(sys.argv[1])
log_dir = Path(sys.argv[2])
runtime_dir = Path(sys.argv[3])
docker_dir = Path(sys.argv[4])
config_dir = Path(sys.argv[5])
snapshot_dir = Path(sys.argv[6])
raw = output / "raw"

def load_json(path: Path) -> dict:
    try:
        text = path.read_text().strip()
        return json.loads(text) if text else {}
    except Exception:
        return {}

def load_toml(path: Path) -> dict:
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return {}
    data: dict[str, object] = {}
    section: list[str] = []
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = [part.strip() for part in line.strip("[]").split(".") if part.strip()]
            target = data
            for part in section:
                target = target.setdefault(part, {})  # type: ignore[assignment]
            continue
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        target = data
        for part in section:
            target = target.setdefault(part, {})  # type: ignore[assignment]
        target[key.strip()] = parse_toml_value(raw_value.strip())  # type: ignore[index]
    return data

def parse_toml_value(value: str) -> object:
    if value in {"true", "false"}:
        return value == "true"
    try:
        return ast.literal_eval(value)
    except Exception:
        pass
    try:
        if any(char in value for char in (".", "e", "E")):
            return float(value)
        return int(value)
    except ValueError:
        return value.strip('"')

def as_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def as_list(value: object) -> list:
    return value if isinstance(value, list) else []

def pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100.0, 2)

def iter_jsonl(path: Path):
    try:
        handle = path.open("r", encoding="utf-8")
    except Exception:
        return
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                yield payload

def iter_jsonl_tail(path: Path, max_lines: int):
    try:
        result = subprocess.run(
            ["tail", "-n", str(max(max_lines, 1)), str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return
    if result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            yield payload

def ref_source_count(payload: dict) -> int:
    try:
        return int(float(payload.get("external_reference_source_count") or 0))
    except (TypeError, ValueError):
        return 0

def ref_available(payload: dict) -> bool:
    raw_available = payload.get("external_reference_available")
    if isinstance(raw_available, bool):
        return raw_available
    return ref_source_count(payload) > 0

def empty_symbol_stats() -> dict:
    return {
        "records": 0,
        "with_reference": 0,
        "without_reference": 0,
        "latest_reference": {},
    }

def update_symbol_stats(stats: dict, symbol: str, payload: dict) -> None:
    if not symbol:
        return
    row = stats.setdefault(symbol, empty_symbol_stats())
    row["records"] += 1
    if ref_available(payload):
        row["with_reference"] += 1
        row["latest_reference"] = {
            "source_count": ref_source_count(payload),
            "sources": payload.get("external_reference_sources"),
            "reference_symbol": payload.get("external_reference_symbol"),
            "reference_price": payload.get("external_reference_price"),
            "reference_time": payload.get("external_reference_time"),
            "age_seconds": payload.get("external_reference_age_seconds"),
            "premium_bps": payload.get("external_premium_bps"),
            "max_deviation_bps": payload.get("external_reference_max_deviation_bps"),
        }
    else:
        row["without_reference"] += 1

def compact_symbol_stats(stats: dict) -> dict:
    compact: dict[str, dict] = {}
    for symbol, row in sorted(stats.items()):
        records = int(row.get("records") or 0)
        with_reference = int(row.get("with_reference") or 0)
        compact[symbol] = {
            "records": records,
            "with_reference": with_reference,
            "without_reference": int(row.get("without_reference") or 0),
            "coverage_pct": pct(with_reference, records),
            "latest_reference": row.get("latest_reference") or {},
        }
    return compact

def configured_pod_c_external_symbols(pod_c_config: dict) -> set[str]:
    external = pod_c_config.get("external_reference")
    if not isinstance(external, dict):
        return set()
    symbols = external.get("symbols")
    if not isinstance(symbols, dict):
        return set()
    return {
        str(symbol).strip().strip('"').strip("'").upper()
        for symbol in symbols
        if str(symbol).strip()
    }

P103_SHADOW_FIELDS = (
    "would_block_external_reference_abs_premium_gt_50",
    "would_block_external_reference_abs_premium_gt_100",
    "would_block_external_reference_counter_momentum_5m_6bps",
    "would_block_external_reference_candidate_loose_5m",
    "would_block_external_reference_candidate_default_5m",
)

P108_SYMBOL_GUARD_FIELDS = (
    "would_throttle_dynamic_symbol_guard",
    "would_block_dynamic_symbol_guard",
    "would_reduce_cap_dynamic_symbol_guard",
    "structural_block_candidate",
)

def empty_shadow_stats() -> dict:
    return {
        "records": 0,
        "with_shadow": 0,
        "live_action_unchanged_false": 0,
        "by_gate": {field: 0 for field in P103_SHADOW_FIELDS},
        "by_symbol": {},
    }

def update_shadow_stats(stats: dict, symbol: str, details: dict) -> None:
    if details.get("external_reference_shadow_mode") != "observation_only":
        return
    stats["with_shadow"] += 1
    if details.get("external_reference_shadow_live_action_unchanged") is not True:
        stats["live_action_unchanged_false"] += 1
    symbol_key = str(symbol or "").upper()
    if symbol_key:
        stats["by_symbol"][symbol_key] = int(stats["by_symbol"].get(symbol_key) or 0) + 1
    for field in P103_SHADOW_FIELDS:
        if details.get(field) is True:
            stats["by_gate"][field] = int(stats["by_gate"].get(field) or 0) + 1

def empty_symbol_guard_stats() -> dict:
    return {
        "records": 0,
        "with_shadow": 0,
        "live_action_unchanged_false": 0,
        "by_state": {},
        "by_gate": {field: 0 for field in P108_SYMBOL_GUARD_FIELDS},
        "by_symbol": {},
        "avg_score": 0.0,
        "_score_sum": 0.0,
    }

def update_symbol_guard_stats(stats: dict, symbol: str, details: dict) -> None:
    stats["records"] += 1
    if details.get("symbol_guard_shadow_mode") != "observation_only":
        return
    stats["with_shadow"] += 1
    if details.get("symbol_guard_live_action_unchanged") is not True:
        stats["live_action_unchanged_false"] += 1
    symbol_key = str(symbol or "").upper()
    state = str(details.get("symbol_guard_state") or "unknown")
    stats["by_state"][state] = int(stats["by_state"].get(state) or 0) + 1
    if symbol_key:
        stats["by_symbol"][symbol_key] = int(stats["by_symbol"].get(symbol_key) or 0) + 1
    for field in P108_SYMBOL_GUARD_FIELDS:
        if details.get(field) is True:
            stats["by_gate"][field] = int(stats["by_gate"].get(field) or 0) + 1
    try:
        stats["_score_sum"] += float(details.get("falling_knife_score") or 0.0)
    except (TypeError, ValueError):
        pass
    stats["avg_score"] = round(
        float(stats["_score_sum"]) / max(int(stats["with_shadow"]), 1),
        4,
    )

def combine_shadow_details(*sources: object) -> dict:
    combined: dict = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key not in combined or combined[key] in (None, ""):
                combined[key] = value
    return combined

def finalize_symbol_guard_stats(stats: dict) -> dict:
    compact = dict(stats)
    compact.pop("_score_sum", None)
    compact["by_symbol"] = dict(
        sorted(compact.get("by_symbol", {}).items(), key=lambda item: (-int(item[1]), item[0]))[:20]
    )
    return compact

def build_p108_symbol_guard_focus(*, log_dir: Path) -> dict:
    stats = empty_symbol_guard_stats()
    pod_a_log = log_dir / "pod_a_live.jsonl"
    try:
        journal_tail_lines = max(int(os.getenv("TRIDENT_FETCH_P108_JOURNAL_TAIL_LINES", "2000")), 1)
    except ValueError:
        journal_tail_lines = 2000
    for record in iter_jsonl_tail(pod_a_log, journal_tail_lines) or []:
        event_type = str(record.get("event_type") or "")
        if event_type == "signal":
            signal = record.get("signal") if isinstance(record.get("signal"), dict) else {}
            details = combine_shadow_details(
                signal.get("setup_details"),
                signal.get("dynamic_symbol_guard"),
                signal.get("symbol_guard_shadow"),
            )
            update_symbol_guard_stats(stats, str(signal.get("symbol") or ""), details)
        elif event_type == "signal_review":
            review = record.get("review") if isinstance(record.get("review"), dict) else {}
            details = combine_shadow_details(
                review.get("setup_details"),
                review.get("dynamic_symbol_guard"),
                review.get("symbol_guard_shadow"),
            )
            update_symbol_guard_stats(stats, str(review.get("symbol") or ""), details)
        elif event_type == "trade_close":
            trade = record.get("trade") if isinstance(record.get("trade"), dict) else {}
            details = combine_shadow_details(
                trade.get("setup_details"),
                trade.get("dynamic_symbol_guard"),
                trade.get("symbol_guard_shadow"),
            )
            update_symbol_guard_stats(stats, str(trade.get("symbol") or ""), details)
    status = "WARN"
    reasons: list[str] = []
    if int(stats.get("with_shadow") or 0) > 0:
        status = "PASS"
        reasons.append("shadow P1-08 présent dans les journaux Pod A")
    else:
        reasons.append("aucun champ dynamic_symbol_guard/symbol_guard_* observé dans le tail Pod A")
    if int(stats.get("live_action_unchanged_false") or 0) > 0:
        status = "FAIL"
        reasons.append("shadow P1-08 indique live_action_unchanged=false")
    return {
        "status": status,
        "reasons": reasons,
        "journal_tail_lines": journal_tail_lines,
        "dynamic_symbol_guard": finalize_symbol_guard_stats(stats),
    }

def build_p103_external_reference_focus(
    *,
    runtime_statuses: dict[str, dict],
    pod_c_config: dict,
    log_dir: Path,
    snapshot_dir: Path,
) -> dict:
    configured_symbols = configured_pod_c_external_symbols(pod_c_config)
    runtime_external = runtime_statuses.get("pod_c", {}).get("external_reference")
    if not isinstance(runtime_external, dict):
        runtime_external = {}
    blocked_symbols = {
        str(symbol).upper() for symbol in as_list(pod_c_config.get("blocked_symbols"))
    }

    snapshot_stats: dict[str, dict] = {}
    snapshot_records = 0
    snapshot_with_reference = 0
    try:
        snapshot_file_limit = max(int(os.getenv("TRIDENT_FETCH_P103_SNAPSHOT_FILES", "5")), 1)
    except ValueError:
        snapshot_file_limit = 5
    try:
        snapshot_tail_lines = max(int(os.getenv("TRIDENT_FETCH_P103_SNAPSHOT_TAIL_LINES", "1000")), 1)
    except ValueError:
        snapshot_tail_lines = 1000
    snapshot_files = sorted(
        snapshot_dir.glob("*.jsonl"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
    )[-snapshot_file_limit:]
    for path in snapshot_files:
        for record in iter_jsonl_tail(path, snapshot_tail_lines) or []:
            symbols = record.get("symbols")
            if not isinstance(symbols, list):
                continue
            for item in symbols:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "").upper()
                if configured_symbols and symbol not in configured_symbols:
                    continue
                if not configured_symbols and not symbol.startswith("XYZ:"):
                    continue
                snapshot_records += 1
                if ref_available(item):
                    snapshot_with_reference += 1
                update_symbol_stats(snapshot_stats, symbol, item)

    setup_detail_stats: dict[str, dict] = {}
    symbol_snapshot_stats: dict[str, dict] = {}
    journal_setup_records = 0
    journal_setup_with_reference = 0
    journal_snapshot_records = 0
    journal_snapshot_with_reference = 0
    silver_symbol_blocked_reviews = 0
    shadow_stats = empty_shadow_stats()
    pod_c_log = log_dir / "pod_c_live.jsonl"
    try:
        journal_tail_lines = max(int(os.getenv("TRIDENT_FETCH_P103_JOURNAL_TAIL_LINES", "1000")), 1)
    except ValueError:
        journal_tail_lines = 1000
    for record in iter_jsonl_tail(pod_c_log, journal_tail_lines) or []:
        event_type = str(record.get("event_type") or "")
        candidates: list[tuple[str, dict]] = []
        if event_type == "signal":
            signal = record.get("signal") if isinstance(record.get("signal"), dict) else {}
            details = signal.get("setup_details") if isinstance(signal.get("setup_details"), dict) else {}
            candidates.append((str(signal.get("symbol") or ""), details))
        elif event_type == "signal_review":
            review = record.get("review") if isinstance(record.get("review"), dict) else {}
            details = review.get("setup_details") if isinstance(review.get("setup_details"), dict) else {}
            symbol = str(review.get("symbol") or "")
            candidates.append((symbol, details))
            if symbol.upper() == "XYZ:SILVER" and "symbol_blocked" in str(review.get("reason_summary") or ""):
                silver_symbol_blocked_reviews += 1
        elif event_type == "trade_close":
            trade = record.get("trade") if isinstance(record.get("trade"), dict) else {}
            details = trade.get("setup_details") if isinstance(trade.get("setup_details"), dict) else {}
            candidates.append((str(trade.get("symbol") or ""), details))
        for symbol, details in candidates:
            if configured_symbols and symbol.upper() not in configured_symbols:
                continue
            if not details:
                continue
            journal_setup_records += 1
            shadow_stats["records"] += 1
            if ref_available(details):
                journal_setup_with_reference += 1
            update_symbol_stats(setup_detail_stats, symbol.upper(), details)
            update_shadow_stats(shadow_stats, symbol.upper(), details)

        snapshot = record.get("symbol_snapshot")
        if isinstance(snapshot, dict):
            symbol = str(snapshot.get("symbol") or record.get("symbol") or "").upper()
            if configured_symbols and symbol not in configured_symbols:
                continue
            if not configured_symbols and not symbol.startswith("XYZ:"):
                continue
            journal_snapshot_records += 1
            if ref_available(snapshot):
                journal_snapshot_with_reference += 1
            update_symbol_stats(symbol_snapshot_stats, symbol, snapshot)

    configured_missing_in_snapshots = sorted(
        symbol
        for symbol in configured_symbols
        if int(snapshot_stats.get(symbol, {}).get("with_reference") or 0) == 0
    )
    p103_status = "WARN"
    p103_reasons: list[str] = []
    runtime_enabled = runtime_external.get("enabled")
    if runtime_enabled is not True:
        p103_status = "FAIL"
        p103_reasons.append(f"runtime external_reference.enabled={runtime_enabled!r}")
    elif snapshot_with_reference > 0 or journal_setup_with_reference > 0 or int(runtime_external.get("symbols_enriched") or 0) > 0:
        p103_status = "PASS"
        p103_reasons.append("au moins une référence externe Pod C enrichie")
        if journal_setup_records > 0 and journal_setup_with_reference == 0:
            p103_reasons.append(
                "les derniers setup_details du journal sont encore pré-déploiement ou sans référence; suivre les prochains enregistrements"
            )
    elif snapshot_records > 0 and snapshot_with_reference == 0:
        p103_status = "FAIL"
        p103_reasons.append("snapshots Pod C récupérés mais aucune référence externe enrichie")
    elif journal_setup_records > 0 and journal_setup_with_reference == 0:
        p103_status = "FAIL"
        p103_reasons.append("journaux Pod C avec setup_details mais aucune référence externe disponible")
    else:
        p103_reasons.append("aucun snapshot/journal Pod C exploitable encore disponible après fetch")

    silver_blocked_by_config = "XYZ:SILVER" in blocked_symbols
    if not silver_blocked_by_config:
        p103_status = "FAIL"
        p103_reasons.append("XYZ:SILVER absent de pod_c.blocked_symbols")
    if int(shadow_stats.get("live_action_unchanged_false") or 0) > 0:
        p103_status = "FAIL"
        p103_reasons.append("shadow P1-03 indique live_action_unchanged=false")

    return {
        "status": p103_status,
        "reasons": p103_reasons,
        "runtime_external_reference": runtime_external,
        "configured_symbols": sorted(configured_symbols),
        "snapshot_files_scanned": [str(path) for path in snapshot_files],
        "snapshot_symbol_records": snapshot_records,
        "snapshot_with_reference": snapshot_with_reference,
        "snapshot_reference_coverage_pct": pct(snapshot_with_reference, snapshot_records),
        "snapshot_by_symbol": compact_symbol_stats(snapshot_stats),
        "journal_setup_records": journal_setup_records,
        "journal_setup_with_reference": journal_setup_with_reference,
        "journal_setup_reference_coverage_pct": pct(journal_setup_with_reference, journal_setup_records),
        "journal_setup_by_symbol": compact_symbol_stats(setup_detail_stats),
        "journal_symbol_snapshot_records": journal_snapshot_records,
        "journal_symbol_snapshot_with_reference": journal_snapshot_with_reference,
        "journal_symbol_snapshot_reference_coverage_pct": pct(
            journal_snapshot_with_reference,
            journal_snapshot_records,
        ),
        "journal_symbol_snapshot_by_symbol": compact_symbol_stats(symbol_snapshot_stats),
        "silver_blocked_by_config": silver_blocked_by_config,
        "silver_symbol_blocked_reviews": silver_symbol_blocked_reviews,
        "external_reference_shadow": shadow_stats,
    }

health = load_json(raw / "health.json")
state = load_json(raw / "state.json")
report = load_json(raw / "report.json")
config = load_toml(config_dir / "trident.toml")

def pod_report(name: str) -> dict:
    for item in report.get("pods", []) or []:
        if isinstance(item, dict) and item.get("pod") == name:
            return item
    return {}

def runtime_status(name: str) -> dict:
    return load_json(runtime_dir / f"{name}_live_status.json")

runtime_statuses = {pod: runtime_status(pod) for pod in ("pod_a", "pod_c")}

def log_has_bad_patterns(path: Path) -> tuple[int, int]:
    try:
        text = path.read_text(errors="ignore").lower()
    except Exception:
        return 0, 0
    return text.count("traceback"), text.count("decimal is not json serializable")

def fmt_usd(value: float) -> str:
    return f"{value:.2f}"

def runtime_report(name: str) -> dict:
    status = runtime_statuses.get(name, {})
    value = status.get("report") if isinstance(status, dict) else {}
    return value if isinstance(value, dict) else {}

def sorted_pnl_items(value: object, limit: int = 5) -> list[tuple[str, float]]:
    if not isinstance(value, dict):
        return []
    items = [(str(key), as_float(raw_value)) for key, raw_value in value.items()]
    return sorted(items, key=lambda item: item[1])[:limit]

def trade_pnl(trade: dict) -> float:
    if "pnl_usd" in trade:
        return as_float(trade.get("pnl_usd"))
    gross = as_float(trade.get("gross_pnl_usd"))
    fees = as_float(trade.get("fees_usd"))
    return gross - fees

def stop_loss_focus(name: str) -> dict:
    trades = as_list(runtime_report(name).get("closed_trade_log"))
    rows: list[dict[str, object]] = []
    actual = 0.0
    planned = 0.0
    for item in trades:
        if not isinstance(item, dict):
            continue
        if item.get("close_reason") != "exchange_closed_stop_loss":
            continue
        pnl = trade_pnl(item)
        notional = as_float(item.get("target_notional_usd"))
        stop_bps = as_float(item.get("stop_bps"))
        fees = as_float(item.get("fees_usd"))
        planned_loss = -(notional * stop_bps / 10000.0 + fees) if stop_bps > 0.0 else 0.0
        excess = pnl - planned_loss
        actual += pnl
        planned += planned_loss
        rows.append(
            {
                "symbol": item.get("symbol"),
                "opened_at": item.get("opened_at"),
                "closed_at": item.get("closed_at"),
                "pnl_usd": round(pnl, 4),
                "planned_stop_loss_usd": round(planned_loss, 4),
                "excess_vs_planned_usd": round(excess, 4),
                "stop_bps": item.get("stop_bps"),
                "target_notional_usd": item.get("target_notional_usd"),
            }
        )
    rows.sort(key=lambda row: as_float(row.get("excess_vs_planned_usd")))
    return {
        "count": len(rows),
        "actual_pnl_usd": round(actual, 4),
        "planned_stop_loss_usd": round(planned, 4),
        "excess_vs_planned_usd": round(actual - planned, 4),
        "worst": rows[:5],
    }

trident_config = config.get("trident", {}) if isinstance(config.get("trident"), dict) else {}
execution_config = (
    trident_config.get("execution", {}) if isinstance(trident_config.get("execution"), dict) else {}
)
pod_a_config = config.get("pod_a", {}) if isinstance(config.get("pod_a"), dict) else {}
pod_c_config = config.get("pod_c", {}) if isinstance(config.get("pod_c"), dict) else {}
pod_c_cluster_modes = (
    pod_c_config.get("cluster_modes", {}) if isinstance(pod_c_config.get("cluster_modes"), dict) else {}
)
silver_mode = (
    pod_c_cluster_modes.get("silver", {})
    if isinstance(pod_c_cluster_modes.get("silver"), dict)
    else {}
)

operator_context = {
    "live_max_order_notional_usd": execution_config.get("live_max_order_notional_usd"),
    "live_block_stop_grace_setups": execution_config.get("live_block_stop_grace_setups"),
    "live_stop_grace_catastrophic_sl_bps": execution_config.get("live_stop_grace_catastrophic_sl_bps"),
    "pod_a_stop_grace_minutes": pod_a_config.get("stop_grace_minutes"),
    "pod_c_blocked_symbols": pod_c_config.get("blocked_symbols", []),
    "pod_c_silver_mode": {
        "enabled": silver_mode.get("enabled"),
        "break_even_multiplier": silver_mode.get("break_even_multiplier"),
        "trailing_activation_multiplier": silver_mode.get("trailing_activation_multiplier"),
        "trailing_distance_multiplier": silver_mode.get("trailing_distance_multiplier"),
    },
}

performance_focus = {
    "pod_a": {
        "runtime_realized_pnl_usd": runtime_report("pod_a").get("realized_pnl_usd"),
        "runtime_pnl_by_symbol_worst": sorted_pnl_items(
            runtime_report("pod_a").get("pnl_by_symbol")
        ),
        "stop_loss_focus": stop_loss_focus("pod_a"),
    },
    "pod_c": {
        "runtime_realized_pnl_usd": runtime_report("pod_c").get("realized_pnl_usd"),
        "runtime_pnl_by_symbol_worst": sorted_pnl_items(
            runtime_report("pod_c").get("pnl_by_symbol")
        ),
        "stop_loss_focus": stop_loss_focus("pod_c"),
    },
}

external_reference_focus = build_p103_external_reference_focus(
    runtime_statuses=runtime_statuses,
    pod_c_config=pod_c_config,
    log_dir=log_dir,
    snapshot_dir=snapshot_dir,
)
symbol_guard_focus = build_p108_symbol_guard_focus(log_dir=log_dir)

checks: list[str] = []
warnings: list[str] = []
failures: list[str] = []

if health.get("status") == "ok":
    checks.append("API /health répond ok")
else:
    failures.append(f"API /health inattendu: {health.get('status')!r}")

for pod in ("pod_a", "pod_c"):
    status = runtime_statuses.get(pod, {})
    rep = pod_report(pod)
    label = "Pod A" if pod == "pod_a" else "Pod C"
    if rep.get("healthy") is True:
        checks.append(f"{label} healthy dans /api/report")
    else:
        failures.append(f"{label} non healthy dans /api/report")
    if status.get("live_trading_paused") is False:
        checks.append(f"{label} live_trading_paused=false")
    else:
        warnings.append(f"{label} live_trading_paused={status.get('live_trading_paused')!r}")
    reconciliation = status.get("live_reconciliation") if isinstance(status, dict) else {}
    if isinstance(reconciliation, dict) and reconciliation.get("ready") is True and not reconciliation.get("reasons"):
        checks.append(f"{label} reconciliation ready")
    else:
        warnings.append(f"{label} reconciliation à revoir: {reconciliation}")
    for key in ("unknown_exchange_positions", "missing_exchange_positions", "side_mismatches", "open_orders", "trigger_orders"):
        value = reconciliation.get(key) if isinstance(reconciliation, dict) else None
        if value:
            failures.append(f"{label} {key} non vide: {value}")

for name in ("pod_a_live", "pod_c_live", "trident-api"):
    tracebacks, decimal_errors = log_has_bad_patterns(docker_dir / f"{name}.log")
    if tracebacks:
        failures.append(f"Traceback récent dans {name}.log")
    if decimal_errors:
        failures.append(f"Erreur Decimal JSON récente dans {name}.log")

if as_float(operator_context["live_max_order_notional_usd"], 999999.0) > 200.0:
    warnings.append(
        "Cap live A/C superieur a 200 apres l'ajustement Pod A du 2026-06-09; verifier le deploiement"
    )
if operator_context["live_block_stop_grace_setups"] is not False:
    warnings.append(
        "Pod A live_block_stop_grace_setups n'est pas false; cela bloque les setups grace"
    )
blocked_symbols = {
    str(symbol).upper() for symbol in as_list(operator_context["pod_c_blocked_symbols"])
}
if "XYZ:SILVER" not in blocked_symbols:
    warnings.append("Pod C XYZ:SILVER n'est pas bloque; verifier le contexte de review")

p103_status = str(external_reference_focus.get("status") or "WARN")
if p103_status == "PASS":
    checks.append(
        "P1-03 external_reference Pod C enrichi "
        f"(snapshots={external_reference_focus.get('snapshot_with_reference')}/"
        f"{external_reference_focus.get('snapshot_symbol_records')}, "
        f"journaux={external_reference_focus.get('journal_setup_with_reference')}/"
        f"{external_reference_focus.get('journal_setup_records')})"
    )
elif p103_status == "FAIL":
    failures.append(
        "P1-03 external_reference Pod C KO: "
        + "; ".join(str(item) for item in external_reference_focus.get("reasons", []))
    )
else:
    warnings.append(
        "P1-03 external_reference Pod C à confirmer: "
        + "; ".join(str(item) for item in external_reference_focus.get("reasons", []))
    )

p108_status = str(symbol_guard_focus.get("status") or "WARN")
if p108_status == "PASS":
    guard = symbol_guard_focus.get("dynamic_symbol_guard", {})
    checks.append(
        "P1-08 dynamic symbol guard Pod A shadow collecté "
        f"({guard.get('with_shadow')}/{guard.get('records')}, "
        f"states={guard.get('by_state')})"
    )
elif p108_status == "FAIL":
    failures.append(
        "P1-08 dynamic symbol guard Pod A KO: "
        + "; ".join(str(item) for item in symbol_guard_focus.get("reasons", []))
    )
else:
    warnings.append(
        "P1-08 dynamic symbol guard Pod A à confirmer: "
        + "; ".join(str(item) for item in symbol_guard_focus.get("reasons", []))
    )

status = "PASS" if not failures else "FAIL"
if warnings and status == "PASS":
    status = "WARN"

p103_lines = [
    "# P1-03 Pod C external reference audit",
    "",
    f"- generated_at: `{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}`",
    f"- status: `{external_reference_focus.get('status')}`",
]
for reason in external_reference_focus.get("reasons", []):
    p103_lines.append(f"- reason: `{reason}`")
p103_lines.extend(
    [
        "",
        "## Runtime",
        f"- enabled: `{external_reference_focus.get('runtime_external_reference', {}).get('enabled')}`",
        f"- records_seen: `{external_reference_focus.get('runtime_external_reference', {}).get('records_seen')}`",
        f"- symbols_seen: `{external_reference_focus.get('runtime_external_reference', {}).get('symbols_seen')}`",
        f"- symbols_enriched: `{external_reference_focus.get('runtime_external_reference', {}).get('symbols_enriched')}`",
        f"- symbols_missing_reference: `{external_reference_focus.get('runtime_external_reference', {}).get('symbols_missing_reference')}`",
        f"- fetch_error_count: `{external_reference_focus.get('runtime_external_reference', {}).get('fetch_error_count')}`",
        "",
        "## Snapshots",
        f"- scanned_files: `{len(external_reference_focus.get('snapshot_files_scanned', []))}`",
        f"- symbol_records: `{external_reference_focus.get('snapshot_symbol_records')}`",
        f"- with_reference: `{external_reference_focus.get('snapshot_with_reference')}`",
        f"- coverage_pct: `{external_reference_focus.get('snapshot_reference_coverage_pct')}`",
        "",
        "## Journal Pod C",
        f"- setup_records: `{external_reference_focus.get('journal_setup_records')}`",
        f"- setup_with_reference: `{external_reference_focus.get('journal_setup_with_reference')}`",
        f"- setup_coverage_pct: `{external_reference_focus.get('journal_setup_reference_coverage_pct')}`",
        f"- symbol_snapshot_records: `{external_reference_focus.get('journal_symbol_snapshot_records')}`",
        f"- symbol_snapshot_with_reference: `{external_reference_focus.get('journal_symbol_snapshot_with_reference')}`",
        "",
        "## Shadow P1-03",
        f"- records: `{external_reference_focus.get('external_reference_shadow', {}).get('records')}`",
        f"- with_shadow: `{external_reference_focus.get('external_reference_shadow', {}).get('with_shadow')}`",
        f"- live_action_unchanged_false: `{external_reference_focus.get('external_reference_shadow', {}).get('live_action_unchanged_false')}`",
        f"- by_gate: `{external_reference_focus.get('external_reference_shadow', {}).get('by_gate')}`",
        f"- by_symbol: `{external_reference_focus.get('external_reference_shadow', {}).get('by_symbol')}`",
        "",
        "## Silver",
        f"- silver_blocked_by_config: `{external_reference_focus.get('silver_blocked_by_config')}`",
        f"- silver_symbol_blocked_reviews: `{external_reference_focus.get('silver_symbol_blocked_reviews')}`",
        "",
        "## By Symbol",
    ]
)
for symbol, row in (external_reference_focus.get("snapshot_by_symbol") or {}).items():
    latest = row.get("latest_reference") or {}
    p103_lines.append(
        "- "
        f"{symbol}: snapshots `{row.get('with_reference')}/{row.get('records')}` "
        f"coverage `{row.get('coverage_pct')}%`, latest `{latest}`"
    )
(output / "p103_external_reference_audit.md").write_text(
    "\n".join(p103_lines) + "\n",
    encoding="utf-8",
)
(output / "p103_external_reference_audit.json").write_text(
    json.dumps(external_reference_focus, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)

p108_lines = [
    "# P1-08 Pod A dynamic symbol guard audit",
    "",
    f"- generated_at: `{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}`",
    f"- status: `{symbol_guard_focus.get('status')}`",
]
for reason in symbol_guard_focus.get("reasons", []):
    p108_lines.append(f"- reason: `{reason}`")
guard = symbol_guard_focus.get("dynamic_symbol_guard", {})
p108_lines.extend(
    [
        "",
        "## Shadow P1-08",
        f"- records: `{guard.get('records')}`",
        f"- with_shadow: `{guard.get('with_shadow')}`",
        f"- live_action_unchanged_false: `{guard.get('live_action_unchanged_false')}`",
        f"- avg_score: `{guard.get('avg_score')}`",
        f"- by_state: `{guard.get('by_state')}`",
        f"- by_gate: `{guard.get('by_gate')}`",
        f"- by_symbol: `{guard.get('by_symbol')}`",
    ]
)
(output / "p108_dynamic_symbol_guard_audit.md").write_text(
    "\n".join(p108_lines) + "\n",
    encoding="utf-8",
)
(output / "p108_dynamic_symbol_guard_audit.json").write_text(
    json.dumps(symbol_guard_focus, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)

lines = [
    "# TRIDENT A/C server review",
    "",
    f"- generated_at: `{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}`",
    f"- status: `{status}`",
    f"- mode: `{health.get('mode', report.get('mode', 'unknown'))}`",
    f"- exchange_network: `{health.get('exchange_network', (state.get('exchange') or {}).get('network', 'unknown'))}`",
    f"- ownership_conflict_count: `{report.get('ownership_conflict_count', len(state.get('ownership_conflicts', []) or []))}`",
    "",
    "## Operator Context",
    f"- live_max_order_notional_usd: `{operator_context['live_max_order_notional_usd']}`",
    f"- live_block_stop_grace_setups: `{operator_context['live_block_stop_grace_setups']}`",
    f"- live_stop_grace_catastrophic_sl_bps: `{operator_context['live_stop_grace_catastrophic_sl_bps']}`",
    f"- pod_a_stop_grace_minutes: `{operator_context['pod_a_stop_grace_minutes']}`",
    f"- pod_c_blocked_symbols: `{operator_context['pod_c_blocked_symbols']}`",
    f"- pod_c_silver_mode: `{operator_context['pod_c_silver_mode']}`",
    "",
    "## Performance Focus",
]
for pod, label in (("pod_a", "Pod A"), ("pod_c", "Pod C")):
    focus = performance_focus[pod]
    worst_symbols = ", ".join(
        f"{symbol}:{fmt_usd(pnl)}" for symbol, pnl in focus["runtime_pnl_by_symbol_worst"]
    ) or "n/a"
    stop_focus = focus["stop_loss_focus"]
    lines.extend(
        [
            f"- {label} runtime_realized_pnl_usd: `{focus['runtime_realized_pnl_usd']}`",
            f"- {label} worst_symbols_runtime: `{worst_symbols}`",
            (
                f"- {label} stop_loss_actual_vs_planned: "
                f"`count={stop_focus['count']}, actual={fmt_usd(as_float(stop_focus['actual_pnl_usd']))}, "
                f"planned={fmt_usd(as_float(stop_focus['planned_stop_loss_usd']))}, "
                f"excess={fmt_usd(as_float(stop_focus['excess_vs_planned_usd']))}`"
            ),
        ]
    )
    for row in stop_focus["worst"][:3]:
        lines.append(
            "- "
            f"{label} worst_stop_loss: "
            f"`{row.get('symbol')} pnl={row.get('pnl_usd')} planned={row.get('planned_stop_loss_usd')} "
            f"excess={row.get('excess_vs_planned_usd')} opened={row.get('opened_at')}`"
        )

lines.extend(
    [
        "",
        "## P1-03 External Reference Pod C",
        f"- status: `{external_reference_focus.get('status')}`",
        f"- runtime_enabled: `{external_reference_focus.get('runtime_external_reference', {}).get('enabled')}`",
        f"- runtime_symbols_enriched: `{external_reference_focus.get('runtime_external_reference', {}).get('symbols_enriched')}`",
        f"- snapshot_coverage: `{external_reference_focus.get('snapshot_with_reference')}/{external_reference_focus.get('snapshot_symbol_records')} ({external_reference_focus.get('snapshot_reference_coverage_pct')}%)`",
        f"- journal_setup_coverage: `{external_reference_focus.get('journal_setup_with_reference')}/{external_reference_focus.get('journal_setup_records')} ({external_reference_focus.get('journal_setup_reference_coverage_pct')}%)`",
        f"- shadow_coverage: `{external_reference_focus.get('external_reference_shadow', {}).get('with_shadow')}/{external_reference_focus.get('external_reference_shadow', {}).get('records')}`",
        f"- shadow_live_action_unchanged_false: `{external_reference_focus.get('external_reference_shadow', {}).get('live_action_unchanged_false')}`",
        f"- silver_blocked_by_config: `{external_reference_focus.get('silver_blocked_by_config')}`",
        f"- detail: `{output / 'p103_external_reference_audit.md'}`",
        "",
        "## P1-08 Dynamic Symbol Guard Pod A",
        f"- status: `{symbol_guard_focus.get('status')}`",
        f"- shadow_coverage: `{symbol_guard_focus.get('dynamic_symbol_guard', {}).get('with_shadow')}/{symbol_guard_focus.get('dynamic_symbol_guard', {}).get('records')}`",
        f"- shadow_live_action_unchanged_false: `{symbol_guard_focus.get('dynamic_symbol_guard', {}).get('live_action_unchanged_false')}`",
        f"- avg_score: `{symbol_guard_focus.get('dynamic_symbol_guard', {}).get('avg_score')}`",
        f"- by_state: `{symbol_guard_focus.get('dynamic_symbol_guard', {}).get('by_state')}`",
        f"- by_gate: `{symbol_guard_focus.get('dynamic_symbol_guard', {}).get('by_gate')}`",
        f"- detail: `{output / 'p108_dynamic_symbol_guard_audit.md'}`",
        "",
        "## Next Review Focus",
        "- Verifier que le serveur expose bien `live_max_order_notional_usd=200`, `pod_a.stop_grace_minutes=60` et `live_block_stop_grace_setups=false`.",
        "- P1-03: verifier `external_reference.symbols_enriched>0`, la couverture par symbole et les revues `XYZ:SILVER` en `symbol_blocked` dans `p103_external_reference_audit.md`.",
        "- P1-08: verifier `dynamic_symbol_guard` / `symbol_guard_*` dans `p108_dynamic_symbol_guard_audit.md`, avec `with_shadow>0` et `live_action_unchanged_false=0`.",
        "- Pod A: surveiller les nouveaux `exchange_closed_stop_loss` et `early_failure_exit`; comparer perte reelle vs stop planifie.",
        "- Pod C: verifier qu'aucun nouveau trade `XYZ:SILVER` ne s'ouvre et que les signaux silver sont rejetes `symbol_blocked` si presents.",
        "",
        "## Checks",
    ]
)
lines.extend(f"- {item}" for item in checks)
if warnings:
    lines.append("")
    lines.append("## Warnings")
    lines.extend(f"- {item}" for item in warnings)
if failures:
    lines.append("")
    lines.append("## Failures")
    lines.extend(f"- {item}" for item in failures)

for pod in ("pod_a", "pod_c"):
    rep = pod_report(pod)
    lines.append("")
    lines.append(f"## {pod}")
    for key in ("process_state", "position_count", "open_order_count", "total_fill_count", "realized_pnl_usd", "total_unrealized_pnl_usd", "win_rate"):
        lines.append(f"- {key}: `{rep.get(key)}`")

(output / "review_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
(output / "review_summary.json").write_text(
    json.dumps(
        {
            "status": status,
            "operator_context": operator_context,
            "performance_focus": performance_focus,
            "p103_external_reference_focus": external_reference_focus,
            "p108_dynamic_symbol_guard_focus": symbol_guard_focus,
            "checks": checks,
            "warnings": warnings,
            "failures": failures,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
    ok "Review TRIDENT A/C écrite: ${output}/review_summary.md"
}

echo ""
echo "========================================="
echo "  Fetch TRIDENT A/C"
echo "========================================="
echo ""

if [ -z "$REVIEW_ONLY" ]; then
    if [ -z "$SNAPSHOTS_ONLY" ]; then
        fetch_api
        fetch_logs_runtime
    fi
    if [ -z "$LOGS_ONLY" ]; then
        fetch_snapshots
    fi
fi

write_review

echo ""
ok "Fetch TRIDENT A/C terminé dans ${LOCAL_DIR}/"
