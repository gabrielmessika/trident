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
Usage: ./trident-hip4/deploy.sh [--host trident-hetzner] [--user trident-deploy] [--identity ~/.ssh/trident_hetzner_ed25519] [--remote-dir /opt/trident-hip4] [--start] [--mode paper|observer|testnet] [--config config/hip4_outcome_mainnet_paper.toml] [--api-port 3001] [--with-mainnet-observer] [--fresh-start]

Déploie l'app séparée TRIDENT-HIP4:
- rsync du code vers /opt/trident-hip4 par défaut
- build Docker via docker-compose.hip4.yml
- optionnellement démarre l'API HIP-4 + le runner outcome paper

TRIDENT A/C n'est ni démarré ni arrêté par ce script.
EOF
}

HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
SSH_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
DEPLOY_DIR="${TRIDENT_HIP4_DEPLOY_DIR:-/opt/trident-hip4}"
START=""
HIP4_MODE="${HIP4_OUTCOME_MODE:-paper}"
HIP4_CONFIG="${HIP4_OUTCOME_CONFIG:-config/hip4_outcome_mainnet_paper.toml}"
HIP4_API_PORT="${HIP4_OUTCOME_API_PORT:-3001}"
ENABLE_MAINNET_OBSERVER="${TRIDENT_HIP4_ENABLE_MAINNET_OBSERVER:-}"
FRESH_START=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

selected_services_label() {
    local services=("HIP-4 API" "HIP-4 Outcome Paper")
    [ -n "$ENABLE_MAINNET_OBSERVER" ] && services+=("HIP-4 Mainnet Observer")
    local joined=""
    local service
    for service in "${services[@]}"; do
        if [ -z "$joined" ]; then
            joined="$service"
        else
            joined="${joined}, $service"
        fi
    done
    printf '%s' "$joined"
}

server_flags() {
    local flags=""
    local quoted_mode quoted_config quoted_port
    quoted_mode="$(printf '%q' "$HIP4_MODE")"
    quoted_config="$(printf '%q' "$HIP4_CONFIG")"
    quoted_port="$(printf '%q' "$HIP4_API_PORT")"
    flags="${flags} --mode ${quoted_mode} --config ${quoted_config} --api-port ${quoted_port}"
    [ -n "$ENABLE_MAINNET_OBSERVER" ] && flags="${flags} --with-mainnet-observer"
    [ -n "$FRESH_START" ] && flags="${flags} --fresh-start"
    printf '%s' "$flags"
}

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
            DEPLOY_DIR="$2"
            shift 2
            ;;
        --start)
            START="true"
            shift
            ;;
        --mode)
            HIP4_MODE="$2"
            shift 2
            ;;
        --config)
            HIP4_CONFIG="$2"
            shift 2
            ;;
        --api-port)
            HIP4_API_PORT="$2"
            shift 2
            ;;
        --with-mainnet-observer)
            ENABLE_MAINNET_OBSERVER="true"
            shift
            ;;
        --without-mainnet-observer)
            ENABLE_MAINNET_OBSERVER=""
            shift
            ;;
        --fresh-start)
            FRESH_START="true"
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

case "$HIP4_MODE" in
    paper|observer|testnet)
        ;;
    *)
        error "Mode HIP-4 invalide: ${HIP4_MODE}. Valeurs attendues: paper, observer ou testnet."
        exit 1
        ;;
esac

if [ ! -f "$IDENTITY_FILE" ]; then
    error "Clé SSH introuvable: $IDENTITY_FILE"
    exit 1
fi

ssh_remote() {
    ssh -i "$IDENTITY_FILE" "${SSH_USER}@${HOST}" "$@"
}

validate_local() {
    info "Vérification des prérequis locaux HIP-4..."
    for required in pyproject.toml Dockerfile.trident docker-compose.hip4.yml scripts/trident_hip4_server.sh; do
        if [ ! -f "${REPO_ROOT}/${required}" ]; then
            error "Fichier requis introuvable: ${required}"
            exit 1
        fi
    done

    if ! ssh_remote true 2>/dev/null; then
        error "Connexion SSH impossible vers ${SSH_USER}@${HOST}"
        exit 1
    fi

    ok "Prérequis locaux HIP-4 OK"
}

