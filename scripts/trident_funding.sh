#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./scripts/trident_funding.sh <start|stop|restart|status|logs>

Pilote uniquement le service Docker `funding-collector`.
EOF
}

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

ACTION="$1"
shift || true

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

compose() {
    docker compose -f docker-compose.trident.yml --profile funding "$@"
}

case "$ACTION" in
    start)
        compose up -d funding-collector
        ;;
    stop)
        compose stop funding-collector
        ;;
    restart)
        compose restart funding-collector
        ;;
    status)
        compose ps funding-collector
        ;;
    logs)
        compose logs -f --tail=200 funding-collector
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage
        exit 1
        ;;
esac
