#!/usr/bin/env bash
# Pull the latest version from GitHub, update deps, and restart the bot.
# Run from the repo root: ./deploy/update.sh

set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Pulling latest from origin/main…"
git fetch --quiet origin
git reset --hard origin/main

echo "==> Updating Python dependencies…"
.venv/bin/pip install --upgrade --quiet -r requirements.txt

echo "==> Restarting service…"
sudo systemctl restart polymarket-mlb-bot

echo "==> Status:"
sudo systemctl status polymarket-mlb-bot --no-pager --lines=5
