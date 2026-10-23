#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# deploy.sh — One-shot VPS setup for Ashburn, VA (IAD) server
# Run as root on a fresh Ubuntu 22.04 LTS instance
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

REPO_DIR="/opt/nft-mint-bot"
LOG_FILE="/var/log/nft-mint-bot-deploy.log"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOG_FILE"; }

log "═══ NFT Mint Bot Deployment — Ashburn VA (IAD) ═══"

# ── 1. System packages ───────────────────────────────────────
log "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    curl git build-essential pkg-config libssl-dev \
    libsecp256k1-dev ca-certificates gnupg lsb-release \
    postgresql-client ufw fail2ban

# ── 2. Docker ────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  log "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

if ! command -v docker-compose &>/dev/null; then
  log "Installing Docker Compose..."
  curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose
  chmod +x /usr/local/bin/docker-compose
fi

# ── 3. Rust toolchain ────────────────────────────────────────
if ! command -v cargo &>/dev/null; then
  log "Installing Rust toolchain..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
  source "$HOME/.cargo/env"
fi

# ── 4. Firewall ──────────────────────────────────────────────
log "Configuring UFW firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   # SSH
ufw --force enable

# 7878 and 7879 are bound to 127.0.0.1 only — no external exposure needed

# ── 5. Copy project files ────────────────────────────────────
log "Setting up project directory at $REPO_DIR..."
mkdir -p "$REPO_DIR"
cp -r . "$REPO_DIR/"
cd "$REPO_DIR"

# ── 6. Environment setup ─────────────────────────────────────
if [ ! -f "$REPO_DIR/.env" ]; then
  log "Creating .env from template..."
  cp .env.example .env
  log "⚠️  IMPORTANT: Edit $REPO_DIR/.env before starting the bot!"
  log "    Required: BOT_TOKEN, DB_PASSWORD, FERNET_KEY, ADMIN_TELEGRAM_ID"
fi

# ── 7. Build and start ───────────────────────────────────────
log "Building and starting services..."
docker-compose pull postgres  || true
docker-compose build --no-cache
docker-compose up -d

# ── 8. Wait for postgres and verify ─────────────────────────
log "Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
  if docker-compose exec -T postgres pg_isready -U nftbot -d nftbot_db &>/dev/null; then
    log "PostgreSQL is ready."
    break
  fi
  sleep 2
done

# ── 9. Systemd service for auto-restart ─────────────────────
log "Installing systemd service..."
cat > /etc/systemd/system/nft-mint-bot.service << EOF
[Unit]
Description=NFT Mint Bot
Requires=docker.service
After=docker.service

[Service]
WorkingDirectory=$REPO_DIR
ExecStart=/usr/local/bin/docker-compose up
ExecStop=/usr/local/bin/docker-compose down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable nft-mint-bot

# ── 10. Network latency check ────────────────────────────────
log "Checking latency to key endpoints..."
log "  OpenSea gql: $(curl -o /dev/null -s -w '%{time_connect}s connect, %{time_total}s total\n' https://gql.opensea.io/graphql || echo 'unreachable')"
log "  Base RPC:    $(curl -o /dev/null -s -w '%{time_connect}s connect, %{time_total}s total\n' https://mainnet.base.org || echo 'unreachable')"

log "═══ Deployment complete! ═══"
log "  Services: docker-compose ps"
log "  Logs:     docker-compose logs -f"
log "  Restart:  docker-compose restart"
log ""
log "  ⚠️  Edit /opt/nft-mint-bot/.env then run: docker-compose restart"
