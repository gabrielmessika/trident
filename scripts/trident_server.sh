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
Usage: ./scripts/trident_server.sh <start|stop|restart|update|status|logs|health|ps> [--mode dry-run|live] [--network mainnet|testnet] [--config config/trident.toml] [--without-pod-c] [--without-funding] [--fresh-start] [service]

Actions:
  start     démarre l'API + Pod A + Pod C + funding par défaut en dry-run
  stop      arrête les services sélectionnés
  restart   redémarre les services sélectionnés
  update    rebuild + redémarre les services sélectionnés
  status    affiche l'état docker compose
  logs      suit les logs (service optionnel)
  health    appelle /health local
  ps        alias de status

Compatibilité :
  --with-pod-c / --with-funding restent acceptés mais sont redondants.
  Pod B HIP-4 est maintenant géré par l'app séparée `trident-hip4`.

Sécurité live :
  --mode dry-run est le défaut. --mode live lance Pod A + Pod C en vrais
  ordres sur le réseau A/C choisi, puis lance un preflight credentials +
  reconciliation + orderUpdates pour Pod A/Pod C.
  --network testnet sélectionne config/trident_testnet.toml par défaut et
  isole les state files live Pod A/Pod C du mainnet.
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
REMOTE_DIR="${TRIDENT_DEPLOY_DIR:-/opt/trident}"
ENABLE_POD_C="true"
ENABLE_FUNDING="true"
FRESH_START=""
MODE="${TRIDENT_MODE:-dry-run}"
EXCHANGE_NETWORK="${TRIDENT_EXCHANGE_NETWORK:-mainnet}"
CONFIG_PATH_EXPLICIT="${TRIDENT_CONFIG_PATH:+true}"
CONFIG_PATH="${TRIDENT_CONFIG_PATH:-config/trident.toml}"
SERVICE_ARG=""

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

pod_a_live_state_path() {
    if [ "$EXCHANGE_NETWORK" = "testnet" ]; then
        printf '%s' "${TRIDENT_LIVE_STATE_PATH_POD_A:-runtime/trident/live_state_testnet_pod_a.json}"
        return
    fi
    local global_path="${TRIDENT_LIVE_STATE_PATH:-runtime/trident/live_state_pod_a.json}"
    printf '%s' "${TRIDENT_LIVE_STATE_PATH_POD_A:-$global_path}"
}

pod_c_live_state_path() {
    if [ "$EXCHANGE_NETWORK" = "testnet" ]; then
        printf '%s' "${TRIDENT_LIVE_STATE_PATH_POD_C:-runtime/trident/live_state_testnet_pod_c.json}"
        return
    fi
    printf '%s' "${TRIDENT_LIVE_STATE_PATH_POD_C:-runtime/trident/live_state_pod_c.json}"
}

guard_network_config() {
    if [ "$EXCHANGE_NETWORK" != "testnet" ]; then
        return 0
    fi
    if [ ! -f "$CONFIG_PATH" ]; then
        error "Config testnet introuvable: ${CONFIG_PATH}"
        exit 1
    fi
    if ! grep -q "hyperliquid-testnet" "$CONFIG_PATH"; then
        error "Réseau testnet demandé mais ${CONFIG_PATH} ne contient pas d'endpoint hyperliquid-testnet."
        exit 1
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --with-pod-c)
            ENABLE_POD_C="true"
            shift
            ;;
        --with-funding)
            ENABLE_FUNDING="true"
            shift
            ;;
        --config)
            CONFIG_PATH="$2"
            CONFIG_PATH_EXPLICIT="true"
            shift 2
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
        --without-pod-c)
            ENABLE_POD_C=""
            shift
            ;;
        --without-funding)
            ENABLE_FUNDING=""
            shift
            ;;
        --with-pod-b|--without-pod-b|--only-pod-b|--pod-b-only|--with-hip4-outcome|--without-hip4-outcome|--with-hip4-mainnet-observer|--without-hip4-mainnet-observer)
            error "Pod B/HIP-4 a été séparé. Utilise ./trident-hip4/deploy.sh ou ./scripts/trident_hip4_server.sh."
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
            SERVICE_ARG="$1"
            shift
            ;;
    esac
