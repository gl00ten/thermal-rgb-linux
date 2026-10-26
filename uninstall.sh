#!/usr/bin/env bash
set -euo pipefail
CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
UNITS="$CONFIG_BASE/systemd/user"
APP="$HOME/.local/share/thermal-rgb"
systemctl --user disable --now thermal-rgb.service openrgb-local.service 2>/dev/null || true
rm -f "$UNITS/thermal-rgb.service" "$UNITS/openrgb-local.service"
systemctl --user daemon-reload
rm -rf -- "$APP"
echo 'Temperature RGB services/application removed. OpenRGB and NVIDIA drivers are untouched.'
if [[ "${1:-}" == "--purge-config" ]]; then
  rm -rf -- "$CONFIG_BASE/thermal-rgb"
  echo 'Removed user configuration.'
else
  echo "Preserved user configuration at $CONFIG_BASE/thermal-rgb"
fi
echo 'User lingering setting was not modified. Previous service backups remain in systemd/user.'
