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
Usage: ./scripts/trident_server.sh <start|stop|restart|update|status|logs|health|ps> [--mode dry-run|live] [--config config/trident.toml] [--without-pod-b] [--without-pod-c] [--without-funding] [--without-hip4-outcome] [--without-hip4-mainnet-observer] [--fresh-start] [service]

Actions:
  start     démarre l'API + Pod A + Pod B HIP-4 testnet + observateur HIP-4 mainnet + Pod C + funding par défaut en dry-run
  stop      arrête les services sélectionnés
  restart   redémarre les services sélectionnés
  update    rebuild + redémarre les services sélectionnés
  status    affiche l'état docker compose
  logs      suit les logs (service optionnel)
  health    appelle /health local
  ps        alias de status

Compatibilité :
  --with-pod-b / --with-pod-c / --with-funding restent acceptés mais sont redondants.
  --with-hip4-outcome / --without-hip4-outcome sont des alias du Pod B HIP-4.
  --with-hip4-mainnet-observer / --without-hip4-mainnet-observer contrôle seulement l'observateur mainnet.

Sécurité live :
  --mode dry-run est le défaut. --mode live lance Pod A + Pod C par défaut,
  refuse Pod B HIP-4, puis lance un preflight credentials + reconciliation + orderUpdates.
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
ENABLE_HIP4_OUTCOME="${TRIDENT_ENABLE_HIP4_OUTCOME:-true}"
ENABLE_HIP4_MAINNET_OBSERVER="${TRIDENT_ENABLE_HIP4_MAINNET_OBSERVER:-true}"
FRESH_START=""
MODE="${TRIDENT_MODE:-dry-run}"
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
        --with-hip4-outcome)
            ENABLE_HIP4_OUTCOME="true"
            ENABLE_POD_B="true"
            shift
            ;;
        --with-hip4-mainnet-observer)
            ENABLE_HIP4_MAINNET_OBSERVER="true"
            ENABLE_POD_B="true"
            shift
            ;;
        --config)
            CONFIG_PATH="$2"
            shift 2
            ;;
        --mode)
            MODE="$2"
            shift 2
            ;;
        --without-pod-b)
            ENABLE_POD_B=""
            ENABLE_HIP4_OUTCOME=""
            ENABLE_HIP4_MAINNET_OBSERVER=""
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
        --without-hip4-outcome)
            ENABLE_HIP4_OUTCOME=""
            ENABLE_POD_B=""
            ENABLE_HIP4_MAINNET_OBSERVER=""
            shift
            ;;
        --without-hip4-mainnet-observer)
            ENABLE_HIP4_MAINNET_OBSERVER=""
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

if [ "$MODE" = "live" ]; then
    ENABLE_HIP4_OUTCOME=""
    ENABLE_HIP4_MAINNET_OBSERVER=""
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROFILE_ARGS=()
[ -n "$ENABLE_POD_B" ] && [ -n "$ENABLE_HIP4_OUTCOME" ] && PROFILE_ARGS+=(--profile pod_b)
[ -n "$ENABLE_POD_B" ] && [ -n "$ENABLE_HIP4_MAINNET_OBSERVER" ] && PROFILE_ARGS+=(--profile hip4_mainnet_observer)
[ -n "$ENABLE_POD_C" ] && PROFILE_ARGS+=(--profile pod_c)
[ -n "$ENABLE_FUNDING" ] && PROFILE_ARGS+=(--profile funding)

COMPOSE_ENV_ARGS=()
if [ -f ".env.trident" ]; then
    COMPOSE_ENV_ARGS+=(--env-file .env.trident)
fi

compose() {
    TRIDENT_ENABLE_POD_A="true" \
    TRIDENT_ENABLE_POD_B="" \
    TRIDENT_ENABLE_POD_C="${ENABLE_POD_C:+true}" \
    TRIDENT_ENABLE_HIP4_OUTCOME="${ENABLE_HIP4_OUTCOME:+true}" \
    TRIDENT_ENABLE_HIP4_MAINNET_OBSERVER="${ENABLE_HIP4_MAINNET_OBSERVER:+true}" \
    TRIDENT_MODE="${MODE}" \
    TRIDENT_CONFIG_PATH="${CONFIG_PATH}" \
    docker compose "${COMPOSE_ENV_ARGS[@]}" -f docker-compose.trident.yml "${PROFILE_ARGS[@]}" "$@"
}

compose_all() {
    docker compose "${COMPOSE_ENV_ARGS[@]}" -f docker-compose.trident.yml "$@"
}

default_services() {
    local services=(trident-api pod-a-live)
    if [ -n "$ENABLE_POD_B" ] && [ -n "$ENABLE_HIP4_OUTCOME" ]; then
        services+=(hip4-outcome-dry-run)
    fi
    if [ -n "$ENABLE_POD_B" ] && [ -n "$ENABLE_HIP4_MAINNET_OBSERVER" ]; then
        services+=(hip4-outcome-mainnet-observer)
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
        runtime/hip4_outcome_mainnet_state.json \
        runtime/hip4_outcome_mainnet_rate_limits.json \
        2>/dev/null || true
    rm -rf \
        logs/hip4_outcome \
        logs/hip4_outcome_paper \
        logs/hip4_outcome_testnet \
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
            /app/runtime/hip4_outcome_mainnet_state.json \
            /app/runtime/hip4_outcome_mainnet_rate_limits.json
        rm -rf \
            /app/logs/hip4_outcome \
            /app/logs/hip4_outcome_paper \
            /app/logs/hip4_outcome_testnet \
            /app/logs/hip4_outcome_mainnet
    ' >/dev/null
    ok "Artefacts live réinitialisés"
}

require_runtime_files() {
    mkdir -p logs data runtime
}

guard_live_start() {
    if [ "$MODE" != "live" ]; then
        return 0
    fi
    if [ -n "$ENABLE_POD_B" ]; then
        error "Mode live refuse: Pod B HIP-4 est réservé au dry-run/testnet. Relance avec --without-pod-b."
        exit 1
    fi

    local pod_a_state_path="${TRIDENT_LIVE_STATE_PATH_POD_A:-${TRIDENT_LIVE_STATE_PATH:-runtime/trident/live_state_pod_a.json}}"
    local pod_c_state_path="${TRIDENT_LIVE_STATE_PATH_POD_C:-runtime/trident/live_state_pod_c.json}"

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
        guard_live_start
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        if [ -n "$FRESH_START" ]; then
            fresh_start_cleanup
        else
            stop_unmanaged_services "${SERVICES[@]}"
        fi
        info "Démarrage: ${SERVICES[*]}"
        info "Mode: ${MODE}"
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
        guard_live_start
        mapfile -t SERVICES < <(default_services)
        info "Redémarrage: ${SERVICES[*]}"
        info "Mode: ${MODE}"
        compose restart "${SERVICES[@]}"
        ok "Services redémarrés"
        ;;
    update)
        guard_live_start
        require_runtime_files
        mapfile -t SERVICES < <(default_services)
        info "Rebuild + redémarrage: ${SERVICES[*]}"
        info "Mode: ${MODE}"
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
