#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r edge/requirements.txt

mkdir -p recordings/unsent

if [ ! -f config/pi_config.yaml ]; then
  cp config/pi_config.example.yaml config/pi_config.yaml
  echo "Created config/pi_config.yaml - edit upload.server_url and upload.api_key"
fi

echo "Raspberry Pi environment installed."
