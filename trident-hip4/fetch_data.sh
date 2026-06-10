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
- logs Nautilus shadow si le sidecar opt-in existe
- runtime states HIP-4
- configs HIP-4
- logs Docker HIP-4
- audit local exit-policy / marchés non-BTC priceBinary
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
SSH_CONTROL_PATH="${TRIDENT_SSH_CONTROL_PATH:-${TMPDIR:-/tmp}/trident-hip4-fetch-%C}"
mkdir -p "$(dirname "$SSH_CONTROL_PATH")" 2>/dev/null || true
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
SSH_OPTS=("${SSH_OPTS[@]}" -o ControlMaster=auto -o ControlPersist=60 -o "ControlPath=${SSH_CONTROL_PATH}")
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
    "${LOG_DIR}/hip4_outcome_mainnet" \
    "${LOG_DIR}/hip4_nautilus_shadow"

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
    capture_remote "${API_DIR}/hip4-nautilus-shadow-${ts}.json" "curl -fsS http://127.0.0.1:${API_PORT}/api/hip4-nautilus-shadow"
    capture_remote "${API_DIR}/state-${ts}.json" "curl -fsS http://127.0.0.1:${API_PORT}/api/state"
    capture_remote "${API_DIR}/report-${ts}.json" "curl -fsS http://127.0.0.1:${API_PORT}/api/report"
    ok "API TRIDENT-HIP4 sauvegardée dans ${API_DIR}/"
}

fetch_logs_runtime() {
    info "Rapatriement logs/runtime TRIDENT-HIP4..."
    fetch_optional_remote_file "logs/hip4_outcome_status.json" "${RUNTIME_DIR}/hip4_outcome_status.json" "Runtime status HIP-4"
    fetch_optional_remote_file "logs/hip4_outcome_mainnet_status.json" "${RUNTIME_DIR}/hip4_outcome_mainnet_status.json" "Runtime status HIP-4 mainnet observer"
    fetch_optional_remote_file "logs/hip4_nautilus_shadow/status.json" "${RUNTIME_DIR}/hip4_nautilus_shadow_status.json" "Runtime status Nautilus shadow"
    fetch_optional_remote_file "logs/pod_b_live_status.json" "${RUNTIME_DIR}/pod_b_live_status.json" "Alias status HIP-4"
    fetch_optional_remote_file "logs/trident_hip4_deployment_profile.json" "${RUNTIME_DIR}/trident_hip4_deployment_profile.json" "Profil de déploiement TRIDENT-HIP4"
    fetch_optional_remote_file "runtime/hip4_outcome_state.json" "${RUNTIME_DIR}/hip4_outcome_state.json" "State HIP-4"
    fetch_optional_remote_file "runtime/hip4_outcome_paper_state.json" "${RUNTIME_DIR}/hip4_outcome_paper_state.json" "State HIP-4 paper"
    fetch_optional_remote_file "runtime/hip4_outcome_testnet_state.json" "${RUNTIME_DIR}/hip4_outcome_testnet_state.json" "State HIP-4 testnet"
    fetch_optional_remote_file "runtime/hip4_outcome_mainnet_paper_state.json" "${RUNTIME_DIR}/hip4_outcome_mainnet_paper_state.json" "State HIP-4 mainnet paper"
    fetch_optional_remote_file "runtime/hip4_outcome_mainnet_state.json" "${RUNTIME_DIR}/hip4_outcome_mainnet_state.json" "State HIP-4 mainnet observer"
    fetch_optional_remote_file "runtime/hip4_nautilus_shadow_state.json" "${RUNTIME_DIR}/hip4_nautilus_shadow_state.json" "State Nautilus shadow"
    fetch_optional_remote_file "config/hip4_outcome_testnet.toml" "${CONFIG_DIR}/hip4_outcome_testnet.toml" "Config HIP-4 testnet"
    fetch_optional_remote_file "config/hip4_outcome_mainnet_paper.toml" "${CONFIG_DIR}/hip4_outcome_mainnet_paper.toml" "Config HIP-4 mainnet paper"
    fetch_optional_remote_file "config/hip4_outcome_mainnet_observer.toml" "${CONFIG_DIR}/hip4_outcome_mainnet_observer.toml" "Config HIP-4 mainnet observer"
    fetch_optional_remote_file "config/hip4_nautilus_shadow.toml" "${CONFIG_DIR}/hip4_nautilus_shadow.toml" "Config Nautilus shadow"
    fetch_optional_remote_dir "logs/hip4_outcome_paper" "${LOG_DIR}/hip4_outcome_paper" "Logs HIP-4 paper"
    fetch_optional_remote_dir "logs/hip4_outcome_testnet" "${LOG_DIR}/hip4_outcome_testnet" "Logs HIP-4 testnet"
    fetch_optional_remote_dir "logs/hip4_outcome_mainnet_paper" "${LOG_DIR}/hip4_outcome_mainnet_paper" "Logs HIP-4 mainnet paper"
    fetch_optional_remote_dir "logs/hip4_outcome_mainnet" "${LOG_DIR}/hip4_outcome_mainnet" "Logs HIP-4 mainnet observer"
    fetch_optional_remote_dir "logs/hip4_nautilus_shadow" "${LOG_DIR}/hip4_nautilus_shadow" "Logs Nautilus shadow"

    capture_remote "${DOCKER_DIR}/hip4-api.log" "COMPOSE_PROJECT_NAME=trident-hip4 docker compose -f docker-compose.hip4.yml logs --tail ${LOG_LINES} hip4-api 2>&1"
    capture_remote "${DOCKER_DIR}/hip4-outcome-paper.log" "COMPOSE_PROJECT_NAME=trident-hip4 docker compose -f docker-compose.hip4.yml logs --tail ${LOG_LINES} hip4-outcome-paper 2>&1"
    capture_remote "${DOCKER_DIR}/hip4-mainnet-observer.log" "COMPOSE_PROJECT_NAME=trident-hip4 docker compose -f docker-compose.hip4.yml --profile mainnet_observer logs --tail ${LOG_LINES} hip4-mainnet-observer 2>&1"
    capture_remote "${DOCKER_DIR}/hip4-nautilus-shadow.log" "COMPOSE_PROJECT_NAME=trident-hip4 docker compose -f docker-compose.hip4.yml --profile nautilus_shadow logs --tail ${LOG_LINES} hip4-nautilus-shadow 2>&1"
    ok "Logs/runtime TRIDENT-HIP4 rapatriés"
}

