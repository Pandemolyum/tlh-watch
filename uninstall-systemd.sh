#!/usr/bin/env bash
#
# Remove the Last Hunt watcher systemd service.
#
#   ./uninstall-systemd.sh              # remove the user service
#   sudo ./uninstall-systemd.sh --system  # remove the system service

set -euo pipefail

SERVICE_NAME="tlhwatch"
MODE="user"

for arg in "$@"; do
    case "$arg" in
        --system) MODE="system" ;;
        --user)   MODE="user" ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [[ "$MODE" == "system" ]]; then
    [[ $EUID -eq 0 ]] || { echo "error: --system needs root." >&2; exit 1; }
    SYSTEMCTL=(systemctl)
    UNIT_PATH="/etc/systemd/system/$SERVICE_NAME.service"
else
    SYSTEMCTL=(systemctl --user)
    UNIT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_NAME.service"
fi

if [[ ! -f "$UNIT_PATH" ]]; then
    echo "No $MODE service installed at $UNIT_PATH"
    exit 0
fi

"${SYSTEMCTL[@]}" disable --now "$SERVICE_NAME.service" || true
rm -f "$UNIT_PATH"
"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" reset-failed 2>/dev/null || true

echo "Removed $UNIT_PATH"
