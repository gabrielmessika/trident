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
Usage: ./trident-hip4/fetch_data.sh [--logs-only] [--review-only] [--skip-review] [--dry-run] [--remote-dir /opt/trident-hip4] [--local-dir server-data/hip4] [--api-port 3001]

Rapatrie les données TRIDENT-HIP4 depuis le serveur:
- /health, /api/hip4-outcome, /api/hip4-outcome-mainnet
- logs HIP-4 mainnet paper / observer / testnet historiques
- runtime states HIP-4
- configs HIP-4
- logs Docker HIP-4
- review `hip4_outcome_run_review` locale si les logs sont disponibles
EOF
}

HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
SSH_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
REMOTE_DIR="${TRIDENT_HIP4_DEPLOY_DIR:-/opt/trident-hip4}"
LOCAL_DIR="server-data/hip4"
API_PORT="${HIP4_OUTCOME_API_PORT:-3001}"
LOG_LINES="${TRIDENT_FETCH_LOG_LINES:-300}"
LOGS_ONLY=""
REVIEW_ONLY=""
SKIP_REVIEW="${TRIDENT_SKIP_HIP4_REVIEW:-}"
DRY_RUN=""

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
        --api-port)
            API_PORT="$2"
            shift 2
            ;;
        --logs-only)
            LOGS_ONLY="true"
            shift
            ;;
        --review-only)
            REVIEW_ONLY="true"
            shift
            ;;
        --skip-review)
            SKIP_REVIEW="true"
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

SSH_TARGET="${SSH_USER}@${HOST}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
if [ -f "$IDENTITY_FILE" ]; then
    SSH_OPTS=(-i "$IDENTITY_FILE" "${SSH_OPTS[@]}")
fi

RAW_DIR="${LOCAL_DIR}/raw"
API_DIR="${LOCAL_DIR}/api"
LOG_DIR="${LOCAL_DIR}/logs"
RUNTIME_DIR="${LOCAL_DIR}/runtime"
DOCKER_DIR="${LOCAL_DIR}/docker"
CONFIG_DIR="${LOCAL_DIR}/config"
REVIEW_DIR="${LOCAL_DIR}/reviews"
REPLAY_REPORT_DIR="${LOCAL_DIR}/replay_reports"

mkdir -p \
    "$RAW_DIR" \
    "$API_DIR" \
    "$LOG_DIR" \
    "$RUNTIME_DIR" \
    "$DOCKER_DIR" \
    "$CONFIG_DIR" \
    "$REVIEW_DIR" \
    "$REPLAY_REPORT_DIR" \
    "${LOG_DIR}/hip4_outcome_paper" \
    "${LOG_DIR}/hip4_outcome_testnet" \
    "${LOG_DIR}/hip4_outcome_mainnet_paper" \
    "${LOG_DIR}/hip4_outcome_mainnet"

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

fetch_optional_remote_dir() {
    local remote_path="$1"
    local local_path="$2"
    local label="$3"
    if [ -n "$DRY_RUN" ]; then
        printf '  [dry-run] optional dir %s:%s -> %s/\n' "$SSH_TARGET" "$remote_path" "$local_path"
        return 0
    fi
    mkdir -p "$local_path"
    rsync_remote "${SSH_TARGET}:${REMOTE_DIR}/${remote_path}/" "${local_path}/" >/dev/null 2>&1 || {
        warn "Dossier optionnel absent: ${label} (${remote_path})"
    }
}

fetch_api() {
    local ts
    ts="$(date -u +"%Y-%m-%d_%H%M%S")"
    info "Rapatriement API TRIDENT-HIP4..."
    capture_remote "${API_DIR}/health-${ts}.json" "curl -fsS http://127.0.0.1:${API_PORT}/health"
    capture_remote "${API_DIR}/hip4-outcome-${ts}.json" "curl -fsS http://127.0.0.1:${API_PORT}/api/hip4-outcome"
    capture_remote "${API_DIR}/hip4-outcome-mainnet-${ts}.json" "curl -fsS http://127.0.0.1:${API_PORT}/api/hip4-outcome-mainnet"
    capture_remote "${API_DIR}/state-${ts}.json" "curl -fsS http://127.0.0.1:${API_PORT}/api/state"
    capture_remote "${API_DIR}/report-${ts}.json" "curl -fsS http://127.0.0.1:${API_PORT}/api/report"
    ok "API TRIDENT-HIP4 sauvegardée dans ${API_DIR}/"
}

