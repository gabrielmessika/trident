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
    local latest_health latest_state latest_metrics latest_report
    local api_dir="${base}/api"
    local snapshot_dir="${base}/live_snapshots"
    local log_dir="${base}/logs"
    local runtime_dir="${base}/runtime"
    local docker_dir="${base}/docker"

    latest_health="$(latest_local_file "${api_dir}/health-*.json")"
    latest_state="$(latest_local_file "${api_dir}/state-*.json")"
    latest_metrics="$(latest_local_file "${api_dir}/metrics-*.json")"
    latest_report="$(latest_local_file "${api_dir}/report-*.json")"

    copy_if_exists "${latest_health}" "${RAW_DIR}/health.json" || : > "${RAW_DIR}/health.json"
    copy_if_exists "${latest_state}" "${RAW_DIR}/state.json" || : > "${RAW_DIR}/state.json"
    copy_if_exists "${latest_metrics}" "${RAW_DIR}/metrics.json" || : > "${RAW_DIR}/metrics.json"
    copy_if_exists "${latest_report}" "${RAW_DIR}/report.json" || : > "${RAW_DIR}/report.json"

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
    "pod_b_live_report.json",
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

    if [ -f "${runtime_dir}/pod_b_live_config.json" ]; then
        printf 'present\n' > "${RAW_DIR}/pod_b_runtime_present.txt"
    else
        printf 'missing\n' > "${RAW_DIR}/pod_b_runtime_present.txt"
    fi

    copy_if_exists "${docker_dir}/trident-api.log" "${RAW_DIR}/api_log_tail.txt" || : > "${RAW_DIR}/api_log_tail.txt"
    copy_if_exists "${docker_dir}/pod-a-live.log" "${RAW_DIR}/pod_a_log_tail.txt" || : > "${RAW_DIR}/pod_a_log_tail.txt"
    copy_if_exists "${docker_dir}/pod-b-live.log" "${RAW_DIR}/pod_b_log_tail.txt" || : > "${RAW_DIR}/pod_b_log_tail.txt"
    copy_if_exists "${docker_dir}/pod-c-live.log" "${RAW_DIR}/pod_c_log_tail.txt" || : > "${RAW_DIR}/pod_c_log_tail.txt"

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

lines = []
if health.get("status") == "ok":
    lines.append("trident-api\tUp (derived from local /health snapshot)")

pod_a = load_json(runtime_dir / "pod_a_live_status.json")
if pod_a.get("process_state") == "running" or pods.get("pod_a", {}).get("process_state") == "running":
    lines.append("trident-pod-a-live\tUp (derived from local runtime status)")

pod_b = load_json(runtime_dir / "pod_b_live_status.json")
if pod_b.get("process_state") == "running" or pods.get("pod_b", {}).get("process_state") == "running":
    lines.append("trident-pod-b-live\tUp (derived from local runtime status)")

pod_c = load_json(runtime_dir / "pod_c_live_status.json")
if pod_c.get("process_state") == "running" or pods.get("pod_c", {}).get("process_state") == "running":
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
    capture_remote "snapshot_files.txt" "cd '${REMOTE_DIR}' && find data/live_snapshots -maxdepth 1 -type f -name '*.jsonl' -printf '%T@|%TY-%Tm-%TdT%TH:%TM:%TSZ|%s|%p\n' 2>/dev/null | sort -nr"
    capture_remote "journal_files.txt" "cd '${REMOTE_DIR}' && for f in logs/pod_a_live.jsonl logs/pod_b_live.jsonl logs/pod_c_live.jsonl logs/pod_b_live_report.json; do if [ -f \"\$f\" ]; then printf '%s|%s|%s\n' \"\$f\" \"\$(wc -l < \"\$f\" | tr -d ' ')\" \"\$(stat -c %Y \"\$f\")\"; fi; done"
    capture_remote "pod_b_runtime_present.txt" "cd '${REMOTE_DIR}' && if [ -f runtime/passivbot/live.json ]; then echo present; else echo missing; fi"
    capture_remote "api_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} trident-api 2>&1"
    capture_remote "pod_a_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} pod-a-live 2>&1"
    capture_remote "pod_b_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} pod-b-live 2>&1"
    capture_remote "pod_c_log_tail.txt" "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} pod-c-live 2>&1"
fi

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


docker_ps = parse_docker_ps(read_text("docker_ps.txt"))
state = load_json("state.json") or {}
metrics = load_json("metrics.json") or {}
report = load_json("report.json") or {}
health = load_json("health.json") or {}
latest_snapshot = latest_snapshot_info(read_text("snapshot_files.txt"))
journal_files = parse_journal_files(read_text("journal_files.txt"))
pod_b_runtime_present = read_text("pod_b_runtime_present.txt").strip() == "present"

api_log_patterns = count_log_patterns(read_text("api_log_tail.txt"))
pod_a_log_patterns = count_log_patterns(read_text("pod_a_log_tail.txt"))
pod_b_log_patterns = count_log_patterns(read_text("pod_b_log_tail.txt"))
pod_c_log_patterns = count_log_patterns(read_text("pod_c_log_tail.txt"))

