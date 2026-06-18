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
Usage: ./scripts/fetch_all_data.sh [options]

Rapatrie en une commande les donnees des deux apps separees:
- TRIDENT A/C via ./scripts/fetch_trident_data.sh
- TRIDENT-HIP4 via ./trident-hip4/fetch_data.sh

Options communes:
  --host HOST
  --user USER
  --identity PATH
  --local-dir DIR          Base locale: DIR pour TRIDENT A/C, DIR/hip4 pour HIP-4
  --logs-only
  --review-only
  --dry-run

Options TRIDENT A/C:
  --days N
  --date YYYY-MM-DD
  --all
  --snapshots-only         Lance seulement le fetch snapshots TRIDENT A/C
  --trident-remote-dir DIR
  --trident-local-dir DIR

Options TRIDENT-HIP4:
  --hip4-remote-dir DIR
  --hip4-local-dir DIR
  --hip4-api-port PORT
  --skip-hip4-review

Controle:
  --skip-trident
  --skip-hip4
  --fail-fast              S'arrete au premier fetch en erreur
  -h, --help
EOF
}

need_value() {
    local option="$1"
    local value="${2:-}"
    if [ -z "$value" ]; then
        error "${option} demande une valeur"
        usage
        exit 1
    fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TRIDENT_FETCH="${SCRIPT_DIR}/fetch_trident_data.sh"
HIP4_FETCH="${REPO_ROOT}/trident-hip4/fetch_data.sh"

HOST=""
SSH_USER=""
IDENTITY_FILE=""
BASE_LOCAL_DIR=""
TRIDENT_LOCAL_DIR=""
HIP4_LOCAL_DIR=""
TRIDENT_REMOTE_DIR=""
HIP4_REMOTE_DIR=""
HIP4_API_PORT=""
DAYS=""
DATE_FILTER=""
FETCH_ALL=""
LOGS_ONLY=""
SNAPSHOTS_ONLY=""
REVIEW_ONLY=""
DRY_RUN=""
SKIP_TRIDENT=""
SKIP_HIP4=""
SKIP_HIP4_REVIEW=""
FAIL_FAST=""

while [ $# -gt 0 ]; do
    case "$1" in
        --host)
            need_value "$1" "${2:-}"
            HOST="$2"
            shift 2
            ;;
        --user)
            need_value "$1" "${2:-}"
            SSH_USER="$2"
            shift 2
            ;;
        --identity)
            need_value "$1" "${2:-}"
            IDENTITY_FILE="$2"
            shift 2
            ;;
        --local-dir)
            need_value "$1" "${2:-}"
            BASE_LOCAL_DIR="$2"
            shift 2
            ;;
        --trident-local-dir)
            need_value "$1" "${2:-}"
            TRIDENT_LOCAL_DIR="$2"
            shift 2
            ;;
        --hip4-local-dir)
            need_value "$1" "${2:-}"
            HIP4_LOCAL_DIR="$2"
            shift 2
            ;;
        --trident-remote-dir)
            need_value "$1" "${2:-}"
            TRIDENT_REMOTE_DIR="$2"
            shift 2
            ;;
        --hip4-remote-dir)
            need_value "$1" "${2:-}"
            HIP4_REMOTE_DIR="$2"
            shift 2
            ;;
        --hip4-api-port)
            need_value "$1" "${2:-}"
            HIP4_API_PORT="$2"
            shift 2
            ;;
        --days)
            need_value "$1" "${2:-}"
            DAYS="$2"
            shift 2
            ;;
        --date)
            need_value "$1" "${2:-}"
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
        --skip-trident)
            SKIP_TRIDENT="true"
            shift
            ;;
        --skip-hip4)
            SKIP_HIP4="true"
            shift
            ;;
        --skip-hip4-review)
            SKIP_HIP4_REVIEW="true"
            shift
            ;;
        --fail-fast)
            FAIL_FAST="true"
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

if [ ! -x "$TRIDENT_FETCH" ]; then
    error "Script TRIDENT introuvable ou non executable: ${TRIDENT_FETCH}"
    exit 1
fi

if [ ! -x "$HIP4_FETCH" ]; then
    error "Script TRIDENT-HIP4 introuvable ou non executable: ${HIP4_FETCH}"
    exit 1
fi