latest_file() {
    find "$1" -maxdepth 1 -type f -name "$2" 2>/dev/null | sort | tail -n 1
}

write_next_review_focus() {
    local output="$1"
    local focus_path="${output}/hip4_next_review_focus.md"
    cat > "$focus_path" <<'EOF'
# HIP-4 next review focus

Priorite apres le redeploiement du `2026-06-10`: verifier que la promotion
paper `early_exit_policy = "prob_stop_full"` garde la convexite, limite les
full exits aux stops defensifs et n'ouvre pas de surface non-BTC implicite.

## Checks obligatoires

- Config/status:
  - `early_exit_policy = "prob_stop_full"` actif en mainnet paper.
  - `early_exit_ev_exit_fraction = 0.5` conserve pour la policy `default` et
    l'observabilite, mais non actif sous `prob_stop_full`.
  - `early_exit_reentry_lock_until_settlement = true` actif.
  - `summary.pnl_levers.active_policy` expose `prob_stop_full`.
  - `summary.pnl_levers.active_dry_run` expose `prob_stop_full` actif et
    `bid_over_conservative_hold_ev` inactif.
- `early_exits.csv`:
  - sous `prob_stop_full`, les sorties actives attendues sont uniquement
    `probability_stop`.
  - pas de `bid_over_conservative_hold_ev`, `partial_take_profit`,
    `full_take_profit` ou `free_short_expiry_window` actifs sauf rollback
    explicite vers la policy `default`.
- `decisions.jsonl` / review:
  - verifier la presence de `early_exit_reentry_lock` apres un full exit, ou
    `market_already_open` quand le runner reste ouvert.
  - verifier qu'il n'y a pas de re-entry opposite-side sur le meme market/expiry
    avant settlement.
- `shadow_exit_policies.csv`:
  - comparer `hold_to_settlement`, `ev_plus_2pct_full` et
    `ev_plus_2pct_partial_runner` contre `prob_stop_full`.
  - mesurer PnL, PF, max drawdown et worst loss par politique, pas seulement le
    win rate.
- `hip4_policy_market_audit_latest.md`:
  - verifier les cutoffs post `2026-06-02` et `2026-06-05`;
  - confirmer que `mainnet_paper` et `mainnet_observer` restent BTC-only en
    `priceBinary`, ou lister les underlyings non-BTC tradables apparus.
- `shadow_sizing.csv`:
  - garder le sizing actif inchangé tant que le shadow Kelly est sous le
    minimum executable.
  - ne pas arrondir automatiquement un sizing theorique < `min_order_value_usdc`
    vers le minimum Hyperliquid; le bon verdict experimental est `skip` ou
    shadow.
- Readiness:
  - continuer a reporter settlements, expiries/marches, PF, Brier et samples
    calibration; aucune promotion mainnet sans seuils atteints.
EOF
    cp "$focus_path" "${REPLAY_REPORT_DIR}/hip4_next_review_focus_latest.md"
    ok "Checklist prochaine review HIP-4 écrite: ${focus_path}"
}

