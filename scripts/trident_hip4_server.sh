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
Usage: ./scripts/trident_hip4_server.sh <start|stop|restart|update|status|logs|health|ps> [--mode paper|observer|testnet] [--config config/hip4_outcome_mainnet_paper.toml] [--api-port 3001] [--with-mainnet-observer] [--without-mainnet-observer] [--fresh-start] [service]

Actions:
  start     démarre l'API HIP-4 + le runner HIP-4 outcome paper
  stop      arrête les services HIP-4
  restart   redémarre les services HIP-4
  update    rebuild + redémarre les services HIP-4
  status    affiche l'état docker compose
  logs      suit les logs (service optionnel)
  health    appelle /health local sur le port HIP-4
  ps        alias de status

Notes:
  Cette app est séparée de TRIDENT A/C. Elle ne démarre pas Pod A ni Pod C.
  Le mode par défaut est `paper`; aucun ordre mainnet réel HIP-4 n'est lancé par défaut.
  L'observer mainnet standalone est lancé par défaut en paper; utilisez
  --without-mainnet-observer pour le désactiver explicitement.
EOF
}

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

ACTION="$1"
shift

REMOTE_HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
REMOTE_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
REMOTE_IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
REMOTE_DIR="${TRIDENT_HIP4_DEPLOY_DIR:-${TRIDENT_DEPLOY_DIR:-/opt/trident-hip4}}"
HIP4_MODE="${HIP4_OUTCOME_MODE:-paper}"
HIP4_CONFIG="${HIP4_OUTCOME_CONFIG:-config/hip4_outcome_mainnet_paper.toml}"
HIP4_MAINNET_CONFIG="${HIP4_OUTCOME_MAINNET_CONFIG:-config/hip4_outcome_mainnet_observer.toml}"
HIP4_ALLOW_TESTNET_ORDERS="${HIP4_OUTCOME_ALLOW_TESTNET_ORDERS:-false}"
HIP4_API_PORT="${HIP4_OUTCOME_API_PORT:-3001}"
ENABLE_MAINNET_OBSERVER="${TRIDENT_HIP4_ENABLE_MAINNET_OBSERVER:-true}"
FRESH_START=""
SERVICE_ARG=""

mainnet_observer_enabled() {
    case "${ENABLE_MAINNET_OBSERVER,,}" in
        1|true|yes|on)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)
            HIP4_MODE="$2"
            shift 2
            ;;
        --config)
            HIP4_CONFIG="$2"
            shift 2
            ;;
        --mainnet-observer-config)
            HIP4_MAINNET_CONFIG="$2"
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
            ENABLE_MAINNET_OBSERVER="false"
            shift
            ;;
        --allow-testnet-orders)
            HIP4_ALLOW_TESTNET_ORDERS="true"
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
            SERVICE_ARG="$1"
            shift
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

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROFILE_ARGS=()
mainnet_observer_enabled && PROFILE_ARGS+=(--profile mainnet_observer)

COMPOSE_ENV_ARGS=()
if [ -f ".env.trident-hip4" ]; then
    COMPOSE_ENV_ARGS+=(--env-file .env.trident-hip4)
fi

compose() {
    COMPOSE_PROJECT_NAME="trident-hip4" \
    HIP4_OUTCOME_MODE="${HIP4_MODE}" \
    HIP4_OUTCOME_CONFIG="${HIP4_CONFIG}" \
    HIP4_OUTCOME_MAINNET_CONFIG="${HIP4_MAINNET_CONFIG}" \
    HIP4_OUTCOME_ALLOW_TESTNET_ORDERS="${HIP4_ALLOW_TESTNET_ORDERS}" \
    HIP4_OUTCOME_API_PORT="${HIP4_API_PORT}" \
    TRIDENT_CONFIG_PATH="${TRIDENT_CONFIG_PATH:-config/trident.toml}" \
    docker compose "${COMPOSE_ENV_ARGS[@]}" -f docker-compose.hip4.yml "${PROFILE_ARGS[@]}" "$@"
}

compose_all() {
    COMPOSE_PROJECT_NAME="trident-hip4" \
    docker compose "${COMPOSE_ENV_ARGS[@]}" -f docker-compose.hip4.yml "$@"
}

ssh_remote() {
    local ssh_args=()
    if [ -f "${REMOTE_IDENTITY_FILE}" ]; then
        ssh_args+=(-i "${REMOTE_IDENTITY_FILE}")
    fi
    ssh_args+=(-o BatchMode=yes -o ConnectTimeout=10)
    ssh "${ssh_args[@]}" "${REMOTE_USER}@${REMOTE_HOST}" "$@"
}

