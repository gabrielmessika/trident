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
Usage: ./scripts/fetch_trident_data.sh [options]

Rapatrie les donnees utiles du serveur TRIDENT pour analyse locale, puis
peut lancer automatiquement la revue locale avec suggestions de prompts LLM.

Modes principaux:
  (aucun flag)          Snapshots des dernieres 24h + logs/runtime courants + revue
  --all                 Tous les snapshots live + logs/runtime courants + revue
  --date YYYY-MM-DD     Snapshots d'une date precise + logs/runtime courants + revue
  --days N              Snapshots des N derniers jours + logs/runtime courants + revue
  --logs-only           Uniquement logs/runtime/API courants
  --snapshots-only      Uniquement snapshots live (et revue optionnelle)
  --review-only         Ne rapatrie rien, relance seulement la revue distante

Options:
  --host <host>                 Host SSH. Defaut: trident-hetzner
  --user <user>                 User SSH. Defaut: trident-deploy
  --identity <path>             Cle SSH. Defaut: ~/.ssh/trident_hetzner_ed25519
  --remote-dir <path>           Repertoire TRIDENT sur le serveur. Defaut: /opt/trident
  --local-dir <path>            Dossier local de sortie. Defaut: ./server-data
  --output-dir <path>           Dossier de revue local. Defaut: <local-dir>/reviews/<timestamp>
  --log-lines N                 Nombre de lignes de logs Docker a rapatrier. Defaut: 300
  --snapshot-max-age-minutes N  Seuil de fraicheur pour la revue. Defaut: 15
  --skip-review                 Ne lance pas la revue locale apres fetch
  --dry-run                     Affiche ce qui serait fait sans telecharger
  -h, --help                    Affiche cette aide
EOF
}

MODE="recent"
DATE_FILTER=""
DAYS=1
LOGS_ONLY=""
SNAPSHOTS_ONLY=""
REVIEW_ONLY=""
SKIP_REVIEW=""
DRY_RUN=""
LOG_LINES=300
SNAPSHOT_MAX_AGE_MINUTES=15

HOST="${TRIDENT_DEPLOY_HOST:-trident-hetzner}"
SSH_USER="${TRIDENT_DEPLOY_USER:-trident-deploy}"
IDENTITY_FILE="${TRIDENT_DEPLOY_IDENTITY:-${HOME}/.ssh/trident_hetzner_ed25519}"
REMOTE_DIR="${TRIDENT_DEPLOY_DIR:-/opt/trident}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TIMESTAMP_UTC="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_DIR="${ROOT_DIR}/server-data"
OUTPUT_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all) MODE="all"; shift ;;
        --date) MODE="date"; DATE_FILTER="$2"; shift 2 ;;
        --days) MODE="days"; DAYS="$2"; shift 2 ;;
        --logs-only) LOGS_ONLY="true"; shift ;;
        --snapshots-only) SNAPSHOTS_ONLY="true"; shift ;;
        --review-only) REVIEW_ONLY="true"; shift ;;
        --skip-review) SKIP_REVIEW="true"; shift ;;
        --dry-run) DRY_RUN="true"; shift ;;
        --host) HOST="$2"; shift 2 ;;
        --user) SSH_USER="$2"; shift 2 ;;
        --identity) IDENTITY_FILE="$2"; shift 2 ;;
        --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
        --local-dir) LOCAL_DIR="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --log-lines) LOG_LINES="$2"; shift 2 ;;
        --snapshot-max-age-minutes) SNAPSHOT_MAX_AGE_MINUTES="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) error "Option inconnue: $1"; usage; exit 1 ;;
    esac
done

OUTPUT_DIR="${OUTPUT_DIR:-${LOCAL_DIR}/reviews/${TIMESTAMP_UTC}}"
RAW_DIR="${LOCAL_DIR}/raw/${TIMESTAMP_UTC}"
SNAPSHOT_DIR="${LOCAL_DIR}/live_snapshots"
FUNDING_DIR="${LOCAL_DIR}/funding_history"
LOG_DIR="${LOCAL_DIR}/logs"
API_DIR="${LOCAL_DIR}/api"
RUNTIME_DIR="${LOCAL_DIR}/runtime"
DOCKER_DIR="${LOCAL_DIR}/docker"
HYDRA_DOCS_DIR="${LOCAL_DIR}/hydra_docs"
REPLAY_INPUT_DIR="${LOCAL_DIR}/replay_inputs"
FULL_BOT_REPLAY_INPUT="${REPLAY_INPUT_DIR}/full_bot_latest_fetch.jsonl"

