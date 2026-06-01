#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR/server"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created server/.env - edit API_KEY if needed."
fi

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

set -a
source .env
set +a

exec uvicorn app.main:app --host 0.0.0.0 --port 8080