deploy_code() {
    info "Transfert du code HIP-4 vers ${HOST}:${DEPLOY_DIR}..."
    ssh_remote "mkdir -p ${DEPLOY_DIR}/data ${DEPLOY_DIR}/logs ${DEPLOY_DIR}/runtime"

    local _commit _date _version
    _commit="$(git -C "${REPO_ROOT}" rev-parse --short=8 HEAD 2>/dev/null || echo 'unknown')"
    _date="$(TZ=Europe/Paris git -C "${REPO_ROOT}" log -1 --format='%cd' --date=format-local:'%Y-%m-%d %H:%M' 2>/dev/null || echo '')"
    _version="${_commit} (${_date})"
    printf '%s\n' "${_version}" > "${REPO_ROOT}/VERSION"
    info "Version: ${_version}"

    rsync -azP --delete \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='data/gbot_archive' \
        --exclude='data/server_archive' \
        --exclude='data/replay_reports' \
        --exclude='data/live_snapshots' \
        --exclude='data/live_snapshots_testnet' \
        --exclude='data/live_features' \
        --exclude='data/live_features_testnet' \
        --exclude='data/funding_history' \
        --exclude='data/research' \
        --exclude='server-data' \
        --exclude='tmp' \
        --exclude='logs' \
        --exclude='runtime' \
        --exclude='.env.trident' \
        --exclude='.env.trident-hip4' \
        -e "ssh -i ${IDENTITY_FILE}" \
        "${REPO_ROOT}/" "${SSH_USER}@${HOST}:${DEPLOY_DIR}/"

    ok "Code HIP-4 transféré"
}

build_remote() {
    info "Build Docker HIP-4 sur le serveur..."
    local profile_args=""
    [ -n "$ENABLE_MAINNET_OBSERVER" ] && profile_args="${profile_args} --profile mainnet_observer"
    ssh_remote "cd ${DEPLOY_DIR} && COMPOSE_PROJECT_NAME=trident-hip4 docker compose -f docker-compose.hip4.yml${profile_args} build"
    ok "Image Docker HIP-4 buildée"
}

post_checks() {
    ssh_remote "cd ${DEPLOY_DIR} && chmod +x scripts/*.sh trident-hip4/deploy.sh prepare_server.sh 2>/dev/null || true"
    if ! ssh_remote "test -f ${DEPLOY_DIR}/.env.trident-hip4"; then
        warn "Fichier ${DEPLOY_DIR}/.env.trident-hip4 absent."
        echo "  Copiez ${DEPLOY_DIR}/trident-hip4/.env.trident-hip4.example vers ${DEPLOY_DIR}/.env.trident-hip4 si vous voulez surcharger des variables HIP-4."
    fi
}

start_remote() {
    local extra_args
    extra_args="$(server_flags)"
    info "Démarrage HIP-4 sur le serveur..."
    info "Services demandés: $(selected_services_label)"
    info "Mode HIP-4 demandé: ${HIP4_MODE}"
    info "Config HIP-4 demandée: ${HIP4_CONFIG}"
    ssh_remote "cd ${DEPLOY_DIR} && ./scripts/trident_hip4_server.sh start${extra_args}"
    ok "Services HIP-4 démarrés"
}

echo ""
echo "========================================="
echo "  TRIDENT-HIP4 — Déploiement"
echo "========================================="
echo ""

validate_local
deploy_code
build_remote
post_checks

if [ -n "$START" ]; then
    start_remote
fi

echo ""
echo "========================================="
ok "DÉPLOIEMENT HIP-4 TERMINÉ"
echo "========================================="
echo ""
if [ -n "$START" ]; then
    echo "Services HIP-4 actifs sur ${HOST}: $(selected_services_label)"
    echo "  Mode HIP-4 actif : ${HIP4_MODE}"
    echo "  Config active : ${HIP4_CONFIG}"
    echo "  SSH : ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST}"
    echo "  Dashboard HIP-4 : http://<server-ip-or-dns>:${HIP4_API_PORT}/hip4-outcome"
    echo "  API HIP-4 : http://<server-ip-or-dns>:${HIP4_API_PORT}/api/hip4-outcome"
    echo "  Contrôle serveur : ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST} 'cd ${DEPLOY_DIR} && ./scripts/trident_hip4_server.sh status$(server_flags)'"
else
    echo "Pour démarrer après déploiement :"
    echo "  ./trident-hip4/deploy.sh --start"
    echo "  ./trident-hip4/deploy.sh --start --with-mainnet-observer"
fi
echo ""
