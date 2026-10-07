#!/usr/bin/env bash
set -euo pipefail

# Base directory of this project
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="$PROJECT_ROOT/systemd"

TARGETS=(
    "parental-controls.service"
    "parental-controls-enforce.service"
    "parental-controls-enforce.timer"
)

if [[ "$EUID" -ne 0 ]]; then
    echo "This script must be run as root (use sudo)."
    exit 1
fi

for t in "${TARGETS[@]}"; do
    if [[ -f "$SYSTEMD_DIR/$t" ]]; then
        cp "$SYSTEMD_DIR/$t" /etc/systemd/system/
        echo "Installed $t"
    else
        echo "Warning: $SYSTEMD_DIR/$t not found, skipping"
    fi
done

systemctl daemon-reload
systemctl enable --now parental-controls.service
systemctl enable --now parental-controls-enforce.timer

echo "Systemd units installed and enabled."
echo "Check status with:"
echo "  systemctl status parental-controls.service"
echo "  systemctl status parental-controls-enforce.timer"
