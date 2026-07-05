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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TRIDENT_ROOT="${TRIDENT_RETENTION_TRIDENT_ROOT:-/opt/trident}"
HIP4_ROOT="${TRIDENT_RETENTION_HIP4_ROOT:-/opt/trident-hip4}"
INSTALL_CRON=""
USE_DOCKER="${TRIDENT_RETENTION_USE_DOCKER:-auto}"
PY_ARGS=()

usage() {
    cat <<'EOF'
Usage: ./scripts/trident_disk_retention.sh [--apply] [--scope all|trident|hip4] [--install-cron] [retention options]

Runs conservative server-data retention:
- prunes old daily TRIDENT snapshots/features;
- prunes old runtime backup/archive directories;
- rotates huge HIP-4 observation logs into gzip archives;
- never deletes runtime state, configs, trades, settlements, or status files.

Default mode is dry-run. Use --apply to change files.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --install-cron)
            INSTALL_CRON="true"
            shift
            ;;
        --trident-root)
            TRIDENT_ROOT="$2"
            shift 2
            ;;
        --hip4-root)
            HIP4_ROOT="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            PY_ARGS+=("$1")
            shift
            ;;
    esac
done

install_cron() {
    if ! command -v crontab >/dev/null 2>&1; then
        warn "crontab absent; retention cron non installee"
        return 0
    fi
    local cron_cmd cron_line current
    cron_cmd="cd ${REPO_ROOT} && TRIDENT_RETENTION_USE_DOCKER=auto TRIDENT_RETENTION_TRIDENT_ROOT=${TRIDENT_ROOT} TRIDENT_RETENTION_HIP4_ROOT=${HIP4_ROOT} ./scripts/trident_disk_retention.sh --scope all --apply >> logs/retention_cron.log 2>&1"
    cron_line="17 3 * * * ${cron_cmd} # trident-disk-retention"
    current="$(mktemp)"
    crontab -l 2>/dev/null | grep -v 'trident-disk-retention' > "${current}" || true
    printf '%s\n' "${cron_line}" >> "${current}"
    crontab "${current}"
    rm -f "${current}"
    ok "Retention cron installee: ${cron_line}"
}

select_image() {
    local image
    for image in trident-hip4-hip4-api trident-trident-api; do
        if docker image inspect "${image}" >/dev/null 2>&1 \
            && docker run --rm --entrypoint test "${image}" -f /app/scripts/trident_disk_retention.py >/dev/null 2>&1; then
            printf '%s\n' "${image}"
            return 0
        fi
    done
    return 1
}

run_python_host() {
    python3 "${SCRIPT_DIR}/trident_disk_retention.py" \
        --trident-root "${TRIDENT_ROOT}" \
        --hip4-root "${HIP4_ROOT}" \
        "${PY_ARGS[@]}"
}

run_python_docker() {
    local image
    image="$(select_image)" || return 1
    local volume_args=(-v "${TRIDENT_ROOT}:/mnt/trident")
    if [ -d "${HIP4_ROOT}" ]; then
        volume_args+=(-v "${HIP4_ROOT}:/mnt/trident-hip4")
    fi
    docker run --rm "${volume_args[@]}" "${image}" \
        python /app/scripts/trident_disk_retention.py \
        --trident-root /mnt/trident \
        --hip4-root /mnt/trident-hip4 \
        "${PY_ARGS[@]}"
}

mkdir -p "${REPO_ROOT}/logs"

if [ -n "${INSTALL_CRON}" ]; then
    install_cron
fi

case "${USE_DOCKER}" in
    0|false|no|off)
        run_python_host
        ;;
    1|true|yes|on)
        run_python_docker
        ;;
    auto)
        if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && run_python_docker; then
            :
        else
            warn "Retention Docker indisponible; fallback host python"
            run_python_host
        fi
        ;;
    *)
        warn "TRIDENT_RETENTION_USE_DOCKER invalide (${USE_DOCKER}); fallback auto"
        if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && run_python_docker; then
            :
        else
            run_python_host
        fi
        ;;
esac