mkdir -p "${RAW_DIR}" "${SNAPSHOT_DIR}" "${FUNDING_DIR}" "${LOG_DIR}" "${API_DIR}" "${RUNTIME_DIR}" "${DOCKER_DIR}" "${HYDRA_DOCS_DIR}" "${REPLAY_INPUT_DIR}" "${OUTPUT_DIR}"

SSH_CONTROL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trident-fetch-XXXXXX")"
SSH_CONTROL_PATH="${SSH_CONTROL_DIR}/cm-%C"

SSH_ARGS=()
if [[ -f "${IDENTITY_FILE}" ]]; then
    SSH_ARGS+=(-i "${IDENTITY_FILE}")
else
    warn "Cle SSH absente: ${IDENTITY_FILE}. Utilisation de la config SSH systeme uniquement."
fi
SSH_ARGS+=(
    -o BatchMode=yes
    -o ConnectTimeout=10
    -o ConnectionAttempts=3
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=3
    -o TCPKeepAlive=yes
    -o ControlMaster=auto
    -o ControlPersist=120
    -o ControlPath="${SSH_CONTROL_PATH}"
)
SSH_TARGET="${SSH_USER}@${HOST}"
RSYNC_BIN=()

detect_sudo_prefix() {
    if [[ "$(id -u)" -eq 0 ]]; then
        return 0
    fi

    if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
        printf 'sudo -n'
        return 0
    fi

    return 1
}

run_install_command() {
    local requires_privilege="$1"
    shift

    if [[ "${requires_privilege}" == "true" ]]; then
        local sudo_prefix=""
        if ! sudo_prefix="$(detect_sudo_prefix)"; then
            error "rsync est requis mais introuvable, et aucune elevation non interactive n'est disponible."
            error "Installe rsync manuellement puis relance le script."
            return 1
        fi

        if ! ${sudo_prefix} "$@"; then
            return 1
        fi
        return 0
    fi

    "$@"
}

install_rsync_if_missing() {
    if command -v rsync >/dev/null 2>&1; then
        RSYNC_BIN=(rsync)
        return 0
    fi

    warn "rsync absent localement. Tentative d'installation automatique..."

    if command -v dnf >/dev/null 2>&1; then
        info "Installation de rsync via dnf..."
        if ! run_install_command true dnf install -y rsync; then
            warn "La commande dnf a retourne un code non nul; verification de rsync apres tentative..."
        fi
    elif command -v yum >/dev/null 2>&1; then
        info "Installation de rsync via yum..."
        if ! run_install_command true yum install -y rsync; then
            warn "La commande yum a retourne un code non nul; verification de rsync apres tentative..."
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        info "Installation de rsync via apt-get..."
        if ! run_install_command true apt-get update -qq; then
            warn "La mise a jour APT a retourne un code non nul; verification de rsync apres tentative..."
        fi
        if ! command -v rsync >/dev/null 2>&1; then
            if ! run_install_command true apt-get install -y -qq rsync; then
                warn "La commande apt-get install a retourne un code non nul; verification de rsync apres tentative..."
            fi
        fi
    elif command -v apk >/dev/null 2>&1; then
        info "Installation de rsync via apk..."
        if ! run_install_command true apk add --no-cache rsync; then
            warn "La commande apk a retourne un code non nul; verification de rsync apres tentative..."
        fi
    elif command -v pacman >/dev/null 2>&1; then
        info "Installation de rsync via pacman..."
        if ! run_install_command true pacman -Sy --noconfirm rsync; then
            warn "La commande pacman a retourne un code non nul; verification de rsync apres tentative..."
        fi
    elif command -v zypper >/dev/null 2>&1; then
        info "Installation de rsync via zypper..."
        if ! run_install_command true zypper --non-interactive install rsync; then
            warn "La commande zypper a retourne un code non nul; verification de rsync apres tentative..."
        fi
    elif command -v brew >/dev/null 2>&1; then
        info "Installation de rsync via Homebrew..."
        if ! run_install_command false brew install rsync; then
            warn "La commande brew a retourne un code non nul; verification de rsync apres tentative..."
        fi
    else
        error "rsync est requis mais aucun gestionnaire de paquets supporte n'a ete detecte."
        return 1
    fi

    if ! command -v rsync >/dev/null 2>&1; then
        error "rsync reste introuvable apres installation."
        return 1
    fi

    RSYNC_BIN=(rsync)
    ok "rsync installe automatiquement"
}

