#!/usr/bin/env bash
set -euo pipefail

HEALTH_URL="${TRIDENT_HEALTH_URL:-http://127.0.0.1:3000/health}"
curl -fsS "$HEALTH_URL"
