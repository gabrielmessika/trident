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
Usage: ./scripts/trident_server.sh <start|stop|restart|update|status|logs|health|ps> [--without-pod-b] [--without-pod-c] [--without-funding] [service]

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
    docker compose -f docker-compose.trident.yml "${PROFILE_ARGS[@]}" "$@"
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

require_runtime_files() {
    mkdir -p logs data runtime
}

case "$ACTION" in
    start)
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        info "Démarrage: ${SERVICES[*]}"
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
