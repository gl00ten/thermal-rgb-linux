#!/usr/bin/env bash
set -euo pipefail
config="${XDG_CONFIG_HOME:-$HOME/.config}/thermal-rgb/config.toml"
case "${1:-}" in
  start) systemctl --user start openrgb-local.service thermal-rgb.service ;;
  stop) systemctl --user stop thermal-rgb.service openrgb-local.service ;;
  restart) systemctl --user restart openrgb-local.service thermal-rgb.service ;;
  status) systemctl --user --no-pager status openrgb-local.service thermal-rgb.service ;;
  logs) journalctl --user -b -u openrgb-local.service -u thermal-rgb.service -f ;;
  config) "${EDITOR:-nano}" "$config" ;;
  sensors) "$HOME/.local/share/thermal-rgb/.venv/bin/python" "$HOME/.local/share/thermal-rgb/thermal_rgb.py" --list-sensors ;;
  diagnostics)
    systemctl --user kill --kill-whom=main --signal=USR1 thermal-rgb.service
    echo 'Summary appears on the next sample (within sensor_interval):'
    journalctl --user -u thermal-rgb.service --no-pager -n 10
    ;;
  doctor)
    echo '=== Services ==='
    systemctl --user --no-pager status openrgb-local.service thermal-rgb.service || true
    echo '=== Startup ==='
    systemctl --user is-enabled openrgb-local.service thermal-rgb.service || true
    echo '=== OpenRGB instances (expected: 1) ==='
    pgrep -afu "$(id -u)" '([o]penrgb|[O]penRGB)' || true
    echo '=== OpenRGB TCP listener (check against configured port) ==='
    ss -ltnp | grep -i openrgb || true
    echo '=== User lingering ==='
    loginctl show-user "$(id -un)" -p Linger || true
    echo '=== Last 30 log lines ==='
    journalctl --user -b -u openrgb-local.service -u thermal-rgb.service --no-pager -n 30 || true
    ;;
  *) echo 'Usage: ./manage.sh {start|stop|restart|status|logs|config|doctor|sensors|diagnostics}' >&2; exit 2 ;;
esac