fetch_logs_runtime() {
    info "Rapatriement logs/runtime TRIDENT-HIP4..."
    fetch_optional_remote_file "logs/hip4_outcome_status.json" "${RUNTIME_DIR}/hip4_outcome_status.json" "Runtime status HIP-4"
    fetch_optional_remote_file "logs/hip4_outcome_mainnet_status.json" "${RUNTIME_DIR}/hip4_outcome_mainnet_status.json" "Runtime status HIP-4 mainnet observer"
    fetch_optional_remote_file "logs/pod_b_live_status.json" "${RUNTIME_DIR}/pod_b_live_status.json" "Alias status HIP-4"
    fetch_optional_remote_file "logs/trident_hip4_deployment_profile.json" "${RUNTIME_DIR}/trident_hip4_deployment_profile.json" "Profil de déploiement TRIDENT-HIP4"
    fetch_optional_remote_file "runtime/hip4_outcome_state.json" "${RUNTIME_DIR}/hip4_outcome_state.json" "State HIP-4"
    fetch_optional_remote_file "runtime/hip4_outcome_paper_state.json" "${RUNTIME_DIR}/hip4_outcome_paper_state.json" "State HIP-4 paper"
    fetch_optional_remote_file "runtime/hip4_outcome_testnet_state.json" "${RUNTIME_DIR}/hip4_outcome_testnet_state.json" "State HIP-4 testnet"
    fetch_optional_remote_file "runtime/hip4_outcome_mainnet_paper_state.json" "${RUNTIME_DIR}/hip4_outcome_mainnet_paper_state.json" "State HIP-4 mainnet paper"
    fetch_optional_remote_file "runtime/hip4_outcome_mainnet_state.json" "${RUNTIME_DIR}/hip4_outcome_mainnet_state.json" "State HIP-4 mainnet observer"
    fetch_optional_remote_file "config/hip4_outcome_testnet.toml" "${CONFIG_DIR}/hip4_outcome_testnet.toml" "Config HIP-4 testnet"
    fetch_optional_remote_file "config/hip4_outcome_mainnet_paper.toml" "${CONFIG_DIR}/hip4_outcome_mainnet_paper.toml" "Config HIP-4 mainnet paper"
    fetch_optional_remote_file "config/hip4_outcome_mainnet_observer.toml" "${CONFIG_DIR}/hip4_outcome_mainnet_observer.toml" "Config HIP-4 mainnet observer"
    fetch_optional_remote_dir "logs/hip4_outcome_paper" "${LOG_DIR}/hip4_outcome_paper" "Logs HIP-4 paper"
    fetch_optional_remote_dir "logs/hip4_outcome_testnet" "${LOG_DIR}/hip4_outcome_testnet" "Logs HIP-4 testnet"
    fetch_optional_remote_dir "logs/hip4_outcome_mainnet_paper" "${LOG_DIR}/hip4_outcome_mainnet_paper" "Logs HIP-4 mainnet paper"
    fetch_optional_remote_dir "logs/hip4_outcome_mainnet" "${LOG_DIR}/hip4_outcome_mainnet" "Logs HIP-4 mainnet observer"

    capture_remote "${DOCKER_DIR}/hip4-api.log" "COMPOSE_PROJECT_NAME=trident-hip4 docker compose -f docker-compose.hip4.yml logs --tail ${LOG_LINES} hip4-api 2>&1"
    capture_remote "${DOCKER_DIR}/hip4-outcome-paper.log" "COMPOSE_PROJECT_NAME=trident-hip4 docker compose -f docker-compose.hip4.yml logs --tail ${LOG_LINES} hip4-outcome-paper 2>&1"
    capture_remote "${DOCKER_DIR}/hip4-mainnet-observer.log" "COMPOSE_PROJECT_NAME=trident-hip4 docker compose -f docker-compose.hip4.yml --profile mainnet_observer logs --tail ${LOG_LINES} hip4-mainnet-observer 2>&1"
    ok "Logs/runtime TRIDENT-HIP4 rapatriés"
}

