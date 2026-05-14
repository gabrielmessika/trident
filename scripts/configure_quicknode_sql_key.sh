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
Usage: ./scripts/configure_quicknode_sql_key.sh <quicknode_sql_api_key> [options]

Configure la clé QuickNode SQL Explorer dans /opt/trident/.env.trident côté
serveur, puis lance un test court du backfill TP/SL historique.

Options:
  --host <host>          Host SSH. Défaut: trident-hetzner
  --user <user>          User SSH. Défaut: trident-deploy
  --identity <path>      Clé SSH. Défaut: ~/.ssh/trident_hetzner_ed25519
  --remote-dir <path>    Répertoire TRIDENT serveur. Défaut: /opt/trident
  --skip-test            Configure seulement la clé, sans test SQL
  --run-backfill         Lance ensuite le backfill historique en arrière-plan
  --backfill-start <ts>  Début backfill UTC. Défaut: 2026-04-01
  --backfill-end <ts>    Fin backfill UTC. Défaut: 2026-05-14T17:00:00Z
  --test-start <ts>      Début test UTC. Défaut: 2026-04-29
  --test-end <ts>        Fin test UTC. Défaut: 2026-04-30
  -h, --help             Affiche cette aide

Exemples:
  ./scripts/configure_quicknode_sql_key.sh qn_sql_xxx
  ./scripts/configure_quicknode_sql_key.sh qn_sql_xxx --run-backfill
EOF
}

if [[ $# -lt 1 ]]; then
    usage
    exit 1
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

SQL_API_KEY="$1"
shift

HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
SSH_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
REMOTE_DIR="${TRIDENT_DEPLOY_DIR:-/opt/trident}"
SKIP_TEST=""
RUN_BACKFILL=""
BACKFILL_START="2026-04-01"
BACKFILL_END="2026-05-14T17:00:00Z"
TEST_START="2026-04-29"
TEST_END="2026-04-30"

while [[ $# -gt 0 ]]; do
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
        --skip-test)
            SKIP_TEST="true"
            shift
            ;;
        --run-backfill)
            RUN_BACKFILL="true"
            shift
            ;;
        --backfill-start)
            BACKFILL_START="$2"
            shift 2
            ;;
        --backfill-end)
            BACKFILL_END="$2"
            shift 2
            ;;
        --test-start)
            TEST_START="$2"
            shift 2
            ;;
        --test-end)
            TEST_END="$2"
            shift 2
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

if [[ -z "${SQL_API_KEY}" ]]; then
    error "Clé QuickNode SQL vide."
    exit 1
fi

SSH_ARGS=()
if [[ -f "${IDENTITY_FILE}" ]]; then
    SSH_ARGS+=(-i "${IDENTITY_FILE}")
else
    warn "Clé SSH absente: ${IDENTITY_FILE}. Utilisation de la config SSH système."
fi
SSH_ARGS+=(-o BatchMode=yes -o ConnectTimeout=10)

shell_quote() {
    local value="$1"
    printf "'%s'" "$(printf '%s' "${value}" | sed "s/'/'\\\\''/g")"
}

ssh_remote() {
    ssh "${SSH_ARGS[@]}" "${SSH_USER}@${HOST}" "$@"
}

run_remote_script_with_key() {
    local remote_dir_q key_b64 key_b64_q
    remote_dir_q="$(shell_quote "${REMOTE_DIR}")"
    key_b64="$(printf '%s' "${SQL_API_KEY}" | base64 | tr -d '\n')"
    key_b64_q="$(shell_quote "${key_b64}")"
    ssh_remote "cd ${remote_dir_q} && TRIDENT_SQL_KEY_B64=${key_b64_q} bash -s" <<'REMOTE'
set -euo pipefail

sql_key="$(printf '%s' "${TRIDENT_SQL_KEY_B64}" | base64 -d)"
touch .env.trident
cp .env.trident ".env.trident.bak.$(date +%s)"
tmp_env="$(mktemp)"
grep -v '^TRIDENT_TRIGGER_LIQUIDITY_SQL_API_KEY=' .env.trident > "${tmp_env}" || true
printf 'TRIDENT_TRIGGER_LIQUIDITY_SQL_API_KEY=%s\n' "${sql_key}" >> "${tmp_env}"
mv "${tmp_env}" .env.trident
chmod 600 .env.trident 2>/dev/null || true
unset sql_key TRIDENT_SQL_KEY_B64
echo "sql_key_configured"
REMOTE
}

