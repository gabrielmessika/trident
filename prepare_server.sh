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
Usage: ./prepare_server.sh <host-or-ip> [--user root] [--identity ~/.ssh/trident_hetzner_ed25519]

Prépare un serveur Ubuntu 24/22 pour TRIDENT :
- Docker + docker compose plugin
- fail2ban + ufw
- utilisateur trident-deploy
- /opt/trident/{data,logs,runtime}
- SSH durci (clé uniquement)
- mises à jour de sécurité
EOF
}

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

TARGET="$1"
shift

SSH_USER="root"
IDENTITY_FILE="${HOME}/.ssh/trident_hetzner_ed25519"
DEPLOY_USER="trident-deploy"
APP_DIR="/opt/trident"

while [ $# -gt 0 ]; do
    case "$1" in
        --user)
            SSH_USER="$2"
            shift 2
            ;;
        --identity)
            IDENTITY_FILE="$2"
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

if [ ! -f "$IDENTITY_FILE" ]; then
    error "Clé SSH introuvable: $IDENTITY_FILE"
    exit 1
fi

ssh_root() {
    ssh -i "$IDENTITY_FILE" -o StrictHostKeyChecking=accept-new "${SSH_USER}@${TARGET}" "$@"
}

echo ""
echo "========================================="
echo "  TRIDENT — Préparation du serveur"
echo "  Serveur : ${TARGET}"
echo "========================================="
echo ""

info "Préparation du serveur ${TARGET}..."

ssh_root bash <<'SETUP_SCRIPT'
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo ">>> Mise à jour des paquets..."
apt-get update -qq
apt-get upgrade -y -qq

echo ">>> Installation des utilitaires de base..."
apt-get install -y -qq ca-certificates curl gnupg rsync fail2ban ufw unattended-upgrades

echo ">>> Installation de Docker..."
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    echo "  Docker installé."
else
    echo "  Docker déjà installé."
fi

echo ">>> Configuration du firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
# L'API TRIDENT est publiée sur 127.0.0.1 par défaut. Ouvrir 80/443
# seulement si un reverse proxy HTTP(S) est configuré explicitement.
ufw --force enable

echo ">>> Activation de fail2ban..."
systemctl enable --now fail2ban

echo ">>> Création de l'utilisateur trident-deploy..."
if ! id trident-deploy >/dev/null 2>&1; then
    useradd -m -s /bin/bash trident-deploy
    usermod -aG docker trident-deploy
    mkdir -p /home/trident-deploy/.ssh
    cp /root/.ssh/authorized_keys /home/trident-deploy/.ssh/authorized_keys
    chown -R trident-deploy:trident-deploy /home/trident-deploy/.ssh
    chmod 700 /home/trident-deploy/.ssh
    chmod 600 /home/trident-deploy/.ssh/authorized_keys
    echo "  Utilisateur trident-deploy créé."
else
    usermod -aG docker trident-deploy || true
    echo "  Utilisateur trident-deploy existe déjà."
fi

echo ">>> Préparation de /opt/trident..."
mkdir -p /opt/trident/{data,logs,runtime,docs,scripts}
chown -R trident-deploy:trident-deploy /opt/trident

echo ">>> Sécurisation SSH..."
sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

echo ">>> Limites système..."
cat > /etc/sysctl.d/99-trident.conf <<'SYSCTL'
vm.overcommit_memory=1
fs.file-max=1000000
SYSCTL
sysctl -p /etc/sysctl.d/99-trident.conf >/dev/null 2>&1 || true

cat > /etc/security/limits.d/99-trident.conf <<'LIMITS'
trident-deploy soft nofile 65536
trident-deploy hard nofile 65536
LIMITS

echo 'Unattended-Upgrade::Automatic-Reboot "false";' > /etc/apt/apt.conf.d/51custom-unattended

echo ""
echo ">>> Préparation serveur terminée."
SETUP_SCRIPT

ok "Serveur préparé avec succès"
echo ""
echo "Prochaines étapes :"
echo "  1. Déployer le code :"
echo "     ./deploy.sh --host ${TARGET} --start"
echo ""
echo "  2. Se connecter au serveur :"
echo "     ssh -i ${IDENTITY_FILE} trident-deploy@${TARGET}"
echo ""
echo "  3. Contrôler le bot sur le serveur :"
echo "     cd ${APP_DIR}"
echo "     ./scripts/trident_server.sh status"
echo ""
