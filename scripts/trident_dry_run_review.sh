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
Usage: ./scripts/trident_dry_run_review.sh [options]

Collecte les artefacts d'un dry-run TRIDENT depuis le serveur, applique
des checks deterministes quand c'est possible, puis genere des prompts LLM
pour les points qui demandent un jugement qualitatif.

Options:
  --host <host>                 Host SSH. Defaut: trident-hetzner
  --user <user>                 User SSH. Defaut: trident-deploy
  --identity <path>             Cle SSH. Defaut: ~/.ssh/trident_hetzner_ed25519
  --remote-dir <path>           Repertoire TRIDENT sur le serveur. Defaut: /opt/trident
  --local-dir <path>            Reutilise les artefacts deja rapatries localement
  --output-dir <path>           Repertoire local de sortie. Defaut: runtime/reviews/<timestamp>
  --snapshot-max-age-minutes N  Age max du dernier snapshot pour etre considere frais. Defaut: 15
  --log-lines N                 Nombre de lignes de logs a recuperer par service. Defaut: 120
  -h, --help                    Affiche cette aide
EOF
}

HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
SSH_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
REMOTE_DIR="${TRIDENT_DEPLOY_DIR:-/opt/trident}"
SNAPSHOT_MAX_AGE_MINUTES=15
LOG_LINES=120
TIMESTAMP_UTC="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_DIR=""
LOCAL_DIR=""

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
        --remote-dir)
            REMOTE_DIR="$2"
            shift 2
            ;;
        --local-dir)
            LOCAL_DIR="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --snapshot-max-age-minutes)
            SNAPSHOT_MAX_AGE_MINUTES="$2"
            shift 2
            ;;
        --log-lines)
            LOG_LINES="$2"
            shift 2
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-${ROOT_DIR}/runtime/reviews/${TIMESTAMP_UTC}}"
RAW_DIR="${OUTPUT_DIR}/raw"
mkdir -p "${RAW_DIR}"
HIP4_RUN_REVIEW_JSON="${OUTPUT_DIR}/hip4_outcome_run_review.json"
HIP4_RUN_REVIEW_MD="${OUTPUT_DIR}/hip4_outcome_run_review.md"
HIP4_RUN_REVIEW_STDOUT="${RAW_DIR}/hip4_outcome_run_review_stdout.json"
HIP4_RUN_REVIEW_STDERR="${RAW_DIR}/hip4_outcome_run_review_stderr.txt"
HIP4_RUN_REVIEW_STATUS="${RAW_DIR}/hip4_outcome_run_review_status.txt"

SSH_ARGS=()
if [ -f "${IDENTITY_FILE}" ]; then
    SSH_ARGS+=(-i "${IDENTITY_FILE}")
else
    warn "Cle SSH absente: ${IDENTITY_FILE}. Utilisation de la config SSH systeme uniquement."
fi

SSH_TARGET="${SSH_USER}@${HOST}"

ssh_remote() {
    ssh "${SSH_ARGS[@]}" "${SSH_TARGET}" "$@"
}

latest_local_file() {
    local pattern="$1"
    python3 - "$pattern" <<'PY'
from pathlib import Path
import glob
import sys

pattern = sys.argv[1]
matches = sorted(Path(path) for path in glob.glob(pattern))
print(matches[-1] if matches else "")
PY
}

copy_if_exists() {
    local src="$1"
    local dst="$2"
    if [ -n "${src}" ] && [ -f "${src}" ]; then
        cp "${src}" "${dst}"
        return 0
    fi
    return 1
}

capture_local_review_inputs() {
    local base="$1"
    local latest_health latest_state latest_metrics latest_report latest_hip4 latest_hip4_mainnet
    local api_dir="${base}/api"
    local snapshot_dir="${base}/live_snapshots"
    local log_dir="${base}/logs"
    local runtime_dir="${base}/runtime"
    local docker_dir="${base}/docker"

    latest_health="$(latest_local_file "${api_dir}/health-*.json")"
    latest_state="$(latest_local_file "${api_dir}/state-*.json")"
    latest_metrics="$(latest_local_file "${api_dir}/metrics-*.json")"
    latest_report="$(latest_local_file "${api_dir}/report-*.json")"
    latest_hip4="$(latest_local_file "${api_dir}/hip4-outcome-20*.json")"
    latest_hip4_mainnet="$(latest_local_file "${api_dir}/hip4-outcome-mainnet-*.json")"

    copy_if_exists "${latest_health}" "${RAW_DIR}/health.json" || : > "${RAW_DIR}/health.json"
    copy_if_exists "${latest_state}" "${RAW_DIR}/state.json" || : > "${RAW_DIR}/state.json"
    copy_if_exists "${latest_metrics}" "${RAW_DIR}/metrics.json" || : > "${RAW_DIR}/metrics.json"
    copy_if_exists "${latest_report}" "${RAW_DIR}/report.json" || : > "${RAW_DIR}/report.json"
    copy_if_exists "${latest_hip4}" "${RAW_DIR}/hip4_outcome.json" || : > "${RAW_DIR}/hip4_outcome.json"
    copy_if_exists "${latest_hip4_mainnet}" "${RAW_DIR}/hip4_outcome_mainnet.json" || : > "${RAW_DIR}/hip4_outcome_mainnet.json"

    python3 - "${snapshot_dir}" "${RAW_DIR}/snapshot_files.txt" <<'PY'
from pathlib import Path
from datetime import datetime, timezone
import sys

snapshot_dir = Path(sys.argv[1])
outfile = Path(sys.argv[2])
lines = []
for path in sorted(snapshot_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
    stat = path.stat()
    ts = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"{stat.st_mtime}|{ts}|{stat.st_size}|{path}")
outfile.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
PY

    python3 - "${log_dir}" "${RAW_DIR}/journal_files.txt" <<'PY'
from pathlib import Path
import sys

log_dir = Path(sys.argv[1])
outfile = Path(sys.argv[2])
targets = [
    "pod_a_live.jsonl",
    "pod_b_live.jsonl",
    "pod_c_live.jsonl",
]
lines = []
for name in targets:
    path = log_dir / name
    if not path.exists():
        continue
    line_count = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
    lines.append(f"logs/{name}|{line_count}|{int(path.stat().st_mtime)}")
outfile.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
PY

    python3 - "${log_dir}" "${RAW_DIR}/hip4_files.txt" <<'PY'
from pathlib import Path
import sys

log_root = Path(sys.argv[1])
outfile = Path(sys.argv[2])
targets = [
    "decisions.jsonl",
    "opportunities.csv",
    "short_expiry_features.csv",
    "edge_decay.csv",
    "latency_stats.csv",
    "daily_summary.csv",
    "settlements.csv",
    "trades.csv",
]
lines = []
for directory in ("hip4_outcome_testnet", "hip4_outcome_mainnet"):
    log_dir = log_root / directory
    for name in targets:
        path = log_dir / name
        if not path.exists():
            continue
        line_count = sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
        lines.append(f"logs/{directory}/{name}|{line_count}|{int(path.stat().st_mtime)}")
outfile.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
PY

    if [ -f "${runtime_dir}/pod_b_live_status.json" ]; then
        printf 'present\n' > "${RAW_DIR}/pod_b_runtime_present.txt"
    else
        printf 'missing\n' > "${RAW_DIR}/pod_b_runtime_present.txt"
    fi

    copy_if_exists "${docker_dir}/trident-api.log" "${RAW_DIR}/api_log_tail.txt" || : > "${RAW_DIR}/api_log_tail.txt"
    copy_if_exists "${docker_dir}/pod-a-live.log" "${RAW_DIR}/pod_a_log_tail.txt" || : > "${RAW_DIR}/pod_a_log_tail.txt"
    copy_if_exists "${docker_dir}/hip4-outcome-dry-run.log" "${RAW_DIR}/pod_b_log_tail.txt" \
        || copy_if_exists "${docker_dir}/pod-b-live.log" "${RAW_DIR}/pod_b_log_tail.txt" \
        || : > "${RAW_DIR}/pod_b_log_tail.txt"
    copy_if_exists "${docker_dir}/hip4-outcome-mainnet-observer.log" "${RAW_DIR}/hip4_mainnet_log_tail.txt" \
        || : > "${RAW_DIR}/hip4_mainnet_log_tail.txt"
    copy_if_exists "${docker_dir}/pod-c-live.log" "${RAW_DIR}/pod_c_log_tail.txt" || : > "${RAW_DIR}/pod_c_log_tail.txt"
    copy_if_exists "${docker_dir}/funding-collector.log" "${RAW_DIR}/funding_collector_log_tail.txt" || : > "${RAW_DIR}/funding_collector_log_tail.txt"
    copy_if_exists "${docker_dir}/tradfi-funding-collector.log" "${RAW_DIR}/tradfi_funding_collector_log_tail.txt" || : > "${RAW_DIR}/tradfi_funding_collector_log_tail.txt"

    python3 - "${RAW_DIR}/health.json" "${RAW_DIR}/report.json" "${runtime_dir}" "${RAW_DIR}/docker_ps.txt" <<'PY'
import json
from pathlib import Path
import sys

health_path = Path(sys.argv[1])
report_path = Path(sys.argv[2])
runtime_dir = Path(sys.argv[3])
outfile = Path(sys.argv[4])

def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

health = load_json(health_path)
report = load_json(report_path)
pods = {}
for item in report.get("pods", []):
    if isinstance(item, dict) and item.get("pod"):
        pods[item["pod"]] = item

def effective_process_state(pod_name: str, runtime_filename: str) -> str:
    report_state = str(pods.get(pod_name, {}).get("process_state", "")).strip()
    if report_state:
        return report_state
    return str(load_json(runtime_dir / runtime_filename).get("process_state", "")).strip()

lines = []
if health.get("status") == "ok":
    lines.append("trident-api\tUp (derived from local /health snapshot)")

if effective_process_state("pod_a", "pod_a_live_status.json") == "running":
    lines.append("trident-pod-a-live\tUp (derived from local runtime status)")

if effective_process_state("pod_b", "pod_b_live_status.json") == "running":
    lines.append("trident-hip4-outcome-dry-run\tUp (derived from local runtime status)")

if effective_process_state("pod_c", "pod_c_live_status.json") == "running":
    lines.append("trident-pod-c-live\tUp (derived from local runtime status)")

outfile.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
PY

    cat <<EOF > "${RAW_DIR}/server_meta.txt"
source=local_cache
host=${HOST}
local_dir=${base}
utc_now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

    : > "${RAW_DIR}/compose_ps.txt"
}