build_ssh_transport() {
    local parts=()
    local arg quoted
    for arg in ssh "${SSH_ARGS[@]}"; do
        printf -v quoted '%q' "${arg}"
        parts+=("${quoted}")
    done
    printf '%s ' "${parts[@]}"
}

RSYNC_SSH_CMD="$(build_ssh_transport)"
RSYNC_SSH_CMD="${RSYNC_SSH_CMD% }"

ssh_remote() {
    ssh "${SSH_ARGS[@]}" "${SSH_TARGET}" "$@"
}

rsync_remote() {
    "${RSYNC_BIN[@]}" "$@" -e "${RSYNC_SSH_CMD}"
}

retry_command() {
    local attempts="$1"
    local sleep_seconds="$2"
    shift 2
    local attempt rc=0

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if "$@"; then
            return 0
        else
            rc=$?
        fi
        if (( attempt < attempts )); then
            warn "Tentative ${attempt}/${attempts} echouee (code ${rc}), nouvelle tentative dans ${sleep_seconds}s..."
            sleep "${sleep_seconds}"
        fi
    done

    return "${rc}"
}

copy_remote_file_via_ssh() {
    local remote_path="$1"
    local local_path="$2"

    local tmp_file
    tmp_file="$(mktemp)"
    if ssh_remote "cat '${REMOTE_DIR}/${remote_path}'" > "${tmp_file}" 2>/dev/null; then
        mv "${tmp_file}" "${local_path}"
        return 0
    fi

    rm -f "${tmp_file}"
    return 1
}

close_ssh_master() {
    ssh "${SSH_ARGS[@]}" -O exit "${SSH_TARGET}" >/dev/null 2>&1 || true
    rm -rf "${SSH_CONTROL_DIR}"
}

start_ssh_master() {
    ssh "${SSH_ARGS[@]}" -o ControlMaster=yes -Nf "${SSH_TARGET}" >/dev/null 2>&1
}

trap close_ssh_master EXIT

if ! install_rsync_if_missing; then
    exit 1
fi

if [[ "${REVIEW_ONLY}" != "true" ]]; then
    if ! retry_command 2 1 start_ssh_master; then
        error "Impossible d'ouvrir une connexion SSH persistante vers ${SSH_TARGET}."
        exit 1
    fi
    if ! ssh_remote true 2>/dev/null; then
        error "Impossible de se connecter a ${SSH_TARGET}."
        exit 1
    fi
fi

build_snapshot_filter() {
    local filter_file
    filter_file="$(mktemp)"
    case "${MODE}" in
        all)
            echo "+ *" > "${filter_file}"
            ;;
        date)
            echo "+ */" > "${filter_file}"
            echo "+ *${DATE_FILTER}*" >> "${filter_file}"
            echo "- *" >> "${filter_file}"
            ;;
        days)
            echo "+ */" > "${filter_file}"
            for i in $(seq 0 $((DAYS - 1))); do
                d="$(date -u -d "-${i} days" +%Y-%m-%d 2>/dev/null || date -u -v-"${i}"d +%Y-%m-%d 2>/dev/null)"
                echo "+ *${d}*" >> "${filter_file}"
            done
            echo "- *" >> "${filter_file}"
            ;;
        recent)
            local today yesterday
            today="$(date -u +%Y-%m-%d)"
            yesterday="$(date -u -d "-1 day" +%Y-%m-%d 2>/dev/null || date -u -v-1d +%Y-%m-%d 2>/dev/null)"
            echo "+ */" > "${filter_file}"
            echo "+ *${today}*" >> "${filter_file}"
            echo "+ *${yesterday}*" >> "${filter_file}"
            echo "- *" >> "${filter_file}"
            ;;
        *)
            echo "+ *" > "${filter_file}"
            ;;
    esac
    echo "${filter_file}"
}