remote_compose() {
    local remote_dir_q
    local args=()
    local arg quoted
    printf -v remote_dir_q '%q' "${REMOTE_DIR}"
    for arg in -f docker-compose.hip4.yml "${PROFILE_ARGS[@]}" "$@"; do
        printf -v quoted '%q' "${arg}"
        args+=("${quoted}")
    done
    warn "Docker absent localement; lecture distante sur ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}."
    ssh_remote "cd ${remote_dir_q} && COMPOSE_PROJECT_NAME=trident-hip4 docker compose ${args[*]}"
}

read_only_compose() {
    if command -v docker >/dev/null 2>&1; then
        compose "$@"
        return
    fi
    remote_compose "$@"
}

default_services() {
    local services=(hip4-api hip4-outcome-paper)
    mainnet_observer_enabled && services+=(hip4-mainnet-observer)
    printf '%s\n' "${services[@]}"
}

all_managed_services() {
    printf '%s\n' hip4-api hip4-outcome-paper hip4-mainnet-observer
}

require_runtime_files() {
    mkdir -p logs data runtime
}

fresh_start_cleanup() {
    info "Fresh start HIP-4: arrêt complet et purge des artefacts HIP-4..."
    mapfile -t ALL_SERVICES < <(all_managed_services)
    compose_all stop "${ALL_SERVICES[@]}" >/dev/null 2>&1 || true
    rm -f \
        logs/pod_b_live_status.json \
        logs/hip4_outcome_status.json \
        logs/hip4_outcome_mainnet_status.json \
        runtime/hip4_outcome_state.json \
        runtime/hip4_outcome_paper_state.json \
        runtime/hip4_outcome_testnet_state.json \
        runtime/hip4_outcome_mainnet_paper_state.json \
        runtime/hip4_outcome_mainnet_paper_rate_limits.json \
        runtime/hip4_outcome_mainnet_state.json \
        runtime/hip4_outcome_mainnet_rate_limits.json \
        2>/dev/null || true
    rm -rf \
        logs/hip4_outcome \
        logs/hip4_outcome_paper \
        logs/hip4_outcome_testnet \
        logs/hip4_outcome_mainnet_paper \
        logs/hip4_outcome_mainnet \
        2>/dev/null || true
    ok "Artefacts HIP-4 réinitialisés"
}

write_deployment_profile() {
    require_runtime_files
    python3 - \
        "$HIP4_MODE" \
        "$HIP4_CONFIG" \
        "$HIP4_MAINNET_CONFIG" \
        "$HIP4_API_PORT" \
        "$ENABLE_MAINNET_OBSERVER" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def flag(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


mode, config_path, mainnet_config_path, api_port, enable_mainnet_observer = sys.argv[1:6]
services = ["hip4-api", "hip4-outcome-paper"]
if flag(enable_mainnet_observer):
    services.append("hip4-mainnet-observer")

payload = {
    "app": "trident-hip4",
    "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "mode": mode,
    "config_path": config_path,
    "mainnet_observer_config_path": mainnet_config_path,
    "api_port": int(api_port),
    "mainnet_observer_enabled": flag(enable_mainnet_observer),
    "selected_services": services,
}
Path("logs/trident_hip4_deployment_profile.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

case "$ACTION" in
    start)
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        if [ -n "$FRESH_START" ]; then
            fresh_start_cleanup
        fi
        write_deployment_profile
        info "Démarrage HIP-4: ${SERVICES[*]}"
        info "Mode HIP-4: ${HIP4_MODE}"
        info "Config HIP-4: ${HIP4_CONFIG}"
        info "Port API HIP-4: ${HIP4_API_PORT}"
        compose up -d --force-recreate --remove-orphans "${SERVICES[@]}"
        ok "Services HIP-4 démarrés"
        ;;
    stop)
        mapfile -t SERVICES < <(default_services)
        info "Arrêt HIP-4: ${SERVICES[*]}"
        compose stop "${SERVICES[@]}"
        ok "Services HIP-4 arrêtés"
        ;;
    restart)
        mapfile -t SERVICES < <(default_services)
        write_deployment_profile
        info "Redémarrage HIP-4: ${SERVICES[*]}"
        compose restart "${SERVICES[@]}"
        ok "Services HIP-4 redémarrés"
        ;;
    update)
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        info "Rebuild + redémarrage HIP-4: ${SERVICES[*]}"
        compose build
        write_deployment_profile
        compose up -d --remove-orphans "${SERVICES[@]}"
        ok "Update HIP-4 terminé"
        ;;
    status|ps)
        read_only_compose ps
        ;;
    logs)
        if [ -n "$SERVICE_ARG" ]; then
            read_only_compose logs -f --tail=200 "$SERVICE_ARG"
        else
            read_only_compose logs -f --tail=200
        fi
        ;;
    health)
        curl -fsS "http://127.0.0.1:${HIP4_API_PORT}/health"
        ;;
    *)
        error "Action inconnue: $ACTION"
        usage
        exit 1
        ;;
esac