run_policy_market_audit() {
    local ts output_json output_md latest_json latest_md audit_stdout audit_stderr
    if [ -n "$SKIP_REVIEW" ]; then
        warn "Audit policy/market HIP-4 skippé"
        return 0
    fi
    ts="$(timestamp)"
    mkdir -p "$REPLAY_REPORT_DIR"
    output_json="${REPLAY_REPORT_DIR}/hip4_policy_market_audit_${ts}.json"
    output_md="${REPLAY_REPORT_DIR}/hip4_policy_market_audit_${ts}.md"
    latest_json="${REPLAY_REPORT_DIR}/hip4_policy_market_audit_latest.json"
    latest_md="${REPLAY_REPORT_DIR}/hip4_policy_market_audit_latest.md"
    audit_stdout="${REPLAY_REPORT_DIR}/hip4_policy_market_audit_${ts}.stdout.json"
    audit_stderr="${REPLAY_REPORT_DIR}/hip4_policy_market_audit_${ts}.stderr.txt"

    info "Generation de l'audit HIP-4 exit-policy / marches non-BTC..."
    local audit_cmd=()
    if command -v uv >/dev/null 2>&1; then
        audit_cmd=(uv run python -m app.backtest.hip4_outcome_policy_market_audit)
    else
        audit_cmd=(python3 -m app.backtest.hip4_outcome_policy_market_audit)
    fi
    if ! "${audit_cmd[@]}" \
        --paper-logs-dir "${LOG_DIR}/hip4_outcome_mainnet_paper" \
        --observer-logs-dir "${LOG_DIR}/hip4_outcome_mainnet" \
        --output-json "$output_json" \
        --output-md "$output_md" \
        >"$audit_stdout" \
        2>"$audit_stderr"; then
        warn "Audit policy/market HIP-4 échoué; fetch/review poursuivis:"
        warn "  stdout: ${audit_stdout}"
        warn "  stderr: ${audit_stderr}"
        return 0
    fi
    cp "$output_json" "$latest_json"
    cp "$output_md" "$latest_md"
    ok "Audit policy/market HIP-4 écrit: ${output_md}"
    [ -s "$audit_stderr" ] && warn "Stderr audit capturé: ${audit_stderr}"
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
        --nautilus-shadow-dir "${LOG_DIR}/hip4_nautilus_shadow" \
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
    write_next_review_focus "$output"
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

run_policy_market_audit
run_review

echo ""
ok "Fetch TRIDENT-HIP4 terminé dans ${LOCAL_DIR}/"
