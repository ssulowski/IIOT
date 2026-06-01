#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR/server"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo on an Ubuntu/Debian VM"
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl gnupg

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

if [ ! -f .env ]; then
  cp .env.example .env
  API_KEY_VALUE="$(openssl rand -hex 24 || date +%s)"
  sed -i "s/^API_KEY=.*/API_KEY=$API_KEY_VALUE/" .env
fi

docker compose up -d --build sky-server

echo "Server is running on port 8080."
echo "API key:"
grep '^API_KEY=' .env
echo "For a named Cloudflare Tunnel, set CLOUDFLARE_TUNNEL_TOKEN and run:"
echo "  docker compose --profile tunnel up -d"