capture_remote() {
    local name="$1"
    local command="$2"
    local outfile="${RAW_DIR}/${name}"
    local errfile="${outfile}.stderr"
    if ssh_remote "bash -lc $(printf '%q' "${command}")" >"${outfile}" 2>"${errfile}"; then
        rm -f "${errfile}"
    else
        {
            echo "__REMOTE_COMMAND_FAILED__"
            echo "command=${command}"
            echo "stderr:"
            cat "${errfile}" 2>/dev/null || true
        } >"${outfile}"
        rm -f "${errfile}"
    fi
}

run_hip4_outcome_run_review() {
    if [ -z "${LOCAL_DIR}" ] || [ ! -d "${LOCAL_DIR}/logs" ]; then
        printf 'skipped:no_local_hip4_logs\n' > "${HIP4_RUN_REVIEW_STATUS}"
        return 0
    fi

    local replay_report_dir="${LOCAL_DIR}/replay_reports"
    local latest_json="${replay_report_dir}/hip4_outcome_run_review_latest.json"
    local latest_md="${replay_report_dir}/hip4_outcome_run_review_latest.md"
    mkdir -p "${replay_report_dir}"

    info "Generation de la review HIP-4 outcome depuis ${LOCAL_DIR}/logs..."
    if (
        cd "${ROOT_DIR}" && \
        if command -v uv >/dev/null 2>&1; then
            uv run python -m app.backtest.hip4_outcome_run_review \
                --logs-dir "paper=${LOCAL_DIR}/logs/hip4_outcome_paper" \
                --logs-dir "testnet=${LOCAL_DIR}/logs/hip4_outcome_testnet" \
                --logs-dir "mainnet=${LOCAL_DIR}/logs/hip4_outcome_mainnet" \
                --output-json "${HIP4_RUN_REVIEW_JSON}" \
                --output-md "${HIP4_RUN_REVIEW_MD}"
        elif command -v python3.12 >/dev/null 2>&1; then
            python3.12 -m app.backtest.hip4_outcome_run_review \
                --logs-dir "paper=${LOCAL_DIR}/logs/hip4_outcome_paper" \
                --logs-dir "testnet=${LOCAL_DIR}/logs/hip4_outcome_testnet" \
                --logs-dir "mainnet=${LOCAL_DIR}/logs/hip4_outcome_mainnet" \
                --output-json "${HIP4_RUN_REVIEW_JSON}" \
                --output-md "${HIP4_RUN_REVIEW_MD}"
        elif command -v python3.11 >/dev/null 2>&1; then
            python3.11 -m app.backtest.hip4_outcome_run_review \
                --logs-dir "paper=${LOCAL_DIR}/logs/hip4_outcome_paper" \
                --logs-dir "testnet=${LOCAL_DIR}/logs/hip4_outcome_testnet" \
                --logs-dir "mainnet=${LOCAL_DIR}/logs/hip4_outcome_mainnet" \
                --output-json "${HIP4_RUN_REVIEW_JSON}" \
                --output-md "${HIP4_RUN_REVIEW_MD}"
        else
            python3 -m app.backtest.hip4_outcome_run_review \
                --logs-dir "paper=${LOCAL_DIR}/logs/hip4_outcome_paper" \
                --logs-dir "testnet=${LOCAL_DIR}/logs/hip4_outcome_testnet" \
                --logs-dir "mainnet=${LOCAL_DIR}/logs/hip4_outcome_mainnet" \
                --output-json "${HIP4_RUN_REVIEW_JSON}" \
                --output-md "${HIP4_RUN_REVIEW_MD}"
        fi
    ) >"${HIP4_RUN_REVIEW_STDOUT}" 2>"${HIP4_RUN_REVIEW_STDERR}"; then
        cp "${HIP4_RUN_REVIEW_JSON}" "${latest_json}"
        cp "${HIP4_RUN_REVIEW_MD}" "${latest_md}"
        printf 'ok:%s:%s\n' "${HIP4_RUN_REVIEW_JSON}" "${latest_json}" > "${HIP4_RUN_REVIEW_STATUS}"
        ok "Review HIP-4 outcome generee"
        return 0
    fi

    warn "Review HIP-4 outcome echouee; la revue dry-run continue"
    printf 'failed:%s\n' "${HIP4_RUN_REVIEW_STDERR}" > "${HIP4_RUN_REVIEW_STATUS}"
    return 0
}