ownership_conflicts = int(metrics.get("ownership_conflict_count", 0) or 0)
pod_b_status = state.get("pod_b_status", {}) if isinstance(state.get("pod_b_status", {}), dict) else {}
pod_a_report = pod_report(report, "pod_a") or {}
pod_b_report = pod_report(report, "pod_b") or {}
pod_c_report = pod_report(report, "pod_c") or {}

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

if infra_failures:
    append_stage(
        stage="etape_1_infra_et_collector",
        status="FAIL",
        summary="Le socle dry-run n'est pas sain; corriger l'infra avant d'analyser le trading.",
        deterministic=True,
        checks=infra_checks + infra_failures,
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
    pod_a_checks.append(f"Journal Pod A present ({pod_a_journal['line_count']} lignes)")
else:
    pod_a_checks.append("Journal Pod A absent ou encore vide")

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
        checks=pod_a_checks + pod_a_failures,
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

if container_is_running("trident-pod-b-live"):
    pod_b_checks.append("Pod B tourne")
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

    if pod_b_log_patterns["traceback"] == 0:
        pod_b_checks.append("Pas de traceback recent dans les logs Pod B")
    else:
        pod_b_failures.append("Traceback recent detecte dans les logs Pod B")

    pod_b_prompt = None
    if not pod_b_failures:
        pod_b_prompt = write_prompt(
            "llm_prompt_etape_3_pod_a_plus_pod_b.md",
            "Prompt LLM - Revue cohabitation Pod A + Pod B",
            (
                "Analyse la cohabitation dry-run Pod A + Pod B.\n\n"
                "Objectif: verifier que la coexistence est propre et que Pod B ne montre pas un comportement de range aberrant.\n\n"
                f"Contexte resume:\n"
                f"- ownership_conflict_count: {ownership_conflicts}\n"
                f"- pod_b_process_state: {pod_b_report.get('process_state')}\n"
                f"- pod_b_position_count: {pod_b_report.get('position_count')}\n"
                f"- pod_b_open_order_count: {pod_b_report.get('open_order_count')}\n"
                f"- pod_b_total_fill_count: {pod_b_report.get('total_fill_count')}\n"
                f"- pod_b_realized_pnl_usd: {pod_b_report.get('realized_pnl_usd')}\n"
                f"- pod_b_total_unrealized_pnl_usd: {pod_b_report.get('total_unrealized_pnl_usd')}\n"
                f"- pod_b_runtime_config_present: {pod_b_runtime_present}\n"
                f"- log patterns Pod B: {pod_b_log_patterns}\n\n"
                "Artefacts a lire:\n"
                f"- {raw_dir / 'state.json'}\n"
                f"- {raw_dir / 'metrics.json'}\n"
                f"- {raw_dir / 'report.json'}\n"
                f"- {raw_dir / 'pod_b_log_tail.txt'}\n"
                f"- {raw_dir / 'journal_files.txt'}\n\n"
                "Questions:\n"
                "1. La cohabitation Pod A / Pod B parait-elle saine?\n"
                "2. Y a-t-il des signes d'inventory runaway, de churn de fills, ou d'utilisation incoherente du capital?\n"
                "3. Le PnL et les ordres ouverts de Pod B te paraissent-ils plausibles pour un dry-run encore court?\n"
                "4. Verdict: go pour continuer Pod B, ou no-go avec raisons precises?\n"
            ),
        )

    if pod_b_failures:
        append_stage(
            stage="etape_3_pod_a_plus_pod_b",
            status="FAIL",
            summary="La cohabitation avec Pod B n'est pas saine sur les checks mecaniques.",
            deterministic=True,
            checks=pod_b_checks + pod_b_failures,
        )
    else:
        append_stage(
            stage="etape_3_pod_a_plus_pod_b",
            status="PASS",
            summary="Les checks mecaniques de cohabitation sont verts; une revue qualitative est recommandee.",
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
        checks=["Container pod-b-live absent ou arrete"],
    )


# Etape 4: Pod C optionnel
if container_is_running("trident-pod-c-live"):
    pod_c_checks: list[str] = []
    pod_c_failures: list[str] = []
    if bool(pod_c_report.get("healthy", False)):
        pod_c_checks.append("Supervisor considere Pod C healthy")
    else:
        pod_c_failures.append("Supervisor ne considere pas Pod C healthy")
    if pod_c_log_patterns["traceback"] == 0:
        pod_c_checks.append("Pas de traceback recent dans les logs Pod C")
    else:
        pod_c_failures.append("Traceback recent detecte dans les logs Pod C")

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
            summary="Pod C tourne mais les checks mecaniques ne sont pas satisfaisants.",
            deterministic=True,
            checks=pod_c_checks + pod_c_failures,
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


summary = {
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "host": host,
    "ssh_user": ssh_user,
    "latest_snapshot": latest_snapshot,
    "ownership_conflict_count": ownership_conflicts,
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
    f"- ownership_conflict_count: `{ownership_conflicts}`",
    "",
    "## Stages",
    "",
]
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