fetch_api_snapshot() {
    info "Rapatriement des snapshots API courants..."
    local ts
    ts="$(date -u +%Y-%m-%d_%H%M%S)"
    local commands=(
        "curl -fsS http://127.0.0.1:3000/health"
        "curl -fsS http://127.0.0.1:3000/api/state"
        "curl -fsS http://127.0.0.1:3000/api/metrics"
        "curl -fsS http://127.0.0.1:3000/api/report"
    )
    local names=(
        "health-${ts}.json"
        "state-${ts}.json"
        "metrics-${ts}.json"
        "report-${ts}.json"
    )

    if [[ "${DRY_RUN}" == "true" ]]; then
        printf '  [dry-run] %s -> %s\n' "API snapshots" "${API_DIR}/"
        return
    fi

    local i
    for i in "${!commands[@]}"; do
        if ssh_remote "bash -lc $(printf '%q' "cd '${REMOTE_DIR}' && ${commands[$i]}")" > "${API_DIR}/${names[$i]}" 2>/dev/null; then
            :
        else
            warn "Impossible de recuperer ${names[$i]} (API inaccessible ?)"
            rm -f "${API_DIR}/${names[$i]}"
        fi
    done
    ok "Snapshots API sauvegardes dans ${API_DIR}/"
}

fetch_remote_file() {
    local remote_path="$1"
    local local_path="$2"
    local label="$3"

    if [[ "${DRY_RUN}" == "true" ]]; then
        printf '  [dry-run] %s -> %s\n' "${remote_path}" "${local_path}"
        return
    fi

    mkdir -p "$(dirname "${local_path}")"
    if ssh_remote "test -f '${REMOTE_DIR}/${remote_path}'" 2>/dev/null; then
        if retry_command 3 2 rsync_remote -azP "${SSH_TARGET}:${REMOTE_DIR}/${remote_path}" "${local_path}"; then
            ok "${label} rapatrie"
        elif retry_command 2 1 copy_remote_file_via_ssh "${remote_path}" "${local_path}"; then
            warn "${label} rapatrie via fallback SSH simple"
        else
            warn "Impossible de rapatrier ${label} (${remote_path})"
        fi
    else
        warn "${label} absent sur le serveur (${remote_path})"
    fi
}

fetch_optional_remote_file() {
    local remote_path="$1"
    local local_path="$2"
    local label="$3"

    if [[ "${DRY_RUN}" == "true" ]]; then
        printf '  [dry-run] %s -> %s\n' "${remote_path}" "${local_path}"
        return
    fi

    mkdir -p "$(dirname "${local_path}")"
    if ssh_remote "test -f '${REMOTE_DIR}/${remote_path}'" 2>/dev/null; then
        if retry_command 3 2 rsync_remote -azP "${SSH_TARGET}:${REMOTE_DIR}/${remote_path}" "${local_path}"; then
            ok "${label} rapatrie"
        elif retry_command 2 1 copy_remote_file_via_ssh "${remote_path}" "${local_path}"; then
            warn "${label} rapatrie via fallback SSH simple"
        else
            warn "Impossible de rapatrier ${label} (${remote_path})"
        fi
    else
        info "${label} absent sur le serveur (${remote_path}, optionnel)"
    fi
}

fetch_snapshots() {
    info "Rapatriement des snapshots live..."
    local remote_snapshot_dir="${REMOTE_DIR}/data/live_snapshots/"

    if ! ssh_remote "test -d '${REMOTE_DIR}/data/live_snapshots'" 2>/dev/null; then
        warn "Dossier data/live_snapshots absent sur le serveur"
        return
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        local filter_file
        if [[ "${MODE}" == "all" ]]; then
            printf '  [dry-run] %s -> %s\n' "${remote_snapshot_dir}" "${SNAPSHOT_DIR}/"
        else
            filter_file="$(build_snapshot_filter)"
            rsync_remote -azP -n --filter="merge ${filter_file}" "${SSH_TARGET}:${remote_snapshot_dir}" "${SNAPSHOT_DIR}/"
            rm -f "${filter_file}"
        fi
        return
    fi

    if [[ "${MODE}" == "all" ]]; then
        retry_command 3 2 rsync_remote -azP "${SSH_TARGET}:${remote_snapshot_dir}" "${SNAPSHOT_DIR}/"
    else
        local filter_file
        filter_file="$(build_snapshot_filter)"
        retry_command 3 2 rsync_remote -azP --filter="merge ${filter_file}" "${SSH_TARGET}:${remote_snapshot_dir}" "${SNAPSHOT_DIR}/"
        rm -f "${filter_file}"
    fi
    local snapshot_count
    snapshot_count="$(find "${SNAPSHOT_DIR}" -maxdepth 1 -type f -name '*.jsonl' | wc -l | tr -d ' ')"
    ok "Snapshots live rapatries (${snapshot_count} fichier(s) locaux)"
}