if [ -n "${LOCAL_DIR}" ] && [ -d "${LOCAL_DIR}" ]; then
    info "Collecte des artefacts du dry-run depuis le cache local ${LOCAL_DIR}..."
    capture_local_review_inputs "${LOCAL_DIR}"
else
    info "Collecte des artefacts du dry-run depuis ${SSH_TARGET}..."
    capture_remote "server_meta.txt" "cd '${REMOTE_DIR}' && echo host=\$(hostname) && echo utc_now=\$(date -u +%Y-%m-%dT%H:%M:%SZ) && echo pwd=\$(pwd)"
    capture_remote "docker_ps.txt" "docker ps -a --format '{{.Names}}\t{{.Status}}'"
    capture_remote "compose_ps.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml ps"
    capture_remote "health.json" "cd '${REMOTE_DIR}' && curl -fsS http://127.0.0.1:3000/health"
    capture_remote "state.json" "cd '${REMOTE_DIR}' && curl -fsS http://127.0.0.1:3000/api/state"
    capture_remote "metrics.json" "cd '${REMOTE_DIR}' && curl -fsS http://127.0.0.1:3000/api/metrics"
    capture_remote "report.json" "cd '${REMOTE_DIR}' && curl -fsS http://127.0.0.1:3000/api/report"
    capture_remote "hip4_outcome.json" "cd '${REMOTE_DIR}' && curl -fsS http://127.0.0.1:3000/api/hip4-outcome"
    capture_remote "hip4_outcome_mainnet.json" "cd '${REMOTE_DIR}' && curl -fsS http://127.0.0.1:3000/api/hip4-outcome-mainnet"
    capture_remote "snapshot_files.txt" "cd '${REMOTE_DIR}' && find data/live_snapshots -maxdepth 1 -type f -name '*.jsonl' -printf '%T@|%TY-%Tm-%TdT%TH:%TM:%TSZ|%s|%p\n' 2>/dev/null | sort -nr"
    capture_remote "journal_files.txt" "cd '${REMOTE_DIR}' && for f in logs/pod_a_live.jsonl logs/pod_b_live.jsonl logs/pod_c_live.jsonl; do if [ -f \"\$f\" ]; then printf '%s|%s|%s\n' \"\$f\" \"\$(wc -l < \"\$f\" | tr -d ' ')\" \"\$(stat -c %Y \"\$f\")\"; fi; done"
    capture_remote "hip4_files.txt" "cd '${REMOTE_DIR}' && for d in logs/hip4_outcome_testnet logs/hip4_outcome_mainnet; do for f in \"\$d\"/decisions.jsonl \"\$d\"/opportunities.csv \"\$d\"/short_expiry_features.csv \"\$d\"/edge_decay.csv \"\$d\"/latency_stats.csv \"\$d\"/daily_summary.csv \"\$d\"/settlements.csv \"\$d\"/trades.csv; do if [ -f \"\$f\" ]; then printf '%s|%s|%s\n' \"\$f\" \"\$(wc -l < \"\$f\" | tr -d ' ')\" \"\$(stat -c %Y \"\$f\")\"; fi; done; done"
    capture_remote "pod_b_runtime_present.txt" "cd '${REMOTE_DIR}' && if [ -f logs/pod_b_live_status.json ]; then echo present; else echo missing; fi"
    capture_remote "api_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} trident-api 2>&1"
    capture_remote "pod_a_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} pod-a-live 2>&1"
    capture_remote "pod_b_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} hip4-outcome-dry-run 2>&1 || docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} pod-b-live 2>&1"
    capture_remote "hip4_mainnet_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} hip4-outcome-mainnet-observer 2>&1"
    capture_remote "pod_c_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} pod-c-live 2>&1"
    capture_remote "funding_collector_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} funding-collector 2>&1"
    capture_remote "tradfi_funding_collector_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} tradfi-funding-collector 2>&1"
fi

run_hip4_outcome_run_review

info "Analyse locale des artefacts..."

python3 - <<'PY' "${RAW_DIR}" "${OUTPUT_DIR}" "${SNAPSHOT_MAX_AGE_MINUTES}" "${HOST}" "${SSH_USER}"
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


raw_dir = Path(sys.argv[1])
output_dir = Path(sys.argv[2])
snapshot_max_age_minutes = int(sys.argv[3])
host = sys.argv[4]
ssh_user = sys.argv[5]


@dataclass
class StageResult:
    stage: str
    status: str
    summary: str
    deterministic: bool
    checks: list[str]
    prompt_file: str | None = None


