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
Usage: ./deploy.sh [--host trident-hetzner] [--user trident-deploy] [--identity ~/.ssh/trident_hetzner_ed25519] [--start] [--with-pod-b] [--with-pod-c] [--with-funding]

Déploie TRIDENT sur le serveur :
- rsync du code vers /opt/trident
- build Docker sur le serveur
- optionnellement démarre les services

Par défaut :
- host SSH : trident-hetzner
- démarrage avec `--start` : API + Pod A
- `--with-pod-b` ajoute Pod B
- `--with-pod-c` ajoute Pod C
- `--with-funding` ajoute le collecteur funding/OI autonome
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
WITH_FUNDING=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

selected_pods_label() {
    local pods=("API" "Pod A")
    [ -n "$WITH_POD_B" ] && pods+=("Pod B")
    [ -n "$WITH_POD_C" ] && pods+=("Pod C")
    [ -n "$WITH_FUNDING" ] && pods+=("Funding Collector")
    local joined=""
    local pod
    for pod in "${pods[@]}"; do
        if [ -z "$joined" ]; then
            joined="$pod"
        else
            joined="${joined}, $pod"
        fi
    done
    printf '%s' "$joined"
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
        --with-funding)
            WITH_FUNDING="true"
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

    # Write VERSION file from git before rsync
    local _commit _date _version
    _commit="$(git rev-parse --short=8 HEAD 2>/dev/null || echo 'unknown')"
    _date="$(TZ=Europe/Paris git log -1 --format='%cd' --date=format-local:'%Y-%m-%d %H:%M' 2>/dev/null || echo '')"
    _version="${_commit} (${_date})"
    echo "${_version}" > "${SCRIPT_DIR}/VERSION"
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
        --exclude='data/funding_history' \
        --exclude='data/research' \
        --exclude='docs/pod_funding_research_latest.json' \
        --exclude='docs/pod_funding_research_latest.md' \
        --exclude='docs/pod_liq_research_latest.json' \
        --exclude='docs/pod_liq_research_latest.md' \
        --exclude='logs' \
        --exclude='runtime' \
        --exclude='.env.trident' \
        -e "ssh -i ${IDENTITY_FILE}" \
        "${SCRIPT_DIR}/" "${SSH_USER}@${HOST}:${DEPLOY_DIR}/"

    ok "Code transféré"
}

build_remote() {
    info "Build Docker sur le serveur..."
    local profile_args=""
    [ -n "$WITH_POD_B" ] && profile_args="${profile_args} --profile pod_b"
    [ -n "$WITH_POD_C" ] && profile_args="${profile_args} --profile pod_c"
    [ -n "$WITH_FUNDING" ] && profile_args="${profile_args} --profile funding"
    ssh_remote "cd ${DEPLOY_DIR} && docker compose -f docker-compose.trident.yml${profile_args} build"
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
    [ -n "$WITH_FUNDING" ] && extra_args="${extra_args} --with-funding"
    info "Services demandés: $(selected_pods_label)"
    ssh_remote "cd ${DEPLOY_DIR} && ./scripts/trident_server.sh start${extra_args}"
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
    echo "Services actifs sur ${HOST}: $(selected_pods_label)"
    echo "  SSH : ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST}"
    echo "  Dashboard public : http://<server-ip-or-dns>:3000/dashboard"
    echo "  API health : http://<server-ip-or-dns>:3000/health"
    echo "  État runtime : http://<server-ip-or-dns>:3000/api/state"
    echo "  Note : si ${HOST} est un alias SSH local (ex: trident-hetzner), utilise l'IP publique ou le DNS du serveur dans le navigateur."
    echo "  Contrôle serveur : ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST} 'cd ${DEPLOY_DIR} && ./scripts/trident_server.sh status${WITH_POD_B:+ --with-pod-b}${WITH_POD_C:+ --with-pod-c}${WITH_FUNDING:+ --with-funding}'"
else
    echo "Pour démarrer après déploiement :"
    echo "  ./deploy.sh --start"
    echo "  ./deploy.sh --start --with-pod-b"
    echo "  ./deploy.sh --start --with-funding"
fi
echo ""
