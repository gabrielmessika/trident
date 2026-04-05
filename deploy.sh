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
Usage: ./deploy.sh [--host trident-hetzner] [--user trident-deploy] [--identity ~/.ssh/trident_hetzner_ed25519] [--start] [--with-pod-b] [--with-pod-c]

Déploie TRIDENT sur le serveur :
- rsync du code vers /opt/trident
- build Docker sur le serveur
- optionnellement démarre les services
EOF
}

# Défaut: alias SSH `trident-hetzner`
HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
SSH_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
DEPLOY_DIR="/opt/trident"
START=""
WITH_POD_B=""
WITH_POD_C=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
        --start)
            START="true"
            shift
            ;;
        --with-pod-b)
            WITH_POD_B="true"
            shift
            ;;
        --with-pod-c)
            WITH_POD_C="true"
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

if [ ! -f "$IDENTITY_FILE" ]; then
    error "Clé SSH introuvable: $IDENTITY_FILE"
    exit 1
fi

ssh_remote() {
    ssh -i "$IDENTITY_FILE" "${SSH_USER}@${HOST}" "$@"
}

validate_local() {
    info "Vérification des prérequis locaux..."
    for required in pyproject.toml Dockerfile.trident docker-compose.trident.yml plan_trident.md; do
        if [ ! -f "${SCRIPT_DIR}/${required}" ]; then
            error "Fichier requis introuvable: ${required}"
            exit 1
        fi
    done

    if ! ssh_remote true 2>/dev/null; then
        error "Connexion SSH impossible vers ${SSH_USER}@${HOST}"
        exit 1
    fi

    ok "Prérequis locaux OK"
}

deploy_code() {
    info "Transfert du code vers ${HOST}..."
    ssh_remote "mkdir -p ${DEPLOY_DIR}/data ${DEPLOY_DIR}/logs ${DEPLOY_DIR}/runtime"

    rsync -azP --delete \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='data/gbot_archive' \
        --exclude='data/server_archive' \
        --exclude='data/replay_reports' \
        --exclude='data/live_snapshots' \
        --exclude='logs' \
        --exclude='runtime' \
        --exclude='.env.trident' \
        -e "ssh -i ${IDENTITY_FILE}" \
        "${SCRIPT_DIR}/" "${SSH_USER}@${HOST}:${DEPLOY_DIR}/"

    ok "Code transféré"
}

build_remote() {
    info "Build Docker sur le serveur..."
    ssh_remote "cd ${DEPLOY_DIR} && docker compose -f docker-compose.trident.yml build"
    ok "Image Docker buildée"
}

post_checks() {
    ssh_remote "cd ${DEPLOY_DIR} && chmod +x scripts/*.sh deploy.sh prepare_server.sh 2>/dev/null || true"
    if ! ssh_remote "test -f ${DEPLOY_DIR}/.env.trident"; then
        warn "Fichier ${DEPLOY_DIR}/.env.trident absent."
        echo "  Copiez ${DEPLOY_DIR}/.env.trident.example si vous voulez surcharger des variables."
    fi
}

start_remote() {
    info "Démarrage des services sur le serveur..."
    local extra_args=""
    [ -n "$WITH_POD_B" ] && extra_args="${extra_args} --with-pod-b"
    [ -n "$WITH_POD_C" ] && extra_args="${extra_args} --with-pod-c"
    ssh_remote "cd ${DEPLOY_DIR} && ./scripts/trident_server.sh update${extra_args}"
    ok "Services démarrés"
}

echo ""
echo "========================================="
echo "  TRIDENT — Déploiement"
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
ok "DÉPLOIEMENT TERMINÉ"
echo "========================================="
echo ""
if [ -n "$START" ]; then
    echo "Services actifs sur ${HOST}"
    echo "  SSH : ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST}"
    echo "  Tunnel UI : ssh -i ${IDENTITY_FILE} -L 3000:127.0.0.1:3000 ${SSH_USER}@${HOST}"
    echo "  Contrôle serveur : ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST} 'cd ${DEPLOY_DIR} && ./scripts/trident_server.sh status'"
else
    echo "Pour démarrer après déploiement :"
    echo "  ./deploy.sh --host ${HOST} --start"
fi
echo ""
