#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo"
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="${SUDO_USER:-pi}"
SERVICE_FILE="/etc/systemd/system/sky-watcher.service"

cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Sky Watcher IIoT recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/edge/sky_watcher.py --config $PROJECT_DIR/config/pi_config.yaml
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
echo "Installed $SERVICE_FILE"
echo "Enable with: sudo systemctl enable --now sky-watcher.service"