def read_text(name: str) -> str:
    path = raw_dir / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def load_json(name: str) -> dict[str, object] | None:
    text = read_text(name).strip()
    if not text or text.startswith("__REMOTE_COMMAND_FAILED__"):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def load_output_json(name: str) -> dict[str, object] | None:
    path = output_dir / name
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_docker_ps(text: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            statuses[parts[0].strip()] = parts[1].strip()
    return statuses


def parse_journal_files(text: str) -> dict[str, dict[str, int]]:
    files: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        path, line_count, mtime = parts
        try:
            files[path] = {
                "line_count": int(line_count),
                "mtime_epoch": int(mtime),
            }
        except ValueError:
            continue
    return files


def latest_snapshot_info(text: str) -> dict[str, object] | None:
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        try:
            epoch = float(parts[0])
            timestamp = parts[1]
            size = int(parts[2])
            path = parts[3]
        except ValueError:
            continue
        age_minutes = max(
            0.0,
            (datetime.now(timezone.utc).timestamp() - epoch) / 60.0,
        )
        return {
            "epoch": epoch,
            "timestamp": timestamp,
            "size_bytes": size,
            "path": path,
            "age_minutes": round(age_minutes, 2),
        }
    return None


def count_log_patterns(text: str) -> dict[str, int]:
    patterns = {
        "traceback": r"Traceback",
        "error": r"(?i)\berror\b",
        "exception": r"(?i)\bexception\b",
        "rate_limit": r"(?i)(429|rate[- ]?limit)",
        "connection": r"(?i)(connection reset|broken pipe|timed out|timeout)",
    }
    return {
        name: len(re.findall(pattern, text))
        for name, pattern in patterns.items()
    }


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def pod_report(report: dict[str, object], pod_name: str) -> dict[str, object] | None:
    pods = report.get("pods", [])
    if not isinstance(pods, list):
        return None
    for pod in pods:
        if isinstance(pod, dict) and pod.get("pod") == pod_name:
            return pod
    return None


def write_prompt(name: str, title: str, body: str) -> str:
    path = output_dir / name
    path.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    return str(path)


def service_report(report: dict[str, object], service_name: str) -> dict[str, object] | None:
    services = report.get("services", [])
    if not isinstance(services, list):
        return None
    for service in services:
        if isinstance(service, dict) and service.get("service") == service_name:
            return service
    return None


def nested_report(payload: dict[str, object], key: str) -> dict[str, object]:
    node = payload.get(key, {})
    if not isinstance(node, dict):
        return {}
    report_node = node.get("report", {})
    return report_node if isinstance(report_node, dict) else {}


def summarize_pod_economics(payload: dict[str, object]) -> dict[str, float | int | None]:
    signals = as_int(payload.get("signal_count"))
    accepted = as_int(payload.get("accepted_count"))
    opened = as_int(payload.get("opened_count"))
    skipped = as_int(payload.get("skipped_open_count"))
    closed = as_int(payload.get("closed_trade_count"))
    wins = as_int(payload.get("win_count"))
    realized = as_float(payload.get("realized_pnl_usd"))
    gross = as_float(payload.get("gross_pnl_usd"))
    fees = as_float(payload.get("fees_usd"))
    drawdown = as_float(payload.get("max_drawdown_usd"))
    return {
        "signals": signals,
        "accepted": accepted,
        "opened": opened,
        "skipped": skipped,
        "closed": closed,
        "wins": wins,
        "realized_pnl_usd": realized,
        "gross_pnl_usd": gross,
        "fees_usd": fees,
        "max_drawdown_usd": drawdown,
        "accepted_rate": safe_ratio(accepted, signals),
        "open_rate_given_accepted": safe_ratio(opened, accepted),
        "skip_rate_given_accepted": safe_ratio(skipped, accepted),
        "win_rate": safe_ratio(wins, closed),
        "fees_share_gross": safe_ratio(fees, gross),
    }


def pod_day_metrics(payload: dict[str, object], day: str) -> dict[str, float | int]:
    signals_by_date = payload.get("signals_by_date", {})
    accepted_by_date = payload.get("accepted_by_date", {})
    rejected_by_date = payload.get("rejected_by_date", {})
    pnl_by_date = payload.get("pnl_by_date", {})
    return {
        "signals": as_int(signals_by_date.get(day)) if isinstance(signals_by_date, dict) else 0,
        "accepted": (
            as_int(accepted_by_date.get(day)) if isinstance(accepted_by_date, dict) else 0
        ),
        "rejected": (
            as_int(rejected_by_date.get(day)) if isinstance(rejected_by_date, dict) else 0
        ),
        "pnl_usd": as_float(pnl_by_date.get(day)) if isinstance(pnl_by_date, dict) else 0.0,
    }


docker_ps = parse_docker_ps(read_text("docker_ps.txt"))
state = load_json("state.json") or {}
metrics = load_json("metrics.json") or {}
report = load_json("report.json") or {}
hip4_outcome = load_json("hip4_outcome.json") or {}
hip4_run_review = load_output_json("hip4_outcome_run_review.json") or {}
health = load_json("health.json") or {}
latest_snapshot = latest_snapshot_info(read_text("snapshot_files.txt"))
journal_files = parse_journal_files(read_text("journal_files.txt"))
hip4_files = parse_journal_files(read_text("hip4_files.txt"))
hip4_run_review_status = read_text("hip4_outcome_run_review_status.txt").strip()
pod_b_runtime_present = read_text("pod_b_runtime_present.txt").strip() == "present"

api_log_patterns = count_log_patterns(read_text("api_log_tail.txt"))
pod_a_log_patterns = count_log_patterns(read_text("pod_a_log_tail.txt"))
pod_b_log_patterns = count_log_patterns(read_text("pod_b_log_tail.txt"))
pod_c_log_patterns = count_log_patterns(read_text("pod_c_log_tail.txt"))
funding_collector_log_patterns = count_log_patterns(read_text("funding_collector_log_tail.txt"))
tradfi_funding_collector_log_patterns = count_log_patterns(
    read_text("tradfi_funding_collector_log_tail.txt")
)

ownership_conflicts = int(metrics.get("ownership_conflict_count", 0) or 0)
pod_b_status = state.get("pod_b_status", {}) if isinstance(state.get("pod_b_status", {}), dict) else {}
pod_b_is_hip4 = (
    isinstance(pod_b_status, dict)
    and str(pod_b_status.get("pod_kind", "")).strip().lower() == "hip4_outcome_edge_pod"
)
pod_a_report = pod_report(report, "pod_a") or {}
pod_b_report = pod_report(report, "pod_b") or {}
pod_c_report = pod_report(report, "pod_c") or {}
pod_a_runtime_report = nested_report(state, "pod_a_runtime")
pod_b_runtime_report = nested_report(state, "pod_b_status")
pod_c_runtime_report = nested_report(state, "pod_c_runtime")
funding_service_report = service_report(report, "funding_collector") or {}
tradfi_funding_service_report = service_report(report, "tradfi_funding_collector") or {}
pod_a_economics = summarize_pod_economics(pod_a_report)
pod_b_economics = summarize_pod_economics(pod_b_report)
pod_c_economics = summarize_pod_economics(pod_c_report)
hip4_run_readiness = (
    hip4_run_review.get("readiness", {})
    if isinstance(hip4_run_review.get("readiness", {}), dict)
    else {}
)
hip4_run_profile_count = (
    len(hip4_run_review.get("profiles", []))
    if isinstance(hip4_run_review.get("profiles", []), list)
    else 0
)
latest_business_date = (
    str(latest_snapshot["timestamp"])[:10] if latest_snapshot is not None else ""
)
pod_a_today = (
    pod_day_metrics(pod_a_runtime_report or pod_a_report, latest_business_date)
    if latest_business_date
    else {}
)
pod_b_today = (
    pod_day_metrics(pod_b_runtime_report or pod_b_report, latest_business_date)
    if latest_business_date
    else {}
)
pod_c_today = (
    pod_day_metrics(pod_c_runtime_report or pod_c_report, latest_business_date)
    if latest_business_date
    else {}
)

stages: list[StageResult] = []


def container_is_running(name: str) -> bool:
    status = docker_ps.get(name, "")
    return status.startswith("Up")


def append_stage(
    stage: str,
    status: str,
    summary: str,
    deterministic: bool,
    checks: list[str],
    prompt_file: str | None = None,
) -> None:
    stages.append(
        StageResult(
            stage=stage,
            status=status,
            summary=summary,
            deterministic=deterministic,
            checks=checks,
            prompt_file=prompt_file,
        )
    )


# Etape 1: base infra + collector + snapshots
infra_checks: list[str] = []
infra_failures: list[str] = []
infra_warnings: list[str] = []

if health.get("status") == "ok":
    infra_checks.append("API /health repond ok")
else:
    infra_failures.append("API /health ne repond pas ok")

if container_is_running("trident-api"):
    infra_checks.append("Container trident-api en cours d'execution")
else:
    infra_failures.append("Container trident-api absent ou arrete")

if container_is_running("trident-pod-a-live"):
    infra_checks.append("Container pod-a-live en cours d'execution")
else:
    infra_failures.append("Container pod-a-live absent ou arrete")

if latest_snapshot is None:
    infra_failures.append("Aucun snapshot live trouve dans data/live_snapshots")
else:
    age_minutes = float(latest_snapshot["age_minutes"])
    if age_minutes <= snapshot_max_age_minutes:
        infra_checks.append(
            f"Dernier snapshot frais ({age_minutes:.2f} min, seuil {snapshot_max_age_minutes} min)"
        )
    else:
        infra_failures.append(
            f"Dernier snapshot trop ancien ({age_minutes:.2f} min, seuil {snapshot_max_age_minutes} min)"
        )

if api_log_patterns["traceback"] == 0 and api_log_patterns["error"] == 0:
    infra_checks.append("Pas d'erreur evidente recente dans les logs API")
else:
    infra_failures.append(
        "Logs API recentes contiennent des erreurs/tracebacks"
    )

if bool(funding_service_report.get("healthy", False)):
    infra_checks.append("Funding Collector remonte healthy dans /api/report")
else:
    infra_failures.append("Funding Collector non healthy dans /api/report")

if tradfi_funding_service_report:
    if bool(tradfi_funding_service_report.get("healthy", False)):
        infra_checks.append("Tradfi Funding Collector remonte healthy dans /api/report")
    else:
        infra_failures.append("Tradfi Funding Collector non healthy dans /api/report")

if funding_collector_log_patterns["traceback"] > 0 or funding_collector_log_patterns["connection"] > 0:
    infra_warnings.append(
        "Funding Collector a des erreurs reseau recentes dans les logs Docker"
    )
else:
    infra_checks.append("Funding Collector sans traceback/timeout recent")

if (
    tradfi_funding_collector_log_patterns["traceback"] > 0
    or tradfi_funding_collector_log_patterns["connection"] > 0
):
    infra_warnings.append(
        "Tradfi Funding Collector a des erreurs reseau recentes dans les logs Docker"
    )
else:
    infra_checks.append("Tradfi Funding Collector sans traceback/timeout recent")

if infra_failures:
    append_stage(
        stage="etape_1_infra_et_collector",
        status="FAIL",
        summary="Le socle dry-run n'est pas sain; corriger l'infra avant d'analyser le trading.",
        deterministic=True,
        checks=infra_checks + infra_warnings + infra_failures,
    )
elif infra_warnings:
    append_stage(
        stage="etape_1_infra_et_collector",
        status="WARN",
        summary="Le socle dry-run tient, mais les collecteurs montrent une fragilite operationnelle a surveiller.",
        deterministic=True,
        checks=infra_checks + infra_warnings,
    )
else:
    append_stage(
        stage="etape_1_infra_et_collector",
        status="PASS",
        summary="API, Pod A et snapshots live paraissent sains sur les checks mecaniques.",
        deterministic=True,
        checks=infra_checks,
    )


# Etape 2: Pod A
pod_a_checks: list[str] = []
pod_a_failures: list[str] = []
pod_a_warnings: list[str] = []
pod_a_journal = journal_files.get("logs/pod_a_live.jsonl")

if container_is_running("trident-pod-a-live"):
    pod_a_checks.append("Pod A tourne")
else:
    pod_a_failures.append("Pod A ne tourne pas")

if bool(pod_a_report.get("healthy", False)):
    pod_a_checks.append("Supervisor considere Pod A healthy")
else:
    pod_a_failures.append("Supervisor ne considere pas Pod A healthy")

if pod_a_log_patterns["traceback"] == 0:
    pod_a_checks.append("Pas de traceback recent dans les logs Pod A")
else:
    pod_a_failures.append("Traceback recent detecte dans les logs Pod A")

if pod_a_journal is not None:
    pod_a_journal_line_count = as_int(pod_a_journal.get("line_count"))
    if pod_a_journal_line_count > 0:
        pod_a_checks.append(f"Journal Pod A present ({pod_a_journal_line_count} lignes)")
    elif pod_a_economics["signals"] == 0 and pod_a_economics["closed"] == 0:
        pod_a_checks.append(
            "Journal Pod A present mais vide (coherent: aucun signal ni trade depuis le demarrage)"
        )
    else:
        pod_a_warnings.append(
            "Journal Pod A present mais vide malgre une activite Pod A non nulle"
        )
else:
    pod_a_checks.append("Journal Pod A absent")

pod_a_fees_share = pod_a_economics["fees_share_gross"]
if pod_a_fees_share is not None and pod_a_fees_share >= 0.45:
    pod_a_warnings.append(
        f"Fees Pod A lourdes par rapport au gross PnL ({pod_a_economics['fees_usd']:.2f} USD / {pod_a_economics['gross_pnl_usd']:.2f} USD)"
    )
if (
    pod_a_economics["realized_pnl_usd"] > 0.0
    and pod_a_economics["max_drawdown_usd"] > max(pod_a_economics["realized_pnl_usd"] * 1.5, 10.0)
):
    pod_a_warnings.append(
        f"Max drawdown Pod A eleve vs PnL net ({pod_a_economics['max_drawdown_usd']:.2f} USD vs {pod_a_economics['realized_pnl_usd']:.2f} USD)"
    )
pod_a_signals_by_date = pod_a_report.get("signals_by_date", {})
pod_a_accepted_by_date = pod_a_report.get("accepted_by_date", {})
if isinstance(pod_a_signals_by_date, dict) and latest_snapshot is not None:
    latest_date = str(latest_snapshot["timestamp"])[:10]
    latest_signal_count = as_int(pod_a_signals_by_date.get(latest_date))
    latest_accept_count = (
        as_int(pod_a_accepted_by_date.get(latest_date))
        if isinstance(pod_a_accepted_by_date, dict)
        else 0
    )
    if latest_signal_count > 0 and latest_accept_count == 0:
        pod_a_warnings.append(
            f"Pod A a vu {latest_signal_count} signaux le {latest_date} sans aucune acceptation"
        )

pod_a_prompt = None
if not pod_a_failures:
    pod_a_prompt = write_prompt(
        "llm_prompt_etape_2_pod_a.md",
        "Prompt LLM - Revue Pod A dry-run",
        (
            "Analyse les resultats du dry-run Pod A a partir des artefacts locaux suivants.\n\n"
            "Objectif: juger si Pod A se comporte de facon coherente, meme si le signal est rare.\n\n"
            f"Contexte resume:\n"
            f"- host: {host}\n"
            f"- user: {ssh_user}\n"
            f"- regime: {report.get('regime')}\n"
            f"- ownership_conflict_count: {ownership_conflicts}\n"
            f"- pod_a_healthy: {pod_a_report.get('healthy')}\n"
            f"- pod_a_target_usd: {pod_a_report.get('target_usd')}\n"
            f"- pod_a_preview_count: {pod_a_report.get('preview_count')}\n"
            f"- pod_a_economics: {pod_a_economics}\n"
            f"- dernier snapshot: {latest_snapshot}\n"
            f"- journal Pod A: {pod_a_journal}\n"
            f"- log patterns Pod A: {pod_a_log_patterns}\n\n"
            "Artefacts a lire:\n"
            f"- {raw_dir / 'state.json'}\n"
            f"- {raw_dir / 'metrics.json'}\n"
            f"- {raw_dir / 'report.json'}\n"
            f"- {raw_dir / 'pod_a_log_tail.txt'}\n"
            f"- {raw_dir / 'journal_files.txt'}\n\n"
            "Questions:\n"
            "1. Est-ce que Pod A semble stable et coherent au vu des logs, du supervisor et du journal?\n"
            "2. L'absence ou la rarete des signaux te semble-t-elle normale ou suspecte?\n"
            "3. Y a-t-il un comportement inquietant, meme sans erreur fatale?\n"
            "4. Verdict: go pour continuer le dry-run Pod A, ou no-go avec raisons precises?\n"
        ),
    )

if pod_a_failures:
    append_stage(
        stage="etape_2_pod_a_dry_run",
        status="FAIL",
        summary="Pod A n'est pas suffisamment sain pour une revue qualitative.",
        deterministic=True,
        checks=pod_a_checks + pod_a_warnings + pod_a_failures,
    )
elif pod_a_warnings:
    append_stage(
        stage="etape_2_pod_a_dry_run",
        status="WARN",
        summary="Pod A tient mecaniquement, mais ses economics et sa fraicheur de signal ne sont pas encore convaincants.",
        deterministic=False,
        checks=pod_a_checks + pod_a_warnings,
        prompt_file=pod_a_prompt,
    )
else:
    append_stage(
        stage="etape_2_pod_a_dry_run",
        status="PASS",
        summary="Les checks mecaniques de Pod A sont verts; une revue qualitative reste necessaire.",
        deterministic=False,
        checks=pod_a_checks,
        prompt_file=pod_a_prompt,
    )


# Etape 3: Pod A + Pod B
pod_b_checks: list[str] = []
pod_b_failures: list[str] = []
pod_b_warnings: list[str] = []

if container_is_running("trident-hip4-outcome-dry-run") or container_is_running("trident-pod-b-live"):
    pod_b_checks.append("Pod B tourne")
    if container_is_running("trident-hip4-outcome-dry-run"):
        pod_b_checks.append("Container HIP-4 Outcome actif pour Pod B")
    if container_is_running("trident-pod-b-live"):
        pod_b_failures.append("Ancien container trident-pod-b-live encore actif")
    if bool(pod_b_report.get("healthy", False)):
        pod_b_checks.append("Supervisor considere Pod B healthy")
    else:
        pod_b_failures.append("Supervisor ne considere pas Pod B healthy")

    if str(pod_b_report.get("process_state", "")) == "running":
        pod_b_checks.append("Pod B process_state=running")
    else:
        pod_b_failures.append(
            f"Pod B process_state inattendu: {pod_b_report.get('process_state')}"
        )

    if ownership_conflicts == 0:
        pod_b_checks.append("Pas de conflit d'ownership")
    else:
        pod_b_failures.append(f"Conflits d'ownership detectes: {ownership_conflicts}")

    if pod_b_is_hip4:
        pod_b_checks.append("Alias runtime Pod B pointe sur HIP-4 Outcome")
    else:
        pod_b_failures.append("Alias runtime Pod B ne pointe pas sur HIP-4 Outcome")

    if hip4_outcome:
        if bool(hip4_outcome.get("fresh")):
            pod_b_checks.append("Payload /api/hip4-outcome frais")
        else:
            pod_b_failures.append("Payload /api/hip4-outcome absent ou stale")
        if str(hip4_outcome.get("process_state", "")) == "running":
            pod_b_checks.append("HIP-4 process_state=running")
        else:
            pod_b_failures.append(
                f"HIP-4 process_state inattendu: {hip4_outcome.get('process_state')}"
            )
        markets_seen = as_int(hip4_outcome.get("markets_seen"))
        markets_supported = as_int(hip4_outcome.get("markets_supported"))
        if markets_seen > 0 and markets_supported > 0:
            pod_b_checks.append(
                f"HIP-4 voit {markets_supported}/{markets_seen} marche(s) supporte(s)"
            )
        else:
            pod_b_failures.append("HIP-4 ne voit pas de marche outcome exploitable")
        capital = hip4_outcome.get("capital", {})
        if isinstance(capital, dict):
            pod_b_checks.append(
                "Budget HIP-4: "
                f"exposure={capital.get('open_exposure_usdc')} "
                f"remaining={capital.get('remaining_budget_usdc')} "
                f"budget={capital.get('budget_usdc')}"
            )
    else:
        pod_b_failures.append("Snapshot /api/hip4-outcome absent")

    for required_file in (
        "logs/hip4_outcome_testnet/decisions.jsonl",
        "logs/hip4_outcome_testnet/opportunities.csv",
        "logs/hip4_outcome_testnet/latency_stats.csv",
    ):
        file_info = hip4_files.get(required_file)
        if file_info is None:
            pod_b_failures.append(f"Artefact HIP-4 manquant: {required_file}")
            continue
        line_count = as_int(file_info.get("line_count"))
        if line_count > 0:
            pod_b_checks.append(f"Artefact HIP-4 present: {required_file} ({line_count} lignes)")
        else:
            pod_b_warnings.append(f"Artefact HIP-4 vide: {required_file}")

    if pod_b_log_patterns["traceback"] == 0:
        pod_b_checks.append("Pas de traceback recent dans les logs Pod B")
    else:
        pod_b_failures.append("Traceback recent detecte dans les logs Pod B")

    if pod_b_log_patterns["rate_limit"] > 0:
        pod_b_warnings.append(
            f"Pod B montre {pod_b_log_patterns['rate_limit']} signalement(s) de rate limit recent(s)"
        )
    pod_b_fees_share = pod_b_economics["fees_share_gross"]
    if pod_b_fees_share is not None and pod_b_fees_share >= 0.5:
        pod_b_warnings.append(
            f"Fees Pod B lourdes par rapport au gross PnL ({pod_b_economics['fees_usd']:.2f} USD / {pod_b_economics['gross_pnl_usd']:.2f} USD)"
        )
    if hip4_run_review:
        hip4_status = str(hip4_run_readiness.get("status", "unknown"))
        hip4_recommendation = str(hip4_run_readiness.get("recommendation", ""))
        pod_b_checks.append(
            "Review HIP-4 outcome generee "
            f"({hip4_run_profile_count} profils, status={hip4_status})"
        )
        if hip4_recommendation:
            pod_b_checks.append(f"Recommendation HIP-4: {hip4_recommendation}")
        for reason in hip4_run_readiness.get("reasons", []):
            pod_b_warnings.append(f"Review HIP-4: {reason}")
    elif hip4_run_review_status.startswith("failed:"):
        pod_b_warnings.append(
            f"Review HIP-4 outcome indisponible ({hip4_run_review_status})"
        )
    else:
        pod_b_warnings.append(
            "Review HIP-4 outcome non lancee; lancer fetch_trident_data.sh pour analyser les logs complets"
        )

    pod_b_prompt = None
    if not pod_b_failures:
        pod_b_prompt = write_prompt(
            "llm_prompt_etape_3_pod_a_plus_pod_b.md",
            "Prompt LLM - Revue cohabitation Pod A + Pod B HIP-4",
            (
                "Analyse la cohabitation dry-run Pod A + Pod B HIP-4 Outcome.\n\n"
                "Objectif: verifier que la coexistence est propre avec comptes separes, "
                "et que les opportunites outcome testnet montrent ou non un edge exploitable.\n\n"
                f"Contexte resume:\n"
                f"- ownership_conflict_count: {ownership_conflicts}\n"
                f"- pod_b_process_state: {pod_b_report.get('process_state')}\n"
                f"- pod_b_position_count: {pod_b_report.get('position_count')}\n"
                f"- pod_b_open_order_count: {pod_b_report.get('open_order_count')}\n"
                f"- pod_b_total_fill_count: {pod_b_report.get('total_fill_count')}\n"
                f"- pod_b_realized_pnl_usd: {pod_b_report.get('realized_pnl_usd')}\n"
                f"- pod_b_total_unrealized_pnl_usd: {pod_b_report.get('total_unrealized_pnl_usd')}\n"
                f"- pod_b_runtime_config_present: {pod_b_runtime_present}\n"
                f"- pod_b_economics: {pod_b_economics}\n"
                f"- hip4_outcome_summary: {hip4_outcome}\n"
                f"- hip4_files: {hip4_files}\n"
                f"- hip4_run_review_status: {hip4_run_review_status}\n"
                f"- hip4_run_readiness: {hip4_run_readiness}\n"
                f"- log patterns Pod B: {pod_b_log_patterns}\n\n"
                "Artefacts a lire:\n"
                f"- {raw_dir / 'state.json'}\n"
                f"- {raw_dir / 'metrics.json'}\n"
                f"- {raw_dir / 'report.json'}\n"
                f"- {raw_dir / 'hip4_outcome.json'}\n"
                f"- {raw_dir / 'hip4_files.txt'}\n"
                f"- {output_dir / 'hip4_outcome_run_review.md'}\n"
                f"- {output_dir / 'hip4_outcome_run_review.json'}\n"
                f"- {raw_dir / 'pod_b_log_tail.txt'}\n"
                f"- {raw_dir / 'journal_files.txt'}\n\n"
                "Questions:\n"
                "1. La cohabitation Pod A / Pod B HIP-4 parait-elle saine?\n"
                "2. Les positions paper, edges, latences et fichiers d'opportunites HIP-4 sont-ils coherents?\n"
                "3. Voit-on un edge outcome exploitable ou seulement du bruit/mauvais pricing apparent?\n"
                "4. Verdict: go pour continuer Pod B HIP-4, ou no-go avec raisons precises?\n"
            ),
        )

    if pod_b_failures:
        append_stage(
            stage="etape_3_pod_a_plus_pod_b",
            status="FAIL",
            summary="La cohabitation avec Pod B n'est pas saine sur les checks mecaniques.",
            deterministic=True,
            checks=pod_b_checks + pod_b_warnings + pod_b_failures,
        )
    elif pod_b_warnings:
        append_stage(
            stage="etape_3_pod_a_plus_pod_b",
            status="WARN",
            summary="Pod B HIP-4 reste globalement defendable, mais quelques signaux operationnels meritent surveillance.",
            deterministic=False,
            checks=pod_b_checks + pod_b_warnings,
            prompt_file=pod_b_prompt,
        )
    else:
        append_stage(
            stage="etape_3_pod_a_plus_pod_b",
            status="PASS",
            summary="Les checks mecaniques de cohabitation Pod A / Pod B HIP-4 sont verts; une revue qualitative est recommandee.",
            deterministic=False,
            checks=pod_b_checks,
            prompt_file=pod_b_prompt,
        )
else:
    append_stage(
        stage="etape_3_pod_a_plus_pod_b",
        status="SKIPPED",
        summary="Pod B n'est pas lance; cette etape n'est pas encore evaluable.",
        deterministic=True,
        checks=["Container hip4-outcome-dry-run absent ou arrete"],
    )


# Etape 4: Pod C optionnel
if container_is_running("trident-pod-c-live"):
    pod_c_checks: list[str] = []
    pod_c_failures: list[str] = []
    pod_c_warnings: list[str] = []
    if bool(pod_c_report.get("healthy", False)):
        pod_c_checks.append("Supervisor considere Pod C healthy")
    else:
        pod_c_failures.append("Supervisor ne considere pas Pod C healthy")
    if pod_c_log_patterns["traceback"] == 0:
        pod_c_checks.append("Pas de traceback recent dans les logs Pod C")
    else:
        pod_c_failures.append("Traceback recent detecte dans les logs Pod C")

    pod_c_fees_share = pod_c_economics["fees_share_gross"]
    if pod_c_economics["realized_pnl_usd"] < 0.0:
        pod_c_warnings.append(
            f"Pod C est negatif en net ({pod_c_economics['realized_pnl_usd']:.2f} USD)"
        )
    if pod_c_fees_share is not None and pod_c_fees_share > 1.0:
        pod_c_warnings.append(
            f"Fees Pod C superieures au gross PnL ({pod_c_economics['fees_usd']:.2f} USD / {pod_c_economics['gross_pnl_usd']:.2f} USD)"
        )
    if (
        pod_c_economics["skip_rate_given_accepted"] is not None
        and pod_c_economics["skip_rate_given_accepted"] >= 0.6
    ):
        pod_c_warnings.append(
            f"Pod C convertit mal ses acceptations en ouvertures ({pod_c_economics['skipped']} skipped / {pod_c_economics['accepted']} acceptes)"
        )
    pod_c_rejections = (
        pod_c_report.get("rejections_by_reason", {})
        if isinstance(pod_c_report.get("rejections_by_reason", {}), dict)
        else {}
    )
    margin_below_min_count = as_int(pod_c_rejections.get("margin_below_min"))
    if margin_below_min_count >= 25:
        pod_c_warnings.append(
            f"Pod C est fortement contraint par margin_below_min ({margin_below_min_count} rejets)"
        )
    if (
        pod_c_economics["realized_pnl_usd"] < 0.0
        and pod_c_fees_share is not None
        and pod_c_fees_share > 1.0
        and margin_below_min_count >= 25
        and pod_c_economics["skip_rate_given_accepted"] is not None
        and pod_c_economics["skip_rate_given_accepted"] >= 0.6
    ):
        pod_c_failures.append(
            "Pod C n'est pas defendable economiquement sur cette fenetre: PnL net negatif, fees dominantes et forte friction de gating."
        )

    pod_c_prompt = None
    if not pod_c_failures:
        pod_c_prompt = write_prompt(
            "llm_prompt_etape_4_pod_c.md",
            "Prompt LLM - Revue Pod C",
            (
                "Analyse si Pod C doit rester active dans ce dry-run.\n\n"
                "Objectif: verifier que son activation est defendable, et non un simple bruit experimental.\n\n"
                f"Contexte resume:\n"
                f"- pod_c_healthy: {pod_c_report.get('healthy')}\n"
                f"- pod_c_target_usd: {pod_c_report.get('target_usd')}\n"
                f"- pod_c_preview_count: {pod_c_report.get('preview_count')}\n"
                f"- pod_c_economics: {pod_c_economics}\n"
                f"- log patterns Pod C: {pod_c_log_patterns}\n\n"
                "Artefacts a lire:\n"
                f"- {raw_dir / 'state.json'}\n"
                f"- {raw_dir / 'report.json'}\n"
                f"- {raw_dir / 'pod_c_log_tail.txt'}\n\n"
                "Questions:\n"
                "1. Pod C apporte-t-il quelque chose de credible ou juste du bruit?\n"
                "2. Les logs montrent-ils une logique event-driven plausible?\n"
                "3. Faut-il laisser Pod C actif, le couper, ou le garder uniquement en recherche?\n"
            ),
        )

    if pod_c_failures:
        append_stage(
            stage="etape_4_pod_c_optionnel",
            status="FAIL",
            summary="Pod C tourne, mais son profil economique sur cette fenetre n'est pas defendable.",
            deterministic=True,
            checks=pod_c_checks + pod_c_warnings + pod_c_failures,
        )
    elif pod_c_warnings:
        append_stage(
            stage="etape_4_pod_c_optionnel",
            status="WARN",
            summary="Pod C reste vivant, mais il ressemble davantage a un pod de recherche qu'a un pod live credible.",
            deterministic=False,
            checks=pod_c_checks + pod_c_warnings,
            prompt_file=pod_c_prompt,
        )
    else:
        append_stage(
            stage="etape_4_pod_c_optionnel",
            status="PASS",
            summary="Pod C tourne sans anomalie mecanique evidente; une revue qualitative est necessaire.",
            deterministic=False,
            checks=pod_c_checks,
            prompt_file=pod_c_prompt,
        )
else:
    append_stage(
        stage="etape_4_pod_c_optionnel",
        status="SKIPPED",
        summary="Pod C n'est pas lance, ce qui est normal tant qu'il reste optionnel.",
        deterministic=True,
        checks=["Container pod-c-live absent ou arrete"],
    )


# Etape 5: fraicheur strategique
strategic_checks: list[str] = []
strategic_failures: list[str] = []
strategic_warnings: list[str] = []

if latest_business_date:
    strategic_checks.append(f"Date strategique evaluee: {latest_business_date}")
else:
    strategic_failures.append("Impossible de determiner la date de fraicheur strategique")

freshness_specs = [
    ("Pod A", pod_a_report, pod_a_today),
    ("Pod B", pod_b_report, pod_b_today),
    ("Pod C", pod_c_report, pod_c_today),
]
fresh_active_pods = 0
stale_active_pods = 0
for label, payload, day_metrics in freshness_specs:
    if not latest_business_date:
        continue
    if label == "Pod B" and pod_b_is_hip4:
        open_positions = as_int(hip4_outcome.get("open_positions"))
        opportunities = as_int(hip4_outcome.get("opportunities_this_loop"))
        decisions = as_int(
            hip4_files.get("logs/hip4_outcome_testnet/decisions.jsonl", {}).get("line_count")
        )
        best_edge = as_float(hip4_outcome.get("best_net_edge"))
        if bool(hip4_outcome.get("fresh")) and (open_positions > 0 or opportunities > 0 or decisions > 1):
            fresh_active_pods += 1
            strategic_checks.append(
                "Pod B HIP-4 actif "
                f"(positions={open_positions}, opps_loop={opportunities}, decisions={decisions}, best_edge={best_edge:.4f})"
            )
        else:
            stale_active_pods += 1
            strategic_warnings.append(
                "Pod B HIP-4 est actif comme pod, mais ne montre pas encore d'activite outcome exploitable"
            )
        continue
    target_usd = as_float(payload.get("target_usd"))
    if target_usd <= 0.0:
        strategic_checks.append(f"{label} hors cible aujourd'hui (target_usd={target_usd:.2f})")
        continue
    signals = as_int(day_metrics.get("signals"))
    accepted = as_int(day_metrics.get("accepted"))
    pnl_usd = as_float(day_metrics.get("pnl_usd"))
    if signals == 0:
        stale_active_pods += 1
        strategic_warnings.append(
            f"{label} a un target_usd actif ({target_usd:.2f}) mais aucun signal sur {latest_business_date}"
        )
        continue
    if accepted == 0:
        stale_active_pods += 1
        strategic_warnings.append(
            f"{label} a produit {signals} signaux sur {latest_business_date} sans aucune acceptation"
        )
        continue
    fresh_active_pods += 1
    strategic_checks.append(
        f"{label} reste actif sur {latest_business_date} ({signals} signaux, {accepted} acceptes, pnl_jour={pnl_usd:.2f} USD)"
    )

if latest_business_date and fresh_active_pods == 0 and stale_active_pods > 0:
    strategic_failures.append(
        "Aucun pod cible ne montre encore une activite exploitable sur la journee courante"
    )

if strategic_failures:
    append_stage(
        stage="etape_5_fraicheur_strategique",
        status="FAIL",
        summary="Les artefacts sont frais, mais l'edge live n'est pas frais sur la tranche courante.",
        deterministic=True,
        checks=strategic_checks + strategic_warnings + strategic_failures,
    )
elif strategic_warnings:
    append_stage(
        stage="etape_5_fraicheur_strategique",
        status="WARN",
        summary="Les artefacts sont frais, mais au moins un pod cible ne prouve pas encore sa fraicheur strategique.",
        deterministic=True,
        checks=strategic_checks + strategic_warnings,
    )
else:
    append_stage(
        stage="etape_5_fraicheur_strategique",
        status="PASS",
        summary="Les pods cibles montrent encore une activite exploitable sur la journee courante.",
        deterministic=True,
        checks=strategic_checks,
    )


summary = {
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "host": host,
    "ssh_user": ssh_user,
    "latest_snapshot": latest_snapshot,
    "latest_business_date": latest_business_date,
    "ownership_conflict_count": ownership_conflicts,
    "hip4_run_review": {
        "status": hip4_run_readiness.get("status"),
        "recommendation": hip4_run_readiness.get("recommendation"),
        "reasons": hip4_run_readiness.get("reasons", []),
        "profile_count": hip4_run_profile_count,
        "json_path": str(output_dir / "hip4_outcome_run_review.json")
        if hip4_run_review
        else None,
        "markdown_path": str(output_dir / "hip4_outcome_run_review.md")
        if hip4_run_review
        else None,
        "raw_status": hip4_run_review_status,
    },
    "stages": [asdict(stage) for stage in stages],
}
(output_dir / "review_summary.json").write_text(
    json.dumps(summary, indent=2, ensure_ascii=True) + "\n",
    encoding="utf-8",
)

md_lines = [
    "# TRIDENT dry-run review",
    "",
    f"- host: `{host}`",
    f"- user: `{ssh_user}`",
    f"- generated_at: `{summary['generated_at']}`",
    f"- latest_snapshot: `{latest_snapshot}`",
    f"- latest_business_date: `{latest_business_date}`",
    f"- ownership_conflict_count: `{ownership_conflicts}`",
    "",
]
if hip4_run_review:
    md_lines.extend(
        [
            "## HIP-4 Outcome Run Review",
            "",
            f"- status: `{hip4_run_readiness.get('status', 'unknown')}`",
            f"- recommendation: {hip4_run_readiness.get('recommendation', 'n/a')}",
            f"- report: `{output_dir / 'hip4_outcome_run_review.md'}`",
            f"- json: `{output_dir / 'hip4_outcome_run_review.json'}`",
            "",
        ]
    )
elif hip4_run_review_status:
    md_lines.extend(
        [
            "## HIP-4 Outcome Run Review",
            "",
            f"- status: `{hip4_run_review_status}`",
            "- note: run the review from a local fetch cache to analyze full HIP-4 logs.",
            "",
        ]
    )
md_lines.extend(
    [
        "## Stages",
        "",
    ]
)
for stage in stages:
    md_lines.append(f"### {stage.stage}")
    md_lines.append("")
    md_lines.append(f"- status: `{stage.status}`")
    md_lines.append(f"- deterministic: `{str(stage.deterministic).lower()}`")
    md_lines.append(f"- summary: {stage.summary}")
    if stage.prompt_file:
        md_lines.append(f"- prompt_file: `{stage.prompt_file}`")
    md_lines.append("- checks:")
    for check in stage.checks:
        md_lines.append(f"  - {check}")
    md_lines.append("")

(output_dir / "review_summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

print(json.dumps(summary, indent=2, ensure_ascii=True))
PY

ok "Revue terminee"
echo "Artefacts ecrits dans: ${OUTPUT_DIR}"
echo "  - ${OUTPUT_DIR}/review_summary.md"
echo "  - ${OUTPUT_DIR}/review_summary.json"
if [ -f "${HIP4_RUN_REVIEW_MD}" ]; then
    echo "  - ${HIP4_RUN_REVIEW_MD}"
    echo "  - ${HIP4_RUN_REVIEW_JSON}"
fi