done

case "$MODE" in
    dry-run|live)
        ;;
    observation)
        warn "Mode observation accepté pour compatibilité; préfère --mode dry-run pour les déploiements préparatoires."
        ;;
    *)
        error "Mode invalide: ${MODE}. Valeurs attendues: dry-run ou live."
        exit 1
        ;;
esac

resolve_network_config

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROFILE_ARGS=()
[ -n "$ENABLE_POD_C" ] && PROFILE_ARGS+=(--profile pod_c)
[ -n "$ENABLE_FUNDING" ] && PROFILE_ARGS+=(--profile funding)

COMPOSE_ENV_ARGS=()
if [ -f ".env.trident" ]; then
    COMPOSE_ENV_ARGS+=(--env-file .env.trident)
fi

compose() {
    local funding_enabled="false"
    local pod_a_state_path
    local pod_c_state_path
    [ -n "$ENABLE_FUNDING" ] && funding_enabled="true"
    pod_a_state_path="$(pod_a_live_state_path)"
    pod_c_state_path="$(pod_c_live_state_path)"
    TRIDENT_ENABLE_POD_A="true" \
    TRIDENT_ENABLE_POD_B="false" \
    TRIDENT_ENABLE_POD_C="${ENABLE_POD_C:+true}" \
    TRIDENT_ENABLE_HIP4_OUTCOME="false" \
    TRIDENT_ENABLE_HIP4_MAINNET_OBSERVER="" \
    TRIDENT_ENABLE_FUNDING="${funding_enabled}" \
    TRIDENT_MODE="${MODE}" \
    TRIDENT_EXCHANGE_NETWORK="${EXCHANGE_NETWORK}" \
    TRIDENT_CONFIG_PATH="${CONFIG_PATH}" \
    TRIDENT_LIVE_STATE_PATH_POD_A="${pod_a_state_path}" \
    TRIDENT_LIVE_STATE_PATH_POD_C="${pod_c_state_path}" \
    docker compose "${COMPOSE_ENV_ARGS[@]}" -f docker-compose.trident.yml "${PROFILE_ARGS[@]}" "$@"
}

compose_all() {
    docker compose "${COMPOSE_ENV_ARGS[@]}" -f docker-compose.trident.yml "$@"
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
    for arg in -f docker-compose.trident.yml "${PROFILE_ARGS[@]}" "$@"; do
        printf -v quoted '%q' "${arg}"
        args+=("${quoted}")
    done
    warn "Docker absent localement; lecture distante sur ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}."
    ssh_remote "cd ${remote_dir_q} && docker compose ${args[*]}"
}

read_only_compose() {
    if command -v docker >/dev/null 2>&1; then
        compose "$@"
        return
    fi
    remote_compose "$@"
}

default_services() {
    local services=(trident-api pod-a-live)
    if [ -n "$ENABLE_POD_C" ]; then
        services+=(pod-c-live tradfi-funding-collector)
    fi
    if [ -n "$ENABLE_FUNDING" ]; then
        services+=(funding-collector)
    fi
    printf '%s\n' "${services[@]}"
}

all_managed_services() {
    printf '%s\n' \
        trident-api \
        pod-a-live \
        pod-b-live \
        pod-c-live \
        hip4-outcome-dry-run \
        hip4-outcome-mainnet-observer \
        tradfi-funding-collector \
        funding-collector
}