fetch_funding_history() {
    info "Rapatriement de l'historique funding/OI..."
    local remote_funding_dir="${REMOTE_DIR}/data/funding_history/"

    if ! ssh_remote "test -d '${REMOTE_DIR}/data/funding_history'" 2>/dev/null; then
        warn "Dossier data/funding_history absent sur le serveur"
        return
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        printf '  [dry-run] %s -> %s\n' "${remote_funding_dir}" "${FUNDING_DIR}/"
        return
    fi

    retry_command 3 2 rsync_remote -azP "${SSH_TARGET}:${remote_funding_dir}" "${FUNDING_DIR}/"
    local funding_count
    funding_count="$(find "${FUNDING_DIR}" -maxdepth 1 -type f -name '*.jsonl' | wc -l | tr -d ' ')"
    ok "Funding/OI rapatrie (${funding_count} fichier(s) locaux)"
}

prepare_backtest_inputs() {
    info "Preparation d'un input local pret pour full_bot_replay..."

    local snapshot_files
    snapshot_files="$(find "${SNAPSHOT_DIR}" -maxdepth 1 -type f -name '*.jsonl' | sort)"
    if [[ -z "${snapshot_files}" ]]; then
        warn "Aucun snapshot local disponible pour preparer un input de backtest"
        rm -f "${FULL_BOT_REPLAY_INPUT}"
        return
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        printf '  [dry-run] concat snapshots -> %s\n' "${FULL_BOT_REPLAY_INPUT}"
        return
    fi

    find "${SNAPSHOT_DIR}" -maxdepth 1 -type f -name '*.jsonl' | sort | while IFS= read -r file_path; do
        cat "${file_path}"
    done > "${FULL_BOT_REPLAY_INPUT}"

    local merged_lines
    merged_lines="$(wc -l < "${FULL_BOT_REPLAY_INPUT}" | tr -d ' ')"
    ok "Input full-bot prepare (${merged_lines} lignes): ${FULL_BOT_REPLAY_INPUT}"
}

fetch_logs_and_runtime() {
    info "Rapatriement des logs runtime et statuses..."
    fetch_remote_file "logs/pod_a_live.jsonl" "${LOG_DIR}/pod_a_live.jsonl" "Journal Pod A"
    fetch_remote_file "logs/pod_b_live.jsonl" "${LOG_DIR}/pod_b_live.jsonl" "Journal Pod B"
    fetch_remote_file "logs/pod_c_live.jsonl" "${LOG_DIR}/pod_c_live.jsonl" "Journal Pod C"
    fetch_remote_file "logs/pod_a_live_status.json" "${RUNTIME_DIR}/pod_a_live_status.json" "Runtime status Pod A"
    fetch_remote_file "logs/pod_b_live_status.json" "${RUNTIME_DIR}/pod_b_live_status.json" "Runtime status Pod B"
    fetch_remote_file "logs/pod_c_live_status.json" "${RUNTIME_DIR}/pod_c_live_status.json" "Runtime status Pod C"
    fetch_remote_file "logs/funding_collector_status.json" "${RUNTIME_DIR}/funding_collector_status.json" "Runtime status Funding Collector"
    fetch_remote_file "logs/tradfi_funding_collector_status.json" "${RUNTIME_DIR}/tradfi_funding_collector_status.json" "Runtime status Tradfi Funding Collector"
    fetch_optional_remote_file "docs/pod_funding_research_latest.json" "${HYDRA_DOCS_DIR}/pod_funding_research_latest.json" "Research funding JSON"
    fetch_optional_remote_file "docs/pod_funding_research_latest.md" "${HYDRA_DOCS_DIR}/pod_funding_research_latest.md" "Research funding Markdown"
    fetch_optional_remote_file "docs/pod_liq_research_latest.json" "${HYDRA_DOCS_DIR}/pod_liq_research_latest.json" "Research liq JSON"
    fetch_optional_remote_file "docs/pod_liq_research_latest.md" "${HYDRA_DOCS_DIR}/pod_liq_research_latest.md" "Research liq Markdown"
}

fetch_docker_logs() {
    info "Rapatriement des tails de logs Docker..."
    local services=("trident-api" "pod-a-live" "pod-b-live" "pod-c-live" "tradfi-funding-collector" "funding-collector")
    local files=("trident-api.log" "pod-a-live.log" "pod-b-live.log" "pod-c-live.log" "tradfi-funding-collector.log" "funding-collector.log")
    local i

    for i in "${!services[@]}"; do
        if [[ "${DRY_RUN}" == "true" ]]; then
            printf '  [dry-run] docker compose logs --tail %s %s -> %s/%s\n' "${LOG_LINES}" "${services[$i]}" "${DOCKER_DIR}" "${files[$i]}"
            continue
        fi

        if ssh_remote "bash -lc $(printf '%q' "cd '${REMOTE_DIR}' && docker compose -f docker-compose.trident.yml logs --tail ${LOG_LINES} ${services[$i]} 2>&1")" > "${DOCKER_DIR}/${files[$i]}" 2>/dev/null; then
            ok "Logs Docker ${services[$i]} rapatries"
        else
            warn "Impossible de recuperer les logs Docker ${services[$i]}"
            rm -f "${DOCKER_DIR}/${files[$i]}"
        fi
    done
}