if [ -n "$LOGS_ONLY" ] && [ -n "$SNAPSHOTS_ONLY" ]; then
    error "--logs-only et --snapshots-only sont incompatibles"
    exit 1
fi

if [ -n "$BASE_LOCAL_DIR" ]; then
    [ -z "$TRIDENT_LOCAL_DIR" ] && TRIDENT_LOCAL_DIR="$BASE_LOCAL_DIR"
    [ -z "$HIP4_LOCAL_DIR" ] && HIP4_LOCAL_DIR="${BASE_LOCAL_DIR%/}/hip4"
fi

common_args=()
[ -n "$HOST" ] && common_args+=(--host "$HOST")
[ -n "$SSH_USER" ] && common_args+=(--user "$SSH_USER")
[ -n "$IDENTITY_FILE" ] && common_args+=(--identity "$IDENTITY_FILE")
[ -n "$LOGS_ONLY" ] && common_args+=(--logs-only)
[ -n "$REVIEW_ONLY" ] && common_args+=(--review-only)
[ -n "$DRY_RUN" ] && common_args+=(--dry-run)

trident_args=("${common_args[@]}")
[ -n "$TRIDENT_REMOTE_DIR" ] && trident_args+=(--remote-dir "$TRIDENT_REMOTE_DIR")
[ -n "$TRIDENT_LOCAL_DIR" ] && trident_args+=(--local-dir "$TRIDENT_LOCAL_DIR")
[ -n "$DAYS" ] && trident_args+=(--days "$DAYS")
[ -n "$DATE_FILTER" ] && trident_args+=(--date "$DATE_FILTER")
[ -n "$FETCH_ALL" ] && trident_args+=(--all)
[ -n "$SNAPSHOTS_ONLY" ] && trident_args+=(--snapshots-only)

hip4_args=("${common_args[@]}")
[ -n "$HIP4_REMOTE_DIR" ] && hip4_args+=(--remote-dir "$HIP4_REMOTE_DIR")
[ -n "$HIP4_LOCAL_DIR" ] && hip4_args+=(--local-dir "$HIP4_LOCAL_DIR")
[ -n "$HIP4_API_PORT" ] && hip4_args+=(--api-port "$HIP4_API_PORT")
[ -n "$SKIP_HIP4_REVIEW" ] && hip4_args+=(--skip-review)

if [ -n "$SNAPSHOTS_ONLY" ]; then
    if [ -n "$SKIP_TRIDENT" ]; then
        error "--snapshots-only ne peut pas etre utilise avec --skip-trident"
        exit 1
    fi
    if [ -z "$SKIP_HIP4" ]; then
        warn "--snapshots-only ne concerne que TRIDENT A/C; HIP-4 est ignore."
        SKIP_HIP4="true"
    fi
fi

if [ -n "$SKIP_TRIDENT" ] && [ -n "$SKIP_HIP4" ]; then
    error "Rien a faire: --skip-trident et --skip-hip4 sont tous les deux actifs"
    exit 1
fi

OVERALL_STATUS=0

run_fetch() {
    local label="$1"
    shift

    echo ""
    echo "========================================="
    echo "  ${label}"
    echo "========================================="
    echo ""

    if "$@"; then
        ok "${label} termine"
        return 0
    else
        local status=$?
        error "${label} en erreur (code ${status})"
        [ "$OVERALL_STATUS" -eq 0 ] && OVERALL_STATUS="$status"
        if [ -n "$FAIL_FAST" ]; then
            exit "$status"
        fi
        return 0
    fi
}

info "Fetch global TRIDENT: demarrage"

if [ -z "$SKIP_TRIDENT" ]; then
    run_fetch "Fetch TRIDENT A/C" "$TRIDENT_FETCH" "${trident_args[@]}"
else
    warn "Fetch TRIDENT A/C ignore"
fi

if [ -z "$SKIP_HIP4" ]; then
    run_fetch "Fetch TRIDENT-HIP4" "$HIP4_FETCH" "${hip4_args[@]}"
else
    warn "Fetch TRIDENT-HIP4 ignore"
fi

echo ""
if [ "$OVERALL_STATUS" -eq 0 ]; then
    ok "Fetch global TRIDENT termine"
else
    error "Fetch global TRIDENT termine avec erreur(s)"
fi

exit "$OVERALL_STATUS"
