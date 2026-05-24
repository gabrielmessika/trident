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
    ssh_remote "bash -lc $(remote_quote "cd '${REMOTE_DIR}' && curl -fsS http://127.0.0.1:3000/api/state 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); raw=str((d.get(\"exchange\",{}) or {}).get(\"snapshot_output_dir\", \"data/live_snapshots\")).strip(); raw=raw[2:] if raw.startswith(\"./\") else raw; print(raw if raw.startswith(\"data/\") and \"..\" not in raw.split(\"/\") else \"data/live_snapshots\")' 2>/dev/null || printf '%s\n' data/live_snapshots")"
}

fetch_api() {
    local ts
    ts="$(date -u +"%Y-%m-%d_%H%M%S")"
    info "Rapatriement API TRIDENT A/C..."
    capture_remote "${API_DIR}/health-${ts}.json" "curl -fsS http://127.0.0.1:3000/health"
    capture_remote "${API_DIR}/state-${ts}.json" "curl -fsS http://127.0.0.1:3000/api/state"
    capture_remote "${API_DIR}/metrics-${ts}.json" "curl -fsS http://127.0.0.1:3000/api/metrics"
    capture_remote "${API_DIR}/report-${ts}.json" "curl -fsS http://127.0.0.1:3000/api/report"
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

    python3 - "$output" "$LOG_DIR" "$RUNTIME_DIR" "$DOCKER_DIR" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

output = Path(sys.argv[1])
log_dir = Path(sys.argv[2])
runtime_dir = Path(sys.argv[3])
docker_dir = Path(sys.argv[4])
raw = output / "raw"

def load_json(path: Path) -> dict:
    try:
        text = path.read_text().strip()
        return json.loads(text) if text else {}
    except Exception:
        return {}

health = load_json(raw / "health.json")
state = load_json(raw / "state.json")
report = load_json(raw / "report.json")

def pod_report(name: str) -> dict:
    for item in report.get("pods", []) or []:
        if isinstance(item, dict) and item.get("pod") == name:
            return item
    return {}

def runtime_status(name: str) -> dict:
    return load_json(runtime_dir / f"{name}_live_status.json")

def log_has_bad_patterns(path: Path) -> tuple[int, int]:
    try:
        text = path.read_text(errors="ignore").lower()
    except Exception:
        return 0, 0
    return text.count("traceback"), text.count("decimal is not json serializable")

checks: list[str] = []
warnings: list[str] = []
failures: list[str] = []

if health.get("status") == "ok":
    checks.append("API /health répond ok")
else:
    failures.append(f"API /health inattendu: {health.get('status')!r}")

for pod in ("pod_a", "pod_c"):
    status = runtime_status(pod)
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

status = "PASS" if not failures else "FAIL"
if warnings and status == "PASS":
    status = "WARN"

lines = [
    "# TRIDENT A/C server review",
    "",
    f"- generated_at: `{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}`",
    f"- status: `{status}`",
    f"- mode: `{health.get('mode', report.get('mode', 'unknown'))}`",
    f"- exchange_network: `{health.get('exchange_network', (state.get('exchange') or {}).get('network', 'unknown'))}`",
    f"- ownership_conflict_count: `{report.get('ownership_conflict_count', len(state.get('ownership_conflicts', []) or []))}`",
    "",
    "## Checks",
]
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
    json.dumps({"status": status, "checks": checks, "warnings": warnings, "failures": failures}, indent=2, sort_keys=True) + "\n",
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