latest_file() {
    find "$1" -maxdepth 1 -type f -name "$2" 2>/dev/null | sort | tail -n 1
}

run_review() {
    local ts output latest_json latest_md review_stdout review_stderr
    if [ -n "$SKIP_REVIEW" ]; then
        warn "Review HIP-4 skippée"
        return 0
    fi
    ts="$(timestamp)"
    output="${REVIEW_DIR}/${ts}"
    mkdir -p "${output}/raw" "$REPLAY_REPORT_DIR"

    local latest_health latest_hip4 latest_hip4_mainnet
    latest_health="$(latest_file "$API_DIR" 'health-????-??-??_??????.json')"
    latest_hip4="$(latest_file "$API_DIR" 'hip4-outcome-????-??-??_??????.json')"
    latest_hip4_mainnet="$(latest_file "$API_DIR" 'hip4-outcome-mainnet-????-??-??_??????.json')"
    [ -n "$latest_health" ] && cp "$latest_health" "${output}/raw/health.json" || : > "${output}/raw/health.json"
    [ -n "$latest_hip4" ] && cp "$latest_hip4" "${output}/raw/hip4_outcome.json" || : > "${output}/raw/hip4_outcome.json"
    [ -n "$latest_hip4_mainnet" ] && cp "$latest_hip4_mainnet" "${output}/raw/hip4_outcome_mainnet.json" || : > "${output}/raw/hip4_outcome_mainnet.json"

    latest_json="${REPLAY_REPORT_DIR}/hip4_outcome_run_review_latest.json"
    latest_md="${REPLAY_REPORT_DIR}/hip4_outcome_run_review_latest.md"
    review_stdout="${output}/raw/hip4_outcome_run_review_stdout.json"
    review_stderr="${output}/raw/hip4_outcome_run_review_stderr.txt"

    info "Generation de la review HIP-4..."
    local review_cmd=()
    if command -v uv >/dev/null 2>&1; then
        review_cmd=(uv run python -m app.backtest.hip4_outcome_run_review)
    else
        review_cmd=(python3 -m app.backtest.hip4_outcome_run_review)
    fi
    if ! "${review_cmd[@]}" \
        --logs-dir "paper=${LOG_DIR}/hip4_outcome_paper" \
        --logs-dir "mainnet_paper=${LOG_DIR}/hip4_outcome_mainnet_paper" \
        --logs-dir "testnet=${LOG_DIR}/hip4_outcome_testnet" \
        --logs-dir "mainnet=${LOG_DIR}/hip4_outcome_mainnet" \
        --output-json "${output}/hip4_outcome_run_review.json" \
        --output-md "${output}/hip4_outcome_run_review.md" \
        >"$review_stdout" \
        2>"$review_stderr"; then
        warn "Review HIP-4 échouée; sortie brute conservée:"
        warn "  stdout: ${review_stdout}"
        warn "  stderr: ${review_stderr}"
        return 1
    fi
    cp "${output}/hip4_outcome_run_review.json" "$latest_json"
    cp "${output}/hip4_outcome_run_review.md" "$latest_md"
    ok "Review HIP-4 écrite: ${output}/hip4_outcome_run_review.md"
    [ -s "$review_stderr" ] && warn "Stderr review capturé: ${review_stderr}"
    return 0
}

echo ""
echo "========================================="
echo "  Fetch TRIDENT-HIP4"
echo "========================================="
echo ""

if [ -z "$REVIEW_ONLY" ]; then
    if [ -z "$LOGS_ONLY" ]; then
        fetch_api
    fi
    fetch_logs_runtime
fi

run_review

echo ""
ok "Fetch TRIDENT-HIP4 terminé dans ${LOCAL_DIR}/"
