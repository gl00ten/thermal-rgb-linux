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
  doctor)
    echo '=== Services ==='
    systemctl --user --no-pager status openrgb-local.service thermal-rgb.service || true
    echo '=== Startup ==='
    systemctl --user is-enabled openrgb-local.service thermal-rgb.service || true
    echo '=== OpenRGB instances (expected: 1) ==='
    pgrep -afu "$(id -u)" '([o]penrgb|[O]penRGB)' || true
    echo '=== TCP listener (expected: 127.0.0.1:6742) ==='
    ss -ltnp | grep ':6742' || true
    echo '=== User lingering ==='
    loginctl show-user "$(id -un)" -p Linger || true
    echo '=== Last 30 log lines ==='
    journalctl --user -b -u openrgb-local.service -u thermal-rgb.service --no-pager -n 30 || true
    ;;
  *) echo 'Usage: ./manage.sh {start|stop|restart|status|logs|config|doctor}' >&2; exit 2 ;;
esac
