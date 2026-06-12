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
Usage: ./deploy.sh [--host trident-hetzner] [--user trident-deploy] [--identity ~/.ssh/trident_hetzner_ed25519] [--start] [--mode dry-run|live] [--network mainnet|testnet] [--config config/trident.toml] [--without-pod-c] [--without-funding] [--fresh-start]

Déploie TRIDENT sur le serveur :
- rsync du code vers /opt/trident
- build Docker sur le serveur
- optionnellement démarre les services
- permet de choisir explicitement le mode dry-run/live et le fichier de config
- `--network testnet` sélectionne `config/trident_testnet.toml` par défaut
- `--fresh-start` purge les journaux/statuts live avant démarrage

Par défaut :
- host SSH : trident-hetzner
- mode : dry-run
- réseau A/C : mainnet
- démarrage avec `--start` : API + Pod A + Pod C + funding en dry-run
- `--without-pod-c` retire Pod C
- `--without-funding` retire le collecteur funding/OI global

Sécurité live :
- `--mode live` lance Pod A + Pod C en vrais ordres. Le réseau A/C est choisi par `--network`.
- `--mode live --network testnet` lance les vrais ordres Pod A/Pod C sur
  Hyperliquid testnet, avec une config et des state files séparés.
- le serveur lance un preflight Pod A/Pod C: credentials + reconciliation + orderUpdates.
- pour un live Pod A seul, ajoutez aussi `--without-pod-c`.

Compatibilité :
- `--with-pod-c`, `--with-funding` restent acceptés mais sont désormais redondants.
- Pod B HIP-4 est déployé par `./trident-hip4/deploy.sh`.
EOF
}

# Défaut: alias SSH `trident-hetzner`
HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
SSH_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
DEPLOY_DIR="/opt/trident"
START=""
MODE="${TRIDENT_MODE:-dry-run}"
EXCHANGE_NETWORK="${TRIDENT_EXCHANGE_NETWORK:-mainnet}"
CONFIG_PATH_EXPLICIT="${TRIDENT_CONFIG_PATH:+true}"
CONFIG_PATH="${TRIDENT_CONFIG_PATH:-config/trident.toml}"
ENABLE_POD_C="true"
ENABLE_FUNDING="true"
FRESH_START=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

selected_pods_label() {
    local pods=("API" "Pod A")
    [ -n "$ENABLE_POD_C" ] && pods+=("Pod C" "Tradfi Funding Collector")
    [ -n "$ENABLE_FUNDING" ] && pods+=("Funding Collector")
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

resolve_network_config() {
    case "$EXCHANGE_NETWORK" in
        mainnet|testnet)
            ;;
        *)
            error "Réseau invalide: ${EXCHANGE_NETWORK}. Valeurs attendues: mainnet ou testnet."
            exit 1
            ;;
    esac

    if [ "$EXCHANGE_NETWORK" = "testnet" ] && [ -z "$CONFIG_PATH_EXPLICIT" ]; then
        CONFIG_PATH="config/trident_testnet.toml"
    elif [ "$EXCHANGE_NETWORK" = "mainnet" ] && [ -z "$CONFIG_PATH_EXPLICIT" ]; then
        CONFIG_PATH="config/trident.toml"
    fi

    if [ "$EXCHANGE_NETWORK" = "testnet" ] && [[ "$CONFIG_PATH" != *testnet* ]]; then
        warn "Réseau testnet demandé avec une config non-testnet (${CONFIG_PATH}); vérifie les endpoints Hyperliquid."
    elif [ "$EXCHANGE_NETWORK" = "mainnet" ] && [[ "$CONFIG_PATH" == *testnet* ]]; then
        warn "Réseau mainnet demandé avec une config testnet (${CONFIG_PATH}); vérifie les endpoints Hyperliquid."
    fi
}