stop_unmanaged_services() {
    local selected=("$@")
    mapfile -t ALL_SERVICES < <(all_managed_services)
    local stop_services=()
    local service
    local keep
    for service in "${ALL_SERVICES[@]}"; do
        keep=""
        for selected_service in "${selected[@]}"; do
            if [ "$service" = "$selected_service" ]; then
                keep="true"
                break
            fi
        done
        [ -z "$keep" ] && stop_services+=("$service")
    done
    if [ ${#stop_services[@]} -gt 0 ]; then
        info "Arrêt des services hors profil: ${stop_services[*]}"
        compose_all stop "${stop_services[@]}" >/dev/null 2>&1 || true
    fi
}

stop_legacy_split_services() {
    if ! command -v docker >/dev/null 2>&1; then
        return 0
    fi
    docker stop \
        trident-pod-b-live \
        trident-hip4-outcome-dry-run \
        trident-hip4-outcome-mainnet-observer \
        >/dev/null 2>&1 || true
    docker rm \
        trident-pod-b-live \
        trident-hip4-outcome-dry-run \
        trident-hip4-outcome-mainnet-observer \
        >/dev/null 2>&1 || true
}

fresh_start_cleanup() {
    info "Fresh start: arrêt complet et purge des artefacts live..."
    mapfile -t ALL_SERVICES < <(all_managed_services)
    compose_all stop "${ALL_SERVICES[@]}" >/dev/null 2>&1 || true
    rm -f \
        logs/pod_a_live.jsonl \
        logs/pod_b_live.jsonl \
        logs/pod_c_live.jsonl \
        logs/pod_a_live_status.json \
        logs/pod_b_live_status.json \
        logs/pod_c_live_status.json \
        logs/hip4_outcome_status.json \
        logs/hip4_outcome_mainnet_status.json \
        logs/pod_a_live_report.json \
        logs/pod_b_live_report.json \
        logs/pod_c_live_report.json \
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
    compose_all run --rm --no-deps --entrypoint sh trident-api -c '
        rm -f \
            /app/logs/pod_a_live.jsonl \
            /app/logs/pod_b_live.jsonl \
            /app/logs/pod_c_live.jsonl \
            /app/logs/pod_a_live_status.json \
            /app/logs/pod_b_live_status.json \
            /app/logs/pod_c_live_status.json \
            /app/logs/hip4_outcome_status.json \
            /app/logs/hip4_outcome_mainnet_status.json \
            /app/logs/pod_a_live_report.json \
            /app/logs/pod_b_live_report.json \
            /app/logs/pod_c_live_report.json \
            /app/runtime/hip4_outcome_state.json \
            /app/runtime/hip4_outcome_paper_state.json \
            /app/runtime/hip4_outcome_testnet_state.json \
            /app/runtime/hip4_outcome_mainnet_paper_state.json \
            /app/runtime/hip4_outcome_mainnet_paper_rate_limits.json \
            /app/runtime/hip4_outcome_mainnet_state.json \
            /app/runtime/hip4_outcome_mainnet_rate_limits.json
        rm -rf \
            /app/logs/hip4_outcome \
            /app/logs/hip4_outcome_paper \
            /app/logs/hip4_outcome_testnet \
            /app/logs/hip4_outcome_mainnet_paper \
            /app/logs/hip4_outcome_mainnet
    ' >/dev/null
    ok "Artefacts live réinitialisés"
}

require_runtime_files() {
    mkdir -p logs data runtime
}

write_deployment_profile() {
    require_runtime_files
    python3 - \
        "$MODE" \
        "$EXCHANGE_NETWORK" \
        "$CONFIG_PATH" \
        "$ENABLE_POD_C" \
        "$ENABLE_FUNDING" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def flag(raw: str) -> bool:
    return bool(raw.strip())


mode, network, config_path = sys.argv[1:4]
enable_pod_c, enable_funding = sys.argv[4:6]
services = ["trident-api", "pod-a-live"]
if flag(enable_pod_c):
    services.extend(["pod-c-live", "tradfi-funding-collector"])
if flag(enable_funding):
    services.append("funding-collector")

payload = {
    "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "mode": mode,
    "exchange_network": network,
    "config_path": config_path,
    "app": "trident",
    "pod_b_enabled": False,
    "pod_c_enabled": flag(enable_pod_c),
    "hip4_outcome_enabled": False,
    "hip4_mainnet_observer_enabled": False,
    "funding_collector_enabled": flag(enable_funding),
    "selected_services": services,
}
Path("logs/trident_deployment_profile.json").write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

guard_live_start() {
    if [ "$MODE" != "live" ]; then
        return 0
    fi
    if [ "$EXCHANGE_NETWORK" = "testnet" ]; then
        info "Mode live testnet: Pod A/Pod C placeront de vrais ordres sur Hyperliquid testnet, sans ordre mainnet A/C."
    else
        info "Mode live mainnet: Pod A/Pod C placeront de vrais ordres sur Hyperliquid mainnet."
    fi

    local pod_a_state_path
    local pod_c_state_path
    pod_a_state_path="$(pod_a_live_state_path)"
    pod_c_state_path="$(pod_c_live_state_path)"

    info "Preflight live Pod A: credentials, reconciliation exchange, websocket orderUpdates..."
    local pod_a_args=(
        python -m app.live.preflight
        --config "${CONFIG_PATH}"
        --pod pod_a
        --state-path "${pod_a_state_path}"
    )
    if [ -n "$ENABLE_POD_C" ]; then
        pod_a_args+=(--external-state-path "${pod_c_state_path}")
    fi
    if ! compose run --rm --no-deps trident-api "${pod_a_args[@]}"; then
        error "Preflight live Pod A refuse. Aucun service live ne sera demarre."
        exit 1
    fi

    if [ -n "$ENABLE_POD_C" ]; then
        info "Preflight live Pod C: credentials, reconciliation exchange, websocket orderUpdates..."
        if ! compose run --rm --no-deps trident-api \
            python -m app.live.preflight \
                --config "${CONFIG_PATH}" \
                --pod pod_c \
                --state-path "${pod_c_state_path}" \
                --external-state-path "${pod_a_state_path}"; then
            error "Preflight live Pod C refuse. Aucun service live ne sera demarre."
            exit 1
        fi
    fi
    ok "Preflight live OK"
}

case "$ACTION" in
    start)
        guard_network_config
        guard_live_start
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        if [ -n "$FRESH_START" ]; then
            fresh_start_cleanup
        else
            stop_unmanaged_services "${SERVICES[@]}"
        fi
        stop_legacy_split_services
        write_deployment_profile
        info "Démarrage: ${SERVICES[*]}"
        info "Mode: ${MODE}"
        info "Réseau A/C: ${EXCHANGE_NETWORK}"
        info "Config: ${CONFIG_PATH}"
        if [ "$MODE" = "live" ]; then
            info "State Pod A: $(pod_a_live_state_path)"
            [ -n "$ENABLE_POD_C" ] && info "State Pod C: $(pod_c_live_state_path)"
        fi
        compose up -d --force-recreate --remove-orphans "${SERVICES[@]}"
        ok "Services démarrés"
        ;;
    stop)
        mapfile -t SERVICES < <(default_services)
        info "Arrêt: ${SERVICES[*]}"
        compose stop "${SERVICES[@]}"
        ok "Services arrêtés"
        ;;
    restart)
        guard_network_config
        guard_live_start
        mapfile -t SERVICES < <(default_services)
        write_deployment_profile
        info "Redémarrage: ${SERVICES[*]}"
        info "Mode: ${MODE}"
        info "Réseau A/C: ${EXCHANGE_NETWORK}"
        compose restart "${SERVICES[@]}"
        ok "Services redémarrés"
        ;;
    update)
        guard_network_config
        guard_live_start
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        info "Rebuild + redémarrage: ${SERVICES[*]}"
        info "Mode: ${MODE}"
        info "Réseau A/C: ${EXCHANGE_NETWORK}"
        info "Config: ${CONFIG_PATH}"
        compose build
        write_deployment_profile
        stop_legacy_split_services
        compose up -d --remove-orphans "${SERVICES[@]}"
        ok "Update terminé"
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
        ./scripts/trident_healthcheck.sh
        ;;
    *)
        error "Action inconnue: $ACTION"
        usage
        exit 1
        ;;
esac
