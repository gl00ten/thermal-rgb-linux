#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${HOME}/.local/share/thermal-rgb"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/thermal-rgb"
UNITS="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
BOOT=false

usage() {
  cat <<'HELP'
Usage: ./install.sh [--boot]

Installs a local Python venv, retains your config, and enables two systemd user
services. Default: start at user login. --boot additionally enables systemd
user lingering for startup before login (requires sudo / device permissions).
HELP
}
for arg in "$@"; do
  case "$arg" in
    --boot) BOOT=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$(id -u)" -ne 0 ]] || { echo 'Do not run this installer as root / with sudo.' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'systemd is required.' >&2; exit 1; }

missing=()
command -v python3 >/dev/null || missing+=(python)
if ! command -v openrgb >/dev/null && ! command -v OpenRGB >/dev/null; then missing+=(openrgb); fi
if ((${#missing[@]})); then
  command -v pacman >/dev/null || {
    echo "Install Python 3.11+ (with venv support) and OpenRGB using your distro, then rerun ./install.sh." >&2
    exit 1
  }
  echo "Installing missing packages with pacman: ${missing[*]}"
  sudo pacman -S --needed "${missing[@]}"
fi
command -v nvidia-smi >/dev/null || {
  echo 'Missing nvidia-smi. Install a working NVIDIA driver before installing (do not blindly install nvidia-utils).' >&2
  exit 1
}
OPENRGB_BIN="$(command -v openrgb || command -v OpenRGB)"
[[ "$OPENRGB_BIN" = /* ]] || { echo 'OpenRGB binary path must be absolute.' >&2; exit 1; }

# We own only these two user service names. Stop before replacing them.
systemctl --user stop thermal-rgb.service openrgb-local.service 2>/dev/null || true

# Never kill a user GUI or second OpenRGB daemon automatically: they may have
# unsaved settings. Refuse the install until it is closed to avoid USB/I2C races.
for attempt in 1 2 3 4 5; do
  if ! pgrep -u "$(id -u)" -x openrgb >/dev/null && ! pgrep -u "$(id -u)" -x OpenRGB >/dev/null; then break; fi
  sleep 0.3
done
if pgrep -u "$(id -u)" -x openrgb >/dev/null || pgrep -u "$(id -u)" -x OpenRGB >/dev/null; then
  echo 'ERROR: Another OpenRGB process is running. Close the GUI / manual server first.' >&2
  pgrep -afu "$(id -u)" '([o]penrgb|[O]penRGB)' >&2 || true
  echo 'Then rerun ./install.sh. No process was killed.' >&2
  exit 1
fi
if systemctl is-active --quiet openrgb.service 2>/dev/null || systemctl --user is-active --quiet openrgb.service 2>/dev/null || \
   systemctl is-enabled --quiet openrgb.service 2>/dev/null || systemctl --user is-enabled --quiet openrgb.service 2>/dev/null; then
  echo 'ERROR: A different openrgb.service is running or enabled; stop and disable it to avoid competing controllers on reboot.' >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$UNITS"
install -m 644 "$ROOT/thermal_rgb.py" "$APP_DIR/thermal_rgb.py"
if [[ ! -e "$CONFIG_DIR/config.toml" ]]; then
  install -m 600 "$ROOT/config.example.toml" "$CONFIG_DIR/config.toml"
  echo "Created configuration: $CONFIG_DIR/config.toml"
else
  echo "Preserving existing configuration: $CONFIG_DIR/config.toml"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
"$APP_DIR/.venv/bin/python" "$APP_DIR/thermal_rgb.py" --check-config --config "$CONFIG_DIR/config.toml"

# Preserve any previous manually written service files before replacing them.
stamp="$(date +%Y%m%d-%H%M%S)"
for name in openrgb-local.service thermal-rgb.service; do
  if [[ -f "$UNITS/$name" ]]; then
    cp -p "$UNITS/$name" "$UNITS/$name.backup-$stamp"
    echo "Backed up previous $name"
  fi
done
sed "s|@OPENRGB_BINARY@|$OPENRGB_BIN|g" "$ROOT/systemd/openrgb-local.service.in" > "$UNITS/openrgb-local.service"
install -m 644 "$ROOT/systemd/thermal-rgb.service" "$UNITS/thermal-rgb.service"

# Warn about desktop autostart: user GUI must not scan/control the same hardware.
for f in "${XDG_CONFIG_HOME:-${HOME}/.config}/autostart"/*[Oo]pen[Rr][Gg][Bb]*.desktop; do
  if [[ -f "$f" ]] && ! grep -qi '^Hidden=true' "$f"; then
    echo "WARNING: Desktop OpenRGB autostart may cause a duplicate controller: $f" >&2
  fi
done

systemctl --user daemon-reload
systemctl --user enable --now openrgb-local.service thermal-rgb.service

if [[ "$BOOT" = true ]]; then
  sudo loginctl enable-linger "$(id -un)"
  echo 'Pre-login systemd user service startup enabled (linger=yes).'
fi

echo
echo 'Installed!'
echo "Configuration: $CONFIG_DIR/config.toml"
echo 'Control:       ./manage.sh status | logs | stop | start | restart | doctor'
echo 'Edit config, then restart: ./manage.sh restart'
echo 'Default: starts at login. For pre-login startup, rerun ./install.sh --boot.'
echo 'If lights stay off, check: ./manage.sh logs'