selected_server_flags() {
    local flags=""
    local quoted_config
    local quoted_mode
    local quoted_network
    quoted_mode="$(printf '%q' "$MODE")"
    quoted_network="$(printf '%q' "$EXCHANGE_NETWORK")"
    quoted_config="$(printf '%q' "$CONFIG_PATH")"
    flags="${flags} --mode ${quoted_mode} --network ${quoted_network} --config ${quoted_config}"
    [ -z "$ENABLE_POD_C" ] && flags="${flags} --without-pod-c"
    [ -z "$ENABLE_FUNDING" ] && flags="${flags} --without-funding"
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
        --start)
            START="true"
            shift
            ;;
        --mode)
            MODE="$2"
            shift 2
            ;;
        --network|--exchange-network)
            EXCHANGE_NETWORK="$2"
            shift 2
            ;;
        --testnet)
            EXCHANGE_NETWORK="testnet"
            shift
            ;;
        --config)
            CONFIG_PATH="$2"
            CONFIG_PATH_EXPLICIT="true"
            shift 2
            ;;
        --with-pod-c)
            ENABLE_POD_C="true"
            shift
            ;;
        --with-funding)
            ENABLE_FUNDING="true"
            shift
            ;;
        --without-pod-c)
            ENABLE_POD_C=""
            shift
            ;;
        --without-funding)
            ENABLE_FUNDING=""
            shift
            ;;
        --with-pod-b|--without-pod-b|--only-pod-b|--pod-b-only|--with-hip4-outcome|--without-hip4-outcome|--with-hip4-mainnet-observer|--without-hip4-mainnet-observer)
            error "Pod B/HIP-4 a été séparé. Utilise ./trident-hip4/deploy.sh."
            exit 1
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

case "$MODE" in
    dry-run|live)
        ;;
    *)
        error "Mode invalide: ${MODE}. Valeurs attendues: dry-run ou live."
        exit 1
        ;;
esac

resolve_network_config

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
        --exclude='data/live_snapshots_testnet' \
        --exclude='data/live_features' \
        --exclude='data/live_features_testnet' \
        --exclude='data/funding_history' \
        --exclude='data/research' \
        --exclude='audit_backfills' \
        --exclude='audit_exports' \
        --exclude='server-data' \
        --exclude='tmp' \
        --exclude='docs/pod_funding_research_latest.json' \
        --exclude='docs/pod_funding_research_latest.md' \
        --exclude='docs/pod_liq_research_latest.json' \
        --exclude='docs/pod_liq_research_latest.md' \
        --exclude='logs' \
        --exclude='runtime' \
        --exclude='.env.trident' \
        --exclude='.env.trident-hip4' \
        --exclude='.env.tridentai' \
        -e "ssh -i ${IDENTITY_FILE}" \
        "${SCRIPT_DIR}/" "${SSH_USER}@${HOST}:${DEPLOY_DIR}/"

    ok "Code transféré"
}

build_remote() {
    info "Build Docker sur le serveur..."
    local profile_args=""
    [ -n "$ENABLE_POD_C" ] && profile_args="${profile_args} --profile pod_c"
    [ -n "$ENABLE_FUNDING" ] && profile_args="${profile_args} --profile funding"
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
    local extra_args
    extra_args="$(selected_server_flags)"
    info "Services demandés: $(selected_pods_label)"
    info "Mode demandé: ${MODE}"
    info "Réseau A/C demandé: ${EXCHANGE_NETWORK}"
    info "Config demandée: ${CONFIG_PATH}"
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
    echo "  Mode actif : ${MODE}"
    echo "  Réseau A/C actif : ${EXCHANGE_NETWORK}"
    echo "  Config active : ${CONFIG_PATH}"
    echo "  SSH : ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST}"
    echo "  Dashboard public : http://<server-ip-or-dns>:3000/dashboard"
    echo "  API health : http://<server-ip-or-dns>:3000/health"
    echo "  État runtime : http://<server-ip-or-dns>:3000/api/state"
    echo "  Note : si ${HOST} est un alias SSH local (ex: trident-hetzner), utilise l'IP publique ou le DNS du serveur dans le navigateur."
    echo "  Contrôle serveur : ssh -i ${IDENTITY_FILE} ${SSH_USER}@${HOST} 'cd ${DEPLOY_DIR} && ./scripts/trident_server.sh status$(selected_server_flags)'"
else
    echo "Pour démarrer après déploiement :"
    echo "  ./deploy.sh --start --mode dry-run"
    echo "  ./deploy.sh --start --mode dry-run --config config/trident_crypto_launch_fast_crypto_only.toml"
    echo "  ./deploy.sh --start --mode dry-run --config config/trident_crypto_launch_fast_crypto_only.toml --fresh-start"
    echo "  ./deploy.sh --mode live --config config/trident.toml"
    echo "  ./deploy.sh --start --mode live --network testnet --without-funding"
    echo "  ./deploy.sh --start --mode live --without-funding"
    echo "  ./deploy.sh --start --mode live --without-pod-c --without-funding"
    echo "  ./deploy.sh --start --without-pod-c"
    echo "  ./deploy.sh --start --without-funding"
    echo "  ./trident-hip4/deploy.sh --start"
fi
echo ""
