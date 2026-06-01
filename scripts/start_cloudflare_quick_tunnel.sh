#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR/server"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker first or run the FastAPI server manually."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created server/.env - edit API_KEY before exposing the server."
fi

docker compose up -d --build sky-server

if command -v cloudflared >/dev/null 2>&1; then
  exec cloudflared tunnel --url http://localhost:8080
fi

echo "cloudflared is not installed."
echo "Install it from Cloudflare docs, then rerun this script:"
echo "  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
exit 1