run_review() {
    if [[ "${SKIP_REVIEW}" == "true" ]]; then
        warn "Revue locale skippee (--skip-review)"
        return
    fi

    info "Lancement de la revue locale dry-run..."
    local review_cmd=(
        "${ROOT_DIR}/scripts/trident_dry_run_review.sh"
        --host "${HOST}"
        --user "${SSH_USER}"
        --identity "${IDENTITY_FILE}"
        --remote-dir "${REMOTE_DIR}"
        --local-dir "${LOCAL_DIR}"
        --output-dir "${OUTPUT_DIR}"
        --snapshot-max-age-minutes "${SNAPSHOT_MAX_AGE_MINUTES}"
        --log-lines "${LOG_LINES}"
    )

    if [[ "${DRY_RUN}" == "true" ]]; then
        printf '  [dry-run] %q ' "${review_cmd[@]}"
        printf '\n'
        return
    fi

    "${review_cmd[@]}"
}

echo
echo "========================================="
echo "  TRIDENT — Fetch local + review"
echo "========================================="
echo
echo "  Host SSH : ${SSH_TARGET}"
echo "  Remote   : ${REMOTE_DIR}"
echo "  Local    : ${LOCAL_DIR}"
echo "  Review   : ${OUTPUT_DIR}"
echo

if [[ "${REVIEW_ONLY}" == "true" ]]; then
    run_review
    exit 0
fi

if [[ "${LOGS_ONLY}" == "true" && "${SNAPSHOTS_ONLY}" == "true" ]]; then
    error "Impossible de combiner --logs-only et --snapshots-only"
    exit 1
fi

fetch_api_snapshot

if [[ "${LOGS_ONLY}" != "true" ]]; then
    fetch_snapshots
    fetch_funding_history
    prepare_backtest_inputs
fi

if [[ "${SNAPSHOTS_ONLY}" != "true" ]]; then
    fetch_logs_and_runtime
    fetch_docker_logs
fi

run_review

echo
echo "========================================="
ok "FETCH TERMINE"
echo "========================================="
echo

if [[ "${DRY_RUN}" != "true" ]]; then
    total_size="$(du -sh "${LOCAL_DIR}" 2>/dev/null | awk '{print $1}')"
    total_files="$(find "${LOCAL_DIR}" -type f 2>/dev/null | wc -l | tr -d ' ')"
    echo "  Dossier : ${LOCAL_DIR}"
    echo "  Fichiers : ${total_files}"
    echo "  Taille : ${total_size}"
    echo
    echo "  Artefacts principaux :"
    echo "    - snapshots live : ${SNAPSHOT_DIR}"
    echo "    - funding history : ${FUNDING_DIR}"
    echo "    - logs applicatifs : ${LOG_DIR}"
    echo "    - runtime statuses : ${RUNTIME_DIR}"
    echo "    - hydra docs : ${HYDRA_DOCS_DIR}"
    echo "    - API snapshots : ${API_DIR}"
    echo "    - docker logs : ${DOCKER_DIR}"
    if [[ -f "${FULL_BOT_REPLAY_INPUT}" ]]; then
        echo "    - input full-bot pret : ${FULL_BOT_REPLAY_INPUT}"
    fi
    if [[ "${SKIP_REVIEW}" != "true" ]]; then
        echo "    - review summary : ${OUTPUT_DIR}/review_summary.md"
        echo "    - review json : ${OUTPUT_DIR}/review_summary.json"
    fi
    echo
    if [[ -f "${FULL_BOT_REPLAY_INPUT}" ]]; then
        echo "  Commandes utiles :"
        echo "    uv run python -m app.backtest.full_bot_replay --config config/trident.toml --input ${SNAPSHOT_DIR}"
        echo "    uv run python -m app.backtest.full_bot_replay --config config/trident.toml --input ${FULL_BOT_REPLAY_INPUT}"
        echo
    fi
fi
