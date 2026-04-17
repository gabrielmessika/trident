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
Usage: ./scripts/trident_server.sh <start|stop|restart|update|status|logs|health|ps> [--config config/trident.toml] [--without-pod-b] [--without-pod-c] [--without-funding] [--fresh-start] [service]

Actions:
  start     démarre l'API + Pod A + Pod B + Pod C + funding par défaut
  stop      arrête les services sélectionnés
  restart   redémarre les services sélectionnés
  update    rebuild + redémarre les services sélectionnés
  status    affiche l'état docker compose
  logs      suit les logs (service optionnel)
  health    appelle /health local
  ps        alias de status

Compatibilité :
  --with-pod-b / --with-pod-c / --with-funding restent acceptés mais sont redondants.
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

ENABLE_POD_B="true"
ENABLE_POD_C="true"
ENABLE_FUNDING="true"
FRESH_START=""
CONFIG_PATH="${TRIDENT_CONFIG_PATH:-config/trident.toml}"
SERVICE_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --with-pod-b)
            ENABLE_POD_B="true"
            shift
            ;;
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
            shift 2
            ;;
        --without-pod-b)
            ENABLE_POD_B=""
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

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROFILE_ARGS=()
[ -n "$ENABLE_POD_B" ] && PROFILE_ARGS+=(--profile pod_b)
[ -n "$ENABLE_POD_C" ] && PROFILE_ARGS+=(--profile pod_c)
[ -n "$ENABLE_FUNDING" ] && PROFILE_ARGS+=(--profile funding)

compose() {
    TRIDENT_ENABLE_POD_A="true" \
    TRIDENT_ENABLE_POD_B="${ENABLE_POD_B:+true}" \
    TRIDENT_ENABLE_POD_C="${ENABLE_POD_C:+true}" \
    TRIDENT_CONFIG_PATH="${CONFIG_PATH}" \
    docker compose -f docker-compose.trident.yml "${PROFILE_ARGS[@]}" "$@"
}

compose_all() {
    docker compose -f docker-compose.trident.yml "$@"
}

default_services() {
    local services=(trident-api pod-a-live)
    if [ -n "$ENABLE_POD_B" ]; then
        services+=(pod-b-live)
    fi
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
        logs/pod_a_live_report.json \
        logs/pod_b_live_report.json \
        logs/pod_c_live_report.json
    ok "Artefacts live réinitialisés"
}

require_runtime_files() {
    mkdir -p logs data runtime
}

case "$ACTION" in
    start)
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        if [ -n "$FRESH_START" ]; then
            fresh_start_cleanup
        else
            stop_unmanaged_services "${SERVICES[@]}"
        fi
        info "Démarrage: ${SERVICES[*]}"
        info "Config: ${CONFIG_PATH}"
        compose up -d --force-recreate "${SERVICES[@]}"
        ok "Services démarrés"
        ;;
    stop)
        mapfile -t SERVICES < <(default_services)
        info "Arrêt: ${SERVICES[*]}"
        compose stop "${SERVICES[@]}"
        ok "Services arrêtés"
        ;;
    restart)
        mapfile -t SERVICES < <(default_services)
        info "Redémarrage: ${SERVICES[*]}"
        compose restart "${SERVICES[@]}"
        ok "Services redémarrés"
        ;;
    update)
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        info "Rebuild + redémarrage: ${SERVICES[*]}"
        info "Config: ${CONFIG_PATH}"
        compose build
        compose up -d "${SERVICES[@]}"
        ok "Update terminé"
        ;;
    status|ps)
        compose ps
        ;;
    logs)
        if [ -n "$SERVICE_ARG" ]; then
            compose logs -f --tail=200 "$SERVICE_ARG"
        else
            compose logs -f --tail=200
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
