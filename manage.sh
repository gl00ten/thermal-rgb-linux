#!/usr/bin/env bash
set -euo pipefail
config="${XDG_CONFIG_HOME:-$HOME/.config}/thermal-rgb/config.toml"
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$HOME/.local/share/thermal-rgb/.venv/bin/python"
inspect() {
  if [[ ! -x "$python" ]]; then
    echo 'Not installed. Run ./install.sh first.' >&2
    return 1
  fi
  "$python" "$root/thermal_rgb.py" --config "$config" "$@"
}
case "${1:-}" in
  start) systemctl --user start openrgb-local.service thermal-rgb.service ;;
  stop) systemctl --user stop thermal-rgb.service openrgb-local.service ;;
  restart) systemctl --user restart openrgb-local.service thermal-rgb.service ;;
  status) systemctl --user --no-pager status openrgb-local.service thermal-rgb.service ;;
  logs) journalctl --user -b -u openrgb-local.service -u thermal-rgb.service -f ;;
  config) "${EDITOR:-nano}" "$config" ;;
  sensors) inspect --list-sensors ;;
  devices) inspect --list-devices ;;
  diagnostics)
    systemctl --user kill --kill-whom=main --signal=USR1 thermal-rgb.service
    echo 'Summary appears on the next sample (within sensor_interval):'
    journalctl --user -u thermal-rgb.service --no-pager -n 10
    ;;
  doctor)
    failed=0
    for unit in openrgb-local.service thermal-rgb.service; do
      if systemctl --user is-active --quiet "$unit"; then
        echo "OK: $unit running"
      else
        echo "STOPPED: $unit — run ./manage.sh start (or ./install.sh if missing)"
        failed=1
      fi
      if ! systemctl --user is-enabled --quiet "$unit"; then
        echo "DISABLED: $unit — run ./install.sh to enable startup at login"
        failed=1
      fi
    done
    if ! cmp -s "$root/thermal_rgb.py" "$HOME/.local/share/thermal-rgb/thermal_rgb.py"; then
      echo 'OUTDATED: installed app differs from this checkout — run ./install.sh'
      failed=1
    fi
    inspect --check-devices || failed=1
    echo 'Recent logs (follow with ./manage.sh logs):'
    journalctl --user -b -u openrgb-local.service -u thermal-rgb.service --no-pager -n 10 || true
    exit "$failed"
    ;;
  *) echo 'Usage: ./manage.sh {start|stop|restart|status|logs|config|doctor|sensors|devices|diagnostics}' >&2; exit 2 ;;
esac
