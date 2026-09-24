#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "   Updating NFT Mint Bot Services"
echo "=========================================="

# 1. Pull latest git commits if this is a git repo
if [ -d ".git" ]; then
    echo "[1/3] Pulling latest code changes from Git..."
    git pull
else
    echo "[1/3] Not a Git repo, skipping git pull."
fi

# 2. Rebuild updated containers
echo "[2/3] Rebuilding Docker containers..."
docker compose build rust-engine telegram-bot

# 3. Restart services
echo "[3/3] Restarting services with updated code..."
docker compose up -d rust-engine telegram-bot

echo "=========================================="
echo "Update complete! Status:"
docker compose ps
echo "=========================================="
echo "View real-time logs with: docker compose logs -f"
