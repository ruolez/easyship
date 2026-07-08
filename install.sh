#!/usr/bin/env bash
#
# EasyShip — all-in-one installer for Ubuntu 24 LTS
#
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/ruolez/easyship/main/install.sh)"
#
# Options: Install (clean) / Update (pull code, keep all data) / Remove
#
set -euo pipefail

REPO_URL="https://github.com/ruolez/easyship.git"
INSTALL_DIR="/opt/easyship"
BACKUP_DIR="/opt/easyship-backups"
DEFAULT_PORT="5557"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || fail "Run this script as root: sudo bash install.sh"
}

compose() {
    docker compose --project-directory "$INSTALL_DIR" "$@"
}

get_env() {
    grep -E "^$1=" "$INSTALL_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true
}

server_ip() {
    hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost"
}

# ---------------------------------------------------------------- docker
install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        ok "Docker with Compose already installed"
        return
    fi
    info "Installing Docker (official repository)..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    ok "Docker installed"
}

# ---------------------------------------------------------------- health
wait_for_health() {
    local port="$1"
    info "Waiting for the application to become healthy..."
    for _ in $(seq 1 45); do
        if curl -fsS "http://localhost:${port}/api/health" >/dev/null 2>&1; then
            ok "Application is up"
            return 0
        fi
        sleep 2
    done
    warn "Health check timed out. Inspect logs with: docker compose --project-directory $INSTALL_DIR logs"
    return 1
}

# ---------------------------------------------------------------- backup
backup_data() {
    if ! compose ps postgres 2>/dev/null | grep -q "Up"; then
        warn "Postgres container is not running — skipping database backup"
        return
    fi
    mkdir -p "$BACKUP_DIR"
    local stamp; stamp="$(date +%Y%m%d-%H%M%S)"
    local dump="$BACKUP_DIR/easyship-db-$stamp.sql.gz"
    info "Backing up database to $dump ..."
    compose exec -T postgres pg_dump -U easyship easyship | gzip > "$dump"
    cp "$INSTALL_DIR/.env" "$BACKUP_DIR/env-$stamp" 2>/dev/null || true
    ok "Backup complete ($(du -h "$dump" | cut -f1))"
}

# ---------------------------------------------------------------- install
do_install() {
    if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
        warn "EasyShip is already installed at $INSTALL_DIR."
        read -r -p "Run an UPDATE instead? [Y/n] " answer
        case "${answer:-Y}" in [Yy]*) do_update; return ;; *) fail "Aborted." ;; esac
    fi

    info "Installing prerequisites..."
    apt-get update -qq
    apt-get install -y -qq git curl openssl ca-certificates
    install_docker

    info "Cloning repository..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"

    local port admin_pass
    read -r -p "Port to expose the app on [${DEFAULT_PORT}]: " port
    port="${port:-$DEFAULT_PORT}"
    read -r -p "Initial admin password [admin]: " admin_pass
    admin_pass="${admin_pass:-admin}"

    info "Generating .env with random secrets..."
    cat > "$INSTALL_DIR/.env" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 16)
SECRET_KEY=$(openssl rand -hex 32)
ADMIN_INITIAL_PASSWORD=${admin_pass}
APP_PORT=${port}
EOF
    chmod 600 "$INSTALL_DIR/.env"

    info "Building and starting containers (first build takes a few minutes)..."
    compose up -d --build

    wait_for_health "$port" || true

    if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
        warn "ufw firewall is active. Allow the app port with: ufw allow ${port}/tcp"
    fi

    echo
    ok "EasyShip installed."
    echo -e "  URL:      ${GREEN}http://$(server_ip):${port}${NC}"
    echo -e "  Login:    ${GREEN}admin${NC} / the password you chose"
    echo -e "  Next:     Settings page → Easyship token, origin address, BackOffice, Shopify stores"
}

# ---------------------------------------------------------------- update
do_update() {
    [ -f "$INSTALL_DIR/docker-compose.yml" ] || fail "No installation found at $INSTALL_DIR — run Install first."
    cd "$INSTALL_DIR"

    backup_data

    info "Pulling latest code..."
    git -C "$INSTALL_DIR" fetch origin main
    git -C "$INSTALL_DIR" reset --hard origin/main

    info "Rebuilding and restarting containers (database and labels are kept)..."
    compose up -d --build

    local port; port="$(get_env APP_PORT)"
    wait_for_health "${port:-$DEFAULT_PORT}" || true

    echo
    ok "EasyShip updated. Settings, users, shipment history and labels were preserved."
    echo -e "  URL: ${GREEN}http://$(server_ip):${port:-$DEFAULT_PORT}${NC}"
}

# ---------------------------------------------------------------- remove
do_remove() {
    [ -d "$INSTALL_DIR" ] || fail "No installation found at $INSTALL_DIR."
    cd "$INSTALL_DIR"

    warn "This stops EasyShip and removes its containers."
    read -r -p "Continue? [y/N] " answer
    case "${answer:-N}" in [Yy]*) ;; *) fail "Aborted." ;; esac

    read -r -p "Create a final database backup first? [Y/n] " answer
    case "${answer:-Y}" in [Yy]*) backup_data ;; esac

    read -r -p "ALSO DELETE all data (database, labels)? This cannot be undone. [y/N] " wipe

    case "${wipe:-N}" in
        [Yy]*)
            compose down -v --rmi local 2>/dev/null || true
            rm -rf "$INSTALL_DIR"
            ok "EasyShip and ALL data removed. Backups (if any) remain in $BACKUP_DIR"
            ;;
        *)
            compose down --rmi local 2>/dev/null || true
            rm -rf "$INSTALL_DIR"
            ok "EasyShip removed. Data volumes kept — a reinstall will reuse them."
            ;;
    esac
}

# ---------------------------------------------------------------- menu
main() {
    require_root
    case "${1:-}" in
        install) do_install; exit 0 ;;
        update)  do_update;  exit 0 ;;
        remove)  do_remove;  exit 0 ;;
    esac

    echo
    echo -e "${BLUE}================================${NC}"
    echo -e "${BLUE}   EasyShip installer (Ubuntu)  ${NC}"
    echo -e "${BLUE}================================${NC}"
    echo "  1) Install (clean)"
    echo "  2) Update  (pull latest code, keep all data)"
    echo "  3) Remove"
    echo "  4) Exit"
    echo
    read -r -p "Choose an option [1-4]: " choice
    case "$choice" in
        1) do_install ;;
        2) do_update ;;
        3) do_remove ;;
        *) echo "Bye." ;;
    esac
}

main "$@"