run_sql_test() {
    local remote_dir_q test_start_q test_end_q
    remote_dir_q="$(shell_quote "${REMOTE_DIR}")"
    test_start_q="$(shell_quote "${TEST_START}")"
    test_end_q="$(shell_quote "${TEST_END}")"
    ssh_remote "cd ${remote_dir_q} && TEST_START=${test_start_q} TEST_END=${test_end_q} bash -s" <<'REMOTE'
set -euo pipefail

rm -rf runtime/trigger_liquidity_sql_test
rm -f \
  runtime/trigger_liquidity_sql_backfill_test_state.json \
  runtime/trigger_liquidity_sql_backfill_test_status.json

docker compose --env-file .env.trident -f docker-compose.trident.yml run --rm --no-deps trident-api \
  python -m app.live.trigger_liquidity_sql_backfill \
    --start "${TEST_START}" \
    --end "${TEST_END}" \
    --output-dir runtime/trigger_liquidity_sql_test \
    --state-path runtime/trigger_liquidity_sql_backfill_test_state.json \
    --status-output runtime/trigger_liquidity_sql_backfill_test_status.json \
    --max-pages 1 \
    --sleep-seconds 0

cat runtime/trigger_liquidity_sql_backfill_test_status.json
sample_file="$(find runtime/trigger_liquidity_sql_test -type f -name '*.jsonl' | sort | head -n 1 || true)"
if [[ -n "${sample_file}" ]]; then
  echo
  echo "sample_file=${sample_file}"
  sed -n '1p' "${sample_file}"
fi
REMOTE
}

start_backfill() {
    local remote_dir_q backfill_start_q backfill_end_q
    remote_dir_q="$(shell_quote "${REMOTE_DIR}")"
    backfill_start_q="$(shell_quote "${BACKFILL_START}")"
    backfill_end_q="$(shell_quote "${BACKFILL_END}")"
    ssh_remote "cd ${remote_dir_q} && BACKFILL_START=${backfill_start_q} BACKFILL_END=${backfill_end_q} bash -s" <<'REMOTE'
set -euo pipefail

mkdir -p logs runtime
nohup docker compose --env-file .env.trident -f docker-compose.trident.yml run --rm --no-deps trident-api \
  python -m app.live.trigger_liquidity_sql_backfill \
    --start "${BACKFILL_START}" \
    --end "${BACKFILL_END}" \
    --output-dir data/trigger_liquidity \
  > logs/trigger_liquidity_sql_backfill.log 2>&1 &
echo "$!" > runtime/trigger_liquidity_sql_backfill.pid
echo "backfill_started pid=$(cat runtime/trigger_liquidity_sql_backfill.pid)"
REMOTE
}

info "Configuration de la clé QuickNode SQL sur ${SSH_USER}@${HOST}:${REMOTE_DIR}..."
run_remote_script_with_key
ok "Clé SQL configurée dans .env.trident"

if [[ -z "${SKIP_TEST}" ]]; then
    info "Test SQL Explorer court (${TEST_START} -> ${TEST_END})..."
    run_sql_test
    ok "Test SQL terminé"
else
    warn "Test SQL ignoré (--skip-test)."
fi

if [[ -n "${RUN_BACKFILL}" ]]; then
    info "Démarrage du backfill historique (${BACKFILL_START} -> ${BACKFILL_END})..."
    start_backfill
    ok "Backfill lancé en arrière-plan"
    echo "Suivi:"
    echo "  ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST} 'cd ${REMOTE_DIR} && tail -f logs/trigger_liquidity_sql_backfill.log'"
    echo "Status:"
    echo "  ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST} 'cd ${REMOTE_DIR} && cat runtime/trigger_liquidity_sql_backfill_status.json'"
fi
